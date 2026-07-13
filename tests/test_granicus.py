"""Unit tests for the Granicus parser."""

from __future__ import annotations

import pytest
import requests

from citypods.providers.base import ProviderError
from citypods.providers.granicus import (
    GranicusProvider,
    _archive_urls,
    parse_archive_page,
    parse_feed,
)
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
    provider.validate({"feed_url": "https://x.granicus.com/ViewPublisherRSS.php?view_id=2"})
    provider.validate(
        {
            "feed_urls": [
                "https://a.granicus.com/ViewPublisherRSS.php?view_id=2",
                "https://b.granicus.com/ViewPublisherRSS.php?view_id=3",
            ]
        }
    )


def test_feed_urls_normalizes_single_and_multi():
    from citypods.providers.granicus import _feed_urls

    assert _feed_urls({"feed_url": "https://a"}) == ["https://a"]
    assert _feed_urls({"feed_urls": ["https://a", "https://b"]}) == ["https://a", "https://b"]
    assert _feed_urls({}) == []


def test_archive_urls_are_derived_without_changing_config_identity():
    assert _archive_urls(
        {"feed_url": "https://city.granicus.com/ViewPublisherRSS.php?view_id=9&mode=vpodcast"}
    ) == ["https://city.granicus.com/ViewPublisher.php?view_id=9"]
    assert _archive_urls(
        {
            "feed_url": "https://city.granicus.com/ViewPublisherRSS.php?view_id=9",
            "archive_url": "https://city.granicus.com/ViewPublisher.php?view_id=10",
        }
    ) == ["https://city.granicus.com/ViewPublisher.php?view_id=10"]


def test_parse_native_archive_page_retains_official_document_links():
    content = fixture_bytes("granicus", "archive")
    episodes = parse_archive_page(content, "https://city.granicus.com/ViewPublisher.php?view_id=9")

    assert len(episodes) == 1  # no Video link means a meeting, not a podcast episode
    episode = episodes[0]
    assert episode.guid == "https://city.granicus.com/MediaPlayer.php?view_id=9&clip_id=701"
    assert episode.video_url == "https://city.granicus.com/DownloadFile.php?view_id=9&clip_id=701"
    assert episode.published.isoformat() == "2026-07-07T00:00:00+00:00"
    assert episode.duration == 8100
    assert episode.body == "City Council - Regular Meeting"
    assert episode.links == {
        "canonical_video": "https://city.granicus.com/MediaPlayer.php?view_id=9&clip_id=701",
        "agenda": "https://city.granicus.com/AgendaViewer.php?view_id=9&clip_id=701",
        "minutes": "https://city.granicus.com/MinutesViewer.php?view_id=9&clip_id=701&doc_id=abc",
    }


def test_fetch_episodes_wraps_network_errors(monkeypatch):
    class TimeoutSession:
        def get(self, url, timeout=None):
            raise requests.ConnectTimeout("timed out")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("citypods.providers.granicus.make_session", TimeoutSession)

    expected = r"GET https://city.example/ViewPublisher.php\?view_id=9 failed: timed out"
    with pytest.raises(ProviderError, match=expected):
        GranicusProvider().fetch_episodes(
            {"feed_url": "https://city.example/ViewPublisherRSS.php?view_id=9"}
        )


def test_fetch_episodes_uses_archive_not_capped_rss(monkeypatch):
    import citypods.providers.granicus as granicus

    content = fixture_bytes("granicus", "archive")
    calls = []

    class Response:
        status_code = 200

    Response.content = content

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, timeout=None):
            calls.append(url)
            return Response()

    monkeypatch.setattr(granicus, "make_session", Session)
    episodes = GranicusProvider().fetch_episodes(
        {"feed_url": "https://city.granicus.com/ViewPublisherRSS.php?view_id=9&mode=vpodcast"}
    )
    assert [episode.guid for episode in episodes] == [
        "https://city.granicus.com/MediaPlayer.php?view_id=9&clip_id=701"
    ]
    assert calls == ["https://city.granicus.com/ViewPublisher.php?view_id=9"]
    source = {"feed_url": "https://city.granicus.com/ViewPublisherRSS.php?view_id=9"}
    assert GranicusProvider().detect_change(source) is None
    assert GranicusProvider().fetch_view_counts(source) == []


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


