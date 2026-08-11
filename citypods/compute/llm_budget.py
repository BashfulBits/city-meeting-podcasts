"""CAS-backed quota and soft cost ledger for scheduled LLM routes."""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from citypods.compute.budget import cycle_key
from citypods.compute.llm_policy import LLMRoute

LLM_BUDGET_STATE_KEY = "state/llm_budget.json"
# Dispatch records are reaped by the Worker after its 20-minute processing timeout.  Use the
# same horizon locally: a lost handle must eventually free its physical-route reservation, but a
# normal queued request has ample time to be reconciled before that happens.
LLM_RESERVATION_TTL_SECONDS = 20 * 60


@dataclass
class LLMReservation:
    cost: float
    requests: int
    tokens: int
    # The window keys the ledger was rolled to at reserve time. ``settle``/``release`` compare
    # these against the *current* keys before correcting a running counter: a rollover between
    # reserve and settle/release already zeroed that counter, so re-applying a delta computed
    # against the old reservation would corrupt an unrelated window (and can drive it negative).
    minute_key: str = ""
    day_key: str = ""
    cost_cycle_key: str = ""
    cost_day_key: str = ""
    # TPM is modeled as an average token rate. These fields let release/settle restore or
    # correct the reservation's position in that schedule without disturbing later reservations.
    tokens_available_at_before: str = ""
    tokens_available_at_after: str = ""
    tokens_minute_before: int = 0
    reserved_at: str = ""
    expires_at: str = ""


@dataclass
class RouteLedger:
    cost_used: float = 0.0
    cost_cycle_key: str = ""
    cost_day_used: float = 0.0
    cost_day_key: str = ""
    requests_minute: int = 0
    requests_minute_key: str = ""
    requests_day: int = 0
    requests_day_key: str = ""
    tokens_minute: int = 0
    # End of the currently reserved token-rate schedule. A request may be larger than one
    # minute's TPM; it pushes this point farther into the future.
    tokens_available_at: str = ""
    inflight: dict[str, LLMReservation] = field(default_factory=dict)
    # ISO datetime, UTC; "" means not blocked. Set by a real provider 429/rate-limit response
    # (`LLMBudget.block`), independent of and in addition to our own proactive RPM/RPD/TPM
    # estimate -- our counters can be wrong (shared quota, an unmodeled monthly pool), and a live
    # signal from the provider overrides them regardless of what we predicted. Never reset by
    # `_ledger()`'s window rollovers; only `block()` changes it.
    blocked_until: str = ""

    @property
    def inflight_count(self) -> int:
        return len(self.inflight)


