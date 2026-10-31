from __future__ import annotations

import pytest

from citypods.remedy_p3_publication import validated_p3_plan
from citypods.remedy_p3_runner import P3_MODELS
from citypods.remedy_policy import canonical_hash


def _m(route_id):
    """The logical model a route serves (P3 votes and validators are keyed by model)."""
    from citypods.audit_remedy import P3_ROLE_PLANS

    return P3_ROLE_PLANS["legacy-sol-v1"].model_for_route(route_id)


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
            admitted_models=set(P3_MODELS.values()),
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
            admitted_models=set(P3_MODELS.values()),
        )


def test_human_plan_agreement_revalidates_for_publication_and_rejects_wrong_plan(tmp_path):
    import yaml

    from citypods.audit_remedy import P3_ROLE_PLANS, BodyDecision, P3Vote
    from citypods.audit_remedy_report import P3ReportInput, build_p3_report

    plan = P3_ROLE_PLANS["flash-deepseek-human-v3"]
    feed = "test-city-library-board"
    folder = tmp_path / "config/feeds"
    folder.mkdir(parents=True)
    (folder / (feed + ".yml")).write_text(
        yaml.safe_dump({"slug": feed, "source": {"body": "Library Board"}})
    )
    evidence = {
        "source_key": "test-city",
        "existing_feeds": [{"slug": feed, "body": "Library Board"}],
        "unexpected_findings": [
            {
                "unexpected_body": "Library Board Meeting",
                "episodes": [],
                "count": 3,
            }
        ],
    }
    eh, ph = canonical_hash(evidence), "b" * 64
    votes = [
        P3Vote(
            route_id=plan.reference_routes[role],
            upstream_model="observed-model",
            reasoning_level=None,
            evidence_hash=eh,
            policy_hash=ph,
            response_hash="a" * 64,
            decision=BodyDecision(
                finding_id="finding-1",
                action="union",
                target_feeds=[feed],
                all_observed_episodes=True,
                rationale="Same library board",
            ),
        )
        for role in plan.initial
    ]
    record = P3ReportInput(
        schema_version=1,
        role_plan=plan.name,
        finding_id="finding-1",
        evidence_hash=eh,
        policy_hash=ph,
        evidence_summary="Library meetings",
        feeds=[{"slug": feed, "title": feed}],
        initial_votes=votes,
        initial_validators={_m(v.route_id): True for v in votes},
    )
    report = build_p3_report(record, admitted_models=set(plan.models.values()))
    assert report["role_plan"] == plan.name
    result, _ = validated_p3_plan(
        report,
        evidence,
        repo_root=tmp_path,
        admitted_models=set(plan.models.values()),
        role_plan=plan.name,
    )
    assert len(result.accepted) == 1
    with pytest.raises(ValueError, match="trusted publication config"):
        validated_p3_plan(
            report,
            evidence,
            repo_root=tmp_path,
            admitted_models=set(plan.models.values()),
            role_plan="legacy-sol-v1",
        )
