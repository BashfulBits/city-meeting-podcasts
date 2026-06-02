"""Unit tests for RSS feed building."""

from __future__ import annotations

from xml.etree import ElementTree as ET

import pytest

from citypods.feeds import build_rss


def _items(xml: str):
    return ET.fromstring(xml).find("channel").findall("item")


def test_audio_uses_audio_mime(sample_city, sample_episodes):
    xml = build_rss(sample_city, sample_episodes, "audio", "https://x")
    enc = _items(xml)[0].find("enclosure")
    assert enc.get("type") == "audio/mp4"


def test_video_uses_video_mime(sample_city, sample_episodes):
    xml = build_rss(sample_city, sample_episodes, "video", "https://x")
    enc = _items(xml)[0].find("enclosure")
    assert enc.get("type") == "video/mp4"


def test_enclosure_length_zero(sample_city, sample_episodes):
    xml = build_rss(sample_city, sample_episodes, "video", "https://x")
    assert _items(xml)[0].find("enclosure").get("length") == "0"


def test_episodes_ordered_newest_first(sample_city, sample_episodes):
    # Pass them in reverse; output must be sorted by published desc.
    xml = build_rss(sample_city, list(reversed(sample_episodes)), "video", "https://x")
    titles = [i.find("title").text for i in _items(xml)]
    assert titles[0].startswith("Regular")


def test_truncates_to_max_episodes(sample_city, sample_episodes):
    sample_city.max_episodes = 1
    xml = build_rss(sample_city, sample_episodes, "video", "https://x")
    assert len(_items(xml)) == 1


def test_xml_escaping(sample_city, sample_episodes):
    xml = build_rss(sample_city, sample_episodes, "video", "https://x")
    assert "&lt;Meeting&gt;" in xml and "&amp;" in xml
    # And it still parses.
    ET.fromstring(xml)


def test_invalid_kind_raises(sample_city, sample_episodes):
    with pytest.raises(ValueError):
        build_rss(sample_city, sample_episodes, "transcript", "https://x")


def test_episode_notes_html_renders_links_and_summary():
    from datetime import UTC, datetime

    from citypods.feeds import episode_notes_html
    from citypods.models import Episode

    ep = Episode(
        guid="g",
        title="t",
        published=datetime(2026, 1, 1, tzinfo=UTC),
        video_url="v",
        summary="A short recap.",
        links={"canonical_video": "https://watch", "agenda": "https://agenda.pdf"},
    )
    html = episode_notes_html(ep)
    assert "<p>A short recap.</p>" in html
    # agenda is ordered before canonical_video per LINK_LABELS
    assert html.index("Agenda") < html.index("Watch the video")
    assert '<a href="https://agenda.pdf">Agenda</a>' in html


def test_episode_notes_html_empty_when_no_enrichment():
    from datetime import UTC, datetime

    from citypods.feeds import episode_notes_html
    from citypods.models import Episode

    ep = Episode(
        guid="g",
        title="t",
        published=datetime(2026, 1, 1, tzinfo=UTC),
        video_url="v",
        description="plain",
    )
    assert episode_notes_html(ep) == ""


def test_chapters_json_and_podcast_chapters_tag(tmp_path):
    from datetime import UTC, datetime

    from citypods.feeds import build_rss, chapters_json, chapters_url
    from citypods.models import City, Episode

    city = City(
        slug="x-tx",
        provider="granicus",
        source={"feed_url": "u"},
        podcast_title="X",
        podcast_author="A",
        podcast_email="",
        podcast_description="d",
    )
    ep = Episode(
        guid="g",
        uid="abc123",
        title="t",
        published=datetime(2026, 1, 1, tzinfo=UTC),
        video_url="https://v.mp4",
        media_kind="direct",
        chapters=[{"start": 60, "title": "Two"}, {"start": 5, "title": "One"}],
    )
    doc = chapters_json(ep)
    assert '"version": "1.2.0"' in doc
    assert doc.index('"One"') < doc.index('"Two"')  # sorted by startTime
    assert chapters_url(city, ep, "https://e.test") == "https://e.test/x-tx/chapters/abc123.json"

    xml = build_rss(city, [ep], "audio", "https://e.test")
    assert 'xmlns:podcast="https://podcastindex.org/namespace/1.0"' in xml
    assert (
        '<podcast:chapters url="https://e.test/x-tx/chapters/abc123.json" '
        'type="application/json+chapters"/>' in xml
    )


def test_meetings_link_renders_into_feed_end_to_end(tmp_path):
    """Issue #112: a city's meetings_url, injected by LinksStage, renders as a per-episode
    resource link in the feed notes. Guards the full path (stage -> ordered_links -> RSS) that
    the snapshot test doesn't exercise (it bypasses the enrichment stages)."""
    from datetime import UTC, datetime

    from citypods.feeds import build_rss
    from citypods.models import City, Episode
    from citypods.stages import LinksStage, StageContext
    from tests.test_stages import FakeFfmpeg

    city = City(
        slug="x-tx",
        provider="granicus",
        source={"feed_url": "u"},
        podcast_title="X",
        podcast_author="A",
        podcast_email="",
        podcast_description="d",
        meetings_url="https://x.gov/meetings",
    )
    ep = Episode(
        guid="g",
        uid="abc123",
        title="t",
        published=datetime(2026, 1, 1, tzinfo=UTC),
        video_url="https://v.mp4",
        media_kind="direct",
    )
    ctx = StageContext(storage=None, ffmpeg=FakeFfmpeg(), max_kbps=96, dry_run=False, stop=None)
    LinksStage().process(None, city, [ep], ctx)

    xml = build_rss(city, [ep], "audio", "https://e.test")
    assert "Official meetings page" in xml
    assert "https://x.gov/meetings" in xml
