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
from functools import lru_cache
from typing import Any, Protocol

import requests

from citypods.compute.api_shape import route_speaks_chat
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
# New-route discovery runs on the weekly full scan after the existing route gates pass.
DISCOVERY_ACTIVATION_ENABLED = True
DISCOVERY_SCHEDULED_ENABLED = True
ANOMALY_VERDICTS = frozenset({"retired", "not_served", "not_entitled", "account_blocked"})
RETIREMENT_VERDICTS = frozenset({"retired", "not_served"})
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
    # Request-cap search is advisory and belongs only to newly discovered candidates.
    discovery: dict[str, Any] | None = None

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
    retirement_confirmation: dict[str, Any] | None = None

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
    discovery_sessions: list[dict[str, Any]] = field(default_factory=list)

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
    discovery_enabled: bool = False,
    discovery_automatic: bool = False,
    discovery_axis: str = "all",
    discovery_history=(),
    discovery_history_calls: Mapping[str, int] | None = None,
    discovery_input_success_runs: Mapping[tuple[str, str, str, str], tuple[str, ...]] | None = None,
    discovery_receipt_id: str | None = None,
    discovery_receipt_ids: Mapping[str, str] | None = None,
    discovery_run_id: str = "",
    retry_candidate_keys: set[str] | None = None,
) -> Report:
    report = Report(floor=quality.floor)
    if not isinstance(discovery_axis, str) or discovery_axis not in {"all", "input", "output"}:
        raise ValueError("invalid discovery axis")
    if type(discovery_automatic) is not bool:
        raise ValueError("invalid automatic discovery mode")
    if discovery_axis != "all" and not (discovery_enabled and candidate_keys):
        raise ValueError("axis selection requires an exact manual discovery candidate")
    discovery_shared_budget = None
    if discovery_enabled:
        from citypods.provider_catalog.discovery import (
            MAX_RUN_CALLS,
            MAX_RUN_INPUT,
            MAX_RUN_OUTPUT,
            MAX_RUN_SESSIONS,
            DiscoveryBudget,
        )

        discovery_shared_budget = DiscoveryBudget(
            max_calls=MAX_RUN_CALLS,
            max_input=MAX_RUN_INPUT,
            max_output=MAX_RUN_OUTPUT,
        )
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
    if discovery_automatic:
        pending_since: dict[str, str] = {}
        for key, memory in canary_memory.items():
            provider, separator, _model = key.partition("/")
            pending_on = memory.get("discovery_pending_on")
            if separator and memory.get("discovery_pending") and isinstance(pending_on, str):
                pending_since[provider] = min(pending_since.get(provider, pending_on), pending_on)
        original_order = {provider: index for index, provider in enumerate(selected)}
        selected.sort(
            key=lambda provider: (
                0 if provider in pending_since else 1,
                pending_since.get(provider, ""),
                original_order[provider],
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
        if (
            due_only
            and not any(
                str(r.get("route_id")) in deferred
                or str(r.get("route_id")) in (early_rate_checks or set())
                for r in provider_routes
            )
            and not any(key.startswith(f"{provider}/") for key in retry_candidate_keys or ())
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
            if discovery_history:
                candidate.discovery = _discovery_history_summary(
                    provider,
                    model,
                    record,
                    rules,
                    cfg,
                    discovery_history,
                    (discovery_history_calls or {}).get(
                        _candidate_discovery_identity(provider, model, rules, cfg), 0
                    ),
                    discovery_input_success_runs or {},
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
            # The health canary is a chat completion; a non-chat route (JEV, review/53 PR2) cannot
            # answer one, and on BeatAPI it would spend the account's single request window.
            if not route_speaks_chat(route):
                continue
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
        if (
            (not due_only or retry_candidate_keys)
            and not rules.observation_only
            and not catalog.error
        ):
            retry_models_seen: set[str] = set()

            def terminal_retry(
                model,
                record,
                reason,
                calls=0,
                _provider=provider,
                _cfg=cfg,
                _rules=rules,
            ):
                key = f"{_provider}/{model}"
                receipt_id = (discovery_receipt_ids or {}).get(key)
                if (
                    not discovery_enabled
                    or key not in ((retry_candidate_keys or set()) | (candidate_keys or set()))
                    or (not receipt_id and key not in (candidate_keys or set()))
                    or any(
                        row.get("session", {}).get("provider") == _provider
                        and row.get("session", {}).get("upstream_model") == model
                        for row in report.discovery_sessions
                    )
                ):
                    return
                report.discovery_sessions.append(
                    _terminal_discovery_session(
                        _provider,
                        model,
                        record,
                        _cfg,
                        _rules,
                        run_id=discovery_run_id or "local",
                        today=today,
                        receipt_id=receipt_id,
                        reason=reason,
                        calls=calls,
                    )
                )

            for model, record in sorted(catalog.models.items()):
                key = f"{provider}/{model}"
                if key in ((retry_candidate_keys or set()) | (candidate_keys or set())):
                    retry_models_seen.add(key)
                if candidate_keys is not None and key not in candidate_keys:
                    continue
                if retry_candidate_keys is not None and key not in retry_candidate_keys:
                    continue
                if model in configured_models:
                    terminal_retry(model, record, "route_already_configured")
                    continue
                if not rules.chat_filter(model, record):
                    terminal_retry(model, record, "unsupported_chat_route")
                    continue
                if decisions.is_ignored(provider, model, today):
                    terminal_retry(model, record, "route_ignored_by_maintainer")
                    continue
                alias_of = configured_identities.get(_identity(model, rules.free_suffix))
                if alias_of:
                    terminal_retry(model, record, "route_alias_already_configured")
                    continue  # the same model under another name is already configured
                context = _context_limit(record)
                if context is not None and context < min_context:
                    terminal_retry(model, record, "catalog_context_below_candidate_floor")
                    continue
                free = rules.free_evidence(model, record, ctx) if rules.free_evidence else False
                if free is False:
                    terminal_retry(model, record, "free_route_evidence_no_longer_eligible")
                    continue
                memory = canary_memory.get(f"{provider}/{model}")
                age = _days_since(memory.get("on") if memory else None, today)
                remembered = (
                    candidate_keys is None
                    and retry_candidate_keys is None
                    and memory
                    and age is not None
                    and age < CANARY_MEMORY_DAYS
                    and not memory.get("discovery_pending")
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
                candidates.append(
                    (
                        model,
                        record,
                        score,
                        memory is not None and not memory.get("discovery_pending"),
                    )
                )
            for key in (retry_candidate_keys or set()) | (candidate_keys or set()):
                provider_prefix, separator, model = key.partition("/")
                if (
                    provider_prefix == provider
                    and separator
                    and model
                    and key not in retry_models_seen
                    and not catalog.error
                ):
                    terminal_retry(model, {}, "route_absent_from_current_catalog")
            # Never-tried first, then retries of inconclusive results; best-scored first within
            # each, unscored last. So a candidate that keeps failing cannot starve new ones.
            candidates.sort(key=lambda c: (c[3], c[2] is None, -(c[2] or 0), c[0]))
            if len(candidates) > CANARY_BUDGET_PER_PROVIDER:
                report.observations.append(
                    f"{provider}: {len(candidates) - CANARY_BUDGET_PER_PROVIDER} more free "
                    f"candidate(s) wait for later runs (budget {CANARY_BUDGET_PER_PROVIDER}/run)"
                )
            candidates = candidates[:CANARY_BUDGET_PER_PROVIDER]

        context_ready = bool(context_run) and not due_only and not context_run.get("abandoned")
        if context_ready:
            context_ready = any(
                (ready := context_run["readiness"].get(route["route_id"]) or {}).get("enabled")
                and ready.get("quota_scope")
                == f"{route['provider']}:{route['account_id']}:{route['upstream_model']}"
                for route in provider_routes
                if route.get("free") is True and route.get("rpd") != 0
            )
        if not health and not candidates and not context_ready:
            continue

        runner = (
            RateProbeRunner(
                apply=True,
                max_requests_total=3,
                max_requests_per_route=3,
                max_wall_seconds=900,
                session=session,
            )
            if rate_run_id or context_ready
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
                discovery_pending_on = memory.get("discovery_pending_on") if memory else None
                canary_memory[f"{provider}/{model}"] = {
                    "verdict": result.verdict,
                    "on": today.isoformat(),
                    "method": method,
                    "structured_results": _evidence,
                }
                if discovery_pending_on:
                    canary_memory[f"{provider}/{model}"]["discovery_pending_on"] = (
                        discovery_pending_on
                    )
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
                    candidate = report.candidates[-1]
                    if (
                        discovery_enabled
                        and not pause.contended
                        and len(report.discovery_sessions) < MAX_RUN_SESSIONS
                        and not any(
                            row.get("session", {}).get("provider") == provider
                            for row in report.discovery_sessions
                        )
                    ):
                        discovery_floor = quality.score("openai/gpt-oss-120b", creator="openai")
                        identity = _candidate_discovery_identity(provider, model, rules, cfg)
                        receipt_id = (discovery_receipt_ids or {}).get(
                            candidate.key, discovery_receipt_id
                        )
                        automatic_candidate = (
                            discovery_automatic
                            and receipt_id is None
                            and candidate.key not in (retry_candidate_keys or set())
                        )
                        already_discovered = automatic_candidate and identity in (
                            discovery_history_calls or {}
                        )
                        if already_discovered:
                            if not candidate.discovery:
                                candidate.discovery = {
                                    "retryable": False,
                                    "reason": "automatic_discovery_already_recorded",
                                    "detail": (
                                        "Authenticated discovery history already exists; use the "
                                        "reviewed test-again choice to spend another session."
                                    ),
                                    "axes": [],
                                    "calls": 0,
                                    "input_tokens": 0,
                                    "output_tokens": 0,
                                }
                        elif automatic_candidate and (
                            candidate.score is None
                            or discovery_floor is None
                            or candidate.score <= discovery_floor
                        ):
                            candidate.discovery = {
                                "retryable": False,
                                "reason": "below_automatic_quality_floor",
                                "detail": (
                                    "Automatic input discovery requires an AA score strictly "
                                    "above the current GPT-OSS-120B score."
                                ),
                                "axes": [],
                                "calls": 1 + len((candidate.evidence or {}).get("methods") or ()),
                                "input_tokens": 0,
                                "output_tokens": 0,
                            }
                        else:
                            selected_axes = (
                                ("input",)
                                if automatic_candidate
                                else (discovery_axis,)
                                if discovery_axis != "all"
                                else None
                            )
                            try:
                                session_record = _discover_candidate_caps(
                                    candidate,
                                    record,
                                    provider_cfg=cfg,
                                    rules=rules,
                                    pause=pause,
                                    session=session,
                                    today=today,
                                    run_id=discovery_run_id,
                                    receipt_id=(discovery_receipt_ids or {}).get(
                                        candidate.key, discovery_receipt_id
                                    ),
                                    history=discovery_history,
                                    shared_budget=discovery_shared_budget,
                                    selected_axes=selected_axes,
                                    sleep=sleep,
                                )
                                candidate.discovery = session_record["summary"]
                                report.discovery_sessions.append(session_record["artifact"])
                            except Exception as exc:
                                # Discovery is advisory; preserve the proven reachability result.
                                candidate.discovery = {
                                    "retryable": False,
                                    "reason": "discovery_stopped_safely",
                                    "detail": f"Search stopped safely ({type(exc).__name__}).",
                                    "axes": [],
                                    "calls": 1
                                    + len((candidate.evidence or {}).get("methods") or ()),
                                    "input_tokens": 0,
                                    "output_tokens": 0,
                                }
                    elif discovery_automatic:
                        receipt_id = (discovery_receipt_ids or {}).get(
                            candidate.key, discovery_receipt_id
                        )
                        identity = _candidate_discovery_identity(provider, model, rules, cfg)
                        discovery_floor = quality.score("openai/gpt-oss-120b", creator="openai")
                        if (
                            receipt_id is None
                            and candidate.key not in (retry_candidate_keys or set())
                            and identity not in (discovery_history_calls or {})
                            and candidate.score is not None
                            and discovery_floor is not None
                            and candidate.score > discovery_floor
                        ):
                            canary_memory[f"{provider}/{model}"]["discovery_pending"] = True
                            canary_memory[f"{provider}/{model}"].setdefault(
                                "discovery_pending_on", today.isoformat()
                            )
                            candidate.discovery = {
                                "retryable": False,
                                "reason": "automatic_discovery_deferred_run_budget",
                                "detail": (
                                    "Reachability and quality passed, but this run's bounded "
                                    "discovery slot was unavailable; it will be rechecked "
                                    "next week."
                                ),
                                "axes": [],
                                "calls": 1 + len((candidate.evidence or {}).get("methods") or ()),
                                "input_tokens": 0,
                                "output_tokens": 0,
                            }
                        elif (
                            receipt_id is None
                            and candidate.key not in (retry_candidate_keys or set())
                            and (
                                candidate.score is None
                                or discovery_floor is None
                                or candidate.score <= discovery_floor
                            )
                        ):
                            candidate.discovery = {
                                "retryable": False,
                                "reason": "below_automatic_quality_floor",
                                "detail": (
                                    "Automatic input discovery requires an AA score strictly "
                                    "above the current GPT-OSS-120B score."
                                ),
                                "axes": [],
                                "calls": 1 + len((candidate.evidence or {}).get("methods") or ()),
                                "input_tokens": 0,
                                "output_tokens": 0,
                            }
                else:
                    terminal_retry(
                        model,
                        record,
                        "reachability_not_proven",
                        1 + len((_evidence or {}).get("methods") or ()),
                    )
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

            if context_ready:
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
    elif context_run is None:
        previous = Report()
        _merge_into_last_full(
            previous,
            {"context_changes": (previous_state.get("last_full") or {}).get("context_changes")},
            set(),
        )
        report.context_changes = previous.context_changes
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
            "context_changes": list(report.context_changes),
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
    current_candidates = report.candidates
    current_keys = {candidate.key for candidate in current_candidates}
    report.candidates = [candidate for candidate in candidates if candidate.key not in current_keys]
    report.candidates.extend(current_candidates)
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


def _candidate_discovery_identity(provider, model, rules, provider_cfg):
    from citypods.provider_catalog.discovery import discovery_identity
    from citypods.provider_catalog.probe import chat_url

    accounts = provider_cfg.get("accounts") or []
    if not accounts:
        return ""
    return discovery_identity(
        provider, str(accounts[0].get("id") or ""), model, chat_url(rules, provider_cfg)
    )


def _terminal_discovery_session(
    provider, model, record, provider_cfg, rules, *, run_id, today, receipt_id, reason, calls=0
):
    """Record a selected retry that reached a terminal eligibility result without size probes."""
    from citypods.provider_catalog.discovery import discovery_identity
    from citypods.provider_catalog.evidence import digest
    from citypods.provider_catalog.probe import chat_url

    accounts = provider_cfg.get("accounts") or []
    account_id = str(accounts[0].get("id") or "") if accounts else ""
    endpoint = chat_url(rules, provider_cfg)
    identity = discovery_identity(provider, account_id, model, endpoint)
    key = f"{provider}/{model}"
    metadata = {
        "session_id": digest([run_id, receipt_id, key, identity, today.isoformat(), reason]),
        "identity": identity,
        "provider": provider,
        "account_id": account_id,
        "upstream_model": model,
        "endpoint": endpoint,
        "catalog_digest": digest(record),
        "free_evidence_digest": digest([provider, model, record]),
        "parser_version": rules.discovery_parser_version,
        "fixture_version": "discovery-v1",
        "created_at": datetime.now(UTC).isoformat(),
        "sources": [
            rules.catalog.url
            or f"{provider_cfg.get('api_base', '').rstrip('/')}/{rules.catalog.path.lstrip('/')}"
        ],
        "receipt_id": receipt_id,
    }
    if calls:
        metadata["reachability_calls"] = calls
    return {"session": metadata, "attempts": [], "reason": reason}


@lru_cache(maxsize=1)
def _litellm_discovery_catalog():
    """Read pinned package metadata without importing LiteLLM or fetching its remote cost map."""
    import json
    from importlib.metadata import PackageNotFoundError, distribution

    try:
        package = distribution("litellm")
        path = package.locate_file("litellm/model_prices_and_context_window_backup.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        return (data if isinstance(data, dict) else {}), package.version
    except (PackageNotFoundError, OSError, ValueError):
        return {}, "unavailable"


def _litellm_discovery_limits(provider, model):
    """Exact provider-qualified lookup only; never borrow another host's deployment limits."""
    catalog, version = _litellm_discovery_catalog()
    namespace = {"nvidia": "nvidia_nim"}.get(provider, provider)
    key = f"{namespace}/{model}"
    row = catalog.get(key)
    if not isinstance(row, Mapping) or row.get("litellm_provider") != namespace:
        row = {}
    return {
        "key": key,
        "version": version,
        "input": positive_bound(row, ("max_input_tokens",)),
        "output": positive_bound(row, ("max_output_tokens",)),
        "source": "https://github.com/BerriAI/litellm/blob/main/"
        "model_prices_and_context_window.json",
    }


def _discovery_starting_points(record, litellm):
    """Catalog claims seed experiments, never pre-populate a passing or refused boundary."""
    from citypods.provider_catalog.discovery import (
        INPUT_OUTPUT_COUNTERPART,
        MAX_INPUT,
        MAX_OUTPUT,
    )

    claims = _discovery_claims(record)
    seeds = {}
    input_claim = claims["input"] or litellm.get("input")
    output_claim = claims["output"] or litellm.get("output")
    if input_claim:
        seeds["input"] = min(MAX_INPUT, input_claim)
        seeds["total_input_heavy"] = seeds["input"] + INPUT_OUTPUT_COUNTERPART
    if output_claim:
        seeds["output"] = min(MAX_OUTPUT, output_claim)
        seeds["total_output_heavy"] = seeds["output"] + 2048
    for axis in ("total_input_heavy", "total_output_heavy"):
        if claims[axis]:
            # Total is a starting target only; request shaping still enforces each dimension.
            ceiling = (
                MAX_INPUT + INPUT_OUTPUT_COUNTERPART
                if axis == "total_input_heavy"
                else MAX_OUTPUT + 2048
            )
            seeds[axis] = min(ceiling, claims[axis])
    return seeds


def _discovery_claims(record):
    """Extract the same published size claims for fresh and history-built reports."""
    return {
        "input": positive_bound(
            record, ("inputTokenLimit", "max_input_tokens", "input_context_limit")
        ),
        "output": positive_bound(
            record,
            (
                "outputTokenLimit",
                "max_output_tokens",
                "max_completion_tokens",
                "top_provider.max_completion_tokens",
                "output_context_limit",
            ),
        ),
        "total_input_heavy": positive_bound(
            record, ("context_length", "context_window", "max_context_length")
        ),
        "total_output_heavy": positive_bound(
            record, ("context_length", "context_window", "max_context_length")
        ),
    }


def _discovery_history_summary(
    provider,
    model,
    record,
    rules,
    provider_cfg,
    history,
    history_calls=0,
    input_success_runs=None,
):
    from citypods.provider_catalog.discovery import (
        Bracket,
        default_discovery_seed,
        discovery_identity,
    )
    from citypods.provider_catalog.probe import chat_url
    from citypods.provider_catalog.rules import general_discovery_rules

    rules = general_discovery_rules(rules)
    accounts = provider_cfg.get("accounts") or []
    if not accounts:
        return None
    identity = discovery_identity(
        provider, str(accounts[0].get("id") or ""), model, chat_url(rules, provider_cfg)
    )
    source = rules.catalog.url or (
        f"{provider_cfg.get('api_base', '').rstrip('/')}/{rules.catalog.path.lstrip('/')}"
    )
    compatible = [
        item
        for item in history
        if item.request.identity == identity
        and item.request.parser_version == rules.discovery_parser_version
        and item.request.fixture_version == "discovery-v1"
    ]
    if not compatible:
        return None
    claims = _discovery_claims(record)
    litellm = _litellm_discovery_limits(provider, model)
    axes = []
    for axis in ("input", "output", "total_input_heavy", "total_output_heavy"):
        bracket = Bracket(axis, default_discovery_seed(axis))
        for item in compatible:
            if item.request.axis == axis:
                bracket.observe(item)
        summary = bracket.summary()
        summary["published"] = claims[axis]
        summary["source"] = source
        axes.append(summary)
    characterization = _discovery_input_characterization(
        axes, compatible, identity=identity, parser_version=rules.discovery_parser_version
    )
    if characterization:
        signature = (
            characterization["identity"],
            characterization["parser_version"],
            characterization["fixture_version"],
            characterization["control"],
        )
        successful_run_ids = sorted(set((input_success_runs or {}).get(signature, ())))
        characterization["successful_run_ids"] = successful_run_ids
        matching = next(
            item
            for item in compatible
            if item.request.axis == "input"
            and item.request.control == characterization["control"]
            and item.request.parser_version == characterization["parser_version"]
            and item.request.fixture_version == characterization["fixture_version"]
            and item.outcome == "admitted"
            and item.verified
            and item.reported_input == characterization["admitted_input"]
            and item.reported_output == characterization["generated_output"]
            and item.request.input_tokens == characterization["admitted_local_estimate"]
            and item.request.output_tokens == characterization["requested_output"]
            and item.observed_at == characterization["observed_at"]
        )
        characterization["evidence_digest"] = digest(
            {
                "request": asdict(matching.request),
                "observation": asdict(matching),
                "successful_run_ids": successful_run_ids,
            }
        )
    return {
        "retryable": bool(compatible)
        and any(row["axis"] == "input" and row["status"] != "converged" for row in axes),
        "profile": "history",
        "reason": (
            "input_characterized"
            if any(row["axis"] == "input" and row["status"] == "converged" for row in axes)
            else "partial"
        ),
        "detail": "Bounds are rebuilt from authenticated, compatible provider observations.",
        "reachability": "verified",
        "calls": history_calls or len(compatible),
        "input_tokens": sum(item.request.input_tokens for item in compatible),
        "output_tokens": sum(item.request.output_tokens for item in compatible),
        "axes": axes,
        "input_characterization": characterization,
        "attempt_diagnostics": _discovery_attempt_diagnostics(compatible),
        "litellm": litellm,
    }


def _discovery_input_characterization(axes, attempts, *, identity, parser_version):
    """Advisory input evidence: never mix actual usage with estimated rejected sizes."""
    from citypods.provider_catalog.discovery import INPUT_SIZE_GUIDANCE, positive

    primary = next((axis for axis in axes if axis.get("axis") == "input"), {})
    lower = primary.get("admitted")
    control = primary.get("control")
    if not positive(lower) or not control:
        return {}
    rows = []
    for item in attempts:
        row = item if isinstance(item, Mapping) else asdict(item)
        req, obs = row.get("request") or {}, row.get("observation", row)
        if (
            req.get("identity") == identity
            and req.get("parser_version") == parser_version
            and req.get("fixture_version") == "discovery-v1"
            and req.get("axis") == "input"
            and req.get("control") == control
        ):
            rows.append((req, obs or {}))
    passing = [
        (req, obs)
        for req, obs in rows
        if obs.get("verified") is True
        and obs.get("outcome") == "admitted"
        and obs.get("count_basis") in {"input", "prompt_tokens"}
        and obs.get("usage_state") == "valid"
        and obs.get("reported_input") == lower
        and type(obs.get("reported_output")) is int
        and obs["reported_output"] >= 0
        and positive(req.get("input_tokens"))
    ]
    if not passing:
        return {}
    req, obs = max(passing, key=lambda pair: pair[1].get("observed_at", ""))
    rejected = sorted(
        (
            (r, o)
            for r, o in rows
            if o.get("diagnostic") in INPUT_SIZE_GUIDANCE | {"size_refused_without_exact_counts"}
            and positive(r.get("input_tokens"))
            and r["input_tokens"] > req["input_tokens"]
            and positive(r.get("target"))
            and r["target"] > lower
        ),
        key=lambda pair: pair[0]["input_tokens"],
    )
    rejected_rows = []
    seen = set()
    for r, o in rejected:
        if r["input_tokens"] in seen:
            continue
        seen.add(r["input_tokens"])
        # This maps local estimates through one admitted sample, not a provider refusal count.
        estimate = (r["input_tokens"] * lower + req["input_tokens"] - 1) // req["input_tokens"]
        rejected_rows.append(
            {
                "local_input_estimate": r["input_tokens"],
                "provider_scale_estimate": estimate,
                "requested_output": r.get("output_tokens"),
                "observed_at": o.get("observed_at"),
            }
        )
        if len(rejected_rows) == 3:
            break
    return {
        "admitted_input": lower,
        "admitted_local_estimate": req["input_tokens"],
        "requested_output": req.get("output_tokens"),
        "generated_output": obs.get("reported_output"),
        "count_basis": obs.get("count_basis"),
        "control": req.get("control"),
        "identity": identity,
        "parser_version": parser_version,
        "fixture_version": req.get("fixture_version"),
        "observed_at": obs.get("observed_at"),
        "evidence_digest": digest({"request": req, "observation": obs}),
        "successful_run_ids": [],
        "rejected_estimates": rejected_rows,
    }


def _discovery_attempt_diagnostics(attempts):
    """Keep recent bounded response metadata visible with the candidate's discovery result."""
    rows = []
    for item in list(attempts)[-12:]:
        if isinstance(item, Mapping):
            request_data = item.get("request") or {}
            observation_data = item.get("observation") or {}
        else:
            request_data = item.request
            observation_data = item

        def field(data, key):
            return data.get(key) if isinstance(data, Mapping) else getattr(data, key, None)

        rows.append(
            {
                "axis": field(request_data, "axis"),
                "target": field(request_data, "target"),
                "reserved_input": field(request_data, "input_tokens"),
                "requested_output": field(request_data, "output_tokens"),
                "outcome": field(observation_data, "outcome"),
                "diagnostic": field(observation_data, "diagnostic"),
                "response_status": field(observation_data, "response_status"),
                "response_media_type": field(observation_data, "response_media_type"),
                "response_bytes": field(observation_data, "response_bytes"),
                "stream_bytes": field(observation_data, "stream_bytes"),
                "stream_events": field(observation_data, "stream_events"),
                "message_content_state": field(observation_data, "message_content_state"),
                "finish_reason_state": field(observation_data, "finish_reason_state"),
                "finish_reason": field(observation_data, "finish_reason"),
                "usage_state": field(observation_data, "usage_state"),
                "sentinel_verification": field(observation_data, "sentinel_verification"),
                "reported_input": field(observation_data, "reported_input"),
                "reported_output": field(observation_data, "reported_output"),
                "provider_request_id": field(observation_data, "provider_request_id"),
                "response_id": field(observation_data, "response_id"),
                "response_preview": field(observation_data, "response_preview"),
            }
        )
    return rows


def _discover_candidate_caps(
    candidate,
    record,
    *,
    provider_cfg,
    rules,
    pause,
    session,
    today,
    run_id,
    receipt_id,
    history,
    shared_budget,
    sleep,
    selected_axes=None,
):
    """Run the separate, charged size search only after candidate reachability is proven."""
    import secrets
    import tempfile
    from pathlib import Path

    from citypods.provider_catalog.discovery import (
        INPUT_OUTPUT_COUNTERPART,
        DiscoveryBudget,
        DiscoveryJournal,
        discovery_identity,
        run_discovery,
    )
    from citypods.provider_catalog.evidence import digest
    from citypods.provider_catalog.probe import (
        account_key,
        build_discovery_request,
        chat_url,
        measure_discovery,
    )
    from citypods.provider_catalog.rules import general_discovery_rules

    rules = general_discovery_rules(rules)
    accounts = provider_cfg.get("accounts") or []
    if not accounts or not isinstance(accounts[0], Mapping):
        raise ValueError("candidate has no verified provider account")
    account_id = str(accounts[0].get("id") or "")
    if not account_id or not account_key(provider_cfg, account_id):
        raise ValueError("candidate provider account is unavailable")
    endpoint = chat_url(rules, provider_cfg)
    identity = discovery_identity(candidate.provider, account_id, candidate.model, endpoint)
    # Count the completed reachability call and each structural-output call against the
    # per-candidate submission ceiling; their prompt sizes are not request-cap reservations.
    evidence = candidate.evidence or {}
    reachability_calls = 1 + len(evidence.get("methods") or ())
    session_id = digest(
        [run_id, receipt_id, candidate.key, identity, today.isoformat(), secrets.token_hex(16)]
    )
    metadata = {
        "session_id": session_id,
        "identity": identity,
        "provider": candidate.provider,
        "account_id": account_id,
        "upstream_model": candidate.model,
        "endpoint": endpoint,
        "catalog_digest": digest(record),
        "free_evidence_digest": digest([candidate.provider, candidate.model, record]),
        "parser_version": rules.discovery_parser_version,
        "fixture_version": "discovery-v1",
        "created_at": datetime.now(UTC).isoformat(),
        "sources": [
            rules.catalog.url
            or f"{provider_cfg.get('api_base', '').rstrip('/')}/{rules.catalog.path.lstrip('/')}"
        ],
        "receipt_id": receipt_id,
        "reachability_calls": reachability_calls,
    }
    route = {
        "provider": candidate.provider,
        "account_id": account_id,
        "upstream_model": candidate.model,
        "route_id": candidate.key,
    }
    litellm = _litellm_discovery_limits(candidate.provider, candidate.model)
    seeds = _discovery_starting_points(record, litellm)
    counterparts = {
        axis: min(INPUT_OUTPUT_COUNTERPART, seeds["output"])
        for axis in ("input", "total_input_heavy")
        if "output" in seeds
    }
    nonce = secrets.token_hex(32)
    # Token reservations below describe the size-search requests, which carry the
    # explicit measured token sizes.
    budget = DiscoveryBudget(calls=reachability_calls)
    if shared_budget is not None:
        shared_budget.calls += reachability_calls
    # Reachability/structured-output checks just completed; space the first size probe too.
    last_submission = time.monotonic()
    try:
        provider_rpm = float(provider_cfg.get("rpm") or 0)
    except (TypeError, ValueError):
        provider_rpm = 0
    interval = max(
        rules.canary_interval_seconds,
        60 / provider_rpm if provider_rpm > 0 else 0,
    )

    def measure(request, before_call, timeout):
        def paced_admission():
            nonlocal last_submission
            if last_submission is not None and interval:
                pause.renew()
                sleep(max(0, interval - (time.monotonic() - last_submission)))
            receipt = before_call()
            last_submission = time.monotonic()
            return receipt

        transient = build_discovery_request(request, route, provider_cfg, rules, nonce=nonce)
        return measure_discovery(
            request,
            transient,
            rules,
            session=session,
            before_call=paced_admission,
            clock=time.monotonic,
            timeout=timeout,
        )

    with tempfile.TemporaryDirectory(prefix="citypods-discovery-") as folder:
        journal = DiscoveryJournal(Path(folder) / "checkpoint.json", metadata)
        try:
            result = run_discovery(
                session_id=session_id,
                identity=identity,
                parser_version=rules.discovery_parser_version,
                supported_axes=rules.discovery_axes,
                selected_axes=selected_axes,
                input_priority=selected_axes is None,
                seeds=seeds,
                counterparts=counterparts,
                output_min_tokens=rules.discovery_output_min_tokens(endpoint, candidate.model),
                measure=measure,
                renew=pause.renew,
                journal=journal,
                history=history,
                budget=budget,
                shared_budget=shared_budget,
                sleep=sleep,
            )
        except Exception as exc:
            # Preserve charged attempts even if orchestration fails after admission.
            reason = f"discovery_stopped_safely:{type(exc).__name__}"
            journal.data["reason"] = reason
            journal.save()
            result = None
        axes = []
        claims = _discovery_claims(record)
        for bracket in result.brackets if result is not None else ():
            axes.append(
                {
                    **bracket,
                    "axis": bracket["axis"],
                    "published": claims.get(bracket["axis"]),
                    "source": metadata["sources"][0],
                }
            )
        summary = {
            "retryable": bool(journal.data["attempts"])
            and (
                result is None
                or any(
                    row["axis"] in (selected_axes or ("input",)) and row["status"] != "converged"
                    for row in axes
                )
            ),
            "profile": "input_priority" if selected_axes is None else "selected_axes",
            "reason": result.reason if result is not None else journal.data["reason"],
            "detail": (
                "Input is primary; output and combined examples are supporting evidence. "
                "Estimated rejected sizes indicate an approximate boundary, not a measured upper."
                if selected_axes is None
                else "Isolated selected-axis search; verified comparable counts establish bounds."
            ),
            "reachability": "verified",
            "calls": reachability_calls + len(journal.data["attempts"]),
            "input_tokens": sum(row["request"]["input_tokens"] for row in journal.data["attempts"]),
            "output_tokens": sum(
                row["request"]["output_tokens"] for row in journal.data["attempts"]
            ),
            "axes": axes,
            "input_characterization": _discovery_input_characterization(
                axes,
                list(history) + list(journal.data["attempts"]),
                identity=identity,
                parser_version=rules.discovery_parser_version,
            ),
            "attempt_diagnostics": _discovery_attempt_diagnostics(journal.data["attempts"]),
            "litellm": litellm,
        }
        if result is not None and result.transport_retries:
            summary["transport_retries"] = [
                [dict(attempt) for attempt in group] for group in result.transport_retries
            ]
        return {
            "summary": summary,
            "artifact": {
                "session": metadata,
                "attempts": journal.data["attempts"],
                "reason": result.reason if result is not None else journal.data["reason"],
            },
        }


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


def confirm_retirement_candidates(
    report: Report,
    limits: Mapping[str, Any],
    *,
    session: requests.Session,
    control: DispatchControl,
    today: date,
    canary_fn: CanaryFn = canary,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Make an independent exact-account request before a removal can be recommended.

    The normal reconciliation canary is the initial signal. A second request is made only for a
    complete same-day catalog absence plus a definitive retired/not-served result, while that
    provider is paused. The second result must repeat the same verdict; any other outcome leaves
    the anomaly unconfirmed so retirement planning fails closed.
    """
    routes = [r for r in limits.get("routes") or [] if isinstance(r, dict)]
    by_id = {str(route.get("route_id")): route for route in routes}
    rules_by_provider = all_rules()
    providers = limits.get("providers") or {}
    for anomaly in report.anomalies:
        if anomaly.verdict not in RETIREMENT_VERDICTS or not anomaly.absent_from_catalog:
            continue
        catalog = (report.state.get("catalogs") or {}).get(anomaly.provider) or {}
        records = catalog.get("models")
        if (
            catalog.get("provider") != anomaly.provider
            or catalog.get("observed_at") != today.isoformat()
            or catalog.get("complete") is not True
            or not isinstance(records, (list, tuple))
            or any(not isinstance(record, (list, tuple)) or len(record) != 6 for record in records)
            or any(record[0] == anomaly.model for record in records)
        ):
            report.observations.append(
                f"{anomaly.route_id}: fresh removal confirmation skipped; "
                "current complete catalog absence is not verified"
            )
            continue
        route = by_id.get(anomaly.route_id)
        if (
            not route
            or route.get("provider") != anomaly.provider
            or route.get("upstream_model") != anomaly.model
            or anomaly.config_digest != digest(route)
        ):
            report.observations.append(
                f"{anomaly.route_id}: fresh removal confirmation skipped; "
                "route identity or digest does not match the current config"
            )
            continue
        rules = rules_by_provider.get(anomaly.provider)
        provider_cfg = providers.get(anomaly.provider)
        if not rules or not provider_cfg:
            report.observations.append(
                f"{anomaly.route_id}: fresh removal confirmation skipped; "
                "provider configuration is unavailable"
            )
            continue
        api_key = account_key(provider_cfg, route.get("account_id"))
        if not api_key:
            report.observations.append(
                f"{anomaly.route_id}: fresh removal confirmation skipped; "
                "the exact route account has no configured credential"
            )
            continue
        rid = anomaly.route_id
        try:
            scarce = route.get("rpd") is not None and 0 < float(route["rpd"]) <= SCARCE_RPD
        except (TypeError, ValueError):
            scarce = True
        confirmation_data: dict[str, Any] | None = None
        with control.paused(anomaly.provider) as pause:
            if pause.contended:
                report.observations.append(
                    f"{rid}: fresh removal confirmation deferred; provider dispatch pause contended"
                )
                continue
            quota = control.route_quota(anomaly.provider).get(rid) or {}
            remaining = quota.get("rpd_remaining")
            if scarce and (remaining is None or remaining <= 0):
                report.observations.append(
                    f"{rid}: fresh removal confirmation deferred; "
                    "daily quota is unavailable or spent"
                )
                continue
            if rules.canary_interval_seconds:
                sleep(rules.canary_interval_seconds)
            pause.renew()
            control.reserve(rid)
            response = canary_fn(rules, provider_cfg, anomaly.model, api_key, session)
            confirmation = classify(response, rules)
            checked_at = datetime.now(UTC).isoformat()
            if confirmation.verdict != anomaly.verdict:
                report.observations.append(
                    f"{rid}: fresh removal confirmation did not repeat {anomaly.verdict}; "
                    f"observed {confirmation.verdict} at {checked_at}; removal deferred"
                )
                continue
            confirmation_data = {
                "provider": anomaly.provider,
                "account_id": route.get("account_id"),
                "route_id": rid,
                "model": anomaly.model,
                "config_digest": anomaly.config_digest,
                "verdict": confirmation.verdict,
                "status": response.status,
                "checked_at": checked_at,
            }
        # paused() records expiry in its context-manager exit path, so the final contention
        # decision must happen after leaving the block. A result from an expired pause is not
        # independent of production dispatch and cannot support a removal recommendation.
        if pause.contended:
            report.observations.append(
                f"{rid}: fresh removal confirmation deferred; pause expired before completion"
            )
            continue
        assert confirmation_data is not None
        confirmation_data["contended"] = False
        anomaly.retirement_confirmation = confirmation_data
        report.observations.append(
            f"{rid}: independent removal confirmation repeated {confirmation.verdict} "
            f"at {checked_at} for account {route.get('account_id')}"
        )


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
        _context_count,
        advance_context_state,
        context_history_states,
        context_input_ratio,
        context_rate_wait_ms,
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
        scope = f"{route['provider']}:{route['account_id']}:{route['upstream_model']}"
        if not readiness.get("enabled") or readiness.get("quota_scope") != scope:
            report.observations.append(f"{rid}: context deferred (disabled or unknown quota scope)")
            continue
        key = None
        for dimension in context_run.get("dimensions", ("input", "output")):
            observed = None
            rate_retried_targets = set()
            for _ in range(8 if context_run.get("manual") else 6):
                if context_run.get("abandoned"):
                    return
                budget = context_run["budget"]
                budget["route_requests"] = context_run.setdefault("route_counts", {}).get(rid, 0)
                budget["route_limit"] = 12 if context_run.get("manual") else 6
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
                try:
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
                except ValueError as exc:
                    report.observations.append(f"{rid}/{dimension}: context deferred ({exc})")
                    break
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
                try:
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
                except ValueError as exc:
                    report.observations.append(f"{rid}/{dimension}: context deferred ({exc})")
                    break
                if request.reserved_input > context_run.get("per_call_input", 524288) or (
                    request.requested_output > context_run.get("per_call_output", 32768)
                ):
                    report.observations.append(
                        f"{rid}/{dimension}: manual per-call ceiling; deferred"
                    )
                    break
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
                    reserve = (
                        control.client.reserve_manual_context
                        if context_run.get("manual")
                        else control.client.reserve_context
                    )
                    admitted = reserve(
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
                        retain_error_body=bool(context_run.get("manual")),
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
                if (
                    observed.reported_input is not None
                    and observed.reported_input > request.reserved_input
                ) or (
                    observed.reported_output is not None
                    and observed.reported_output > request.requested_output
                ):
                    report.observations.append(
                        f"{rid}/{dimension}: context accounting overshoot; provider input/output "
                        f"{observed.reported_input}/{observed.reported_output}, reserved "
                        f"{request.reserved_input}/{request.requested_output}; route stopped"
                    )
                current = advance_context_state(target, observed)
                states[key] = current
                context_run.setdefault("rotation", {})[rid] = observed.observed_at
                if current.success:
                    lower = _context_count(current.success)
                    upper = current.rejection.reported_ceiling if current.rejection else "unbounded"
                    report.observations.append(
                        f"{rid}/{dimension}: context {current.status}; provider-token interval "
                        f"[{lower}, {upper}], basis {current.success.count_basis}; "
                        f"observed {current.success.observed_at} (not an exact maximum)"
                    )
                if observed.rate_limit_requested_tokens is not None:
                    rate_probe_steered = current.rate_rejection == observed
                    report.observations.append(
                        f"{rid}/{dimension}: provider TPM rejection at "
                        f"{observed.rate_limit_requested_tokens} provider-counted input tokens; "
                        "recorded only as a probe-search hint"
                    )
                    if (
                        context_run.get("manual")
                        and rate_probe_steered
                        and current.status == "rate_refining"
                        and budget["remaining_requests"] > 0
                    ):
                        wait_ms = context_rate_wait_ms(observed)
                        budget["remaining_seconds"] = min(
                            budget["remaining_seconds"],
                            (context_run["deadline_ms"] - time.time() * 1000) / 1000,
                        )
                        if budget["remaining_seconds"] >= wait_ms / 1000 + 205:
                            sleep(wait_ms / 1000)
                            continue
                elif observed.outcome == "quota" and context_run.get("manual"):
                    # Untyped rate errors get one same-size retry after cooldown. This can
                    # confirm transient recovery, but never creates a numeric size bound.
                    retry_target = target.next_target
                    wait_ms = context_rate_wait_ms(observed)
                    if (
                        observed.provider_error_class != "quota"
                        and retry_target not in rate_retried_targets
                        and budget["remaining_requests"] > 0
                    ):
                        budget["remaining_seconds"] = min(
                            budget["remaining_seconds"],
                            (context_run["deadline_ms"] - time.time() * 1000) / 1000,
                        )
                        if budget["remaining_seconds"] >= wait_ms / 1000 + 205:
                            rate_retried_targets.add(retry_target)
                            states[key] = replace(
                                current,
                                next_target=retry_target,
                                status="retrying",
                            )
                            sleep(wait_ms / 1000)
                            continue
                    if current.status == "retrying":
                        current = replace(
                            current,
                            next_target=None,
                            status="exploring" if current.success else "baseline",
                        )
                        states[key] = current
                    report.observations.append(
                        f"{rid}/{dimension}: rate limit deferred; no typed size bound"
                    )
                if observed.outcome != "verified" or current.status in {
                    "uncertain",
                    "converged",
                    "rate_converged",
                }:
                    if current.status != "rate_converged":
                        # Manual searches may downshift after a typed provider token-rate
                        # rejection, but never replay an admission or exceed the run budget.
                        if not (
                            context_run.get("manual")
                            and current.rate_rejection == observed
                            and observed.rate_limit_requested_tokens is not None
                            and current.status == "rate_refining"
                            and budget["remaining_requests"] > 0
                        ):
                            break
                        # The typed rejection branch above waits for the Worker cooldown. If
                        # insufficient deadline remains, retain the next target for a later run.
                        if (
                            budget["remaining_seconds"]
                            < max(60_000, observed.retry_after_ms or 0) / 1000 + 205
                        ):
                            break
                    else:
                        break
            if dimension == "input" and (
                key is None
                or states.get(key) is None
                or states[key].success is None
                or states[key].status == "uncertain"
                or (
                    observed is not None
                    and observed.outcome != "verified"
                    and states[key].status != "rate_converged"
                )
            ):
                break
    report.context_states = list(states.values())
