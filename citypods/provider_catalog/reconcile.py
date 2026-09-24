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
from datetime import date
from typing import Any, Protocol

import requests

from citypods.compute.llm_lanes import LaneConfig
from citypods.provider_catalog.classify import Classification, classify
from citypods.provider_catalog.decisions import Decisions
from citypods.provider_catalog.probe import account_key, canary, fetch_catalog
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

    @property
    def actionable(self) -> bool:
        return bool(self.candidates or self.anomalies)


def _route_model_keys(route: Mapping[str, Any]) -> set[str]:
    keys = {str(route.get("model") or ""), str(route.get("model_key") or "")}
    keys.update(str(m) for m in route.get("also_serves") or [])
    return keys - {""}


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
    for key in _CONTEXT_KEYS:
        value = record.get(key)
        if isinstance(value, int) and value > 0:
            return value
        if isinstance(value, str) and value.isdigit() and int(value) > 0:
            return int(value)
    return None


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
    canary_fn: CanaryFn = canary,
    sleep: Callable[[float], None] = time.sleep,
) -> Report:
    report = Report(floor=quality.floor)
    canary_memory: dict[str, dict[str, str]] = dict(previous_state.get("canaries") or {})
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
    selected = sorted(providers or provider_cfgs)
    for provider in selected:
        rules = all_rules().get(provider)
        cfg = provider_cfgs.get(provider)
        if rules is None or cfg is None:
            report.observations.append(f"{provider}: no plugin or no config; skipped")
            continue
        provider_routes = [r for r in routes if r.get("provider") == provider]
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

        # --- configured-route health: one live route per upstream model -------------------------
        health: list[dict[str, Any]] = []
        seen_models: set[str] = set()
        for route in provider_routes:
            model = str(route.get("upstream_model") or "")
            if route.get("rpd") == 0 or model in seen_models:
                continue
            seen_models.add(model)
            scarce = route.get("rpd") is not None and 0 < float(route["rpd"]) <= SCARCE_RPD
            rid = str(route["route_id"])
            if due_only and rid not in deferred:
                continue
            if scarce and not due_only:
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
                memory = canary_memory.get(f"{provider}/{model}")
                age = _days_since(memory.get("on") if memory else None, today)
                remembered = (
                    memory
                    and age is not None
                    and age < CANARY_MEMORY_DAYS
                    and memory.get("verdict") in DEFINITIVE_VERDICTS
                )
                if remembered:
                    if memory.get("verdict") == "proven":
                        score = quality.score(
                            model, creator=rules.creator, free_suffix=rules.free_suffix
                        )
                        report.candidates.append(
                            _candidate(
                                provider,
                                model,
                                record,
                                score,
                                rules,
                                report.floor,
                                incumbents,
                                memory["on"],
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

        if not health and not candidates:
            continue

        with control.paused(provider) as pause:
            quota = control.route_quota(provider)
            if pause.contended:
                report.observations.append(
                    f"{provider}: probes ran without a drained dispatch pause; capacity signals "
                    "may reflect production traffic"
                )
            first_probe = True
            for item in health:
                pause.renew()
                route = item["route"]
                rid = str(route["route_id"])
                model = str(route["upstream_model"])
                if item["scarce"] and (quota.get(rid) or {}).get("rpd_remaining") == 0:
                    deferred[rid] = str((quota.get(rid) or {}).get("rpd_resets_at", ""))
                    report.observations.append(f"{rid}: daily quota spent; check deferred to reset")
                    continue
                api_key = account_key(cfg, route.get("account_id"))
                if not api_key:
                    report.observations.append(f"{rid}: no API key for its account; not checked")
                    continue
                if not first_probe and rules.canary_interval_seconds:
                    sleep(rules.canary_interval_seconds)
                first_probe = False
                result = classify(canary_fn(rules, cfg, model, api_key, session), rules)
                control.reserve(rid)
                route_checks[rid] = today.isoformat()
                deferred.pop(rid, None)
                absent = (
                    not catalog.error and rules.catalog_is_complete and model not in catalog.models
                )
                _record_health(
                    report,
                    decisions,
                    provider,
                    rid,
                    model,
                    result,
                    absent,
                    known_anomalies,
                    usage=lane_usage(route, lanes, routes),
                )
            api_key = account_key(cfg)
            for model, record, score, _retry in candidates:
                pause.renew()
                if not first_probe and rules.canary_interval_seconds:
                    sleep(rules.canary_interval_seconds)
                first_probe = False
                result = classify(canary_fn(rules, cfg, model, api_key or "", session), rules)
                canary_memory[f"{provider}/{model}"] = {
                    "verdict": result.verdict,
                    "on": today.isoformat(),
                }
                if result.verdict == "proven":
                    report.candidates.append(
                        _candidate(
                            provider,
                            model,
                            record,
                            score,
                            rules,
                            report.floor,
                            incumbents,
                            today.isoformat(),
                        )
                    )
                else:
                    report.observations.append(
                        f"{provider}/{model}: free-marked but {result.verdict} ({result.reason})"
                    )

    # Forget canary memory that has aged out so the state marker stays bounded.
    canary_memory = {
        key: value
        for key, value in canary_memory.items()
        if (_days_since(value.get("on"), today) or 0) < CANARY_MEMORY_DAYS
    }
    if due_only:
        _merge_into_last_full(report, previous_state.get("last_full") or {}, route_checks)
    report.state = {
        "version": 1,
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
    return report


def _merge_into_last_full(
    report: Report, last_full: Mapping[str, Any], route_checks: Mapping[str, str]
) -> None:
    """A due-only run re-checks a few deferred routes; everything else carries over unchanged.

    Anomalies for routes this run re-checked are replaced by this run's result; candidates and
    observations from the last full run are kept, so the issue never loses them.
    """
    rechecked = {a.route_id for a in report.anomalies} | {
        rid for rid in route_checks if any(rid in o for o in report.observations)
    }
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
        provider, route_id, model, result.verdict, result.reason, absent, lane_usage=usage
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
