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
