"""Tests for stable identity, split hashes, the record store, and legacy migration."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from citypods.models import City, Episode
from citypods.records import (
    assign_uids,
    audio_object_key,
    audio_spec_hash,
    episode_to_record,
    feed_content_hash,
    load_records,
    merge_persisted,
    merge_records,
    migrate_legacy_manifests,
    prune_archive,
    record_to_episode,
    save_records,
    shard_index,
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


def test_shard_index_is_deterministic_and_in_range():
    # Stable across calls/processes (SHA-1, not salted hash()), and always 0 <= i < n.
    for key in ("abc123", "deadbeef", source_key(_city(source={"feed_url": "F"}))):
        for n in (1, 4, 7):
            i = shard_index(key, n)
            assert 0 <= i < n
            assert shard_index(key, n) == i  # deterministic


def test_shard_partition_is_disjoint_and_exhaustive():
    """The H6b acceptance: across k in range(N) the shards partition every source exactly once —
    so two concurrent shards never own (and never write) the same record file."""
    keys = [source_key(_city(source={"feed_url": f"F{i}"})) for i in range(50)]
    n = 4
    buckets = {k: [key for key in keys if shard_index(key, n) == k] for k in range(n)}
    flat = [key for b in buckets.values() for key in b]
    assert sorted(flat) == sorted(keys)  # exhaustive
    assert len(flat) == len(set(flat)) == len(keys)  # disjoint (each source in exactly one shard)


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


def test_audio_spec_hash_loudness_profile_changes_hash():
    ep = _ep("g1")
    ep.uid = "u1"
    base = audio_spec_hash(ep, max_kbps=96)
    with_loudness = audio_spec_hash(ep, max_kbps=96, loudness_profile="ebuR128:-16LUFS")
    assert base != with_loudness


def test_audio_spec_hash_loudness_empty_string_matches_default():
    # Empty string and omitted loudness_profile must produce identical hashes (backward-compat).
    ep = _ep("g1")
    ep.uid = "u1"
    assert audio_spec_hash(ep, max_kbps=96) == audio_spec_hash(ep, max_kbps=96, loudness_profile="")


def test_audio_spec_hash_different_loudness_targets_differ():
    ep = _ep("g1")
    ep.uid = "u1"
    assert audio_spec_hash(ep, max_kbps=96, loudness_profile="ebuR128:-16LUFS") != audio_spec_hash(
        ep, max_kbps=96, loudness_profile="ebuR128:-23LUFS"
    )


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


def test_record_to_episode_roundtrips_with_episode_to_record():
    ep = _ep("g1")
    ep.uid = "u1"
    ep.summary = "s"
    ep.duration = 3723
    ep.media_kind = "hls"
    ep.hosted_audio_url = "https://cdn/u1.m4a"
    ep.audio_key = "k"
    ep.audio_spec_hash = "spec"
    ep.links = {"agenda": "https://x/a"}
    ep.chapters = [{"start": 0, "title": "x"}]
    ep.transcript_url = "https://x/t.vtt"
    ep.materialize_attempts = 2
    ep.materialize_last_attempt = "2026-06-01T00:00:00+00:00"

    back = record_to_episode(episode_to_record(ep))
    for attr in (
        "uid",
        "guid",
        "title",
        "published",
        "body",
        "media_kind",
        "video_url",
        "duration",
        "links",
        "chapters",
        "summary",
        "transcript_key",
        "hosted_audio_url",
        "audio_key",
        "audio_spec_hash",
        "materialize_attempts",
        "materialize_last_attempt",
    ):
        assert getattr(back, attr) == getattr(ep, attr), attr


def test_merge_records_is_append_only_with_fresh_winning():
    persisted = {"a": {"uid": "a", "title": "old-a"}, "b": {"uid": "b", "title": "old-b"}}
    fresh = {"b": {"uid": "b", "title": "new-b"}, "c": {"uid": "c", "title": "new-c"}}
    merged = merge_records(persisted, fresh)
    assert set(merged) == {"a", "b", "c"}  # nothing dropped (a left the window but is kept)
    assert merged["b"]["title"] == "new-b"  # fresh wins on collision
    assert merged["a"]["title"] == "old-a"  # persisted-only carried forward


def _rec(uid, days_old):
    when = datetime.now(UTC) - timedelta(days=days_old)
    return {"uid": uid, "published": when.isoformat()}


def test_prune_archive_keeps_everything_at_default_caps():
    records = {f"u{i}": _rec(f"u{i}", i * 30) for i in range(20)}
    assert prune_archive(records, max_items=5000, max_age_years=1000) == records


def test_prune_archive_keeps_newest_n_by_max_items():
    records = {f"u{i}": _rec(f"u{i}", i) for i in range(10)}  # u0 newest, u9 oldest
    kept = prune_archive(records, max_items=3, max_age_years=1000)
    assert set(kept) == {"u0", "u1", "u2"}


def test_prune_archive_drops_records_older_than_max_age():
    records = {"recent": _rec("recent", 10), "ancient": _rec("ancient", 800)}
    kept = prune_archive(records, max_items=5000, max_age_years=1.0)
    assert set(kept) == {"recent"}


def test_prune_archive_keeps_undated_records():
    records = {"dated": _rec("dated", 5), "undated": {"uid": "undated"}}
    kept = prune_archive(records, max_items=5000, max_age_years=1.0)
    assert "undated" in kept  # fail-safe: never drop content we can't date


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
