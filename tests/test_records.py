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
    merge_preserving_foreign,
    merge_records,
    migrate_legacy_manifests,
    pending_audio_work,
    pending_transcribe_work,
    protected_blocks_for_lane,
    prune_archive,
    record_to_episode,
    save_records,
    shard_assignment,
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


def test_shard_assignment_is_deterministic_and_in_range():
    # Stable across calls/processes (sorted order, not salted hash()), and always 0 <= i < n.
    keys = ["abc123", "deadbeef", source_key(_city(source={"feed_url": "F"}))]
    for n in (1, 4, 7):
        a = shard_assignment(keys, n)
        assert set(a) == set(keys)
        assert all(0 <= v < n for v in a.values())
        assert shard_assignment(keys, n) == a  # deterministic
        assert shard_assignment(reversed(keys), n) == a  # order-independent (sorts internally)


def test_shard_assignment_is_disjoint_and_exhaustive():
    """The H6b acceptance: across k in range(N) the shards partition every source exactly once —
    so two concurrent shards never own (and never write) the same record file."""
    keys = [source_key(_city(source={"feed_url": f"F{i}"})) for i in range(50)]
    n = 4
    a = shard_assignment(keys, n)
    buckets = {k: [key for key in keys if a[key] == k] for k in range(n)}
    flat = [key for b in buckets.values() for key in b]
    assert sorted(flat) == sorted(keys)  # exhaustive
    assert len(flat) == len(set(flat)) == len(keys)  # disjoint (each source in exactly one shard)


def test_shard_assignment_fallback_is_balanced_and_never_empty():
    """With omitted/equal weights, assignment keeps every shard non-empty (within +/-1) until
    #sources < N, fixing the wasted empty ``audio (0)`` runner hash-mod produced."""
    # 10 distinct sources, N=4 (the catalog shape that left shards empty under hash-mod).
    keys = [source_key(_city(source={"feed_url": f"F{i}"})) for i in range(10)]
    n = 4
    a = shard_assignment(keys, n)
    sizes = [sum(1 for v in a.values() if v == k) for k in range(n)]
    assert min(sizes) >= 1  # no empty shard
    assert max(sizes) - min(sizes) <= 1  # balanced (10 over 4 → 3,3,2,2)
    assert sorted(sizes, reverse=True) == [3, 3, 2, 2]


def test_shard_assignment_balances_weighted_sources():
    """Weighted assignment keeps source ownership atomic while packing heavy sources first."""
    keys = ["heavy", "mid", "small-a", "small-b"]
    weights = {"heavy": 8, "mid": 7, "small-a": 6, "small-b": 5}
    a = shard_assignment(reversed(keys), 2, weights=weights)
    loads = [sum(weights[k] for k, shard in a.items() if shard == i) for i in range(2)]
    assert sorted(loads) == [13, 13]
    assert a["heavy"] != a["mid"]
    assert shard_assignment(keys, 2, weights=weights) == a  # input-order independent


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


def test_audio_spec_hash_processing_profile_changes_hash():
    ep = _ep("g1")
    ep.uid = "u1"
    base = audio_spec_hash(ep, max_kbps=96, loudness_profile="ebuR128:-16LUFS")
    processed = audio_spec_hash(
        ep,
        max_kbps=96,
        loudness_profile="ebuR128:-16LUFS",
        processing_profile="podcast-speech-v2",
    )
    assert base != processed


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


def test_pending_audio_work_counts_only_episodes_still_needing_an_encode(tmp_path):
    done = _ep("g-done")
    done.uid = "u-done"
    done.hosted_audio_url = "https://cdn/u-done.m4a"
    done.audio_spec_hash = audio_spec_hash(done, max_kbps=96)

    stale_spec = _ep("g-stale")
    stale_spec.uid = "u-stale"
    stale_spec.hosted_audio_url = "https://cdn/u-stale.m4a"
    stale_spec.audio_spec_hash = "an-old-spec-that-no-longer-matches"

    backing_off = _ep("g-backoff")
    backing_off.uid = "u-backoff"
    backing_off.materialize_attempts = 1
    backing_off.materialize_last_attempt = datetime.now(UTC).isoformat()

    never_attempted = _ep("g-new")
    never_attempted.uid = "u-new"

    save_records(
        tmp_path,
        "src",
        {
            "u-done": episode_to_record(done),
            "u-stale": episode_to_record(stale_spec),
            "u-backoff": episode_to_record(backing_off),
            "u-new": episode_to_record(never_attempted),
        },
    )

    pending = pending_audio_work(
        tmp_path, "src", extract_audio=True, max_kbps=96, loudness_profile="", processing_profile=""
    )
    # Only the stale-spec and never-attempted episodes still need an encode: "done" is already
    # hosted under the current spec, and "backoff" won't be retried this run either.
    assert pending == 2


