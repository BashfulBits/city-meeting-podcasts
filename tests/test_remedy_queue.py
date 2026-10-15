"""review/51 P4: bounded publication planning, markers, branches and snapshots."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from citypods import remedy_queue as q

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
LIMITS = SimpleNamespace(
    decisions_per_pr=5, prs_per_rolling_week=8, open_prs=8, active_directional_issues=12
)


def did(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()


def cand(name, city="dallas-tx", family="council", outcome="assign", age_days=1.0, **extra):
    return q.Candidate(
        decision_id=did(name),
        city=city,
        family=family,
        outcome=outcome,
        kind=extra.pop("kind", "pr"),
        eligible_since=NOW - timedelta(days=age_days),
        evidence_hash=extra.pop("evidence_hash", "e1"),
        **extra,
    )


def artifact(name, kind="pr", state="open", opened_days_ago=1.0, number=None, **extra):
    return q.Artifact(
        kind=kind,
        number=number or int(did(name)[:6], 16),
        url=f"https://github.com/o/r/{'pull' if kind == 'pr' else 'issues'}/{name}",
        state=state,
        opened_at=NOW - timedelta(days=opened_days_ago),
        decision_ids=tuple(did(n) for n in extra.pop("decisions", (name,))),
        head_ref=f"fix/remedy-{name}" if kind == "pr" else None,
        **extra,
    )


def plan(candidates, artifacts=(), ledger=None, complete=True, limits=LIMITS):
    return q.plan_publications(
        candidates,
        ledger or {},
        q.GitHubSnapshot(tuple(artifacts), complete),
        limits=limits,
        now=NOW,
    )


# ---- markers and branches ------------------------------------------------------------------


def test_markers_round_trip_and_reject_malformed_ids():
    ids = [did("b"), did("a")]
    body = "Proposed.\n" + q.render_markers(ids) + "\nMore text."
    assert q.parse_markers(body) == tuple(sorted(ids))
    assert (
        q.parse_markers(None) == () and q.parse_markers("<!-- citypods:remedy:v1:short -->") == ()
    )
    assert q.marker(did("a")) == f"<!-- citypods:remedy:v1:{did('a')} -->"
    with pytest.raises(ValueError):
        q.marker("not-a-hash")


def test_a_branch_is_derived_from_the_unit_ids_not_their_order_or_the_evidence():
    first = q.branch_name("Dallas, TX", "City Council!", [did("b"), did("a")])
    again = q.branch_name("dallas-tx", "city-council", [did("a"), did("b"), did("a")])
    assert first == again
    assert (
        first.startswith("fix/remedy-dallas-tx-city-council-") and len(first.split("-")[-1]) == 12
    )
    assert q.branch_name("dallas-tx", "council", [did("a")]) != first


@pytest.mark.parametrize("city,family", [("", "council"), ("dallas", "!!!"), ("日本", "council")])
def test_unusable_branch_components_are_refused(city, family):
    with pytest.raises(ValueError):
        q.branch_name(city, family, [did("a")])
    with pytest.raises(ValueError):
        q.branch_name("dallas-tx", "council", [])


# ---- packing -------------------------------------------------------------------------------


def test_a_pr_packs_at_most_five_decisions_and_the_sixth_starts_another():
    result = plan([cand(f"d{i}", age_days=10 - i) for i in range(6)])
    sizes = sorted(len(pr.decision_ids) for pr in result.prs)
    assert sizes == [1, 5] and not result.queued
    assert all(pr.branch == q.branch_name(pr.city, pr.family, pr.decision_ids) for pr in result.prs)


def test_only_the_same_city_family_and_outcome_share_a_pr():
    result = plan(
        [
            cand("a"),
            cand("b"),
            cand("other-city", city="austin-tx"),
            cand("other-family", family="zoning"),
            cand("other-outcome", outcome="new_feed"),
        ]
    )
    groups = sorted(sorted(pr.decision_ids) for pr in result.prs)
    assert len(result.prs) == 4
    assert sorted([did("a"), did("b")]) in groups


def test_a_new_body_never_shares_a_pr_with_an_automatic_alias():
    result = plan(
        [
            cand("alias-1", automatic_alias=True),
            cand("alias-2", automatic_alias=True),
            cand("brand-new", new_body=True),
            cand("plain"),
        ]
    )
    for pr in result.prs:
        kinds = {
            (
                "alias"
                if i in {did("alias-1"), did("alias-2")}
                else "new"
                if i == did("brand-new")
                else "plain"
            )
            for i in pr.decision_ids
        }
        assert not {"alias", "new"} <= kinds


# ---- scheduling ----------------------------------------------------------------------------


def test_cities_take_turns_oldest_first_so_one_backlog_cannot_starve_another():
    big = [cand(f"big-{i}", city="dallas-tx", family=f"f{i}", age_days=30 - i) for i in range(10)]
    small = [cand("small", city="austin-tx", family="x", age_days=2)]
    narrow = q.plan_publications(
        big + small,
        {},
        q.GitHubSnapshot(()),
        limits=SimpleNamespace(
            decisions_per_pr=1, prs_per_rolling_week=2, open_prs=8, active_directional_issues=12
        ),
        now=NOW,
    )
    assert {pr.city for pr in narrow.prs} == {"dallas-tx", "austin-tx"}
    ordered = q.round_robin(big + small)
    assert [c.city for c in ordered[:2]] == ["dallas-tx", "austin-tx"]
    assert [c.decision_id for c in ordered if c.city == "dallas-tx"] == [
        did(f"big-{i}") for i in range(10)
    ]


def test_excess_work_stays_queued_with_its_reason_and_age_and_nothing_is_dropped():
    result = plan(
        [cand(f"d{i}", family=f"f{i}", age_days=i + 1) for i in range(10)],
        limits=SimpleNamespace(
            decisions_per_pr=5, prs_per_rolling_week=8, open_prs=8, active_directional_issues=12
        ),
    )
    assert len(result.prs) == 8
    assert len(result.queued) == 2
    assert {item.reason for item in result.queued} == {"rolling_week_pr_budget"}
    assert all(item.age_days > 0 for item in result.queued)
    # Every candidate is either planned or queued.
    planned = {i for pr in result.prs for i in pr.decision_ids}
    assert planned | {item.decision_id for item in result.queued} == {
        did(f"d{i}") for i in range(10)
    }


# ---- budgets -------------------------------------------------------------------------------


def test_the_rolling_week_counts_every_marked_pr_opened_or_reopened_by_anyone():
    old = [artifact(f"old-{i}", opened_days_ago=20 + i, state="merged") for i in range(5)]
    recent = [
        artifact(f"recent-{i}", opened_days_ago=2 + i, state="closed", actor="human")
        for i in range(3)
    ]
    reopened = artifact(
        "reopened", opened_days_ago=30, state="open", reopened_at=(NOW - timedelta(days=1),)
    )
    result = plan([cand("new")], old + recent + [reopened])
    assert result.budget.opened_in_window == 4  # three recent + one reopening; the old ones are out
    assert result.budget.pr_slots == 4  # min(8 - 4 weekly, 8 - 1 open)
    assert len(result.prs) == 1


def test_closing_or_merging_frees_an_open_slot_but_never_resets_the_weekly_count():
    full = [artifact(f"p{i}", opened_days_ago=1 + i * 0.1) for i in range(8)]
    blocked = plan([cand("x")], full)
    assert blocked.queued[0].reason == "rolling_week_pr_budget" and not blocked.prs
    closed = [artifact(f"p{i}", opened_days_ago=1 + i * 0.1, state="merged") for i in range(8)]
    still = plan([cand("x")], closed)
    assert still.budget.open_prs == 0 and still.budget.pr_slots == 0
    assert still.queued[0].reason == "rolling_week_pr_budget"


def test_the_open_pr_cap_binds_when_the_week_is_not_spent():
    open_old = [artifact(f"o{i}", opened_days_ago=20 + i) for i in range(8)]
    result = plan([cand("x")], open_old)
    assert result.budget.opened_in_window == 0 and result.budget.pr_slots == 0
    assert result.queued[0].reason == "open_pr_cap"


def test_directional_issues_are_capped_separately_from_prs():
    active = [artifact(f"i{i}", kind="issue", opened_days_ago=3) for i in range(11)]
    result = plan([cand("a", kind="issue"), cand("b", kind="issue"), cand("pr-work")], active)
    assert len(result.issues) == 1 and result.issues[0].decision_id in {did("a"), did("b")}
    assert [i.reason for i in result.queued] == ["issue_capacity"]
    assert len(result.prs) == 1  # PR work is unaffected by the issue cap


def test_an_incomplete_snapshot_reserves_nothing():
    result = plan([cand("a"), cand("b", kind="issue")], complete=False)
    assert not result.prs and not result.issues
    assert {i.reason for i in result.queued} == {"github_snapshot_incomplete"}
    assert result.notes


# ---- dedupe, transitions and reopening -----------------------------------------------------


def test_a_marked_open_artifact_is_updated_and_consumes_no_slot():
    existing = artifact("a", decisions=("a", "b"))
    result = plan([cand("a"), cand("b"), cand("c")], [existing])
    assert [u.artifact.url for u in result.updates] == [existing.url]
    assert result.updates[0].decision_ids == tuple(sorted([did("a"), did("b")]))
    assert [pr.decision_ids for pr in result.prs] == [(did("c"),)]
    assert result.budget.opened_in_window == 1  # the existing one; the update added none


def test_merged_or_closed_work_is_not_republished_unless_a_reopen_was_requested():
    merged = artifact("m", state="merged", opened_days_ago=40)
    closed = artifact("c", state="closed", opened_days_ago=40)
    result = plan([cand("m"), cand("c")], [merged, closed])
    assert {s.reason for s in result.skipped} == {"already_merged", "already_closed"}
    assert not result.prs and not result.reopens

    asked = plan(
        [cand("m", reopen_requested=True), cand("c", reopen_requested=True)], [merged, closed]
    )
    # A merged PR cannot be reopened; a closed one can, and it takes a slot.
    assert [r.artifact.url for r in asked.reopens] == [closed.url]
    assert asked.budget.pr_slots == 8 and any(s.reason == "already_merged" for s in asked.skipped)


def test_a_reopening_counts_against_the_budget_before_new_work():
    closed = artifact("c", state="closed", opened_days_ago=40)
    seven = [artifact(f"w{i}", state="merged", opened_days_ago=1 + i * 0.1) for i in range(7)]
    result = plan([cand("new"), cand("c", reopen_requested=True)], [closed, *seven])
    assert len(result.reopens) == 1  # takes the last weekly slot
    assert not result.prs and result.queued[0].reason == "rolling_week_pr_budget"


def test_a_reopening_that_does_not_fit_stays_queued_with_its_city_and_age():
    closed = artifact("c", state="closed", opened_days_ago=40)
    full = [artifact(f"w{i}", state="merged", opened_days_ago=1 + i * 0.1) for i in range(8)]
    result = plan([cand("c", city="austin-tx", age_days=9, reopen_requested=True)], [closed, *full])
    [item] = result.queued
    assert (item.reason, item.city, item.age_days) == ("reopen_budget", "austin-tx", 9.0)
    assert not result.reopens


def test_a_rejected_decision_does_not_regenerate_until_its_evidence_changes():
    rejected = {did("r"): q.LedgerView("refinement_needed", "rejected", "e1")}
    same = plan([cand("r", evidence_hash="e1")], ledger=rejected)
    assert [s.reason for s in same.skipped] == ["rejected_unchanged_evidence"] and not same.prs
    changed = plan([cand("r", evidence_hash="e2")], ledger=rejected)
    assert len(changed.prs) == 1 and not changed.skipped


def test_a_settled_decision_with_current_evidence_is_not_published():
    settled = {did("s"): q.LedgerView("covered", "assigned", "e1")}
    result = plan([cand("s")], ledger=settled)
    assert [s.reason for s in result.skipped] == ["terminal_disposition_current"]


def test_a_repeated_dispatch_plans_nothing_new_once_its_artifacts_exist():
    first = plan([cand("a"), cand("b"), cand("i", kind="issue")])
    published = [
        artifact("pr", decisions=("a", "b"), number=1),
        artifact("issue", kind="issue", decisions=("i",), number=2),
    ]
    second = plan([cand("a"), cand("b"), cand("i", kind="issue")], published)
    assert first.prs and first.issues
    assert not second.prs and not second.issues and len(second.updates) == 2


def test_duplicate_candidates_are_planned_once():
    result = plan([cand("a"), cand("a")])
    assert len(result.prs) == 1 and [s.reason for s in result.skipped] == ["duplicate_candidate"]


def test_the_plan_report_is_plain_data():
    result = plan([cand("a"), cand("b", kind="issue")], [artifact("old", state="merged")])
    report = q.plan_report(result)
    assert report["new_prs"][0]["decisions"] == [did("a")]
    assert report["new_issues"][0]["decision"] == did("b")
    assert report["budget"]["weekly_limit"] == 8 and report["queued"] == []


# ---- snapshots -----------------------------------------------------------------------------


def _node(number, body, state="OPEN", pr=True, **extra):
    node = {
        "number": number,
        "url": f"https://github.com/o/r/{'pull' if pr else 'issues'}/{number}",
        "state": state,
        "createdAt": "2026-10-14T10:00:00Z",
        "body": body,
        "author": {"login": "bot"},
        **extra,
    }
    if pr:
        node["headRefName"] = f"fix/remedy-x-{number}"
    return node


def test_a_graphql_search_result_becomes_a_snapshot():
    payload = {
        "issueCount": 3,
        "pageInfo": {"hasNextPage": False},
        "nodes": [
            _node(
                1,
                q.render_markers([did("a")]),
                timelineItems={"nodes": [{"createdAt": "2026-10-14T12:00:00Z"}]},
            ),
            _node(
                2,
                q.render_markers([did("b"), did("c")]),
                state="MERGED",
                mergedAt="2026-10-15T00:00:00Z",
            ),
            _node(3, q.marker(did("d")), pr=False, state="CLOSED"),
            _node(4, "no marker here"),  # a search hit without a marker is ignored
        ],
    }
    snapshot = q.parse_snapshot(payload)
    assert snapshot.complete
    assert [(a.kind, a.state) for a in snapshot.artifacts] == [
        ("pr", "open"),
        ("pr", "merged"),
        ("issue", "closed"),
    ]
    assert snapshot.artifacts[0].reopened_at and snapshot.artifacts[1].decision_ids == tuple(
        sorted([did("b"), did("c")])
    )


def test_a_truncated_search_is_an_incomplete_snapshot():
    cut = q.parse_snapshot(
        {
            "issueCount": 5,
            "pageInfo": {"hasNextPage": False},
            "nodes": [_node(1, q.marker(did("a")))],
        }
    )
    assert not cut.complete
    paged = q.parse_snapshot({"pageInfo": {"hasNextPage": True}, "nodes": []})
    assert not paged.complete


def test_a_first_generation_p3_issue_key_still_names_its_decision():
    body = f"intro\n<!-- citypods:remedy-p3:key={did('legacy')} -->\nmore"
    assert q.parse_markers(body) == (did("legacy"),)
    both = body + q.marker(did("new"))
    assert q.parse_markers(both) == tuple(sorted([did("legacy"), did("new")]))


def test_a_decision_may_be_both_a_pr_candidate_and_a_refinement_issue():
    rejected = {did("r"): q.LedgerView("refinement_needed", "rejected", "e1")}
    result = plan(
        [
            cand("r", evidence_hash="e1"),
            cand("r", kind="issue", evidence_hash="e1", refines="pr-url"),
        ],
        ledger=rejected,
    )
    assert [s.reason for s in result.skipped] == ["rejected_unchanged_evidence"]  # no second PR
    assert [i.decision_id for i in result.issues] == [did("r")]  # the refinement issue goes out
    assert result.issues[0].refines == "pr-url"
