"""Tests for the read-only locator shadow report and explicit rollout gates."""

from __future__ import annotations

import pytest

from scripts.research.agenda_chapters.report_locator_shadow import (
    build_report,
    evaluate_gate,
    score_route,
)


def _fixtures():
    results = {
        "provider_labels_in_requests": False,
        "results": [
            {
                "uid": "one",
                "provider": "swagit",
                "agenda_item_count": 3,
                "routes": {
                    "full": {
                        "status": "completed",
                        "route": "full",
                        "anchors": [
                            {"agenda_item_index": 0, "unit_id": "u00001", "start": 10.0},
                            {"agenda_item_index": 1, "unit_id": "u00002", "start": 100.0},
                            # This is close to a provider start but belongs to another item in the
                            # source crosswalk.  It is a suspected signal, not a human verdict.
                            {"agenda_item_index": 2, "unit_id": "u00003", "start": 201.0},
                        ],
                    }
                },
            },
            {
                "uid": "failed",
                "provider": "swagit",
                "routes": {"full": {"status": "failed", "error": "timeout"}},
            },
        ],
    }
    gold = {
        "episodes": [{"uid": "one", "chapters": [{"start": 10}, {"start": 100}, {"start": 200}]}]
    }
    crosswalk = {
        "episodes": [
            {
                "uid": "one",
                "provider_chapters": [
                    {"status": "strong", "best_generated_item_index": 0, "start": 10},
                    {"status": "strong", "best_generated_item_index": 1, "start": 100},
                    {"status": "strong", "best_generated_item_index": 1, "start": 200},
                ],
                "generated_items": [
                    {"generated_item_index": 0, "status": "mapped"},
                    {"generated_item_index": 1, "status": "mapped"},
                    {"generated_item_index": 2, "status": "unmapped"},
                ],
            }
        ]
    }
    return results, gold, crosswalk


def test_report_separates_timing_recall_from_item_precision_and_failures():
    results, gold, crosswalk = _fixtures()
    report = build_report(
        results,
        gold,
        crosswalk=crosswalk,
        tolerance=2,
        gate_kwargs={"min_completed_episodes": 1, "min_provider_start_recall": 0.9},
    )
    route = report["routes"]["full"]
    assert route["counts"]["completed_episodes"] == 1
    assert route["counts"]["failed_episodes"] == 1
    assert route["counts"]["provider_start_hits"] == 3
    assert route["metrics"]["provider_start_recall"] == 1.0
    assert route["metrics"]["provider_start_precision"] == 1.0
    assert route["counts"]["correct_item_valid_boundary"] == 2
    assert route["metrics"]["correct_item_valid_boundary_precision"] == 0.6667
    assert route["counts"]["suspected_wrong_item"] == 1
    assert route["counts"]["suspected_skipped_item_anchors"] == 1
    assert report["gates"]["full"]["status"] == "pass"
    assert report["provider_labels_in_requests"] is False


def test_gate_without_thresholds_is_not_configured():
    results, gold, crosswalk = _fixtures()
    report = build_report(results, gold, crosswalk=crosswalk)
    assert report["gates"]["full"]["status"] == "not_configured"


def test_report_does_not_count_unmatched_gold_as_wrong_item():
    results = {
        "provider_labels_in_requests": False,
        "results": [
            {
                "uid": "one",
                "provider": "civicplus",
                "agenda_item_count": 1,
                "routes": {
                    "full": {
                        "status": "completed",
                        "anchors": [{"agenda_item_index": 0, "unit_id": "u00001", "start": 30}],
                    }
                },
            }
        ],
    }
    gold = {"episodes": [{"uid": "one", "chapters": [{"start": 30}]}]}
    crosswalk = {
        "episodes": [
            {
                "uid": "one",
                "provider_chapters": [
                    {"status": "ambiguous", "best_generated_item_index": 0, "start": 30}
                ],
            }
        ]
    }
    report = build_report(results, gold, crosswalk=crosswalk, tolerance=1)
    assert report["routes"]["full"]["counts"].get("suspected_wrong_item", 0) == 0


def test_duplicate_anchors_match_strong_targets_one_to_one():
    results = {
        "provider_labels_in_requests": False,
        "results": [
            {
                "uid": "dup",
                "provider": "swagit",
                "agenda_item_count": 1,
                "routes": {
                    "full": {
                        "status": "completed",
                        "anchors": [
                            {"agenda_item_index": 0, "unit_id": "u00001", "start": 10.0},
                            {"agenda_item_index": 0, "unit_id": "u00002", "start": 11.0},
                        ],
                    }
                },
            }
        ],
    }
    gold = {"episodes": [{"uid": "dup", "chapters": [{"start": 10.0}]}]}
    crosswalk = {
        "episodes": [
            {
                "uid": "dup",
                "provider_chapters": [
                    {"status": "strong", "best_generated_item_index": 0, "start": 10.0}
                ],
            }
        ]
    }
    report = build_report(results, gold, crosswalk=crosswalk, tolerance=5.0)
    route = report["routes"]["full"]
    # Only 1 strong target exists, so one-to-one matching must count only 1 correct hit
    assert route["counts"]["anchors"] == 2
    assert route["counts"]["correct_item_valid_boundary"] == 1
    assert route["metrics"]["correct_item_valid_boundary_precision"] == 0.5
    # The duplicate anchor is for Item 0, not a different item, so it is NOT suspected_wrong_item
    assert route["counts"].get("suspected_wrong_item", 0) == 0


@pytest.mark.parametrize(
    ("gate_arg", "val"),
    [
        ("min_completed_episodes", 0),
        ("min_completed_episodes", -1),
        ("min_completed_episodes", True),
        ("min_completed_episodes", 1.5),
        ("min_completed_episodes", "two"),
        ("min_provider_start_recall", -0.1),
        ("min_provider_start_recall", 1.1),
        ("min_provider_start_recall", float("nan")),
        ("min_provider_start_recall", float("inf")),
        ("min_provider_start_recall", float("-inf")),
        ("min_provider_start_recall", True),
        ("min_correct_item_precision", -0.5),
        ("min_correct_item_precision", 1.5),
        ("max_suspected_wrong_rate", -0.1),
        ("max_suspected_wrong_rate", 1.2),
        ("max_failed_rate", -0.01),
        ("max_failed_rate", 2.0),
    ],
)
def test_evaluate_gate_rejects_invalid_thresholds(gate_arg, val):
    report = {"counts": {"completed_episodes": 5}, "metrics": {"provider_start_recall": 0.8}}
    with pytest.raises(ValueError):
        evaluate_gate(report, **{gate_arg: val})


def test_build_report_requires_explicit_boolean_provider_labels_in_requests():
    results_missing = {"results": []}
    results_non_bool = {"provider_labels_in_requests": "false", "results": []}
    gold = {"episodes": []}
    with pytest.raises(ValueError, match="provider_labels_in_requests"):
        build_report(results_missing, gold)
    with pytest.raises(ValueError, match="provider_labels_in_requests"):
        build_report(results_non_bool, gold)


@pytest.mark.parametrize(
    "invalid_tolerance",
    [-1.0, -0.01, float("nan"), float("inf"), float("-inf"), True, False, "60"],
)
def test_score_route_and_build_report_reject_invalid_tolerance(invalid_tolerance):
    results = {"provider_labels_in_requests": False, "results": []}
    gold = {"episodes": []}
    with pytest.raises(ValueError, match="tolerance"):
        score_route([], {}, tolerance=invalid_tolerance)
    with pytest.raises(ValueError, match="tolerance"):
        build_report(results, gold, tolerance=invalid_tolerance)
