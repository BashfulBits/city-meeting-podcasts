"""Tests for index rendering (JSON-backed virtualization + no-JS fallback)."""

from __future__ import annotations

import json
import re

from citypods.models import City
from citypods.site import render_index


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
