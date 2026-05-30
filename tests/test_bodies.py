"""Tests for generic per-body extraction/filtering and the global budget."""

from __future__ import annotations

from datetime import UTC, datetime

from citypods.bodies import filter_by_body, granicus_body, matches
from citypods.media import GlobalBudget
from citypods.models import Episode


def _ep(body):
    return Episode(
        guid=body,
        title=body,
        published=datetime(2026, 5, 1, tzinfo=UTC),
        video_url="x",
        body=body,
    )


def test_granicus_body_parses_title():
    assert granicus_body("City Council on 2026-05-19 4:00 PM - May 19") == "City Council"
    assert (
        granicus_body("Planning and Zoning Commission on 2026-05-27 5:00 PM")
        == "Planning and Zoning Commission"
    )
    assert granicus_body("Some PSA video") is None


def test_matches_is_case_insensitive_substring():
    assert matches("City Council Agenda Meetings", "city council")
    assert not matches("Board of Adjustments", "council")
    assert not matches(None, "council")


def test_filter_by_body():
    eps = [_ep("City Council"), _ep("Planning and Zoning"), _ep("City Council Briefing")]
    assert [e.body for e in filter_by_body(eps, "City Council")] == [
        "City Council",
        "City Council Briefing",
    ]
    assert filter_by_body(eps, None) == eps  # no filter -> unchanged


def test_global_budget_caps_total():
    b = GlobalBudget(2)
    assert b.take() and b.take()
    assert not b.take()
    assert GlobalBudget(0).take() is False
