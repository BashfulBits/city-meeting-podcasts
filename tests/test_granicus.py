"""Unit tests for the Granicus parser."""

from __future__ import annotations

import pytest
import requests

from citypods.providers.base import ProviderError
from citypods.providers.granicus import GranicusProvider, parse_feed
from tests.conftest import fixture_bytes, recorded_slugs

SAMPLE = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:media="http://search.yahoo.com/mrss/">
<channel><title>Test</title>
 <item><title>Council A</title><pubDate>Tue, 20 May 2025 10:00:00 GMT</pubDate>
   <guid>clip-1</guid><description>desc</description>
   <enclosure url="https://x/a.mp4" type="video/mp4" length="0"/>
   <itunes:duration>1:02:03</itunes:duration></item>
 <item><title>Council B</title><pubDate>Tue, 13 May 2025 10:00:00 GMT</pubDate>
   <guid>clip-2</guid>
   <media:content url="https://x/b.mp4" type="video/mp4"/></item>
 <item><title>NoMedia</title><pubDate>Tue, 06 May 2025 10:00:00 GMT</pubDate></item>
 <item><title>NoDate</title><guid>clip-3</guid>
   <enclosure url="https://x/c.mp4" type="video/mp4"/></item>
</channel></rss>"""


def test_parses_enclosure_and_media_content():
    eps = parse_feed(SAMPLE)
    # NoMedia (no url) and NoDate (unorderable) are dropped.
    assert [e.title for e in eps] == ["Council A", "Council B"]
    assert eps[0].video_url == "https://x/a.mp4"
    assert eps[1].video_url == "https://x/b.mp4"  # media:content fallback


def test_duration_parsing():
    eps = parse_feed(SAMPLE)
    assert eps[0].duration == 3723  # 1:02:03
    assert eps[1].duration is None


def test_invalid_xml_raises():
    with pytest.raises(ProviderError):
        parse_feed(b"<rss><channel>")


def test_no_channel_raises():
    with pytest.raises(ProviderError):
        parse_feed(b"<rss></rss>")


def test_validate_requires_feed_url():
    provider = GranicusProvider()
    with pytest.raises(ValueError):
        provider.validate({})
    provider.validate({"feed_url": "https://x"})  # no raise
    provider.validate({"feed_urls": ["https://a", "https://b"]})  # multi-view also valid


def test_feed_urls_normalizes_single_and_multi():
    from citypods.providers.granicus import _feed_urls

    assert _feed_urls({"feed_url": "https://a"}) == ["https://a"]
    assert _feed_urls({"feed_urls": ["https://a", "https://b"]}) == ["https://a", "https://b"]
    assert _feed_urls({}) == []


def test_fetch_episodes_wraps_network_errors(monkeypatch):
    class TimeoutSession:
        def get(self, url, timeout=None):
            raise requests.ConnectTimeout("timed out")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("citypods.providers.granicus.make_session", TimeoutSession)

    with pytest.raises(ProviderError, match=r"GET https://city.example/rss failed: timed out"):
        GranicusProvider().fetch_episodes({"feed_url": "https://city.example/rss"})


@pytest.mark.parametrize("slug", recorded_slugs())
def test_recorded_fixtures_parse(slug):
    """Every committed fixture parses into a non-empty, well-formed episode list."""
    eps = parse_feed(fixture_bytes("granicus", slug))
    assert eps, f"{slug}: no episodes parsed"
    for e in eps:
        assert e.guid and e.title and e.video_url
        assert e.published is not None


def test_synthesizes_agenda_and_canonical_links():
    rss = b"""<rss><channel><item>
      <title>City Council - Regular</title>
      <link>https://city.granicus.com/MediaPlayer.php?view_id=2&amp;clip_id=99</link>
      <pubDate>Tue, 19 May 2026 18:30:00 GMT</pubDate>
      <enclosure url="https://city.granicus.com/DownloadFile.php?view_id=2&amp;clip_id=99"/>
    </item></channel></rss>"""
    ep = parse_feed(rss)[0]
    assert ep.links["canonical_video"] == (
        "https://city.granicus.com/MediaPlayer.php?view_id=2&clip_id=99"
    )
    assert ep.links["agenda"] == ("https://city.granicus.com/AgendaViewer.php?view_id=2&clip_id=99")


def test_agenda_link_derived_from_enclosure_when_no_link():
    # No <link>; view_id/clip_id still recoverable from the DownloadFile enclosure.
    rss = b"""<rss><channel><item>
      <title>Council</title>
      <pubDate>Tue, 19 May 2026 18:30:00 GMT</pubDate>
      <enclosure url="https://city.granicus.com/DownloadFile.php?view_id=7&amp;clip_id=12"/>
    </item></channel></rss>"""
    ep = parse_feed(rss)[0]
    assert "canonical_video" not in ep.links
    assert ep.links["agenda"].endswith("AgendaViewer.php?view_id=7&clip_id=12")


def test_parse_index_json_keeps_agenda_level_only():
    from citypods.providers.granicus import parse_index_json

    payload = b"""[[
      {"time":"3","type":"meta","text":"Rollcall:1","title":"Roll Call"},
      {"time":"970","type":"meta","text":"Agenda:2","title":"I) CALL TO ORDER"},
      {"time":"2170","type":"meta","text":"Agenda:3","title":"7.1. May 5<br />Afternoon"},
      {"time":"2092","type":"meta","text":"Motion","title":"Motion to Approve"}
    ]]"""
    chapters = parse_index_json(payload)
    assert [c["start"] for c in chapters] == [970, 2170]  # roll call + motion excluded, sorted
    assert chapters[1]["title"] == "7.1. May 5 Afternoon"  # <br/> stripped, whitespace collapsed


def test_resolve_media_url_constructs_archive_url():
    """UUID guid produces a direct archive-video.granicus.com URL, bypassing DownloadFile.php."""
    rss = b"""<rss><channel><item>
      <title>Council</title>
      <pubDate>Tue, 19 May 2026 18:30:00 GMT</pubDate>
      <guid>ec2832a9-95ac-4edc-9480-78dfba51d6c1</guid>
      <enclosure url="https://arlingtontx.granicus.com/DownloadFile.php?view_id=2&amp;clip_id=5310"/>
    </item></channel></rss>"""
    ep = parse_feed(rss)[0]
    source = {"feed_url": "https://arlingtontx.granicus.com/ViewPublisherRSS.php?view_id=2"}
    url = GranicusProvider().resolve_media_url(ep, source)
    assert url == (
        "https://archive-video.granicus.com"
        "/arlingtontx/arlingtontx_ec2832a9-95ac-4edc-9480-78dfba51d6c1.mp4"
    )


def test_resolve_media_url_falls_back_for_non_uuid_guid():
    """Non-UUID guid falls back to episode.video_url (the DownloadFile.php enclosure)."""
    rss = b"""<rss><channel><item>
      <title>Council</title>
      <pubDate>Tue, 19 May 2026 18:30:00 GMT</pubDate>
      <guid>clip-99</guid>
      <enclosure url="https://arlingtontx.granicus.com/DownloadFile.php?view_id=2&amp;clip_id=99"/>
    </item></channel></rss>"""
    ep = parse_feed(rss)[0]
    source = {"feed_url": "https://arlingtontx.granicus.com/ViewPublisherRSS.php?view_id=2"}
    url = GranicusProvider().resolve_media_url(ep, source)
    assert url == "https://arlingtontx.granicus.com/DownloadFile.php?view_id=2&clip_id=99"


def test_granicus_subdomain_helper():
    from citypods.providers.granicus import _granicus_subdomain

    assert _granicus_subdomain({"feed_url": "https://arlingtontx.granicus.com/rss"}) == (
        "arlingtontx"
    )
    assert _granicus_subdomain({"feed_url": "https://fortworthgov.granicus.com/rss"}) == (
        "fortworthgov"
    )
    assert (
        _granicus_subdomain({"feed_urls": ["https://pflugerville.granicus.com/rss"]})
        == "pflugerville"
    )
    assert _granicus_subdomain({}) is None


def test_fetch_chapters_uses_clip_id(monkeypatch):
    import citypods.providers.granicus as g

    ep = parse_feed(
        b"""<rss><channel><item><title>Council</title>
        <pubDate>Tue, 19 May 2026 18:30:00 GMT</pubDate>
        <enclosure url="https://c.granicus.com/DownloadFile.php?view_id=2&amp;clip_id=99"/>
        </item></channel></rss>"""
    )[0]

    class Resp:
        status_code = 200
        content = b'[[{"time":"10","type":"meta","text":"Agenda:1","title":"Item"}]]'

    class Sess:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, timeout=None):
            assert url == "https://c.granicus.com/JSON.php?clip_id=99"
            return Resp()

    monkeypatch.setattr(g, "make_session", lambda: Sess())
    chapters, transcript = g.GranicusProvider().fetch_chapters(ep, {})
    assert chapters == [{"start": 10, "title": "Item"}] and transcript is None
