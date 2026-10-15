"""review/51 P4: feedback from remedy PRs and issues, and `/remedy reopen`, into the P2 ledger."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from citypods import remedy_feedback as fb
from citypods.bodies import body_key
from citypods.remedy_ledger import append_event, decision_id, load_decisions
from citypods.remedy_queue import (
    Artifact,
    branch_name,
    parse_snapshot,
    render_binding,
    render_markers,
)
from tests.test_remedy_ledger import FakeEventStorage, FakeRemedyLease, event

REPO = "BashfulBits/city-meeting-podcasts"
SOURCE = "source-a"
LABEL = "committee"
DECISION = decision_id("example-tx", SOURCE, "committee", LABEL)
OTHER_LABEL = body_key("Library Board")
OTHER = decision_id("example-tx", SOURCE, "committee", OTHER_LABEL)
BOT = {"login": "github-actions[bot]", "type": "Bot"}


def binding(label=LABEL, **extra):
    return {
        "city": "example-tx",
        "source_key": SOURCE,
        "policy_id": "committee",
        "normalized_label": label,
        "evidence_hash": "a" * 64,
        "config_hash": "b" * 64,
        "policy_hash": "c" * 64,
        "recording_refs": [{"source_key": SOURCE, "uid": "uid-1", "provider_guid": None}],
        **extra,
    }


def body(*pairs):
    ids = [i for i, _b in pairs]
    return (
        "Proposed.\n"
        + render_markers(ids)
        + "\n"
        + "\n".join(render_binding(i, b) for i, b in pairs)
    )


def pr_payload(action="closed", *, merged=False, pairs=None, sender="maintainer", **overrides):
    pairs = pairs or [(DECISION, binding())]
    ids = [i for i, _b in pairs]
    pr = {
        "number": 41,
        "html_url": f"https://github.com/{REPO}/pull/41",
        "user": BOT,
        "body": body(*pairs),
        "merged": merged,
        "merged_at": "2026-10-16T09:00:00Z" if merged else None,
        "closed_at": "2026-10-16T09:00:00Z",
        "updated_at": "2026-10-16T10:00:00Z",
        "head": {
            "repo": {"full_name": REPO},
            "ref": branch_name("example-tx", "council", ids),
        },
    }
    pr.update(overrides)
    return {
        "action": action,
        "repository": {"full_name": REPO},
        "sender": {"login": sender, "type": "User"},
        "pull_request": pr,
    }


def issue_payload(action="closed", *, sender="maintainer", **overrides):
    issue = {
        "number": 52,
        "html_url": f"https://github.com/{REPO}/issues/52",
        "user": BOT,
        "body": body((DECISION, binding())),
        "closed_at": "2026-10-16T09:30:00Z",
        "updated_at": "2026-10-16T10:30:00Z",
    }
    issue.update(overrides)
    return {
        "action": action,
        "repository": {"full_name": REPO},
        "sender": {"login": sender, "type": "User"},
        "issue": issue,
    }


def signal(payload, name="pull_request_target"):
    return fb.signal_from_event(name, payload, repository=REPO)


def record(storage, sig):
    return fb.record_signal(storage, sig, lease=FakeRemedyLease())


def tip(storage, identity=DECISION):
    state = load_decisions(storage, source_keys=[SOURCE])[identity]
    return state, next(e for e in state.events if e.event_id == state.tip_ids[0])


# ---- verification -------------------------------------------------------------------------


def test_a_merged_remedy_pr_is_a_recognized_signal():
    sig = signal(pr_payload(merged=True))
    assert (sig.kind, sig.action, sig.actor, sig.number) == ("pr", "merged", "maintainer", 41)
    assert sig.decision_ids == (DECISION,) and sig.at == "2026-10-16T09:00:00+00:00"
    assert sig.delivery == f"{REPO}:pr:41:merged:2026-10-16T09:00:00+00:00"


@pytest.mark.parametrize(
    "payload,reason",
    [
        ({**pr_payload(), "repository": {"full_name": "someone/else"}}, "not for this repository"),
        ({**pr_payload(), "sender": {"login": "robot", "type": "Bot"}}, "bots do not produce"),
        (pr_payload(user={"login": "mallory", "type": "User"}), "Actions identity"),
        (pr_payload(body="no markers here"), "binding for every"),
        (
            pr_payload(
                head={"repo": {"full_name": "fork/repo"}, "ref": "fix/remedy-x-aaaaaaaaaaaa"}
            ),
            "not in this repository",
        ),
        (
            pr_payload(
                head={
                    "repo": {"full_name": REPO},
                    "ref": "fix/remedy-example-tx-council-aaaaaaaaaaaa",
                }
            ),
            "branch is not the one",
        ),
        (pr_payload(html_url=f"https://github.com/{REPO}/pull/99"), "URL does not match"),
        (pr_payload("edited"), "unsupported action"),
    ],
)
def test_signals_that_cannot_be_trusted_are_refused(payload, reason):
    with pytest.raises(fb.FeedbackError, match=reason):
        signal(payload)


def test_a_binding_that_does_not_recompute_to_its_decision_is_refused():
    forged = pr_payload(pairs=[(DECISION, binding(label="a-different-body"))])
    with pytest.raises(fb.FeedbackError, match="recompute"):
        signal(forged)


def test_a_pull_request_is_not_a_remedy_issue_and_other_events_are_unsupported():
    payload = issue_payload()
    payload["issue"]["pull_request"] = {"url": "x"}
    with pytest.raises(fb.FeedbackError, match="not a remedy issue"):
        signal(payload, "issues")
    with pytest.raises(fb.FeedbackError, match="unsupported event"):
        signal(pr_payload(), "push")


# ---- events -------------------------------------------------------------------------------


def test_merging_records_the_maintainers_approval():
    storage = FakeEventStorage()
    [key] = record(storage, signal(pr_payload(merged=True)))
    state, tip_event = tip(storage)
    assert (state.state, state.disposition) == ("covered", "assigned")
    assert tip_event.approval_ref == f"https://github.com/{REPO}/pull/41"
    assert tip_event.actor_kind == "maintainer" and tip_event.actor_id == "maintainer"
    assert tip_event.evidence_hash == "a" * 64 and tip_event.recording_refs[0].uid == "uid-1"
    assert key.endswith(".json") and not state.diagnostics


def test_closing_unmerged_is_a_rejection_that_a_refinement_issue_answers():
    storage = FakeEventStorage()
    record(storage, signal(pr_payload("closed")))
    state, tip_event = tip(storage)
    assert (state.state, state.disposition) == ("refinement_needed", "rejected")
    assert tip_event.artifact_ref == f"https://github.com/{REPO}/pull/41"
    [(identity, found)] = fb.refinement_candidates(load_decisions(storage, source_keys=[SOURCE]))
    assert identity == DECISION and found.artifact_ref.endswith("/pull/41")


def test_a_reopened_pr_is_back_in_review_with_the_rejection_as_its_parent():
    storage = FakeEventStorage()
    record(storage, signal(pr_payload("closed")))
    record(storage, signal(pr_payload("reopened")))
    state, tip_event = tip(storage)
    assert (state.state, state.disposition) == ("in_review", None)
    assert len(state.events) == 2 and tip_event.parent_event_ids == (state.events[0].event_id,)


def test_closing_a_refinement_issue_holds_the_decision_and_reopening_resumes_it():
    storage = FakeEventStorage()
    record(storage, signal(issue_payload("closed"), "issues"))
    assert tip(storage)[0].state == "blocked" and tip(storage)[0].disposition == "watch"
    record(storage, signal(issue_payload("reopened"), "issues"))
    assert tip(storage)[0].state == "in_review"


def test_the_same_fact_is_recorded_once_whether_by_webhook_or_retry():
    storage = FakeEventStorage()
    sig = signal(pr_payload(merged=True))
    first = record(storage, sig)
    again = record(storage, sig)
    assert first and again == []  # already recorded: nothing appended
    assert len(tip(storage)[0].events) == 1


def test_a_close_never_unsettles_a_decision_a_person_already_covered():
    storage = FakeEventStorage()
    record(storage, signal(pr_payload(merged=True)))
    other = pr_payload("closed")
    other["pull_request"]["number"] = 77
    other["pull_request"]["html_url"] = f"https://github.com/{REPO}/pull/77"
    assert record(storage, signal(other)) == []
    assert tip(storage)[0].state == "covered"


def test_a_damaged_history_stops_the_write():
    storage = FakeEventStorage()
    refs = [{"source_key": SOURCE, "uid": "u", "provider_guid": None}]
    first = event(state="blocked", disposition="watch", recording_refs=refs)
    second = event(
        state="in_review",
        disposition=None,
        approval_ref=None,
        rationale="a second root",
        occurred_at="2026-10-11T00:00:00Z",
        recording_refs=refs,
    )
    for payload in (first, second):
        append_event(storage, payload, lease=FakeRemedyLease())
    with pytest.raises(fb.FeedbackError, match="damaged or forked"):
        record(storage, signal(pr_payload("closed")))


# ---- one decision among several -----------------------------------------------------------


def test_a_pr_carrying_several_decisions_records_each_with_its_own_parents():
    storage = FakeEventStorage()
    pairs = [(DECISION, binding()), (OTHER, binding(label=OTHER_LABEL))]
    keys = record(storage, signal(pr_payload(merged=True, pairs=pairs)))
    assert len(keys) == 2
    states = load_decisions(storage, source_keys=[SOURCE])
    assert {s.state for s in states.values()} == {"covered"} and set(states) == {DECISION, OTHER}


# ---- reconciliation -----------------------------------------------------------------------


def _artifact(**extra):
    base = dict(
        kind="pr",
        number=41,
        url=f"https://github.com/{REPO}/pull/41",
        state="merged",
        opened_at=datetime(2026, 10, 14, tzinfo=UTC),
        decision_ids=(DECISION,),
        head_ref=branch_name("example-tx", "council", [DECISION]),
        actor="github-actions[bot]",
        body=body((DECISION, binding())),
        head_repo=REPO,
        settled_by="maintainer",
        settled_at=datetime(2026, 10, 16, 9, 0, tzinfo=UTC),
    )
    base.update(extra)
    return Artifact(**base)


def test_reconciliation_recovers_a_missed_merge_with_the_same_delivery_id_as_the_webhook():
    storage = FakeEventStorage()
    hook = signal(pr_payload(merged=True))
    reconciled = fb.signal_from_snapshot(_artifact(), repository=REPO)
    assert reconciled.delivery == hook.delivery and reconciled.actor == "maintainer"
    record(storage, reconciled)
    assert tip(storage)[0].state == "covered"
    assert record(storage, hook) == []  # the webhook arriving late changes nothing


@pytest.mark.parametrize(
    "extra",
    [
        {"state": "open"},
        {"settled_by": "github-actions[bot]"},
        {"settled_by": ""},
        {"settled_at": None},
        {"actor": "mallory"},
        {"head_repo": "fork/repo"},
        {"decision_ids": (OTHER,)},
        {"head_ref": "fix/remedy-example-tx-council-aaaaaaaaaaaa"},
    ],
)
def test_reconciliation_ignores_artifacts_it_cannot_verify(extra):
    assert fb.signal_from_snapshot(_artifact(**extra), repository=REPO) is None


def test_the_graphql_listing_carries_who_settled_each_artifact():
    nodes = {
        "issueCount": 1,
        "pageInfo": {"hasNextPage": False},
        "nodes": [
            {
                "number": 41,
                "url": f"https://github.com/{REPO}/pull/41",
                "state": "MERGED",
                "createdAt": "2026-10-14T00:00:00Z",
                "mergedAt": "2026-10-16T09:00:00Z",
                "mergedBy": {"__typename": "User", "login": "maintainer"},
                "body": body((DECISION, binding())),
                "headRefName": branch_name("example-tx", "council", [DECISION]),
                "headRepository": {"nameWithOwner": REPO},
                "author": {"__typename": "Bot", "login": "github-actions"},
                "timelineItems": {
                    "nodes": [
                        {
                            "__typename": "ClosedEvent",
                            "createdAt": "2026-10-16T09:00:00Z",
                            "actor": {"__typename": "User", "login": "maintainer"},
                        }
                    ]
                },
            },
            {
                "number": 52,
                "url": f"https://github.com/{REPO}/issues/52",
                "state": "CLOSED",
                "createdAt": "2026-10-14T00:00:00Z",
                "body": body((DECISION, binding())),
                "author": {"__typename": "Bot", "login": "github-actions"},
                "timelineItems": {
                    "nodes": [
                        {
                            "__typename": "ClosedEvent",
                            "createdAt": "2026-10-15T00:00:00Z",
                            "actor": {"__typename": "User", "login": "early"},
                        },
                        {
                            "__typename": "ClosedEvent",
                            "createdAt": "2026-10-16T09:30:00Z",
                            "actor": {"__typename": "User", "login": "maintainer"},
                        },
                        {"__typename": "ReopenedEvent", "createdAt": "2026-10-15T12:00:00Z"},
                    ]
                },
            },
        ],
    }
    pr, issue = parse_snapshot(nodes).artifacts
    assert (pr.settled_by, pr.head_repo) == ("maintainer", REPO)
    assert (issue.settled_by, issue.settled_at.isoformat()) == (
        "maintainer",
        "2026-10-16T09:30:00+00:00",
    )
    assert len(issue.reopened_at) == 1  # closes are not reopenings
    assert fb.signal_from_snapshot(pr, repository=REPO).action == "merged"
    assert fb.signal_from_snapshot(issue, repository=REPO).action == "closed"


# ---- /remedy reopen -----------------------------------------------------------------------

COMMENT_URL = f"https://github.com/{REPO}/pull/41#issuecomment-900"


def comment(**extra):
    return {
        "id": 900,
        "html_url": COMMENT_URL,
        "created_at": "2026-10-17T08:00:00Z",
        "user": {"login": "maintainer"},
        "body": f"/remedy reopen {DECISION}",
        **extra,
    }


WRITE = {"permission": "write", "user": {"login": "maintainer"}}


def pr_artifact():
    return pr_payload()["pull_request"]


def reopen(storage, **overrides):
    history = load_decisions(storage, source_keys=[SOURCE]).get(DECISION)
    args = dict(
        command_decision=DECISION,
        comment=comment(),
        permission=WRITE,
        repository=REPO,
        artifact=pr_artifact(),
        kind="pr",
        history=history,
    )
    args.update(overrides)
    return fb.reopen_event(**args)


def test_the_command_is_parsed_strictly():
    assert fb.parse_reopen_command(f"/remedy reopen {DECISION}") == DECISION
    assert fb.parse_reopen_command(f"/remedy reopen {DECISION}\n") == DECISION
    for text in (
        "/remedy reopen",
        "/remedy reopen xyz",
        f"/remedy reopen {DECISION} now",
        f"hello\n/remedy reopen {DECISION}",
        f"/remedy  reopen {DECISION}",
        None,
        7,
    ):
        assert fb.parse_reopen_command(text) is None


def test_reopening_a_rejected_decision_records_a_proposed_event_that_changes_nothing_else():
    storage = FakeEventStorage()
    record(storage, signal(pr_payload("closed")))
    rejected = tip(storage)[1]
    event_ = reopen(storage)
    assert (event_.state, event_.disposition) == ("proposed", None)
    assert event_.parent_event_ids == (rejected.event_id,)
    for field in ("evidence_hash", "config_hash", "policy_hash", "policy_id", "recording_refs"):
        assert getattr(event_, field) == getattr(rejected, field)
    assert event_.approval_ref == COMMENT_URL and event_.actor_id == "maintainer"
    fb.record_reopen(storage, event_, lease=FakeRemedyLease())
    state = load_decisions(storage, source_keys=[SOURCE])[DECISION]
    assert fb.reopened(state) and state.state == "proposed"
    # A retried delivery returns the same event instead of failing or duplicating.
    assert reopen(storage).event_id == event_.event_id


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"permission": {"permission": "read", "user": {"login": "maintainer"}}}, "write"),
        (
            {"permission": {"permission": "write", "user": {"login": "someone-else"}}},
            "does not belong",
        ),
        ({"comment": comment(id=0)}, "comment id"),
        (
            {"comment": comment(html_url="https://github.com/other/repo/pull/41#issuecomment-900")},
            "comment URL",
        ),
        ({"command_decision": OTHER}, "does not carry that decision"),
        (
            {"artifact": {**pr_artifact(), "user": {"login": "mallory", "type": "User"}}},
            "Actions identity",
        ),
    ],
)
def test_a_reopen_that_cannot_be_trusted_is_refused(overrides, reason):
    storage = FakeEventStorage()
    record(storage, signal(pr_payload("closed")))
    with pytest.raises(fb.FeedbackError, match=reason):
        reopen(storage, **overrides)


def test_there_must_be_history_and_the_decision_must_not_already_be_open():
    with pytest.raises(fb.FeedbackError, match="no recorded decision history"):
        reopen(FakeEventStorage())
    storage = FakeEventStorage()
    record(storage, signal(pr_payload("closed")))
    record(storage, signal(pr_payload("reopened")))
    with pytest.raises(fb.FeedbackError, match="already open"):
        reopen(storage)


def test_reopened_is_false_for_an_ordinary_proposed_decision_and_for_open_ones():
    storage = FakeEventStorage()
    append_event(
        storage,
        event(
            state="proposed",
            disposition=None,
            approval_ref=None,
            recording_refs=[{"source_key": SOURCE, "uid": "u", "provider_guid": None}],
        ),
        lease=FakeRemedyLease(),
    )
    assert not fb.reopened(load_decisions(storage, source_keys=[SOURCE])[DECISION])
    assert not fb.reopened(None)


# ---- the command-line wrapper and the workflows ---------------------------------------------

import contextlib  # noqa: E402
import json  # noqa: E402
from pathlib import Path  # noqa: E402

import yaml  # noqa: E402

from citypods.remedy_queue import GitHubSnapshot  # noqa: E402
from scripts import remedy_feedback as cli  # noqa: E402

PERMISSION = {"permission": "write", "user": {"login": "maintainer"}}


@contextlib.contextmanager
def _lease():
    yield FakeRemedyLease()


def test_a_webhook_event_needs_the_senders_current_write_permission():
    storage = FakeEventStorage()
    payload = pr_payload(merged=True)
    result = cli.apply_event(
        "pull_request_target", payload, PERMISSION, REPO, storage=storage, lease_factory=_lease
    )
    assert result["recorded"] == 1 and tip(storage)[0].state == "covered"
    for denied in (
        {"permission": "read", "user": {"login": "maintainer"}},
        {"permission": "write", "user": {"login": "someone-else"}},
        {"permission": "none"},
    ):
        with pytest.raises(fb.FeedbackError):
            cli.apply_event(
                "pull_request_target",
                pr_payload("closed"),
                denied,
                REPO,
                storage=FakeEventStorage(),
                lease_factory=_lease,
            )


def test_a_reopen_command_works_on_an_issue_and_on_a_pr():
    for kind, artifact in (("issue", None), ("pr", pr_artifact())):
        storage = FakeEventStorage()
        record(storage, signal(issue_payload("closed"), "issues"))
        issue = issue_payload()["issue"]
        event_ = {
            "comment": comment(
                html_url=f"https://github.com/{REPO}/{'pull' if kind == 'pr' else 'issues'}/"
                f"{41 if kind == 'pr' else 52}#issuecomment-900"
            ),
            "issue": issue,
        }
        result = cli.apply_reopen(
            event_, WRITE, REPO, artifact, storage=storage, lease_factory=_lease
        )
        assert result["recorded"] == 1 and result["decision"] == DECISION
        assert tip(storage)[0].state == "proposed"
    with pytest.raises(fb.FeedbackError, match="not a `/remedy reopen"):
        cli.apply_reopen(
            {"comment": comment(body="/remedy"), "issue": issue_payload()["issue"]},
            WRITE,
            REPO,
            None,
            storage=FakeEventStorage(),
            lease_factory=_lease,
        )


def test_reconciliation_records_each_settled_artifact_once_and_nothing_on_a_bad_listing():
    storage = FakeEventStorage()
    snapshot = GitHubSnapshot((_artifact(),), complete=True)
    first = cli.apply_reconcile(snapshot, REPO, storage=storage, lease_factory=_lease)
    again = cli.apply_reconcile(snapshot, REPO, storage=storage, lease_factory=_lease)
    assert (first["checked"], first["recorded"]) == (1, 1) and again["recorded"] == 0
    cut = cli.apply_reconcile(
        GitHubSnapshot((_artifact(),), complete=False), REPO, storage=storage, lease_factory=_lease
    )
    assert cut == {"recorded": 0, "checked": 0, "reason": "github_snapshot_incomplete"}
    assert len(tip(storage)[0].events) == 1


def test_an_untrusted_event_is_a_quiet_outcome_but_a_storage_failure_is_a_red_run(
    tmp_path, monkeypatch
):
    event = tmp_path / "event.json"
    event.write_text(json.dumps(pr_payload(user={"login": "mallory", "type": "User"})))
    permission = tmp_path / "permission.json"
    permission.write_text(json.dumps(PERMISSION))
    out = tmp_path / "out.json"
    argv = ["event", "--event-name", "pull_request_target", "--event", str(event)]
    argv += ["--permission", str(permission), "--repository", REPO, "--result", str(out)]
    monkeypatch.setattr(cli, "_storage", lambda: FakeEventStorage())
    monkeypatch.setattr(cli, "_lease", lambda storage, owner: _lease())
    assert cli.main(argv) == 0
    refused = json.loads(out.read_text())
    assert refused["accepted"] is False and "Actions identity" in refused["error"]

    def broken():
        raise RuntimeError("B2 down")

    monkeypatch.setattr(cli, "_storage", broken)
    assert cli.main(argv) == 1
    assert "could not be recorded" in json.loads(out.read_text())["error"]


WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def test_the_feedback_workflow_is_safe_on_pull_request_target():
    text = (WORKFLOWS / "remedy-feedback.yml").read_text()
    flow = yaml.safe_load(text)
    assert flow["permissions"] == {} and "concurrency" not in flow  # not workflow-level
    assert flow[True]["pull_request_target"]["types"] == ["closed", "reopened"]  # YAML `on` is True
    for name in ("feedback", "reconcile"):
        # Job-level, so unrelated PR events never enter the group, and every waiting remedy run is
        # kept: a group otherwise holds one pending run and replaces it with the next.
        assert flow["jobs"][name]["concurrency"] == {
            "group": "remedy-taxonomy-writes",
            "cancel-in-progress": False,
            "queue": "max",
        }
    job = flow["jobs"]["feedback"]
    guard = job["if"]
    for required in (
        "github.event.sender.type != 'Bot'",
        "github.event.pull_request.head.repo.full_name == github.repository",
        "'github-actions[bot]'",
        "<!-- citypods:remedy:v1:",
    ):
        assert required in guard
    assert job["permissions"] == {"contents": "read", "pull-requests": "read", "issues": "read"}
    checkout = next(s for s in job["steps"] if "checkout" in str(s.get("uses", "")))
    assert checkout["with"] == {"ref": "main", "persist-credentials": False}
    # Never the PR head, and no model-provider credentials anywhere in the file.
    assert "pull_request.head.sha" not in text and "head.ref }}" not in text
    for secret in ("_API_KEY", "LLM_DISPATCH", "BEATAPI", "OPENAI", "GEMINI"):
        assert secret not in text
    assert flow["jobs"]["reconcile"]["if"].count("refs/heads/main") == 1
    # A piped step must still fail when the script does: `tee` alone would hide its exit code.
    for name in ("feedback", "reconcile"):
        runs = [
            step["run"]
            for step in flow["jobs"][name]["steps"]
            if "remedy_feedback.py" in step.get("run", "")
        ]
        assert runs and all(run.lstrip().startswith("set -o pipefail") for run in runs)


def test_the_reopen_job_is_maintainer_only_and_runs_from_main():
    flow = yaml.safe_load((WORKFLOWS / "remedy-commands.yml").read_text())
    job = flow["jobs"]["reopen"]
    assert job["concurrency"] == {
        "group": "remedy-taxonomy-writes",
        "cancel-in-progress": False,
        "queue": "max",
    }
    for required in (
        "'/remedy reopen '",
        "'github-actions[bot]'",
        '["OWNER","MEMBER","COLLABORATOR"]',
    ):
        assert required in job["if"]
    checkout = next(s for s in job["steps"] if "checkout" in str(s.get("uses", "")))
    assert checkout["with"]["ref"] == "main"
    # The old `/remedy` job keeps its own marker requirement, so a reopen is not mistaken for it.
    assert "citypods:feed-health:key=unexpected-body" in flow["jobs"]["remedy"]["if"]


def test_a_graphql_bot_login_is_normalized_so_actions_artifacts_are_recognized():
    """CodeRabbit CLI review: GraphQL reports Actions as `github-actions` with `__typename: Bot`."""
    nodes = {
        "issueCount": 2,
        "pageInfo": {"hasNextPage": False},
        "nodes": [
            {
                "number": 41,
                "url": f"https://github.com/{REPO}/pull/41",
                "state": "CLOSED",
                "createdAt": "2026-10-14T00:00:00Z",
                "body": body((DECISION, binding())),
                "headRefName": branch_name("example-tx", "council", [DECISION]),
                "headRepository": {"nameWithOwner": REPO},
                "author": {"__typename": "Bot", "login": "github-actions"},
                "timelineItems": {
                    "nodes": [
                        {
                            "__typename": "ClosedEvent",
                            "createdAt": "2026-10-16T09:00:00Z",
                            "actor": {"__typename": "User", "login": "maintainer"},
                        }
                    ]
                },
            },
            {
                "number": 52,
                "url": f"https://github.com/{REPO}/issues/52",
                "state": "CLOSED",
                "createdAt": "2026-10-14T00:00:00Z",
                "body": body((DECISION, binding())),
                "author": {"__typename": "Bot", "login": "github-actions"},
                "timelineItems": {
                    "nodes": [
                        {
                            "__typename": "ClosedEvent",
                            "createdAt": "2026-10-16T09:30:00Z",
                            "actor": {"__typename": "Bot", "login": "github-actions"},
                        }
                    ]
                },
            },
        ],
    }
    pr, issue = parse_snapshot(nodes).artifacts
    assert pr.actor == "github-actions[bot]" and pr.settled_by == "maintainer"
    assert fb.signal_from_snapshot(pr, repository=REPO) is not None  # recognized as ours
    assert issue.settled_by == "github-actions[bot]"
    assert fb.signal_from_snapshot(issue, repository=REPO) is None  # a bot's close is no feedback
    impostor = {**nodes["nodes"][0], "author": {"__typename": "User", "login": "github-actions"}}
    [fake] = parse_snapshot({**nodes, "nodes": [impostor]}).artifacts
    assert fake.actor == "github-actions" and fb.signal_from_snapshot(fake, repository=REPO) is None
