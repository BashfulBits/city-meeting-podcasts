"""Tests for index rendering (JSON-backed virtualization + no-JS fallback)."""

from __future__ import annotations

import json
import re

from citypods.models import City
from citypods.site import render_index, render_search_page


def _city(slug, author, title):
    return City(
        slug=slug,
        provider="granicus",
        source={"feed_url": "u"},
        podcast_title=title,
        podcast_author=author,
        podcast_email="",
        podcast_description="d",
        state="TX",
    )


def _render(cities):
    return render_index(
        cities,
        {"site_title": "T", "site_description": "D"},
        "https://e.test",
        {c.slug: {"has_audio": True, "has_video": False} for c in cities},
    )


def _dataset(html):
    m = re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S)
    assert m, "embedded dataset not found"
    return json.loads(m.group(1))


def test_index_embeds_json_dataset_grouped_by_author():
    cities = [
        _city("a-tx-council", "City of A, TX", "A — Council"),
        _city("a-tx-zoning", "City of A, TX", "A — Zoning"),
        _city("b-tx-council", "City of B, TX", "B — Council"),
    ]
    data = _dataset(_render(cities))
    # Two author groups; the A group carries both feeds.
    labels = {g["label"]: len(g["feeds"]) for g in data}
    assert labels == {"City of A, TX": 2, "City of B, TX": 1}
    # Builder-only key is stripped; search strings are lowercased.
    for g in data:
        assert "search_parts" not in g
        assert g["search"] == g["search"].lower()
        for f in g["feeds"]:
            assert {"url", "label", "has_audio", "has_video", "search"} <= set(f)


def test_index_has_noscript_fallback_listing_all_feeds():
    html = _render([_city("a-tx-council", "City of A, TX", "A — Council")])
    noscript = html[html.index("<noscript>") : html.index("</noscript>")]
    assert "City of A, TX" in noscript
    assert "https://e.test/a-tx-council/" in noscript


def test_search_page_points_at_static_manifest_and_vendored_engine():
    html = render_search_page({"site_title": "T", "site_description": "D"}, "https://e.test")
    assert "https://e.test/data/search/manifest.json" in html
    assert "https://e.test/assets/minisearch-7.1.2.js" in html
    assert "Search meetings" in html
    assert 'id="tag"' in html
    assert 'id="coverage"' in html
    assert 'id="source"' not in html
    assert "Transcript coverage:" in html
    assert "documentKey(shard.source_key, doc.uid)" in html
    # Withheld results deliberately set their timestamp to null before an href is assembled.
    assert "const time = doc.is_withheld ? null : hitTime(doc, terms);" in html
    assert "unavailable" in html
    assert "Transcript coverage temporarily unavailable." in html
    assert "window.setTimeout(search, 200)" in html


def test_index_hides_global_search_link_when_disabled():
    city = _city("a-tx-council", "City of A, TX", "A — Council")
    html = render_index(
        [city],
        {"site_title": "T", "site_description": "D"},
        "https://e.test",
        search_enabled=False,
    )
    assert "/search/" not in html


def test_city_page_keeps_video_only_episode_for_video_only_city(monkeypatch):
    # CR2-CP-12: filtering episodes on the audio enclosure only meant a video-only city
    # (has_audio=False, has_video=True) rendered zero episodes despite a populated video feed.
    from datetime import UTC, datetime

    import citypods.site as site_mod
    from citypods.models import Episode

    def fake_enclosure_url(ep, kind):
        return "https://cdn/x.mp4" if kind == "video" else None

    monkeypatch.setattr(site_mod, "enclosure_url", fake_enclosure_url)

    ep = Episode(
        guid="g",
        title="City Council – May 1",
        published=datetime(2026, 5, 1, tzinfo=UTC),
        video_url="https://cdn/x.mp4",
        media_kind="direct",
    )
    html = site_mod.render_city_page(
        _city("x-tx", "City of X", "X — Council"),
        "https://e.test",
        [ep],
        has_audio=False,
        has_video=True,
    )
    assert "City Council – May 1" in html


