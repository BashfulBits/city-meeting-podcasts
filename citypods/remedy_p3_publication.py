"""Translate an admitted P3 consensus report into a validated feed mutation plan.

This module is deliberately provider-free. It reuses the ordinary unexpected-body validator and
mutation code so the P3 publisher cannot create a second, weaker path to feed configuration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from citypods.audit_remedy import (
    P3_ROLE_PLANS,
    BodyDecision,
    BodyDecisions,
    P3ReviewDecision,
    RemedyPlan,
    _compact_evidence,
    _resolve_decisions,
    build_p3_review_decision,
    feed_paths_by_slug,
    validate_proposals,
)
from citypods.audit_remedy_report import _selected_proposal
from citypods.remedy_policy import canonical_hash


def validated_p3_plan(
    report: dict[str, Any],
    evidence: dict[str, Any],
    *,
    repo_root: str | Path,
    admitted_models: set[str] | frozenset[str],
    role_plan: str = "legacy-sol-v1",
) -> tuple[RemedyPlan, dict[str, Path]]:
    """Revalidate one report-ready consensus against the current local feed catalog.

    The report is not trusted to name files or paths. The assignment is resolved against the
    original evidence, then passed through the existing body-remedy validator. Only one accepted
    proposal is returned; any shape drift or validation failure holds publication.
    """
    decision = report.get("decision")
    candidate = report.get("proposal_candidate")
    if not isinstance(decision, dict) or decision.get("disposition") != "pr_candidate":
        raise ValueError("P3 report is not a proposal candidate")
    if decision.get("validator_passed") is not True:
        raise ValueError("P3 consensus did not pass deterministic validation")
    if not isinstance(candidate, dict) or not isinstance(candidate.get("assignment"), dict):
        raise ValueError("P3 report has no complete selected assignment")
    if canonical_hash(evidence) != decision.get("evidence_hash"):
        raise ValueError("P3 report does not bind the publication evidence")
    if report.get("role_plan", "legacy-sol-v1") != role_plan:
        raise ValueError("P3 report role plan does not match the trusted publication config")
    parsed_decision = P3ReviewDecision.model_validate(decision)
    recomputed = build_p3_review_decision(
        finding_id=parsed_decision.finding_id,
        evidence_hash=parsed_decision.evidence_hash,
        policy_hash=parsed_decision.policy_hash,
        initial_votes=parsed_decision.initial_votes,
        initial_validators={
            P3_ROLE_PLANS[role_plan].model_for_route(vote.route_id): True
            for vote in parsed_decision.initial_votes
        },
        adjudication=parsed_decision.adjudication,
        adjudication_validator_passed=True,
        admitted_models=admitted_models,
        role_plan=role_plan,
    )
    if recomputed != parsed_decision:
        raise ValueError("P3 decision does not recompute from its route-bound votes")
    if candidate != _selected_proposal(parsed_decision, role_plan):
        raise ValueError("P3 proposal candidate differs from the agreed route-bound assignment")

    assignment = BodyDecision.model_validate(candidate["assignment"])
    findings = evidence.get("unexpected_findings")
    if not isinstance(findings, list) or len(findings) != 1:
        raise ValueError("P3 publication requires exactly the source finding that was judged")
    if assignment.finding_id != report.get("decision", {}).get("finding_id"):
        raise ValueError("P3 assignment finding identity does not match the report")
    if findings[0].get("unexpected_body") is None:
        raise ValueError("source evidence is missing its unexpected-body label")

    local_assignment = assignment.model_copy(update={"finding_id": "f0"})
    resolved = _resolve_decisions(
        BodyDecisions(proposals=[local_assignment]), evidence, _compact_evidence(evidence)
    )
    if resolved.unresolved or len(resolved.proposals) != 1:
        raise ValueError("P3 assignment could not be resolved against current source evidence")

    paths = feed_paths_by_slug(repo_root)
    plan = validate_proposals(resolved, evidence, paths)
    if len(plan.accepted) != 1 or plan.rejected:
        reasons = [item.reason for item in plan.rejected]
        raise ValueError("P3 assignment failed current feed validation: " + "; ".join(reasons))
    return plan, paths
