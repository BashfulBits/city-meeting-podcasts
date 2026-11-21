"""Review/54 §8.14: configured-route input/output cap re-measurement."""

import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from citypods.provider_catalog import limits as limits_module
from citypods.provider_catalog.apply import EditPlan, proposal_body
from citypods.provider_catalog.discovery import DiscoveryObservation, DiscoveryRequest
from citypods.provider_catalog.limits import (
    context_choice,
    is_reduction,
    merge_context_changes,
    producer_review_title,
    reduction_observation,
    remeasure_cap_changes,
    remeasure_notes,
    remeasure_review,
    session_axis_results,
)

NOW = datetime.now(UTC) - timedelta(hours=1)
ROUTE = {
    "route_id": "r",
    "provider": "groq",
    "account_id": "primary",
    "upstream_model": "m",
    "free": True,
    "rpd": 100,
    "input_context_limit": 131072,
    "output_context_limit": 8192,
    "hard_input_ceiling": 100000,
}
CONFIG = {
    "providers": {
        "groq": {
            "api_base": "https://api.example.test/v1",
            "chat_path": "/chat/completions",
            "accounts": [{"id": "primary"}],
        }
    },
    "routes": [ROUTE],
}


@pytest.fixture(autouse=True)
def fixed_identity(monkeypatch):
    monkeypatch.setattr(limits_module, "remeasure_identity", lambda route, config: "id")
    monkeypatch.setattr(limits_module, "remeasure_parser_version", lambda route: "p")


_attempts = iter(range(1_000_000))


def obs(session, days, outcome, size, *, axis="input", basis=None, hint=None):
    """One discovery observation ``days`` before NOW in ``session``."""
    basis = basis or axis
    request = DiscoveryRequest(
        session_id=session,
        attempt_id=f"{session}-{next(_attempts)}",
        identity="id",
        axis=axis,
        target=size,
        input_tokens=min(size, 1_000_000) if axis == "input" else 10,
        output_tokens=min(size, 131_072) if axis == "output" else 10,
        control="c",
        parser_version="p",
    )
    return DiscoveryObservation(
        request=request,
        observed_at=(NOW - timedelta(days=days) + timedelta(seconds=next(_attempts))).isoformat(),
        outcome=outcome,
        coordinate=size if outcome in {"admitted", "refused"} else None,
        count_basis=basis if outcome in {"admitted", "refused"} else "unknown",
        verified=outcome in {"admitted", "refused"},
        target_limit_hint=hint,
        **(
            {"finish_reason": "length", "reported_output": size}
            if axis == "output" and outcome == "admitted"
            else {}
        ),
    )


def refused_session(session, days, *, admitted=80000, refused=90000):
    return [
        obs(session, days, "admitted", admitted),
        obs(session, days, "admitted", admitted - 1000),
        obs(session, days, "refused", refused),
    ]


def test_a_raise_is_offered_from_one_session():
    history = [
        obs("s1", 2, "admitted", 100000),
        obs("s1", 2, "admitted", 115000),
        obs("s1", 2, "admitted", 120000),
        obs("s1", 2, "refused", 124000),
    ]
    (change,) = remeasure_cap_changes(history, CONFIG, now=NOW)
    # Clipped to the window minus the production output reservation.
    assert change[:4] == ("r", "hard_input_ceiling", 100000, min(120000, 131072 - 8192))
    assert not is_reduction(change)


def test_a_raise_within_the_bracket_width_is_noise():
    history = [obs("s1", 2, "admitted", 100200), obs("s1", 2, "admitted", 100300)]
    assert not remeasure_cap_changes(history, CONFIG, now=NOW)


def test_one_session_never_reduces_and_shows_pending():
    review = remeasure_review(refused_session("s1", 8), CONFIG, now=NOW)
    assert not review.changes
    ((rid, axis, cap, pending),) = review.pending
    assert (rid, axis, cap, pending.candidate) == ("r", "input", 100000, 80000)
    # A pending reduction moves the route to the front of the next run.
    assert review.due[0] == ("r", "input")


def test_two_runs_on_different_days_reduce_at_the_larger_value():
    history = [
        *refused_session("s1", 8, admitted=80000, refused=90000),
        *refused_session("s2", 1, admitted=82000, refused=88000),
    ]
    review = remeasure_review(history, CONFIG, now=NOW)
    (change,) = review.changes
    assert change[:4] == ("r", "hard_input_ceiling", 100000, 82000)
    assert is_reduction(change)
    assert not review.pending
    detail = review.details[context_choice(change)]
    assert detail["kind"] == "reduction" and len(detail["sessions"]) == 2