def test_city_page_drops_episode_with_no_enclosure_for_either_offered_kind(monkeypatch):
    from datetime import UTC, datetime

    import citypods.site as site_mod
    from citypods.models import Episode

    monkeypatch.setattr(site_mod, "enclosure_url", lambda ep, kind: None)

    ep = Episode(
        guid="g",
        title="Should Not Appear",
        published=datetime(2026, 5, 1, tzinfo=UTC),
        video_url="https://cdn/x.mp4",
        media_kind="direct",
    )
    html = site_mod.render_city_page(
        _city("x-tx", "City of X", "X — Council"),
        "https://e.test",
        [ep],
        has_audio=True,
        has_video=True,
    )
    assert "Should Not Appear" not in html


def test_render_redirect_page_escapes_the_url():
    from citypods.site import render_redirect_page

    html = render_redirect_page('https://x.test/"><script>alert(1)</script>')
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_city_page_renders_episode_resource_links():
    from datetime import UTC, datetime

    from citypods.models import Episode
    from citypods.site import render_city_page

    ep = Episode(
        guid="g",
        title="City Council – May 1",
        published=datetime(2026, 5, 1, tzinfo=UTC),
        video_url="https://cdn/x.mp4",
        media_kind="direct",
        links={"canonical_video": "https://watch/page", "agenda": "https://agenda.pdf"},
    )
    html = render_city_page(_city("x-tx", "City of X", "X — Council"), "https://e.test", [ep])
    # agenda label appears before the video link (LINK_LABELS order) and both are linked
    assert '<a href="https://agenda.pdf">Agenda</a>' in html
    assert '<a href="https://watch/page">Watch the video</a>' in html
    assert html.index("Agenda</a>") < html.index("Watch the video</a>")


def test_archive_renders_calendar_only_rows_without_making_them_episode_pages():
    from datetime import UTC, datetime

    from citypods.models import AgendaRecord, Episode
    from citypods.site import render_city_archive_page

    episode = Episode(
        guid="archive-1",
        uid="episode-1",
        title="Recorded Council Meeting",
        published=datetime(2026, 5, 1, tzinfo=UTC),
        video_url="https://cdn.example/recording.mp4",
        body="City Council",
    )
    html = render_city_archive_page(
        _city("x-tx", "City of X", "X — Council"),
        "https://e.test",
        [episode],
        calendar_records=[
            AgendaRecord(
                body="City Council",
                title="Duplicate Calendar Row",
                published=episode.published,
                video_guid="archive-1",
            ),
            AgendaRecord(
                body="Library Board",
                title="Library Board Meeting",
                published=datetime(2026, 5, 2, tzinfo=UTC),
                links={"agenda": "https://agenda.example/library.pdf"},
                uid="calendar-only",
            ),
        ],
    )

    assert "Recorded Council Meeting" in html
    assert "Duplicate Calendar Row" not in html
    assert "Calendar-only meetings" in html
    assert "Library Board Meeting" in html
    assert '<a href="https://agenda.example/library.pdf">Agenda</a>' in html
    assert "/calendar-only/" not in html


def test_city_page_renders_original_provider_transcript_link():
    from datetime import UTC, datetime

    from citypods.models import Episode
    from citypods.site import render_city_page

    ep = Episode(
        guid="g",
        title="City Council - May 1",
        published=datetime(2026, 5, 1, tzinfo=UTC),
        video_url="https://cdn/x.mp4",
        media_kind="direct",
        links={"agenda": "https://agenda.pdf"},
        provider_transcript={
            "known_good": {
                "hosted_url": "https://cdn/provider/original.vtt",
                "format": "vtt",
                "synced": True,
            }
        },
    )
    html = render_city_page(_city("x-tx", "City of X", "X - Council"), "https://e.test", [ep])

    assert '<a href="https://agenda.pdf">Agenda</a>' in html
    assert (
        '<a href="https://cdn/provider/original.vtt">Original city-provided transcript</a>' in html
    )


