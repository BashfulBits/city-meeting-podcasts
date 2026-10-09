"""Plan one reconciliation run: catalog drift, configured-route health, and proven candidates.

Provider-agnostic by construction (review/48 R2): per-provider behavior comes only from the
plugin registry, lanes only from `load_lanes()`, and standing decisions only from the decisions
file. Nothing here writes config; Slice 1's only side effect is the rolling issue (see issue.py).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Protocol

import requests

from citypods.compute.llm_lanes import LaneConfig
from citypods.llm_rate_probe import (
    ProbeBudgetExceeded,
    RateProbeRunner,
    RouteBudgetExceeded,
    load_route_catalog,
    run_phase_0,
    run_phase_1,
)
from citypods.provider_catalog.classify import Classification, classify
from citypods.provider_catalog.decisions import Decisions
from citypods.provider_catalog.evidence import (
    CatalogEvidence,
    LimitObservation,
    RouteEvidence,
    candidate_digest,
    catalog_digest,
    digest,
    logical_identity,
    positive_bound,
)
from citypods.provider_catalog.probe import (
    CANARY_TIMEOUT_SECONDS,
    STRUCTURED_METHODS,
    account_key,
    canary,
    fetch_catalog,
    structured_canary,
)
from citypods.provider_catalog.quality import QualityIndex
from citypods.provider_catalog.registry import all_rules
from citypods.provider_catalog.rules import ProviderRules, Response

# At most this many NEW candidate canaries per provider per run. Configured-route health checks
# are not capped: they are one per configured upstream model.
CANARY_BUDGET_PER_PROVIDER = 3
# A candidate whose canary failed is not retried for this long; a proven one stays listed this long.
CANARY_MEMORY_DAYS = 28
# Routes with a daily quota this small are health-checked on this cadence, not every run, and only
# when the Worker reports quota left (review/48 R3).
SCARCE_RPD = 50
SCARCE_CHECK_DAYS = 28
ANOMALY_VERDICTS = frozenset({"retired", "not_served", "not_entitled", "account_blocked"})
# Verdicts worth remembering for CANARY_MEMORY_DAYS. Inconclusive and spent-quota results say
# nothing durable about the model, so those candidates are retried next run (after fresh ones).
DEFINITIVE_VERDICTS = ANOMALY_VERDICTS | {"proven"}
_VARIANT_SUFFIX = re.compile(r"-(it|instruct|chat|preview|latest|reasoning)$")


def _identity(model: str, free_suffix: str = "") -> str:
    """A model ID reduced to its bare name (no free/variant decorations), for alias checks."""
    name = model.removesuffix(free_suffix) if free_suffix else model
    name = re.sub(r"[^a-z0-9]+", "-", name.split("/")[-1].lower()).strip("-")
    while (stripped := _VARIANT_SUFFIX.sub("", name)) != name:
        name = stripped
    return name


_CONTEXT_KEYS = ("context_length", "context_window", "max_context_length", "inputTokenLimit")


class PauseOutcomeLike(Protocol):
    contended: bool

    def renew(self) -> None: ...


class DispatchControl(Protocol):
    """The v2 Worker's pause/quota surface (citypods.compute.llm_dispatch_pause), or a no-op."""

    def paused(self, provider: str) -> AbstractContextManager[PauseOutcomeLike]: ...

    def route_quota(self, provider: str) -> Mapping[str, Mapping[str, Any]]: ...

    def reserve(self, route_id: str) -> None: ...


@dataclass
class _Unpaused:
    contended: bool = True  # nothing was paused, so production may have competed

    def renew(self) -> None:
        return None


class NoDispatchControl:
    """Used when the Worker is not configured: probes still run, marked as contended."""

    @contextmanager
    def paused(self, provider: str) -> Iterator[PauseOutcomeLike]:
        yield _Unpaused()

    def route_quota(self, provider: str) -> Mapping[str, Mapping[str, Any]]:
        return {}

    def reserve(self, route_id: str) -> None:
        return None


@dataclass
class Candidate:
    provider: str
    model: str
    score: float | None
    below_floor: bool | None
    context_limit: int | None
    links: tuple[tuple[str, str], ...]
    lanes: tuple[str, ...]  # recommended: scores at least the lane's weakest scored model
    proven_on: str
    # Every other eligible lane, with how far below that lane's weakest scored model this one is
    # (capacity backups are often weaker by design); shown, not offered as a checkbox.
    other_lanes: tuple[tuple[str, float], ...] = ()
    structured_output_method: str | None = None
    structured_output_verified_on: str | None = None

    evidence: dict[str, Any] | None = None
    evidence_digest: str | None = None

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.model}"


@dataclass
class Anomaly:
    provider: str
    route_id: str
    model: str
    verdict: str
    reason: str
    absent_from_catalog: bool
    new: bool = True
    # Each lane whose models this route serves, with the lane's other models (or this model's
    # other routes) that still have a live route -- what keeps the lane working without it.
    lane_usage: tuple[tuple[str, tuple[str, ...]], ...] = ()
    observed_on: str | None = None
    config_digest: str | None = None
    contended: bool = True

    @property
    def key(self) -> str:
        return f"{self.route_id}:{self.verdict}"


@dataclass
class Report:
    candidates: list[Candidate] = field(default_factory=list)
    anomalies: list[Anomaly] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    floor: float | None = None
    rate_observations: list[LimitObservation] = field(default_factory=list)
    rate_changes: list = field(default_factory=list)
    rate_attempted_routes: set[str] = field(default_factory=set)
    context_observations: list = field(default_factory=list)
    context_states: list = field(default_factory=list)
    context_changes: list = field(default_factory=list)
    context_attempted_routes: set[str] = field(default_factory=set)

    @property
    def actionable(self) -> bool:
        return bool(self.candidates or self.anomalies or self.rate_changes or self.context_changes)


def _route_model_keys(route: Mapping[str, Any]) -> set[str]:
    keys = {str(route.get("model") or ""), str(route.get("model_key") or "")}
    keys.update(str(m) for m in route.get("also_serves") or [])
    return keys - {""}


