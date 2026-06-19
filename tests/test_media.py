"""Tests for the audio materialization pipeline (content-addressed, spec-aware)."""

from __future__ import annotations

import subprocess
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from citypods.media import (
    _ENCODE_RSS_COPY_BYTES,
    _ENCODE_RSS_MAX_BYTES,
    _ENCODE_RSS_UNKNOWN_BYTES,
    _probe_duration_secs,
    encode_args,
    estimate_encode_rss_bytes,
    materialize_audio,
)
from citypods.models import City, Episode
from citypods.records import audio_object_key, audio_spec_hash, source_key
from citypods.storage.local import LocalStorage
from citypods.timeline import Segment, Timeline

MAX_KBPS = 96


class FakeFfmpeg:
    """Minimal fake for materialize_audio tests — records calls, writes stub bytes."""

    def __init__(self, fail: bool = False):
        self.calls: list[str] = []  # first resolved URL per call
        self.chapters: list[list[dict] | None] = []
        self.timelines: list = []
        self.loudness_profiles: list[str | None] = []
        self.processing_profiles: list[str | None] = []
        self.fail = fail

    def extract_audio(
        self,
        timeline,
        sources_by_id,
        dest,
        chapters=None,
        *,
        loudness_profile=None,
        processing_profile=None,
        asset_resolver=None,
    ) -> None:
        # Expose the first source URL for backward-compat assertions
        first_url = next(iter(sources_by_id.values())) if sources_by_id else ""
        self.calls.append(first_url)
        self.chapters.append(chapters)
        self.timelines.append(timeline)
        self.loudness_profiles.append(loudness_profile)
        self.processing_profiles.append(processing_profile)
        if self.fail:
            raise subprocess.CalledProcessError(1, "ffmpeg")
        # Write a realistic, non-empty stub: the #39 truncation guard rejects an encode under
        # _MIN_PLAUSIBLE_AUDIO_BYTES (an empty/throttled fetch), so a few marker bytes won't do.
        dest.write_bytes(b"fake-m4a" * 1024)


def _city(slug="x-tx", extract_audio=False):
    return City(
        slug=slug,
        provider="civicplus",
        source={"feed_url": "x"},
        podcast_title="X",
        podcast_author="City of X",
        podcast_email="",
        podcast_description="d",
        extract_audio=extract_audio,
    )


def _ep(guid, kind="hls", url="https://src/manifest.m3u8"):
    return Episode(
        guid=guid,
        uid=f"uid-{guid}",
        title=f"Meeting {guid}",
        published=datetime(2026, 5, 20, tzinfo=UTC),
        video_url=url,
        media_kind=kind,
    )


def _edited_timeline():
    return Timeline(
        version="test-concat:1",
        segments=(
            Segment(
                served_start=0.0,
                served_end=1200.0,
                kind="source",
                source_id="part-1",
                source_start=30.0,
                source_end=1230.0,
            ),
            Segment(
                served_start=1200.0,
                served_end=3300.0,
                kind="source",
                source_id="part-2",
                source_start=0.0,
                source_end=2100.0,
            ),
        ),
    )


def _store(tmp_path):
    return LocalStorage(root=tmp_path / "audio", url_prefix="https://cdn/audio")


def _materialize(city, eps, store, ff, stop=None):
    return materialize_audio(
        city,
        eps,
        storage=store,
        ffmpeg=ff,
        max_kbps=MAX_KBPS,
        resolve_media_url=lambda e: e.video_url,
        stop=stop,
    )


class FakeAdmission:
    def __init__(self, admitted=True):
        self.admitted = admitted
        self.calls: list[tuple[str, str]] = []

    def wait(self, *, kind, label, stop=None):
        self.calls.append((kind, label))
        return self.admitted


def test_hls_episode_is_hosted(tmp_path):
    eps = [_ep("g1")]
    ff = FakeFfmpeg()
    city = _city()
    stats = _materialize(city, eps, _store(tmp_path), ff)
    spec = audio_spec_hash(eps[0], max_kbps=MAX_KBPS)
    key = audio_object_key(city, eps[0], spec)
    assert stats.hosted == 1
    assert eps[0].hosted_audio_url == f"https://cdn/audio/{key}"
    assert eps[0].audio_key == key and eps[0].audio_spec_hash == spec
    assert ff.calls == ["https://src/manifest.m3u8"]
    assert (tmp_path / "audio" / key).exists()


def test_encode_args_copies_or_reencodes_by_cap():
    assert encode_args(64_000, 96) == ["-c:a", "copy"]
    assert encode_args(96_000, 96) == ["-c:a", "copy"]
    assert encode_args(128_000, 96) == ["-c:a", "aac", "-b:a", "96k", "-ac", "1"]
    assert encode_args(None, 96) == ["-c:a", "aac", "-b:a", "96k", "-ac", "1"]
    assert encode_args(64_000, 96, source_codec="mp2") == [
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-ac",
        "1",
    ]
    assert encode_args(64_000, 96, source_codec=None) == [
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-ac",
        "1",
    ]


def test_loudness_profile_passed_to_ffmpeg(tmp_path):
    eps = [_ep("g1")]
    ff = FakeFfmpeg()
    materialize_audio(
        _city(),
        eps,
        storage=_store(tmp_path),
        ffmpeg=ff,
        max_kbps=MAX_KBPS,
        loudness_profile="ebuR128:-16LUFS",
        resolve_media_url=lambda e: e.video_url,
    )
    assert ff.loudness_profiles == ["ebuR128:-16LUFS"]


def test_loudness_empty_string_passed_as_none_to_ffmpeg(tmp_path):
    eps = [_ep("g1")]
    ff = FakeFfmpeg()
    materialize_audio(
        _city(),
        eps,
        storage=_store(tmp_path),
        ffmpeg=ff,
        max_kbps=MAX_KBPS,
        loudness_profile="",
        resolve_media_url=lambda e: e.video_url,
    )
    assert ff.loudness_profiles == [None]


def test_processing_profile_passed_to_ffmpeg_and_audio_spec(tmp_path):
    from citypods.media import PODCAST_SPEECH_PROFILE

    eps = [_ep("g1")]
    ff = FakeFfmpeg()
    materialize_audio(
        _city(),
        eps,
        storage=_store(tmp_path),
        ffmpeg=ff,
        max_kbps=MAX_KBPS,
        loudness_profile="ebuR128:-16LUFS",
        processing_profile=PODCAST_SPEECH_PROFILE,
        resolve_media_url=lambda e: e.video_url,
    )
    expected = audio_spec_hash(
        eps[0],
        max_kbps=MAX_KBPS,
        loudness_profile="ebuR128:-16LUFS",
        processing_profile=PODCAST_SPEECH_PROFILE,
    )
    assert ff.processing_profiles == [PODCAST_SPEECH_PROFILE]
    assert eps[0].audio_spec_hash == expected


def test_audio_encode_waits_for_resource_admission(tmp_path):
    eps = [_ep("g1")]
    ff = FakeFfmpeg()
    admission = FakeAdmission()
    stats = materialize_audio(
        _city(),
        eps,
        storage=_store(tmp_path),
        ffmpeg=ff,
        max_kbps=MAX_KBPS,
        resolve_media_url=lambda e: e.video_url,
        resource_admission=admission,
    )
    assert admission.calls == [("audio", "uid-g1")]
    assert stats.encoded == 1
    assert ff.calls == ["https://src/manifest.m3u8"]