@dataclass
class LLMBudget:
    routes: dict[str, RouteLedger] = field(default_factory=dict)

    @staticmethod
    def route_key(model: str, route: LLMRoute) -> str:
        """Physical route key shared with the Worker ledger; old hand-built routes use model."""
        return route.route_id or model

    def _ledger(
        self, model: str, now: datetime, *, route: LLMRoute, create: bool = True
    ) -> RouteLedger | None:
        physical_key = self.route_key(model, route)
        # A prior Python ledger used logical model names before the provider/account catalog
        # gained physical route IDs. Promote that one legacy entry in-memory on first access so
        # old ephemeral state remains usable without maintaining two accounting shapes.
        legacy_key = route.model
        if (
            physical_key != legacy_key
            and physical_key not in self.routes
            and legacy_key in self.routes
        ):
            self.routes[physical_key] = self.routes.pop(legacy_key)
        led = self.routes.get(physical_key)
        if led is None:
            if not create:
                return None
            led = RouteLedger()
            self.routes[physical_key] = led
        mk = minute_key(now)
        if led.requests_minute_key != mk:
            led.requests_minute = 0
            if not led.tokens_available_at:
                led.tokens_minute = 0
            led.requests_minute_key = mk
        if route.quota.rpd is not None:
            dk = daily_reset_key(now, route.quota.reset_timezone)
            if led.requests_day_key != dk:
                led.requests_day = 0
                led.requests_day_key = dk
        if route.quota.tpm is not None and led.tokens_available_at:
            if datetime.fromisoformat(led.tokens_available_at) <= now.astimezone(UTC):
                # An oversized request's token debt has drained. Start a fresh burst budget.
                led.tokens_available_at = ""
                led.tokens_minute = 0
        ck = cycle_key(now)
        if led.cost_cycle_key != ck:
            led.cost_used = 0.0
            led.cost_cycle_key = ck
        # Price ceilings are normally monthly, but cautious paid experiments can opt into a
        # stricter per-day cap using the route's own quota reset timezone.
        if route.pricing.daily_cost_cap is not None:
            cost_day_key = daily_reset_key(now, route.quota.reset_timezone)
            if led.cost_day_key != cost_day_key:
                led.cost_day_used = 0.0
                led.cost_day_key = cost_day_key
        for owner, reservation in list(led.inflight.items()):
            if not reservation.expires_at:
                continue
            try:
                expiry = datetime.fromisoformat(reservation.expires_at)
                expired = expiry.tzinfo is None or expiry <= now.astimezone(UTC)
            except (TypeError, ValueError):
                expired = True
            if expired:
                del led.inflight[owner]
                self._unwind_reservation(led, reservation)
        return led

    @staticmethod
    def _unwind_reservation(led: RouteLedger, reservation: LLMReservation) -> None:
        """Undo a reservation without applying usage.

        This is shared by explicit release and expiry reaping.  The reservation's recorded
        window keys, rather than today's route configuration, determine which counters were
        originally charged.  That also makes cleanup safe after a route's quota is edited or the
        route is retired between dispatch and reconciliation.
        """
        if led.cost_cycle_key == reservation.cost_cycle_key:
            led.cost_used = max(0.0, led.cost_used - reservation.cost)
        if reservation.cost_day_key and led.cost_day_key == reservation.cost_day_key:
            led.cost_day_used = max(0.0, led.cost_day_used - reservation.cost)
        if led.requests_minute_key == reservation.minute_key:
            led.requests_minute = max(0, led.requests_minute - reservation.requests)
            led.tokens_minute = max(0, led.tokens_minute - reservation.tokens)
        if (
            reservation.tokens_available_at_after
            and led.tokens_available_at == reservation.tokens_available_at_after
        ):
            led.tokens_available_at = reservation.tokens_available_at_before
        if reservation.day_key and led.requests_day_key == reservation.day_key:
            led.requests_day = max(0, led.requests_day - reservation.requests)

    def available(
        self,
        model: str,
        *,
        route: LLMRoute,
        requests: int,
        tokens: int,
        cost: float,
        now: datetime,
    ) -> bool:
        led = self._ledger(model, now, route=route, create=False)
        if led is None:
            return True
        if led.blocked_until and now.astimezone(UTC) < datetime.fromisoformat(led.blocked_until):
            return False
        quota = route.quota
        pricing = route.pricing
        return (
            (quota.rpm is None or led.requests_minute + requests <= quota.rpm)
            and (quota.rpd is None or led.requests_day + requests <= quota.rpd)
            and (
                quota.tpm is None
                or not led.tokens_available_at
                or now.astimezone(UTC) >= datetime.fromisoformat(led.tokens_available_at)
            )
            and (quota.concurrency is None or led.inflight_count < quota.concurrency)
            and (pricing.cost_cap is None or led.cost_used + cost <= pricing.cost_cap)
            and (
                pricing.daily_cost_cap is None or led.cost_day_used + cost <= pricing.daily_cost_cap
            )
        )

    def block(self, model: str, until: datetime, *, route: LLMRoute, now: datetime) -> None:
        """Reactively mark ``model`` unavailable until ``until`` -- called after a real 429/
        rate-limit response, overriding our own proactive estimate rather than retrying into the
        same error. Never moves an existing block *earlier* (the longer of the two wins), since a
        second 429 arriving after the first block was already set is itself new information."""
        led = self._ledger(model, now, route=route)
        assert led is not None
        current = datetime.fromisoformat(led.blocked_until) if led.blocked_until else None
        if current is None or until > current:
            led.blocked_until = until.astimezone(UTC).isoformat()

    def find_inflight_owner(self, owner: str) -> str | None:
        """The physical route key ``owner`` currently has a reservation under, if any.
        A dispatch-mode retry under the same (deterministic, recipe_hash-derived) owner must
        resolve to the *same* model it originally reserved -- the Worker's own idempotency-key
        dedup still resolves to that original request regardless of what a fresh selection pass
        would pick this time."""
        for model, ledger in self.routes.items():
            if owner in ledger.inflight:
                return model
        return None

    def reserve(
        self,
        owner: str,
        model: str,
        *,
        route: LLMRoute,
        requests: int,
        tokens: int,
        cost: float,
        now: datetime,
    ) -> None:
        """Reserve capacity for ``owner``. Idempotent: a caller that retries the same logical
        dispatch under the same owner (e.g. ``owner`` derived from a stable ``recipe_hash``, per
        R13) before its first reservation settles must not have that retry double-counted against
        quota — the provider-facing request has not actually been sent twice."""
        led = self._ledger(model, now, route=route)
        assert led is not None
        if owner in led.inflight:
            return
        led.cost_used += cost
        if route.pricing.daily_cost_cap is not None:
            led.cost_day_used += cost
        tokens_minute_before = led.tokens_minute
        led.requests_minute += requests
        led.tokens_minute += tokens
        if route.quota.rpd is not None:
            led.requests_day += requests
        reserved_at = now.astimezone(UTC)
        token_schedule_before = led.tokens_available_at
        if route.quota.tpm is not None:
            total_tokens = tokens_minute_before + tokens
            if not token_schedule_before and total_tokens > route.quota.tpm:
                token_schedule_after = (
                    reserved_at + timedelta(seconds=total_tokens * 60 / route.quota.tpm)
                ).isoformat()
                led.tokens_available_at = token_schedule_after
            else:
                token_schedule_after = token_schedule_before
        else:
            token_schedule_after = token_schedule_before
        led.inflight[owner] = LLMReservation(
            cost=cost,
            requests=requests,
            tokens=tokens,
            minute_key=led.requests_minute_key,
            day_key=led.requests_day_key,
            cost_cycle_key=led.cost_cycle_key,
            cost_day_key=led.cost_day_key,
            tokens_available_at_before=token_schedule_before,
            tokens_available_at_after=token_schedule_after,
            tokens_minute_before=tokens_minute_before,
            reserved_at=reserved_at.isoformat(),
            expires_at=(reserved_at + timedelta(seconds=LLM_RESERVATION_TTL_SECONDS)).isoformat(),
        )

    def settle(
        self,
        owner: str,
        model: str,
        *,
        route: LLMRoute,
        now: datetime,
        actual_requests: int | None = None,
        actual_tokens: int | None = None,
        actual_cost: float | None = None,
    ) -> None:
        led = self._ledger(model, now, route=route)
        assert led is not None
        reservation = led.inflight.pop(owner, None)
        if reservation is None:
            return
        if actual_requests is not None and led.requests_minute_key == reservation.minute_key:
            led.requests_minute = max(
                0, led.requests_minute + actual_requests - reservation.requests
            )
        if (
            actual_requests is not None
            and reservation.day_key
            and led.requests_day_key == reservation.day_key
        ):
            led.requests_day = max(0, led.requests_day + actual_requests - reservation.requests)
        if actual_tokens is not None and led.requests_minute_key == reservation.minute_key:
            led.tokens_minute = max(0, led.tokens_minute + actual_tokens - reservation.tokens)
        if (
            actual_tokens is not None
            and route.quota.tpm is not None
            and reservation.tokens_available_at_after
            and led.tokens_available_at == reservation.tokens_available_at_after
        ):
            token_start = datetime.fromisoformat(reservation.reserved_at).astimezone(UTC)
            led.tokens_available_at = (
                token_start
                + timedelta(
                    seconds=(reservation.tokens_minute_before + actual_tokens)
                    * 60
                    / route.quota.tpm
                )
            ).isoformat()
        if actual_cost is not None and led.cost_cycle_key == reservation.cost_cycle_key:
            led.cost_used = max(0.0, led.cost_used + actual_cost - reservation.cost)
        if (
            actual_cost is not None
            and reservation.cost_day_key
            and led.cost_day_key == reservation.cost_day_key
        ):
            led.cost_day_used = max(0.0, led.cost_day_used + actual_cost - reservation.cost)

    def release(self, owner: str, model: str, *, route: LLMRoute, now: datetime) -> None:
        led = self._ledger(model, now, route=route)
        assert led is not None
        reservation = led.inflight.pop(owner, None)
        if reservation is None:
            return
        self._unwind_reservation(led, reservation)

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "routes": {
                model: {
                    "cost_used": ledger.cost_used,
                    "cost_cycle_key": ledger.cost_cycle_key,
                    "cost_day_used": ledger.cost_day_used,
                    "cost_day_key": ledger.cost_day_key,
                    "requests_minute": ledger.requests_minute,
                    "requests_minute_key": ledger.requests_minute_key,
                    "requests_day": ledger.requests_day,
                    "requests_day_key": ledger.requests_day_key,
                    "tokens_minute": ledger.tokens_minute,
                    "tokens_available_at": ledger.tokens_available_at or None,
                    "blocked_until": ledger.blocked_until or None,
                    "inflight": {
                        owner: {
                            "cost": reservation.cost,
                            "requests": reservation.requests,
                            "tokens": reservation.tokens,
                            "minute_key": reservation.minute_key,
                            "day_key": reservation.day_key,
                            "cost_cycle_key": reservation.cost_cycle_key,
                            "cost_day_key": reservation.cost_day_key,
                            "tokens_available_at_before": reservation.tokens_available_at_before
                            or None,
                            "tokens_available_at_after": reservation.tokens_available_at_after
                            or None,
                            "tokens_minute_before": reservation.tokens_minute_before,
                            "reserved_at": reservation.reserved_at or None,
                            "expires_at": reservation.expires_at or None,
                        }
                        for owner, reservation in ledger.inflight.items()
                    },
                }
                for model, ledger in self.routes.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> LLMBudget:
        routes: dict[str, RouteLedger] = {}
        for model, raw in (data.get("routes") or {}).items():
            if not isinstance(raw, dict):
                continue
            inflight = {}
            for owner, value in (raw.get("inflight") or {}).items():
                if isinstance(value, dict):
                    inflight[str(owner)] = LLMReservation(
                        cost=float(value.get("cost", 0.0)),
                        requests=int(value.get("requests", 0)),
                        tokens=int(value.get("tokens", 0)),
                        minute_key=str(value.get("minute_key") or ""),
                        day_key=str(value.get("day_key") or ""),
                        cost_cycle_key=str(value.get("cost_cycle_key") or ""),
                        cost_day_key=str(value.get("cost_day_key") or ""),
                        tokens_available_at_before=str(
                            value.get("tokens_available_at_before") or ""
                        ),
                        tokens_available_at_after=str(value.get("tokens_available_at_after") or ""),
                        tokens_minute_before=int(value.get("tokens_minute_before", 0)),
                        reserved_at=str(value.get("reserved_at") or ""),
                        expires_at=str(value.get("expires_at") or ""),
                    )
            routes[str(model)] = RouteLedger(
                cost_used=float(raw.get("cost_used", 0.0)),
                cost_cycle_key=str(raw.get("cost_cycle_key") or ""),
                cost_day_used=float(raw.get("cost_day_used", 0.0)),
                cost_day_key=str(raw.get("cost_day_key") or ""),
                requests_minute=int(raw.get("requests_minute", 0)),
                requests_minute_key=str(raw.get("requests_minute_key") or ""),
                requests_day=int(raw.get("requests_day", 0)),
                requests_day_key=str(raw.get("requests_day_key") or ""),
                tokens_minute=int(raw.get("tokens_minute", 0)),
                tokens_available_at=str(raw.get("tokens_available_at") or ""),
                blocked_until=str(raw.get("blocked_until") or ""),
                inflight=inflight,
            )
        return cls(routes=routes)


def minute_key(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M")


def daily_reset_key(now: datetime, tz_name: str) -> str:
    return now.astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d")


def serialize_llm_budget(budget: LLMBudget) -> bytes:
    """The one canonical on-disk encoding for :class:`LLMBudget` — callers outside this module
    (the scheduler's ``select_and_reserve`` CAS loop) must use this rather than re-encoding
    ``to_dict()`` themselves, so the two can never silently drift out of sync."""
    return (json.dumps(budget.to_dict(), indent=2, sort_keys=True) + "\n").encode()


def load_llm_budget_cas(storage) -> tuple[LLMBudget, str | None]:
    got = storage.get_bytes(LLM_BUDGET_STATE_KEY)
    if got is None:
        return LLMBudget(), None
    data, etag = got
    try:
        parsed = json.loads(data)
        return LLMBudget.from_dict(parsed if isinstance(parsed, dict) else {}), etag
    except (AttributeError, TypeError, ValueError):
        return LLMBudget(), etag


def mutate_llm_budget(
    storage,
    mutate,
    *,
    now: datetime | None = None,
    max_attempts: int = 8,
    base_sleep: float = 0.05,
    max_sleep: float = 1.0,
    sleep=time.sleep,
    rng: random.Random | None = None,
) -> LLMBudget:
    """Atomic read-modify-write via CAS. ``mutate`` is ``Callable[[LLMBudget, datetime], None]``:
    it receives an **attempt-local** timestamp, not a value fixed before the retry loop starts.

    A conflict means a sibling writer committed first — possibly advancing the ledger's
    minute/day/cost-cycle window in the process. Reusing a timestamp computed before that retry
    would make ``_ledger()`` see an *older* window key than the one already on the freshly-loaded
    ledger and roll it backward, zeroing counts the winning writer had already correctly recorded.
    So when the caller didn't pin an explicit ``now`` (i.e. it's absent, not merely falsy-at-call-
    time), each attempt gets a fresh ``datetime.now(UTC)`` reflecting real elapsed backoff time.
    An explicitly supplied ``now`` is honored unchanged on every attempt, for deterministic tests.
    """
    from citypods.storage import CASConflict

    rng = rng or random
    last: CASConflict | None = None
    for attempt in range(max_attempts):
        attempt_now = now if now is not None else datetime.now(UTC)
        budget, etag = load_llm_budget_cas(storage)
        mutate(budget, attempt_now)
        try:
            if etag is None:
                storage.put_cas(
                    LLM_BUDGET_STATE_KEY,
                    serialize_llm_budget(budget),
                    "application/json",
                    if_none_match="*",
                )
            else:
                storage.put_cas(
                    LLM_BUDGET_STATE_KEY,
                    serialize_llm_budget(budget),
                    "application/json",
                    if_match=etag,
                )
            return budget
        except CASConflict as exc:
            last = exc
            sleep(min(base_sleep * 2**attempt, max_sleep) * (0.5 + rng.random()))
    assert last is not None
    raise last


def settle_route_reservation(
    storage,
    owner: str,
    model: str,
    *,
    route: LLMRoute,
    now: datetime | None = None,
    actual_tokens: int | None = None,
    actual_cost: float | None = None,
    actual_requests: int | None = None,
    max_attempts: int = 8,
    base_sleep: float = 0.05,
    max_sleep: float = 1.0,
    sleep=time.sleep,
    rng: random.Random | None = None,
) -> LLMBudget:
    return mutate_llm_budget(
        storage,
        lambda budget, attempt_now: budget.settle(
            owner,
            model,
            route=route,
            now=attempt_now,
            actual_requests=actual_requests,
            actual_tokens=actual_tokens,
            actual_cost=actual_cost,
        ),
        now=now,
        max_attempts=max_attempts,
        base_sleep=base_sleep,
        max_sleep=max_sleep,
        sleep=sleep,
        rng=rng,
    )


def release_route_reservation(
    storage,
    owner: str,
    model: str,
    *,
    route: LLMRoute,
    now: datetime | None = None,
    max_attempts: int = 8,
    base_sleep: float = 0.05,
    max_sleep: float = 1.0,
    sleep=time.sleep,
    rng: random.Random | None = None,
) -> LLMBudget:
    return mutate_llm_budget(
        storage,
        lambda budget, attempt_now: budget.release(owner, model, route=route, now=attempt_now),
        now=now,
        max_attempts=max_attempts,
        base_sleep=base_sleep,
        max_sleep=max_sleep,
        sleep=sleep,
        rng=rng,
    )


def block_route_until(
    storage,
    model: str,
    until: datetime,
    *,
    route: LLMRoute,
    now: datetime | None = None,
    max_attempts: int = 8,
    base_sleep: float = 0.05,
    max_sleep: float = 1.0,
    sleep=time.sleep,
    rng: random.Random | None = None,
) -> LLMBudget:
    return mutate_llm_budget(
        storage,
        lambda budget, attempt_now: budget.block(model, until, route=route, now=attempt_now),
        now=now,
        max_attempts=max_attempts,
        base_sleep=base_sleep,
        max_sleep=max_sleep,
        sleep=sleep,
        rng=rng,
    )


__all__ = [
    "LLM_BUDGET_STATE_KEY",
    "LLMBudget",
    "LLMReservation",
    "RouteLedger",
    "block_route_until",
    "daily_reset_key",
    "load_llm_budget_cas",
    "minute_key",
    "mutate_llm_budget",
    "release_route_reservation",
    "serialize_llm_budget",
    "settle_route_reservation",
]
