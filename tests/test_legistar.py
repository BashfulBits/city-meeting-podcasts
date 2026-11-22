"""Offline tests for the Legistar calendar index provider."""

from __future__ import annotations

from pathlib import Path

import pytest

from citypods.models import Episode
from citypods.providers.legistar import (
    LegistarProvider,
    _extract_aspnet_fields,
    _iter_year_pages,
    _parse_calendar_page,
    _parse_calendar_records_page,
)
from citypods.records import attach_auxiliary_agenda_links

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


def test_video_row_without_a_view_id_is_retained_as_calendar_metadata():
    records = _parse_calendar_records_page(
        _fixture("legistar_calendar_2024_p1.html"), {**SOURCE, "view_id": None}
    )

    assert len(records) == 2
    assert records[0].video_guid is None
    assert records[0].video_url is None
    assert records[0].links["agenda"] == "https://pflugerville.legistar.com/View.ashx?M=A&ID=1"


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


def test_validation_allows_omitting_granicus_base():
    provider = LegistarProvider()
    source = {
        "calendar_url": "https://cityofdallas.legistar.com/Calendar.aspx",
        "backfill_since": "2020-01-01",
    }
    provider.validate(source)
    with pytest.raises(ValueError, match="granicus_base"):
        provider.validate({**source, "granicus_base": "http://insecure.granicus.com"})


def test_parse_calendar_records_with_swagit_video_link():
    html = """
    <tr class="rgRow" id="ctl00_ContentPlaceHolder1_gridCalendar_ctl00__0">
        <td>
            <a id="ctl00_ContentPlaceHolder1_gridCalendar_ctl00_ctl04_hypBody">City Council</a>
        </td><td class="rgSorted">11/19/2026</td><td>
        </td><td>
            <span id="ctl00_ContentPlaceHolder1_gridCalendar_ctl00_ctl04_lblTime">9:00 AM</span>
        </td><td>
            <a id="ctl00_ContentPlaceHolder1_gridCalendar_ctl00_ctl04_hypAgenda"
               href="View.ashx?M=A&amp;ID=100">Agenda</a>
        </td><td>
            <a id="ctl00_ContentPlaceHolder1_gridCalendar_ctl00_ctl04_hypVideo" class="videolink"
               href="https://dallastx.new.swagit.com/videos/410396?place=holder&amp;Mode2=Video">Video</a>
        </td>
    </tr>
    """
    source = {
        "calendar_url": "https://cityofdallas.legistar.com/Calendar.aspx",
        "backfill_since": "2020-01-01",
    }
    records = _parse_calendar_records_page(html, source)
    assert len(records) == 1
    record = records[0]
    assert record.body == "City Council"
    assert record.video_guid == "410396"
    assert record.video_url is None
    assert record.links["agenda"] == "https://cityofdallas.legistar.com/View.ashx?M=A&ID=100"
    assert record.links["canonical_video"] == "https://dallastx.new.swagit.com/videos/410396"

    # Verify integration with attach_auxiliary_agenda_links
    episode = Episode(
        guid="410396",
        title="City Council – November 19, 2026",
        published=record.published,
        video_url="https://dallastx.new.swagit.com/videos/410396/download",
        body="City Council",
        links={"canonical_video": "https://dallastx.new.swagit.com/videos/410396"},
    )
    attach_auxiliary_agenda_links([episode], [record])
    assert episode.links["agenda"] == "https://cityofdallas.legistar.com/View.ashx?M=A&ID=100"


def test_parse_calendar_records_with_swagit_event_link():
    html = """
    <tr class="rgRow" id="ctl00_ContentPlaceHolder1_gridCalendar_ctl00__0">
        <td>
            <a id="ctl00_ContentPlaceHolder1_gridCalendar_ctl00_ctl04_hypBody">City Council</a>
        </td><td class="rgSorted">11/25/2026</td><td>
        </td><td>
            <span id="ctl00_ContentPlaceHolder1_gridCalendar_ctl00_ctl04_lblTime">9:00 AM</span>
        </td><td>
            <a id="ctl00_ContentPlaceHolder1_gridCalendar_ctl00_ctl04_hypAgenda"
               href="View.ashx?M=A&amp;ID=200">Agenda</a>
        </td><td>
            <a id="ctl00_ContentPlaceHolder1_gridCalendar_ctl00_ctl04_hypVideo" class="videolink"
               href="https://dallastx.new.swagit.com/events/50601?Mode2=Video">Video</a>
        </td>
    </tr>
    """
    source = {
        "calendar_url": "https://cityofdallas.legistar.com/Calendar.aspx",
        "backfill_since": "2020-01-01",
    }
    records = _parse_calendar_records_page(html, source)
    assert len(records) == 1
    record = records[0]
    assert record.body == "City Council"
    assert record.video_guid is None
    assert record.video_url is None
    assert (
        record.links["canonical_video"]
        == "https://dallastx.new.swagit.com/events/50601?Mode2=Video"
    )
    assert record.links["agenda"] == "https://cityofdallas.legistar.com/View.ashx?M=A&ID=200"

    # Matches by body and date even without video_guid
    episode = Episode(
        guid="some-diff-guid",
        title="City Council – November 25, 2026",
        published=record.published,
        video_url="https://dallastx.new.swagit.com/videos/something/download",
        body="City Council",
        links={},
    )
    attach_auxiliary_agenda_links([episode], [record])
    assert episode.links["agenda"] == "https://cityofdallas.legistar.com/View.ashx?M=A&ID=200"


def test_video_deeplink_swagit_delegation():
    provider = LegistarProvider()
    link = provider.video_deeplink("https://dallastx.new.swagit.com/videos/410396", 125)
    assert link == "https://dallastx.new.swagit.com/play/410396/125"
    # An unrelated host with swagit.com in path/query falls back to Granicus
    non_swagit = provider.video_deeplink(
        "https://not-swagit.com/swagit.com/MediaPlayer.php?clip_id=1", 60
    )
    assert non_swagit == "https://not-swagit.com/swagit.com/MediaPlayer.php?clip_id=1&starttime=60"


def test_parse_calendar_records_ignores_swagit_url_in_query_param():
    html = """
    <tr class="rgRow" id="ctl00_ContentPlaceHolder1_gridCalendar_ctl00__0">
        <td>
            <a id="ctl00_ContentPlaceHolder1_gridCalendar_ctl00_ctl04_hypBody">City Council</a>
        </td><td class="rgSorted">11/25/2026</td><td>
        </td><td>
            <span id="ctl00_ContentPlaceHolder1_gridCalendar_ctl00_ctl04_lblTime">9:00 AM</span>
        </td><td>
            <a id="ctl00_ContentPlaceHolder1_gridCalendar_ctl00_ctl04_hypVideo" class="videolink"
               href="https://example.gov/Video.aspx?url=https://dallastx.new.swagit.com/videos/410396">Video</a>
        </td>
    </tr>
    """
    source = {
        "calendar_url": "https://cityofdallas.legistar.com/Calendar.aspx",
        "backfill_since": "2020-01-01",
    }
    records = _parse_calendar_records_page(html, source)
    assert len(records) == 1
    record = records[0]
    assert record.video_guid is None
    assert record.links["canonical_video"] == (
        "https://example.gov/Video.aspx?url=https://dallastx.new.swagit.com/videos/410396"
    )