def test_pending_audio_work_skips_direct_episodes_when_extraction_disabled(tmp_path):
    direct = _ep("g-direct")
    direct.uid = "u-direct"
    direct.media_kind = "direct"
    save_records(tmp_path, "src", {"u-direct": episode_to_record(direct)})

    assert (
        pending_audio_work(
            tmp_path,
            "src",
            extract_audio=False,
            max_kbps=96,
            loudness_profile="",
            processing_profile="",
        )
        == 0
    )
    assert (
        pending_audio_work(
            tmp_path,
            "src",
            extract_audio=True,
            max_kbps=96,
            loudness_profile="",
            processing_profile="",
        )
        == 1
    )


def test_pending_audio_work_is_zero_for_unknown_source(tmp_path):
    assert (
        pending_audio_work(
            tmp_path,
            "no-such-src",
            extract_audio=False,
            max_kbps=96,
            loudness_profile="",
            processing_profile="",
        )
        == 0
    )


def test_pending_transcribe_work_sums_duration_of_episodes_still_needing_asr(tmp_path):
    no_audio_yet = _ep("g-no-audio")
    no_audio_yet.uid = "u-no-audio"
    no_audio_yet.duration = 9999  # would dominate the sum if counted — must be excluded

    synced_provider = _ep("g-provider")
    synced_provider.uid = "u-provider"
    synced_provider.audio_key = "audio/src/u-provider.m4a"
    synced_provider.audio_spec_hash = "spec"
    synced_provider.hosted_audio_url = "https://cdn/u-provider.m4a"
    synced_provider.audio_duration_served = 1200.0
    synced_provider.transcript_key = "transcripts/src/u-provider-officialminutes.vtt"
    synced_provider.transcript_synced = True

    synced_current_asr = _ep("g-current-asr")
    synced_current_asr.uid = "u-current-asr"
    synced_current_asr.audio_key = "audio/src/u-current-asr.m4a"
    synced_current_asr.audio_spec_hash = "spec"
    synced_current_asr.hosted_audio_url = "https://cdn/u-current-asr.m4a"
    synced_current_asr.audio_duration_served = 1800.0
    synced_current_asr.transcript_key = "transcripts/src/u-current-asr-asr-recipe.vtt"
    synced_current_asr.transcript_synced = True
    synced_current_asr.transcript_pipeline_version = "3"

    stale_asr = _ep("g-stale-asr")
    stale_asr.uid = "u-stale-asr"
    stale_asr.audio_key = "audio/src/u-stale-asr.m4a"
    stale_asr.audio_spec_hash = "spec"
    stale_asr.hosted_audio_url = "https://cdn/u-stale-asr.m4a"
    stale_asr.audio_duration_served = 2400.0
    stale_asr.transcript_key = "transcripts/src/u-stale-asr-asr-recipe.vtt"
    stale_asr.transcript_synced = True
    stale_asr.transcript_pipeline_version = "2"  # superseded by ASR_PIPELINE_VERSION="3"

    never_transcribed = _ep("g-pending")
    never_transcribed.uid = "u-pending"
    never_transcribed.audio_key = "audio/src/u-pending.m4a"
    never_transcribed.audio_spec_hash = "spec"
    never_transcribed.hosted_audio_url = "https://cdn/u-pending.m4a"
    never_transcribed.audio_duration_served = 3600.0

    no_served_duration = _ep("g-fallback")
    no_served_duration.uid = "u-fallback"
    no_served_duration.audio_key = "audio/src/u-fallback.m4a"
    no_served_duration.audio_spec_hash = "spec"
    no_served_duration.hosted_audio_url = "https://cdn/u-fallback.m4a"
    no_served_duration.duration = 600  # falls back to the source-feed duration

    save_records(
        tmp_path,
        "src",
        {
            "u-no-audio": episode_to_record(no_audio_yet),
            "u-provider": episode_to_record(synced_provider),
            "u-current-asr": episode_to_record(synced_current_asr),
            "u-stale-asr": episode_to_record(stale_asr),
            "u-pending": episode_to_record(never_transcribed),
            "u-fallback": episode_to_record(no_served_duration),
        },
    )

    pending = pending_transcribe_work(tmp_path, "src", asr_enabled=True, asr_pipeline_version="3")
    # Excluded: no hosted audio yet, a synced provider transcript, and a synced ASR transcript
    # already on the current pipeline version. Included: the stale-pipeline ASR transcript (redo)
    # and the two never-transcribed episodes (one via audio_duration_served, one via the
    # ep.duration fallback) — 2400 + 3600 + 600.
    assert pending == 2400.0 + 3600.0 + 600.0