def test_two_sessions_on_the_same_day_do_not_confirm():
    history = [*refused_session("s1", 1), *refused_session("s2", 1)]
    review = remeasure_review(history, CONFIG, now=NOW)
    assert not review.changes and review.pending


def test_inconsistent_observations_leave_the_newer_one_pending():
    # The second session admitted more than the first refused: not the same limit.
    history = [
        *refused_session("s1", 8, admitted=60000, refused=70000),
        *refused_session("s2", 1, admitted=80000, refused=90000),
    ]
    review = remeasure_review(history, CONFIG, now=NOW)
    assert not review.changes
    assert review.pending[0][3].session_id == "s2"


def test_an_admission_at_the_cap_resets_a_pending_reduction():
    history = [
        *refused_session("s1", 15),
        obs("s2", 8, "admitted", 100000),
        obs("s2", 8, "admitted", 99000),
        *refused_session("s3", 1),
    ]
    review = remeasure_review(history, CONFIG, now=NOW)
    assert not review.changes
    assert review.pending[0][3].session_id == "s3"


@pytest.mark.parametrize("outcome", ["inconclusive", "quota", "outage", "daily", "lower_only"])
def test_timeouts_429s_and_outages_are_never_reduction_observations(outcome):
    history = [
        obs("s1", 8, outcome, 90000),
        obs("s2", 1, outcome, 90000),
    ]
    review = remeasure_review(history, CONFIG, now=NOW)
    assert not review.changes and not review.pending


def test_typed_context_length_limit_counts_with_headroom():
    (result,) = session_axis_results([obs("s1", 1, "inconclusive", 1, hint=90000)], "id", "input")
    observation = reduction_observation(result, 100000)
    assert observation.bound == 90000 and observation.candidate == 90000 * 95 // 100


def test_output_raise_is_clipped_to_window_minus_input_ceiling():
    history = [
        obs("s1", 2, "admitted", 30000, axis="output"),
        obs("s1", 2, "admitted", 40000, axis="output"),
    ]
    (change,) = remeasure_cap_changes(history, CONFIG, now=NOW)
    assert change[:4] == ("r", "output_context_limit", 8192, 131072 - 100000)


def test_an_axis_with_no_recent_session_is_due():
    review = remeasure_review([*refused_session("s1", 40)], CONFIG, now=NOW)
    assert set(review.due) == {("r", "input"), ("r", "output")}
    review = remeasure_review(
        [
            obs("s1", 3, "admitted", 99000),
            obs("s2", 3, "admitted", 8000, axis="output"),
        ],
        CONFIG,
        now=NOW,
    )
    assert not review.due


def test_ineligible_routes_are_never_due():
    for change in ({"free": False}, {"rpd": 0}, {"account_id": "gone"}, {"api_shape": "embed"}):
        config = {**CONFIG, "routes": [{**ROUTE, **change}]}
        assert not remeasure_review([], config, now=NOW).due


def test_the_remeasure_offer_wins_over_an_older_duplicate():
    older = (("r", "hard_input_ceiling", 100000, 90000, "a" * 64), ("s", "x", 1, 2, "b" * 64))
    remeasured = (("r", "hard_input_ceiling", 100000, 110000, "c" * 64),)
    merged = merge_context_changes(older, remeasured)
    assert ("r", "hard_input_ceiling", 100000, 110000, "c" * 64) in merged
    assert ("r", "hard_input_ceiling", 100000, 90000, "a" * 64) not in merged
    assert len(merged) == 2


def test_older_context_path_needs_two_days_to_reduce():
    from citypods.provider_catalog.limits import _rejections_on_two_days

    class Row:
        def __init__(self, ceiling, run, day):
            self.reported_ceiling = ceiling
            self.run_id = run
            self.observed_at = (NOW - timedelta(days=day)).isoformat()

    assert not _rejections_on_two_days([Row(900, "1", 1)], 1000)
    assert not _rejections_on_two_days([Row(900, "1", 1), Row(900, "2", 1)], 1000)
    assert not _rejections_on_two_days([Row(900, "1", 1), Row(1100, "2", 8)], 1000)
    assert _rejections_on_two_days([Row(900, "1", 1), Row(950, "2", 8)], 1000)


def test_pr_notes_list_sessions_for_chosen_changes():
    history = [
        *refused_session("s1", 8, admitted=80000, refused=90000),
        *refused_session("s2", 1, admitted=82000, refused=88000),
    ]
    review = remeasure_review(history, CONFIG, now=NOW)
    (note,) = remeasure_notes(review.changes, review.details)
    assert "100000 → 82000" in note and "reduction" in note and note.count("admitted") == 2


