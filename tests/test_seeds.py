from __future__ import annotations

from datetime import UTC, datetime

import pytest

from citypods.models import City, Episode
from citypods.records import source_key
from citypods.seeds import merge_seed_episodes, seed_episodes


def _city(extra=None):
    return City(
        slug="x-tx",
        provider="civicplus",
        source={"feed_url": "https://x.test/rss"},
        podcast_title="X",
        podcast_author="City of X",
        podcast_email="",
        podcast_description="d",
        extra=extra or {},
    )


def test_seed_episodes_parse_top_level_feed_extra_without_changing_source_key():
    city = _city(
        {
            "seed_episodes": [
                {
                    "title": "Historic Meeting",
                    "published": "2026-02-03T18:00:00-06:00",
                    "video_url": "https://x.test/CivicMedia.aspx?VID=1#player",
                    "body": "City Council",
                    "media_kind": "hls",
                    "duration": 120,
                }
            ]
        }
    )

    before = source_key(city)
    [seed] = seed_episodes(city)

    assert source_key(city) == before
    assert seed.guid == "https://x.test/CivicMedia.aspx?VID=1#player"
    assert seed.title == "Historic Meeting"
    assert seed.published == datetime.fromisoformat("2026-02-03T18:00:00-06:00")
    assert seed.video_url == "https://x.test/CivicMedia.aspx?VID=1#player"
    assert seed.body == "City Council"
    assert seed.media_kind == "hls"
    assert seed.duration == 120


def test_merge_seed_episodes_prefers_fetched_duplicate():
    city = _city(
        {
            "seed_episodes": [
                {
                    "title": "Regular Meeting",
                    "published": "2026-02-03T18:00:00+00:00",
                    "video_url": "https://x.test/CivicMedia.aspx?VID=1#player",
                    "body": "City Council",
                    "media_kind": "hls",
                },
                {
                    "title": "Older Meeting",
                    "published": "2026-01-20T18:00:00+00:00",
                    "video_url": "https://x.test/CivicMedia.aspx?VID=2#player",
                    "body": "City Council",
                    "media_kind": "hls",
                },
            ]
        }
    )
    fetched = [
        Episode(
            guid="live",
            title="Regular Meeting",
            published=datetime(2026, 2, 3, 18, tzinfo=UTC),
            video_url="https://x.test/CivicMedia.aspx?VID=1",
            body="City Council",
            media_kind="hls",
        )
    ]

    merged = merge_seed_episodes(city, fetched)

    assert [ep.title for ep in merged] == ["Regular Meeting", "Older Meeting"]


def test_seed_episodes_requires_core_fields():
    city = _city({"seed_episodes": [{"title": "Missing URL"}]})

    with pytest.raises(ValueError, match="missing required keys"):
        seed_episodes(city)
