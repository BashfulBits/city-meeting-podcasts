"""review/51 P4 live stage: the publisher executes its plan, and only when asked to."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import yaml

from citypods import remedy_feedback as fb
from citypods import remedy_publication as pub
from citypods.remedy_evaluation import RemedyConfig, config_fingerprint
from citypods.remedy_ledger import decision_id
from citypods.remedy_queue import (
    Artifact,
    Candidate,
    GitHubSnapshot,
    branch_name,
    render_binding,
    render_markers,
)
from scripts import remedy_p3_publish as publisher
from tests import test_remedy_publication as base
from tests.test_remedy_feedback import (  # noqa: F401
    DECISION,
    OTHER,
    OTHER_LABEL,
    REPO,
    SOURCE,
    binding,
    pr_payload,
    record,
    signal,
)
from tests.test_remedy_ledger import FakeEventStorage, FakeRemedyLease  # noqa: F401
from tests.test_remedy_publication import FakeGitHub, sh

repo = base.repo  # the temporary repository with a local bare origin

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
LIMITS = SimpleNamespace(
    decisions_per_pr=5, prs_per_rolling_week=8, open_prs=8, active_directional_issues=12
)
CONFIG = SimpleNamespace(limits=LIMITS)


class LiveGitHub(FakeGitHub):
    def __init__(self):
        super().__init__()
        self.reopened: list[tuple[str, int]] = []
        self.issues: list[tuple[str, str]] = []

    def reopen(self, kind, number):
        self.reopened.append((kind, number))

    def create_issue(self, title, body):
        self.issues.append((title, body))
        return f"https://github.com/{REPO}/issues/{200 + len(self.issues)}"


def meta(label, identity):
    plan = SimpleNamespace(
        accepted=[SimpleNamespace(action="union", target_feeds=["council"], new_feed_slug="")]
    )
    bound = {**binding(label=label), "decision_id": identity}
    return {
        "report": {"decision": {"initial_votes": [], "adjudication": None}},
        "binding": bound,
        "plan": plan,
        "context": None,
        "finding_id": f"finding-{identity[:6]}",
        "evidence_hash": "a" * 64,
    }


def candidate(identity, label, *, kind="pr", age=2.0, **extra):
    return Candidate(
        decision_id=identity,
        city="example-tx",
        family="council",
        outcome="union",
        kind=kind,
        eligible_since=NOW - timedelta(days=age),
        evidence_hash="a" * 64,
        **extra,
    )


def run_live(
    repo,
    tmp_path,
    candidates,
    pr_meta,
    *,
    storage=None,
    snapshot=None,
    github=None,
    dry_run=False,
    issue_meta=None,
    apply=None,
    verify=None,
):
    github = github or LiveGitHub()
    result = publisher._publish_live(
        config=CONFIG,
        candidates=list(candidates),
        pr_meta=pr_meta,
        issue_meta=issue_meta or {},
        storage=storage or FakeEventStorage(),
        repo_root=repo,
        dry_run=dry_run,
        deferred=[],
        source_keys={SOURCE},
        github=github,
        git=pub.Git(repo),
        apply=apply or (lambda worktree: _edit(worktree, "council.yml")),
        verify=verify or (lambda worktree: (True, "")),
        snapshot=snapshot or GitHubSnapshot((), True),
        now=NOW,
    )
    return result, github


def _edit(worktree, name, text="body_any: [x]\n"):
    path = worktree / "config" / "feeds" / name
    path.write_text(path.read_text() + text)
    return [path]


def marked_artifact(*ids, kind="pr", state="open", number=7, days=1.0, **extra):
    return Artifact(
        kind=kind,
        number=number,
        url=f"https://github.com/{REPO}/{'pull' if kind == 'pr' else 'issues'}/{number}",
        state=state,
        opened_at=NOW - timedelta(days=days),
        decision_ids=tuple(ids),
        head_ref=branch_name("example-tx", "council", ids) if kind == "pr" else None,
        **extra,
    )


def test_two_decisions_for_one_feed_become_one_pr_with_markers_and_bindings(repo, tmp_path):
    metas = {DECISION: meta("committee", DECISION), OTHER: meta(OTHER_LABEL, OTHER)}
    result, github = run_live(
        repo, tmp_path, [candidate(DECISION, "committee"), candidate(OTHER, OTHER_LABEL)], metas
    )
    [published] = result["published"]
    assert published["type"] == "proposal_pr" and published["created"]
    [(branch, _title, body)] = github.created
    assert branch == branch_name("example-tx", "council", [DECISION, OTHER])
    assert render_markers([DECISION, OTHER]) in body and render_binding(DECISION, binding()) in body
    assert result["publication_plan"]["mode"] == "live"
    assert result["publication_plan"]["new_prs"][0]["decisions"] == sorted([DECISION, OTHER])
    assert sh("git", "branch", "--show-current", cwd=repo).strip() == "main"


def test_a_repeat_dispatch_updates_nothing_and_opens_nothing_new(repo, tmp_path):
    existing = marked_artifact(DECISION, OTHER)
    result, github = run_live(
        repo,
        tmp_path,
        [candidate(DECISION, "committee"), candidate(OTHER, OTHER_LABEL)],
        {DECISION: meta("committee", DECISION), OTHER: meta(OTHER_LABEL, OTHER)},
        snapshot=GitHubSnapshot((existing,), True),
    )
    assert github.created == [] and result["published"] == []
    assert result["publication_plan"]["updates"][0]["url"] == existing.url
    assert result["status"] == "no_candidates"


def test_the_weekly_budget_queues_work_instead_of_opening_it(repo, tmp_path):
    spent = [
        marked_artifact(f"{i:064x}", state="merged", number=10 + i, days=1 + i * 0.1)
        for i in range(8)
    ]
    result, github = run_live(
        repo,
        tmp_path,
        [candidate(DECISION, "committee")],
        {DECISION: meta("committee", DECISION)},
        snapshot=GitHubSnapshot(tuple(spent), True),
    )
    assert github.created == [] and result["status"] == "queued"
    [queued] = result["publication_plan"]["queued"]
    assert queued["reason"] == "rolling_week_pr_budget" and queued["decision"] == DECISION


def test_an_incomplete_github_listing_opens_nothing(repo, tmp_path):
    result, github = run_live(
        repo,
        tmp_path,
        [candidate(DECISION, "committee")],
        {DECISION: meta("committee", DECISION)},
        snapshot=GitHubSnapshot((), False),
    )
    assert github.created == [] and result["status"] == "queued"
    assert result["publication_plan"]["queued"][0]["reason"] == "github_snapshot_incomplete"


def test_a_dry_run_plans_and_executes_nothing(repo, tmp_path):
    result, github = run_live(
        repo,
        tmp_path,
        [candidate(DECISION, "committee")],
        {DECISION: meta("committee", DECISION)},
        dry_run=True,
    )
    assert result["status"] == "dry_run" and github.created == [] and github.issues == []
    assert result["publication_plan"]["new_prs"]
    assert "remedy" not in sh("git", "branch", cwd=repo)


def test_one_failing_unit_does_not_stop_the_others(repo, tmp_path):
    first, second = "1" * 64, "2" * 64
    metas = {first: meta("committee", first), second: meta("committee", second)}
    cands = [
        Candidate(first, "example-tx", "council", "union", "pr", NOW - timedelta(days=3), "a" * 64),
        Candidate(
            second, "example-tx", "library", "union", "pr", NOW - timedelta(days=2), "a" * 64
        ),
    ]
    calls = []

    def verify(worktree):
        calls.append(worktree.name)
        return (len(calls) > 1, "ruff says no")

    result, github = run_live(
        repo,
        tmp_path,
        cands,
        metas,
        apply=lambda worktree: _edit(worktree, "council.yml") + _edit(worktree, "library.yml")[:0],
        verify=verify,
    )
    assert len(result["failures"]) == 1 and "ruff says no" in result["failures"][0]["reason"]
    assert [p["type"] for p in result["published"]] == ["proposal_pr"] and len(github.created) == 1
    assert sh("git", "status", "--porcelain", cwd=repo).strip() == ""


def test_a_rejected_pr_gets_one_refinement_issue_and_never_a_second_pr(repo, tmp_path):
    storage = FakeEventStorage()
    record(storage, signal(pr_payload("closed")))  # a maintainer closed the PR unmerged
    github = LiveGitHub()
    result, _ = run_live(
        repo,
        tmp_path,
        [candidate(DECISION, "committee")],
        {DECISION: meta("committee", DECISION)},
        storage=storage,
        github=github,
    )
    assert github.created == []  # the rejected evidence does not regenerate a PR
    [issue] = result["published"]
    assert issue["type"] == "refinement_issue"
    [(title, body)] = github.issues
    assert "Remedy refinement" in title and f"https://github.com/{REPO}/pull/41" in body
    assert f"/remedy reopen {DECISION}" in body
    # The next dispatch finds the marked issue open and creates nothing.
    marked = marked_artifact(DECISION, kind="issue", number=201)
    again, github2 = run_live(
        repo,
        tmp_path,
        [candidate(DECISION, "committee")],
        {DECISION: meta("committee", DECISION)},
        storage=storage,
        snapshot=GitHubSnapshot((marked,), True),
    )
    assert github2.issues == [] and again["published"] == []


def test_a_maintainers_reopen_reopens_the_closed_pr_within_the_budget(repo, tmp_path):
    storage = FakeEventStorage()
    record(storage, signal(pr_payload("closed")))
    event_ = fb.reopen_event(
        command_decision=DECISION,
        comment={
            "id": 900,
            "html_url": f"https://github.com/{REPO}/pull/41#issuecomment-900",
            "created_at": "2026-10-17T08:00:00Z",
            "user": {"login": "maintainer"},
            "body": f"/remedy reopen {DECISION}",
        },
        permission={"permission": "write", "user": {"login": "maintainer"}},
        repository=REPO,
        artifact=pr_payload()["pull_request"],
        kind="pr",
        history=fb.load_decisions(storage, source_keys=[SOURCE])[DECISION],
    )
    fb.record_reopen(storage, event_, lease=FakeRemedyLease())
    closed = marked_artifact(DECISION, state="closed", number=41, days=5)
    result, github = run_live(
        repo,
        tmp_path,
        [candidate(DECISION, "committee")],
        {DECISION: meta("committee", DECISION)},
        storage=storage,
        snapshot=GitHubSnapshot((closed,), True),
    )
    assert github.reopened == [("pr", 41)] and github.created == []
    assert [p["type"] for p in result["published"]] == ["reopened"]


# ---- the switch ----------------------------------------------------------------------------


def test_the_publication_mode_defaults_to_report_only_and_is_not_part_of_the_fingerprint():
    raw = yaml.safe_load(open("config/remedy.yml").read())
    raw["publication_mode"] = "report_only"
    config = RemedyConfig.model_validate(raw)
    live = RemedyConfig.model_validate({**raw, "publication_mode": "live"})
    assert config.publication_mode == "report_only" and live.publication_mode == "live"
    assert config_fingerprint(config) == config_fingerprint(live)
    without = {k: v for k, v in raw.items() if k != "publication_mode"}
    assert RemedyConfig.model_validate(without).publication_mode == "report_only"
    with pytest.raises(ValueError):
        RemedyConfig.model_validate({**raw, "publication_mode": "yolo"})


def test_changing_only_the_mode_leaves_the_p3_config_hash_unchanged():
    """The daily caller binds observations to this hash; flipping the switch must not re-ask."""
    raw = yaml.safe_load(open("config/remedy.yml").read())
    before = RemedyConfig.model_validate({k: v for k, v in raw.items() if k != "publication_mode"})
    assert config_fingerprint(before) == config_fingerprint(RemedyConfig.model_validate(raw))
    assert decision_id("example-tx", SOURCE, "committee", "committee") == DECISION


def test_every_kind_of_failure_is_isolated_to_its_unit_and_reported(repo, tmp_path):
    """CodeRabbit CLI review: only PublicationError was isolated, so a ValueError from the
    mutation or a RuntimeError from `gh` skipped every remaining unit and issue."""
    first, second = "1" * 64, "2" * 64
    metas = {first: meta("committee", first), second: meta("committee", second)}
    cands = [
        Candidate(first, "example-tx", "council", "union", "pr", NOW - timedelta(days=3), "a" * 64),
        Candidate(
            second, "example-tx", "library", "union", "pr", NOW - timedelta(days=2), "a" * 64
        ),
        Candidate(
            OTHER, "example-tx", "council", "manual", "issue", NOW - timedelta(days=1), "a" * 64
        ),
    ]
    calls = []

    def apply(worktree):
        calls.append(worktree.name)
        if len(calls) == 1:
            raise ValueError("stale evidence")  # not a PublicationError
        return _edit(worktree, "library.yml")

    github = LiveGitHub()

    def explode(*_a, **_k):
        raise RuntimeError("gh is down")

    github.create_issue = explode
    refinement_storage = FakeEventStorage()
    record(refinement_storage, signal(pr_payload("closed")))
    result, _ = run_live(
        repo,
        tmp_path,
        cands + [candidate(DECISION, "committee")],
        metas,
        github=github,
        apply=apply,
        storage=refinement_storage,
    )
    reasons = {f["unit"]: f["reason"] for f in result["failures"]}
    assert any("ValueError: stale evidence" in r for r in reasons.values())
    assert any("RuntimeError: gh is down" in r for r in reasons.values())
    assert [p["type"] for p in result["published"]] == ["proposal_pr"]  # the second unit went out
    assert sh("git", "status", "--porcelain", cwd=repo).strip() == ""


def test_a_failing_reopen_does_not_stop_the_pass(repo, tmp_path):
    closed = marked_artifact(DECISION, state="closed", number=41, days=5)
    storage = FakeEventStorage()
    record(storage, signal(pr_payload("closed")))
    event_ = fb.reopen_event(
        command_decision=DECISION,
        comment={
            "id": 900,
            "html_url": f"https://github.com/{REPO}/pull/41#issuecomment-900",
            "created_at": "2026-10-17T08:00:00Z",
            "user": {"login": "maintainer"},
            "body": f"/remedy reopen {DECISION}",
        },
        permission={"permission": "write", "user": {"login": "maintainer"}},
        repository=REPO,
        artifact=pr_payload()["pull_request"],
        kind="pr",
        history=fb.load_decisions(storage, source_keys=[SOURCE])[DECISION],
    )
    fb.record_reopen(storage, event_, lease=FakeRemedyLease())
    github = LiveGitHub()

    def explode(kind, number):
        raise RuntimeError("cannot reopen")

    github.reopen = explode
    result, _ = run_live(
        repo,
        tmp_path,
        [candidate(DECISION, "committee"), candidate(OTHER, OTHER_LABEL)],
        {DECISION: meta("committee", DECISION), OTHER: meta(OTHER_LABEL, OTHER)},
        storage=storage,
        snapshot=GitHubSnapshot((closed,), True),
        github=github,
    )
    assert any("cannot reopen" in f["reason"] for f in result["failures"])
    assert any(p["type"] == "proposal_pr" for p in result["published"])  # work went on


def test_an_open_pr_is_left_alone_but_stale_evidence_is_called_out(repo, tmp_path):
    stale_body = (
        "x\n"
        + render_markers([DECISION])
        + "\n"
        + render_binding(DECISION, {**binding(), "evidence_hash": "f" * 64})
    )
    open_pr = marked_artifact(DECISION, number=41, body=stale_body)
    result, github = run_live(
        repo,
        tmp_path,
        [candidate(DECISION, "committee")],
        {DECISION: meta("committee", DECISION)},
        snapshot=GitHubSnapshot((open_pr,), True),
    )
    assert github.created == [] and result["published"] == []
    assert result["unchanged"] == [{"url": open_pr.url, "kind": "pr", "evidence_changed": True}]


def test_an_open_p3_issue_is_refreshed_through_the_issue_reconciler(repo, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(
        publisher, "_reconcile_issue", lambda report, bound, dry_run: seen.append(bound) or "u"
    )
    issue = marked_artifact(DECISION, kind="issue", number=52)
    result, _ = run_live(
        repo,
        tmp_path,
        [candidate(DECISION, "committee", kind="issue")],
        {},
        issue_meta={DECISION: ({"decision": {}}, binding())},
        snapshot=GitHubSnapshot((issue,), True),
    )
    assert seen == [binding()] and result["published"] == [{"type": "issue_updated", "url": "u"}]


def test_the_live_switch_fails_closed_instead_of_falling_back_to_the_legacy_publisher():
    assert publisher._live_fail_closed(False, "bad time", []) is None  # report_only is unaffected
    assert publisher._live_fail_closed(True, "", []) is None
    closed = publisher._live_fail_closed(True, "unreadable evidence collection time: x", [{"a": 1}])
    assert closed == {
        "status": "deferred",
        "published": [],
        "deferred": [{"a": 1}],
        "publication_plan": {"mode": "live", "error": "unreadable evidence collection time: x"},
    }
