"""Exercise one synthetic P3 decision without a provider or remote GitHub."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import yaml

from citypods import remedy_feedback as feedback
from citypods import remedy_publication as publication
from citypods import remedy_queue as queue
from citypods.audit_remedy import P3_ROLE_PLANS, BodyDecision, P3Vote
from citypods.audit_remedy_report import P3ReportInput, build_p3_report
from citypods.remedy_ledger import load_decisions
from citypods.remedy_p3_publication import validated_p3_plan
from citypods.remedy_policy import canonical_hash
from scripts.remedy_p3_publish import _binding_for, _pr_candidate, _unit_detail
from tests.test_remedy_ledger import FakeEventStorage, FakeRemedyLease
from tests.test_remedy_publication import FakeGitHub, apply_to
from tests.test_remedy_publication import repo as offline_repo  # noqa: F401
from tests.test_remedy_queue import LIMITS


def test_synthetic_proposal_feedback_and_duplicate_recovery(offline_repo, tmp_path):  # noqa: F811
    """Use a local bare Git remote, fake GitHub and in-memory ledger throughout."""
    repo = offline_repo
    feed = "council"
    # This fixture is private to the local test repo, not a real meeting or city.
    (repo / "config/feeds/council.yml").write_text(
        yaml.safe_dump({"slug": feed, "source": {"body": "City Council"}})
    )
    evidence = {
        "source_key": "source-a",
        "existing_feeds": [{"slug": feed, "body": "City Council"}],
        "unexpected_findings": [
            {
                "unexpected_body": "City Council Meeting",
                "episodes": [{"uid": "synthetic-1", "provider_guid": "synthetic-guid"}],
                "count": 1,
            }
        ],
    }
    role_plan = P3_ROLE_PLANS["flash-deepseek-human-v3"]
    evidence_hash, policy_hash = canonical_hash(evidence), "b" * 64
    votes = [
        P3Vote(
            route_id=role_plan.reference_routes[role],
            upstream_model="synthetic-model",
            reasoning_level=None,
            evidence_hash=evidence_hash,
            policy_hash=policy_hash,
            response_hash="a" * 64,
            decision=BodyDecision(
                finding_id="synthetic-finding",
                action="union",
                target_feeds=[feed],
                all_observed_episodes=True,
                rationale="Synthetic matching council label",
            ),
        )
        for role in role_plan.initial
    ]
    record = P3ReportInput(
        schema_version=1,
        role_plan=role_plan.name,
        finding_id="synthetic-finding",
        evidence_hash=evidence_hash,
        policy_hash=policy_hash,
        evidence_summary="Synthetic council meeting",
        feeds=[{"slug": feed, "title": feed}],
        initial_votes=votes,
        initial_validators={role_plan.model_for_route(v.route_id): True for v in votes},
    )
    report = build_p3_report(record, admitted_models=set(role_plan.models.values()))
    validated, _ = validated_p3_plan(
        report,
        evidence,
        repo_root=repo,
        admitted_models=set(role_plan.models.values()),
        role_plan=role_plan.name,
    )
    finding = evidence["unexpected_findings"][0]
    source = {"city": {"slug": "example-tx"}, "source_key": "source-a"}
    binding = _binding_for(source, finding, report, "c" * 64, [], "2026-10-15T12:00:00+00:00")
    now = datetime(2026, 10, 15, 12, tzinfo=UTC)
    candidate = _pr_candidate(validated, binding, now)
    initial = queue.plan_publications(
        [candidate], {}, queue.GitHubSnapshot(()), limits=LIMITS, now=now
    )
    assert len(initial.prs) == 1
    unit = initial.prs[0]
    gh = FakeGitHub()
    detail = _unit_detail(report, binding, validated, "synthetic-finding", evidence_hash)
    outcome = publication.publish_pr(
        unit,
        [detail],
        git=publication.Git(repo),
        github=gh,
        work_root=tmp_path / "work",
        base="origin/main",
        apply=apply_to("config/feeds/council.yml", "body_any: [meeting]\n"),
        verify=lambda root: (True, "offline synthetic fixture"),
    )
    assert outcome.created and len(gh.created) == 1
    branch, _, body = gh.created[0]
    artifact = queue.Artifact(
        kind="pr",
        number=101,
        url="https://github.com/example/repo/pull/101",
        state="open",
        opened_at=now,
        decision_ids=(candidate.decision_id,),
        head_ref=branch,
        actor="github-actions[bot]",
        body=body,
        head_repo="example/repo",
    )
    repeated = queue.plan_publications(
        [candidate],
        {},
        queue.GitHubSnapshot((artifact,)),
        limits=LIMITS,
        now=now,
    )
    assert not repeated.prs and len(repeated.updates) == 1

    merged_at = "2026-10-16T09:00:00Z"
    payload = {
        "action": "closed",
        "repository": {"full_name": "example/repo"},
        "sender": {"login": "maintainer", "type": "User"},
        "pull_request": {
            "number": 101,
            "html_url": artifact.url,
            "user": {"login": "github-actions[bot]", "type": "Bot"},
            "body": body,
            "merged": True,
            "merged_at": merged_at,
            "closed_at": merged_at,
            "updated_at": merged_at,
            "head": {"repo": {"full_name": "example/repo"}, "ref": branch},
        },
    }
    signal = feedback.signal_from_event("pull_request_target", payload, repository="example/repo")
    settled = replace(
        artifact,
        state="merged",
        settled_by="maintainer",
        settled_at=datetime(2026, 10, 16, 9, tzinfo=UTC),
    )
    recovered = feedback.signal_from_snapshot(settled, repository="example/repo")
    assert recovered is not None and recovered.delivery == signal.delivery
    storage = FakeEventStorage()
    assert feedback.record_signal(storage, recovered, lease=FakeRemedyLease())
    assert not feedback.record_signal(storage, signal, lease=FakeRemedyLease())
    history = load_decisions(storage, source_keys=["source-a"])[candidate.decision_id]
    assert (history.state, history.disposition) == ("covered", "assigned")
