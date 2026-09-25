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
