"""Unit tests for RSS feed building."""

from __future__ import annotations

from datetime import UTC, datetime
from xml.etree import ElementTree as ET

import pytest

from citypods.availability import (
    AVAILABLE,
    CONFIRMED_EMPTY,
    CONFIRMED_PARTIAL,
    MediaAvailability,
    with_operator_override,
)
from citypods.feeds import build_rss, enclosure_duration, enclosure_url, ordered_links
from citypods.models import Episode


def _items(xml: str):
    return ET.fromstring(xml).find("channel").findall("item")


def test_ordered_links_drops_non_http_schemes():
    # M5/MR-TM-01: a provider RSS <link> is scraped, not maintainer-authored -- a compromised/
    # malformed upstream link must not reach an href attribute with only entity-escaping.
    links = {
        "agenda": "https://city.example/agenda.pdf",
        "canonical_video": "javascript:alert(1)",
        "minutes": "data:text/html,<script>alert(1)</script>",
        "transcript": "http://city.example/t.vtt",
    }
    result = ordered_links(links)
    urls = {url for _label, url in result}
    assert urls == {"https://city.example/agenda.pdf", "http://city.example/t.vtt"}


def test_ordered_links_drops_empty_and_none_values():
    result = ordered_links({"agenda": "", "minutes": None, "documents": "https://x/d.pdf"})
    assert result == [("Meeting documents", "https://x/d.pdf")]


def test_audio_uses_audio_mime(sample_city, sample_episodes):
    xml = build_rss(sample_city, sample_episodes, "audio", "https://x")
    enc = _items(xml)[0].find("enclosure")
    assert enc.get("type") == "audio/mp4"


def test_withheld_availability_omits_enclosure_from_both_kinds(sample_episodes):
    ep = sample_episodes[0]
    ep.media_availability = MediaAvailability(state=CONFIRMED_EMPTY)
    assert enclosure_url(ep, "audio") is None
    assert enclosure_url(ep, "video") is None


def test_available_verdict_still_yields_enclosure(sample_episodes):
    ep = sample_episodes[0]
    ep.media_availability = MediaAvailability(state=AVAILABLE)
    assert enclosure_url(ep, "video") == ep.video_url


def test_operator_override_to_available_re_enables_enclosure(sample_episodes):
    ep = sample_episodes[0]
    confirmed = MediaAvailability(state=CONFIRMED_EMPTY)
    ep.media_availability = with_operator_override(confirmed, AVAILABLE, "verified good")
    assert enclosure_url(ep, "video") == ep.video_url


def test_withheld_episode_dropped_from_built_feed(sample_city, sample_episodes):
    sample_episodes[0].media_availability = MediaAvailability(state=CONFIRMED_EMPTY)
    xml = build_rss(sample_city, sample_episodes, "video", "https://x")
    titles = [it.find("title").text for it in _items(xml)]
    assert all("Regular" not in (t or "") for t in titles)  # the withheld episode is gone
    assert any("Earlier" in (t or "") for t in titles)  # the playable one remains


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


def _ep_dur(duration=None, served=None) -> Episode:
    return Episode(
        guid="g",
        title="t",
        published=datetime(2026, 5, 1, tzinfo=UTC),
        video_url="https://x/v.mp4",
        duration=duration,
        audio_duration_served=served,
    )


class TestEnclosureDuration:
    def test_audio_prefers_served_rounded(self):
        # The hosted (trimmed) file is shorter than the source — advertise the real played length.
        assert enclosure_duration(_ep_dur(duration=7200, served=3545.6), "audio") == 3546

    def test_audio_falls_back_to_source_when_no_served(self):
        assert enclosure_duration(_ep_dur(duration=7200, served=None), "audio") == 7200

    def test_audio_uses_canonical_fields_when_legacy_values_absent(self):
        ep = _ep_dur()
        ep.source_duration_seconds = 7200.0
        ep.served_duration_seconds = 3545.6
        assert enclosure_duration(ep, "audio") == 3546

    def test_video_uses_source_duration(self):
        assert enclosure_duration(_ep_dur(duration=7200, served=3545.6), "video") == 7200

    def test_none_when_unknown(self):
        assert enclosure_duration(_ep_dur(duration=None, served=None), "audio") is None


def test_audio_feed_itunes_duration_uses_served(sample_city, sample_episodes):
    ep = sample_episodes[0]
    ep.duration = 7200
    ep.audio_duration_served = 3545.6
    xml = build_rss(sample_city, [ep], "audio", "https://x")
    assert "<itunes:duration>3546</itunes:duration>" in xml
    assert "<itunes:duration>7200</itunes:duration>" not in xml


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


def test_episode_notes_html_includes_original_provider_transcript_link():
    from datetime import UTC, datetime

    from citypods.feeds import episode_notes_html
    from citypods.models import Episode

    ep = Episode(
        guid="g",
        title="t",
        published=datetime(2026, 1, 1, tzinfo=UTC),
        video_url="v",
        links={"agenda": "https://agenda.pdf"},
        provider_transcript={
            "known_good": {
                "hosted_url": "https://cdn/provider/original.vtt",
                "format": "vtt",
                "synced": True,
            }
        },
    )

    html = episode_notes_html(ep)

    assert '<a href="https://agenda.pdf">Agenda</a>' in html
    assert (
        '<a href="https://cdn/provider/original.vtt">Original city-provided transcript</a>' in html
    )
    assert html.index("Agenda</a>") < html.index("Original city-provided transcript</a>")


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