def test_source_cache_fetch_happens_before_native_cpu_admission(tmp_path):
    events: list[str] = []
    cached = tmp_path / "cached.mka"
    cached.write_bytes(b"source")

    class _Cache:
        def get_or_fetch(self, uid, url):
            events.append("fetch")
            return cached

    class _Gate:
        def acquire(self, *, kind, label, stop=None):
            events.append("gate")
            return True

        def release(self, *, kind):
            events.append("release")

    stats = materialize_audio(
        _city(),
        [_ep("g1")],
        storage=_store(tmp_path),
        ffmpeg=FakeFfmpeg(),
        max_kbps=MAX_KBPS,
        resolve_media_url=lambda e: e.video_url,
        source_cache=_Cache(),
        native_work_gate=_Gate(),
    )

    assert stats.encoded == 1
    assert events == ["fetch", "gate", "release"]


def test_audio_encode_defers_when_resource_admission_stops(tmp_path):
    eps = [_ep("g1")]
    ff = FakeFfmpeg()
    admission = FakeAdmission(admitted=False)
    stats = materialize_audio(
        _city(),
        eps,
        storage=_store(tmp_path),
        ffmpeg=ff,
        max_kbps=MAX_KBPS,
        resolve_media_url=lambda e: e.video_url,
        resource_admission=admission,
    )
    assert admission.calls == [("audio", "uid-g1")]
    assert stats.skipped_budget == 1
    assert stats.encoded == 0
    assert ff.calls == []


def test_loudness_profile_changes_spec_hash_and_key():
    from citypods.records import audio_spec_hash

    ep = _ep("g1")
    ep.uid = "uid-g1"
    spec_plain = audio_spec_hash(ep, max_kbps=MAX_KBPS, loudness_profile="")
    spec_loud = audio_spec_hash(ep, max_kbps=MAX_KBPS, loudness_profile="ebuR128:-16LUFS")
    assert spec_plain != spec_loud


def test_sources_by_id_single_source_uses_resolved_url(tmp_path):
    """Single ep.sources entry → resolved URL (may be fresher presigned) is used."""
    from citypods.media import _sources_by_id
    from citypods.timeline import SourceMedia

    ep = _ep("g1")
    ep.sources = [
        SourceMedia(
            id="s0",
            provider="swagit",
            ref="https://cdn/old.mp4",
            media_kind="direct",
            duration=3600.0,
            watch_url=None,
        )
    ]
    result = _sources_by_id(ep, "https://cdn/fresh.mp4")
    assert result == {"s0": "https://cdn/fresh.mp4"}


def test_sources_by_id_multi_source_uses_refs(tmp_path):
    """Multiple ep.sources → each ref URL used directly; resolved_url ignored."""
    from citypods.media import _sources_by_id
    from citypods.timeline import SourceMedia

    ep = _ep("g1")
    ep.sources = [
        SourceMedia(
            id="s0",
            provider="swagit",
            ref="https://cdn/seg1.mp4",
            media_kind="direct",
            duration=1800.0,
            watch_url=None,
        ),
        SourceMedia(
            id="s1",
            provider="swagit",
            ref="https://cdn/seg2.mp4",
            media_kind="direct",
            duration=2700.0,
            watch_url=None,
        ),
    ]
    result = _sources_by_id(ep, "https://cdn/ignored.mp4")
    assert result == {"s0": "https://cdn/seg1.mp4", "s1": "https://cdn/seg2.mp4"}


def test_sources_by_id_no_sources_uses_s0():
    from citypods.media import _sources_by_id

    ep = _ep("g1")
    assert _sources_by_id(ep, "https://cdn/file.mp4") == {"s0": "https://cdn/file.mp4"}


def test_deferred_episodes_encoded_before_fresh(tmp_path):
    """Episodes with prior failed attempts (out of backoff) are encoded before fresh ones."""
    from datetime import timedelta

    store = _store(tmp_path)
    ff = FakeFfmpeg()

    fresh = _ep("fresh")
    deferred = _ep("deferred")
    deferred.materialize_attempts = 2
    deferred.materialize_last_attempt = (
        datetime(2026, 1, 1, tzinfo=UTC) - timedelta(days=10)
    ).isoformat()  # well past backoff window

    # Pass fresh first; deferred should still be encoded first due to sort.
    materialize_audio(
        _city(),
        [fresh, deferred],
        storage=store,
        ffmpeg=ff,
        max_kbps=MAX_KBPS,
        resolve_media_url=lambda e: e.video_url,
    )
    # deferred was encoded first → its URL appears first in ff.calls
    assert ff.calls[0] == deferred.video_url
    assert ff.calls[1] == fresh.video_url


def test_content_addressed_key_changes_only_when_spec_changes():
    ep = _ep("g1")
    city = _city()
    spec1 = audio_spec_hash(ep, max_kbps=96)
    k1 = audio_object_key(city, ep, spec1)
    # A spec change (chapters added) -> different key (re-host); URL would change.
    ep.chapters = [{"start": 0, "title": "Call to order"}]
    spec2 = audio_spec_hash(ep, max_kbps=96)
    assert spec1 != spec2
    assert audio_object_key(city, ep, spec2) != k1
    # Same source+uid+spec -> stable key (dedup across feeds).
    assert audio_object_key(city, ep, spec1) == k1


def test_direct_not_hosted_unless_extract_audio(tmp_path):
    eps = [_ep("g1", kind="direct", url="https://src/v.mp4")]
    ff = FakeFfmpeg()
    stats = _materialize(_city(extract_audio=False), eps, _store(tmp_path), ff)
    assert stats.hosted == 0 and ff.calls == []
    assert eps[0].hosted_audio_url is None


def test_direct_hosted_when_extract_audio(tmp_path):
    eps = [_ep("g1", kind="direct", url="https://src/v.mp4")]
    ff = FakeFfmpeg()
    stats = _materialize(_city(extract_audio=True), eps, _store(tmp_path), ff)
    assert stats.hosted == 1 and eps[0].hosted_audio_url


def test_stop_signal_defers_encodes(tmp_path):
    """When the shared stop predicate is True (wall-clock spent or superseded), no new encode
    starts — every needs-encoding episode is deferred to a later run."""
    eps = [_ep("g1"), _ep("g2"), _ep("g3")]
    ff = FakeFfmpeg()
    stats = _materialize(_city(), eps, _store(tmp_path), ff, stop=lambda: True)
    assert stats.encoded == 0 and stats.skipped_budget == 3 and ff.calls == []
    assert all(e.hosted_audio_url is None for e in eps)


def test_stop_signal_checked_per_episode(tmp_path):
    """The stop predicate is consulted per episode, so a run can encode a few then stop mid-scan
    (e.g. the deadline passes) and defer the rest."""
    eps = [_ep("g1"), _ep("g2"), _ep("g3")]
    ff = FakeFfmpeg()
    calls = {"n": 0}

    def stop():  # allow the first two encodes, then stop
        calls["n"] += 1
        return calls["n"] > 2

    stats = _materialize(_city(), eps, _store(tmp_path), ff, stop=stop)
    assert stats.encoded == 2 and stats.skipped_budget == 1


def test_stats_split_encoded_vs_credited(tmp_path):
    """``hosted`` splits into expensive encodes and near-free storage re-credits, so the budget's
    per-episode time estimate isn't blended across two ~10-100x-different operations."""
    city = _city()
    store = _store(tmp_path)
    # g1's object is already in storage (record drifted) -> credited, no ffmpeg.
    pre = _ep("g1")
    pre_key = audio_object_key(city, pre, audio_spec_hash(pre, max_kbps=MAX_KBPS))
    _seed_object(store, pre_key)
    # g2 has no object yet -> encoded.
    fresh = _ep("g2")
    ff = FakeFfmpeg()
    stats = _materialize(city, [pre, fresh], store, ff)
    assert stats.hosted == 2
    assert stats.encoded == 1 and stats.credited == 1
    assert ff.calls == ["https://src/manifest.m3u8"]  # only the un-stored one was encoded


