"""Fail-closed additions/ignore planning; issue data never supplies a config write directly."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
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
    # Keep later decision types visible, but reject them until their slice is implemented.
    if any(not any(x.startswith(f"`{c.key}`: ") for c in snapshot.candidates) for x in checked):
        raise ValueError("paid-route/shadow/limit decisions require Slice 2b")
    if not result:
        raise ValueError("select at least one additions/ignore decision")
    return tuple(result)


def plan_apply(report: Report, decisions: tuple[Decision, ...], config: ApplyConfig) -> EditPlan:
    candidates = {c.key: c for c in report.candidates}
    routes, backups, ignored, applied, deferred, rejected = [], [], [], [], [], []
    existing = list(config.limits.get("routes") or [])
    providers = config.limits.get("providers") or {}
    for decision in decisions:
        provider = providers.get(decision.provider)
        if not provider or not any(a.get("id") for a in provider.get("accounts") or []):
            rejected.append(f"{decision.key}: provider/account no longer configured")
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
        if not physical:
            routes.append(tuple(route.items()))
        backups.extend((lane, model_key) for lane in decision.lanes)
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
