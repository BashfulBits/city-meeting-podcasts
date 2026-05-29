"""Unit tests for the Granicus parser."""

from __future__ import annotations

import pytest

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


@pytest.mark.parametrize("slug", recorded_slugs())
def test_recorded_fixtures_parse(slug):
    """Every committed fixture parses into a non-empty, well-formed episode list."""
    eps = parse_feed(fixture_bytes("granicus", slug))
    assert eps, f"{slug}: no episodes parsed"
    for e in eps:
        assert e.guid and e.title and e.video_url
        assert e.published is not None