def test_city_page_formats_float_duration_from_archival_metadata():
    from datetime import UTC, datetime

    from citypods.models import Episode
    from citypods.site import render_city_page

    ep = Episode(
        guid="g",
        title="City Council",
        published=datetime(2026, 5, 1, tzinfo=UTC),
        video_url="https://cdn/x.mp4",
        media_kind="direct",
        duration=3660.5,
    )

    html = render_city_page(_city("x-tx", "City of X", "X — Council"), "https://e.test", [ep])

    assert "1h 01m" in html


def test_meeting_page_renders_permalinks_chapters_and_transcript(sample_city):
    from datetime import UTC, datetime

    from citypods.models import Episode
    from citypods.site import render_meeting_page

    ep = Episode(
        guid="g",
        uid="meeting-1",
        title="City Council - May 1",
        published=datetime(2026, 5, 1, tzinfo=UTC),
        video_url="https://media.example/x.mp4",
        hosted_audio_url="https://cdn.example/x.m4a",
        transcript_hosted_url="https://cdn.example/x.vtt",
        transcript_format="vtt",
        transcript_synced=True,
        duration=600,
        links={"canonical_video": "https://city.granicus.com/MediaPlayer.php?view_id=1&clip_id=2"},
        chapters=[{"start": 120, "title": "Public comment"}],
        chapters_basis="served",
    )
    html = render_meeting_page(
        sample_city,
        ep,
        "https://e.test",
        site_config={"github_repo": "owner/repo"},
    )
    assert 'rel="canonical" href="https://e.test/denton-tx/meeting-1/"' in html
    assert 'src="https://cdn.example/x.m4a"' in html
    assert "Public comment" in html
    assert "Loading transcript" in html
    assert "Report a problem" in html
    assert "starttime=120" in html


def test_meeting_page_keeps_unavailable_recording_discoverable(sample_city):
    from datetime import UTC, datetime

    from citypods.availability import CONFIRMED_EMPTY, MediaAvailability
    from citypods.models import Episode
    from citypods.site import render_meeting_page

    ep = Episode(
        guid="g",
        uid="missing-1",
        title="Missing meeting",
        published=datetime(2026, 5, 1, tzinfo=UTC),
        video_url="https://media.example/x.mp4",
        media_availability=MediaAvailability(state=CONFIRMED_EMPTY, reason="empty file"),
    )
    html = render_meeting_page(sample_city, ep, "https://e.test")
    assert "Recording unavailable" in html
    assert "empty file" in html
    assert '<audio id="player"' not in html


def test_city_page_links_recent_meetings_and_archive(sample_city, sample_episodes):
    from citypods.site import render_city_page

    sample_episodes[0].uid = "meeting-1"
    html = render_city_page(sample_city, "https://e.test", [sample_episodes[0]])
    assert 'href="https://e.test/denton-tx/meeting-1/"' in html
    assert 'href="https://e.test/denton-tx/archive/"' in html


def test_city_archive_lists_retained_unavailable_meetings(sample_city):
    from datetime import UTC, datetime

    from citypods.availability import CONFIRMED_EMPTY, MediaAvailability
    from citypods.models import Episode
    from citypods.site import render_city_archive_page

    ep = Episode(
        guid="g",
        uid="missing-1",
        title="Missing meeting",
        published=datetime(2026, 5, 1, tzinfo=UTC),
        video_url="https://media.example/x.mp4",
        media_availability=MediaAvailability(state=CONFIRMED_EMPTY, reason="empty file"),
    )
    html = render_city_archive_page(sample_city, "https://e.test", [ep])
    assert 'href="https://e.test/denton-tx/missing-1/"' in html
    assert "Confirmed Empty" in html
