"""Tests for stable identity, split hashes, the record store, and legacy migration."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from citypods.models import City, Episode
from citypods.records import (
    _capped_exponential_backoff,
    assign_uids,
    audio_object_key,
    audio_spec_hash,
    episode_to_record,
    estimate_audio_shard_work,
    estimate_transcribe_shard_work,
    feed_content_hash,
    load_records,
    merge_persisted,
    merge_preserving_foreign,
    merge_records,
    migrate_legacy_manifests,
    pending_transcribe_items,
    protected_blocks_for_lane,
    prune_archive,
    record_to_episode,
    save_records,
    shard_assignment,
    source_key,
    transcript_media_hash,
    transcript_timeout_backoff_until,
)
from citypods.timeline import Segment, Timeline


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


def test_audio_spec_hash_prefers_source_chapters_over_stale_served_copy():
    from citypods.timeline import Segment, Timeline

    ep = _ep("g1")
    ep.uid = "u1"
    ep.source_chapters = [{"start": 600, "title": "Agenda"}]
    ep.chapters = [{"start": 9999, "title": "Stale served"}]
    ep.chapters_basis = "served:old"
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

    stale_hash = audio_spec_hash(ep, max_kbps=96)
    ep.chapters = [{"start": 300.0, "title": "Agenda"}]
    expected_hash = audio_spec_hash(ep, max_kbps=96)
    assert stale_hash == expected_hash


def test_transcript_media_hash_ignores_audio_only_inputs_but_tracks_timeline():
    ep = _ep("g1")
    ep.uid = "u1"
    base = transcript_media_hash(ep)
    ep.chapters = [{"start": 0, "title": "x"}]
    ep.audio_rebuild = "force-audio-only"
    assert transcript_media_hash(ep) == base

    ep.timeline = Timeline(
        version="trim-v1",
        segments=[
            Segment(
                kind="source",
                source_id="s0",
                served_start=0,
                served_end=10,
                source_start=5,
                source_end=15,
            )
        ],
    )
    assert transcript_media_hash(ep) != base


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


def test_record_to_episode_prefers_canonical_duration_helpers():
    rec = {
        "uid": "u1",
        "provider_guid": "g1",
        "title": "Meeting",
        "published": "2026-07-01T00:00:00+00:00",
        "video_url": "https://x/g1.mp4",
        "source_duration_seconds": "601.2",
        "served_duration_seconds": "599.5",
        "audio": {"duration_served": 400.0},
    }

    ep = record_to_episode(rec)

    assert ep.source_duration_seconds == pytest.approx(601.2)
    assert ep.duration == 601
    assert ep.served_duration_seconds == pytest.approx(599.5)
    assert ep.audio_duration_served == pytest.approx(599.5)


def test_merge_persisted_keeps_fresh_provider_source_duration():
    fresh = _ep("g-source-fresh")
    fresh.uid = "u-source-fresh"
    fresh.duration = 900
    fresh.source_duration_seconds = 900.0

    rec = {
        "uid": fresh.uid,
        "duration": 1200,
        "source_duration_seconds": 1200.0,
    }

    merge_persisted([fresh], {fresh.uid: rec})

    assert fresh.source_duration_seconds == pytest.approx(900.0)
    assert fresh.duration == 900


def test_merge_persisted_restores_canonical_served_duration():
    fresh = _ep("g-served-restore")
    fresh.uid = "u-served-restore"

    rec = {
        "uid": fresh.uid,
        "served_duration_seconds": 1200.5,
        "audio": {"key": "audio/u-served-restore.m4a"},
    }

    merge_persisted([fresh], {fresh.uid: rec})

    assert fresh.served_duration_seconds == pytest.approx(1200.5)
    assert fresh.audio_duration_served == pytest.approx(1200.5)


def test_estimate_audio_shard_work_weights_pending_encodes_by_duration(tmp_path):
    done = _ep("g-done")
    done.uid = "u-done"
    done.hosted_audio_url = "https://cdn/u-done.m4a"
    done.audio_spec_hash = audio_spec_hash(done, max_kbps=96)

    stale_spec = _ep("g-stale")
    stale_spec.uid = "u-stale"
    stale_spec.duration = 3600
    stale_spec.hosted_audio_url = "https://cdn/u-stale.m4a"
    stale_spec.audio_spec_hash = "an-old-spec-that-no-longer-matches"

    backing_off = _ep("g-backoff")
    backing_off.uid = "u-backoff"
    backing_off.materialize_attempts = 1
    backing_off.materialize_last_attempt = datetime.now(UTC).isoformat()

    never_attempted = _ep("g-new")
    never_attempted.uid = "u-new"
    never_attempted.duration = 1800

    errored = _ep("g-errored")
    errored.uid = "u-errored"
    errored.duration = 900
    errored.hosted_audio_url = "https://cdn/u-errored.m4a"
    errored.audio_spec_hash = audio_spec_hash(errored, max_kbps=96)
    errored.materialize_error = "duration"

    save_records(
        tmp_path,
        "src",
        {
            "u-done": episode_to_record(done),
            "u-stale": episode_to_record(stale_spec),
            "u-backoff": episode_to_record(backing_off),
            "u-new": episode_to_record(never_attempted),
            "u-errored": episode_to_record(errored),
        },
    )

    weight = estimate_audio_shard_work(
        tmp_path, "src", extract_audio=True, max_kbps=96, loudness_profile="", processing_profile=""
    )
    # The stale-spec, never-attempted, and current-spec-but-errored episodes still need an encode:
    # "done" is current and healthy, while "backoff" won't be retried this run.
    assert weight == 6300


def test_estimate_audio_shard_work_skips_direct_episodes_when_extraction_disabled(tmp_path):
    direct = _ep("g-direct")
    direct.uid = "u-direct"
    direct.media_kind = "direct"
    direct.duration = 1200
    save_records(tmp_path, "src", {"u-direct": episode_to_record(direct)})

    assert (
        estimate_audio_shard_work(
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
        estimate_audio_shard_work(
            tmp_path,
            "src",
            extract_audio=True,
            max_kbps=96,
            loudness_profile="",
            processing_profile="",
        )
        == 1200
    )


def test_estimate_audio_shard_work_is_zero_for_unknown_source(tmp_path):
    assert (
        estimate_audio_shard_work(
            tmp_path,
            "no-such-src",
            extract_audio=False,
            max_kbps=96,
            loudness_profile="",
            processing_profile="",
        )
        == 0
    )


def test_estimate_audio_shard_work_uses_timeline_and_source_average(tmp_path):
    from citypods.timeline import Segment, Timeline

    planned = _ep("g-planned")
    planned.uid = "u-planned"
    planned.duration = 7200
    planned.timeline = Timeline(
        version="trimmed",
        segments=(
            Segment(
                served_start=0,
                served_end=1800,
                kind="source",
                source_id="s0",
                source_start=0,
                source_end=1800,
            ),
        ),
    )
    unknown = _ep("g-unknown")
    unknown.uid = "u-unknown"

    save_records(
        tmp_path,
        "src",
        {
            "u-planned": episode_to_record(planned),
            "u-unknown": episode_to_record(unknown),
        },
    )

    assert (
        estimate_audio_shard_work(
            tmp_path,
            "src",
            extract_audio=True,
            max_kbps=96,
            loudness_profile="",
            processing_profile="",
        )
        == 3600
    )


def test_estimate_audio_shard_work_gives_withheld_media_only_recheck_cost(tmp_path):
    from citypods.availability import MediaAvailability
    from citypods.records import AUDIO_WITHHELD_RECHECK_WEIGHT_SECONDS

    available = _ep("g-available")
    available.uid = "u-available"
    available.duration = 3600
    available.media_availability = MediaAvailability(state="available")

    recovered = _ep("g-recovered")
    recovered.uid = "u-recovered"
    recovered.duration = 1800
    recovered.media_availability = MediaAvailability(state="recovered")

    withheld = _ep("g-withheld")
    withheld.uid = "u-withheld"
    withheld.duration = 8 * 3600
    withheld.media_availability = MediaAvailability(state="confirmed_empty")

    save_records(
        tmp_path,
        "src",
        {
            "u-available": episode_to_record(available),
            "u-recovered": episode_to_record(recovered),
            "u-withheld": episode_to_record(withheld),
        },
    )

    assert (
        estimate_audio_shard_work(
            tmp_path,
            "src",
            extract_audio=True,
            max_kbps=96,
            loudness_profile="",
            processing_profile="",
        )
        == 5400 + AUDIO_WITHHELD_RECHECK_WEIGHT_SECONDS
    )


def test_estimate_audio_shard_work_handles_extreme_backoff_attempts(tmp_path):
    pending = _ep("g-pending")
    pending.uid = "u-pending"
    pending.duration = 1800

    backing_off = _ep("g-backoff")
    backing_off.uid = "u-backoff"
    backing_off.duration = 7200
    backing_off.materialize_attempts = 1 << 30
    backing_off.materialize_last_attempt = datetime.now(UTC).isoformat()

    save_records(
        tmp_path,
        "src",
        {
            "u-pending": episode_to_record(pending),
            "u-backoff": episode_to_record(backing_off),
        },
    )

    assert (
        estimate_audio_shard_work(
            tmp_path,
            "src",
            extract_audio=True,
            max_kbps=96,
            loudness_profile="",
            processing_profile="",
        )
        == 1800
    )


def test_transcribe_shard_work_separates_local_duration_from_blocked_work(tmp_path):
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

    oversized = _ep("g-oversized")
    oversized.uid = "u-oversized"
    oversized.audio_key = "audio/src/u-oversized.m4a"
    oversized.audio_spec_hash = "spec"
    oversized.hosted_audio_url = "https://cdn/u-oversized.m4a"
    oversized.audio_duration_served = 5 * 3600.0

    unknown_duration = _ep("g-unknown")
    unknown_duration.uid = "u-unknown"
    unknown_duration.audio_key = "audio/src/u-unknown.m4a"
    unknown_duration.audio_spec_hash = "spec"
    unknown_duration.hosted_audio_url = "https://cdn/u-unknown.m4a"

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
            "u-oversized": episode_to_record(oversized),
            "u-unknown": episode_to_record(unknown_duration),
        },
    )

    estimate = estimate_transcribe_shard_work(
        tmp_path,
        "src",
        asr_enabled=True,
        asr_pipeline_version="3",
        local_max_duration_hours=4,
    )
    # Excluded: no hosted audio yet, a synced provider transcript, and a synced ASR transcript
    # already on the current pipeline version. Included: the stale-pipeline ASR transcript (redo)
    # and locally eligible never-transcribed episodes. The >4h item contributes only a cheap
    # blocked/defer cost; the unknown-duration item is weighted by the average *known* local
    # duration in this source ((2400+3600+600)/3 = 2200), not a flat 2h fallback.
    known_local_seconds = 2400.0 + 3600.0 + 600.0
    assert estimate.local_items == 4
    assert estimate.local_seconds == known_local_seconds + known_local_seconds / 3
    assert estimate.blocked_items == 1
    assert estimate.dispatch_items == 0
    assert estimate.inflight_items == 0
    assert estimate.shard_weight() == estimate.local_seconds + 1.0

    no_ceiling = estimate_transcribe_shard_work(
        tmp_path,
        "src",
        asr_enabled=True,
        asr_pipeline_version="3",
        local_max_duration_hours=0,
    )
    # With no ceiling the 5h item routes local too, so it joins the known-duration set and the
    # unknown item is now averaged against four known durations ((2400+3600+600+18000)/4 = 6150).
    no_ceiling_known = 2400.0 + 3600.0 + 600.0 + 5 * 3600.0
    assert no_ceiling.local_items == 5
    assert no_ceiling.local_seconds == no_ceiling_known + no_ceiling_known / 4
    assert no_ceiling.blocked_items == 0


def test_pending_transcribe_items_matches_aggregate_and_excludes_non_pending(tmp_path):
    # Per-episode plan input must agree with the aggregate estimate: same pending uids, and the
    # per-uid weights sum to the source's shard weight (review/18 §3.1).
    synced = _ep("done")
    synced.uid = "u-done"
    synced.audio_key = "audio/src/u-done.m4a"
    synced.audio_spec_hash = "spec"
    synced.hosted_audio_url = "https://cdn/u-done.m4a"
    synced.audio_duration_served = 1200.0
    synced.transcript_key = "transcripts/src/u-done-asr-recipe.vtt"
    synced.transcript_synced = True
    synced.transcript_pipeline_version = "3"

    pending = _ep("pend")
    pending.uid = "u-pend"
    pending.audio_key = "audio/src/u-pend.m4a"
    pending.audio_spec_hash = "spec"
    pending.hosted_audio_url = "https://cdn/u-pend.m4a"
    pending.audio_duration_served = 3600.0

    oversized = _ep("big")
    oversized.uid = "u-big"
    oversized.audio_key = "audio/src/u-big.m4a"
    oversized.audio_spec_hash = "spec"
    oversized.hosted_audio_url = "https://cdn/u-big.m4a"
    oversized.audio_duration_served = 5 * 3600.0

    no_audio = _ep("noaud")
    no_audio.uid = "u-noaud"  # not transcribable this run → never pending

    save_records(
        tmp_path,
        "src",
        {
            "u-done": episode_to_record(synced),
            "u-pend": episode_to_record(pending),
            "u-big": episode_to_record(oversized),
            "u-noaud": episode_to_record(no_audio),
        },
    )
    kwargs = dict(asr_enabled=True, asr_pipeline_version="3", local_max_duration_hours=4)
    items = pending_transcribe_items(tmp_path, "src", **kwargs)
    estimate = estimate_transcribe_shard_work(tmp_path, "src", **kwargs)

    assert {uid for uid, _ in items} == {"u-pend", "u-big"}  # synced + no-audio excluded
    assert sum(w for _, w in items) == estimate.shard_weight()
    weights = dict(items)
    assert weights["u-pend"] == 3600.0  # local: recording-duration proxy
    assert weights["u-big"] == 1.0  # blocked (>4h): minimal defer cost


def test_pending_transcribe_items_empty_when_asr_disabled(tmp_path):
    ep = _ep("x")
    ep.uid = "u-x"
    ep.audio_key = "audio/src/u-x.m4a"
    ep.audio_spec_hash = "spec"
    ep.hosted_audio_url = "https://cdn/u-x.m4a"
    ep.audio_duration_served = 600.0
    save_records(tmp_path, "src", {"u-x": episode_to_record(ep)})
    assert (
        pending_transcribe_items(
            tmp_path, "src", asr_enabled=False, asr_pipeline_version="3", local_max_duration_hours=4
        )
        == []
    )


def test_transcribe_shard_work_accepts_canonical_gpu_route_classification(tmp_path):
    local = _ep("g-local")
    local.uid = "u-local"
    local.audio_key = "audio/src/u-local.m4a"
    local.audio_spec_hash = "spec"
    local.hosted_audio_url = "https://cdn/u-local.m4a"
    local.audio_duration_served = 3600.0

    dispatch = _ep("g-dispatch")
    dispatch.uid = "u-dispatch"
    dispatch.audio_key = "audio/src/u-dispatch.m4a"
    dispatch.audio_spec_hash = "spec"
    dispatch.hosted_audio_url = "https://cdn/u-dispatch.m4a"
    dispatch.audio_duration_served = 8 * 3600.0

    inflight = _ep("g-inflight")
    inflight.uid = "u-inflight"
    inflight.audio_key = "audio/src/u-inflight.m4a"
    inflight.audio_spec_hash = "spec"
    inflight.hosted_audio_url = "https://cdn/u-inflight.m4a"
    inflight.audio_duration_served = 6 * 3600.0

    save_records(
        tmp_path,
        "src",
        {
            "u-local": episode_to_record(local),
            "u-dispatch": episode_to_record(dispatch),
            "u-inflight": episode_to_record(inflight),
        },
    )

    routes = {"u-local": "local", "u-dispatch": "dispatch", "u-inflight": "inflight"}
    estimate = estimate_transcribe_shard_work(
        tmp_path,
        "src",
        asr_enabled=True,
        asr_pipeline_version="3",
        local_max_duration_hours=4,
        route_classifier=lambda ep, _duration: routes[ep.uid],
    )

    assert estimate.local_seconds == 3600.0
    assert estimate.local_items == 1
    assert estimate.dispatch_items == 1
    assert estimate.blocked_items == 0
    assert estimate.inflight_items == 1
    assert estimate.shard_weight() == 3660.0


def test_transcribe_shard_work_treats_timeout_backoff_as_blocked(tmp_path):
    ep = _ep("g-timeout")
    ep.uid = "u-timeout"
    ep.audio_key = "audio/src/u-timeout.m4a"
    ep.audio_spec_hash = "spec"
    ep.hosted_audio_url = "https://cdn/u-timeout.m4a"
    ep.audio_duration_served = 3 * 3600.0
    ep.transcript_timeout_attempts = 1
    ep.transcript_timeout_last_attempt = datetime.now(UTC).isoformat()
    save_records(tmp_path, "src", {ep.uid: episode_to_record(ep)})

    estimate = estimate_transcribe_shard_work(
        tmp_path,
        "src",
        asr_enabled=True,
        asr_pipeline_version="3",
        local_max_duration_hours=4,
    )

    assert estimate.local_items == 0
    assert estimate.local_seconds == 0
    assert estimate.blocked_items == 1
    assert estimate.shard_weight() == 1


def test_transcribe_shard_work_skips_audio_error_episodes(tmp_path):
    """An episode whose audio failed to materialize is not transcribable this run — it must not
    count toward the shard weight or the backlog (run #25: ~600 errored episodes inflated both)."""
    healthy = _ep("g-ok")
    healthy.uid = "u-ok"
    healthy.audio_key = "audio/src/u-ok.m4a"
    healthy.audio_spec_hash = "spec"
    healthy.hosted_audio_url = "https://cdn/u-ok.m4a"
    healthy.audio_duration_served = 1800.0

    errored = _ep("g-err")
    errored.uid = "u-err"
    errored.audio_key = "audio/src/u-err.m4a"
    errored.audio_spec_hash = "spec"
    errored.hosted_audio_url = "https://cdn/u-err.m4a"  # bytes uploaded but flagged broken
    errored.materialize_error = "error"

    save_records(
        tmp_path,
        "src",
        {"u-ok": episode_to_record(healthy), "u-err": episode_to_record(errored)},
    )

    estimate = estimate_transcribe_shard_work(
        tmp_path,
        "src",
        asr_enabled=True,
        asr_pipeline_version="3",
        local_max_duration_hours=4,
    )

    assert estimate.local_items == 1
    assert estimate.local_seconds == 1800.0


def test_transcribe_shard_work_falls_back_to_constant_when_no_known_durations(tmp_path):
    """With no known durations to average against, unknown-duration locals use the constant
    fallback so an all-unknown source still registers as work rather than weighing ~0."""
    unknown = _ep("g-unknown")
    unknown.uid = "u-unknown"
    unknown.audio_key = "audio/src/u-unknown.m4a"
    unknown.audio_spec_hash = "spec"
    unknown.hosted_audio_url = "https://cdn/u-unknown.m4a"
    save_records(tmp_path, "src", {"u-unknown": episode_to_record(unknown)})

    estimate = estimate_transcribe_shard_work(
        tmp_path,
        "src",
        asr_enabled=True,
        asr_pipeline_version="3",
        local_max_duration_hours=4,
        unknown_local_seconds=1234.0,
    )

    assert estimate.local_items == 1
    assert estimate.local_seconds == 1234.0


def test_transcribe_shard_work_is_empty_when_asr_disabled(tmp_path):
    pending_ep = _ep("g-pending")
    pending_ep.uid = "u-pending"
    pending_ep.audio_key = "audio/src/u-pending.m4a"
    pending_ep.audio_spec_hash = "spec"
    pending_ep.hosted_audio_url = "https://cdn/u-pending.m4a"
    pending_ep.audio_duration_served = 3600.0
    save_records(tmp_path, "src", {"u-pending": episode_to_record(pending_ep)})

    assert (
        estimate_transcribe_shard_work(
            tmp_path,
            "src",
            asr_enabled=False,
            asr_pipeline_version="3",
            local_max_duration_hours=4,
        ).shard_weight()
        == 0
    )


def test_transcribe_shard_work_is_empty_for_unknown_source(tmp_path):
    assert (
        estimate_transcribe_shard_work(
            tmp_path,
            "no-such-src",
            asr_enabled=True,
            asr_pipeline_version="3",
            local_max_duration_hours=4,
        ).shard_weight()
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
    ep.transcript_timeout_attempts = 3
    ep.transcript_timeout_last_attempt = "2026-06-02T00:00:00+00:00"
    ep.provider_transcript = {
        "known_good": {
            "url": "https://city.example/transcript.pdf",
            "key": "transcripts/src/u1-provider-abc.pdf",
            "spec_hash": "abc",
            "format": "pdf",
            "basis": "source:s0",
            "synced": False,
            "confidence": None,
        }
    }
    ep.speakers_key = "transcripts/src/u1-provider-diarize-abc.speakers.json"
    ep.speakers_url = "https://cdn/transcripts/src/u1-provider-diarize-abc.speakers.json"
    ep.speakers_spec_hash = "speaker-spec"
    ep.speakers_format = "json"
    ep.speakers_synced = True
    ep.speakers_confidence = 0.9
    ep.speakers_pipeline_version = "1"

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
        "transcript_timeout_attempts",
        "transcript_timeout_last_attempt",
        "provider_transcript",
        "speakers_key",
        "speakers_url",
        "speakers_spec_hash",
        "speakers_format",
        "speakers_synced",
        "speakers_confidence",
        "speakers_pipeline_version",
    ):
        assert getattr(back, attr) == getattr(ep, attr), attr


def test_record_to_episode_timeline_tolerates_unknown_segment_keys():
    # CR2-CP-27: a legacy/newer record's segment dict may carry keys this version's Segment
    # doesn't declare — filter like the sibling _source_media_from_dict/_availability_from_rec
    # rebuilders, rather than raising and failing episode load entirely.
    rec = {
        "provider_guid": "g1",
        "timeline": {
            "version": "v1",
            "segments": [
                {
                    "served_start": 0,
                    "served_end": 10,
                    "kind": "source",
                    "source_id": "s0",
                    "source_start": 0,
                    "source_end": 10,
                    "from_a_future_schema_version": "ignore me",
                }
            ],
        },
    }
    ep = record_to_episode(rec)
    assert ep.timeline is not None
    assert ep.timeline.version == "v1"
    assert ep.timeline.segments[0].source_id == "s0"


def test_record_to_episode_timeline_tolerates_missing_version_key():
    rec = {
        "provider_guid": "g1",
        "timeline": {"segments": []},
    }
    ep = record_to_episode(rec)
    assert ep.timeline is not None
    assert ep.timeline.version == ""


def test_record_to_episode_timeline_tolerates_non_dict_segment_entry():
    # CodeRabbit (PR #877): a non-dict item in the stored segments list (e.g. corrupted JSON)
    # raised AttributeError at s.items(), escaping the (TypeError, ValueError) catch and aborting
    # the whole episode load instead of falling back to None like other malformed-timeline cases.
    rec = {
        "provider_guid": "g1",
        "timeline": {
            "version": "v1",
            "segments": [
                {
                    "served_start": 0,
                    "served_end": 10,
                    "kind": "source",
                    "source_id": "s0",
                    "source_start": 0,
                    "source_end": 10,
                },
                "not-a-dict",
            ],
        },
    }
    ep = record_to_episode(rec)
    assert ep.timeline is not None
    assert len(ep.timeline.segments) == 1  # the malformed entry is dropped, not fatal
    assert ep.timeline.segments[0].source_id == "s0"


def test_record_to_episode_timeline_falls_back_to_none_on_inconsistent_segment():
    # A segment that's internally inconsistent for its own kind (Segment.__post_init__,
    # CR2-CP-13) makes this episode's timeline unrecoverable from the record -- fall back to
    # None (identity/re-plan) rather than raising and aborting the whole episode-list load.
    rec = {
        "provider_guid": "g1",
        "timeline": {
            "version": "v1",
            "segments": [{"served_start": 0, "served_end": 10, "kind": "source"}],
        },
    }
    ep = record_to_episode(rec)
    assert ep.timeline is None


def test_provider_transcript_registry_persists_and_merges():
    ep = _ep("g-provider-transcript")
    ep.uid = "u-provider-transcript"
    ep.provider_transcript = {
        "known_good": {
            "url": "https://city.example/transcript.pdf",
            "key": "transcripts/src/u-provider-transcript-provider-abc.pdf",
            "spec_hash": "abc",
            "format": "pdf",
            "basis": "source:s0",
            "synced": False,
            "confidence": 0.8,
        },
        "candidate": {
            "url": "https://city.example/transcript.pdf",
            "key": "transcripts/src/u-provider-transcript-provider-def.pdf",
            "spec_hash": "def",
            "format": "pdf",
            "basis": "source:s0",
            "synced": False,
            "confidence": None,
        },
    }

    rec = episode_to_record(ep)
    assert rec["provider_transcript"] == ep.provider_transcript
    assert record_to_episode(rec).provider_transcript == ep.provider_transcript

    fresh = _ep("g-provider-transcript")
    fresh.uid = ep.uid
    merge_persisted([fresh], {ep.uid: rec})
    assert fresh.provider_transcript == ep.provider_transcript


def test_timeout_backoff_persists_without_a_transcript_artifact():
    ep = _ep("g-timeout")
    ep.uid = "u-timeout"
    ep.transcript_timeout_attempts = 1
    ep.transcript_timeout_last_attempt = "2026-06-20T12:00:00+00:00"

    rec = episode_to_record(ep)
    assert rec["transcript"]["key"] is None
    back = record_to_episode(rec)
    assert back.transcript_key is None
    assert back.transcript_timeout_attempts == 1
    assert back.transcript_timeout_last_attempt == "2026-06-20T12:00:00+00:00"


def test_timeout_backoff_accepts_legacy_naive_timestamp_as_utc():
    ep = _ep("g-timeout-naive")
    ep.transcript_timeout_attempts = 1
    ep.transcript_timeout_last_attempt = "2026-06-20T12:00:00"

    assert transcript_timeout_backoff_until(ep) == datetime(2026, 6, 21, 12, tzinfo=UTC)


def test_capped_exponential_backoff_does_not_clamp_fractional_caps_early():
    assert _capped_exponential_backoff(timedelta(days=1), timedelta(hours=36), 1) == timedelta(
        days=1
    )
    assert _capped_exponential_backoff(timedelta(days=1), timedelta(hours=36), 2) == timedelta(
        hours=36
    )


def test_timeout_backoff_caps_extreme_attempt_counts():
    ep = _ep("g-timeout-huge")
    ep.transcript_timeout_attempts = 1 << 30
    ep.transcript_timeout_last_attempt = "2026-06-20T12:00:00+00:00"

    assert transcript_timeout_backoff_until(ep) == datetime(2026, 7, 20, 12, tzinfo=UTC)


def test_timeout_attempts_coerce_malformed_persisted_values_to_zero():
    ep = _ep("g-timeout-malformed")
    ep.uid = "u-timeout-malformed"
    rec = episode_to_record(ep)
    rec["transcript"] = {"timeout_attempts": "not-an-integer"}

    restored = record_to_episode(rec)
    assert restored.transcript_timeout_attempts == 0

    fresh = _ep("g-timeout-malformed")
    fresh.uid = "u-timeout-malformed"
    merge_persisted([fresh], {fresh.uid: rec})
    assert fresh.transcript_timeout_attempts == 0


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
    # A lane preserves the artifact block(s) it does NOT own from the freshest remote. The audio
    # lane owns both the audio bytes and the media-availability verdict it derives (H16 PR3).
    assert protected_blocks_for_lane("audio") == frozenset(
        {"transcript", "provider_transcript", "speakers", "integrity"}
    )
    assert protected_blocks_for_lane("transcribe") == frozenset(
        {"audio", "speakers", "media_availability", "integrity"}
    )
    assert protected_blocks_for_lane("align") == frozenset(
        {"audio", "speakers", "media_availability", "integrity"}
    )
    assert protected_blocks_for_lane("diarize") == frozenset(
        {"audio", "transcript", "media_availability", "integrity"}
    )
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


def test_merge_preserving_foreign_keeps_remote_decoded_plan_over_stale_audio_snapshot():
    remote = {
        "u1": {
            "uid": "u1",
            "title": "old title",
            "sources": [{"id": "s0", "duration": 100.0, "duration_basis": "decoded"}],
            "timeline": {"version": "silence:2+swagit-concat:1", "segments": []},
            "chapters": [{"title": "remote chapter", "start": 1.0}],
            "chapters_basis": "served:silence:2+swagit-concat:1",
            "audio": {"url": "REMOTE", "key": "decoded-key", "spec_hash": "decoded-spec"},
            "transcript": {"key": "remote-transcript"},
        },
        "u2": {
            "uid": "u2",
            "sources": [{"id": "s0", "duration": 200.0, "duration_basis": "decoded"}],
            "timeline": {"version": "silence:2+swagit-concat:1", "segments": []},
            "audio": {"url": "REMOTE2", "key": "decoded-key-2", "spec_hash": "decoded-spec-2"},
        },
    }
    local = {
        "u1": {
            "uid": "u1",
            "title": "fresh title",
            "sources": [{"id": "s0", "duration": 101.0, "duration_basis": "container"}],
            "timeline": {"version": "silence:2+swagit-concat:1", "segments": []},
            "chapters": [{"title": "local stale chapter", "start": 2.0}],
            "chapters_basis": "served:silence:2+swagit-concat:1",
            "audio": {"url": "LOCAL", "key": "container-key", "spec_hash": "container-spec"},
            "transcript": {"key": "local-stale-transcript"},
        },
        "u2": {
            "uid": "u2",
            "sources": [],
            "timeline": {"version": "silence:2+swagit-concat:1", "segments": []},
            "chapters": [{"title": "stale local chapter", "start": 3.0}],
            "audio": {"url": "LOCAL2", "key": "missing-source-key", "spec_hash": "missing-spec"},
            "transcript": {"key": "stale-local-transcript"},
        },
    }

    merged = merge_preserving_foreign(remote, local, protected_blocks_for_lane("audio"))

    assert merged["u1"]["title"] == "fresh title"
    assert merged["u1"]["sources"] == remote["u1"]["sources"]
    assert merged["u1"]["timeline"] == remote["u1"]["timeline"]
    assert merged["u1"]["chapters"] == remote["u1"]["chapters"]
    assert merged["u1"]["audio"] == remote["u1"]["audio"]
    assert merged["u1"]["transcript"] == remote["u1"]["transcript"]
    assert merged["u2"]["sources"] == remote["u2"]["sources"]
    assert merged["u2"]["audio"] == remote["u2"]["audio"]
    assert "chapters" not in merged["u2"]
    assert "transcript" not in merged["u2"]


def test_merge_preserving_foreign_writes_local_decoded_plan_over_remote_container_plan():
    remote = {
        "u1": {
            "uid": "u1",
            "sources": [{"id": "s0", "duration": 101.0, "duration_basis": "container"}],
            "timeline": {"version": "silence:2+swagit-concat:1", "segments": []},
            "audio": {"url": "REMOTE", "key": "container-key", "spec_hash": "container-spec"},
        }
    }
    local = {
        "u1": {
            "uid": "u1",
            "sources": [{"id": "s0", "duration": 100.0, "duration_basis": "decoded"}],
            "timeline": {"version": "silence:2+swagit-concat:1", "segments": []},
            "audio": {"url": "LOCAL", "key": "decoded-key", "spec_hash": "decoded-spec"},
        }
    }

    merged = merge_preserving_foreign(remote, local, protected_blocks_for_lane("audio"))

    assert merged["u1"]["sources"] == local["u1"]["sources"]
    assert merged["u1"]["timeline"] == local["u1"]["timeline"]
    assert merged["u1"]["audio"] == local["u1"]["audio"]


def test_merge_preserving_foreign_audio_lane_keeps_remote_provider_transcript():
    remote = {
        "u1": {
            "uid": "u1",
            "provider_transcript": {"known_good": {"key": "provider", "confidence": 0.8}},
        }
    }
    local = {"u1": {"uid": "u1", "audio": {"url": "NEW"}, "provider_transcript": None}}
    merged = merge_preserving_foreign(remote, local, protected_blocks_for_lane("audio"))
    assert merged["u1"]["provider_transcript"] == {
        "known_good": {"key": "provider", "confidence": 0.8}
    }
    assert merged["u1"]["audio"]["url"] == "NEW"


def test_merge_preserving_foreign_transcribe_lane_keeps_remote_availability():
    # An ASR (transcribe) shard does not own the audio-derived availability verdict; on push it must
    # preserve the freshest remote one rather than regress it with its stale snapshot (H16 PR3).
    remote = {"u1": {"uid": "u1", "media_availability": {"state": "confirmed_empty"}}}
    local = {"u1": {"uid": "u1", "media_availability": None, "transcript": {"key": "t1"}}}
    merged = merge_preserving_foreign(remote, local, protected_blocks_for_lane("transcribe"))
    assert merged["u1"]["media_availability"] == {"state": "confirmed_empty"}
    assert merged["u1"]["transcript"] == {"key": "t1"}


def test_merge_preserving_foreign_transcribe_lane_keeps_remote_speakers():
    remote = {"u1": {"uid": "u1", "speakers": {"key": "speaker-json", "synced": True}}}
    local = {"u1": {"uid": "u1", "speakers": None, "transcript": {"key": "t1"}}}
    merged = merge_preserving_foreign(remote, local, protected_blocks_for_lane("transcribe"))
    assert merged["u1"]["speakers"] == {"key": "speaker-json", "synced": True}
    assert merged["u1"]["transcript"] == {"key": "t1"}


def test_merge_preserving_foreign_diarize_lane_keeps_remote_transcript():
    remote = {"u1": {"uid": "u1", "transcript": {"key": "provider-align", "synced": True}}}
    local = {"u1": {"uid": "u1", "transcript": None, "speakers": {"key": "speaker-json"}}}
    merged = merge_preserving_foreign(remote, local, protected_blocks_for_lane("diarize"))
    assert merged["u1"]["transcript"] == {"key": "provider-align", "synced": True}
    assert merged["u1"]["speakers"] == {"key": "speaker-json"}


def test_availability_block_round_trips():
    from citypods.availability import CONFIRMED_EMPTY, MediaAvailability

    ep = _ep("g")
    ep.media_availability = MediaAvailability(
        state=CONFIRMED_EMPTY,
        reason="silence confirmed",
        source_fingerprint="abc123",
        profile="noise=-40",
        silent_confirmations=2,
    )
    back = record_to_episode(episode_to_record(ep))
    assert back.media_availability == ep.media_availability


def test_availability_absent_on_unclassified_record():
    ep = _ep("g")
    rec = episode_to_record(ep)
    assert rec["media_availability"] is None
    assert record_to_episode(rec).media_availability is None


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


def test_merge_preserving_foreign_owned_uids_none_is_unchanged_behavior():
    # owned_uids=None must reproduce today's source-atomic merge byte-for-byte (audio/align/full).
    remote = {"u1": {"uid": "u1", "audio": {"url": "R"}, "transcript": {"key": "tR"}}}
    local = {"u1": {"uid": "u1", "audio": {"url": "L"}, "transcript": {"key": "tL"}}}
    protected = protected_blocks_for_lane("transcribe")
    assert merge_preserving_foreign(remote, local, protected) == merge_preserving_foreign(
        remote, local, protected, owned_uids=None
    )


def test_merge_preserving_foreign_owned_uids_prevents_sibling_uid_regression():
    # The reviewer's race (review/18 §2.1): two transcribe shards split one source. Both pulled the
    # full snapshot, so shard B's `local` carries a snapshot-stale transcript for uid1 (owned by A).
    # Shard A already wrote uid1's fresh transcript into remote. Shard B owns only uid2 — its merge
    # must NOT regress uid1's fresh transcript with B's stale snapshot copy.
    remote = {
        "uid1": {"uid": "uid1", "transcript": {"key": "FRESH-from-A", "synced": True}},
        "uid2": {"uid": "uid2", "transcript": None},
    }
    local = {  # shard B's whole-source view, stale for uid1
        "uid1": {"uid": "uid1", "transcript": {"key": "STALE-snapshot", "synced": False}},
        "uid2": {"uid": "uid2", "transcript": {"key": "FRESH-from-B", "synced": True}},
    }
    merged = merge_preserving_foreign(
        remote, local, protected_blocks_for_lane("transcribe"), owned_uids=frozenset({"uid2"})
    )
    assert merged["uid1"]["transcript"]["key"] == "FRESH-from-A"  # sibling not regressed
    assert merged["uid2"]["transcript"]["key"] == "FRESH-from-B"  # owned uid written


def test_merge_preserving_foreign_unowned_new_uid_carries_no_artifact_block():
    # §2.2 unowned-uid rule: a uid discovered this run but owned by no one contributes provider/
    # render fields but NO artifact block (a sibling will own and write it).
    remote: dict = {}
    local = {
        "newbie": {
            "uid": "newbie",
            "title": "T",
            "audio": {"url": "L"},
            "transcript": {"key": "tL"},
        }
    }
    merged = merge_preserving_foreign(
        remote, local, protected_blocks_for_lane("transcribe"), owned_uids=frozenset()
    )
    assert merged["newbie"]["title"] == "T"  # provider/render fields kept
    assert "audio" not in merged["newbie"]  # artifact blocks dropped
    assert "transcript" not in merged["newbie"]


def test_merge_preserving_foreign_better_remote_plan_keeps_owned_transcript():
    """Regression (GH#831 first long Modal canary): a transcribe worker's just-written transcript
    was silently deleted when remote's plan rank advanced mid-run and remote had no transcript.
    The VTT/words artifact is already uploaded (lease reaper infers done from artifact presence),
    so a null transcript block is a permanent, invisible loss. The owned block must survive a
    better-but-transcript-less remote plan."""
    remote = {
        "u1": {
            "uid": "u1",
            # Strictly-better remote plan (decoded > container) — triggers the preservation path.
            "sources": [{"id": "s0", "duration": 100.0, "duration_basis": "decoded"}],
            "timeline": {"version": "silence:2", "segments": []},
            "audio": {"url": "REMOTE", "key": "decoded-key", "spec_hash": "decoded-spec"},
            # ...but remote has NO transcript yet (this is the first transcription).
        }
    }
    local = {
        "u1": {
            "uid": "u1",
            "sources": [{"id": "s0", "duration": 101.0, "duration_basis": "container"}],
            "timeline": {"version": "silence:2", "segments": []},
            "audio": {"url": "LOCAL", "key": "container-key", "spec_hash": "container-spec"},
            "transcript": {"key": "transcripts/src/u1-asr-abc.vtt", "synced": True},
        }
    }

    merged = merge_preserving_foreign(
        remote, local, protected_blocks_for_lane("transcribe"), owned_uids=frozenset({"u1"})
    )

    # The freshly-written, owned transcript survives...
    assert merged["u1"]["transcript"] == {"key": "transcripts/src/u1-asr-abc.vtt", "synced": True}
    # ...while the non-owned audio + the better remote plan still win (protected/planning behavior).
    assert merged["u1"]["audio"] == remote["u1"]["audio"]
    assert merged["u1"]["sources"] == remote["u1"]["sources"]


def test_merge_preserving_foreign_better_remote_plan_still_replaces_owned_from_truthy_remote():
    """The preservation path must still REPLACE an owned block when remote actually has a fresher
    value for it (the legitimate audio-lane case: a stale container-plan audio yields to remote's
    decoded-plan audio). Only *dropping* an owned block remote lacks is disallowed."""
    remote = {
        "u1": {
            "uid": "u1",
            "sources": [{"id": "s0", "duration": 100.0, "duration_basis": "decoded"}],
            "timeline": {"version": "silence:2", "segments": []},
            "audio": {"url": "REMOTE-DECODED", "key": "decoded-key", "spec_hash": "decoded-spec"},
        }
    }
    local = {
        "u1": {
            "uid": "u1",
            "sources": [{"id": "s0", "duration": 101.0, "duration_basis": "container"}],
            "timeline": {"version": "silence:2", "segments": []},
            "audio": {"url": "LOCAL-CONTAINER", "key": "container-key", "spec_hash": "c-spec"},
        }
    }

    merged = merge_preserving_foreign(remote, local, protected_blocks_for_lane("audio"))

    assert merged["u1"]["audio"] == remote["u1"]["audio"]  # owned audio yields to fresher remote


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


def test_confirmed_dead_recheck_due_flat_interval():
    from citypods.availability import CONFIRMED_EMPTY, MediaAvailability
    from citypods.records import CONFIRMED_DEAD_RECHECK_INTERVAL, confirmed_dead_recheck_due

    now = datetime.now(UTC)
    ep = _ep("g1")

    # Inside the flat window → not due (poll on the flat cadence, no re-download).
    ep.media_availability = MediaAvailability(
        state=CONFIRMED_EMPTY,
        last_check=(now - CONFIRMED_DEAD_RECHECK_INTERVAL / 2).isoformat(),
    )
    assert not confirmed_dead_recheck_due(ep, now)

    # Past the flat window → due for its one periodic recheck.
    ep.media_availability = MediaAvailability(
        state=CONFIRMED_EMPTY,
        last_check=(now - CONFIRMED_DEAD_RECHECK_INTERVAL - timedelta(days=1)).isoformat(),
    )
    assert confirmed_dead_recheck_due(ep, now)

    # No verdict / no anchor → treated as due so a freshly-confirmed episode rechecks once.
    ep.media_availability = None
    assert confirmed_dead_recheck_due(ep, now)
