"""Pure LLM route selection plus CAS-backed selection-and-reservation."""

from __future__ import annotations

import math
import random
import time
import uuid
from collections.abc import Mapping, Set
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from citypods.compute.llm_budget import (
    LLM_BUDGET_STATE_KEY,
    LLMBudget,
    load_llm_budget_cas,
    serialize_llm_budget,
)
from citypods.compute.llm_policy import ROUTES, LLMRequestPolicy, LLMRoute


@dataclass(frozen=True)
class SelectionResult:
    model: str | None
    route: LLMRoute | None
    reason: str
    rejected: tuple[tuple[str, str], ...] = ()
    # The ledger reservation owner this selection reserved under (or is reusing), or `None` when
    # nothing was selected. Callers settle/release against this, not a value they precomputed --
    # owner uniqueness depends on the *selected* route's transport (see `select_and_reserve`),
    # which isn't known until selection completes.
    owner: str | None = None
    # When nothing was selectable *now*, the earliest UTC time an allowed route is predicted to
    # become eligible again (the soonest per-minute window rollover, daily quota reset, or the end
    # of a real-429 block across the allowed routes), or `None` if nothing will free up. A pacing
    # caller (see `LiteLLMBackend._run_policy_job`) sleeps until this instead of deferring to a
    # future run, so a single run can drain its full daily quota across successive minute windows
    # rather than bursting one window's worth and stopping. `None` when a route *was* selected.
    retry_at: datetime | None = None
    # Exact capacity reserved for the selected route. Direct structured calls reserve their
    # potential corrective retry; a queued Worker route reserves its single upstream attempt.
    reserved_requests: int | None = None
    reserved_tokens: int | None = None
    # The single transport this call will actually use for the selected route, resolved once here
    # -- the sole source of truth `_owner_for` (below) and `llm.py`'s dispatch-vs-direct branch
    # both read, rather than each independently re-deriving it from `route.transports` and the
    # caller's policy/config. A route offering only dispatch transports (Mistral) always resolves
    # to that transport; a route that also offers `direct` (Gemini) resolves to `direct` unless
    # the caller set `allow_dispatch_overflow=True`. `None` when nothing was selected.
    transport: str | None = None


def _reservation_size(route: LLMRoute, *, requests: int, tokens: int) -> tuple[int, int]:
    """Return this route's bounded provider reservation from a caller-wide worst case.

    ``tokens`` is the total for ``requests`` equal-sized possible attempts. A dispatch Worker
    has no local corrective retry, so its route can safely reserve one attempt while direct
    routes keep the caller's two-attempt structured-output reservation.
    """
    route_requests = min(requests, route.max_provider_attempts or requests)
    per_attempt_tokens = max(1, math.ceil(tokens / requests))
    return route_requests, per_attempt_tokens * route_requests


def _window_bounds(window, now: datetime) -> tuple[datetime, datetime] | None:
    try:
        zone = ZoneInfo(window.tz)
    except Exception:
        return None
    local = now.astimezone(zone)
    if window.end > window.start:
        start = datetime.combine(local.date(), window.start, tzinfo=zone)
        end = datetime.combine(local.date(), window.end, tzinfo=zone)
        if local >= end:
            start += timedelta(days=1)
            end += timedelta(days=1)
    elif local.timetz().replace(tzinfo=None) < window.end:
        start = datetime.combine(local.date() - timedelta(days=1), window.start, tzinfo=zone)
        end = datetime.combine(local.date(), window.end, tzinfo=zone)
    else:
        start = datetime.combine(local.date(), window.start, tzinfo=zone)
        end = datetime.combine(local.date() + timedelta(days=1), window.end, tzinfo=zone)
    return start.astimezone(UTC), end.astimezone(UTC)


def _active_multiplier(route: LLMRoute, now: datetime) -> float:
    for window in route.pricing.windows:
        bounds = _window_bounds(window, now)
        if bounds is None:
            continue
        start, end = bounds
        if start <= now.astimezone(UTC) < end:
            return window.multiplier
    return 1.0


def _next_discount_window_end(route: LLMRoute, now: datetime) -> datetime | None:
    candidates = []
    for window in route.pricing.windows:
        if window.multiplier >= 1:
            continue
        bounds = _window_bounds(window, now)
        if bounds is not None:
            start, end = bounds
            if start > now.astimezone(UTC):
                candidates.append((start, end))
            elif end > now.astimezone(UTC):
                return end
    return min((end for _, end in candidates), default=None)


