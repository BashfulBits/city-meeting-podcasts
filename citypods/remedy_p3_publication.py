"""Translate an admitted P3 consensus report into a validated feed mutation plan.

This module is deliberately provider-free. It reuses the ordinary unexpected-body validator and
mutation code so the P3 publisher cannot create a second, weaker path to feed configuration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from citypods.audit_remedy import (
    BodyDecision,
    BodyDecisions,
    RemedyPlan,
    _compact_evidence,
    _resolve_decisions,
    feed_paths_by_slug,
    validate_proposals,
)
from citypods.remedy_policy import canonical_hash


def validated_p3_plan(
    report: dict[str, Any], evidence: dict[str, Any], *, repo_root: str | Path
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
    if canonical_hash(evidence) != decision.get("evidence_hash"):
        raise ValueError("P3 report does not bind the publication evidence")
    if not isinstance(candidate, dict) or not isinstance(candidate.get("assignment"), dict):
        raise ValueError("P3 report has no complete selected assignment")

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
