"""Offline tests for the Legistar calendar index provider."""

from __future__ import annotations

from pathlib import Path

import pytest

from citypods.providers.legistar import (
    LegistarProvider,
    _extract_aspnet_fields,
    _iter_year_pages,
    _parse_calendar_page,
    _parse_calendar_records_page,
)

FIXTURES = Path(__file__).parent / "fixtures"
SOURCE = {
    "calendar_url": "https://pflugerville.legistar.com/Calendar.aspx",
    "granicus_base": "https://pflugerville.granicus.com",
    "view_id": 1,
    "backfill_since": "2024-01-01",
}


class _Response:
    status_code = 200

    def __init__(self, text: str):
        self.text = text


class _Session:
    def __init__(self, pages: list[str]):
        self.pages = iter(pages)
        self.posts: list[dict] = []
        self.post_urls: list[str] = []

    def get(self, *args, **kwargs):
        return _Response(next(self.pages))

    def post(self, *args, **kwargs):
        self.post_urls.append(args[0])
        self.posts.append(kwargs["data"])
        return _Response(next(self.pages))


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_parse_single_page_and_fallback_view_id():
    episodes = _parse_calendar_page(_fixture("legistar_calendar_2024_p1.html"), SOURCE)

    assert len(episodes) == 1  # agenda-only Library Board row is not an episode
    episode = episodes[0]
    assert episode.guid.endswith("view_id=1&clip_id=123")
    assert episode.published.hour == 18
    assert episode.links["agenda"] == ("https://pflugerville.legistar.com/View.ashx?M=A&ID=1")


def test_parse_calendar_records_retains_rows_without_video_as_metadata():
    records = _parse_calendar_records_page(_fixture("legistar_calendar_2024_p1.html"), SOURCE)

    assert len(records) == 2
    recorded, agenda_only = records
    assert recorded.video_guid and recorded.video_url
    assert recorded.links["agenda_portal"].endswith("view_id=1&clip_id=123")
    assert agenda_only.body == "Library Board"
    assert agenda_only.video_guid is None
    assert agenda_only.video_url is None
    assert agenda_only.links["agenda"] == "https://pflugerville.legistar.com/View.ashx?M=A&ID=2"


def test_body_filter_and_row_view_id_precedence():
    source = {**SOURCE, "body": "City Council", "view_id": 1}
    episodes = _parse_calendar_page(_fixture("legistar_calendar_2024_p2.html"), source)

    assert [episode.body for episode in episodes] == ["City Council"]
    assert episodes[0].guid.endswith("view_id=10&clip_id=122")


def test_pagination_posts_fresh_aspnet_state_and_terminates():
    first = _fixture("legistar_calendar_2024_p1.html")
    second = _fixture("legistar_calendar_2024_p2.html")
    session = _Session([first, second])

    assert list(_iter_year_pages(session, SOURCE["calendar_url"], 2024)) == [first, second]
    assert session.posts == [
        {
            "__VIEWSTATE": "state-one",
            "__VIEWSTATEGENERATOR": "generator",
            "__EVENTVALIDATION": "validation",
            "__EVENTTARGET": "pager-target",
        }
    ]
    assert session.post_urls == ["https://pflugerville.legistar.com/Calendar.aspx?Mode=2024"]


def test_state_extraction_requires_all_webforms_fields():
    fields = _extract_aspnet_fields(_fixture("legistar_calendar_2024_p1.html"))
    assert fields["__VIEWSTATE"] == "state-one"
    with pytest.raises(Exception, match="ASP.NET fields"):
        _extract_aspnet_fields('<input id="__VIEWSTATE" value="x" />')


def test_provider_contract_and_validation():
    provider = LegistarProvider()
    provider.validate(SOURCE)
    assert provider.detect_change(SOURCE) is None
    assert provider.fetch_view_counts(SOURCE) == []
    assert provider.video_deeplink("https://x.granicus.com/MediaPlayer.php?clip_id=1", 90).endswith(
        "starttime=90"
    )
    with pytest.raises(ValueError, match="calendar_url"):
        provider.validate(
            {"granicus_base": "https://x.granicus.com", "backfill_since": "2024-01-01"}
        )
