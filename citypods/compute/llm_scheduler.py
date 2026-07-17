"""Pure LLM route selection plus CAS-backed selection-and-reservation."""

from __future__ import annotations

import random
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from citypods.compute.llm_budget import (
    LLM_BUDGET_STATE_KEY,
    LLMBudget,
    load_llm_budget_cas,
    minute_key,
    serialize_llm_budget,
)
from citypods.compute.llm_policy import ROUTES, LLMRequestPolicy, LLMRoute


@dataclass(frozen=True)
class SelectionResult:
    model: str | None
    route: LLMRoute | None
    reason: str
    rejected: tuple[tuple[str, str], ...] = ()


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


def _next_quota_reset(route: LLMRoute, ledger, now: datetime) -> datetime:
    resets: list[datetime] = []
    quota = route.quota
    if quota.rpm is not None or quota.tpm is not None:
        if ledger.requests_minute_key == minute_key(now):
            current = now.astimezone(UTC)
            resets.append(current.replace(second=0, microsecond=0) + timedelta(minutes=1))
    if quota.rpd is not None:
        zone = ZoneInfo(quota.reset_timezone)
        local = now.astimezone(zone)
        tomorrow = local.date() + timedelta(days=1)
        resets.append(datetime.combine(tomorrow, datetime.min.time(), tzinfo=zone).astimezone(UTC))
    return min(resets, default=now)


def _estimated_cost(route: LLMRoute, estimated_tokens: int, now: datetime) -> float:
    return (
        estimated_tokens
        * (route.pricing.input_per_token + route.pricing.output_per_token)
        * _active_multiplier(route, now)
    )


def select_route(
    policy: LLMRequestPolicy,
    *,
    routes: Mapping[str, LLMRoute],
    ledger: LLMBudget,
    backend_mode: Literal["direct", "dispatch"],
    estimated_tokens: int,
    now: datetime,
) -> SelectionResult:
    """Select one eligible route from a read-only ledger snapshot."""
    rejected: list[tuple[str, str]] = []
    candidates: list[tuple[LLMRoute, float, datetime]] = []
    allowed = set(policy.allowed_models) if policy.allowed_models is not None else None

    for model, route in sorted(routes.items()):
        if route.transport != ("direct" if backend_mode == "direct" else "mistral-dispatch"):
            rejected.append((model, "transport gate"))
            continue
        if allowed is not None and model not in allowed:
            rejected.append((model, "allowlist gate"))
            continue
        if not policy.allow_paid and not route.free:
            rejected.append((model, "paid model disallowed"))
            continue
        cost = _estimated_cost(route, estimated_tokens, now)
        if not ledger.available(
            model,
            route=route,
            requests=1,
            tokens=estimated_tokens,
            cost=cost,
            now=now,
        ):
            route_ledger = ledger.routes.get(model)
            if route_ledger is None:
                route_ledger = ledger._ledger(model, now, route=route)
            predicted = _next_quota_reset(route, route_ledger, now)
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
        candidates.append((route, cost, predicted))

    if not candidates:
        reason = "no configured LLM route is eligible right now"
        return SelectionResult(None, None, reason, tuple(rejected))
    route, _, predicted = min(
        candidates,
        key=lambda item: (not item[0].free, item[1], item[2], item[0].model),
    )
    rejected.extend(
        (candidate.model, "lower-ranked eligible route")
        for candidate, _, _ in candidates
        if candidate.model != route.model
    )
    return SelectionResult(route.model, route, "selected eligible route", tuple(rejected))


def select_and_reserve(
    storage,
    owner: str,
    policy: LLMRequestPolicy,
    *,
    routes: Mapping[str, LLMRoute] = ROUTES,
    backend_mode: Literal["direct", "dispatch"],
    estimated_tokens: int,
    now: datetime | None = None,
    max_attempts: int = 8,
    base_sleep: float = 0.05,
    max_sleep: float = 1.0,
    sleep=time.sleep,
    rng: random.Random | None = None,
) -> SelectionResult:
    """Select and reserve a route atomically, reselecting after CAS conflicts."""
    from citypods.storage import CASConflict

    now = now or datetime.now(UTC)
    rng = rng or random
    last_selection = SelectionResult(None, None, "CAS reservation attempts exhausted")
    for attempt in range(max_attempts):
        ledger, etag = load_llm_budget_cas(storage)
        selection = select_route(
            policy,
            routes=routes,
            ledger=ledger,
            backend_mode=backend_mode,
            estimated_tokens=estimated_tokens,
            now=now,
        )
        last_selection = selection
        if selection.route is None:
            return selection
        # No availability recheck here: `select_route` already confirmed `ledger.available(...)`
        # for this exact candidate against this same unmutated `ledger` snapshot when it built
        # `candidates` (§5 gate 3) -- a second check here would always pass and never fires.
        cost = _estimated_cost(selection.route, estimated_tokens, now)
        ledger.reserve(
            owner,
            selection.model,
            route=selection.route,
            requests=1,
            tokens=estimated_tokens,
            cost=cost,
            now=now,
        )
        try:
            body = serialize_llm_budget(ledger)
            if etag is None:
                storage.put_cas(LLM_BUDGET_STATE_KEY, body, "application/json", if_none_match="*")
            else:
                storage.put_cas(LLM_BUDGET_STATE_KEY, body, "application/json", if_match=etag)
            return selection
        except CASConflict:
            sleep(min(base_sleep * 2**attempt, max_sleep) * (0.5 + rng.random()))
    return SelectionResult(None, None, last_selection.reason, last_selection.rejected)


__all__ = ["SelectionResult", "select_and_reserve", "select_route"]