def test_credit_path_backfills_served_duration(tmp_path):
    city = _city()
    store = _store(tmp_path)
    ep = _ep("g1")
    ep.duration = 3600
    key = audio_object_key(city, ep, audio_spec_hash(ep, max_kbps=MAX_KBPS))
    _seed_object(store, key)

    stats = _materialize(city, [ep], store, FakeFfmpeg())

    assert stats.credited == 1
    assert ep.audio_duration_served == pytest.approx(3600.0)


def test_credit_path_does_not_download_hosted_audio_for_duration(tmp_path, monkeypatch):
    city = _city()
    store = _store(tmp_path)
    ep = _ep("g1")
    ep.duration = 7200
    key = audio_object_key(city, ep, audio_spec_hash(ep, max_kbps=MAX_KBPS))
    _seed_object(store, key)

    def _unexpected_probe(*_args, **_kwargs):
        raise AssertionError("credit/reuse path should not probe hosted audio")

    monkeypatch.setattr("citypods.media._probe_duration_secs", _unexpected_probe)

    stats = _materialize(city, [ep], store, FakeFfmpeg())

    assert stats.credited == 1
    assert ep.audio_duration_served == pytest.approx(7200.0)


def test_credits_run_even_when_stopped(tmp_path):
    """Near-free storage re-credits are not gated by the stop predicate — only encodes are — so a
    superseded/over-window run still reconciles drifted records before it wraps up."""
    city = _city()
    store = _store(tmp_path)
    # Three episodes whose objects already exist in storage (all credits) + one that needs encoding.
    credited_eps = [_ep(f"c{i}") for i in range(3)]
    for ep in credited_eps:
        _seed_object(store, audio_object_key(city, ep, audio_spec_hash(ep, max_kbps=MAX_KBPS)))
    fresh = _ep("enc")
    ff = FakeFfmpeg()
    # Stopped: the 3 credits still all go through; only the encode is deferred.
    stats = _materialize(city, [*credited_eps, fresh], store, ff, stop=lambda: True)
    assert stats.credited == 3 and stats.encoded == 0
    assert stats.skipped_budget == 1 and ff.calls == []  # only the encode deferred


def _seed_object(store, key):
    """Write a placeholder object at ``key`` so a reused record has something to point at."""
    src = Path(store.root).parent / "seed.m4a"
    src.write_bytes(b"seed")
    store.put_file(key, src, "audio/mp4")


def test_matching_spec_reuses_without_ffmpeg(tmp_path):
    ep = _ep("g1")
    ep.duration = 3600
    city = _city()
    store = _store(tmp_path)
    spec = audio_spec_hash(ep, max_kbps=MAX_KBPS)  # already current
    key = audio_object_key(city, ep, spec)
    _seed_object(store, key)  # object really exists in storage
    ep.audio_key = key
    ep.hosted_audio_url = store.public_url(key)
    ep.audio_spec_hash = spec
    ff = FakeFfmpeg()
    stats = _materialize(city, [ep], store, ff)
    assert stats.reused == 1 and ff.calls == []
    assert ep.hosted_audio_url == store.public_url(key)
    assert ep.audio_duration_served == pytest.approx(3600.0)


def test_matching_spec_corrects_edited_timeline_served_duration(tmp_path):
    ep = _ep("g1")
    ep.timeline = _edited_timeline()
    ep.audio_duration_served = 3300.8
    city = _city()
    store = _store(tmp_path)
    spec = audio_spec_hash(ep, max_kbps=MAX_KBPS)
    key = audio_object_key(city, ep, spec)
    _seed_object(store, key)
    ep.audio_key = key
    ep.hosted_audio_url = store.public_url(key)
    ep.audio_spec_hash = spec

    stats = _materialize(city, [ep], store, FakeFfmpeg())

    assert stats.reused == 1
    assert ep.audio_duration_served == pytest.approx(3300.0)


def test_zero_source_duration_does_not_backfill_served_duration(tmp_path):
    ep = _ep("g1")
    ep.duration = 0
    city = _city()
    store = _store(tmp_path)
    spec = audio_spec_hash(ep, max_kbps=MAX_KBPS)
    key = audio_object_key(city, ep, spec)
    _seed_object(store, key)
    ep.audio_key = key
    ep.hosted_audio_url = store.public_url(key)
    ep.audio_spec_hash = spec

    stats = _materialize(city, [ep], store, FakeFfmpeg())

    assert stats.reused == 1
    assert ep.audio_duration_served is None


def test_legacy_spec_is_reused(tmp_path):
    ep = _ep("g1")
    city = _city()
    store = _store(tmp_path)
    key = f"{city.provider}/{source_key(city)}/legacy.m4a"
    _seed_object(store, key)
    ep.audio_key = key
    ep.hosted_audio_url = store.public_url(key)
    ep.audio_spec_hash = "legacy"  # carried over from the old manifest
    ff = FakeFfmpeg()
    stats = _materialize(city, [ep], store, ff)
    assert stats.reused == 1 and ff.calls == []


def test_processing_profile_invalidates_legacy_audio(tmp_path):
    from citypods.media import PODCAST_SPEECH_PROFILE

    ep = _ep("g1")
    city = _city()
    store = _store(tmp_path)
    legacy_key = f"{city.provider}/{source_key(city)}/legacy.m4a"
    _seed_object(store, legacy_key)
    ep.audio_key = legacy_key
    ep.hosted_audio_url = store.public_url(legacy_key)
    ep.audio_spec_hash = "legacy"
    ff = FakeFfmpeg()

    stats = materialize_audio(
        city,
        [ep],
        storage=store,
        ffmpeg=ff,
        max_kbps=MAX_KBPS,
        loudness_profile="ebuR128:-16LUFS",
        processing_profile=PODCAST_SPEECH_PROFILE,
        resolve_media_url=lambda e: e.video_url,
    )

    assert stats.encoded == 1
    assert ff.processing_profiles == [PODCAST_SPEECH_PROFILE]
    assert ep.audio_spec_hash != "legacy"


def test_stale_record_for_missing_object_re_materializes(tmp_path):
    """Issue #116: a record claims hosted audio (matching spec) but the object isn't in storage
    — e.g. a Swagit episode whose presigned source expired. The pipeline must re-materialize,
    not trust the dead record."""
    ep = _ep("g1")
    city = _city()
    store = _store(tmp_path)
    spec = audio_spec_hash(ep, max_kbps=MAX_KBPS)
    key = audio_object_key(city, ep, spec)
    ep.audio_key = key
    ep.hosted_audio_url = store.public_url(key)  # points at an object that was never written
    ep.audio_spec_hash = spec
    ff = FakeFfmpeg()
    stats = _materialize(city, [ep], store, ff)
    assert stats.reused == 0 and stats.hosted == 1
    assert ff.calls == ["https://src/manifest.m3u8"]
    assert (Path(store.root) / key).exists()


class _FailUrls:
    """ffmpeg fake that fails only for source URLs containing a marker substring."""

    def __init__(self, marker="FAIL"):
        self.marker = marker
        self.calls: list[str] = []

    def extract_audio(
        self,
        timeline,
        sources_by_id,
        dest,
        chapters=None,
        *,
        loudness_profile=None,
        processing_profile=None,
        asset_resolver=None,
    ):
        first_url = next(iter(sources_by_id.values())) if sources_by_id else ""
        self.calls.append(first_url)
        if self.marker in first_url:
            raise subprocess.CalledProcessError(1, "ffmpeg")
        dest.write_bytes(b"fake-m4a" * 1024)


