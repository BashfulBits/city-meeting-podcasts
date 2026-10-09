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

from citypods.provider_catalog.evidence import ContextObservation, LimitObservation

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


@dataclass(frozen=True)
class ContextSearchState:
    """One comparable bracket, using provider counts rather than local prompt estimates."""

    identity_digest: str
    dimension: str
    success: ContextObservation | None = None
    rejection: ContextObservation | None = None
    next_target: int | None = None
    status: str = "baseline"
    last_attempted_at: str | None = None
    count_pairs: tuple[tuple[int, int], ...] = ()

    def __post_init__(self):
        if self.dimension not in {"input", "output"} or self.status not in {
            "baseline",
            "exploring",
            "refining",
            "converged",
            "budget_limited",
            "uncertain",
            "unsupported",
        }:
            raise ValueError("invalid context state")
        if (
            len(self.count_pairs) > 16
            or any(
                type(value) is not int or not 0 < value <= 2**53 - 1
                for pair in self.count_pairs
                for value in pair
            )
            or any(len(pair) != 2 for pair in self.count_pairs)
        ):
            raise ValueError("invalid context count mapping")
        if self.next_target is not None and (
            type(self.next_target) is not int or not 0 < self.next_target <= 2**53 - 1
        ):
            raise ValueError("invalid context target")
        for observation in (self.success, self.rejection):
            if observation is not None and (
                observation.identity_digest != self.identity_digest
                or observation.dimension != self.dimension
            ):
                raise ValueError("incompatible context state reference")


def _context_count(observation):
    if observation.count_basis == "combined_reserved":
        return observation.reported_input + observation.requested_output
    if observation.count_basis == "combined_generated":
        return observation.reported_total
    return (
        observation.reported_input
        if observation.dimension == "input"
        else observation.reported_output
    )


def _context_converged(success, rejection):
    lower, upper = _context_count(success), rejection.reported_ceiling
    width = upper - lower
    return width > 0 and (width <= 128 or width * 200 <= lower)


def advance_context_state(state, observation):
    """Update only a compatible actual-count bracket; failures never fabricate progress."""
    from dataclasses import replace

    if (
        observation.identity_digest != state.identity_digest
        or observation.dimension != state.dimension
    ):
        raise ValueError("incompatible context observation")
    attempted = replace(state, last_attempted_at=observation.observed_at)
    reference = state.success or state.rejection
    if (
        reference
        and reference.count_basis != observation.count_basis
        and observation.count_basis != "unknown"
    ):
        return replace(attempted, status="uncertain")
    if observation.outcome == "unsupported":
        return replace(attempted, status="unsupported")
    if observation.outcome != "verified":
        # A documented size rejection is independently useful, unlike quota/transport errors.
        if observation.evidence_kind != "size_rejection" or observation.outcome != "inconclusive":
            return attempted
        if observation.count_basis not in {
            state.dimension,
            "combined_reserved",
            "combined_generated",
        }:
            return replace(attempted, status="uncertain")
        if state.success and observation.reported_ceiling <= _context_count(state.success):
            return replace(attempted, status="uncertain", rejection=observation)
        if state.rejection and observation.reported_ceiling >= state.rejection.reported_ceiling:
            return attempted
        updated = replace(attempted, rejection=observation, status="refining")
        if state.success and _context_converged(state.success, observation):
            updated = replace(updated, status="converged")
        return updated
    pairs = state.count_pairs
    if observation.local_input_estimate > 0 and observation.reported_input:
        pairs = (*pairs, (observation.local_input_estimate, observation.reported_input))[-16:]
    if (
        observation.reported_input is None
        or observation.reported_input > observation.reserved_input
        or (
            observation.reported_output is not None
            and observation.reported_output > observation.requested_output
        )
    ):
        return replace(attempted, count_pairs=pairs, status="uncertain")
    expected_kind = "processed_input" if state.dimension == "input" else "generated_output"
    if observation.evidence_kind != expected_kind:
        return replace(attempted, status="uncertain")
    count = _context_count(observation)
    if state.success and count <= _context_count(state.success):
        return replace(attempted, count_pairs=pairs, status="uncertain")
    rejection = state.rejection
    if rejection and count > rejection.reported_ceiling:
        rejection = (
            None  # fresh success supersedes the old active rejection, not diagnostic history
        )
    elif rejection and count == rejection.reported_ceiling:
        return replace(attempted, count_pairs=pairs, status="uncertain")
    status = "exploring"
    if rejection:
        status = "converged" if _context_converged(observation, rejection) else "refining"
    return replace(
        attempted,
        success=observation,
        rejection=rejection,
        count_pairs=pairs,
        next_target=None,
        status=status,
    )