def _next_local_midnight(reset_timezone: str, now: datetime) -> datetime:
    """The start of the next calendar day in ``reset_timezone``, as a UTC instant -- shared by
    every axis in ``_next_quota_reset`` that resets on that same daily boundary (RPD, and the
    route's ``daily_cost_cap``), so the two can't drift out of sync with each other."""
    zone = ZoneInfo(reset_timezone)
    local = now.astimezone(zone)
    tomorrow = local.date() + timedelta(days=1)
    return datetime.combine(tomorrow, datetime.min.time(), tzinfo=zone).astimezone(UTC)


def _next_quota_reset(
    route: LLMRoute, ledger, now: datetime, *, requests: int, tokens: int, cost: float
) -> datetime:
    """The soonest time an axis actually keeping ``model`` unavailable right now will reset.

    Only offers a candidate for an axis genuinely responsible for the current ``available()``
    failure -- checking ``ledger.requests_minute_key == minute_key(now)`` (true on nearly every
    call, since merely checking availability stamps that key for the rest of the minute)
    previously offered "next minute" as a candidate unconditionally, even when RPM/TPM were
    nowhere near their cap and the real blocker was RPD. `min()` would then pick that bogus
    near-immediate time over the real tomorrow reset, so a paced caller would busy-retry every
    few seconds for the rest of the day instead of correctly giving up until the real reset --
    for a caller that never gives up on `None` (`LiteLLMBackend._run_policy_job_paced`), that can
    burn its *entire* deadline on one route, never reaching whatever else was queued behind it.

    Same reasoning applies to ``blocked_until``: it only ever moves forward (`LLMBudget.block()`
    extends it, never clears it), so a route blocked by an *earlier* real 429 keeps a stale
    timestamp in the ledger long after that block itself expired. `available()` already treats a
    block as in effect only while ``now < blocked_until`` (see its own check) -- this function must
    agree, or a past `blocked_until` wins `min()` over the real (future) axis reset the same way an
    unconditional "next minute" used to, reintroducing the identical busy-retry-forever failure
    mode this function exists to prevent.

    And again for ``pricing.daily_cost_cap``: it resets on the same daily boundary as RPD
    (``daily_reset_key`` in ``llm_budget.py``'s ``_ledger()``), so it gets the same real reset-time
    treatment rather than falling into the imprecise fallback below.
    """
    resets: list[datetime] = []
    quota = route.quota
    pricing = route.pricing
    per_minute_binding = (
        quota.rpm is not None and ledger.requests_minute + requests > quota.rpm
    ) or (quota.tpm is not None and ledger.tokens_minute + tokens > quota.tpm)
    if per_minute_binding:
        current = now.astimezone(UTC)
        resets.append(current.replace(second=0, microsecond=0) + timedelta(minutes=1))
    if quota.rpd is not None and ledger.requests_day + requests > quota.rpd:
        resets.append(_next_local_midnight(quota.reset_timezone, now))
    if pricing.daily_cost_cap is not None and ledger.cost_day_used + cost > pricing.daily_cost_cap:
        resets.append(_next_local_midnight(quota.reset_timezone, now))
    if ledger.blocked_until:
        blocked_until = datetime.fromisoformat(ledger.blocked_until)
        if now.astimezone(UTC) < blocked_until:
            resets.append(blocked_until)
    if not resets:
        # available() failed on an axis this function doesn't model a precise reset for:
        # `concurrency` (an in-flight slot frees on an arbitrary future settle/release, not a
        # clock boundary -- there is no reset time to compute, and polling periodically really is
        # the correct strategy here, not an approximation of one) or the monthly `cost_cap` (no
        # route configures one today, so there's nothing live to get right; add a real prediction
        # -- mirroring `citypods.compute.budget.cycle_key`'s rollover-day anchor -- if that
        # changes). Falls back to the one-minute guess this function used to make unconditionally
        # for every axis, rather than `now`, which would reintroduce the same busy-retry this
        # function exists to prevent for the axes it does model precisely.
        resets.append(now.astimezone(UTC).replace(second=0, microsecond=0) + timedelta(minutes=1))
    return min(resets)