def resolved_method(route: Mapping[str, Any], routes, providers) -> str:
    """Resolve the method actually compiled for this physical route, including pooled models.

    The compiler contract test compares this against compile_llm_limits' resolver. Source YAML
    uses model_key for a pool; compiled routes put that identity in model.
    """
    if route.get("structured_output_method"):
        return str(route["structured_output_method"])
    model = route.get("model_key") or route.get("model")
    methods = {
        other["structured_output_method"]
        for other in routes
        if (other.get("model_key") or other.get("model")) == model
        and other.get("structured_output_method")
        and other.get("structured_output_verified_on")
    }
    if len(methods) == 1:
        return next(iter(methods))
    return (providers.get(route.get("provider")) or {}).get(
        "structured_output_method", "prompt_only"
    )


def lane_incumbent_scores(
    lanes: Mapping[str, LaneConfig], routes: list[Mapping[str, Any]], quality: QualityIndex
) -> dict[str, list[float]]:
    """AA scores of each eligible lane's current models, resolved through configured routes."""
    rules = all_rules()
    out: dict[str, list[float]] = {}
    for purpose, lane in lanes.items():
        if not lane.accepts_catalog_backups:
            continue
        scores = []
        for lane_model in (*lane.models, *lane.backup_models):
            best = None
            for route in routes:
                if lane_model not in _route_model_keys(route):
                    continue
                provider_rules = rules.get(str(route.get("provider")))
                score = quality.score(
                    str(route.get("upstream_model") or ""),
                    creator=provider_rules.creator if provider_rules else None,
                    free_suffix=provider_rules.free_suffix if provider_rules else "",
                )
                if score is not None and (best is None or score > best):
                    best = score
            if best is not None:
                scores.append(best)
        out[purpose] = scores
    return out


def suggest_lanes(
    score: float | None,
    incumbents: Mapping[str, list[float]],
    *,
    exclude: float | None = None,
) -> tuple[str, ...]:
    """Lanes a scored candidate could back up: it scores at least the lane's weakest scored model.

    Backups add capacity, so they are usually weaker than a lane's primary; the bar is "no worse
    than something this lane already accepts", not "better than everything in it" (the backtest
    showed the stricter rule rejected 12 real lane memberships). A lane with no scored model is
    offered (no comparison is possible). An unscored candidate is offered no lane. `exclude`
    removes one score from each lane (the backtest's own model).
    """
    if score is None:
        return ()
    offered = []
    for purpose, scores in sorted(incumbents.items()):
        pool = list(scores)
        if exclude is not None and exclude in pool:
            pool.remove(exclude)
        if not pool or score >= min(pool):
            offered.append(purpose)
    return tuple(offered)


def other_lanes(
    score: float | None, incumbents: Mapping[str, list[float]]
) -> tuple[tuple[str, float], ...]:
    """Eligible lanes not recommended, with the gap to each lane's weakest scored model."""
    if score is None:
        return ()
    return tuple(
        (purpose, round(min(scores) - score, 1))
        for purpose, scores in sorted(incumbents.items())
        if scores and score < min(scores)
    )


