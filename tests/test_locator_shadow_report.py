"""Tests for the read-only locator shadow report and explicit rollout gates."""

from __future__ import annotations

from scripts.research.agenda_chapters.report_locator_shadow import build_report


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
        ]
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
