"""The queue round-trip probe: job shape, ordering and summary."""

from __future__ import annotations

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "llm_queue_latency_probe", Path("scripts/llm_queue_latency_probe.py")
)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def test_the_probe_job_is_a_queue_only_city_onboarding_request_at_the_asked_priority():
    for priority in (0, 1):
        job = probe.make_job(priority, "run:0", "gemini/test-model")
        policy = job.inputs["llm_policy"]
        assert policy.priority == priority and policy.queue_only and policy.timeout_class == "fast"
        assert policy.purpose == "city-onboarding" and policy.allowed_models == (
            "gemini/test-model",
        )
        assert not policy.require_direct and not policy.allow_paid
        assert job.inputs["max_tokens"] <= 16
    # A fresh recipe per call and per priority: the registry would answer a repeat from a result.
    hashes = {probe.make_job(p, n, "m").recipe_hash for p in (0, 1) for n in ("a:0", "a:1")}
    assert len(hashes) == 4


def test_priorities_alternate_in_pairs_so_neither_always_goes_first():
    assert probe.order(3) == [1, 0, 0, 1, 1, 0]


def test_summary_reports_each_priority_and_counts_failures():
    rows = [
        {"priority": 1, "outcome": "completed", "submit_seconds": 1.0, "total_seconds": 30.0},
        {"priority": 1, "outcome": "completed", "submit_seconds": 1.0, "total_seconds": 50.0},
        {"priority": 1, "outcome": "timeout", "total_seconds": 1200.0},
        {"priority": 0, "outcome": "completed", "submit_seconds": 1.0, "total_seconds": 8.0},
        {"priority": 0, "outcome": "completed", "submit_seconds": 2.0, "total_seconds": 12.0},
    ]
    summary = probe.summarize(rows)
    assert summary["0"]["completed"] == 2 and summary["0"]["total_median"] == 10.0
    assert summary["0"]["sequence_total"] == 20.0
    assert summary["1"]["calls"] == 3 and summary["1"]["outcomes"] == {"completed": 2, "timeout": 1}
    assert summary["1"]["total_max"] == 50.0
    text = probe.markdown(summary, {"before": {"queued": 4}, "after": "unavailable"})
    assert "| 0 | 2 | 2 |" in text and "| 1 | 3 | 2 |" in text and "queued" in text


def test_an_all_failed_priority_still_summarizes():
    summary = probe.summarize([{"priority": 0, "outcome": "submit_error", "error": "X"}])
    assert summary["0"] == {"calls": 1, "completed": 0, "outcomes": {"submit_error": 1}}