def test_pending_transcribe_work_is_zero_when_asr_disabled(tmp_path):
    pending_ep = _ep("g-pending")
    pending_ep.uid = "u-pending"
    pending_ep.audio_key = "audio/src/u-pending.m4a"
    pending_ep.audio_spec_hash = "spec"
    pending_ep.hosted_audio_url = "https://cdn/u-pending.m4a"
    pending_ep.audio_duration_served = 3600.0
    save_records(tmp_path, "src", {"u-pending": episode_to_record(pending_ep)})

    assert (
        pending_transcribe_work(tmp_path, "src", asr_enabled=False, asr_pipeline_version="3") == 0
    )


def test_pending_transcribe_work_is_zero_for_unknown_source(tmp_path):
    assert (
        pending_transcribe_work(tmp_path, "no-such-src", asr_enabled=True, asr_pipeline_version="3")
        == 0
    )


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


# --- cross-lane write isolation (review/12 §H6) ----------------------------------------


def test_protected_blocks_for_lane():
    # A lane preserves the artifact block(s) it does NOT own from the freshest remote.
    assert protected_blocks_for_lane("audio") == frozenset({"transcript"})
    assert protected_blocks_for_lane("transcribe") == frozenset({"audio"})
    assert protected_blocks_for_lane("align") == frozenset({"audio"})
    # A full/unscoped run (None) or an unknown lane owns every artifact → protects nothing.
    assert protected_blocks_for_lane(None) == frozenset()
    assert protected_blocks_for_lane("mystery") == frozenset()


def test_merge_preserving_foreign_asr_lane_keeps_remote_audio():
    # The reported regression: an ASR (transcribe) shard pulled state before an audio run wrote a
    # new hosted-audio URL, so its local audio block is the stale start-of-run snapshot. On push it
    # must keep the remote's newer audio and write only its own fresh transcript.
    remote = {"u1": {"uid": "u1", "title": "old", "audio": {"url": "NEW", "key": "kNEW"}}}
    local = {
        "u1": {
            "uid": "u1",
            "title": "fresh",
            "audio": {"url": None, "key": None},
            "transcript": {"key": "t1", "url": "T", "synced": True},
        }
    }
    merged = merge_preserving_foreign(remote, local, protected_blocks_for_lane("transcribe"))
    assert merged["u1"]["audio"] == {"url": "NEW", "key": "kNEW"}  # remote audio preserved
    assert merged["u1"]["transcript"]["synced"] is True  # local transcript written
    assert merged["u1"]["title"] == "fresh"  # provider/render fields are fresh (local wins)


def test_merge_preserving_foreign_audio_lane_keeps_remote_transcript():
    # Symmetric: an audio run finishing after an ASR run must not erase the fresh transcript.
    remote = {"u1": {"uid": "u1", "audio": {"url": "OLD"}, "transcript": {"key": "tNEW"}}}
    local = {"u1": {"uid": "u1", "audio": {"url": "NEW"}, "transcript": None}}
    merged = merge_preserving_foreign(remote, local, protected_blocks_for_lane("audio"))
    assert merged["u1"]["transcript"] == {"key": "tNEW"}  # remote transcript preserved
    assert merged["u1"]["audio"]["url"] == "NEW"  # local audio written (this lane owns it)


def test_merge_preserving_foreign_unions_new_and_remote_only_uids():
    remote = {"r": {"uid": "r", "audio": {"url": "R"}}}
    local = {"newbie": {"uid": "newbie", "audio": {"url": "L"}}}
    merged = merge_preserving_foreign(remote, local, frozenset({"audio"}))
    assert set(merged) == {"r", "newbie"}  # remote-only uid kept; new local uid added
    assert merged["r"]["audio"]["url"] == "R"
    assert merged["newbie"]["audio"]["url"] == "L"  # taken whole (no remote counterpart to protect)


def test_merge_preserving_foreign_never_drops_a_block_remote_lacks():
    # Protected block, but remote lacks it → keep local's so an artifact is never lost.
    remote = {"u1": {"uid": "u1", "audio": {"url": "NEW"}}}  # no transcript on remote
    local = {"u1": {"uid": "u1", "audio": {"url": "OLD"}, "transcript": {"key": "t1"}}}
    merged = merge_preserving_foreign(remote, local, frozenset({"transcript"}))
    assert merged["u1"]["transcript"] == {"key": "t1"}  # local transcript kept (remote had none)
    assert merged["u1"]["audio"]["url"] == "OLD"  # audio not protected here → local value


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
