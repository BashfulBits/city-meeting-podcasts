from __future__ import annotations

import json
from types import SimpleNamespace

from citypods.compute.llm_submission_telemetry import (
    TELEMETRY_FILE_ENV,
    record,
    record_enqueue_outcomes,
    record_producer_caps,
    record_producer_observations,
    record_stage_activity,
    render_markdown,
)
from scripts.llm_submission_telemetry import _scheduler_summary


def _job(*, purpose: str = "topic-tags", model: str = "gemini/gemini-3.1-flash-lite"):
    """Build the smallest inference-job-shaped test double for telemetry grouping."""
    return SimpleNamespace(
        task="tag",
        inputs={"llm_policy": SimpleNamespace(purpose=purpose, allowed_models=(model,))},
    )


def test_submission_telemetry_records_safe_aggregate_and_renders_summary(tmp_path, monkeypatch):
    """The workflow report exposes all producer, stage, and scheduler bottleneck dimensions."""
    destination = tmp_path / "telemetry.jsonl"
    monkeypatch.setenv(TELEMETRY_FILE_ENV, str(destination))

    record_producer_observations(
        {
            ("topic-tags", "tag", "gemini/gemini-3.1-flash-lite"): {
                "candidate": 5,
                "prior_pending": 7,
            }
        }
    )
    record_enqueue_outcomes(
        [
            (_job(), "fresh_admitted", None),
            (_job(), "replayed", None),
            (_job(), "deferred", "purpose_write_budget_exceeded"),
        ],
        elapsed_seconds=3.25,
        payload_stage_seconds=1.0,
        request_seconds=0.5,
        persist_seconds=1.5,
        transport_retries=0,
    )
    record_stage_activity(
        lane="tag",
        stage="tags",
        ran=409,
        reused=2091,
        backlog=366,
        errors=856,
        seconds=1619.5,
        defer_reasons={"tag-llm-dispatch": 125},
    )
    record(
        "llm_submission_worker_snapshot",
        phase="start",
        summary={
            "active_bundles": 2,
            "active_calls": 3,
            "queued": 97,
            "claimed": 170,
            "ingested": 1117,
            "last_reason": "active_bundle_limit",
            "reason_counts": {"active_bundle_limit": 684},
            "queued_by_model": {"google/gemma-3-27b-it:free": 56},
        },
    )

    events = [json.loads(line) for line in destination.read_text().splitlines()]
    assert all("recipe_hash" not in event for event in events)
    assert all("payload_key" not in event for event in events)
    markdown = render_markdown(events)
    assert "Existing pending" in markdown
    assert "Fresh" in markdown
    assert "purpose_write_budget_exceeded" in markdown
    assert "gemini/gemini-3.1-flash-lite" in markdown
    assert "Producer-stage gates" in markdown
    assert "tag-llm-dispatch" in markdown
    assert "Worker snapshots" in markdown
    assert "active bundles `2`" in markdown


def test_submission_telemetry_is_quiet_without_a_destination(monkeypatch):
    """Disabled telemetry never creates local output or affects job submission."""
    monkeypatch.delenv(TELEMETRY_FILE_ENV, raising=False)
    record_enqueue_outcomes(
        [(_job(), "fresh_admitted", None)],
        elapsed_seconds=0.0,
        payload_stage_seconds=0.0,
        request_seconds=0.0,
        persist_seconds=0.0,
        transport_retries=0,
    )


def test_runner_accounting_surfaces_stuck_slots(tmp_path, monkeypatch):
    from threading import Event

    destination = tmp_path / "telemetry.jsonl"
    monkeypatch.setenv(TELEMETRY_FILE_ENV, str(destination))
    closed = Event()
    closed.set()
    ctx = SimpleNamespace(
        lane="tag",
        tag_max_dispatches=10,
        tag_dispatches_count=2,
        tag_dispatches_reserved=1,
        tag_llm_dispatch_exhausted=closed,
        chapter_locator_max_dispatches=1,
        chapter_locator_dispatches_count=2,
    )
    record_producer_caps(ctx)
    events = [json.loads(line) for line in destination.read_text().splitlines()]
    tagger = next(event for event in events if event["purpose"] == "topic-tags:tagger")
    assert tagger["anomalies"] == [
        "unsettled_reservations",
        "closed_below_cap_check_ingress_reason",
    ]
    assert "producer_cap_exceeded" in render_markdown(events)
    assert "not fresh DO rows" in render_markdown(events)
    assert "fresh_admitted" not in tagger