def lane_usage(
    route: Mapping[str, Any],
    lanes: Mapping[str, LaneConfig],
    routes: list[Mapping[str, Any]],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Lanes this route serves, each with what would still serve that lane without it."""
    keys = _route_model_keys(route)
    rid = route.get("route_id")

    def live_elsewhere(model: str) -> bool:
        return any(
            other.get("route_id") != rid
            and other.get("rpd") != 0
            and model in _route_model_keys(other)
            for other in routes
        )

    usage = []
    for purpose, lane in sorted(lanes.items()):
        lane_models = (*lane.models, *lane.backup_models)
        if not keys & set(lane_models):
            continue
        alternates = tuple(
            f"{m} (its other routes)" if m in keys else m for m in lane_models if live_elsewhere(m)
        )
        usage.append((purpose, alternates))
    return tuple(usage)


def _context_limit(record: Mapping[str, Any]) -> int | None:
    return positive_bound(record, _CONTEXT_KEYS)


def _days_since(stamp: str | None, today: date) -> int | None:
    if not stamp:
        return None
    try:
        return (today - date.fromisoformat(stamp)).days
    except ValueError:
        return None


CanaryFn = Callable[[ProviderRules, Mapping[str, Any], str, str, requests.Session], Response]


def reconcile(
    limits: Mapping[str, Any],
    lanes: Mapping[str, LaneConfig],
    decisions: Decisions,
    quality: QualityIndex,
    previous_state: Mapping[str, Any],
    *,
    session: requests.Session,
    control: DispatchControl,
    today: date,
    providers: set[str] | None = None,
    due_only: bool = False,
    candidate_keys: set[str] | None = None,
    route_ids: set[str] | None = None,
    canary_fn: CanaryFn = canary,
    sleep: Callable[[float], None] = time.sleep,
    structured_canary_fn: Callable[..., dict[str, dict[str, Any]]] = structured_canary,
    rate_run_id: str = "",
    rate_now: datetime | None = None,
    early_rate_checks: set[str] | None = None,
    context_run=None,
) -> Report:
    report = Report(floor=quality.floor)
    rechecked_routes: set[str] = set()
    structured_checks = dict(previous_state.get("structured_checks") or {})
    canary_memory: dict[str, dict[str, Any]] = dict(previous_state.get("canaries") or {})
    route_checks: dict[str, str] = dict(previous_state.get("route_checks") or {})
    deferred: dict[str, str] = dict(previous_state.get("deferred") or {})
    known_anomalies = set(previous_state.get("anomalies") or [])
    if quality.error:
        report.observations.append(f"quality: {quality.error}; every candidate is unscored")

    routes = [r for r in limits.get("routes") or [] if isinstance(r, dict)]
    incumbents = lane_incumbent_scores(lanes, routes, quality)
    # Data-driven usability floor: no candidate smaller than the smallest configured free route.
    min_context = min(
        (
            int(r["input_context_limit"])
            for r in routes
            if r.get("free") and isinstance(r.get("input_context_limit"), int)
        ),
        default=0,
    )
    provider_cfgs = limits.get("providers") or {}
    catalogs = dict(previous_state.get("catalogs") or {})
    selected = sorted(providers or provider_cfgs)
    if context_run and not due_only:
        from citypods.provider_catalog.limits import plan_context_scan

        ordered = plan_context_scan(limits, context_run.get("rotation", {}))
        rank = {route["route_id"]: i for i, route in enumerate(ordered)}
        selected.sort(
            key=lambda provider: min(
                (rank.get(r["route_id"], len(rank)) for r in routes if r["provider"] == provider),
                default=len(rank),
            )
        )
    for provider in selected:
        rules = all_rules().get(provider)
        cfg = provider_cfgs.get(provider)
        if rules is None or cfg is None:
            report.observations.append(f"{provider}: no plugin or no config; skipped")
            continue
        provider_routes = [r for r in routes if r.get("provider") == provider]
        # A daily run calls only providers with deferred routes or bounded early-check triggers.
        if due_only and not any(
            str(r.get("route_id")) in deferred
            or str(r.get("route_id")) in (early_rate_checks or set())
            for r in provider_routes
        ):
            continue
        catalog = fetch_catalog(rules, cfg, session)
        if catalog.error:
            report.anomalies.append(
                Anomaly(provider, f"{provider}:catalog", "-", "inconclusive", catalog.error, False)
            )
        else:
            report.observations.append(f"{provider}: catalog lists {len(catalog.models)} models")
        ctx = rules.prepare(session) if rules.prepare and not due_only else {}
        if ctx and ctx.get("error"):
            report.observations.append(
                f"{provider}: free-evidence source unavailable: {ctx['error']}"
            )

        catalog_evidence = CatalogEvidence(
            provider,
            today.isoformat(),
            not catalog.error and rules.catalog_is_complete,
            tuple(
                (
                    model,
                    rules.chat_filter(model, record),
                    rules.free_evidence(model, record, ctx) if rules.free_evidence else False,
                    _context_limit(record),
                    positive_bound(
                        record,
                        (
                            "outputTokenLimit",
                            "max_output_tokens",
                            "max_completion_tokens",
                            "top_provider.max_completion_tokens",
                            "output_context_limit",
                        ),
                    ),
                    logical_identity(provider, model, rules, record, routes),
                )
                for model, record in catalog.models.items()
            ),
        )
        catalogs[provider] = asdict(catalog_evidence)

        def evidenced_candidate(
            model,
            record,
            score,
            proven_on,
            method,
            response=None,
            method_results=None,
            contended=False,
            *,
            provider=provider,
            rules=rules,
            cfg=cfg,
            ctx=ctx,
            catalog_evidence=catalog_evidence,
        ):
            candidate = _candidate(
                provider,
                model,
                record,
                score,
                rules,
                report.floor,
                incumbents,
                proven_on,
                method=method,
            )
            accounts = cfg.get("accounts") or []
            account_id = str(accounts[0].get("id") or "") if accounts else ""
            observations = (
                tuple(
                    LimitObservation(
                        metric, value, today.isoformat(), provider, account_id, scope=scope
                    )
                    for metric, value, scope in rules.limit_observations(response)
                    if value > 0
                )
                if response
                else ()
            )
            candidate.evidence = RouteEvidence(
                provider,
                model,
                account_id,
                proven_on,
                catalog_digest(catalog_evidence),
                catalog_evidence.complete,
                rules.free_evidence(model, record, ctx) is True if rules.free_evidence else False,
                candidate.context_limit,
                positive_bound(
                    record,
                    (
                        "outputTokenLimit",
                        "max_output_tokens",
                        "max_completion_tokens",
                        "top_provider.max_completion_tokens",
                        "output_context_limit",
                    ),
                ),
                logical_identity(provider, model, rules, record, routes),
                "proven",
                method,
                proven_on if method else None,
                observations,
                tuple(
                    (
                        m,
                        e.get("outcome", "inconclusive"),
                        e.get("first_byte_seconds"),
                        e.get("completion_seconds"),
                    )
                    for m, e in (method_results or {}).items()
                ),
                contended,
            ).to_dict()
            candidate.evidence_digest = candidate_digest(
                RouteEvidence.from_dict(candidate.evidence), candidate.lanes
            )
            return candidate

        # --- configured-route health: one live route per upstream model -------------------------
        health: list[dict[str, Any]] = []
        seen_models: set[str] = set()
        health_routes = provider_routes if candidate_keys is None or route_ids else []
        for route in health_routes:
            rid = str(route["route_id"])
            if route_ids is not None and rid not in route_ids:
                continue
            model = str(route.get("upstream_model") or "")
            if route.get("rpd") == 0 or (
                route_ids is None
                and model in seen_models
                and rid not in (early_rate_checks or set())
            ):
                continue
            seen_models.add(model)
            scarce = route.get("rpd") is not None and 0 < float(route["rpd"]) <= SCARCE_RPD
            rid = str(route["route_id"])
            if due_only and rid not in deferred and rid not in (early_rate_checks or set()):
                continue
            if scarce and not due_only and route_ids is None:
                since = _days_since(route_checks.get(rid), today)
                if since is not None and since < SCARCE_CHECK_DAYS:
                    continue
            health.append({"route": route, "scarce": scarce})

        # --- candidates ------------------------------------------------------------------------
        candidates: list[tuple[str, Mapping[str, Any], float | None, bool]] = []
        configured_models = {str(r.get("upstream_model") or "") for r in provider_routes}
        configured_identities = {_identity(m, rules.free_suffix): m for m in configured_models if m}
        if not due_only and not rules.observation_only and not catalog.error:
            for model, record in sorted(catalog.models.items()):
                if candidate_keys is not None and f"{provider}/{model}" not in candidate_keys:
                    continue
                if model in configured_models or not rules.chat_filter(model, record):
                    continue
                if decisions.is_ignored(provider, model, today):
                    continue
                alias_of = configured_identities.get(_identity(model, rules.free_suffix))
                if alias_of:
                    continue  # the same model under another name is already configured
                context = _context_limit(record)
                if context is not None and context < min_context:
                    continue
                free = rules.free_evidence(model, record, ctx) if rules.free_evidence else False
                if free is False:
                    continue
                memory = canary_memory.get(f"{provider}/{model}")
                age = _days_since(memory.get("on") if memory else None, today)
                remembered = (
                    candidate_keys is None
                    and memory
                    and age is not None
                    and age < CANARY_MEMORY_DAYS
                    and memory.get("verdict") in DEFINITIVE_VERDICTS
                    and (memory.get("verdict") != "proven" or memory.get("method"))
                )
                if remembered:
                    if memory.get("verdict") == "proven":
                        score = quality.score(
                            model, creator=rules.creator, free_suffix=rules.free_suffix
                        )
                        report.candidates.append(
                            evidenced_candidate(
                                model,
                                record,
                                score,
                                memory["on"],
                                memory.get("method"),
                                method_results=memory.get("structured_results"),
                                contended=True,
                            )
                        )
                    continue
                free = rules.free_evidence(model, record, ctx) if rules.free_evidence else False
                if not free:
                    continue
                score = quality.score(model, creator=rules.creator, free_suffix=rules.free_suffix)
                candidates.append((model, record, score, memory is not None))
            # Never-tried first, then retries of inconclusive results; best-scored first within
            # each, unscored last. So a candidate that keeps failing cannot starve new ones.
            candidates.sort(key=lambda c: (c[3], c[2] is None, -(c[2] or 0), c[0]))
            if len(candidates) > CANARY_BUDGET_PER_PROVIDER:
                report.observations.append(
                    f"{provider}: {len(candidates) - CANARY_BUDGET_PER_PROVIDER} more free "
                    f"candidate(s) wait for later runs (budget {CANARY_BUDGET_PER_PROVIDER}/run)"
                )
            candidates = candidates[:CANARY_BUDGET_PER_PROVIDER]

        if not health and not candidates and not context_run:
            continue

        runner = (
            RateProbeRunner(
                apply=True,
                max_requests_total=3,
                max_requests_per_route=3,
                max_wall_seconds=900,
                session=session,
            )
            if rate_run_id or context_run
            else None
        )

        def count_existing_request(rid, runner=runner):
            if runner is not None:
                runner.total_requests += 1
                runner.route_request_counts[rid] = runner.route_request_counts.get(rid, 0) + 1

        with control.paused(provider) as pause:
            quota = {k: dict(v) for k, v in control.route_quota(provider).items()}
            if pause.contended:
                report.observations.append(
                    f"{provider}: probes ran without a drained dispatch pause; capacity signals "
                    "may reflect production traffic"
                )
            first_probe = True
            cooldown = rules.canary_interval_seconds

            def verify(
                model: str,
                key: str,
                route: Mapping[str, Any] | None = None,
                *,
                quota=quota,
                rules=rules,
                cfg=cfg,
                provider=provider,
            ):
                def before_attempt():
                    nonlocal first_probe
                    if route is not None:
                        rid = str(route["route_id"])
                        remaining = (quota.get(rid) or {}).get("rpd_remaining")
                        if route.get("rpd") is not None and 0 < float(route["rpd"]) <= SCARCE_RPD:
                            if remaining is None or remaining <= 0:
                                deferred[rid] = str((quota.get(rid) or {}).get("rpd_resets_at", ""))
                                return False
                    if not first_probe and rules.canary_interval_seconds:
                        sleep(rules.canary_interval_seconds)
                    pause.renew()  # renew AFTER spacing, immediately before the request
                    first_probe = False
                    if route is not None:
                        control.reserve(rid)
                        report.rate_attempted_routes.add(rid)
                        if remaining is not None:
                            quota[rid] = {**quota[rid], "rpd_remaining": remaining - 1}
                    count_existing_request(str(route["route_id"]) if route else model)
                    return True

                evidence = structured_canary_fn(
                    rules,
                    cfg,
                    model,
                    key,
                    session,
                    before_attempt=before_attempt,
                    methods=limits.get("structured_output_methods") or {},
                    renew_pause=pause.renew,
                )
                working = [
                    m for m in STRUCTURED_METHODS if evidence.get(m, {}).get("outcome") == "valid"
                ]
                report.observations.append(f"{provider}/{model}: structured output {evidence}")
                if any(
                    e.get("completion_seconds", 0) >= CANARY_TIMEOUT_SECONDS
                    for e in evidence.values()
                ):
                    report.observations.append(
                        f"{provider}/{model}: canary reached the Worker response ceiling; "
                        "task usability is unverified"
                    )
                return (working[0] if working else None), evidence

            for item in health:
                pause.renew()
                route = item["route"]
                rid = str(route["route_id"])
                model = str(route["upstream_model"])
                if item["scarce"] and (
                    (quota.get(rid) or {}).get("rpd_remaining") is None
                    or (quota.get(rid) or {}).get("rpd_remaining") <= 0
                ):
                    deferred[rid] = str((quota.get(rid) or {}).get("rpd_resets_at", ""))
                    report.observations.append(
                        f"{rid}: daily quota unavailable or spent; check deferred to reset"
                    )
                    continue
                api_key = account_key(cfg, route.get("account_id"))
                if not api_key:
                    report.observations.append(f"{rid}: no API key for its account; not checked")
                    continue
                if not first_probe and rules.canary_interval_seconds:
                    sleep(rules.canary_interval_seconds)
                pause.renew()
                first_probe = False
                control.reserve(rid)
                report.rate_attempted_routes.add(rid)
                count_existing_request(rid)
                response = canary_fn(rules, cfg, model, api_key, session)
                result = classify(response, rules)
                rechecked_routes.add(rid)
                if rate_run_id and not pause.contended and result.verdict == "proven":
                    # Duplicate physical model/account routes share capacity; assigning the
                    # header to both would turn one account ceiling into independent budgets.
                    siblings = [
                        r
                        for r in provider_routes
                        if r.get("upstream_model") == model
                        and r.get("account_id") == route.get("account_id")
                    ]
                    for metric, value, scope in rules.limit_observations(response):
                        if value == 0:
                            report.anomalies.append(
                                Anomaly(
                                    provider,
                                    rid,
                                    model,
                                    "rate_limit_zero",
                                    f"{metric}: observed zero; no scalar change",
                                    False,
                                )
                            )
                            continue
                        if len(siblings) == 1 and scope == "route":
                            report.rate_observations.append(
                                LimitObservation(
                                    metric,
                                    value,
                                    (rate_now or datetime.now(UTC)).isoformat(),
                                    provider,
                                    route["account_id"],
                                    rid,
                                    scope,
                                    run_id=rate_run_id,
                                )
                            )
                if (quota.get(rid) or {}).get("rpd_remaining") is not None:
                    quota[rid]["rpd_remaining"] -= 1
                deferred.pop(rid, None)
                if item["scarce"] and result.verdict == "quota_exhausted":
                    deferred[rid] = str((quota.get(rid) or {}).get("rpd_resets_at", ""))
                if result.verdict == "proven":
                    method, evidence = verify(model, api_key, route)
                    if item["scarce"] and any(e.get("status") == 429 for e in evidence.values()):
                        deferred[rid] = str((quota.get(rid) or {}).get("rpd_resets_at", ""))
                    if evidence:
                        previous = structured_checks.get(rid) or {}
                        structured_checks[rid] = {
                            "on": today.isoformat(),
                            "method": method or previous.get("method"),
                            "results": {**(previous.get("results") or {}), **evidence},
                        }
                    configured = resolved_method(route, routes, provider_cfgs)
                    outcome = evidence.get(configured, {}).get("outcome")
                    if outcome in {"empty", "invalid", "rejected"}:
                        anomaly = Anomaly(
                            provider,
                            rid,
                            model,
                            "structured_output_invalid",
                            f"structured_output_invalid: {configured} ({outcome}); "
                            f"verified alternative: {method}",
                            False,
                            lane_usage=lane_usage(route, lanes, routes),
                        )
                        anomaly.new = anomaly.key not in known_anomalies
                        acknowledged = decisions.acknowledgement(provider, model, anomaly.verdict)
                        if acknowledged:
                            report.observations.append(
                                f"{rid}: {anomaly.reason} -- acknowledged: {acknowledged['reason']}"
                            )
                        else:
                            report.anomalies.append(anomaly)
                route_checks[rid] = today.isoformat()
                absent = (
                    not catalog.error and rules.catalog_is_complete and model not in catalog.models
                )
                _record_health(
                    report,
                    Decisions() if route_ids is not None else decisions,
                    provider,
                    rid,
                    model,
                    result,
                    absent,
                    known_anomalies,
                    usage=lane_usage(route, lanes, routes),
                    observed_on=today.isoformat(),
                    config_digest=digest(route),
                    contended=pause.contended,
                )
            api_key = account_key(cfg)
            if not api_key:
                report.observations.append(f"{provider}: no API key; candidates not checked")
                continue
            for model, record, score, _retry in candidates:
                pause.renew()
                if not first_probe and rules.canary_interval_seconds:
                    sleep(rules.canary_interval_seconds)
                pause.renew()
                first_probe = False
                count_existing_request(model)
                response = canary_fn(rules, cfg, model, api_key or "", session)
                result = classify(response, rules)
                method, _evidence = None, {}
                if result.verdict == "proven":
                    method, _evidence = verify(model, api_key or "")
                    if method is None:
                        result = Classification("inconclusive", result.status, "JSON not verified")
                canary_memory[f"{provider}/{model}"] = {
                    "verdict": result.verdict,
                    "on": today.isoformat(),
                    "method": method,
                    "structured_results": _evidence,
                }
                if result.verdict == "proven":
                    report.candidates.append(
                        evidenced_candidate(
                            model,
                            record,
                            score,
                            today.isoformat(),
                            method,
                            response,
                            _evidence,
                            pause.contended,
                        )
                    )
                else:
                    report.observations.append(
                        f"{provider}/{model}: free-marked but {result.verdict} ({result.reason})"
                    )

            if runner is not None and not pause.contended:
                eligible = {o.route_id for o in report.rate_observations if o.provider == provider}
                # Unknown header scope cannot justify spending additional provider quota. Use
                # only an already-successful physical route with an explicitly configured pace.
                compiled = {r["route_id"]: r for r in load_route_catalog()}
                for route in provider_routes:
                    rid = route["route_id"]
                    if rid not in eligible or rid not in compiled or not route.get("rpm"):
                        continue
                    pace = min(float(route["rpm"]), float(cfg.get("rpm") or route["rpm"]), 1.0)
                    if pace <= 0:
                        continue
                    spacing = max(rules.canary_interval_seconds, 60 / pace)

                    def before_sample(
                        runner=runner,
                        spacing=spacing,
                        rid=rid,
                        route=route,
                        quota=quota,
                        pause=pause,
                    ):
                        nonlocal first_probe, cooldown
                        # Reserve room for control calls, a bounded request and the final
                        # cooldown. Recheck after control I/O before any provider request.
                        needed = (spacing if not first_probe else 0) + 55 + spacing
                        if time.monotonic() - runner.start_time + needed >= 900:
                            raise ProbeBudgetExceeded("maintenance time including cooldown spent")
                        runner.check_budgets(rid)
                        remaining = (quota.get(rid) or {}).get("rpd_remaining")
                        if route.get("rpd") is not None and 0 < float(route["rpd"]) <= SCARCE_RPD:
                            if remaining is None or remaining <= 0:
                                raise ProbeBudgetExceeded("scarce daily quota unavailable")
                        if not first_probe:
                            sleep(spacing)
                        pause.renew()
                        control.reserve(rid)
                        report.rate_attempted_routes.add(rid)
                        if remaining is not None:
                            quota[rid]["rpd_remaining"] -= 1
                        left = 900 - (time.monotonic() - runner.start_time) - spacing
                        if left <= 0:
                            raise ProbeBudgetExceeded("maintenance time spent during reservation")
                        runner.request_timeout_seconds = min(15, left)
                        cooldown = max(cooldown, spacing)
                        first_probe = False

                    def capture_sample(
                        sample, rules=rules, provider=provider, route=route, rid=rid
                    ):
                        response = Response(
                            status=sample.get("status"), headers=sample.get("headers") or {}
                        )
                        for metric, value, scope in rules.limit_observations(response):
                            if value == 0:
                                report.observations.append(f"{rid}/{metric}: observed zero")
                            elif scope == "route":
                                report.rate_observations.append(
                                    LimitObservation(
                                        metric,
                                        value,
                                        (rate_now or datetime.now(UTC)).isoformat(),
                                        provider,
                                        route["account_id"],
                                        rid,
                                        scope,
                                        run_id=rate_run_id,
                                    )
                                )

                    runner.before_request = before_sample
                    try:
                        runner.check_budgets(rid)
                        sample = run_phase_0(runner, compiled[rid])
                        capture_sample(sample)
                        if sample.get("status") == 200:
                            run_phase_1(
                                runner, compiled[rid], max_samples=1, on_response=capture_sample
                            )
                    except (ProbeBudgetExceeded, RouteBudgetExceeded):
                        report.observations.append(f"{provider}: optional rate sampling deferred")
                    # One provider-wide allowance, not three requests for each route.
                    break

            if context_run and not due_only:
                _measure_context_routes(
                    report,
                    context_run,
                    provider_routes,
                    cfg,
                    rules,
                    runner,
                    pause,
                    control,
                    session,
                    sleep=sleep,
                    cooldown=cooldown,
                )
                if report.context_attempted_routes:
                    first_probe = False

            # Unconfigured candidates have no route ledger entry. Keep the account paused until
            # its final probe's window has cleared, so resumed dispatch does not hit that window.
            if not first_probe and cooldown:
                sleep(cooldown)
                pause.renew()

        if pause.contended:
            report.rate_observations[:] = [
                o for o in report.rate_observations if o.provider != provider
            ]
            report.context_observations[:] = [
                o for o in report.context_observations if o.provider != provider
            ]
            if context_run:
                from citypods.provider_catalog.limits import context_history_states

                context_run["states"] = context_history_states(
                    [*context_run["history"], *report.context_observations], now=datetime.now(UTC)
                )

    # Forget canary memory that has aged out so the state marker stays bounded.
    canary_memory = {
        key: value
        for key, value in canary_memory.items()
        if (_days_since(value.get("on"), today) or 0) < CANARY_MEMORY_DAYS
    }
    if due_only:
        _merge_into_last_full(report, previous_state.get("last_full") or {}, rechecked_routes)
    report.state = {
        "version": 3,
        "catalogs": catalogs,
        "structured_checks": {
            rid: evidence
            for rid, evidence in structured_checks.items()
            if any(r.get("route_id") == rid for r in routes)
        },
        "canaries": canary_memory,
        "route_checks": route_checks,
        "deferred": deferred,
        "anomalies": sorted(a.key for a in report.anomalies),
        "last_full": {
            "candidates": [asdict(c) for c in report.candidates],
            "anomalies": [asdict(a) for a in report.anomalies],
            "observations": list(report.observations),
        },
    }
    if context_run:
        report.state["context_v1"] = {
            "version": 1,
            "rotation": context_run.get("rotation", {}),
            "statuses": {
                f"{key[0]}/{key[1]}": value.status
                for key, value in context_run.get("states", {}).items()
            },
        }
        report.context_states = list(context_run.get("states", {}).values())
    elif previous_state.get("context_v1"):
        report.state["context_v1"] = previous_state["context_v1"]
    return report


def _merge_into_last_full(
    report: Report, last_full: Mapping[str, Any], rechecked_routes: set[str]
) -> None:
    """A due-only run re-checks a few deferred routes; everything else carries over unchanged.

    Anomalies for routes this run re-checked are replaced by this run's result; candidates and
    observations from the last full run are kept, so the issue never loses them.
    """
    from citypods.provider_catalog.limits import RateChange

    report.rate_changes = [RateChange(**v) for v in last_full.get("rate_changes") or []]
    from citypods.provider_catalog.apply import parse_context_choice
    from citypods.provider_catalog.limits import context_choice

    report.context_changes = []
    values = last_full.get("context_changes")
    for value in values[:128] if isinstance(values, list) else ():
        try:
            report.context_changes.append(parse_context_choice(context_choice(value)))
        except (TypeError, ValueError):
            continue  # editable advisory state cannot break a scheduled scan or supply proof
    rechecked = {a.route_id for a in report.anomalies} | rechecked_routes
    carried = [
        Anomaly(
            **{
                **a,
                "new": False,
                "lane_usage": tuple(
                    (lane, tuple(alts)) for lane, alts in a.get("lane_usage") or ()
                ),
            }
        )
        for a in last_full.get("anomalies") or []
        if a.get("route_id") not in rechecked
    ]
    candidates = [
        Candidate(
            **{
                **c,
                "links": tuple(map(tuple, c["links"])),
                "lanes": tuple(c["lanes"]),
                "other_lanes": tuple(map(tuple, c.get("other_lanes") or ())),
            }
        )
        for c in last_full.get("candidates") or []
        if c.get("structured_output_method")
    ]
    report.anomalies = carried + report.anomalies
    report.candidates = candidates + report.candidates
    report.observations = list(last_full.get("observations") or []) + report.observations


def _candidate(
    provider: str,
    model: str,
    record: Mapping[str, Any],
    score: float | None,
    rules: ProviderRules,
    floor: float | None,
    incumbents: Mapping[str, list[float]],
    proven_on: str,
    method: str | None = None,
) -> Candidate:
    return Candidate(
        provider=provider,
        model=model,
        score=score,
        below_floor=None if score is None or floor is None else score < floor,
        context_limit=_context_limit(record),
        links=rules.research_links(model),
        lanes=suggest_lanes(score, incumbents),
        proven_on=proven_on,
        structured_output_method=method,
        structured_output_verified_on=proven_on if method else None,
        other_lanes=other_lanes(score, incumbents),
    )


def _record_health(
    report: Report,
    decisions: Decisions,
    provider: str,
    route_id: str,
    model: str,
    result: Classification,
    absent: bool,
    known: set[str],
    *,
    usage: tuple[tuple[str, tuple[str, ...]], ...] = (),
    observed_on: str | None = None,
    config_digest: str | None = None,
    contended: bool = True,
) -> None:
    where = " (absent from catalog)" if absent else ""
    if result.verdict not in ANOMALY_VERDICTS:
        if absent:
            report.observations.append(f"{route_id}: absent from catalog but {result.verdict}")
        return
    acknowledged = decisions.acknowledgement(provider, model, result.verdict)
    if acknowledged:
        report.observations.append(
            f"{route_id}: {result.verdict}{where} -- acknowledged: {acknowledged.get('reason')}"
        )
        return
    anomaly = Anomaly(
        provider,
        route_id,
        model,
        result.verdict,
        result.reason,
        absent,
        lane_usage=usage,
        observed_on=observed_on,
        config_digest=config_digest,
        contended=contended,
    )
    anomaly.new = anomaly.key not in known
    report.anomalies.append(anomaly)


@dataclass
class BacktestRow:
    provider: str
    route_id: str
    model: str
    gate: str  # "discoverable", or the first discovery gate that would drop this model
    score: float | None
    lanes_offered: tuple[str, ...]
    lanes_actual: tuple[str, ...]


def backtest_discovery(
    limits: Mapping[str, Any],
    lanes: Mapping[str, LaneConfig],
    quality: QualityIndex,
    *,
    session: requests.Session,
    providers: set[str] | None = None,
) -> list[BacktestRow]:
    """Would discovery have found each CONFIGURED model if it were not configured yet?

    Replays the candidate gates (plugin, catalog, chat filter, free evidence) for one route per
    configured (provider, upstream model), plus the lane offer, without sending canaries -- a
    configured route's canary is its weekly health check. A model dropped by a gate here is a model
    the reconciler could never have proposed: evidence to refine a plugin, not a config error.
    """
    rows: list[BacktestRow] = []
    routes = [r for r in limits.get("routes") or [] if isinstance(r, dict)]
    incumbents = lane_incumbent_scores(lanes, routes, quality)
    catalogs: dict[str, Any] = {}
    contexts: dict[str, Mapping[str, Any]] = {}
    seen: set[tuple[str, str]] = set()
    for route in routes:
        provider = str(route.get("provider"))
        model = str(route.get("upstream_model") or "")
        if (providers and provider not in providers) or (provider, model) in seen:
            continue
        seen.add((provider, model))
        rules = all_rules().get(provider)
        cfg = (limits.get("providers") or {}).get(provider)
        keys = _route_model_keys(route)
        # Only lanes that can take catalog backups: per-model comparison lanes never can.
        actual = tuple(
            sorted(
                p
                for p, lane in lanes.items()
                if lane.accepts_catalog_backups and keys & set((*lane.models, *lane.backup_models))
            )
        )
        score = (
            quality.score(model, creator=rules.creator, free_suffix=rules.free_suffix)
            if rules
            else None
        )

        gate = "no plugin"
        if rules is not None and cfg is not None:
            if provider not in catalogs and not rules.observation_only:
                catalogs[provider] = fetch_catalog(rules, cfg, session)
                contexts[provider] = rules.prepare(session) if rules.prepare else {}
            gate = _discovery_gate(rules, model, catalogs.get(provider), contexts.get(provider))
        rows.append(
            BacktestRow(
                provider,
                str(route["route_id"]),
                model,
                gate,
                score,
                suggest_lanes(score, incumbents, exclude=score) if gate == "discoverable" else (),
                actual,
            )
        )
    return rows


def _discovery_gate(
    rules: ProviderRules, model: str, catalog: Any, ctx: Mapping[str, Any] | None
) -> str:
    """The first candidate gate `model` fails, or "discoverable"."""
    if rules.observation_only:
        return "observation-only provider"
    if catalog.error:
        return f"catalog unavailable ({catalog.error})"
    record = catalog.models.get(model)
    if record is None:
        return "not in catalog"
    if not rules.chat_filter(model, record):
        return "chat filter"
    free = rules.free_evidence(model, record, ctx or {}) if rules.free_evidence else False
    if free is None:
        return "free evidence unavailable"
    return "discoverable" if free else "no free evidence"


def backtest_summary(rows: list[BacktestRow]) -> str:
    """One observation line: how many configured models discovery would re-find, and why not."""
    found = sum(r.gate == "discoverable" for r in rows)
    misses = [f"{r.provider}/{r.model} ({r.gate})" for r in rows if r.gate != "discoverable"]
    tail = f"; not: {', '.join(misses)}" if misses else ""
    return f"discovery self-check: {found}/{len(rows)} configured models would be re-found{tail}"


def _measure_context_routes(
    report, context_run, routes, cfg, rules, runner, pause, control, session, *, sleep, cooldown
):
    """Optional context calls share the existing provider request/time allowance, input first."""
    import secrets
    from dataclasses import replace
    from fractions import Fraction

    from citypods.compute.llm_dispatch_pause import DispatchPauseError
    from citypods.provider_catalog.evidence import digest
    from citypods.provider_catalog.limits import (
        ContextSearchState,
        advance_context_state,
        context_history_states,
        context_input_ratio,
        next_context_probe,
        plan_context_scan,
    )
    from citypods.provider_catalog.probe import build_context_request, measure_context

    if not context_run or context_run.get("abandoned") or pause.contended or runner is None:
        return
    now = datetime.now(UTC)
    states = context_run.setdefault(
        "states", context_history_states(context_run["history"], now=now)
    )
    eligible = plan_context_scan({"routes": routes}, context_run.get("rotation", {}))
    for route in eligible:
        rid = route["route_id"]
        readiness = context_run["readiness"].get(rid) or {}
        if not readiness.get("enabled") or not readiness.get("quota_scope"):
            report.observations.append(f"{rid}: context deferred (disabled or unknown quota scope)")
            continue
        key = None
        for dimension in ("input", "output"):
            observed = None
            for _ in range(6):
                if context_run.get("abandoned"):
                    return
                budget = context_run["budget"]
                budget["route_requests"] = context_run.setdefault("route_counts", {}).get(rid, 0)
                budget["remaining_seconds"] = min(
                    (context_run["deadline_ms"] - time.time() * 1000) / 1000,
                    900 - (time.monotonic() - runner.start_time) - cooldown,
                )
                if (
                    runner.total_requests >= runner.max_requests_total
                    or budget["remaining_seconds"] < 205
                ):
                    return
                prior = Fraction(str(route.get("input_token_ratio") or 1))
                baseline = (
                    route.get("hard_input_ceiling") or route.get("input_context_limit")
                    if dimension == "input"
                    else route.get("output_context_limit")
                )
                if type(baseline) is not int or baseline <= 0:
                    break
                # A small fixture is used only to resolve the documented measurement identity.
                seed = build_context_request(
                    route,
                    cfg,
                    rules,
                    dimension=dimension,
                    target=1000,
                    ratio=prior,
                    attempt_ordinal=1,
                    nonce="offline-identity-" * 4,
                )
                key = (seed.identity_digest, dimension)
                from citypods.provider_catalog.rules import context_parser_support

                supported, reason = context_parser_support(rules, seed)
                if not supported:
                    report.observations.append(f"{rid}/{dimension}: context deferred ({reason})")
                    break
                current = states.get(key, ContextSearchState(*key))
                target = next_context_probe(
                    current,
                    {
                        "baseline": baseline,
                        "opposite_reservation": 256 if dimension == "input" else 2048,
                    },
                    budget,
                )
                states[key] = target
                if target.status in {"budget_limited", "uncertain", "unsupported"}:
                    report.observations.append(
                        f"{rid}/{dimension}: context {target.status}; "
                        f"desired target {target.next_target}"
                    )
                    break
                ordinal = context_run.setdefault("ordinal", 0) + 1
                context_run["ordinal"] = ordinal
                request = build_context_request(
                    route,
                    cfg,
                    rules,
                    dimension=dimension,
                    target=target.next_target,
                    ratio=context_input_ratio(current, prior),
                    attempt_ordinal=ordinal,
                    nonce=context_run.setdefault("nonce", secrets.token_hex(32)),
                )
                if request.reserved_input > budget["remaining_input"] or (
                    request.requested_output > budget["remaining_output"]
                ):
                    break
                attempt_id = digest([context_run["run_id"], rid, dimension, ordinal])
                request_digest = digest(
                    [request.identity_digest, request.fixture_version, request.shaped_body]
                )
                call_admitted = False

                def before_call(
                    rid=rid,
                    dimension=dimension,
                    request=request,
                    budget=budget,
                    attempt_id=attempt_id,
                    request_digest=request_digest,
                ):
                    nonlocal call_admitted
                    if context_run["deadline_ms"] - time.time() * 1000 < 205000:
                        raise DispatchPauseError("context session has insufficient cleanup time")
                    runner.check_budgets(rid)
                    if rules.canary_interval_seconds:
                        sleep(rules.canary_interval_seconds)
                    pause.renew()
                    report.context_attempted_routes.add(rid)
                    admitted = control.client.reserve_context(
                        run_id=context_run["run_id"],
                        catalog_digest=context_run["catalog_digest"],
                        route_id=rid,
                        dimension=dimension,
                        attempt_id=attempt_id,
                        input_tokens=request.reserved_input,
                        output_tokens=request.requested_output,
                        request_digest=request_digest,
                    )
                    call_admitted = True
                    budget["remaining_input"] -= request.reserved_input
                    budget["remaining_output"] -= request.requested_output
                    budget["remaining_requests"] -= 1
                    runner.total_requests += 1
                    runner.route_request_counts[rid] = runner.route_request_counts.get(rid, 0) + 1
                    context_run["route_counts"][rid] = budget["route_requests"] + 1
                    return admitted

                try:
                    observed = measure_context(
                        request,
                        session=session,
                        before_call=before_call,
                        clock=time.monotonic,
                        timeout=120,
                    )
                except (DispatchPauseError, ProbeBudgetExceeded, RouteBudgetExceeded):
                    context_run["abandoned"] = (
                        True  # admission ambiguity never permits a new attempt
                    )
                    report.observations.append(
                        f"{rid}: context admission unavailable; run abandoned"
                    )
                    return
                if not call_admitted:
                    report.observations.append(f"{rid}: context preflight unavailable; deferred")
                    break
                observed = replace(
                    observed,
                    observed_at=datetime.now(UTC).isoformat(),
                    run_id=context_run["run_id"],
                    head_sha=context_run["head_sha"],
                    attempt_id=attempt_id,
                )
                report.context_observations.append(observed)
                current = advance_context_state(target, observed)
                states[key] = current
                context_run.setdefault("rotation", {})[rid] = observed.observed_at
                if observed.outcome != "verified" or current.status in {"uncertain", "converged"}:
                    break
            if dimension == "input" and (
                key is None
                or states.get(key) is None
                or states[key].success is None
                or states[key].status == "uncertain"
                or (observed is not None and observed.outcome != "verified")
            ):
                break
    report.context_states = list(states.values())
