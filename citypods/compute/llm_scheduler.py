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

from citypods.compute.budget import cycle_key
from citypods.compute.llm_budget import (
    LLM_BUDGET_STATE_KEY,
    LLMBudget,
    RouteLedger,
    daily_reset_key,
    load_llm_budget_cas,
    serialize_llm_budget,
)
from citypods.compute.llm_policy import (
    ROUTE_REGISTRY,
    LLMRequestPolicy,
    LLMRoute,
    canonical_model,
)


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
    # become eligible again (the first route whose continuous RPM/TPM schedules and daily quota
    # are all clear, or the end of a real-429 block), or `None` if nothing will free up. A pacing
    # caller (see `LiteLLMBackend._run_policy_job`) sleeps until this instead of deferring to a
    # future run, so a single run can drain its full daily quota while respecting average-rate
    # spacing. `None` when a route *was* selected.
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


def _reservation_size(
    route: LLMRoute, *, requests: int, tokens: int, transport: str | None = None
) -> tuple[int, int]:
    """Return this route's bounded provider reservation from a caller-wide worst case.

    ``tokens`` is the total for ``requests`` equal-sized possible attempts. A dispatch Worker
    has no local corrective retry, so its route can safely reserve one attempt while direct
    routes keep the caller's two-attempt structured-output reservation.
    """
    max_attempts = (
        1 if transport in _DISPATCH_TRANSPORTS else (route.max_provider_attempts or requests)
    )
    route_requests = min(requests, max_attempts)
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
    _, _, windows = route.pricing.rates_at(now)
    for window in windows:
        bounds = _window_bounds(window, now)
        if bounds is None:
            continue
        start, end = bounds
        if start <= now.astimezone(UTC) < end:
            return window.multiplier
    return 1.0


def _next_cheapest_window(route: LLMRoute, now: datetime) -> datetime | None:
    """Return when a currently more-expensive route reaches its cheapest recurring window.

    A route remains selectable at its current price when a caller's deadline would be missed by
    waiting; otherwise flexible/deferred work is held until the cheapest active-period multiplier
    is available.  A route with no price premium (or already in its cheapest window) returns
    ``None``.
    """
    _, _, windows = route.pricing.rates_at(now)
    if not windows:
        return None
    now_utc = now.astimezone(UTC)
    active = _active_multiplier(route, now)
    cheapest = min(1.0, *(window.multiplier for window in windows))
    if active <= cheapest:
        return None

    if cheapest == 1.0:
        # The ordinary/off-peak period is the cheapest one. The active premium window's end is
        # therefore the next point at which the route reaches it.
        for window in windows:
            bounds = _window_bounds(window, now)
            if bounds is not None:
                start, end = bounds
                if start <= now_utc < end and window.multiplier == active:
                    cheapest_at = end
                    break
        else:
            cheapest_at = None
    else:
        # A sub-1x window is the cheapest period. Find its next recurring start;
        # `_window_bounds` returns today's or tomorrow's occurrence depending on the current
        # local time.
        starts = []
        for window in windows:
            if window.multiplier != cheapest:
                continue
            bounds = _window_bounds(window, now)
            if bounds is not None and bounds[0] > now_utc:
                starts.append(bounds[0])
        cheapest_at = min(starts, default=None)

    # The current rate card can be replaced before its next cheap window begins (or ends). Wake
    # at that transition too, and decide from the newly active card rather than retaining a stale
    # premium-window assumption. This matters when a provider changes a rate card mid-window.
    next_period = min(
        (
            period.effective_at.astimezone(UTC)
            for period in route.pricing.periods
            if period.effective_at.astimezone(UTC) > now_utc
        ),
        default=None,
    )
    candidates = (candidate for candidate in (cheapest_at, next_period) if candidate is not None)
    return min(candidates, default=None)


def _next_local_midnight(reset_timezone: str, now: datetime) -> datetime:
    """The start of the next calendar day in ``reset_timezone``, as a UTC instant -- shared by
    every axis in ``_next_quota_reset`` that resets on that same daily boundary (RPD, and the
    route's ``daily_cost_cap``), so the two can't drift out of sync with each other."""
    zone = ZoneInfo(reset_timezone)
    local = now.astimezone(zone)
    tomorrow = local.date() + timedelta(days=1)
    return datetime.combine(tomorrow, datetime.min.time(), tzinfo=zone).astimezone(UTC)