def next_context_probe(state, limits, budget):
    """Keep an unfit desired target rather than repeating a smaller budget-capped experiment."""
    from dataclasses import replace

    if state.status == "unsupported":
        return state
    if state.status == "uncertain":
        # The caller stops this run on uncertainty; its next weekly scan revalidates baseline.
        state = replace(state, success=None, rejection=None, next_target=None, status="baseline")
    if state.success is None:
        target = state.next_target or limits["baseline"]
        status = "baseline"
    elif state.rejection and state.status != "converged":
        target = (_context_count(state.success) + state.rejection.reported_ceiling) // 2
        status = "refining"
    else:
        factor = Fraction(11, 10) if state.rejection else Fraction(3, 2)
        target = -(-(_context_count(state.success) * factor.numerator) // factor.denominator)
        status = "exploring"
    per_call_input, per_call_output = (524288, 256) if state.dimension == "input" else (2048, 32768)
    input_size = target if state.dimension == "input" else limits["opposite_reservation"]
    output_size = limits["opposite_reservation"] if state.dimension == "input" else target
    if (
        input_size > min(per_call_input, budget["remaining_input"])
        or output_size > min(per_call_output, budget["remaining_output"])
        or budget["remaining_requests"] <= 0
        or budget.get("route_requests", 0) >= 6
        or budget.get("remaining_seconds", 3600) < 205
    ):
        status = "budget_limited"
    return replace(state, next_target=target, status=status)


def context_input_ratio(state, prior):
    """Fixture-local maximum observed correction and existing prior, without another buffer."""
    if isinstance(prior, bool) or not 0 < prior < float("inf"):
        raise ValueError("invalid configured context ratio")
    return max(
        [
            Fraction(str(prior)),
            *(Fraction(count, estimate) for estimate, count in state.count_pairs),
        ]
    )


def plan_context_scan(limits, rotation):
    """All configured free routes, never-attempted first; untrusted rotation grants no evidence."""
    routes = [r for r in limits.get("routes") or [] if r.get("free") is True and r.get("rpd") != 0]
    if len(routes) > 128:
        raise ValueError("context identity coverage exceeds reviewed bound")

    def order(route):
        stamp = rotation.get(route["route_id"])
        try:
            value = datetime.fromisoformat(stamp)
            if value.tzinfo is None:
                raise ValueError("missing rotation timezone")
            return (1, value.astimezone(UTC), route["route_id"])
        except (TypeError, ValueError):
            return (0, datetime.min.replace(tzinfo=UTC), route["route_id"])

    return tuple(sorted(routes, key=order))


def _replay_context_history(identity, dimension, values):
    """Restart uncertain brackets at a later weekly run; older proof remains diagnostic only."""
    from dataclasses import replace

    state = ContextSearchState(identity, dimension)
    active, previous_run = [], None
    for observation in sorted(values, key=lambda o: (o.observed_at, o.run_id, o.attempt_id)):
        if state.status == "uncertain" and observation.run_id != previous_run:
            state = replace(
                state, success=None, rejection=None, next_target=None, status="baseline"
            )
            active = []
        state = advance_context_state(state, observation)
        active.append(observation)
        previous_run = observation.run_id
    return state, active


def context_history_states(history, *, now):
    """Rebuild bounded brackets from authenticated unexpired observations, never issue summaries."""
    groups = defaultdict(list)
    for observation in history:
        try:
            stamp = datetime.fromisoformat(observation.observed_at)
        except (ValueError, TypeError):
            continue
        if stamp.tzinfo is None or not now - timedelta(days=90) <= stamp <= now:
            continue
        groups[(observation.identity_digest, observation.dimension)].append(observation)
    if len(groups) > 128:
        raise ValueError("context identity coverage exceeds reviewed bound")
    states = {}
    for key, values in groups.items():
        state, _active = _replay_context_history(*key, values)
        states[key] = state
    return states


def context_choice(change):
    rid, field, old, new, stamp = change
    before = "null" if old is None else str(old)
    return f"{rid}: set {field} from {before} to {new} (context {stamp})"


def context_cap_changes(history, config, *, now):
    """Explicit reviewed cap offers from two compatible successful weekly runs, without a buffer."""
    from dataclasses import asdict

    from citypods.provider_catalog.evidence import context_identity, digest
    from citypods.provider_catalog.probe import chat_url
    from citypods.provider_catalog.registry import rules_for

    states = context_history_states(history, now=now)
    routes = {r["route_id"]: r for r in config.get("routes", [])}
    changes = []
    for (identity, dimension), state in states.items():
        if state.success is None or state.status in {"uncertain", "unsupported"}:
            continue
        success = state.success
        route = routes.get(success.route_id)
        if not route or route.get("free") is not True or route.get("rpd") == 0:
            continue
        cfg = config["providers"][route["provider"]]
        current = context_identity(
            route,
            cfg,
            dimension=dimension,
            count_basis=success.count_basis,
            parser_version="chat-usage-v1",
            opposite_reservation=256 if dimension == "input" else 2048,
            gateway_path=chat_url(rules_for(route["provider"]), cfg),
        )
        if current != identity or success.count_basis == "combined_generated":
            continue
        retained = sorted(
            (o for o in history if o.identity_digest == identity and o.dimension == dimension),
            key=lambda o: (o.observed_at, o.run_id, o.attempt_id),
        )[-16:]
        _state, retained = _replay_context_history(identity, dimension, retained)
        compatible = [
            o
            for o in retained
            if o.identity_digest == identity
            and o.dimension == dimension
            and o.outcome == "verified"
            and o.evidence_kind
            == ("processed_input" if dimension == "input" else "generated_output")
            and o.count_basis == success.count_basis
            and o.reported_input is not None
            and o.reported_input <= o.reserved_input
            and o.reported_output is not None
            and o.reported_output <= o.requested_output
        ]
        fresh = []
        for observation in compatible:
            stamp = datetime.fromisoformat(observation.observed_at)
            if stamp.tzinfo is not None and now - timedelta(days=90) <= stamp <= now:
                fresh.append(observation)
        weeks = {
            (
                datetime.fromisoformat(o.observed_at).date()
                - timedelta(days=datetime.fromisoformat(o.observed_at).weekday())
            )
            for o in fresh
        }
        if len({o.run_id for o in fresh}) < 2 or len(weeks) < 2:
            continue
        value = _context_count(success)
        window = route.get("input_context_limit")
        if type(window) is not int or window <= 0:
            continue
        field = "hard_input_ceiling" if dimension == "input" else "output_context_limit"
        if dimension == "input":
            production_output = route.get("output_context_limit")
            if type(production_output) is not int or production_output <= 0:
                continue
            if success.count_basis == "combined_reserved":
                value -= production_output
            value = min(value, window - production_output)
        else:
            value = min(value, window)
        # Unsupported plugins cannot turn an old or fabricated endpoint basis into a config offer.

        from citypods.provider_catalog.probe import build_context_request
        from citypods.provider_catalog.rules import context_parser_support

        check = build_context_request(
            route,
            cfg,
            rules_for(route["provider"]),
            dimension=dimension,
            target=1000,
            ratio=1,
            attempt_ordinal=1,
            nonce="offline-support-check-" * 2,
        )
        supported, _reason = context_parser_support(rules_for(route["provider"]), check)
        # The current chat parsers declare per-dimension usage, not combined-window semantics.
        # Future combined-window support must provide its own documented parser fixtures.
        if not supported or success.count_basis != check.dimension or value <= 0:
            continue
        old = route.get(field)
        if old is not None and (type(old) is not int or old <= 0):
            continue
        if old == value:
            continue
        planned = next_context_probe(
            state,
            {
                "baseline": value,
                "opposite_reservation": 256 if dimension == "input" else 2048,
            },
            {
                "remaining_input": 2097152,
                "remaining_output": 131072,
                "remaining_requests": 24,
            },
        )
        if planned.status == "budget_limited" and old is not None and value < old:
            # An experiment allowance is not evidence that an existing cap is too large.
            continue
        stamp = digest(
            {
                "identity": identity,
                "observations": [asdict(o) for o in fresh],
                "rejection": asdict(state.rejection) if state.rejection else None,
                "production_output": route.get("output_context_limit"),
                "window": window,
                "value": value,
            }
        )
        changes.append((route["route_id"], field, old, value, stamp))
    return tuple(sorted(changes))


def context_edit_plan(changes, config, *, selected=(), deferred=(), history=(), now=None):
    """Every context edit requires its exact freshly reconstructed maintainer selection."""
    from citypods.provider_catalog.apply import SOURCE_PATHS, EditPlan
    from citypods.provider_catalog.evidence import digest

    chosen = tuple(c for c in changes if context_choice(c) in set(selected))
    missing = sorted(set(selected) - {context_choice(c) for c in chosen})
    notes = []
    if history and now is not None:
        states = context_history_states(history, now=now)
        for rid, field, _old, _new, _stamp in chosen:
            dimension = "input" if field == "hard_input_ceiling" else "output"
            state = next(
                (
                    s
                    for s in states.values()
                    if s.dimension == dimension and s.success and s.success.route_id == rid
                ),
                None,
            )
            if state:
                upper = state.rejection.reported_ceiling if state.rejection else "unbounded"
                planned = next_context_probe(
                    state,
                    {
                        "baseline": _new,
                        "opposite_reservation": 256 if dimension == "input" else 2048,
                    },
                    {
                        "remaining_input": 2097152,
                        "remaining_output": 131072,
                        "remaining_requests": 24,
                    },
                )
                notes.append(
                    f"Evidence {rid}/{dimension}: provider basis {state.success.count_basis}; "
                    f"reasoning {state.success.reasoning_basis}; "
                    f"interval [{_context_count(state.success)}, {upper}]; status {state.status}. "
                    f"Observed {state.success.observed_at}; provider input/output "
                    f"{state.success.reported_input}/{state.success.reported_output}, "
                    f"reserved input/output {state.success.reserved_input}/"
                    f"{state.success.requested_output}. Next target {planned.next_target} "
                    f"({planned.status}). "
                    "Full production output/window guards retained."
                )
    return EditPlan(
        config.base_commit,
        tuple((p, digest(config.texts[p])) for p in SOURCE_PATHS),
        context_changes=chosen,
        proposal_kind="context",
        applied=(*tuple(context_choice(c) for c in chosen), *notes),
        deferred=(
            *deferred,
            *(f"{c}: current verified context offer unavailable" for c in missing),
        ),
    )
