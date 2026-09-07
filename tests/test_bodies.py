"""Tests for generic per-body extraction/filtering."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from citypods.bodies import (
    body_key,
    canonical_body,
    filter_by_body,
    granicus_body,
    matches,
    matches_exact_body_label,
    record_matches_body,
    source_body_filter,
    source_body_inclusions,
)
from citypods.models import Episode


def _ep(body):
    return Episode(
        guid=body,
        title=body,
        published=datetime(2026, 5, 1, tzinfo=UTC),
        video_url="x",
        body=body,
    )


def test_granicus_body_parses_varied_title_formats():
    # Denton: "<body> on <datetime> - <date>"
    assert granicus_body("City Council on 2026-05-19 4:00 PM - May 19, 2026") == "City Council"
    # Fort Worth / Arlington: "<body> - <date>"
    assert granicus_body("Board of Adjustment - May 20, 2026") == "Board of Adjustment"
    assert (
        granicus_body("Planning and Zoning Commission - Regular Session - May 13, 2026")
        == "Planning and Zoning Commission - Regular Session"
    )
    # Pflugerville: trailing embedded date "<body> m-d-y - <date>"
    assert (
        granicus_body("City Council Worksession 5-26-26 - May 26, 2026")
        == "City Council Worksession"
    )
    # "on" inside a body name must NOT be treated as the Denton datetime split.
    assert granicus_body("Commission on Disabilities - May 1, 2026") == "Commission on Disabilities"


def test_canonical_body_merges_variants():
    # Prefix time-of-day qualifier — Arlington Evening/Afternoon -> one "Council".
    assert canonical_body("Evening Council") == "Council"
    assert canonical_body("Afternoon Council") == "Council"
    # Leading occurrence qualifiers.
    assert canonical_body("Special Called City Council") == "City Council"
    # Trailing subtype after a dash/colon.
    assert canonical_body("Planning and Zoning Commission - Regular Session") == (
        "Planning and Zoning Commission"
    )
    assert canonical_body("Board of Adjustments: Panel A") == "Board of Adjustments"


def test_canonical_body_keeps_distinct_meeting_types_and_bodies():
    # Distinct meeting TYPES are kept separate (not stripped).
    assert canonical_body("City Council Worksession") == "City Council Worksession"
    assert canonical_body("Council Briefing") == "Council Briefing"
    assert canonical_body("City Council Agenda Meetings") == "City Council Agenda Meetings"
    # Body-type words are never stripped; distinct bodies stay distinct.
    assert canonical_body("Animal Advisory Commission") == "Animal Advisory Commission"
    assert canonical_body("Senior Affairs Commission") == "Senior Affairs Commission"


def test_canonical_body_titlecases_all_caps():
    assert canonical_body("ZONING COMMISSION") == "Zoning Commission"
    assert canonical_body("CITY COUNCIL on 2026-05-19 4:00 PM") == "City Council"
    # Mixed/proper casing is preserved (not forced to title-case).
    assert canonical_body("Fort Worth Housing Finance Corporation") == (
        "Fort Worth Housing Finance Corporation"
    )


def test_body_key_merges_spelling_variants():
    assert body_key("Historic & Cultural Landmarks Commission") == body_key(
        "Historic and Cultural Landmark Commission"
    )
    assert body_key("ZONING COMMISSION") == body_key("Zoning Commission")


def test_matches_is_variant_tolerant():
    assert matches("City Council Agenda Meetings", "city council")
    assert not matches("Board of Adjustments", "council")
    assert not matches(None, "council")
    # &/plural variant still matches (one feed captures all spellings across views).
    assert matches(
        "Historic and Cultural Landmark Commission on 2026-05-01",
        "Historic & Cultural Landmarks Commission",
    )


def test_matches_supports_normalized_globs_for_provider_label_families():
    assert matches("Purchasing Bids 7-16-2015", "Purchasing Bids *")
    assert matches("Purchasing Bids_4-4-2013", "Purchasing Bids *")
    assert not matches("Purchasing Committee", "Purchasing Bids *")


def test_filter_by_body():
    eps = [_ep("City Council"), _ep("Planning and Zoning"), _ep("City Council Briefing")]
    assert [e.body for e in filter_by_body(eps, "City Council")] == [
        "City Council",
        "City Council Briefing",
    ]
    assert filter_by_body(eps, None) == eps  # no filter -> unchanged


def test_source_body_filter_supports_explicit_alternatives_without_broadening():
    eps = [
        _ep("City Council Agenda Meetings"),
        _ep("Special Called City Council Meeting"),
        _ep("Ad Hoc City Council Canvassing Committee"),
        _ep("Council Briefing"),
    ]
    selector = source_body_filter(
        {
            "body": "City Council Agenda Meetings",
            "body_any": ["Special Called City Council Meeting"],
        }
    )

    assert [e.body for e in filter_by_body(eps, selector)] == [
        "City Council Agenda Meetings",
        "Special Called City Council Meeting",
    ]


def test_source_body_filter_rejects_malformed_alternatives():
    with pytest.raises(ValueError, match="body_any"):
        source_body_filter({"body": "City Council", "body_any": "Special Called"})


def test_source_body_filter_rejects_empty_alternatives():
    with pytest.raises(ValueError, match="body_any"):
        source_body_filter({"body_any": []})


def test_body_inclusions_match_only_the_exact_provider_guid():
    inclusions = source_body_inclusions(
        {
            "body": "City Council",
            "body_includes": [
                {
                    "provider_guid": "https://example/MediaPlayer.php?view_id=5&clip_id=6331",
                    "body": "Work Session",
                }
            ],
        }
    )
    eps = [
        Episode(
            guid="https://example/MediaPlayer.php?view_id=5&clip_id=6331",
            title="Work Session",
            published=datetime(2026, 5, 1, tzinfo=UTC),
            video_url="x",
            body="Work Session",
        ),
        Episode(
            guid="https://example/MediaPlayer.php?view_id=5&clip_id=6332",
            title="Work Session",
            published=datetime(2026, 5, 1, tzinfo=UTC),
            video_url="x",
            body="Work Session",
        ),
        Episode(
            guid="other",
            title="Planning and Zoning Commission Work Session",
            published=datetime(2026, 5, 1, tzinfo=UTC),
            video_url="x",
            body="Planning and Zoning Commission Work Session",
        ),
    ]

    assert [e.guid for e in filter_by_body(eps, "City Council", inclusions)] == [
        "https://example/MediaPlayer.php?view_id=5&clip_id=6331"
    ]
    assert record_matches_body(
        {"provider_guid": "https://example/MediaPlayer.php?view_id=5&clip_id=6331"},
        "City Council",
        inclusions,
    )
    assert matches_exact_body_label("Work Session Work Session", "Work Session")
    assert not matches_exact_body_label(
        "Planning and Zoning Commission Work Session", "Work Session"
    )


def test_body_inclusions_validate_entries():
    with pytest.raises(ValueError, match="body_includes"):
        source_body_inclusions({"body_includes": [{"provider_guid": "x"}]})
    with pytest.raises(ValueError, match="duplicate"):
        source_body_inclusions(
            {
                "body_includes": [
                    {"provider_guid": "x", "body": "Work Session"},
                    {"provider_guid": "x", "body": "Work Session"},
                ]
            }
        )
