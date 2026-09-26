"""The token-budget monitor turns Worker failure counts into actionable, lane-named findings."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import llm_budget_monitor as mon  # noqa: E402

LANES = {
    "r6-moments": SimpleNamespace(models=("zai/glm-5.3-flash",), backup_models=()),
    "tournament:tag": SimpleNamespace(models=("zai/glm-5.3-flash",), backup_models=()),
    "topic-tags:prelabeler": SimpleNamespace(models=("google/gemma-4-31b-it",), backup_models=()),
}
ROUTES = {
    "glm": {"model": "zai/glm-5.3-flash", "reasoning_controls": {"off": {"x": 1}, "low": {"y": 2}}},
    "gemma": {"model": "google/gemma-4-31b-it", "tpm": 14400},
}


def _row(route_id, failure_class, count):
    return {
        "utc_day": "2026-09-25",
        "route_id": route_id,
        "failure_class": failure_class,
        "count": count,
    }


def test_a_length_stop_at_the_route_limit_suggests_lowering_reasoning_for_that_lane():
    report, findings = mon.build_report([_row("glm", "output_budget_exhausted", 5)], ROUTES, LANES)
    [finding] = findings
    assert finding["lanes"] == ["r6-moments", "tournament:tag"]
    # r6-moments sends the route's limit: the model reasoned to it -> lower its reasoning there.
    assert 'reasoning["zai/glm-5.3-flash"]' in finding["suggestion"]
    assert "['low', 'off']" in finding["suggestion"]
    # tournament:tag sends a fixed budget -> raise it or opt into route_max.
    assert "tournament:tag send a fixed budget" in finding["suggestion"]
    assert "`output_budget_exhausted`" in report


def test_counts_below_threshold_and_unknown_classes_are_not_findings():
    rows = [
        _row("glm", "output_budget_exhausted", 2),
        _row("gemma", "own_tpm", 19),
        _row("gemma", "server_error", 500),
    ]
    report, findings = mon.build_report(rows, ROUTES, LANES)
    assert findings == [] and report.startswith("No token-budget")


def test_own_tpm_names_the_route_tpm_and_the_lanes():
    _report, [finding] = mon.build_report([_row("gemma", "own_tpm", 125)], ROUTES, LANES)
    assert finding["lanes"] == ["topic-tags:prelabeler"]
    assert "tpm 14400" in finding["suggestion"]


def test_jobs_failed_over_the_route_ceiling_name_the_ceiling_and_the_lanes():
    """The Worker's claim-time oversize failures name the route ceiling and the affected lanes."""
    routes = {"gemma": {**ROUTES["gemma"], "hard_input_ceiling": 14400}}
    report, [finding] = mon.build_report(
        [_row("gemma", "input_over_route_ceiling", 1)], routes, LANES
    )
    assert finding["lanes"] == ["topic-tags:prelabeler"]
    assert "`hard_input_ceiling` (14400)" in finding["suggestion"]
    assert "`input_over_route_ceiling`" in report


def test_route_max_lanes_match_the_job_builders_that_set_the_flag():
    # The monitor's advice depends on which lanes send the route's limit; keep it in step with code.
    flagged = {
        "chapter-agenda": ROOT / "citypods/chapter_jobs.py",
        "chapter-locator": ROOT / "citypods/chapter_jobs.py",
        "r6-moments": ROOT / "citypods/stages.py",
        "topic-tags:tagger": ROOT / "citypods/tags.py",
    }
    for path in set(flagged.values()):
        assert '"max_tokens_mode": "route_max"' in path.read_text(), path
    assert set(flagged) == mon.ROUTE_MAX_LANES


def _usage(**over):
    row = {
        "purpose": "chapter-agenda",
        "route_id": "nemotron",
        "calls": 40,
        "reserved_output_mean": 16384,
        "output_tokens_p90": 12000,
        "over_reservation_calls": 0,
        "slow_calls": 0,
        "max_duration_ms": 90_000,
    }
    return {**row, **over}


def test_usage_flags_reservations_that_are_too_small_or_too_large_and_slow_calls():
    kinds = lambda rows: sorted(f["kind"] for f in mon.usage_findings(rows))  # noqa: E731
    assert kinds([_usage()]) == []  # a healthy reservation
    assert kinds([_usage(over_reservation_calls=10)]) == ["reservation_too_small"]
    assert kinds([_usage(output_tokens_p90=2000)]) == ["reservation_too_large"]
    assert kinds([_usage(slow_calls=3, max_duration_ms=700_000)]) == ["slow_calls"]
    assert kinds([_usage(calls=5, output_tokens_p90=10)]) == []  # too few calls to judge


def test_usage_findings_appear_in_the_report_even_without_failures():
    report, findings = mon.build_report([], ROUTES, LANES, [_usage(output_tokens_p90=2000)])
    assert [f["kind"] for f in findings] == ["reservation_too_large"]
    assert "reservation_too_large" in report and "Reservations and latency" in report


def test_unmeasured_calls_do_not_make_a_lane_look_over_reserved():
    # 30 calls, but only 5 returned usage: too few measurements to judge the reservation.
    row = _usage(calls=30, measured_calls=5, output_tokens_p90=10)
    assert mon.usage_findings([row]) == []


def test_the_stats_request_names_only_the_classes_the_report_acts_on(monkeypatch):
    seen = {}

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {}

    def fake_get(url, params, headers, timeout):
        seen.update(params)
        return _Response()

    monkeypatch.setattr(mon.requests, "get", fake_get)
    mon.fetch_stats("https://dispatch.example.com", "token")
    assert seen["failure_class"].split(",") == sorted(mon.THRESHOLDS)