def test_episode_notes_html_confirmed_partial_prepends_disclaimer():
    """GH#851: a confirmed-partial episode publishes with a factual disclaimer, linked to the
    source watch page when known, rather than being excluded or silently truncated."""
    from citypods.feeds import episode_notes_html

    ep = Episode(
        guid="g",
        title="t",
        published=datetime(2026, 1, 1, tzinfo=UTC),
        video_url="v",
        summary="A short recap.",
        links={"canonical_video": "https://watch.example.com/123"},
        media_availability=MediaAvailability(state=CONFIRMED_PARTIAL),
    )
    html = episode_notes_html(ep)
    assert "This recording appears incomplete" in html
    assert '<a href="https://watch.example.com/123">video page</a>' in html
    # Disclaimer comes first, ahead of the normal summary.
    assert html.index("incomplete") < html.index("A short recap.")


def test_episode_notes_html_confirmed_partial_without_link_omits_anchor():
    from citypods.feeds import episode_notes_html

    ep = Episode(
        guid="g",
        title="t",
        published=datetime(2026, 1, 1, tzinfo=UTC),
        video_url="v",
        media_availability=MediaAvailability(state=CONFIRMED_PARTIAL),
    )
    html = episode_notes_html(ep)
    assert "This recording appears incomplete" in html
    assert "<a href=" not in html


def test_episode_notes_html_suspected_partial_no_disclaimer_yet():
    """Only a *confirmed* (reproduced) short source gets the disclaimer — a single, unconfirmed
    observation must not badge an episode that may just be a transient truncated fetch."""
    from citypods.availability import SUSPECTED_PARTIAL
    from citypods.feeds import episode_notes_html

    ep = Episode(
        guid="g",
        title="t",
        published=datetime(2026, 1, 1, tzinfo=UTC),
        video_url="v",
        media_availability=MediaAvailability(state=SUSPECTED_PARTIAL),
    )
    assert episode_notes_html(ep) == ""


def test_confirmed_partial_enclosure_is_not_excluded_unlike_withheld(sample_episodes):
    """GH#851: confirmed_partial must keep its enclosure — distinct from withheld media, which
    is excluded (test_withheld_availability_omits_enclosure_from_both_kinds above)."""
    ep = sample_episodes[0]
    ep.media_availability = MediaAvailability(state=CONFIRMED_PARTIAL)
    assert enclosure_url(ep, "video") == ep.video_url


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


def test_chapters_json_prefers_source_chapters_when_timeline_changes():
    from datetime import UTC, datetime

    from citypods.feeds import chapters_json
    from citypods.models import Episode
    from citypods.timeline import Segment, Timeline

    ep = Episode(
        guid="g",
        uid="abc123",
        title="t",
        published=datetime(2026, 1, 1, tzinfo=UTC),
        video_url="https://v.mp4",
        media_kind="direct",
        source_chapters=[{"start": 600, "title": "Source item"}],
        chapters=[{"start": 9999, "title": "Stale served"}],
        chapters_basis="served:old",
    )
    ep.timeline = Timeline(
        version="silence-v1",
        segments=(
            Segment(
                served_start=0,
                served_end=300,
                kind="source",
                source_id="s0",
                source_start=0,
                source_end=300,
            ),
            Segment(
                served_start=300,
                served_end=3300,
                kind="source",
                source_id="s0",
                source_start=600,
                source_end=3600,
            ),
        ),
    )

    doc = chapters_json(ep)
    assert '"startTime": 300' in doc
    assert "Stale served" not in doc


def test_meetings_link_renders_into_feed_end_to_end(tmp_path):
    """Issue #112: a city's meetings_url, injected by LinksStage, renders as a per-episode
    resource link in the feed notes. Guards the full path (stage -> ordered_links -> RSS) that
    the snapshot test doesn't exercise (it bypasses the enrichment stages)."""
    from datetime import UTC, datetime

    from citypods.feeds import build_rss
    from citypods.models import City, Episode
    from citypods.stages import LinksStage, StageContext

    # CR2-TS-09: a self-contained stand-in instead of importing tests.test_stages.FakeFfmpeg —
    # LinksStage never calls ffmpeg (it only builds resource links), so this test doesn't need
    # that module's full fake and shouldn't silently break if it's renamed/removed.
    class _UnusedFfmpeg:
        pass

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
    ctx = StageContext(storage=None, ffmpeg=_UnusedFfmpeg(), max_kbps=96, dry_run=False, stop=None)
    LinksStage().process(None, city, [ep], ctx)

    xml = build_rss(city, [ep], "audio", "https://e.test")
    assert "Official meetings page" in xml
    assert "https://x.gov/meetings" in xml


def test_known_good_provider_transcript_fills_podcast_tag_until_active_transcript_exists():
    from datetime import UTC, datetime

    from citypods.feeds import build_rss
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
        provider_transcript={
            "known_good": {
                "hosted_url": "https://cdn/provider/original.vtt",
                "format": "vtt",
                "synced": True,
            }
        },
    )

    xml = build_rss(city, [ep], "audio", "https://e.test")

    assert '<podcast:transcript url="https://cdn/provider/original.vtt" type="text/vtt"/>' in xml
    assert "Original city-provided transcript" in xml


def test_active_transcript_wins_podcast_tag_over_provider_original():
    from datetime import UTC, datetime

    from citypods.feeds import build_rss
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
        transcript_hosted_url="https://cdn/asr/served.vtt",
        transcript_format="vtt",
        transcript_synced=True,
        provider_transcript={
            "known_good": {
                "hosted_url": "https://cdn/provider/original.vtt",
                "format": "vtt",
                "synced": True,
            }
        },
    )

    xml = build_rss(city, [ep], "audio", "https://e.test")

    assert '<podcast:transcript url="https://cdn/asr/served.vtt" type="text/vtt"/>' in xml
    assert 'podcast:transcript url="https://cdn/provider/original.vtt"' not in xml
    assert "https://cdn/provider/original.vtt" in xml
