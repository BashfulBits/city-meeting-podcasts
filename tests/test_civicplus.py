"""Unit tests for the CivicPlus / CivicMedia adapter."""

from __future__ import annotations

import pytest
import requests

from citypods.providers.base import ProviderError
from citypods.providers.civicplus import CivicPlusProvider, parse_civicmedia_feed
from tests.conftest import fixture_bytes

SAMPLE = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<title>Media Center - City Council</title>
<item><title>May 19 Meeting</title>
  <link>https://city.example/CivicMedia?VID=285</link>
  <pubDate>Fri, 22 May 2026 09:41:00 -0600</pubDate>
  <guid isPermaLink="false">https://city.example/CivicMedia?VID=285/123</guid></item>
<item><title>No link item</title>
  <pubDate>Fri, 22 May 2026 09:41:00 -0600</pubDate></item>
</channel></rss>"""

WATCH_PAGE = """
<html><body>
<script>var p = "https://civplus.tikiliveapi.com/embed?scheme=embedVod&amp;videoId=159920&amp;autoplay=yes";</script>
<img src="https://civplus.tikiliveapi.com/public/files/videos/159/159920/159920-640x360.jpg">
</body></html>
"""

EMBED_PAGE = """
<html><script>
source: "https://wms.civplus.tikiliveapi.com/vodhttporigin/159920/playlist.m3u8?token=abc&amp;ip=1.2.3.4"
</script></html>
"""


class FakeResponse:
    def __init__(self, text, status=200):
        self.text = text
        self.content = text.encode()
        self.status_code = status


class FakeSession:
    """Maps URL substrings to canned responses; supports the context-manager API."""

    def __init__(self, routes):
        self.routes = routes
        self.requested = []

    def get(self, url, timeout=None):
        self.requested.append(url)
        for needle, resp in self.routes.items():
            if needle in url:
                return resp
        raise AssertionError(f"unexpected GET {url}")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_parse_feed_marks_hls_and_keeps_watch_link():
    eps = parse_civicmedia_feed(SAMPLE)
    assert len(eps) == 1  # the item with no <link> is dropped
    ep = eps[0]
    assert ep.media_kind == "hls"
    assert ep.video_url == "https://city.example/CivicMedia?VID=285"
    assert ep.needs_materialization()


def test_validate_requires_feed_url():
    with pytest.raises(ValueError):
        CivicPlusProvider().validate({})
    CivicPlusProvider().validate({"feed_url": "https://x"})


def test_fetch_episodes_wraps_network_errors(monkeypatch):
    class TimeoutSession:
        def get(self, url, timeout=None):
            raise requests.ConnectTimeout("timed out")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("citypods.providers.civicplus.make_session", TimeoutSession)

    with pytest.raises(ProviderError, match=r"GET https://city.example/rss failed: timed out"):
        CivicPlusProvider().fetch_episodes({"feed_url": "https://city.example/rss"})


def test_resolve_media_url_walks_page_then_embed(monkeypatch):
    provider = CivicPlusProvider()
    fake = FakeSession(
        {
            "CivicMedia?VID=285": FakeResponse(WATCH_PAGE),
            "/embed?": FakeResponse(EMBED_PAGE),
        }
    )
    monkeypatch.setattr("citypods.providers.civicplus.make_session", lambda: fake)

    eps = parse_civicmedia_feed(SAMPLE)
    url = provider.resolve_media_url(eps[0], {"feed_url": "x"})
    assert url.endswith("playlist.m3u8?token=abc&ip=1.2.3.4")  # &amp; decoded
    # It fetched the watch page first, then the embed.
    assert "VID=285" in fake.requested[0] and "/embed?" in fake.requested[1]


def test_resolve_raises_when_no_embed(monkeypatch):
    provider = CivicPlusProvider()
    fake = FakeSession({"CivicMedia?VID=285": FakeResponse("<html>no player</html>")})
    monkeypatch.setattr("citypods.providers.civicplus.make_session", lambda: fake)
    eps = parse_civicmedia_feed(SAMPLE)
    with pytest.raises(ProviderError, match="embed"):
        provider.resolve_media_url(eps[0], {"feed_url": "x"})


def test_recorded_fixture_parses():
    eps = parse_civicmedia_feed(fixture_bytes("civicplus", "gainesville-tx"))
    assert eps
    assert all(e.media_kind == "hls" and e.published is not None for e in eps)