# `/apply` producer-review issue (§8.14.D).


def plan_with(*changes):
    from citypods.provider_catalog.limits import reduced_caps

    return EditPlan(
        "base",
        (),
        context_changes=tuple(changes),
        proposal_kind="context",
        reduced_caps=reduced_caps(changes, {}),
    )


REDUCTION = ("r", "hard_input_ceiling", 100000, 82000, "a" * 64)
RAISE = ("q", "output_context_limit", 8192, 16384, "b" * 64)


def test_apply_files_one_issue_per_reduction_and_none_for_raises():
    from scripts import provider_catalog_commands as commands

    calls = []

    def run_fn(args):
        calls.append(args)
        if args[:3] == ["gh", "issue", "list"]:
            return "[]"
        return "https://github.com/o/r/issues/9"

    plan = commands.file_producer_reviews(plan_with(REDUCTION, RAISE), run_fn=run_fn)
    created = [c for c in calls if c[:3] == ["gh", "issue", "create"]]
    assert len(created) == 1
    assert producer_review_title(REDUCTION) in created[0]
    assert created[0].count("--label") == 2
    body = proposal_body(plan)
    assert "https://github.com/o/r/issues/9" in body


def test_apply_reuses_an_open_issue_so_the_body_is_stable():
    from scripts import provider_catalog_commands as commands

    def run_fn(args):
        if args[:3] == ["gh", "issue", "list"]:
            return json.dumps(
                [{"title": producer_review_title(REDUCTION), "url": "https://x/issues/3"}]
            )
        raise AssertionError("must not file a duplicate")

    plan = commands.file_producer_reviews(plan_with(REDUCTION), run_fn=run_fn)
    assert any("https://x/issues/3" in line for line in plan.applied)


def test_apply_notes_a_filing_failure_in_the_pr_body():
    from scripts import provider_catalog_commands as commands

    def run_fn(args):
        raise subprocess.CalledProcessError(1, args)

    plan = commands.file_producer_reviews(plan_with(REDUCTION), run_fn=run_fn)
    body = proposal_body(plan)
    assert "could not be filed" in body and producer_review_title(REDUCTION) in body


def test_no_reduction_files_nothing():
    from scripts import provider_catalog_commands as commands

    def run_fn(args):
        raise AssertionError("no issue for a raise")

    plan = plan_with(RAISE)
    assert commands.file_producer_reviews(plan, run_fn=run_fn) == plan


def test_reduction_observation_ignores_refusals_above_the_cap():
    (result,) = session_axis_results(refused_session("s1", 1, refused=120000), "id", "input")
    assert reduction_observation(result, 100000) is None
    assert reduction_observation(replace(result, refused=100000), 100000) is not None


def test_weekly_script_merges_offers_and_records_pending_state():
    from citypods.provider_catalog.reconcile import Report
    from scripts.reconcile_provider_routes import _apply_remeasure_review

    older = ("r", "hard_input_ceiling", 100000, 95000, "d" * 64)
    report = Report(context_changes=[older], state={"last_full": {}})
    report.remeasure_sessions.append({"route_id": "r", "axis": "input", "ran": True})
    _apply_remeasure_review(report, refused_session("s1", 1), CONFIG)
    # One refusal leaves the older offer alone and shows a pending confirmation instead.
    assert report.context_changes == [older]
    state = report.state["remeasure"]
    assert state["pending"][0]["route_id"] == "r" and state["sessions"][0]["ran"]
    json.dumps(report.state)  # the state marker must stay serialisable

    history = [*refused_session("s0", 8, admitted=80000), *refused_session("s1", 1)]
    _apply_remeasure_review(report, history, CONFIG)
    ((rid, field, old, new, _stamp),) = report.context_changes
    assert (rid, field, old, new) == ("r", "hard_input_ceiling", 100000, 80000)
    assert report.state["last_full"]["context_changes"] == report.context_changes


def test_sessions_one_job_length_apart_across_midnight_do_not_confirm():
    # One run spanning UTC midnight cannot supply both observations.
    late = NOW.replace(hour=23, minute=0) - timedelta(days=2)
    first = [replace(o, observed_at=late.isoformat()) for o in refused_session("s1", 0)]
    second = [
        replace(o, observed_at=(late + timedelta(hours=2)).isoformat())
        for o in refused_session("s2", 0)
    ]
    review = remeasure_review([*first, *second], CONFIG, now=NOW)
    assert not review.changes and review.pending


