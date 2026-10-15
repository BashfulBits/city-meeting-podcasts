from __future__ import annotations

import pytest

from citypods.remedy_p3_publication import validated_p3_plan
from citypods.remedy_p3_runner import P3_ROUTES
from citypods.remedy_policy import canonical_hash


@pytest.mark.parametrize(
    ("decision", "candidate", "message"),
    [
        ({"disposition": "issue_candidate"}, {}, "not a proposal candidate"),
        (
            {"disposition": "pr_candidate", "validator_passed": False},
            {"assignment": {}},
            "did not pass deterministic validation",
        ),
        (
            {"disposition": "pr_candidate", "validator_passed": True},
            {},
            "no complete selected assignment",
        ),
    ],
)
def test_p3_publication_rejects_non_consensus_reports(decision, candidate, message, tmp_path):
    evidence = {"unexpected_findings": []}
    decision.setdefault("evidence_hash", canonical_hash(evidence))
    with pytest.raises(ValueError, match=message):
        validated_p3_plan(
            {"decision": decision, "proposal_candidate": candidate},
            evidence,
            repo_root=tmp_path,
            admitted_routes=set(P3_ROUTES.values()),
        )


def test_p3_publication_binds_report_to_exact_evidence(tmp_path):
    evidence = {"unexpected_findings": [{}]}
    report = {
        "decision": {
            "disposition": "pr_candidate",
            "validator_passed": True,
            "evidence_hash": "0" * 64,
        },
        "proposal_candidate": {"assignment": {}},
    }

    with pytest.raises(ValueError, match="does not bind the publication evidence"):
        validated_p3_plan(
            report,
            evidence,
            repo_root=tmp_path,
            admitted_routes=set(P3_ROUTES.values()),
        )
