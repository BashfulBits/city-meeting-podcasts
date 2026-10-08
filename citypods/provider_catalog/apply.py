"""Fail-closed additions/ignore planning; issue data never supplies a config write directly."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from fnmatch import fnmatchcase
from glob import escape
from typing import Any

from citypods.compute.llm_lanes import LaneConfig
from citypods.provider_catalog.evidence import RouteEvidence, candidate_digest, digest
from citypods.provider_catalog.issue import decision_choices
from citypods.provider_catalog.reconcile import Report
from citypods.review_issues import DECISION_BLOCK_END, DECISION_BLOCK_START, checked_decisions

BRANCH = "automation/provider-catalog-additions"
PR_MARKER = "<!-- citypods:provider-catalog-additions -->"
SOURCE_PATHS = (
    "config/provider_limits.yml",
    "config/site_config.yml",
    "config/provider_catalog_decisions.yml",
)
COMPILED_PATHS = (
    "citypods/compute/llm_routes.json",
    "workers/llm-dispatch-v2/src/dispatch_limits.json",
    "workers/llm-dispatch-v2/src/ingress_reservations.json",
)


@dataclass(frozen=True)
class Decision:
    provider: str
    model: str
    action: str
    lanes: tuple[str, ...] = ()
    evidence: RouteEvidence | None = None
    candidate_digest: str | None = None
    offered_lanes: tuple[str, ...] = ()
    route_id: str | None = None
    route_digest: str | None = None

    @property
    def key(self):
        return f"{self.provider}/{self.model}"


@dataclass(frozen=True)
class EditPlan:
    base_commit: str
    config_hashes: tuple[tuple[str, str], ...]
    routes: tuple[tuple[tuple[str, Any], ...], ...] = ()
    backups: tuple[tuple[str, str], ...] = ()
    ignored: tuple[tuple[str, str, str], ...] = ()
    applied: tuple[str, ...] = ()
    deferred: tuple[str, ...] = ()
    rejected: tuple[str, ...] = ()
    paid_routes: tuple[str, ...] = ()
    acknowledged: tuple[tuple[str, str, str], ...] = ()


@dataclass(frozen=True)
class ApplyConfig:
    limits: Mapping[str, Any]
    lanes: Mapping[str, LaneConfig]
    texts: Mapping[str, str]
    base_commit: str
    today: date


def parse_decisions(body: str, snapshot: Report) -> tuple[Decision, ...]:
    """Read exact publisher choices; reject forged or duplicate checked rows."""
    if body.count(DECISION_BLOCK_START) != 1 or body.count(DECISION_BLOCK_END) != 1:
        raise ValueError("expected one complete decision block")
    start = body.index(DECISION_BLOCK_START) + len(DECISION_BLOCK_START)
    end = body.index(DECISION_BLOCK_END)
    if end < start:
        raise ValueError("invalid decision block")
    rows = re.findall(r"(?mi)^- \[x\] (.*?)\s*$", body[start:end])
    choices = decision_choices(snapshot)
    checked = checked_decisions(body, choices)
    if len(rows) != len(set(rows)) or set(rows) != set(checked):
        raise ValueError("unknown or duplicate checked decision")
    result = []
    for candidate in snapshot.candidates:
        prefix = f"`{candidate.key}`: "
        selected = [x.removeprefix(prefix) for x in checked if x.startswith(prefix)]
        if not selected:
            continue
        ignore = "ignore" in selected
        if ignore and len(selected) != 1:
            raise ValueError(f"{candidate.key}: ignore conflicts with addition")
        if "add route only" in selected and len(selected) != 1:
            raise ValueError(f"{candidate.key}: route-only conflicts with lane placement")
        evidence = RouteEvidence.from_dict(candidate.evidence) if candidate.evidence else None
        if evidence and candidate.evidence_digest != candidate_digest(evidence, candidate.lanes):
            raise ValueError(f"{candidate.key}: invalid candidate evidence digest; refresh issue")
        lanes = tuple(
            x.removeprefix("add as backup to `").removesuffix("`")
            for x in selected
            if x.startswith("add as backup to `")
        )
        result.append(
            Decision(
                candidate.provider,
                candidate.model,
                "ignore" if ignore else "add",
                lanes,
                evidence,
                candidate.evidence_digest,
                candidate.lanes,
            )
        )
    for anomaly in snapshot.anomalies:
        if anomaly.verdict != "not_entitled":
            continue
        selected = [x for x in checked if x.startswith(f"`{anomaly.route_id}`: ")]
        if len(selected) > 1:
            raise ValueError(f"{anomaly.route_id}: conflicting paid-route decisions")
        if selected:
            result.append(
                Decision(
                    anomaly.provider,
                    anomaly.model,
                    "keep_paid" if selected[0].endswith("keep as a paid route") else "remove",
                    route_id=anomaly.route_id,
                    route_digest=anomaly.config_digest,
                )
            )
    if not result:
        raise ValueError("select at least one supported decision")
    return tuple(result)


def paid_fulfilled(decision: Decision, config: ApplyConfig) -> bool:
    from citypods.provider_catalog.config_edit import load_config

    route = next(
        (r for r in config.limits.get("routes") or [] if r.get("route_id") == decision.route_id),
        None,
    )
    entries = load_config(config.texts[SOURCE_PATHS[2]]).get("acknowledged") or []
    return bool(
        route
        and route.get("provider") == decision.provider
        and route.get("upstream_model") == decision.model
        and route.get("free") is False
        and any(
            a.get("provider") == decision.provider
            and fnmatchcase(decision.model, str(a.get("model_glob") or ""))
            and a.get("verdict") == "not_entitled"
            for a in entries
        )
    )


def plan_apply(report: Report, decisions: tuple[Decision, ...], config: ApplyConfig) -> EditPlan:
    candidates = {c.key: c for c in report.candidates}
    routes, backups, ignored, applied, deferred, rejected = [], [], [], [], [], []
    existing = list(config.limits.get("routes") or [])
    paid_routes, acknowledged = [], []
    anomalies = {a.route_id: a for a in report.anomalies}
    providers = config.limits.get("providers") or {}
    for decision in decisions:
        provider = providers.get(decision.provider)
        if not provider or not any(a.get("id") for a in provider.get("accounts") or []):
            rejected.append(f"{decision.key}: provider/account no longer configured")
            continue
        if decision.action == "remove":
            deferred.append(f"{decision.route_id}: removal requires deployed Slice 3 rescue")
            continue
        if decision.action == "keep_paid":
            matches = [r for r in existing if r.get("route_id") == decision.route_id]
            if len(matches) != 1:
                rejected.append(f"{decision.route_id}: route missing or ambiguous")
                continue
            route = matches[0]
            if (
                route.get("provider") != decision.provider
                or route.get("upstream_model") != decision.model
            ):
                deferred.append(f"{decision.route_id}: route identity changed; refresh the issue")
                continue
            if not isinstance(route.get("free"), bool):
                rejected.append(f"{decision.route_id}: explicit boolean free field required")
                continue
            model_glob = escape(decision.model)
            if paid_fulfilled(decision, config):
                applied.append(f"{decision.route_id}: keep paid already fulfilled")
                continue
            proof = anomalies.get(decision.route_id)
            if (
                not proof
                or proof.verdict != "not_entitled"
                or proof.contended
                or proof.observed_on != config.today.isoformat()
                or proof.config_digest != digest(route)
                or decision.route_digest != digest(route)
                or proof.provider != decision.provider
                or proof.model != decision.model
                or route.get("rpd") == 0
            ):
                deferred.append(
                    f"{decision.route_id}: fresh paused unchanged not_entitled proof required"
                )
                continue
            if any(
                r.get("provider") == decision.provider
                and r.get("upstream_model") == decision.model
                and r.get("route_id") != decision.route_id
                and r.get("free") is True
                for r in existing
            ):
                deferred.append(
                    f"{decision.route_id}: acknowledgement covers other free routes; "
                    "review manually"
                )
                continue
            # Evaluate the complete proposed paid batch, preserving a free primary pool.
            proposed = set(paid_routes) | {decision.route_id}
            losing = []
            for purpose, lane in config.lanes.items():

                def serves(r, lane=lane):
                    return set(lane.models) & {
                        str(r.get("model") or ""),
                        str(r.get("model_key") or ""),
                        *map(str, r.get("also_serves") or []),
                    }

                before = [
                    r for r in existing if r.get("free") is True and r.get("rpd") != 0 and serves(r)
                ]
                if not any(r.get("route_id") == decision.route_id for r in before):
                    continue
                if not any(r.get("route_id") not in proposed for r in before):
                    losing.append(purpose)
            if losing:
                rejected.append(
                    f"{decision.route_id}: would empty free lane pool: {', '.join(losing)}"
                )
                continue
            impacted = [
                purpose
                for purpose, lane in config.lanes.items()
                if set((*lane.models, *lane.backup_models))
                & {
                    str(route.get("model") or ""),
                    str(route.get("model_key") or ""),
                    *map(str, route.get("also_serves") or []),
                }
            ]
            paid_routes.append(decision.route_id)
            acknowledged.append((decision.provider, model_glob, config.today.isoformat()))
            applied.append(
                f"{decision.route_id}: keep paid; loses free eligibility in "
                f"{', '.join(impacted) or 'no lanes'}"
            )
            continue
        if decision.action == "ignore":
            ignored.append((decision.provider, decision.model, config.today.isoformat()))
            applied.append(f"{decision.key}: ignore")
            continue
        if decision.action != "add":
            rejected.append(f"{decision.key}: unsupported decision")
            continue
        # Already present in main: this choice is fulfilled; never duplicate a physical route.
        physical = [
            r
            for r in existing
            if r.get("provider") == decision.provider and r.get("upstream_model") == decision.model
        ]
        current = candidates.get(decision.key)
        if physical:
            if not any(r.get("free") is True and r.get("rpd") != 0 for r in physical):
                deferred.append(f"{decision.key}: configured route is paused or not free")
                continue
            keys = {str(r.get("model_key") or r["model"]) for r in physical}
            if len(keys) != 1:
                rejected.append(f"{decision.key}: ambiguous configured identity")
                continue
            model_key = next(iter(keys))
        else:
            proof = (
                RouteEvidence.from_dict(current.evidence) if current and current.evidence else None
            )
            old = decision.evidence
            if not proof or not old:
                deferred.append(f"{decision.key}: fresh evidence unavailable; refresh the issue")
                continue
            if decision.offered_lanes and tuple(current.lanes) != decision.offered_lanes:
                deferred.append(f"{decision.key}: offered lanes changed; choose again")
                continue
            comparison = (
                "provider",
                "upstream_model",
                "account_id",
                "catalog_digest",
                "free",
                "input_context_limit",
                "output_context_limit",
                "model_key",
            )
            if any(getattr(proof, k) != getattr(old, k) for k in comparison):
                deferred.append(f"{decision.key}: candidate changed; refresh and choose again")
                continue
            if (
                not proof.catalog_complete
                or not proof.free
                or proof.contended
                or proof.verdict != "proven"
                or proof.observed_at != config.today.isoformat()
                or proof.structured_output_verified_on != config.today.isoformat()
                or proof.structured_output_method
                not in config.limits.get("structured_output_methods", {})
            ):
                deferred.append(f"{decision.key}: complete paused fresh JSON proof required")
                continue
            if not proof.model_key:
                deferred.append(f"{decision.key}: identity_required; review a manual mapping")
                continue
            if not proof.input_context_limit or not proof.output_context_limit:
                deferred.append(f"{decision.key}: compiler requires both evidenced context bounds")
                continue
            model_key = proof.model_key
            account = proof.account_id
            if account not in {a.get("id") for a in provider["accounts"]}:
                rejected.append(f"{decision.key}: account changed")
                continue
            rid = re.sub(r"[^a-zA-Z0-9_]+", "_", f"{decision.provider}_{decision.model}").strip("_")
            if any(r.get("route_id") == rid for r in existing + [dict(r) for r in routes]):
                rejected.append(f"{decision.key}: route ID collision")
                continue
            route = {
                "route_id": rid,
                "model": model_key,
                "provider": decision.provider,
                "upstream_model": decision.model,
                "account_id": account,
                "input_context_limit": proof.input_context_limit,
                "structured_output_method": proof.structured_output_method,
                "structured_output_verified_on": proof.structured_output_verified_on,
                "rpm": 1,
                "concurrency": 1,
                "free": True,
            }
            # Only route-scoped positive observations can supply independent route limits.
            for observation in proof.limits:
                if observation.scope == "route" and observation.metric in {"rpm", "tpm", "rpd"}:
                    route[observation.metric] = observation.value
            if proof.output_context_limit:
                route["output_context_limit"] = proof.output_context_limit
        bad_lanes = [
            lane
            for lane in decision.lanes
            if lane not in config.lanes
            or not config.lanes[lane].accepts_catalog_backups
            or (current and lane not in current.lanes)
        ]
        if bad_lanes:
            rejected.append(f"{decision.key}: no longer eligible for {', '.join(bad_lanes)}")
            continue
        missing_backups = tuple(
            (lane, model_key)
            for lane in decision.lanes
            if model_key not in config.lanes[lane].backup_models
        )
        if physical and missing_backups:
            # Once another change adds the route, an old candidate checkbox is not fresh lane
            # admission evidence. It could now refer to a task plugin or changed eligibility.
            deferred.append(f"{decision.key}: route now configured; review missing lane placement")
            continue
        if not physical:
            routes.append(tuple(route.items()))
        backups.extend(missing_backups)
        applied.append(f"{decision.key}: add ({', '.join(decision.lanes) or 'route only'})")
    return EditPlan(
        config.base_commit,
        tuple((p, digest(config.texts[p])) for p in SOURCE_PATHS),
        tuple(routes),
        tuple(backups),
        tuple(ignored),
        tuple(applied),
        tuple(deferred),
        tuple(rejected),
        tuple(paid_routes),
        tuple(acknowledged),
    )


def proposal_body(plan: EditPlan) -> str:
    lines = [PR_MARKER, "Provider catalog selections reverified against current main.", ""]
    for label, values in (
        ("Selected", plan.applied),
        ("Deferred (still ticked)", plan.deferred),
        ("Rejected", plan.rejected),
    ):
        if values:
            lines += [f"{label}:", *[f"- {value}" for value in values], ""]
    lines += [
        f"Base: `{plan.base_commit}`.",
        "Validated with both compilers and catalog/lane/limit tests in the workflow.",
        "No primary changes, automatic merge/deployment, pipeline bump or catalog backfill.",
    ]
    return "\n".join(lines) + "\n"
