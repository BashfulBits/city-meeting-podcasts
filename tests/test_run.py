"""Tests for incremental builds: content-hash change detection + state persistence."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from citypods import run
from citypods.models import Episode
from citypods.providers import get_provider, register
from citypods.records import feed_content_hash
from citypods.state import build_fingerprint


def _ep(guid="g1", title="City Council", hosted=None):
    return Episode(
        guid=guid,
        uid=f"uid-{guid}",
        title=title,
        published=datetime(2026, 5, 1, tzinfo=UTC),
        video_url=f"https://x/{guid}.mp4",
        duration=600,
        body="City Council",
        hosted_audio_url=hosted,
    )


# --- pure content-hash / fingerprint units ---------------------------------------------


def test_content_hash_stable_and_order_independent():
    fp = "fp0"
    a = [_ep("g1"), _ep("g2")]
    b = [_ep("g2"), _ep("g1")]  # reversed
    assert feed_content_hash(a, fp) == feed_content_hash(b, fp)


def test_content_hash_changes_with_episodes_and_fingerprint():
    base = feed_content_hash([_ep("g1")], "fp0")
    assert base != feed_content_hash([_ep("g1", title="Different")], "fp0")
    assert base != feed_content_hash([_ep("g1")], "fp1")  # fingerprint bust
    # A newly-hosted enclosure changes the hash so the feed re-renders.
    assert base != feed_content_hash([_ep("g1", hosted="https://cdn/g1.m4a")], "fp0")


def test_build_fingerprint_tracks_base_url_and_templates():
    assert build_fingerprint("https://a") != build_fingerprint("https://b")
    assert build_fingerprint("https://a") == build_fingerprint("https://a")


# --- end-to-end incremental build via a fake provider ----------------------------------


class _FakeProvider:
    name = "faketest"

    def __init__(self):
        self.episodes = [_ep("g1"), _ep("g2")]
        self.fetches = 0

    def validate(self, source):
        pass

    def detect_change(self, source):
        return None  # no HTTP validator -> exercises the content-hash path

    def fetch_episodes(self, source):
        self.fetches += 1
        return list(self.episodes)

    def resolve_media_url(self, episode, source):
        return episode.video_url


@pytest.fixture
def fake_provider():
    provider = _FakeProvider()
    register(provider)
    try:
        yield provider
    finally:
        from citypods.providers import _REGISTRY

        _REGISTRY.pop("faketest", None)


def _setup(tmp_path):
    cities = tmp_path / "cities"
    cities.mkdir()
    (cities / "fake-city.yml").write_text(
        "slug: fake-city\n"
        "provider: faketest\n"
        "source: {feed_url: 'https://x'}\n"
        'podcast_title: "Fake City"\n'
        'podcast_author: "City of Fake"\n'
        'podcast_email: ""\n'
        'podcast_description: "desc"\n'
    )
    (tmp_path / "site_config.yml").write_text(f"state_dir: {tmp_path / 'state'}\n")
    return cities


def _build(tmp_path, cities):
    return run.build(
        site_config_path=tmp_path / "site_config.yml",
        cities_dir=cities,
        output_dir=tmp_path / "docs",
        base_url="https://example.test",
    )


def test_unchanged_city_is_skipped_on_second_build(tmp_path, fake_provider):
    assert get_provider("faketest") is fake_provider
    cities = _setup(tmp_path)

    first = _build(tmp_path, cities)
    assert [r.status for r in first] == ["built"]

    second = _build(tmp_path, cities)
    assert [r.status for r in second] == ["skipped"]
    # State persisted outside docs/ so it survives a wiped output tree.
    assert (tmp_path / "state" / "feed_etags.json").exists()


def test_changed_content_rebuilds(tmp_path, fake_provider):
    cities = _setup(tmp_path)
    _build(tmp_path, cities)

    fake_provider.episodes.append(_ep("g3", title="Planning Commission"))
    result = _build(tmp_path, cities)
    assert [r.status for r in result] == ["built"]


def test_missing_outputs_force_rebuild_even_if_hash_matches(tmp_path, fake_provider):
    cities = _setup(tmp_path)
    _build(tmp_path, cities)

    # Simulate the CI case where docs/ is wiped but state (cache) survives.
    import shutil

    shutil.rmtree(tmp_path / "docs")
    result = _build(tmp_path, cities)
    assert [r.status for r in result] == ["built"]


def test_write_chapter_sidecars_writes_and_prunes(tmp_path):
    import json
    from datetime import UTC, datetime

    from citypods.models import City, Episode
    from citypods.run import _write_chapter_sidecars

    city = City(
        slug="x-tx",
        provider="granicus",
        source={"feed_url": "u"},
        podcast_title="X",
        podcast_author="A",
        podcast_email="",
        podcast_description="d",
    )

    def ep(uid, chapters):
        return Episode(
            guid=uid,
            uid=uid,
            title="t",
            published=datetime(2026, 1, 1, tzinfo=UTC),
            video_url="v",
            media_kind="direct",
            chapters=chapters,
        )

    city_dir = tmp_path / "x-tx"
    city_dir.mkdir()
    (city_dir / "chapters").mkdir()
    (city_dir / "chapters" / "stale.json").write_text("{}")  # leftover -> should be pruned

    eps = [ep("u1", [{"start": 0, "title": "Intro"}]), ep("u2", [])]  # u2 has no chapters
    _write_chapter_sidecars(city_dir, city, eps, "https://e.test")

    doc = json.loads((city_dir / "chapters" / "u1.json").read_text())
    assert doc["chapters"] == [{"startTime": 0, "title": "Intro"}]
    assert not (city_dir / "chapters" / "u2.json").exists()  # no chapters -> no file
    assert not (city_dir / "chapters" / "stale.json").exists()  # pruned