def _estimated_cost(route: LLMRoute, estimated_tokens: int, now: datetime) -> float:
    return (
        estimated_tokens
        * (route.pricing.input_per_token + route.pricing.output_per_token)
        * _active_multiplier(route, now)
    )


def _utilization(route: LLMRoute, ledger_entry) -> float:
    """Fraction of quota already spent on this route's tightest capped axis (0 = empty, 1 = at
    the cap). Used as a tie-break among otherwise-equal candidates so load spreads across routes
    with independent quota pools (e.g. two free same-cost models) instead of always favoring
    whichever sorts first alphabetically -- a route that's already 80% through its per-minute or
    per-day window loses the tie to one that's mostly idle, and the preference flips back once
    usage evens out. Uncapped axes (``None``) don't contribute -- there's nothing to be full of."""
    fractions = []
    if route.quota.rpm:
        fractions.append(ledger_entry.requests_minute / route.quota.rpm)
    if route.quota.rpd:
        fractions.append(ledger_entry.requests_day / route.quota.rpd)
    return max(fractions, default=0.0)


# Transports that dedupe server-side on `idempotency-key: recipe_hash`.
_DISPATCH_TRANSPORTS = frozenset({"mistral-dispatch", "llm-dispatch"})


def _selected_transport(
    route: LLMRoute, available_transports: Set[str], *, allow_dispatch_overflow: bool
) -> str | None:
    """The single transport this call will actually use for ``route`` -- computed once, here, so
    `_owner_for` and `llm.py`'s dispatch-vs-direct branch can both read the *same* resolved value
    instead of each independently re-deriving a preference from `route.transports` and the
    caller's policy/config. Two callers computing this separately was a real bug (CodeRabbit,
    review/41): a fix that made `_owner_for` key off `route.transports` (the route's *capability*)
    rather than the *actually selected* transport still reserved every dual-transport route as if
    it always dispatched, even on a call that (correctly, per `allow_dispatch_overflow=False`)
    went direct -- silently deduping two genuinely concurrent direct Gemini calls that happened to
    share a `recipe_hash` into one reservation, undercounting real API calls.

    `direct` wins whenever it's reachable, *unless* a dispatch transport is also reachable and the
    caller explicitly opted into overflow (`allow_dispatch_overflow=True`) -- in which case the
    dispatch transport wins, since that opt-in's whole purpose is reaching Worker-only capacity a
    direct call can't see (matching `LLMRequestPolicy.allow_dispatch_overflow` §3.3 in review/41).
    Returns `None` if no transport in `route.transports` is reachable at all (the caller's gate 0
    already filters this route out in that case).
    """
    reachable = set(route.transports) & set(available_transports)
    if not reachable:
        return None
    dispatch_reachable = reachable & _DISPATCH_TRANSPORTS
    if dispatch_reachable and (allow_dispatch_overflow or "direct" not in reachable):
        return next(iter(sorted(dispatch_reachable)))
    return "direct"


def _owner_for(recipe_hash: str, transport: str | None) -> str:
    """Owner uniqueness depends on the *selected* transport for this call, not a route's
    capabilities:
    - dispatch (`mistral-dispatch`/`llm-dispatch`): the Worker dedupes on
      `idempotency-key: recipe_hash`, so a retry before this reservation settles is the *same*
      underlying provider request -- owner must be that same deterministic recipe_hash, or it would
      double-reserve quota for a call the Worker holds.
    - `direct` (or `None`, defensively): there is no server-side dedup at all. Every attempt --
      the first, a concurrent call, or a later `reconcile()` retry of a deferred handle -- is a
      genuinely new request and must reserve its own independent slot.
    """
    if transport in _DISPATCH_TRANSPORTS:
        return recipe_hash
    return f"{recipe_hash}:{uuid.uuid4().hex}"