def test_stale_record_cleared_when_stopped(tmp_path):
    """A dead record (object missing) must drop its pointer rather than keep advertising audio
    that isn't in storage — even when the run is stopped and can't re-encode it this run."""
    ep = _ep("g1")
    city = _city()
    store = _store(tmp_path)
    spec = audio_spec_hash(ep, max_kbps=MAX_KBPS)
    key = audio_object_key(city, ep, spec)
    ep.audio_key = key
    ep.hosted_audio_url = store.public_url(key)
    ep.audio_spec_hash = spec
    stats = _materialize(city, [ep], store, FakeFfmpeg(), stop=lambda: True)
    assert stats.skipped_budget == 1 and stats.reused == 0
    assert ep.hosted_audio_url is None and ep.audio_key is None


def test_spec_change_re_encodes(tmp_path):
    ep = _ep("g1")
    ep.hosted_audio_url = "https://cdn/audio/old.m4a"
    ep.audio_spec_hash = "stale-spec"  # no longer matches the computed spec
    ff = FakeFfmpeg()
    stats = _materialize(_city(), [ep], _store(tmp_path), ff)
    assert stats.hosted == 1 and ff.calls == ["https://src/manifest.m3u8"]


def test_ffmpeg_error_recorded(tmp_path):
    eps = [_ep("g1")]
    stats = _materialize(_city(), eps, _store(tmp_path), FakeFfmpeg(fail=True))
    assert stats.errors and eps[0].hosted_audio_url is None


def test_ffmetadata_renders_chapters():
    from citypods.media import _ffmetadata

    meta = _ffmetadata(
        [
            {"start": 51, "end": 491, "title": "AGENDA"},
            {"start": 491, "title": "Open; Mic"},  # no end -> next start; title escaped
        ]
    )
    assert meta.startswith(";FFMETADATA1")
    assert "START=51000\nEND=491000\ntitle=AGENDA" in meta
    assert "START=491000\nEND=492000" in meta  # last chapter falls back to start+1s
    assert r"title=Open\; Mic" in meta  # ';' escaped for ffmetadata


def test_chapters_passed_through_to_ffmpeg(tmp_path):
    from citypods.media import materialize_audio

    ffmpeg = FakeFfmpeg()
    storage = LocalStorage(root=tmp_path, url_prefix="https://cdn")
    ep = _ep("g1", kind="hls")
    ep.chapters = [{"start": 0, "end": 10, "title": "Intro"}]
    materialize_audio(
        _city(),
        [ep],
        storage=storage,
        ffmpeg=ffmpeg,
        max_kbps=96,
        resolve_media_url=lambda e: e.video_url,
    )
    assert ffmpeg.chapters == [[{"start": 0, "end": 10, "title": "Intro"}]]


# --- materialization backoff for repeatedly-failing episodes (issue #120) ------------------


def test_failed_attempt_records_backoff_state(tmp_path):
    ep = _ep("bad", url="https://src/FAIL.m3u8")
    stats = _materialize(_city(), [ep], _store(tmp_path), _FailUrls())
    assert stats.hosted == 0 and len(stats.errors) == 1
    assert ep.materialize_attempts == 1 and ep.materialize_last_attempt is not None


def test_episode_in_backoff_is_skipped_without_consuming_budget(tmp_path):
    """A recently-failed episode must not be re-tried (no ffmpeg call, no budget spent) until its
    backoff window elapses — otherwise a broken meeting churns budget/time every run."""
    ep = _ep("bad", url="https://src/FAIL.m3u8")
    ep.materialize_attempts = 1
    ep.materialize_last_attempt = datetime.now(UTC).isoformat()
    ff = _FailUrls()
    stats = _materialize(_city(), [ep], _store(tmp_path), ff)
    assert stats.skipped_backoff == 1 and stats.errors == []
    assert ff.calls == []  # never attempted


def test_old_loudness_error_retries_immediately_under_peak_fallback(tmp_path):
    from citypods.media import PODCAST_SPEECH_PROFILE

    ep = _ep("peak-constrained")
    ep.materialize_attempts = 4
    ep.materialize_last_attempt = datetime.now(UTC).isoformat()
    ep.materialize_error = "loudness"
    ff = FakeFfmpeg()

    stats = materialize_audio(
        _city(),
        [ep],
        storage=_store(tmp_path),
        ffmpeg=ff,
        max_kbps=MAX_KBPS,
        loudness_profile="ebuR128:-16LUFS",
        processing_profile=PODCAST_SPEECH_PROFILE,
        resolve_media_url=lambda e: e.video_url,
    )

    assert stats.encoded == 1
    assert stats.skipped_backoff == 0
    assert ep.materialize_attempts == 0
    assert ep.materialize_error is None


def test_backoff_window_elapsed_re_attempts(tmp_path):
    ep = _ep("bad", url="https://src/FAIL.m3u8")
    ep.materialize_attempts = 1
    ep.materialize_last_attempt = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    ff = _FailUrls()
    stats = _materialize(_city(), [ep], _store(tmp_path), ff)
    # base backoff is 1 day; 2 days elapsed -> re-attempt (which fails again, bumping the counter)
    assert stats.skipped_backoff == 0 and len(stats.errors) == 1
    assert ff.calls and ep.materialize_attempts == 2


def test_success_resets_backoff_state(tmp_path):
    ep = _ep("g1")
    ep.materialize_attempts = 3
    ep.materialize_last_attempt = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    ep.materialize_error = "dead"
    stats = _materialize(_city(), [ep], _store(tmp_path), FakeFfmpeg())
    assert stats.hosted == 1
    assert ep.materialize_attempts == 0 and ep.materialize_last_attempt is None
    assert ep.materialize_error is None


def test_categorized_failure_records_its_code(tmp_path):
    from citypods.media import materialize_audio
    from citypods.providers.base import MEDIA_DEAD, MediaUnavailable

    ep = _ep("g1")

    def _resolve(_e):
        raise MediaUnavailable("no media", code=MEDIA_DEAD)

    materialize_audio(
        _city(),
        [ep],
        storage=_store(tmp_path),
        ffmpeg=FakeFfmpeg(),
        max_kbps=MAX_KBPS,
        resolve_media_url=_resolve,
    )
    assert ep.materialize_error == MEDIA_DEAD and ep.materialize_attempts == 1


def test_uncategorized_failure_records_generic_error(tmp_path):
    ep = _ep("bad", url="https://src/FAIL.m3u8")
    _materialize(_city(), [ep], _store(tmp_path), _FailUrls())
    assert ep.materialize_error == "error"


def test_encode_timeout_is_caught_and_tagged_timeout(tmp_path):
    # A stalled source trips the per-encode timeout (subprocess.TimeoutExpired). It must be caught
    # (not crash the build / hang the worker) and recorded as a backoff-eligible failure tagged
    # "timeout" so it isn't retried every run — the in-flight-hang gap behind issue #63.
    class _TimeoutFfmpeg:
        def extract_audio(
            self,
            timeline,
            sources_by_id,
            dest,
            chapters=None,
            *,
            loudness_profile=None,
            processing_profile=None,
            asset_resolver=None,
        ):
            raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=2700)

    ep = _ep("slow")
    stats = _materialize(_city(), [ep], _store(tmp_path), _TimeoutFfmpeg())
    assert stats.hosted == 0 and len(stats.errors) == 1
    assert ep.materialize_error == "timeout" and ep.materialize_attempts == 1
    assert ep.materialize_last_attempt is not None


def test_probe_duration_reads_local_file(monkeypatch, tmp_path):
    import citypods.media as media

    fake_path = tmp_path / "audio.m4a"
    fake_path.write_bytes(b"fake")

    def _fake_run(cmd, **kw):
        class _R:
            stdout = "7265.123\n"

        return _R()

    monkeypatch.setattr(media.subprocess, "run", _fake_run)
    result = _probe_duration_secs(fake_path)
    assert result == pytest.approx(7265.123)