def test_reconciliation_summary_preserves_unknowns_and_model_independence():
    events = [
        {
            "event": "llm_judge_backpressure",
            "purpose": "judge:sibling",
            "model": model,
            "outstanding": outstanding,
            "limit": 480,
            "room": max(0, 480 - outstanding),
            "run_cap": 20,
        }
        for model, outstanding in (("gemma", 1840), ("qwen", 0))
    ]
    events.extend(
        [
            {"event": "llm_pending_audit", "loaded": 30, "listed": 31, "unavailable": 1},
            {"event": "llm_dispatch_v2_batch", "operation": "poll-batch", "missing": 7},
            {"event": "llm_dispatch_v2_batch", "operation": "poll-batch", "unknown": 2},
            {"event": "llm_pending_poll", "task": "chapter-agenda", "missing": 7},
            {"event": "llm_deferred_sweep_end", "missing_cleared": 6, "v2_unobserved": 2},
        ]
    )
    markdown = render_markdown(events)
    assert "| gemma | 1840 | 480 | 0 | 20 |" in markdown
    assert "| qwen | 0 | 480 | 480 | 20 |" in markdown
    assert "retries can repeat jobs" in markdown
    assert "missing=7" in markdown and "unknown=2" in markdown
    assert "missing handles cleared=6" in markdown
    assert "**Unresolved accounting:**" in markdown
    assert "30 loaded / 31 selected" in markdown
    assert "| chapter-agenda | 0 | 0 | 7 | 0 | 0 |" in markdown


def test_scheduler_summary_matches_worker_stats_shape():
    summary = _scheduler_summary(
        {
            "jobs": {"by_state": {"queued": 12}},
            "bundles": {"active": 2, "active_call_count": 7},
            "scheduler": {"bundle_count_today": 19, "jobs_ingested_today": 83},
            "claim": {
                "last_reason": "active_bundle_limit",
                "empty_count_today": 4,
                "reason_counts_today": {"active_bundle_limit": 11},
            },
            "catalog_rescue": {"digest": "catalog", "cursor": "cursor", "complete": False},
        }
    )
    assert summary == {
        "utc_day": None,
        "active_bundles": 2,
        "active_calls": 7,
        "queued": 12,
        "claimed": 19,
        "ingested": 83,
        "last_reason": "active_bundle_limit",
        "empty_claims": 4,
        "reason_counts": {"active_bundle_limit": 11},
        "row_budget": {
            "rows_written_today": None,
            "account_safe_stop": None,
            "enqueue_open": None,
            "claims_open": None,
        },
        "ingress_by_purpose": [],
        "catalog_rescue": {"digest": "catalog", "cursor": "cursor", "complete": False},
    }


def test_scheduler_snapshot_reports_existing_admission_fields_without_extra_requests():
    summary = _scheduler_summary(
        {
            "scheduler": {"utc_day": "2026-10-18"},
            "row_budget": {
                "rows_written_today": 9981,
                "account_safe_stop": 90000,
                "enqueue_open": True,
                "claims_open": True,
                "secret": "never-include",
            },
            "ingress": {
                "by_purpose": [
                    {"purpose": "chapter-agenda", "jobs_ingested": 0, "daily_quota": 200}
                ]
            },
        }
    )
    markdown = render_markdown([{"event": "llm_submission_worker_snapshot", "summary": summary}])
    assert "UTC day `2026-10-18`" in markdown
    assert "rows written `9981` / safe stop `90000`" in markdown
    assert "enqueue open `True`" in markdown
    assert "`chapter-agenda`: ingress today `0` / `200`" in markdown
    assert "never-include" not in json.dumps(summary)


def test_old_worker_has_unknown_recovery_evidence():
    assert _scheduler_summary({})["catalog_rescue"] == {
        "digest": None,
        "cursor": None,
        "complete": None,
    }


def test_empty_recovery_summary_explicitly_reports_missing_evidence():
    for events in ([], [{"event": "llm_submission_worker_snapshot", "summary": {}}]):
        markdown = render_markdown(events)
        assert "### Structural recovery evidence" in markdown
        assert "No recovery observations were recorded" in markdown
        assert "Missing observations do not establish canary success" in markdown
        assert "Persisted audits:" not in markdown


def test_recovery_summary_reports_count_changes_and_bounds_recipe_sample():
    events = [
        {
            "event": "llm_structural_recovery",
            "recipe_fingerprint": f"fingerprint-{index}",
            "operation": "audit_persisted",
            "failure_count_before": 2,
            "failure_count_after": 3 if index == 0 else 2,
            "schema_correction_before": True,
            "schema_correction_after": index != 0,
        }
        for index in range(12)
    ]
    decision = {
        "event": "llm_structural_recovery",
        "recipe_fingerprint": "fingerprint-0",
        "operation": "submission_decision",
        "disposition": "unchanged_or_unfitting_generation",
    }
    events.extend([decision, decision])
    markdown = render_markdown(events)
    assert "Persisted audits: `12`" in markdown
    assert "retry-count changes: `1`" in markdown
    assert "schema-state changes: `1`" in markdown
    assert "`unchanged_or_unfitting_generation`=2" in markdown
    assert markdown.count("| `fingerprint-0` | submission_decision") == 1
    assert "fingerprint-9" in markdown and "fingerprint-10" not in markdown
    assert "Missing observations do not establish canary success" in markdown