def select_route(
    policy: LLMRequestPolicy,
    *,
    routes: Mapping[str, LLMRoute],
    ledger: LLMBudget,
    available_transports: Set[str],
    estimated_tokens: int,
    requests: int = 1,
    now: datetime,
) -> SelectionResult:
    """Select one eligible route from a read-only ledger snapshot.

    ``available_transports`` is the set of transports *this backend instance* can physically
    reach right now (e.g. ``{"direct"}``, or ``{"direct", "mistral-dispatch"}`` when a dispatch
    Worker is configured) -- not a single fixed mode. A caller able to reach both transports (the
    deferred-request sweep, in particular) needs the scheduler to pick freely among every eligible
    route regardless of which transport backs it.

    ``requests``/``estimated_tokens`` should already reflect the *worst-case* number of provider
    attempts a single logical dispatch can make -- e.g. 2 for a structured call, since Instructor's
    corrective retry can send a second request (see ``llm.py``'s ``run_inference``) -- not just 1.
    """
    rejected: list[tuple[str, str]] = []
    candidates: list[tuple[LLMRoute, float, datetime, float, int, int]] = []
    allowed = set(policy.allowed_models) if policy.allowed_models is not None else None
    # Earliest time an otherwise-allowed route (transport/allowlist/paid gates all passed) that is
    # only *temporarily* unavailable -- per-minute window full, daily quota spent, or under a real
    # 429 block -- is predicted to free up again. A pacing caller sleeps until this rather than
    # deferring the whole request to a future run. Off-peak (discount-window) waits are excluded:
    # a route gated only on a price window is available *now*, just not at its cheapest, so pacing
    # against it would stall a free request for a discount it doesn't need.
    retry_ats: list[datetime] = []

    for model, route in sorted(routes.items()):
        if not any(t in available_transports for t in route.transports):
            rejected.append((model, "transport gate"))
            continue
        if allowed is not None and model not in allowed:
            rejected.append((model, "allowlist gate"))
            continue
        if not policy.allow_paid and not route.free:
            rejected.append((model, "paid model disallowed"))
            continue
        if route.experimental and not policy.allow_experimental:
            rejected.append((model, "experimental route disallowed"))
            continue
        route_requests, route_tokens = _reservation_size(
            route, requests=requests, tokens=estimated_tokens
        )
        cost = _estimated_cost(route, route_tokens, now)
        if not ledger.available(
            model,
            route=route,
            requests=route_requests,
            tokens=route_tokens,
            cost=cost,
            now=now,
        ):
            route_ledger = ledger.routes.get(model)
            if route_ledger is None:
                route_ledger = ledger._ledger(model, now, route=route)
            predicted = _next_quota_reset(
                route,
                route_ledger,
                now,
                requests=route_requests,
                tokens=route_tokens,
                cost=cost,
            )
            retry_ats.append(predicted)
            reason = "quota or budget exhausted"
            if policy.deadline_at is not None and predicted > policy.deadline_at:
                rejected.append((model, f"deadline gate: next eligibility {predicted.isoformat()}"))
            else:
                rejected.append((model, reason))
            continue
        predicted = now
        if policy.deadline_at is not None and predicted > policy.deadline_at:
            rejected.append((model, "deadline gate"))
            continue
        if _active_multiplier(route, now) == 1:
            window_end = _next_discount_window_end(route, now)
            if window_end is not None and (
                policy.deadline_at is None or window_end <= policy.deadline_at
            ):
                rejected.append((model, f"off-peak gate: waits until {window_end.isoformat()}"))
                continue
        # Already fetched (and, if needed, window-rolled) by the `ledger.available(...)` check
        # above, which internally calls the same `_ledger()` -- this is that same entry, not a
        # second lookup.
        candidates.append(
            (
                route,
                cost,
                predicted,
                _utilization(route, ledger.routes[model]),
                route_requests,
                route_tokens,
            )
        )

    if not candidates:
        reason = "no configured LLM route is eligible right now"
        return SelectionResult(
            None, None, reason, tuple(rejected), retry_at=min(retry_ats, default=None)
        )
    route, _, predicted, _, route_requests, route_tokens = min(
        candidates,
        key=lambda item: (not item[0].free, item[1], item[3], item[2], item[0].model),
    )
    rejected.extend(
        (candidate.model, "lower-ranked eligible route")
        for candidate, _, _, _, _, _ in candidates
        if candidate.model != route.model
    )
    return SelectionResult(
        route.model,
        route,
        "selected eligible route",
        tuple(rejected),
        reserved_requests=route_requests,
        reserved_tokens=route_tokens,
        transport=_selected_transport(
            route, available_transports, allow_dispatch_overflow=policy.allow_dispatch_overflow
        ),
    )


