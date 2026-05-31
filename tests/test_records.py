"""Tests for stable identity, split hashes, the record store, and legacy migration."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from citypods.models import City, Episode
from citypods.records import (
    assign_uids,
    audio_object_key,
    audio_spec_hash,
    episode_to_record,
    feed_content_hash,
    load_records,
    merge_persisted,
    migrate_legacy_manifests,
    save_records,
    source_key,
)


def _city(provider="granicus", source=None, author="City of Denton, TX"):
    return City(
        slug="denton-tx",
        provider=provider,
        source=source or {"feed_url": "F"},
        podcast_title="t",
        podcast_author=author,
        podcast_email="",
        podcast_description="d",
    )


def _ep(guid, body="City Council", when=datetime(2026, 5, 19, 16, 0, tzinfo=UTC)):
    return Episode(
        guid=guid, title=body, published=when, video_url=f"https://x/{guid}.mp4", body=body
    )


def test_uid_is_stable_across_provider_migration():
    # Same meeting (author+body+date) from two different providers / guids -> same uid.
    granicus = _city(provider="granicus", source={"feed_url": "G"})
    swagit = _city(provider="swagit", source={"list_url": "S", "body": "City Council"})
    e1 = _ep("granicus-clip-1")
    e2 = _ep("swagit-video-9")
    assign_uids(granicus, [e1])
    assign_uids(swagit, [e2])
    assert e1.uid == e2.uid  # subscribers don't re-download on migration


def test_uid_disambiguates_same_day_sessions():
    morning = _ep("a", when=datetime(2026, 5, 19, 9, 0, tzinfo=UTC))
    evening = _ep("b", when=datetime(2026, 5, 19, 18, 0, tzinfo=UTC))
    assign_uids(_city(), [evening, morning])  # order-independent
    assert morning.uid != evening.uid


def test_source_key_ignores_body_so_feeds_share_storage():
    combined = _city(source={"feed_url": "F"})
    per_board = _city(source={"feed_url": "F", "body": "City Council"})
    assert source_key(combined) == source_key(per_board)


def test_audio_spec_hash_and_key_track_only_audio_inputs():
    ep = _ep("g1")
    ep.uid = "u1"
    base = audio_spec_hash(ep, max_kbps=96)
    # A feed-only change (summary) does NOT change the audio spec...
    ep.summary = "a summary"
    assert audio_spec_hash(ep, max_kbps=96) == base
    # ...but chapters or a bitrate change does.
    ep.chapters = [{"start": 0, "title": "x"}]
    assert audio_spec_hash(ep, max_kbps=96) != base
    assert audio_object_key(_city(), ep, base).endswith(f"u1-{base}.m4a")


def test_feed_hash_reacts_to_notes_but_audio_spec_does_not():
    ep = _ep("g1")
    ep.uid = "u1"
    before = feed_content_hash([ep], "fp")
    ep.summary = "new summary"
    assert feed_content_hash([ep], "fp") != before  # summary re-renders the feed


def test_record_store_roundtrip(tmp_path):
    ep = _ep("g1")
    ep.uid = "u1"
    ep.summary = "s"
    ep.hosted_audio_url = "https://cdn/u1.m4a"
    ep.audio_key = "k"
    ep.audio_spec_hash = "spec"
    save_records(tmp_path, "src", {"u1": episode_to_record(ep)})

    loaded = load_records(tmp_path, "src")
    fresh = _ep("g1-new-guid")  # provider guid changed; uid is what matches
    fresh.uid = "u1"
    merge_persisted([fresh], loaded)
    assert fresh.summary == "s"
    assert fresh.hosted_audio_url == "https://cdn/u1.m4a"
    assert fresh.audio_spec_hash == "spec"
    # Envelope carries a schema version for future migrations.
    raw = json.loads((tmp_path / "sources" / "src" / "episodes.json").read_text())
    assert raw["schema_version"] >= 1


def test_legacy_manifest_carryover(tmp_path):
    # Old per-slug manifest keyed by provider guid.
    slug_dir = tmp_path / "denton-tx"
    slug_dir.mkdir()
    (slug_dir / "audio_manifest.json").write_text(
        json.dumps({"clip-1": {"key": "old/k.m4a", "url": "https://cdn/old.m4a"}})
    )
    ep = _ep("clip-1")
    ep.uid = "u1"
    seeded = migrate_legacy_manifests(tmp_path, [ep])
    assert seeded == 1
    assert ep.hosted_audio_url == "https://cdn/old.m4a"
    assert ep.audio_spec_hash == "legacy"  # reused, not re-encoded