def test_probe_duration_returns_none_on_error(monkeypatch, tmp_path):
    import citypods.media as media

    monkeypatch.setattr(
        media.subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(OSError("no ffprobe"))
    )
    result = _probe_duration_secs(tmp_path / "audio.m4a")
    assert result is None


def test_audio_duration_served_set_from_probe(tmp_path):
    """materialize_audio sets audio_duration_served from the probed output file."""
    import citypods.media as media

    eps = [_ep("g1")]
    city = _city()
    ff = FakeFfmpeg()

    probed_values: list[float] = []

    def _fake_probe(path, ffmpeg_binary="ffmpeg"):
        v = 7200.5
        probed_values.append(v)
        return v

    original = media._probe_duration_secs
    media._probe_duration_secs = _fake_probe
    try:
        _materialize(city, eps, _store(tmp_path), ff)
    finally:
        media._probe_duration_secs = original

    assert probed_values == [7200.5]
    assert eps[0].audio_duration_served == pytest.approx(7200.5)


def test_audio_duration_served_uses_edited_timeline_total(monkeypatch, tmp_path):
    """Edited timelines store the EDL total, not ffprobe's rounded container duration."""
    import citypods.media as media

    ep = _ep("g1")
    ep.timeline = _edited_timeline()
    monkeypatch.setattr(media, "_probe_duration_secs", lambda *args, **kwargs: 3300.8)

    stats = _materialize(_city(), [ep], _store(tmp_path), FakeFfmpeg())

    assert stats.encoded == 1
    assert ep.audio_duration_served == pytest.approx(3300.0)


def test_audio_duration_served_fallback_when_probe_fails(tmp_path):
    """When probe returns None, _served_duration fallback is used (ep.duration)."""
    import citypods.media as media

    ep = _ep("g1")
    ep.duration = 3600

    def _failing_probe(path, ffmpeg_binary="ffmpeg"):
        return None

    original = media._probe_duration_secs
    media._probe_duration_secs = _failing_probe
    try:
        _materialize(_city(), [ep], _store(tmp_path), FakeFfmpeg())
    finally:
        media._probe_duration_secs = original

    assert ep.audio_duration_served == pytest.approx(3600.0)


def test_command_ffmpeg_wires_timeouts(monkeypatch, tmp_path):
    # Wiring guard: a normal run never reveals whether the timeouts are passed, but dropping them
    # reopens the hang. Assert ffprobe gets a (capped) timeout and the encode gets both -rw_timeout
    # and the hard subprocess timeout.
    import citypods.media as media

    calls: list[tuple[list, dict]] = []

    def _fake_run(cmd, **kw):
        calls.append((cmd, kw))

        class _R:
            stdout = "128000"  # ffprobe bitrate

        return _R()

    monkeypatch.setattr(media.subprocess, "run", _fake_run)
    media.CommandFfmpeg(max_kbps=96, timeout_seconds=2700).extract_audio(
        timeline=None,
        sources_by_id={"s0": "https://src/manifest.m3u8"},
        dest=tmp_path / "out.m4a",
    )
    (_probe_cmd, probe_kw), (enc_cmd, enc_kw) = calls[0], calls[1]
    assert probe_kw["timeout"] == 120.0  # capped to _PROBE_TIMEOUT_S
    assert "-rw_timeout" in enc_cmd and enc_kw["timeout"] == 2700


def test_guarded_ffmpeg_stops_on_low_available_memory():
    import citypods.media as media
    from citypods.resources import ResourceSnapshot

    events: list[str] = []

    class FakeProc:
        pid = 1234
        returncode = None
        terminated = False
        killed = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

        def communicate(self, timeout=None):
            return b"", b""

    proc = FakeProc()

    def _fake_popen(cmd, **kw):
        events.append(f"popen:{cmd[0]}")
        return proc

    def _low_memory():
        return ResourceSnapshot(
            rss_bytes=100,
            mem_total_bytes=1000,
            mem_available_bytes=50,
            load1=0.0,
            load5=0.0,
            cpus=4,
        )

    with pytest.raises(media.FfmpegMemoryLimitExceeded, match="filter-render"):
        media._run_ffmpeg_guarded(
            ["ffmpeg"],
            phase="filter-render",
            memory_floor_bytes=100,
            snapshot=_low_memory,
            sleep=lambda _seconds: None,
            log=events.append,
            popen=_fake_popen,
            child_rss=lambda _pid: 12 * 1024 * 1024,
        )

    assert proc.terminated
    assert any("memory-stop" in event for event in events)
    assert any("peak_rss=12.0MiB" in event for event in events)
    assert any("min_mem_avail=50B" in event for event in events)


def test_guarded_ffmpeg_logs_peak_child_rss_and_min_available_memory():
    import citypods.media as media
    from citypods.resources import ResourceSnapshot

    events: list[str] = []
    polls = iter([None, None, 0])
    snapshots = iter(
        [
            ResourceSnapshot(
                rss_bytes=100,
                mem_total_bytes=1000,
                mem_available_bytes=900,
                load1=0.0,
                load5=0.0,
                cpus=4,
            ),
            ResourceSnapshot(
                rss_bytes=100,
                mem_total_bytes=1000,
                mem_available_bytes=700,
                load1=0.0,
                load5=0.0,
                cpus=4,
            ),
            ResourceSnapshot(
                rss_bytes=100,
                mem_total_bytes=1000,
                mem_available_bytes=800,
                load1=0.0,
                load5=0.0,
                cpus=4,
            ),
        ]
    )
    rss_values = iter([5 * 1024 * 1024, 23 * 1024 * 1024, 11 * 1024 * 1024])

    class FakeProc:
        pid = 4321

        def poll(self):
            return next(polls)

        def communicate(self, timeout=None):
            return b"", b""

    media._run_ffmpeg_guarded(
        ["ffmpeg"],
        phase="filter-render",
        memory_floor_bytes=100,
        snapshot=lambda: next(snapshots),
        sleep=lambda _seconds: None,
        log=events.append,
        popen=lambda cmd, **kw: FakeProc(),
        child_rss=lambda _pid: next(rss_values),
    )

    done = [event for event in events if "ffmpeg filter-render done" in event][0]
    assert "peak_rss=23.0MiB" in done
    assert "min_mem_avail=700B" in done
    assert "samples=3" in done


def test_guarded_ffmpeg_logs_metrics_and_stderr_on_nonzero_exit():
    import citypods.media as media
    from citypods.resources import ResourceSnapshot

    events: list[str] = []

    class FakeProc:
        pid = 5678

        def poll(self):
            return 8

        def communicate(self, timeout=None):
            return b"", b"muxer failed\nsecond line"

    with pytest.raises(subprocess.CalledProcessError):
        media._run_ffmpeg_guarded(
            ["ffmpeg"],
            phase="filter-render",
            memory_floor_bytes=100,
            snapshot=lambda: ResourceSnapshot(
                rss_bytes=100,
                mem_total_bytes=1000,
                mem_available_bytes=640,
                load1=0.0,
                load5=0.0,
                cpus=4,
            ),
            sleep=lambda _seconds: None,
            log=events.append,
            popen=lambda cmd, **kw: FakeProc(),
            child_rss=lambda _pid: 17 * 1024 * 1024,
        )

    error = [event for event in events if "ffmpeg filter-render error" in event][0]
    assert "returncode=8" in error
    assert "peak_rss=17.0MiB" in error
    assert "min_mem_avail=640B" in error
    assert "stderr=muxer failed second line" in error