def _next_quota_reset(
    route: LLMRoute,
    ledger,
    now: datetime,
    *,
    requests: int,
    tokens: int,
    cost: float,
    provider_ledger=None,
) -> datetime:
    """The next time all axes currently keeping ``model`` unavailable will be clear.

    Only offers a candidate for an axis genuinely responsible for the current ``available()``
    failure. RPM is a continuous request-rate schedule; TPM is an average throughput rate and uses
    the route's persisted oversized-request cooldown instead of comparing one request with a hard
    one-minute allowance. RPD remains a daily reset.

    Same reasoning applies to ``blocked_until``: it only ever moves forward (`LLMBudget.block()`
    extends it, never clears it), so a route blocked by an *earlier* real 429 keeps a stale
    timestamp in the ledger long after that block itself expired. `available()` already treats a
    block as in effect only while ``now < blocked_until`` (see its own check) -- this function must
    agree, or a past `blocked_until` could become the selected retry time instead of the latest
    real axis reset.

    And again for ``pricing.daily_cost_cap``: it resets on the same daily boundary as RPD
    (``daily_reset_key`` in ``llm_budget.py``'s ``_ledger()``), so it gets the same real reset-time
    treatment rather than falling into the imprecise fallback below.
    """
    resets: list[datetime] = []
    quota = route.quota
    pricing = route.pricing
    if quota.rpm is not None:
        if ledger.requests_available_at:
            request_ready_at = datetime.fromisoformat(ledger.requests_available_at)
            if request_ready_at > now.astimezone(UTC):
                resets.append(request_ready_at)
        elif ledger.requests_minute + requests > quota.rpm:
            current = now.astimezone(UTC)
            resets.append(current.replace(second=0, microsecond=0) + timedelta(minutes=1))
    if route.provider_rpm is not None and provider_ledger is not None:
        if provider_ledger.requests_available_at:
            provider_ready_at = datetime.fromisoformat(provider_ledger.requests_available_at)
            if provider_ready_at > now.astimezone(UTC):
                resets.append(provider_ready_at)
    if quota.tpm is not None and ledger.tokens_available_at:
        token_ready_at = datetime.fromisoformat(ledger.tokens_available_at)
        if token_ready_at > now.astimezone(UTC):
            resets.append(token_ready_at)
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
    # Every candidate in `resets` is a necessary gate. Retrying at the earliest candidate can
    # immediately fail on another still-blocking axis (for example, route RPM before tomorrow's
    # RPD reset), so wait for the latest candidate.
    return max(resets)


def _estimated_cost(route: LLMRoute, estimated_tokens: int, now: datetime) -> float:
    input_per_token, output_per_token, _ = route.pricing.rates_at(now)
    return estimated_tokens * (input_per_token + output_per_token) * _active_multiplier(route, now)


