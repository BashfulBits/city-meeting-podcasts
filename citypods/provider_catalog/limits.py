"""Offline scoped rate-history decisions for review/48 Slice 4.

Callers must rehydrate observations from authenticated, digest-verified workflow artifacts.
Issue markers and a matching run ID alone are not evidence. This module does not fetch artifacts,
probe providers, edit configuration or authorize publication.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from fractions import Fraction

from citypods.provider_catalog.evidence import LimitObservation

RATE_METRICS = frozenset({"rpm", "tpm", "rpd"})
CEILING_SOURCES = frozenset({"header", "documented"})


def _time(observation: LimitObservation) -> datetime | None:
    try:
        value = datetime.fromisoformat(observation.observed_at)
    except (TypeError, ValueError):
        return None
    return value.astimezone(UTC) if value.tzinfo is not None else None


def _scope(observation: LimitObservation) -> tuple:
    return (
        observation.provider,
        observation.account_id,
        observation.route_id if observation.scope == "route" else "",
        observation.scope,
        observation.metric,
    )


def merge_observations(
    existing: Iterable[LimitObservation],
    incoming: Iterable[LimitObservation],
    *,
    now: datetime,
) -> tuple[LimitObservation, ...]:
    """Retain at most six distinct runs per scope/metric in a 90-day verified window.

    Keep evidence kinds separate, taking the largest observation of each kind in one run.
    Throughput can remain advisory history but cannot support ceiling decisions.
    """
    if now.tzinfo is None:
        raise ValueError("rate history requires a timezone-aware current time")
    now = now.astimezone(UTC)
    groups: dict[tuple, dict[tuple, LimitObservation]] = defaultdict(dict)
    for observation in (*existing, *incoming):
        observed = _time(observation)
        if (
            observed is None
            or not now - timedelta(days=90) <= observed <= now
            or not all(
                isinstance(value, str) and value.strip()
                for value in (
                    observation.run_id,
                    observation.provider,
                    observation.account_id,
                    observation.metric,
                    observation.scope,
                    observation.source,
                )
            )
            or (observation.route_id is not None and not isinstance(observation.route_id, str))
            or not observation.run_id
            or not observation.provider
            or not observation.account_id
            or observation.metric not in RATE_METRICS
            or observation.scope not in {"route", "provider_account"}
            or (observation.scope == "route" and not (observation.route_id or "").strip())
            or observation.source not in CEILING_SOURCES | {"throughput"}
        ):
            continue
        group = groups[_scope(observation)]
        key = (observation.run_id, observation.source)
        previous = group.get(key)
        if previous is None or (observation.value, observed) > (previous.value, _time(previous)):
            group[key] = observation
    retained = []
    for group in groups.values():
        latest: dict[str, datetime] = {}
        for observation in group.values():
            observed = _time(observation)
            latest[observation.run_id] = max(latest.get(observation.run_id, observed), observed)
        runs = set(sorted(latest, key=lambda run: (latest[run], run), reverse=True)[:6])
        retained.extend(observation for observation in group.values() if observation.run_id in runs)
    return tuple(
        sorted(
            retained,
            key=lambda observation: (
                *_scope(observation),
                observation.observed_at,
                observation.run_id,
                observation.source,
            ),
        )
    )


def effective_limit(observations: Iterable[LimitObservation]) -> int | None:
    """The conservative maximum of one comparable ceiling-evidence kind, or unknown."""
    observations = tuple(observations)
    if (
        not observations
        or len({_scope(observation) for observation in observations}) != 1
        or len({observation.source for observation in observations}) != 1
        or observations[0].source not in CEILING_SOURCES
    ):
        return None
    return max(observation.value for observation in observations)


@dataclass(frozen=True)
class RateDecision:
    """An internal decision for one existing scalar; never a serialized config instruction."""

    action: str
    value: int | None
    material: bool = False


def plan_limit_changes(
    observations: Iterable[LimitObservation],
    configured_value: int | float,
    *,
    now: datetime,
    recent_runs: tuple[str, ...] | None = None,
) -> RateDecision:
    """Plan one comparable scalar after artifact and target-scope verification by the caller.

    Provider-account decisions additionally require compatible evidence for every configured
    account before the caller may target an existing shared provider cap. This single-scope helper
    never converts account capacity into independent per-route capacity.
    """
    try:
        configured = Fraction(str(configured_value))
    except (ValueError, ZeroDivisionError):
        return RateDecision("defer", None)
    if (
        isinstance(configured_value, bool)
        or not isinstance(configured_value, (int, float))
        or configured <= 0
    ):
        return RateDecision("defer", None)
    history = merge_observations((), observations, now=now)
    effective = effective_limit(history)
    if effective is None:
        return RateDecision("defer", None)
    if effective > configured * Fraction(6, 5):
        return RateDecision("offer_increase", effective)
    runs = sorted(history, key=lambda observation: (_time(observation), observation.run_id))
    if (
        len(runs) >= 3
        and (
            recent_runs is None
            or (len(recent_runs) >= 3 and set(recent_runs[:3]) <= {o.run_id for o in runs})
        )
        and effective < configured * Fraction(4, 5)
        and all(observation.value < configured * Fraction(4, 5) for observation in runs[-3:])
    ):
        return RateDecision("tighten", effective, effective < configured * Fraction(1, 2))
    return RateDecision("unchanged", None)


@dataclass(frozen=True)
class RateChange:
    """An exact existing scalar target, reconstructed from verified evidence and current config."""

    scope: str
    provider: str
    target: str
    metric: str
    old: int | float
    new: int
    action: str
    config_digest: str
    material: bool = False

    @property
    def choice(self) -> str:
        return f"`rate:{self.scope}:{self.target}:{self.metric}`: increase {self.old} to {self.new}"


def configured_limit_changes(observations, limits, *, now, recent_runs):
    """Map authenticated scopes onto existing representable scalars, without splitting caps.

    Shared provider caps require the same effective ceiling and direction for every declared
    account. Unequal capacities, missing accounts and provider RPD (not compiled) defer rather
    than inventing aggregation or a schema. Recent successful runs include empty artifacts, so
    a missing/unknown sample interrupts the three-consecutive-run tightening requirement.
    """
    from citypods.provider_catalog.evidence import digest, provider_rate_digest

    history = merge_observations((), observations, now=now)
    changes, deferred = [], []
    for route in limits.get("routes") or []:
        for metric in sorted(RATE_METRICS):
            if metric not in route:
                continue
            samples = [
                o
                for o in history
                if o.scope == "route"
                and o.route_id == route["route_id"]
                and o.provider == route.get("provider")
                and o.account_id == route.get("account_id")
                and o.metric == metric
            ]
            recent = (
                recent_runs.get(("route", route["route_id"]), ())
                if isinstance(recent_runs, Mapping)
                else recent_runs
            )
            decision = plan_limit_changes(samples, route[metric], now=now, recent_runs=recent)
            if decision.action in {"tighten", "offer_increase"}:
                changes.append(
                    RateChange(
                        "route",
                        route["provider"],
                        route["route_id"],
                        metric,
                        route[metric],
                        decision.value,
                        decision.action,
                        digest(route),
                        decision.material,
                    )
                )
    for provider, config in (limits.get("providers") or {}).items():
        accounts = [a["id"] for a in config.get("accounts") or []]
        for metric in sorted(RATE_METRICS & config.keys()):
            scoped = [
                o
                for o in history
                if o.scope == "provider_account" and o.provider == provider and o.metric == metric
            ]
            if not scoped:
                continue
            decisions = [
                plan_limit_changes(
                    [o for o in scoped if o.account_id == account],
                    config[metric],
                    now=now,
                    recent_runs=(
                        recent_runs.get(("provider_account", provider, account), ())
                        if isinstance(recent_runs, Mapping)
                        else recent_runs
                    ),
                )
                for account in accounts
            ]
            if (
                metric == "rpd"
                or not decisions
                or len({(d.action, d.value) for d in decisions}) != 1
                or decisions[0].action == "defer"
            ):
                deferred.append(f"{provider}/{metric}: shared account scope is not representable")
                continue
            decision = decisions[0]
            if decision.action in {"tighten", "offer_increase"}:
                changes.append(
                    RateChange(
                        "provider",
                        provider,
                        provider,
                        metric,
                        config[metric],
                        decision.value,
                        decision.action,
                        provider_rate_digest(limits, provider),
                        decision.material,
                    )
                )
    return tuple(changes), tuple(deferred)


def scope_recent_runs(accepted, limits):
    """Only attempted scopes participate; an attempted scope with no sample still interrupts."""
    routes = {r["route_id"]: r for r in limits.get("routes") or []}
    scoped = defaultdict(list)
    for run in sorted(accepted, key=lambda r: (r["observed_at"], r["run_id"]), reverse=True):
        touched = set()
        for rid in run.get("attempted_routes") or []:
            if rid not in routes:
                continue
            route = routes[rid]
            touched.add(("route", rid))
            touched.add(("provider_account", route["provider"], route["account_id"]))
        for scope in touched:
            if run["run_id"] not in scoped[scope]:
                scoped[scope].append(run["run_id"])
    return {scope: tuple(runs) for scope, runs in scoped.items()}


def rate_edit_plan(changes, config, *, selected=(), automatic=False, deferred=()):
    """Exact increase choices authorize only current reconstructed offers."""
    from citypods.provider_catalog.apply import SOURCE_PATHS, EditPlan
    from citypods.provider_catalog.evidence import digest

    selected = set(selected)
    applied = tuple(
        c
        for c in changes
        if (automatic and c.action == "tighten")
        or (not automatic and c.action == "offer_increase" and c.choice in selected)
    )
    unavailable = tuple(sorted(selected - {c.choice for c in applied}))
    return EditPlan(
        config.base_commit,
        tuple((p, digest(config.texts[p])) for p in SOURCE_PATHS),
        rate_changes=applied,
        proposal_kind="limits",
        applied=tuple(
            c.choice
            if c.action == "offer_increase"
            else f"{c.target}/{c.metric}: tighten {c.old} to {c.new}"
            for c in applied
        ),
        deferred=(
            *deferred,
            *[f"{choice}: current verified offer unavailable" for choice in unavailable],
        ),
    )


def early_rate_routes(failures, limits, state, *, today):
    """Today's existing UTC counters schedule observations only, at most once per route/day."""
    configured = {r["route_id"] for r in limits.get("routes") or []}
    counts = defaultdict(dict)
    for failure in failures:
        if not isinstance(failure, dict):
            continue
        rid, kind, count = (failure.get(k) for k in ("route_id", "failure_class", "count"))
        if (
            rid in configured
            and kind in {"own_rpm", "own_tpm", "unknown_429"}
            and failure.get("utc_day") == today.isoformat()
            and type(count) is int
            and count > 0
        ):
            counts[rid][kind] = max(counts[rid].get(kind, 0), count)
    return {
        rid
        for rid, classes in counts.items()
        if sum(classes.values()) >= 3
        and (state.get("route_checks") or {}).get(rid) != today.isoformat()
    }