def test_reduction_details_keep_measured_admissions_apart_from_proposals():
    history = [
        obs("s1", 8, "inconclusive", 1, hint=90000),
        obs("s2", 1, "inconclusive", 1, hint=90000),
    ]
    review = remeasure_review(history, CONFIG, now=NOW)
    (change,) = review.changes
    sessions = review.details[context_choice(change)]["sessions"]
    assert all(s["admitted"] is None and s["proposed"] == 85500 for s in sessions)
    (note,) = remeasure_notes(review.changes, review.details)
    assert "admitted None" in note and "proposes 85500" in note


def test_an_implicit_input_cap_can_be_reduced_and_files_a_review():
    from citypods.provider_catalog.limits import reduced_caps

    route = {k: v for k, v in ROUTE.items() if k != "hard_input_ceiling"}
    config = {**CONFIG, "routes": [route]}
    # Effective cap: 131072 - 8192 = 122880.
    history = [
        *refused_session("s1", 8, admitted=100000, refused=110000),
        *refused_session("s2", 1, admitted=101000, refused=109000),
    ]
    review = remeasure_review(history, config, now=NOW)
    (change,) = review.changes
    assert change[:4] == ("r", "hard_input_ceiling", None, 101000)
    assert not is_reduction(change)
    ((rid, field, old, new),) = reduced_caps(review.changes, review.details)
    assert (rid, field, old, new) == ("r", "hard_input_ceiling", 122880, 101000)


def test_observations_from_another_parser_version_are_ignored():
    history = [
        replace(o, request=replace(o.request, parser_version="old"))
        for o in [obs("s1", 2, "admitted", 115000), obs("s1", 2, "admitted", 120000)]
    ]
    assert not remeasure_cap_changes(history, CONFIG, now=NOW)


MANUAL_WEEKLY_ENV = {
    "GITHUB_EVENT_NAME": "workflow_dispatch",
    "GITHUB_REF": "refs/heads/main",
    "GITHUB_REPOSITORY": "owner/repo",
    "GITHUB_RUN_ID": "100",
    "GITHUB_SHA": "a" * 40,
}


def _manual_weekly_script(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from citypods.provider_catalog.reconcile import Report
    from scripts import reconcile_provider_routes as script

    for key, value in MANUAL_WEEKLY_ENV.items():
        monkeypatch.setenv(key, value)
    control = script.WorkerDispatchControl(SimpleNamespace())
    monkeypatch.setattr(script, "_control", lambda _no_pause: control)
    monkeypatch.setattr(script, "discover_discovery_references", lambda **_kwargs: ())
    monkeypatch.setattr(
        script, "verified_discovery_history", lambda *_args, **_kwargs: ((), (), ())
    )
    monkeypatch.setattr(script, "fetch_quality_index", lambda _session: None)
    seen = {}

    def reconcile(*_args, **kwargs):
        seen.update(kwargs)
        return Report(state={"last_full": {}})

    monkeypatch.setattr(script, "reconcile", reconcile)
    monkeypatch.setattr(script, "backtest_discovery", lambda *_args, **_kwargs: [])
    return script, seen, str(tmp_path / "evidence.json")


def test_a_manual_weekly_dispatch_remeasures_configured_routes(monkeypatch, tmp_path):
    script, seen, evidence = _manual_weekly_script(monkeypatch, tmp_path)
    assert script.main(["--weekly-full", "--discovery-evidence", evidence]) == 0
    assert seen["remeasure_time_budget_seconds"]
    assert seen["remeasure_run_seconds"] and not seen["due_only"]


@pytest.mark.parametrize(
    "extra",
    [["--due-only"], ["--incremental"], ["--provider", "groq"], ["--rate-evidence", "r.json"]],
)
def test_manual_weekly_refuses_other_modes_and_missing_evidence(monkeypatch, tmp_path, extra):
    script, _seen, evidence = _manual_weekly_script(monkeypatch, tmp_path)
    for args in (["--weekly-full", *extra, "--discovery-evidence", evidence], ["--weekly-full"]):
        with pytest.raises(SystemExit) as exc:
            script.main(args)
        assert exc.value.code == 2


@pytest.mark.parametrize(
    ("event", "ref"),
    [("schedule", "refs/heads/main"), ("workflow_dispatch", "refs/heads/feature")],
)
def test_manual_weekly_is_only_for_a_manual_main_dispatch(monkeypatch, tmp_path, event, ref):
    script, _seen, evidence = _manual_weekly_script(monkeypatch, tmp_path)
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.setenv("GITHUB_REF", ref)
    with pytest.raises(SystemExit) as exc:
        script.main(["--weekly-full", "--discovery-evidence", evidence])
    assert exc.value.code == 2