def _cost_caps_allow_at(
    ledger: LLMBudget,
    route_key: str,
    route: LLMRoute,
    cost: float,
    at: datetime,
    now: datetime,
) -> bool:
    """Check cost caps at a future pricing retry without rolling the live ledger forward."""
    entry = ledger._ledger(route_key, now, route=route, create=False)
    if entry is None:
        return True
    pricing = route.pricing
    future_cycle_used = entry.cost_used if entry.cost_cycle_key == cycle_key(at) else 0.0
    future_day_used = (
        entry.cost_day_used
        if entry.cost_day_key == daily_reset_key(at, route.quota.reset_timezone)
        else 0.0
    )
    return (pricing.cost_cap is None or future_cycle_used + cost <= pricing.cost_cap) and (
        pricing.daily_cost_cap is None or future_day_used + cost <= pricing.daily_cost_cap
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


# Transports that dedupe server-side on `idempotency-key: recipe_hash`. "llm-dispatch-v2" fits
# this criterion too (coordinator.js derives its idempotency_key from job.recipe_hash), even
# though no compiled route currently selects it via this scheduler's normal path -- see the
# matching note on citypods/compute/llm.py's is_dispatch.
_DISPATCH_TRANSPORTS = frozenset({"mistral-dispatch", "llm-dispatch", "llm-dispatch-v2"})


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
    input_tokens: int | None = None,
    output_tokens: int | None = None,
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
    ``input_tokens`` and ``output_tokens`` describe one attempt and are checked against each
    physical route's independent context ceilings before quota admission.
    """
    rejected: list[tuple[str, str]] = []
    candidates: list[tuple[LLMRoute, str, float, datetime, float, int, int, str]] = []
    allowed = (
        {canonical_model(model) for model in policy.allowed_models}
        if policy.allowed_models is not None
        else None
    )
    # Earliest time an otherwise-allowed route (transport/allowlist/paid gates all passed) that is
    # only *temporarily* unavailable -- a rate schedule full, daily quota spent, under a real 429
    # block, or waiting for its cheapest price window -- is predicted to become eligible again. A
    # pacing caller sleeps until this rather than deferring the whole request to a future run.
    retry_ats: list[datetime] = []
    input_tokens = estimated_tokens if input_tokens is None else input_tokens
    output_tokens = 0 if output_tokens is None else output_tokens

    for route_key, route in sorted(routes.items()):
        model = route.model
        if not any(t in available_transports for t in route.transports):
            rejected.append((model, "transport gate"))
            continue
        if allowed is not None and model not in allowed:
            rejected.append((model, "allowlist gate"))
            continue
        if input_tokens > route.input_context_limit:
            rejected.append((model, "input context limit"))
            continue
        if output_tokens > route.output_context_limit:
            rejected.append((model, "output context limit"))
            continue
        if not policy.allow_paid and not route.free:
            rejected.append((model, "paid model disallowed"))
            continue
        if route.experimental and not policy.allow_experimental:
            rejected.append((model, "experimental route disallowed"))
            continue
        transport = _selected_transport(
            route, available_transports, allow_dispatch_overflow=policy.allow_dispatch_overflow
        )
        route_requests, route_tokens = _reservation_size(
            route, requests=requests, tokens=estimated_tokens, transport=transport
        )
        # Flexible work waits for the cheaper price before capacity admission. In particular,
        # applying a peak-rate estimate to a daily cost cap must not reject work that can fit at
        # the imminent off-peak rate; its next selection re-evaluates all quota and cost gates.
        price_ready_at = _next_cheapest_window(route, now)
        if (
            price_ready_at is not None
            and (policy.deadline_at is None or price_ready_at <= policy.deadline_at)
            and _cost_caps_allow_at(
                ledger,
                route_key,
                route,
                _estimated_cost(route, route_tokens, price_ready_at),
                price_ready_at,
                now,
            )
        ):
            retry_ats.append(price_ready_at)
            rejected.append((model, f"price-window gate: waits until {price_ready_at.isoformat()}"))
            continue

        cost = _estimated_cost(route, route_tokens, now)
        if not ledger.available(
            route_key,
            route=route,
            requests=route_requests,
            tokens=route_tokens,
            cost=cost,
            now=now,
        ):
            route_ledger = ledger._ledger(route_key, now, route=route, create=False)
            # A missing ledger is unspent and therefore available. Reaching this branch means
            # `available()` observed a real exhausted or blocked physical route.
            assert route_ledger is not None
            predicted = _next_quota_reset(
                route,
                route_ledger,
                now,
                requests=route_requests,
                tokens=route_tokens,
                cost=cost,
                provider_ledger=ledger._provider_ledger(route, create=False),
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
        # Already fetched (and, if needed, window-rolled) by the `ledger.available(...)` check
        # above, which internally calls the same `_ledger()` -- this is that same entry, not a
        # second lookup.
        candidates.append(
            (
                route,
                route_key,
                cost,
                predicted,
                _utilization(
                    route,
                    ledger._ledger(route_key, now, route=route, create=False) or RouteLedger(),
                ),
                route_requests,
                route_tokens,
                transport,
            )
        )

    if not candidates:
        reason = "no configured LLM route is eligible right now"
        return SelectionResult(
            None, None, reason, tuple(rejected), retry_at=min(retry_ats, default=None)
        )
    route, route_key, _, predicted, _, route_requests, route_tokens, transport = min(
        candidates,
        key=lambda item: (not item[0].free, item[2], item[4], item[3], item[0].model, item[1]),
    )
    rejected.extend(
        (candidate.model, "lower-ranked eligible route")
        for candidate, candidate_key, _, _, _, _, _, _ in candidates
        if candidate_key != route_key
    )
    return SelectionResult(
        route.model,
        route,
        "selected eligible route",
        tuple(rejected),
        reserved_requests=route_requests,
        reserved_tokens=route_tokens,
        transport=transport,
    )


def select_and_reserve(
    storage,
    recipe_hash: str,
    policy: LLMRequestPolicy,
    *,
    routes: Mapping[str, LLMRoute] = ROUTE_REGISTRY,
    available_transports: Set[str],
    estimated_tokens: int,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
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
        existing_route_id = ledger.find_inflight_owner(recipe_hash)
        if existing_route_id is not None and existing_route_id in routes:
            # `owner == recipe_hash` only ever happens for a dispatch reservation (`_owner_for`
            # returns a fresh UUID for direct) -- so the transport being reused here is, by
            # construction, one of this route's own dispatch transports.
            existing_route = routes[existing_route_id]
            reused_transport = next(
                (t for t in existing_route.transports if t in _DISPATCH_TRANSPORTS), None
            )
            # If the route's dispatch transport is no longer in `available_transports` (e.g.
            # the Worker was removed from the backend's config since the original reservation),
            # don't return a result with `transport=None` -- fall through to fresh selection
            # which will re-evaluate transport availability normally.
            if reused_transport is not None and reused_transport in available_transports:
                return SelectionResult(
                    existing_route.model,
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
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            requests=requests,
            now=attempt_now,
        )
        last_selection = selection
        if selection.route is None:
            # Availability checks roll minute/day/token windows in memory. Persist that rollover
            # even when no route can be reserved, otherwise a coordination ledger can display an
            # old day key until the first successful request and every caller sees stale quota
            # accounting. This is a bookkeeping-only CAS write; unchanged ledgers are untouched.
            rolled_body = serialize_llm_budget(ledger)
            if etag is not None:
                current_body, _ = storage.get_bytes(LLM_BUDGET_STATE_KEY) or (None, None)
                if current_body != rolled_body:
                    try:
                        storage.put_cas(
                            LLM_BUDGET_STATE_KEY, rolled_body, "application/json", if_match=etag
                        )
                    except CASConflict:
                        sleep(min(base_sleep * 2**attempt, max_sleep) * (0.5 + rng.random()))
                        continue
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
            selection.route.route_id or selection.model,
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
