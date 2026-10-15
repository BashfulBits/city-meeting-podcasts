import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from citypods.audit_remedy import P3_INITIAL_ROUTES, BodyDecision, P3Vote
from citypods.audit_remedy_report import P3ReportInput, build_p3_report
from citypods.remedy_actions import COMMAND, _p3_generated_section
from citypods.remedy_ledger import load_decisions
from scripts import remedy_disposition_command, remedy_p3_publish
from tests.test_remedy_actions import inputs
from tests.test_remedy_ledger import FakeEventStorage, FakeRemedyLease


def binding_for(action):
    return {
        "city": action["city"],
        "source_key": action["source_key"],
        "policy_id": action["policy_id"],
        "normalized_label": action["normalized_label"],
        "decision_id": action["decision_id"],
        "evidence_hash": action["evidence_hash"],
        "config_hash": action["config_hash"],
        "policy_hash": action["policy_hash"],
        "recording_refs": action["recording_refs"],
        "expected_parent_event_ids": action["expected_parent_event_ids"],
        "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "feed_slugs": ["test-city-council", "test-city-library-board"],
    }


def test_disposition_workflow_reuses_authenticated_issue_action_and_lease():
    action, context = inputs(state="covered", disposition="assigned")
    action["feed_dispositions"] = [
        {"feed_slug": "test-city-council", "disposition": "include"},
        {"feed_slug": "test-city-library-board", "disposition": "exclude"},
    ]
    binding = binding_for(action)
    issue_body = (
        "<!-- citypods:generated:start -->\n"
        "<!-- citypods:remedy-p3:binding="
        + json.dumps(binding, sort_keys=True, separators=(",", ":"))
        + " -->\n"
        "- [x] Include `test-city-council`\n"
        "- [ ] Exclude `test-city-council`\n"
        "- [ ] Include `test-city-library-board`\n"
        "- [x] Exclude `test-city-library-board`\n"
        "- [ ] No feed assignment\n"
        "- [ ] Hold for future evidence\n"
        "<!-- citypods:generated:end -->\n"
        "\n## Evidence supplied by the issue reporter\n"
        "- [x] Exclude `test-city-council`\n"
    )
    comment = {
        **context["comment"],
        "body": COMMAND + "\n" + json.dumps(action),
        "created_at": datetime(2026, 10, 10, 0, tzinfo=UTC).isoformat(),
    }
    event_payload = {
        "repository": {"full_name": "example/repo"},
        "issue": {
            "number": 1,
            "body": issue_body,
            "user": {"login": "github-actions[bot]"},
        },
        "comment": comment,
    }
    storage = FakeEventStorage()

    class LeaseContext:
        def __enter__(self):
            return FakeRemedyLease()

        def __exit__(self, *_args):
            return False

    key = remedy_disposition_command.apply_comment(
        event_payload,
        {"permission": "write", "user": {"login": "human"}},
        storage=storage,
        lease_factory=LeaseContext,
    )
    history = load_decisions(storage, source_keys=[action["source_key"]])
    decision = history[action["decision_id"]]
    assert key.endswith(".json")
    assert decision.events[0].feed_dispositions[0].disposition == "include"
    assert decision.events[0].feed_dispositions[1].disposition == "exclude"

    event_payload["issue"]["user"]["login"] = "untrusted-user"
    with pytest.raises(ValueError, match=r"must be opened by github-actions\[bot\]"):
        remedy_disposition_command.apply_comment(
            event_payload,
            {"permission": "write", "user": {"login": "human"}},
            storage=storage,
            lease_factory=LeaseContext,
        )


def test_manual_issue_reconciliation_preserves_checked_boxes_and_notes():
    report = {
        "decision": {"finding_id": "finding-1"},
        "manual_issue_candidate": (
            "- [ ] Include `test-city-council`\n- [ ] Exclude `test-city-council`"
        ),
    }
    binding = {
        "decision_id": "a" * 64,
        "evidence_hash": "b" * 64,
        "policy_hash": "c" * 64,
        "policy_id": "p3-unexpected-body-routing-v1",
        "normalized_label": "committee",
        "city": "example-tx",
        "feed_slugs": ["test-city-council"],
        "expected_parent_event_ids": [],
        "source_key": "example:source",
        "recording_refs": [],
        "config_hash": "d" * 64,
        "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    first = remedy_p3_publish._render_issue(report, binding)
    prior = (
        first.replace("- [ ] Include `test-city-council`", "- [x] Include `test-city-council`")
        + "\n\n### Maintainer notes\nKeep this context.\n"
        + "- [x] Exclude `test-city-council`\n"
    )
    newer_binding = {
        **binding,
        "expected_parent_event_ids": ["e" * 64],
        "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    updated = remedy_p3_publish._render_issue(report, newer_binding, prior)
    generated = _p3_generated_section(updated)
    assert "- [x] Include `test-city-council`" in generated
    assert "- [x] Exclude `test-city-council`" not in generated
    assert "Keep this context." in updated
    assert (
        remedy_p3_publish.P3_ISSUE_KEY.format(
            remedy_p3_publish.canonical_hash([binding["decision_id"], binding["evidence_hash"]])
        )
        in updated
    )


def test_binding_uses_source_feeds_if_report_feed_list_is_absent():
    evidence_hash = "a" * 64
    policy_hash = "b" * 64
    routes = sorted(P3_INITIAL_ROUTES)
    votes = [
        P3Vote(
            route_id=route,
            upstream_model=f"test/{route}",
            reasoning_level="high",
            evidence_hash=evidence_hash,
            policy_hash=policy_hash,
            response_hash="c" * 64,
            decision=BodyDecision(
                finding_id="finding-1",
                action="manual_review",
                rationale="The evidence does not establish a safe assignment.",
            ),
        )
        for route in routes
    ]
    report = build_p3_report(
        P3ReportInput(
            schema_version=1,
            finding_id="finding-1",
            evidence_hash=evidence_hash,
            policy_hash=policy_hash,
            evidence_summary="Unresolved ownership evidence.",
            feeds=[{"slug": "source-council", "title": "City Council"}],
            initial_votes=votes,
            initial_validators={route: False for route in routes},
        )
    )
    source = {
        "city": {"slug": "test-city-tx"},
        "source_key": "test-source",
        "existing_feeds": [{"slug": "source-council"}],
    }
    finding = {"unexpected_body": "Unverified Board", "episodes": []}
    report_without_feeds = {key: value for key, value in report.items() if key != "feeds"}

    binding = remedy_p3_publish._binding_for(
        source,
        finding,
        report_without_feeds,
        "d" * 64,
        [],
        "2026-10-15T00:00:00Z",
    )

    assert binding["feed_slugs"] == ["source-council"]


def test_publisher_holds_before_github_or_storage_when_routes_are_not_admitted(tmp_path):
    report = tmp_path / "report.json"
    evidence = tmp_path / "evidence.json"
    report.write_text(json.dumps({"findings": []}), encoding="utf-8")
    evidence.write_text(json.dumps({"sources": []}), encoding="utf-8")
    result = remedy_p3_publish.publish(report, evidence, Path.cwd())
    assert result == {"status": "policy_hold", "published": []}