# --------------------------------------------------------------------------------------------------
# Per-host rate limiting + truncation guard + responsive timing (issue #39)
# --------------------------------------------------------------------------------------------------


def _snap():
    from citypods.resources import ResourceSnapshot

    return ResourceSnapshot(
        rss_bytes=1,
        mem_total_bytes=10**9,
        mem_available_bytes=10**9,
        load1=0.0,
        load5=0.0,
        cpus=4,
    )


def test_run_ffmpeg_guarded_serializes_remote_fetches_per_host_to_the_cap():
    """#39: the ffmpeg fetch path acquires the same per-host slot as the requests session, so a
    sharded burst of encodes never opens more than the cap of connections to one host."""
    import threading
    import time

    import citypods.media as media
    from citypods.http import HOST_LIMITER

    HOST_LIMITER.configure({"granicus.com": 1})
    active = [0]
    peak = [0]
    lock = threading.Lock()

    def _popen(cmd, **kw):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.02)
        with lock:
            active[0] -= 1

        class _P:
            pid = 1

            def poll(self):
                return 0

            def communicate(self, timeout=None):
                return b"", b""

        return _P()

    def run_once():
        media._run_ffmpeg_guarded(
            ["ffmpeg"],
            phase="source-cache",
            memory_floor_bytes=1,
            rate_limit_urls=("https://archive-video.granicus.com/x.mp4",),
            snapshot=_snap,
            sleep=lambda _s: None,
            log=lambda *a, **k: None,
            popen=_popen,
            child_rss=lambda _p: 0,
        )

    try:
        ts = [threading.Thread(target=run_once) for _ in range(5)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert peak[0] == 1  # cap-1 fully serialized the 5-way burst
    finally:
        HOST_LIMITER.configure({})  # reset the process-global singleton


def test_run_ffmpeg_guarded_local_cap_does_not_hoard_distributed_slots(tmp_path):
    """#342: the local host slot must be acquired *before* the distributed lease, so a process whose
    local cap is one can never hold more than one distributed slot for that domain — even with
    several threads racing for it — leaving the other slot free for a different shard to use."""
    import time

    import citypods.media as media
    from citypods.http import HOST_LIMITER
    from citypods.provider_leases import DistributedProviderLeasePool

    url = "https://archive-video.granicus.com/x.mp4"
    store = LocalStorage(root=tmp_path / "bucket", url_prefix="https://cdn")

    class _CountingPool(DistributedProviderLeasePool):
        """Wraps real lease acquisition to track how many leases *this shard* holds concurrently,
        independent of where in the call stack they're acquired from."""

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.active = 0
            self.peak = 0
            self._count_lock = threading.Lock()

        def slots(self, urls):
            inner = super().slots(urls)

            @contextmanager
            def _counted():
                with inner:
                    with self._count_lock:
                        self.active += 1
                        self.peak = max(self.peak, self.active)
                    try:
                        yield
                    finally:
                        with self._count_lock:
                            self.active -= 1

            return _counted()

    # Both pools share the same backing storage (the "distributed" part), but each represents a
    # different process: shard A is the process under test (cap=1 local limiter, two threads
    # racing); shard B is a separate process with its own (uncontended) local limiter.
    lease_rule = {
        "granicus.com": {"slots": 2, "ttl_seconds": 60, "poll_seconds": 0.01, "settle_seconds": 0}
    }
    shard_a_pool = _CountingPool(prefix="test-provider-leases")
    shard_a_pool.configure(store, lease_rule)
    shard_b_pool = DistributedProviderLeasePool(prefix="test-provider-leases")
    shard_b_pool.configure(store, lease_rule)
    HOST_LIMITER.configure({"granicus.com": 1})

    original_pool = media.DISTRIBUTED_PROVIDER_LEASES
    media.DISTRIBUTED_PROVIDER_LEASES = shard_a_pool

    a_holding = threading.Event()
    b_acquired = threading.Event()

    class _FakeProc:
        pid = 1

        def poll(self):
            return 0

        def communicate(self, timeout=None):
            return b"", b""

    def _popen(cmd, **kw):
        a_holding.set()
        # Give the other shard a chance to grab the second distributed slot while this process's
        # second thread is still queued on its own local cap, not on the distributed lease.
        b_acquired.wait(timeout=1)
        return _FakeProc()

    def run_shard_a():
        media._run_ffmpeg_guarded(
            ["ffmpeg"],
            phase="source-cache",
            memory_floor_bytes=1,
            rate_limit_urls=(url,),
            snapshot=_snap,
            sleep=lambda _s: None,
            log=lambda *a, **k: None,
            popen=_popen,
            child_rss=lambda _p: 0,
        )

    def run_shard_b():
        # A separate process has its own local rate limiter, so it never contends with shard A's
        # local cap — only the shared distributed lease pool decides whether it can proceed.
        a_holding.wait(timeout=1)
        with shard_b_pool.slots([url]):
            b_acquired.set()
            time.sleep(0.02)

    try:
        shard_a_threads = [threading.Thread(target=run_shard_a) for _ in range(2)]
        shard_b_thread = threading.Thread(target=run_shard_b)
        for t in shard_a_threads:
            t.start()
        shard_b_thread.start()
        for t in shard_a_threads:
            t.join(timeout=2)
        shard_b_thread.join(timeout=2)

        assert shard_a_pool.peak == 1  # local cap of one held at most one distributed slot
        assert b_acquired.is_set()  # the other shard could still use the unused distributed slot
    finally:
        media.DISTRIBUTED_PROVIDER_LEASES = original_pool
        HOST_LIMITER.configure({})


def test_run_ffmpeg_guarded_classifies_provider_throttle(monkeypatch):
    import citypods.media as media

    err = subprocess.CalledProcessError(
        1,
        ["ffmpeg"],
        stderr=b"https://archive-video.granicus.com/x.mp4: Server returned 403 Forbidden",
    )
    monkeypatch.setattr(media.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(err))

    with pytest.raises(media.RateLimitedMediaFetchError, match="HTTP 403"):
        media._run_ffmpeg_guarded(
            ["ffmpeg"],
            phase="source-cache",
            rate_limit_urls=("https://archive-video.granicus.com/x.mp4",),
            log=lambda *a, **k: None,
        )


def test_run_ffmpeg_guarded_poll_interval_is_responsive():
    """Regression guard: a 5s poll cadence logged every sub-5s fetch as ``seconds=5.0``, masking the
    truncation bug. Keep the cadence small so reported timings reflect real runtime."""
    import citypods.media as media

    assert media._FFMPEG_GUARD_POLL_SECONDS <= 1.0


def test_run_ffmpeg_guarded_sleeps_responsively_between_polls():
    import citypods.media as media

    slept: list[float] = []
    polls = iter([None, None, 0])

    class _P:
        pid = 1

        def poll(self):
            return next(polls)

        def communicate(self, timeout=None):
            return b"", b""

    media._run_ffmpeg_guarded(
        ["ffmpeg"],
        phase="source-cache",
        memory_floor_bytes=1,
        snapshot=_snap,
        sleep=lambda s: slept.append(s),
        log=lambda *a, **k: None,
        popen=lambda cmd, **kw: _P(),
        child_rss=lambda _p: 0,
    )
    assert slept  # it did sleep between polls
    assert max(slept) <= media._FFMPEG_GUARD_POLL_SECONDS <= 1.0


def _ep_with_duration(duration):
    return Episode(
        guid="g",
        title="Council",
        published=datetime(2026, 5, 19, 16, 0, tzinfo=UTC),
        video_url="https://x/v.mp4",
        body="City Council",
        duration=duration,
    )


def test_truncation_guard_raises_when_encode_far_shorter_than_declared():
    import citypods.media as media

    ep = _ep_with_duration(7200)  # a 2h meeting
    with pytest.raises(media.TruncatedAudioError):
        media._guard_against_truncated_audio(ep, probed=5.0)  # the ~5s throttled-fetch stub


def test_truncation_guard_passes_full_length_and_silence_trimmed_output():
    import citypods.media as media

    ep = _ep_with_duration(7200)
    media._guard_against_truncated_audio(ep, probed=7200.0)  # identity / loudnorm-only
    media._guard_against_truncated_audio(ep, probed=6500.0)  # silence-trimmed (~10% removed)


def test_truncation_guard_is_noop_without_a_declared_duration_or_probe():
    import citypods.media as media

    media._guard_against_truncated_audio(_ep_with_duration(None), probed=5.0)  # e.g. Swagit
    media._guard_against_truncated_audio(_ep_with_duration(0), probed=5.0)
    media._guard_against_truncated_audio(_ep_with_duration(7200), probed=None)  # probe failed


def test_truncation_guard_rejects_empty_output_even_without_a_declared_duration():
    """The 258-byte Swagit stubs: no declared duration (ratio check can't see them), so the absolute
    byte floor is what catches them."""
    import citypods.media as media

    ep = _ep_with_duration(None)  # Swagit declares none
    with pytest.raises(media.TruncatedAudioError):
        media._guard_against_truncated_audio(ep, probed=None, size_bytes=258)


def test_truncation_guard_passes_a_realistic_size_with_no_duration():
    import citypods.media as media

    media._guard_against_truncated_audio(
        _ep_with_duration(None), probed=None, size_bytes=media._MIN_PLAUSIBLE_AUDIO_BYTES + 1
    )


def test_truncated_audio_error_carries_backoff_code():
    import citypods.media as media
    from citypods.providers.base import ProviderError

    err = media.TruncatedAudioError("short")
    assert isinstance(err, ProviderError)  # caught by the encode loop's handler → #120 backoff
    assert err.code == "truncated"


def test_materialize_backs_off_a_truncated_encode_instead_of_hosting_it(tmp_path, monkeypatch):
    """End-to-end (#39): when the encoded audio probes far shorter than the feed-declared duration
    (a throttled/truncated fetch), the episode is failed into the #120 backoff — not hosted."""
    ep = _ep("g1")
    ep.duration = 7200  # feed says 2 hours
    monkeypatch.setattr(
        "citypods.media._probe_duration_secs", lambda *a, **k: 5.0
    )  # but ~5s landed
    store = _store(tmp_path)
    stats = _materialize(_city(), [ep], store, FakeFfmpeg())

    assert stats.hosted == 0
    assert ep.hosted_audio_url is None
    assert ep.materialize_attempts == 1  # backed off (will retry next run)
    assert ep.materialize_error == "truncated"
    assert len(stats.errors) == 1


# --------------------------------------------------------------------------------------------------
# Browser-compatible User-Agent for ffmpeg/ffprobe fetches (Granicus CDN 403s non-browser UAs)
# --------------------------------------------------------------------------------------------------


def test_user_agent_is_browser_compatible():
    from citypods.http import USER_AGENT

    # The Granicus media CDN 403s non-browser UAs; the prefix and platform token are load-bearing.
    assert USER_AGENT.startswith("Mozilla/5.0")
    assert "Chrome" in USER_AGENT  # must look like a real browser to pass CDN bot-detection


def test_download_audio_cmd_sends_browser_user_agent(monkeypatch):
    import citypods.media as media
    from citypods.http import USER_AGENT

    cmds: list[list[str]] = []
    monkeypatch.setattr(media, "_run_ffmpeg_guarded", lambda cmd, **kw: cmds.append(cmd))
    media._download_audio("https://archive-video.granicus.com/x.mp4", Path("/tmp/nope.mka"))
    cmd = cmds[0]
    assert "-user_agent" in cmd
    assert cmd[cmd.index("-user_agent") + 1] == USER_AGENT
    assert "-f" in cmd and cmd[cmd.index("-f") + 1] == "matroska"
    assert "-movflags" not in cmd


def test_download_audio_max_seconds_truncates_else_omits_t(monkeypatch):
    import citypods.media as media

    cmds: list[list[str]] = []
    monkeypatch.setattr(media, "_run_ffmpeg_guarded", lambda cmd, **kw: cmds.append(cmd))
    media._download_audio("https://x/y.mp4", Path("/tmp/nope.mka"), max_seconds=3)
    media._download_audio("https://x/y.mp4", Path("/tmp/nope.mka"))
    assert "-t" in cmds[0] and cmds[0][cmds[0].index("-t") + 1] == "3"  # truncated fetch
    assert "-t" not in cmds[1]  # full fetch by default


def test_source_cache_rate_limit_does_not_immediately_retry_direct_render(tmp_path):
    import citypods.media as media

    class _RateLimitedCache:
        def get_or_fetch(self, uid, url):
            raise media.RateLimitedMediaFetchError("ffmpeg source-cache hit provider throttle")

    ep = _ep("g1", kind="direct", url="https://archive-video.granicus.com/x.mp4")
    city = _city(extract_audio=True)
    ff = FakeFfmpeg()

    stats = materialize_audio(
        city,
        [ep],
        storage=_store(tmp_path),
        ffmpeg=ff,
        max_kbps=MAX_KBPS,
        resolve_media_url=lambda e: e.video_url,
        source_cache=_RateLimitedCache(),
    )

    assert ff.calls == []
    assert ep.materialize_error == "rate_limited"
    assert len(stats.errors) == 1


def test_rate_limit_circuit_skips_later_same_domain_after_threshold(tmp_path):
    import citypods.media as media

    class _RateLimitFfmpeg(FakeFfmpeg):
        def extract_audio(self, *args, **kwargs):
            super().extract_audio(*args, **kwargs)
            raise media.RateLimitedMediaFetchError("ffmpeg filter-render hit provider throttle")

    eps = [
        _ep("g1", kind="direct", url="https://archive-video.granicus.com/one.mp4"),
        _ep("g2", kind="direct", url="https://archive-video.granicus.com/two.mp4"),
    ]
    city = _city(extract_audio=True)
    ff = _RateLimitFfmpeg()

    stats = materialize_audio(
        city,
        eps,
        storage=_store(tmp_path),
        ffmpeg=ff,
        max_kbps=MAX_KBPS,
        resolve_media_url=lambda e: e.video_url,
        rate_limit_circuit=media.MediaRateLimitCircuitBreaker(
            {"granicus.com": {"threshold": 1, "cooldown_seconds": 60}}
        ),
    )

    assert len(ff.calls) == 1
    assert eps[0].materialize_error == "rate_limited"
    assert eps[1].materialize_attempts == 0
    assert stats.skipped_budget == 1


def test_rate_limit_circuit_opens_once_under_concurrent_failures():
    import citypods.media as media

    circuit = media.MediaRateLimitCircuitBreaker(
        {"granicus.com": {"threshold": 1, "cooldown_seconds": 60}}
    )
    urls = ["https://archive-video.granicus.com/x.mp4"]
    opened: list[str | None] = []
    lock = threading.Lock()

    def _fail():
        result = circuit.record_rate_limited(urls)
        with lock:
            opened.append(result)

    threads = [threading.Thread(target=_fail) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert opened.count("granicus.com") == 1
    telemetry = circuit.telemetry()["granicus.com"]
    assert telemetry["rate_limited"] == 8
    assert telemetry["circuit_trips"] == 1


def test_ffmpeg_rechecks_circuit_after_provider_lease_acquisition(monkeypatch):
    import citypods.media as media

    circuit = media.MediaRateLimitCircuitBreaker(
        {"granicus.com": {"threshold": 1, "cooldown_seconds": 60}}
    )
    urls = ("https://archive-video.granicus.com/x.mp4",)
    subprocess_started = False

    @contextmanager
    def _lease_that_observes_failure(_urls):
        circuit.record_rate_limited(urls)
        yield

    def _unexpected_run(*args, **kwargs):
        nonlocal subprocess_started
        subprocess_started = True
        raise AssertionError("ffmpeg must not start after the circuit opens")

    monkeypatch.setattr(media.DISTRIBUTED_PROVIDER_LEASES, "slots", _lease_that_observes_failure)
    monkeypatch.setattr(media.subprocess, "run", _unexpected_run)

    with pytest.raises(media.CircuitOpenMediaFetchError, match="granicus.com"):
        media._run_ffmpeg_guarded(
            ["ffmpeg", "-version"],
            phase="test",
            rate_limit_urls=urls,
            rate_limit_circuit=circuit,
            log=None,
        )

    assert subprocess_started is False


def test_circuit_deferred_telemetry_is_counted_by_media_domain():
    import citypods.media as media

    circuit = media.MediaRateLimitCircuitBreaker(
        {"granicus.com": {"threshold": 1, "cooldown_seconds": 60}}
    )
    urls = ["https://swagit-video.granicus.com/archive/x.mp4"]
    circuit.record_rate_limited(urls)

    assert circuit.record_circuit_deferred(urls) == "granicus.com"
    assert circuit.telemetry()["granicus.com"]["circuit_deferred"] == 1


def test_probe_audio_bitrate_sends_browser_user_agent(monkeypatch):
    import citypods.media as media
    from citypods.http import USER_AGENT

    captured: dict = {}

    class _Out:
        stdout = '{"streams":[{"codec_name":"aac","bit_rate":"128000"}]}'

    def _fake_run(argv, **kw):
        captured["argv"] = argv
        return _Out()

    monkeypatch.setattr(media.subprocess, "run", _fake_run)
    media._probe_audio_bitrate("https://archive-video.granicus.com/x.mp4")
    argv = captured["argv"]
    assert "-user_agent" in argv and argv[argv.index("-user_agent") + 1] == USER_AGENT


def test_identity_render_cmd_sends_browser_user_agent(monkeypatch):
    import citypods.media as media
    from citypods.http import USER_AGENT

    cmds: list[list[str]] = []
    monkeypatch.setattr(media, "_run_ffmpeg_guarded", lambda cmd, **kw: cmds.append(cmd))
    monkeypatch.setattr(
        media,
        "_probe_audio_stream",
        lambda *a, **k: media.AudioStreamInfo("aac", 96_000),
    )
    runner = media.CommandFfmpeg(max_kbps=MAX_KBPS)
    runner.extract_audio(
        None, {"s0": "https://archive-video.granicus.com/x.mp4"}, Path("/tmp/o.m4a")
    )
    cmd = cmds[0]
    assert "-user_agent" in cmd and cmd[cmd.index("-user_agent") + 1] == USER_AGENT


def test_ua_args_only_for_remote_inputs():
    import citypods.media as media
    from citypods.http import USER_AGENT

    assert media._ua_args("https://archive-video.granicus.com/x.mp4") == ["-user_agent", USER_AGENT]
    assert media._ua_args("http://x/y.mp4") == ["-user_agent", USER_AGENT]
    # Local cached copies (the source-cache hands these to the encode) must NOT get -user_agent —
    # ffmpeg errors "Option user_agent not found" on a file: input (the #293 regression).
    assert media._ua_args("/tmp/citypods_src_abc/deadbeef.mka") == []
    assert media._ua_args("file:///tmp/x.mka") == []


def test_identity_render_local_source_omits_user_agent(monkeypatch):
    """The encode of a source-cache LOCAL copy must not carry -user_agent (else returncode=8)."""
    import citypods.media as media

    cmds: list[list[str]] = []
    monkeypatch.setattr(media, "_run_ffmpeg_guarded", lambda cmd, **kw: cmds.append(cmd))
    monkeypatch.setattr(
        media,
        "_probe_audio_stream",
        lambda *a, **k: media.AudioStreamInfo("aac", 96_000),
    )
    runner = media.CommandFfmpeg(max_kbps=MAX_KBPS)
    runner.extract_audio(None, {"s0": "/tmp/citypods_src_x/cached.mka"}, Path("/tmp/o.m4a"))
    assert "-user_agent" not in cmds[0]


def test_probe_audio_bitrate_local_file_omits_user_agent(monkeypatch):
    import citypods.media as media

    captured: dict = {}

    class _Out:
        stdout = '{"streams":[{"codec_name":"aac","bit_rate":"128000"}]}'

    def _fake_run(argv, **kw):
        captured["argv"] = argv
        return _Out()

    monkeypatch.setattr(media.subprocess, "run", _fake_run)
    media._probe_audio_bitrate("/tmp/citypods_src_x/cached.mka")
    assert "-user_agent" not in captured["argv"]


def test_estimate_rss_copy_path_is_cheap():
    ep = _ep_with_duration(3600)  # no timeline, no loudnorm → copy path
    assert estimate_encode_rss_bytes(ep, loudness_profile="") == _ENCODE_RSS_COPY_BYTES


def test_estimate_rss_filter_scales_with_duration_and_clamps():
    ep = _ep_with_duration(3600)  # 60 min, loudnorm on → filter path
    one_hour = estimate_encode_rss_bytes(ep, loudness_profile="ebuR128:-16LUFS")
    assert _ENCODE_RSS_COPY_BYTES < one_hour < _ENCODE_RSS_MAX_BYTES

    ep.duration = 1800  # 30 min → smaller estimate (monotonic in served length)
    assert estimate_encode_rss_bytes(ep, loudness_profile="ebuR128:-16LUFS") < one_hour

    ep.duration = 100_000  # absurdly long → clamped to the observed ceiling
    clamped = estimate_encode_rss_bytes(ep, loudness_profile="ebuR128:-16LUFS")
    assert clamped == _ENCODE_RSS_MAX_BYTES


def test_estimate_rss_uses_edited_timeline_served_length():
    ep = _ep("e")  # no declared duration; the non-identity timeline supplies served length
    ep.timeline = _edited_timeline()
    est = estimate_encode_rss_bytes(ep, loudness_profile="")  # filter via non-identity timeline
    assert _ENCODE_RSS_COPY_BYTES < est < _ENCODE_RSS_MAX_BYTES


def test_estimate_rss_unknown_length_reserves_conservatively():
    ep = _ep("u")  # no timeline, no duration, but loudnorm on → filter path, length unknown
    est = estimate_encode_rss_bytes(ep, loudness_profile="ebuR128:-16LUFS")
    assert est == _ENCODE_RSS_UNKNOWN_BYTES


def test_estimate_rss_speech_profile_is_bounded_independent_of_duration():
    from citypods.media import _ENCODE_RSS_STREAMING_BYTES, PODCAST_SPEECH_PROFILE

    short = _ep_with_duration(60)
    long = _ep_with_duration(20_000)
    assert (
        estimate_encode_rss_bytes(
            short,
            loudness_profile="ebuR128:-16LUFS",
            processing_profile=PODCAST_SPEECH_PROFILE,
        )
        == _ENCODE_RSS_STREAMING_BYTES
    )
    assert (
        estimate_encode_rss_bytes(
            long,
            loudness_profile="ebuR128:-16LUFS",
            processing_profile=PODCAST_SPEECH_PROFILE,
        )
        == _ENCODE_RSS_STREAMING_BYTES
    )