def select_and_reserve(
    storage,
    recipe_hash: str,
    policy: LLMRequestPolicy,
    *,
    routes: Mapping[str, LLMRoute] = ROUTES,
    available_transports: Set[str],
    estimated_tokens: int,
    requests: int = 1,
    now: datetime | None = None,
    max_attempts: int = 8,
    base_sleep: float = 0.05,
    max_sleep: float = 1.0,
    sleep=time.sleep,
    rng: random.Random | None = None,
) -> SelectionResult:
    """Select and reserve a route atomically, reselecting after CAS conflicts.

    ``recipe_hash`` identifies the logical request, not a specific ledger owner -- the actual
    reservation owner is derived per attempt from the *selected* route's transport (see
    `_owner_for`), since that isn't known until selection completes.

    ``now``: when the caller doesn't pin an explicit value, each attempt asks for the current
    time again rather than reusing one resolved before the retry loop started -- a conflict means
    a sibling committed first, possibly advancing the ledger's window in the process, and a stale
    timestamp would make ``_ledger()`` roll that window backward. An explicitly supplied ``now``
    is honored unchanged on every attempt, for deterministic tests.
    """
    from citypods.storage import CASConflict

    rng = rng or random
    last_selection = SelectionResult(None, None, "CAS reservation attempts exhausted")
    for attempt in range(max_attempts):
        attempt_now = now if now is not None else datetime.now(UTC)
        ledger, etag = load_llm_budget_cas(storage)
        # A dispatch-mode retry reserves deterministically under `recipe_hash` itself (see
        # `_owner_for`); if that's still inflight from an earlier attempt that hasn't settled,
        # reuse the same route rather than reselecting -- the Worker's idempotency key still
        # resolves to the original request regardless of what a fresh pass would pick now.
        existing_model = ledger.find_inflight_owner(recipe_hash)
        if existing_model is not None and existing_model in routes:
            # `owner == recipe_hash` only ever happens for a dispatch reservation (`_owner_for`
            # returns a fresh UUID for direct) -- so the transport being reused here is, by
            # construction, one of this route's own dispatch transports.
            existing_route = routes[existing_model]
            reused_transport = next(
                (t for t in existing_route.transports if t in _DISPATCH_TRANSPORTS), None
            )
            # If the route's dispatch transport is no longer in `available_transports` (e.g.
            # the Worker was removed from the backend's config since the original reservation),
            # don't return a result with `transport=None` -- fall through to fresh selection
            # which will re-evaluate transport availability normally.
            if reused_transport is not None and reused_transport in available_transports:
                return SelectionResult(
                    existing_model,
                    existing_route,
                    "reusing in-flight reservation",
                    (),
                    owner=recipe_hash,
                    transport=reused_transport,
                )
        selection = select_route(
            policy,
            routes=routes,
            ledger=ledger,
            available_transports=available_transports,
            estimated_tokens=estimated_tokens,
            requests=requests,
            now=attempt_now,
        )
        last_selection = selection
        if selection.route is None:
            return selection
        owner = _owner_for(recipe_hash, selection.transport)
        # No availability recheck here: `select_route` already confirmed `ledger.available(...)`
        # for this exact candidate against this same unmutated `ledger` snapshot when it built
        # `candidates` (§5 gate 3) -- a second check here would always pass and never fires.
        reserved_requests = selection.reserved_requests or requests
        reserved_tokens = selection.reserved_tokens or estimated_tokens
        cost = _estimated_cost(selection.route, reserved_tokens, attempt_now)
        ledger.reserve(
            owner,
            selection.model,
            route=selection.route,
            requests=reserved_requests,
            tokens=reserved_tokens,
            cost=cost,
            now=attempt_now,
        )
        try:
            body = serialize_llm_budget(ledger)
            if etag is None:
                storage.put_cas(LLM_BUDGET_STATE_KEY, body, "application/json", if_none_match="*")
            else:
                storage.put_cas(LLM_BUDGET_STATE_KEY, body, "application/json", if_match=etag)
            return SelectionResult(
                selection.model,
                selection.route,
                selection.reason,
                selection.rejected,
                owner=owner,
                reserved_requests=reserved_requests,
                reserved_tokens=reserved_tokens,
                transport=selection.transport,
            )
        except CASConflict:
            sleep(min(base_sleep * 2**attempt, max_sleep) * (0.5 + rng.random()))
    return SelectionResult(
        None, None, last_selection.reason, last_selection.rejected, retry_at=last_selection.retry_at
    )


__all__ = ["SelectionResult", "select_and_reserve", "select_route"]
