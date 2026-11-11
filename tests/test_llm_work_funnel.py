"""Producer funnel telemetry (review/58 section 9): additive fields, old events still readable."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from citypods.compute.base import JobHandle, JobResult
from citypods.compute.llm_work import LLMWorkTracker, write_run_event
from citypods.ops import backlog_trend as trend
from tests.test_llm_work import row, tracker


def test_snapshot_adds_funnel_fields_and_keeps_existing_ones():
    work = tracker()
    work.item("new-purpose", "a", producer="new-stage").defer("queued")
    work.item("new-purpose", "b", producer="new-stage").consumed()
    snap = row(work)
    for legacy in ("states", "backlog", "consumed", "observed", "job_outcomes", "coverage"):
        assert legacy in snap
    assert snap["first_seen_at"] and snap["oldest_eligible_at"]
    assert snap["jobs"] == 0
    assert snap["admission"] == {
        "admitted": 0,
        "replayed": 0,
        "deferred": 0,
        "rejected": 0,
        "prior_pending": 0,
        "cached_completed": 0,
    }
    # Nothing polled: every queued item is unobserved, and the split sums to the queued state.
    assert snap["queue"] == {
        "waiting_at_worker": 0,
        "completed_uncollected": 0,
        "failed_at_worker": 0,
        "unobserved": 1,
    }
    assert "packets" not in snap


def test_oldest_eligible_ignores_consumed_items():
    work = tracker()
    work.item("new-purpose", "done", producer="new-stage").consumed()
    assert row(work)["oldest_eligible_at"] is None
    assert row(work)["first_seen_at"]


def test_admission_survives_the_flush_that_rereports_the_job_as_queued():
    work = tracker()
    item = work.item("new-purpose", "a", producer="new-stage")
    for recipe, status in (
        ("r1", "fresh_admitted"),
        ("r2", "replayed"),
        ("r3", "client_daily_cap"),
    ):
        item.job_outcome(recipe, status)
        item.job_outcome(recipe, "queued")  # what BatchingDispatchBackend.flush reports next
    snap = row(work)
    assert snap["admission"]["admitted"] == 1
    assert snap["admission"]["replayed"] == 1
    assert snap["admission"]["deferred"] == 1
    assert snap["jobs"] == 3
    # The legacy per-recipe view still reports only the latest status.
    assert snap["job_outcomes"] == {"queued": 3}


def test_admission_is_recorded_even_when_the_item_is_already_consumed():
    work = tracker()
    item = work.item("new-purpose", "a", producer="new-stage")
    item.consumed()
    item.job_outcome("r1", "replayed")
    assert row(work)["admission"]["replayed"] == 1
    assert row(work)["states"] == {"consumed": 1}


def test_queue_split_uses_poll_observations_only():
    work = tracker()
    backend = SimpleNamespace(
        poll_batch=lambda handles: {"ref-wait": None, "ref-done": JobResult("tag", {}, "done")}
    )
    for identity, recipe, ref in (
        ("w", "wait", "ref-wait"),
        ("d", "done", "ref-done"),
        ("u", "unpolled", "ref-none"),
    ):
        item = work.item("new-purpose", identity, producer="new-stage")
        item.defer("queued")
        handle = JobHandle("tag", recipe, "llm-dispatch-v2", ref)
        item.backend(backend).poll_batch([handle])
    snap = row(work)
    assert snap["queue"] == {
        "waiting_at_worker": 1,
        "completed_uncollected": 1,
        "failed_at_worker": 0,
        "unobserved": 1,
    }
    assert sum(snap["queue"].values()) == snap["states"]["queued"]
    # Consuming the completed result removes it from the queue split.
    work.item("new-purpose", "d", producer="new-stage").consumed()
    assert row(work)["queue"]["completed_uncollected"] == 0


def test_poll_failures_split_terminal_from_transport_errors():
    from citypods.compute.llm import LLMDispatchMissingError, LLMDispatchTerminalError

    work = tracker()
    errors = {
        "ref-term": LLMDispatchTerminalError("failed permanently"),
        "ref-miss": LLMDispatchMissingError("gone"),
        "ref-net": RuntimeError("transport"),
    }
    backend = SimpleNamespace(poll_batch=lambda handles: errors)
    for ref in errors:
        item = work.item("new-purpose", ref, producer="new-stage")
        item.defer("queued")
        item.backend(backend).poll_batch([JobHandle("tag", ref, "llm-dispatch-v2", ref)])
    assert row(work)["queue"] == {
        "waiting_at_worker": 0,
        "completed_uncollected": 0,
        "failed_at_worker": 1,
        "unobserved": 2,
    }


def test_packet_counts_are_validated_and_optional():
    work = tracker()
    work.item("new-purpose", "a", producer="new-stage")
    with pytest.raises(ValueError, match="Unregistered"):
        work.packet_counts("nope", {"x": 1})
    work.packet_counts("new-purpose", {"outstanding": 4, "bad": -1, "flag": True, "s": "x"})
    assert row(work)["packets"] == {"outstanding": 4}


def test_run_event_with_funnel_fields_is_accepted_by_the_backlog_reader(tmp_path):
    work = tracker()
    work.item("new-purpose", "a", producer="new-stage").defer("queued")
    work.packet_counts("new-purpose", {"outstanding": 2})
    write_run_event(work, tmp_path, "new-stage")
    events = trend.load_events(tmp_path, since=datetime.now(UTC) - timedelta(days=1))
    assert events[0]["llm_work"]["purposes"]["new-purpose"]["packets"] == {"outstanding": 2}


def test_real_lanes_register_for_judge_and_remedy_packet_counts():
    work = LLMWorkTracker()
    work.activate("judge:anchor", producer="judge")
    work.activate("remedy:p3", producer="remedy")
    work.packet_counts("judge:anchor", {"outstanding": 3, "submitted": 1})
    work.packet_counts("remedy:p3", {"findings": 2})
    purposes = work.snapshot()["purposes"]
    assert purposes["judge:anchor"]["packets"] == {"outstanding": 3, "submitted": 1}
    assert purposes["remedy:p3"]["packets"] == {"findings": 2}


def test_remedy_daily_snapshot_counts_findings_without_payloads():
    from scripts.remedy_p3_daily import _funnel_snapshot

    snap = _funnel_snapshot(
        [
            {"finding_id": "f1", "status": "report_ready", "submitted_or_completed": 2},
            {"finding_id": "f2", "status": "initial_pending", "pending": 1},
            {"finding_id": "f3", "status": "policy_hold"},
        ]
    )["purposes"]["remedy:p3"]
    assert snap["states"] == {"consumed": 1, "policy_held": 1, "queued": 1}
    assert snap["packets"] == {"findings": 3, "pending": 1, "submitted_or_completed": 2}
    assert "f1" not in str(snap)
