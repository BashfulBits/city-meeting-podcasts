"""Offline tests for deterministic locator retrieval research paths."""

from __future__ import annotations

from citypods.chapter_locator import LocatorUnit
from scripts.research.agenda_chapters.build_locator_packets import build_packet
from scripts.research.agenda_chapters.evaluate_locator_retrieval import (
    lexical_scores,
    ranked_unit_indices,
    score_episode,
    union_ranked_indices,
)


def _units() -> list[LocatorUnit]:
    return [
        LocatorUnit("u00001", 0.0, 5.0, "Call to order and opening remarks."),
        LocatorUnit("u00002", 10.0, 15.0, "Staff discussed the DCA26-0002B proposed revision."),
        LocatorUnit("u00003", 20.0, 25.0, "Members voted to approve the proposed revision."),
    ]


def test_lexical_identifier_score_prefers_matching_unit():
    scores = lexical_scores(
        {
            "title": "Proposed revision",
            "evidence_text": "DCA26-0002B proposed revision",
            "display_ref": "DCA26-0002B",
        },
        _units(),
    )
    assert ranked_unit_indices(scores, top_k=1) == [1]


def test_union_expands_neighboring_windows():
    lexical = [0.1, 0.9, 0.0]
    tfidf = [0.0, 0.8, 0.0]
    assert union_ranked_indices(lexical, tfidf, top_k=1) == [0, 1, 2]


def test_score_episode_uses_hidden_crosswalk_only_for_scoring():
    units = _units()
    result = score_episode(
        manifest_row={
            "uid": "episode-1",
            "provider": "swagit",
            "slug": "planning",
            "split": "development",
            "generated_agenda": {
                "mistral/mistral-medium-2508": {
                    "items": [
                        {
                            "title": "Proposed revision",
                            "evidence_text": "DCA26-0002B proposed revision",
                            "display_ref": "DCA26-0002B",
                        }
                    ]
                }
            },
        },
        crosswalk_row={
            "provider_chapters": [
                {
                    "status": "strong",
                    "best_generated_item_index": 0,
                    "start": 10.0,
                }
            ]
        },
        units=units,
        top_ks=(1, 3),
        tolerance=1.0,
        model="mistral/mistral-medium-2508",
    )
    assert result["covered_provider_chapters"] == 1
    assert result["covered_generated_candidates"] == 1
    assert result["hits"]["1"]["lexical"] == 1
    assert result["hits"]["1"]["union"] == 1
    assert result["candidate_hits"]["1"]["union"] == 1
    assert result["neighbor_radius"] == 1
    assert result["full_context_input_tokens"] > 0


def test_compact_packet_adds_learned_units_without_exposing_labels():
    packet = build_packet(
        {
            "uid": "episode-1",
            "provider": "swagit",
            "slug": "planning",
            "generated_agenda": {
                "mistral/mistral-medium-2508": {
                    "items": [
                        {
                            "title": "Proposed revision",
                            "evidence_text": "DCA26-0002B proposed revision",
                            "display_ref": "DCA26-0002B",
                        }
                    ]
                }
            },
        },
        _units(),
        agenda_model="mistral/mistral-medium-2508",
        scorer_detail={
            "item_diagnostics": {
                "0": {"learned_top_units": [{"id": "u00003", "score": 0.9, "start": 20.0}]}
            }
        },
        top_k=1,
        neighbor_radius=0,
        crosswalk_row={
            "provider_chapters": [
                {"status": "strong", "best_generated_item_index": 0, "start": 20.0}
            ]
        },
        tolerance=1.0,
    )
    compact = packet["routes"]["learned_compact"]
    assert "u00003" in compact["unit_ids"]
    assert packet["provider_labels_in_requests"] is False
    assert packet["hidden_scores"]["routes"]["learned_compact"]["candidate_hits"] == 1


def test_compact_packet_applies_top_k_to_learned_candidates():
    packet = build_packet(
        {
            "uid": "episode-1",
            "provider": "swagit",
            "slug": "planning",
            "generated_agenda": {
                "mistral/mistral-medium-2508": {
                    "items": [
                        {
                            "title": "No matching words",
                            "evidence_text": "No matching words",
                            "display_ref": "REF-1",
                        }
                    ]
                }
            },
        },
        _units(),
        agenda_model="mistral/mistral-medium-2508",
        scorer_detail={
            "item_diagnostics": {
                "0": {
                    "learned_top_units": [
                        {"id": "u00003", "score": 0.9, "start": 20.0},
                        {"id": "u00002", "score": 0.8, "start": 10.0},
                    ]
                }
            }
        },
        top_k=1,
        neighbor_radius=0,
        crosswalk_row=None,
        tolerance=1.0,
    )

    # u00001 is the deterministic tie winner; only the first learned candidate is admitted.
    assert packet["routes"]["learned_compact"]["unit_ids"] == ["u00001", "u00003"]
