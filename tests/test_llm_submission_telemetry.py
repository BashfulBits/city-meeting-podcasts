from __future__ import annotations

import json
from types import SimpleNamespace

from citypods.compute.llm_submission_telemetry import (
    TELEMETRY_FILE_ENV,
    record,
    record_enqueue_outcomes,
    record_producer_observations,
    record_stage_activity,
    render_markdown,
)


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
