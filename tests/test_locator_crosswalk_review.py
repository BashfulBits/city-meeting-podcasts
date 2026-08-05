"""Tests for the development-only locator crosswalk review packet selector."""

from scripts.research.agenda_chapters.prepare_locator_crosswalk_review import (
    _category,
    _episode_fields,
    _line_excerpt,
)


def test_crosswalk_ambiguity_threshold_and_plain_numbered_title():
    assert (
        _category({"status": "strong", "top_candidates": [{"score": 0.60}, {"score": 0.60}]})
        == "ambiguous"
    )
    assert (
        _category(
            {
                "status": "strong",
                "provider_title": "Approve the contract",
                "source_best": {"status": "strong"},
                "top_candidates": [{"score": 0.59}, {"score": 0.59}],
            }
        )
        == "clear_control"
    )
    assert (
        _category(
            {
                "status": "strong",
                "provider_title": "1. Approve the contract",
                "source_best": {"status": "strong"},
                "top_candidates": [],
            }
        )
        == "clear_control"
    )


def test_crosswalk_strata_prioritize_ambiguity_and_source_gaps():
    assert _category({"status": "ambiguous", "top_candidates": []}) == "ambiguous"
    assert (
        _category(
            {
                "status": "unmatched",
                "source_best": {"status": "strong"},
                "top_candidates": [],
            }
        )
        == "source_strong_generated_gap"
    )


def test_crosswalk_strata_cover_procedural_unmatched_and_control():
    assert (
        _category({"status": "strong", "provider_title": "PUBLIC HEARING", "top_candidates": []})
        == "procedural_or_consent"
    )
    assert (
        _category(
            {
                "status": "unmatched",
                "provider_title": "3. A. Item",
                "source_best": {"status": "unmatched"},
                "top_candidates": [],
            }
        )
        == "unmatched_structural_or_hierarchical"
    )
    assert (
        _category(
            {
                "status": "strong",
                "provider_title": "Approve the contract",
                "source_best": {"status": "strong"},
                "top_candidates": [],
            }
        )
        == "clear_control"
    )


def test_line_excerpt_marks_only_source_lines():
    assert _line_excerpt(["one", "two", "three", "four"], 2, 3, padding=1) == [
        {"number": 1, "text": "one", "highlight": False},
        {"number": 2, "text": "two", "highlight": True},
        {"number": 3, "text": "three", "highlight": True},
        {"number": 4, "text": "four", "highlight": False},
    ]


def test_episode_fields_do_not_copy_timing_or_transcript_pointers():
    result = _episode_fields(
        {
            "uid": "u",
            "provider": "swagit",
            "slug": "city",
            "body": "Board",
            "published": "2026-01-01",
            "duration_bucket": "2-to-4h",
            "agenda": {"url": "https://example.test/agenda", "bytes": 12},
            "transcript": {"url": "secret"},
            "chapter_starts": [1.0],
        }
    )
    assert result["uid"] == "u"
    assert set(result) == {
        "uid",
        "provider",
        "slug",
        "body",
        "published",
        "duration_bucket",
        "agenda_url",
        "agenda_bytes",
    }