def test_resolve_media_url_follows_downloadfile_redirect(monkeypatch):
    """resolve_media_url pre-follows the DownloadFile.php redirect and returns the signed URL."""
    rss = b"""<rss><channel><item>
      <title>Council</title>
      <pubDate>Tue, 19 May 2026 18:30:00 GMT</pubDate>
      <guid>ec2832a9-95ac-4edc-9480-78dfba51d6c1</guid>
      <enclosure url="https://arlingtontx.granicus.com/DownloadFile.php?view_id=2&amp;clip_id=5310"/>
    </item></channel></rss>"""
    ep = parse_feed(rss)[0]
    source = {"feed_url": "https://arlingtontx.granicus.com/ViewPublisherRSS.php?view_id=2"}

    signed = (
        "https://archive-video.granicus.com/arlingtontx/arlingtontx_ec2832a9.mp4"
        "?Expires=9999999999&Signature=FAKESIG&Key-Pair-Id=FAKEKID"
    )

    class _FakeResp:
        status_code = 302
        headers = {"Location": signed}

    class _FakeSession:
        def get(self, url, **kwargs):
            return _FakeResp()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    validated = []

    monkeypatch.setattr("citypods.providers.granicus.make_session", lambda: _FakeSession())
    monkeypatch.setattr(
        "citypods.providers.granicus.validate_source_url",
        lambda *a, **kw: validated.append((a, kw)),
    )
    url = GranicusProvider().resolve_media_url(ep, source)
    assert url == signed
    assert validated == [((signed,), {"resolve": True})]


def test_resolve_media_url_makes_a_single_request_no_bespoke_retry(monkeypatch):
    """The 403-as-rate-limit retry was lifted into the shared make_session adapter (#39), so
    resolve_media_url itself issues exactly ONE request. A 403 the mock returns here bypasses the
    adapter's internal retry, so it falls back to the bare URL rather than looping."""
    rss = b"""<rss><channel><item>
      <title>Council</title>
      <pubDate>Tue, 19 May 2026 18:30:00 GMT</pubDate>
      <guid>clip-99</guid>
      <enclosure url="https://arlingtontx.granicus.com/DownloadFile.php?view_id=2&amp;clip_id=99"/>
    </item></channel></rss>"""
    ep = parse_feed(rss)[0]
    source = {"feed_url": "https://arlingtontx.granicus.com/ViewPublisherRSS.php?view_id=2"}

    calls = []

    class _Resp403:
        status_code = 403
        headers: dict = {}

    class _FakeSession:
        def get(self, url, **kwargs):
            calls.append(url)
            return _Resp403()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    monkeypatch.setattr("citypods.providers.granicus.make_session", lambda: _FakeSession())
    url = GranicusProvider().resolve_media_url(ep, source)
    # No bespoke loop: a single call, and (mock-bypassed adapter) 403 falls back to the bare URL.
    assert len(calls) == 1
    assert url == "https://arlingtontx.granicus.com/DownloadFile.php?view_id=2&clip_id=99"


def test_resolve_media_url_falls_back_when_redirect_fails(monkeypatch):
    """If the DownloadFile.php request raises, the original URL is returned unchanged."""
    import requests as req

    rss = b"""<rss><channel><item>
      <title>Council</title>
      <pubDate>Tue, 19 May 2026 18:30:00 GMT</pubDate>
      <guid>clip-99</guid>
      <enclosure url="https://arlingtontx.granicus.com/DownloadFile.php?view_id=2&amp;clip_id=99"/>
    </item></channel></rss>"""
    ep = parse_feed(rss)[0]
    source = {"feed_url": "https://arlingtontx.granicus.com/ViewPublisherRSS.php?view_id=2"}

    class _FakeSession:
        def get(self, url, **kwargs):
            raise req.ConnectionError("timeout")

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    monkeypatch.setattr("citypods.providers.granicus.make_session", lambda: _FakeSession())
    url = GranicusProvider().resolve_media_url(ep, source)
    assert url == "https://arlingtontx.granicus.com/DownloadFile.php?view_id=2&clip_id=99"


def test_resolve_media_url_passthrough_for_non_downloadfile_url():
    """Non-DownloadFile.php URLs are returned unchanged without a network call."""
    rss = b"""<rss><channel><item>
      <title>Council</title>
      <pubDate>Tue, 19 May 2026 18:30:00 GMT</pubDate>
      <guid>some-guid</guid>
      <enclosure url="https://archive-video.granicus.com/arlingtontx/arlingtontx_abc.mp4"/>
    </item></channel></rss>"""
    ep = parse_feed(rss)[0]
    source = {"feed_url": "https://arlingtontx.granicus.com/ViewPublisherRSS.php?view_id=2"}
    url = GranicusProvider().resolve_media_url(ep, source)
    assert url == "https://archive-video.granicus.com/arlingtontx/arlingtontx_abc.mp4"


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
