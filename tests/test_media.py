"""Tests for the audio materialization pipeline (content-addressed, spec-aware)."""

from __future__ import annotations

import shutil
import subprocess
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from citypods.http import StopRequested
from citypods.integrity import REPAIR_AUDIO_REMATERIALIZE, set_timeline_audio_integrity
from citypods.media import (
    _ENCODE_RSS_COPY_BYTES,
    _ENCODE_RSS_MAX_BYTES,
    _ENCODE_RSS_UNKNOWN_BYTES,
    SourceCache,
    _concat_local_sources,
    _concat_render_timeline,
    _fetch_mp4_header,
    _mp4_moov_extent,
    _probe_audio_duration_details,
    _probe_audio_duration_header,
    _probe_duration_secs,
    encode_args,
    estimate_encode_rss_bytes,
    materialize_audio,
)
from citypods.models import City, Episode
from citypods.records import audio_object_key, audio_spec_hash, source_key
from citypods.storage.local import LocalStorage
from citypods.timeline import Segment, SourceMedia, Timeline, timeline_digest

MAX_KBPS = 96


class FakeFfmpeg:
    """Minimal fake for materialize_audio tests — records calls, writes stub bytes."""

    def __init__(self, fail: bool = False):
        self.calls: list[str] = []  # first resolved URL per call
        self.chapters: list[list[dict] | None] = []
        self.timelines: list = []
        self.source_registries: list = []
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
        sources=None,
        loudness_profile=None,
        processing_profile=None,
        asset_resolver=None,
    ) -> None:
        # Expose the first source URL for backward-compat assertions
        first_url = next(iter(sources_by_id.values())) if sources_by_id else ""
        self.calls.append(first_url)
        self.chapters.append(chapters)
        self.timelines.append(timeline)
        self.source_registries.append(tuple(sources or ()))
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
        source={"feed_url": f"https://src/{slug}"},
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


def test_audio_encode_defers_without_backoff_when_source_cache_raises_stop_requested(
    tmp_path, capsys
):
    """The run's wall-clock budget expiring while queued on the source cache is not a source
    failure: defer without recording it as an error/backoff (#120-style false penalty)."""

    class _StoppingCache:
        def get_or_fetch(self, uid, url):
            raise StopRequested(f"source cache wait for uid={uid!r} stopped")

    ep = _ep("g1")
    stats = materialize_audio(
        _city(),
        [ep],
        storage=_store(tmp_path),
        ffmpeg=FakeFfmpeg(),
        max_kbps=MAX_KBPS,
        resolve_media_url=lambda e: e.video_url,
        source_cache=_StoppingCache(),
    )
    assert stats.skipped_budget == 1
    assert stats.defer_reasons == {"source-cache-stop": 1}
    assert stats.defer_samples == ["uid-g1:source-cache-stop"]
    assert stats.encoded == 0
    assert stats.errors == []
    assert ep.materialize_attempts == 0  # no backoff recorded for a budget-expiry defer
    out = capsys.readouterr().out
    assert "audio materialize deferred" in out
    assert "uid=uid-g1" in out
    assert "reason=source-cache-stop" in out


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
    assert stats.defer_reasons == {"resource-admission": 1}
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


def test_served_duration_uses_edl_for_identity_timeline():
    """An identity (unedited) timeline still carries one real full-span segment from the
    silence-planning pass that already downloaded and analyzed the source. Gating on
    ``timeline_digest() != ""`` (a cache-invalidation sentinel, not a "no duration data" signal)
    used to discard that already-known length and fall back to the frequently-unpopulated
    ``ep.duration`` instead — this is exactly why the identity case must use ``edl_duration``
    too, not skip it."""
    import citypods.media as media
    from citypods.timeline import identity_timeline

    ep = _ep("g1")
    ep.sources = [
        SourceMedia(
            id="s0",
            provider="granicus",
            ref="https://src/vid.mp4",
            media_kind="direct",
            duration=None,
            watch_url=None,
        )
    ]
    ep.timeline = identity_timeline(ep.sources[0], duration=5400.0)
    ep.duration = None  # the provider never reports a duration in feed metadata (e.g. Granicus)

    assert timeline_digest(ep.timeline, ep.sources) == ""  # confirms this genuinely is identity
    assert media._served_duration(ep) == pytest.approx(5400.0)


def test_served_duration_falls_back_to_source_when_edl_duration_is_degenerate():
    """``edl_duration`` can itself return ``None`` even with non-empty segments (a degenerate
    zero/negative-span timeline) — must still fall through to ``ep.duration`` rather than
    propagating that ``None``, matching pre-fix behavior for this edge case."""
    import citypods.media as media

    ep = _ep("g1")
    ep.timeline = Timeline(
        version="degenerate",
        segments=(
            Segment(
                served_start=10.0,
                served_end=10.0,  # zero span -> edl_duration returns None
                kind="source",
                source_id="s0",
                source_start=0.0,
                source_end=10.0,
            ),
        ),
    )
    ep.duration = 1800.0

    assert media._served_duration(ep) == pytest.approx(1800.0)


def test_credit_path_backfills_from_identity_timeline_when_source_duration_unknown(tmp_path):
    """Integration counterpart: the reuse/credit path (no fresh probe) must backfill
    ``audio_duration_served`` from an identity timeline's EDL length even when ``ep.duration``
    (the raw provider-reported source duration) is unknown."""
    from citypods.timeline import identity_timeline

    city = _city()
    store = _store(tmp_path)
    ep = _ep("g1")
    ep.sources = [
        SourceMedia(
            id="s0",
            provider="granicus",
            ref="https://src/vid.mp4",
            media_kind="direct",
            duration=None,
            watch_url=None,
        )
    ]
    ep.timeline = identity_timeline(ep.sources[0], duration=5400.0)
    ep.duration = None
    key = audio_object_key(city, ep, audio_spec_hash(ep, max_kbps=MAX_KBPS))
    _seed_object(store, key)

    stats = _materialize(city, [ep], store, FakeFfmpeg())

    assert stats.credited == 1
    assert ep.audio_duration_served == pytest.approx(5400.0)


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


class CountingStorage:
    """Wraps a real storage backend, counting ``list_objects`` calls per prefix (issue #344)."""

    def __init__(self, inner):
        self._inner = inner
        self.list_objects_calls: list[str] = []

    def list_objects(self, prefix: str = ""):
        self.list_objects_calls.append(prefix)
        return list(self._inner.list_objects(prefix))

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_hosted_keys_cache_lists_once_per_source(tmp_path):
    """``HostedKeysCache`` lists a source's storage prefix at most once, however many times
    ``materialize_audio`` is called for that source — the fix for the global queue (H5 PR3)
    dispatching ``AudioStage`` once per episode instead of once per source."""
    from citypods.media import HostedKeysCache

    city = _city()
    store = CountingStorage(_store(tmp_path))
    cache = HostedKeysCache()
    ff = FakeFfmpeg()

    # Simulate the global queue: one materialize_audio() call per episode, same source, same cache.
    for i in range(50):
        materialize_audio(
            city,
            [_ep(f"g{i}")],
            storage=store,
            ffmpeg=ff,
            max_kbps=MAX_KBPS,
            resolve_media_url=lambda e: e.video_url,
            hosted_keys_cache=cache,
        )

    assert len(store.list_objects_calls) == 1


def test_hosted_keys_cache_is_per_source(tmp_path):
    """Two distinct sources each get their own listing — the cache doesn't conflate sources."""
    from citypods.media import HostedKeysCache

    store = CountingStorage(_store(tmp_path))
    cache = HostedKeysCache()
    ff = FakeFfmpeg()

    for slug in ("city-a", "city-b"):
        city = _city(slug=slug)
        materialize_audio(
            city,
            [_ep(f"{slug}-g1")],
            storage=store,
            ffmpeg=ff,
            max_kbps=MAX_KBPS,
            resolve_media_url=lambda e: e.video_url,
            hosted_keys_cache=cache,
        )

    assert len(store.list_objects_calls) == 2


def test_audio_artifact_cache_encodes_duplicate_source_views_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from citypods.media import AudioArtifactCache

    store = _store(tmp_path)
    cache = AudioArtifactCache()
    ff = FakeFfmpeg()
    city_a = _city(slug="combined")
    city_b = _city(slug="board")
    ep_a = _ep("shared")
    ep_b = _ep("shared")
    source_a = source_key(city_a)
    source_b = source_key(city_b)
    assert source_a != source_b
    for source in (source_a, source_b):
        cache.register(city_a.provider, source, "uid-shared")

    def _run(city, ep):
        return materialize_audio(
            city,
            [ep],
            storage=store,
            ffmpeg=ff,
            max_kbps=MAX_KBPS,
            resolve_media_url=lambda e: e.video_url,
            audio_artifact_cache=cache,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        stats_a, stats_b = list(
            pool.map(lambda args: _run(*args), [(city_a, ep_a), (city_b, ep_b)])
        )

    assert len(ff.calls) == 1
    assert ep_a.audio_key == ep_b.audio_key
    assert ep_a.audio_key.startswith(f"{city_a.provider}/{min(source_a, source_b)}/")
    assert sum(stats.encoded for stats in (stats_a, stats_b)) == 1
    assert sum(stats.reused for stats in (stats_a, stats_b)) == 1
    assert len(list(store.list_objects(f"{city_a.provider}/"))) == 1


def test_coalesced_follower_keeps_valid_served_duration(tmp_path):
    # GH#421 follow-up / Audio #58: a credited canonical winner can carry no probed duration. A
    # follower adopting the shared artifact must NOT regress to 0s — it backfills from its own
    # timeline/source (the audio is identical across the recipe).
    from citypods.media import AudioArtifact, AudioArtifactCache, audio_spec_hash

    store = _store(tmp_path)
    ff = FakeFfmpeg()
    cache = AudioArtifactCache()
    city = _city(slug="board")
    ep = _ep("shared")
    ep.duration = 3600  # the source declares a duration, so _served_duration can backfill
    src = source_key(city)
    spec = audio_spec_hash(ep, max_kbps=MAX_KBPS)
    winner_key = f"{city.provider}/{src}/{ep.uid}-{spec}.m4a"
    cache.register(city.provider, src, ep.uid)
    # The canonical winner completed WITHOUT a probed duration (e.g. credited from storage).
    cache.complete(
        (city.provider, ep.uid, spec),
        AudioArtifact(
            key=winner_key,
            spec=spec,
            url=store.public_url(winner_key),
            duration=None,
            size=123,
            encoded_at="2026-06-23T00:00:00+00:00",
        ),
    )

    materialize_audio(
        city,
        [ep],
        storage=store,
        ffmpeg=ff,
        max_kbps=MAX_KBPS,
        resolve_media_url=lambda e: e.video_url,
        audio_artifact_cache=cache,
    )

    assert ep.audio_key == winner_key  # adopted the shared object
    assert ep.audio_duration_served == pytest.approx(3600.0)  # NOT downgraded to None/0
    assert ff.calls == []  # follower did not re-encode


def test_global_queue_drain_is_bounded_by_sources_not_episodes(tmp_path):
    """Storage listings scale with the number of sources, not the number of queued episodes —
    the timing/operation-count contract from issue #344. Without ``hosted_keys_cache``, this same
    loop would issue one ``list_objects`` call per episode."""
    from citypods.media import HostedKeysCache

    n_sources = 5
    n_episodes_per_source = 400  # thousands in production; smaller here for test speed
    store = CountingStorage(_store(tmp_path))
    cache = HostedKeysCache()
    ff = FakeFfmpeg()
    cities = [_city(slug=f"city-{i}") for i in range(n_sources)]

    # Global queue: episodes from every source interleaved, stop() latched from the start so
    # every episode takes the cheap deferred path (no encodes) — exactly the drain scenario.
    for i in range(n_episodes_per_source):
        for city in cities:
            materialize_audio(
                city,
                [_ep(f"{city.slug}-g{i}")],
                storage=store,
                ffmpeg=ff,
                max_kbps=MAX_KBPS,
                resolve_media_url=lambda e: e.video_url,
                hosted_keys_cache=cache,
                stop=lambda: True,
            )

    assert len(store.list_objects_calls) == n_sources
    assert ff.calls == []


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


def test_errored_record_is_re_encoded_not_reused(tmp_path):
    """An episode flagged with a materialization error must not be reused/credited even when a
    stale matching-spec object is still in storage: re-encode so a clean pass clears the error and
    records a served duration. This surfaces the errored episodes the ASR lane skips (run #25)."""
    ep = _ep("g1")
    ep.duration = 3600
    city = _city()
    store = _store(tmp_path)
    spec = audio_spec_hash(ep, max_kbps=MAX_KBPS)
    key = audio_object_key(city, ep, spec)
    _seed_object(store, key)  # stale object from the failed encode is present
    ep.audio_key = key
    ep.hosted_audio_url = store.public_url(key)
    ep.audio_spec_hash = spec
    ep.materialize_error = "error"

    ff = FakeFfmpeg()
    stats = _materialize(city, [ep], store, ff)

    assert stats.reused == 0 and stats.credited == 0
    assert stats.hosted == 1
    assert ff.calls == ["https://src/manifest.m3u8"]  # actually re-encoded
    assert ep.materialize_error is None  # cleared by the successful pass


def test_matching_spec_keeps_recorded_served_duration(tmp_path):
    """A reuse of an already-recorded served duration must not be overwritten with the EDL sum.

    review/20: ``audio_duration_served`` is the measured hosted-file duration. The reuse path no
    longer "corrects" it to the EDL total (3300.0) — it preserves the recorded 3300.8."""
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
    assert ep.audio_duration_served == pytest.approx(3300.8)


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
        sources=None,
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
    assert stats.defer_reasons == {"error-backoff": 1}
    assert stats.defer_samples == ["uid-bad:error-backoff"]
    assert ff.calls == []  # never attempted


def test_repair_spec_change_bypasses_stale_materialize_backoff_once(tmp_path):
    """A targeted repair recipe is new work, so an old generic backoff must not block its first
    encode attempt. If that new spec fails, ``error_spec_hash`` keys normal backoff again."""
    ep = _ep("bad", url="https://src/manifest.m3u8")
    ep.materialize_attempts = 4
    ep.materialize_last_attempt = datetime.now(UTC).isoformat()
    ep.materialize_error = "error"
    ep.materialize_error_spec_hash = None  # old record from before spec-keyed failure tracking
    set_timeline_audio_integrity(
        ep,
        {
            "status": "rendered-duration-mismatch",
            "repair": [REPAIR_AUDIO_REMATERIALIZE],
        },
    )
    ff = FakeFfmpeg()

    stats = _materialize(_city(), [ep], _store(tmp_path), ff)

    assert stats.encoded == 1
    assert stats.skipped_backoff == 0
    assert ff.calls == ["https://src/manifest.m3u8"]
    assert ep.materialize_attempts == 0
    assert ep.materialize_error is None
    assert ep.materialize_error_spec_hash is None


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
            sources=None,
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


def test_probe_audio_duration_details_uses_stream_sample_clock(tmp_path):
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe required for sample-clock duration probe")

    audio = tmp_path / "audio.m4a"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=1.5",
            "-c:a",
            "aac",
            str(audio),
        ],
        check=True,
        timeout=30,
    )

    probe = _probe_audio_duration_details(audio)

    assert probe.container_duration == pytest.approx(1.5, abs=0.01)
    assert probe.stream_sample_duration == pytest.approx(1.5, abs=0.01)
    assert probe.stream_duration_source == "stream-duration-ts"
    assert probe.probe_error is None


def test_probe_audio_duration_details_reports_ffprobe_error(tmp_path):
    probe = _probe_audio_duration_details(tmp_path / "missing.m4a")

    assert probe.container_duration is None
    assert probe.stream_sample_duration is None
    assert probe.probe_error == "ffprobe-error"


# ---------------------------------------------------------------------------
# Header-only (range-read) duration probe
# ---------------------------------------------------------------------------


def _mp4_box(fourcc: bytes, payload: bytes = b"") -> bytes:
    """A synthetic top-level MP4 box: 4-byte size + 4-byte type + payload. Only the box
    header matters to ``_mp4_moov_extent``/``_fetch_mp4_header`` — the payload bytes never
    need to be a real, parseable ``moov``/``ftyp`` for these unit tests."""
    return (8 + len(payload)).to_bytes(4, "big") + fourcc + payload


class TestMp4MoovExtent:
    def test_locates_moov_between_ftyp_and_mdat(self):
        ftyp = _mp4_box(b"ftyp", b"M4A \x00\x00\x02\x00")
        moov = _mp4_box(b"moov", b"x" * 100)
        mdat = _mp4_box(b"mdat", b"y" * 1000)
        buf = ftyp + moov + mdat

        assert _mp4_moov_extent(buf) == (len(ftyp), len(ftyp) + len(moov))

    def test_skips_a_leading_64bit_largesize_box(self):
        payload = b"z" * 50
        large_free = (
            (1).to_bytes(4, "big") + b"free" + (16 + len(payload)).to_bytes(8, "big") + payload
        )
        moov = _mp4_box(b"moov", b"x" * 20)
        buf = large_free + moov

        assert _mp4_moov_extent(buf) == (len(large_free), len(large_free) + len(moov))

    def test_returns_none_when_huge_mdat_precedes_moov(self):
        # A non-faststart object: mdat (declared far larger than any prefix read) comes first,
        # so moov is unreachable from a bounded initial read — the fast path does not apply.
        ftyp = _mp4_box(b"ftyp")
        huge_mdat_header = (50_000_000).to_bytes(4, "big") + b"mdat"
        buf = ftyp + huge_mdat_header

        assert _mp4_moov_extent(buf) is None

    def test_returns_none_for_a_zero_sized_extends_to_eof_box(self):
        buf = _mp4_box(b"ftyp") + (0).to_bytes(4, "big") + b"mdat"

        assert _mp4_moov_extent(buf) is None

    def test_returns_none_for_a_too_short_buffer(self):
        assert _mp4_moov_extent(b"abc") is None


def _fake_get_range(buf: bytes):
    calls: list[tuple[int, int]] = []

    def _get(start: int, end: int) -> bytes:
        calls.append((start, end))
        return buf[start : end + 1]

    return _get, calls


class TestFetchMp4Header:
    def test_single_round_trip_when_moov_fits_the_initial_chunk(self):
        ftyp = _mp4_box(b"ftyp")
        moov = _mp4_box(b"moov", b"x" * 100)
        mdat = _mp4_box(b"mdat", b"y" * 100_000)
        get_range, calls = _fake_get_range(ftyp + moov + mdat)

        header = _fetch_mp4_header(get_range)

        assert header == ftyp + moov
        assert len(calls) == 1

    def test_second_round_trip_when_moov_spans_the_initial_chunk(self):
        import citypods.media as media

        ftyp = _mp4_box(b"ftyp")
        moov = _mp4_box(b"moov", b"x" * (media._MP4_INITIAL_RANGE_BYTES + 5000))
        mdat = _mp4_box(b"mdat", b"y" * 1000)
        get_range, calls = _fake_get_range(ftyp + moov + mdat)

        header = _fetch_mp4_header(get_range)

        assert header == ftyp + moov
        assert len(calls) == 2
        assert calls[0] == (0, media._MP4_INITIAL_RANGE_BYTES - 1)

    def test_returns_none_when_moov_is_not_found(self):
        ftyp = _mp4_box(b"ftyp")
        huge_mdat_header = (50_000_000).to_bytes(4, "big") + b"mdat"
        get_range, _ = _fake_get_range(ftyp + huge_mdat_header)

        assert _fetch_mp4_header(get_range) is None

    def test_returns_none_and_skips_second_read_past_the_safety_cap(self):
        import citypods.media as media

        ftyp = _mp4_box(b"ftyp")
        size = media._MP4_MAX_MOOV_BYTES + 1000 + 8
        moov_header_only = size.to_bytes(4, "big") + b"moov"
        get_range, calls = _fake_get_range(ftyp + moov_header_only)

        assert _fetch_mp4_header(get_range) is None
        assert len(calls) == 1  # never attempts to fetch the oversized claimed moov

    def test_returns_none_when_the_initial_range_read_fails(self):
        assert _fetch_mp4_header(lambda s, e: None) is None
        assert _fetch_mp4_header(lambda s, e: b"") is None

    def test_returns_none_when_the_second_read_is_truncated(self):
        # A storage backend is allowed to return fewer bytes than requested when the range
        # extends past EOF (e.g. LocalStorage.get_range). If moov's second read comes back
        # short — a corrupt/truncated object, not a normal EOF-at-file-end case since moov's
        # end was computed from its own declared size — the header must not be trusted as
        # complete; ffprobe could otherwise report a definitive-looking but wrong duration.
        import citypods.media as media

        ftyp = _mp4_box(b"ftyp")
        moov = _mp4_box(b"moov", b"x" * (media._MP4_INITIAL_RANGE_BYTES + 5000))
        full = ftyp + moov

        def _get(start: int, end: int) -> bytes:
            # Truncate the second read to 10 bytes short of what was asked for.
            return full[start : min(end + 1, len(full) - 10)]

        assert _fetch_mp4_header(_get) is None


def _local_get_range(path: Path):
    def _get(start: int, end: int) -> bytes:
        with open(path, "rb") as f:
            f.seek(start)
            return f.read(end - start + 1)

    return _get


def _write_synthetic_m4a(path: Path, duration_seconds: float) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=48000:duration={duration_seconds}",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(path),
        ],
        check=True,
        timeout=60,
    )


def test_probe_audio_duration_header_matches_full_probe_short_clip(tmp_path):
    """The core assumption behind the header-only probe (GH follow-up to review/20's
    duration-measurement churn): for this project's own faststart-remuxed hosted audio, a
    header-only (range-read) probe must report *exactly* what a full-download probe reports,
    since both derive their numbers from the same `moov` bytes and neither touches `mdat`."""
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe required for the header-vs-full-probe parity check")

    audio = tmp_path / "audio.m4a"
    _write_synthetic_m4a(audio, 1.5)

    full = _probe_audio_duration_details(audio)
    header = _probe_audio_duration_header(_local_get_range(audio))

    assert header is not None
    assert header.container_duration == full.container_duration
    assert header.stream_sample_duration == full.stream_sample_duration
    assert header.stream_duration_source == full.stream_duration_source
    assert header.probe_error == full.probe_error is None


def test_probe_audio_duration_header_matches_full_probe_when_moov_spans_two_range_reads(
    tmp_path,
):
    """Same parity check as above, but for a long enough episode that its `moov` (dominated by
    per-frame stsz/stco tables) exceeds the initial range-read chunk — forcing the second,
    exactly-sized read this project's episodes routinely need in production."""
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe required for the header-vs-full-probe parity check")

    audio = tmp_path / "audio.m4a"
    _write_synthetic_m4a(audio, 400)  # ~76KB moov, past the 64KB initial chunk

    calls: list[tuple[int, int]] = []

    def _get(start: int, end: int) -> bytes:
        calls.append((start, end))
        return _local_get_range(audio)(start, end)

    full = _probe_audio_duration_details(audio)
    header = _probe_audio_duration_header(_get)

    assert len(calls) == 2, "fixture didn't actually exercise the two-round-trip path"
    assert header is not None
    assert header.container_duration == full.container_duration
    assert header.stream_sample_duration == full.stream_sample_duration


def _build_gapped_ts(tmp_path: Path, gap_seconds: float = 2.0) -> Path:
    """Build a short source whose second half has a forward PTS discontinuity."""
    seg1 = tmp_path / "seg1.ts"
    seg2 = tmp_path / "seg2.ts"
    gapped = tmp_path / "gapped.ts"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=5",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-muxdelay",
            "0",
            "-muxpreload",
            "0",
            str(seg1),
        ],
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:duration=5",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-output_ts_offset",
            str(5.0 + gap_seconds),
            "-muxdelay",
            "0",
            "-muxpreload",
            "0",
            str(seg2),
        ],
        check=True,
    )
    gapped.write_bytes(seg1.read_bytes() + seg2.read_bytes())
    return gapped


def test_streaming_single_source_filter_selects_on_compacted_pts(tmp_path):
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe required for PTS-gap render check")

    import citypods.media as media

    source = _build_gapped_ts(tmp_path, gap_seconds=2.0)
    timeline = Timeline(
        version="silence-test",
        segments=(
            Segment(
                served_start=0.0,
                served_end=10.0,
                kind="source",
                source_id="s0",
                source_start=0.0,
                source_end=10.0,
            ),
        ),
    )
    out = tmp_path / "out.m4a"

    media.CommandFfmpeg(max_kbps=MAX_KBPS, timeout_seconds=120, threads=1).extract_audio(
        timeline,
        {"s0": str(source)},
        out,
        sources=[
            SourceMedia(
                id="s0",
                provider="test",
                ref=str(source),
                media_kind="direct",
                duration=12.0,
                watch_url=None,
            )
        ],
    )

    assert _probe_duration_secs(out) == pytest.approx(10.0, abs=0.15)


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


def test_audio_duration_served_is_probed_hosted_duration(monkeypatch, tmp_path):
    """Edited timelines store the probed hosted-stream duration, not the EDL total (review/20).

    The served clock is the real file: a render that disagrees with the EDL must stay visible to the
    audit rather than being overwritten with the EDL sum."""
    import citypods.media as media

    ep = _ep("g1")
    ep.timeline = _edited_timeline()  # EDL total 3300.0
    monkeypatch.setattr(media, "_probe_duration_secs", lambda *args, **kwargs: 3300.8)

    stats = _materialize(_city(), [ep], _store(tmp_path), FakeFfmpeg())

    assert stats.encoded == 1
    assert ep.audio_duration_served == pytest.approx(3300.8)


def test_materialize_passes_source_registry_to_ffmpeg(tmp_path):
    ep = _ep("g1")
    ep.sources = [
        SourceMedia(
            id="s0",
            provider="granicus",
            ref="https://src/vid.mp4",
            media_kind="direct",
            duration=3600.0,
            watch_url=None,
        )
    ]
    ep.timeline = Timeline(
        version="buggy-tail-trim",
        segments=(
            Segment(
                served_start=0.0,
                served_end=1800.0,
                kind="source",
                source_id="s0",
                source_start=0.0,
                source_end=1800.0,
            ),
        ),
    )
    ff = FakeFfmpeg()

    _materialize(_city(), [ep], _store(tmp_path), ff)

    assert ff.source_registries == [tuple(ep.sources)]


def test_materialize_concat_cache_uses_render_timeline_identity_context(tmp_path):
    ep = _ep("g1")
    ep.sources = [
        SourceMedia(
            id="s0",
            provider="swagit",
            ref="https://src/seg0.mp4",
            media_kind="direct",
            duration=10.0,
            watch_url=None,
        ),
        SourceMedia(
            id="s1",
            provider="swagit",
            ref="https://src/seg1.mp4",
            media_kind="direct",
            duration=20.0,
            watch_url=None,
        ),
    ]
    ep.timeline = Timeline(
        version="concat-v1",
        segments=(
            Segment(0.0, 10.0, "source", "s0", 0.0, 10.0),
            Segment(10.0, 30.0, "source", "s1", 0.0, 20.0),
        ),
    )
    combined = tmp_path / "combined.mka"
    combined.write_bytes(b"combined")

    class _Cache:
        def get_or_fetch_concat(self, uid, sources):
            return combined

    ff = FakeFfmpeg()
    materialize_audio(
        _city(),
        [ep],
        storage=_store(tmp_path),
        ffmpeg=ff,
        max_kbps=MAX_KBPS,
        resolve_media_url=lambda e: pytest.fail("multi-source render should not resolve URL"),
        source_cache=_Cache(),
    )

    assert ff.timelines[0].segments[0].source_id == "combined"
    assert ff.source_registries == [()]


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
    from tests._cas_fake import MemCAS

    url = "https://archive-video.granicus.com/x.mp4"
    store = MemCAS()  # the distributed pool needs real CAS (R2); LocalStorage is non-CAS

    class _CountingPool(DistributedProviderLeasePool):
        """Wraps real lease acquisition to track how many leases *this shard* holds concurrently,
        independent of where in the call stack they're acquired from."""

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.active = 0
            self.peak = 0
            self._count_lock = threading.Lock()

        def slots(self, urls, *, stop=None):
            inner = super().slots(urls, stop=stop)

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


def test_run_ffmpeg_guarded_redacts_signed_url_from_logs_and_exception(monkeypatch):
    import citypods.media as media

    signed = (
        "https://swagit-video.granicus.com/archive/video.mp4"
        "?X-Amz-Credential=secret&X-Amz-Signature=also-secret"
    )
    err = subprocess.CalledProcessError(
        1,
        ["ffmpeg", "-i", signed],
        stderr=f"{signed}: Invalid data found".encode(),
    )
    monkeypatch.setattr(media.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(err))
    logs: list[str] = []

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        media._run_ffmpeg_guarded(
            ["ffmpeg", "-i", signed],
            phase="source-cache",
            rate_limit_urls=(signed,),
            log=logs.append,
        )

    rendered = f"{excinfo.value} {excinfo.value.cmd} {excinfo.value.stderr} {' '.join(logs)}"
    assert "swagit-video.granicus.com/archive/video.mp4" in rendered
    assert "Invalid data found" in rendered
    assert "X-Amz-" not in rendered
    assert "secret" not in rendered


def test_run_ffmpeg_guarded_records_direct_granicus_success(monkeypatch):
    import citypods.media as media

    archive_url = (
        "https://archive-video.granicus.com/arlingtontx/"
        "arlingtontx_f65c7a2f-9c73-4d9b-b7b7-205f7c12c0bf.mp4"
    )
    monkeypatch.setattr(
        media.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, b"audio", b""),
    )
    circuit = media.ProviderTransportTelemetry({"granicus.com": {"threshold": 3}})

    media._run_ffmpeg_guarded(
        ["ffmpeg", "-i", archive_url, "out.m4a"],
        phase="source-cache",
        rate_limit_urls=(archive_url,),
        transport_telemetry=circuit,
        log=lambda *_args: None,
    )

    telemetry = circuit.telemetry()["granicus.com/tenant:arlingtontx"]
    assert telemetry["direct_fetch_successes"] == 1
    assert telemetry["direct_fetch_403s"] == 0


def test_run_ffmpeg_guarded_uses_worker_once_after_direct_granicus_403(monkeypatch):
    import citypods.media as media

    archive_url = (
        "https://archive-video.granicus.com/arlingtontx/"
        "arlingtontx_f65c7a2f-9c73-4d9b-b7b7-205f7c12c0bf.mp4"
    )
    monkeypatch.setenv("GRANICUS_PROXY_BASE_URL", "worker.example")
    monkeypatch.setenv("GRANICUS_PROXY_TOKEN", "secret-token")
    monkeypatch.setattr("citypods.granicus_proxy.validate_source_url", lambda _url: None)
    calls: list[list[str]] = []

    def _run(command, **_kwargs):
        calls.append(command)
        if len(calls) == 1:
            raise subprocess.CalledProcessError(
                1,
                command,
                stderr=b"Server returned 403 Forbidden",
            )
        return subprocess.CompletedProcess(command, 0, b"audio", b"")

    monkeypatch.setattr(media.subprocess, "run", _run)
    logs: list[str] = []
    circuit = media.ProviderTransportTelemetry(
        {"granicus.com": {"threshold": 1, "cooldown_seconds": 60}}
    )

    stdout, _stderr = media._run_ffmpeg_guarded(
        ["ffmpeg", "-i", archive_url, "out.m4a"],
        phase="source-cache",
        rate_limit_urls=(archive_url,),
        transport_telemetry=circuit,
        log=logs.append,
    )

    assert stdout == b"audio"
    assert len(calls) == 2
    assert calls[0][calls[0].index("-i") + 1] == archive_url
    assert calls[1][calls[1].index("-i") + 1].startswith("https://worker.example/")
    assert "Authorization: Bearer secret-token\r\n" in calls[1]
    assert any("strategy=cloudflare-worker" in line for line in logs)
    assert all("secret-token" not in line for line in logs)
    # GH#337 telemetry: one fallback attempt, recovered, on the Arlington tenant scope.
    telemetry = circuit.telemetry()
    assert sum(row["worker_fallback_attempts"] for row in telemetry.values()) == 1
    assert sum(row["worker_fallback_successes"] for row in telemetry.values()) == 1
    assert sum(row["worker_fallback_failures"] for row in telemetry.values()) == 0
    assert sum(row["direct_fetch_403s"] for row in telemetry.values()) == 1
    assert sum(row["direct_fetch_successes"] for row in telemetry.values()) == 0


def test_worker_failure_records_provider_throttle_without_leaking_token(monkeypatch):
    import citypods.media as media

    archive_url = (
        "https://archive-video.granicus.com/arlingtontx/"
        "arlingtontx_f65c7a2f-9c73-4d9b-b7b7-205f7c12c0bf.mp4"
    )
    monkeypatch.setenv("GRANICUS_PROXY_BASE_URL", "worker.example")
    monkeypatch.setenv("GRANICUS_PROXY_TOKEN", "secret-token")
    monkeypatch.setattr("citypods.granicus_proxy.validate_source_url", lambda _url: None)

    def _run(command, **_kwargs):
        raise subprocess.CalledProcessError(
            1,
            command,
            stderr=b"Server returned 403 Forbidden",
        )

    monkeypatch.setattr(media.subprocess, "run", _run)
    circuit = media.ProviderTransportTelemetry(
        {"granicus.com": {"threshold": 1, "cooldown_seconds": 60}}
    )

    with pytest.raises(media.RateLimitedMediaFetchError) as excinfo:
        media._run_ffmpeg_guarded(
            ["ffmpeg", "-i", archive_url, "out.m4a"],
            phase="source-cache",
            rate_limit_urls=(archive_url,),
            transport_telemetry=circuit,
            log=lambda _line: None,
        )

    assert "secret-token" not in str(excinfo.value)
    telemetry = circuit.telemetry()
    assert sum(row["worker_fallback_attempts"] for row in telemetry.values()) == 1
    assert sum(row["worker_fallback_failures"] for row in telemetry.values()) == 1


def test_monitored_source_cache_uses_worker_after_direct_granicus_403(monkeypatch):
    import citypods.media as media

    archive_url = (
        "https://archive-video.granicus.com/arlingtontx/"
        "arlingtontx_f65c7a2f-9c73-4d9b-b7b7-205f7c12c0bf.mp4"
    )
    monkeypatch.setenv("GRANICUS_PROXY_BASE_URL", "worker.example")
    monkeypatch.setenv("GRANICUS_PROXY_TOKEN", "secret-token")
    monkeypatch.setattr("citypods.granicus_proxy.validate_source_url", lambda _url: None)
    commands: list[list[str]] = []

    class _Process:
        pid = 123

        def __init__(self, command):
            self.command = command
            self.returncode = 1 if len(commands) == 1 else 0

        def poll(self):
            return self.returncode

        def communicate(self, timeout=None):
            if self.returncode:
                return b"", b"Server returned 403 Forbidden"
            return b"audio", b""

    def _popen(command, **_kwargs):
        commands.append(command)
        return _Process(command)

    circuit = media.ProviderTransportTelemetry(
        {"granicus.com": {"threshold": 1, "cooldown_seconds": 60}}
    )
    stdout, _stderr = media._run_ffmpeg_guarded(
        ["ffmpeg", "-i", archive_url, "out.mka"],
        phase="source-cache",
        memory_floor_bytes=1,
        rate_limit_urls=(archive_url,),
        transport_telemetry=circuit,
        snapshot=_snap,
        sleep=lambda _seconds: None,
        log=lambda _line: None,
        popen=_popen,
        child_rss=lambda _pid: 0,
    )

    assert stdout == b"audio"
    assert len(commands) == 2
    assert commands[1][commands[1].index("-i") + 1].startswith("https://worker.example/")
    telemetry = circuit.telemetry()
    assert sum(row["worker_fallback_successes"] for row in telemetry.values()) == 1


def test_monitored_worker_failure_records_throttle_and_redacts_endpoint(monkeypatch):
    """Memory-floor path: a Worker that also 403s records the throttle exactly once and the
    worker-fallback failure once, never leaks the bearer token, and scrubs the secret Worker
    endpoint that ffmpeg's stderr echoes at ``-loglevel error`` (R3 + R4 coverage)."""
    import citypods.media as media

    archive_url = (
        "https://archive-video.granicus.com/arlingtontx/"
        "arlingtontx_f65c7a2f-9c73-4d9b-b7b7-205f7c12c0bf.mp4"
    )
    monkeypatch.setenv("GRANICUS_PROXY_BASE_URL", "worker.example")
    monkeypatch.setenv("GRANICUS_PROXY_TOKEN", "secret-token")
    monkeypatch.setattr("citypods.granicus_proxy.validate_source_url", lambda _url: None)
    commands: list[list[str]] = []

    class _Process:
        pid = 123

        def __init__(self, command):
            self.command = command
            self.is_worker = any(
                isinstance(arg, str) and arg.startswith("https://worker.example/")
                for arg in command
            )

        def poll(self):
            return 1

        def communicate(self, timeout=None):
            if self.is_worker:
                # ffmpeg echoes the failing input URL in stderr; the Worker endpoint is a secret.
                return b"", b"https://worker.example/v1/archive/x: Server returned 403 Forbidden"
            return b"", b"Server returned 403 Forbidden"

    def _popen(command, **_kwargs):
        commands.append(command)
        return _Process(command)

    logs: list[str] = []
    circuit = media.ProviderTransportTelemetry(
        {"granicus.com": {"threshold": 1, "cooldown_seconds": 60}}
    )

    with pytest.raises(media.RateLimitedMediaFetchError) as excinfo:
        media._run_ffmpeg_guarded(
            ["ffmpeg", "-i", archive_url, "out.mka"],
            phase="source-cache",
            memory_floor_bytes=1,
            rate_limit_urls=(archive_url,),
            transport_telemetry=circuit,
            snapshot=_snap,
            sleep=lambda _seconds: None,
            log=logs.append,
            popen=_popen,
            child_rss=lambda _pid: 0,
        )

    assert len(commands) == 2  # one direct, one Worker — no third attempt
    assert "secret-token" not in str(excinfo.value)
    telemetry = circuit.telemetry()
    assert sum(row["worker_fallback_attempts"] for row in telemetry.values()) == 1
    assert sum(row["worker_fallback_failures"] for row in telemetry.values()) == 1
    assert all("secret-token" not in line for line in logs)
    assert all("worker.example" not in line for line in logs)
    assert any("<granicus-worker>" in line for line in logs)


def test_half_configured_worker_secret_does_not_mask_direct_403(monkeypatch):
    """A half-set GRANICUS_PROXY_* config must not turn a handled 403 into an uncaught ValueError:
    the fallback disables itself and the normal throttle classification still runs (R2)."""
    import citypods.media as media

    archive_url = (
        "https://archive-video.granicus.com/arlingtontx/"
        "arlingtontx_f65c7a2f-9c73-4d9b-b7b7-205f7c12c0bf.mp4"
    )
    monkeypatch.setenv("GRANICUS_PROXY_BASE_URL", "worker.example")
    monkeypatch.delenv("GRANICUS_PROXY_TOKEN", raising=False)

    def _run(command, **_kwargs):
        raise subprocess.CalledProcessError(1, command, stderr=b"Server returned 403 Forbidden")

    monkeypatch.setattr(media.subprocess, "run", _run)
    circuit = media.ProviderTransportTelemetry(
        {"granicus.com": {"threshold": 1, "cooldown_seconds": 60}}
    )

    with pytest.raises(media.RateLimitedMediaFetchError):
        media._run_ffmpeg_guarded(
            ["ffmpeg", "-i", archive_url, "out.m4a"],
            phase="source-cache",
            rate_limit_urls=(archive_url,),
            transport_telemetry=circuit,
            log=lambda _line: None,
        )

    telemetry = circuit.telemetry()
    assert sum(row["worker_fallback_attempts"] for row in telemetry.values()) == 0


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


def test_download_audio_raises_before_ffmpeg_when_preflight_reports_too_large(monkeypatch):
    """Issue #497: a source-cache fetch must reject an honestly-oversized source before ffmpeg
    ever starts, and must not be swallowed into a plain ``return False`` (which callers treat as
    "fall back to streaming the URL directly" — exactly what the guard must prevent)."""
    import citypods.media as media
    from citypods.http import MediaSizePreflight
    from citypods.security import MediaSourceTooLargeError

    monkeypatch.setattr(
        media,
        "preflight_media_size",
        lambda url, max_bytes, **kw: MediaSizePreflight(
            status="known_too_large", content_length=max_bytes + 1
        ),
    )
    cmds: list[list[str]] = []
    monkeypatch.setattr(media, "_run_ffmpeg_guarded", lambda cmd, **kw: cmds.append(cmd))

    with pytest.raises(MediaSourceTooLargeError):
        media._download_audio(
            "https://archive-video.granicus.com/x.mp4",
            Path("/tmp/nope.mka"),
            max_media_bytes=100,
        )
    assert cmds == []  # ffmpeg must never be invoked for a rejected source


@pytest.mark.parametrize("status", ["known_ok", "unknown"])
def test_download_audio_proceeds_when_preflight_does_not_reject(monkeypatch, status):
    import citypods.media as media
    from citypods.http import MediaSizePreflight

    monkeypatch.setattr(
        media,
        "preflight_media_size",
        lambda url, max_bytes, **kw: MediaSizePreflight(status=status, content_length=10),
    )
    cmds: list[list[str]] = []
    monkeypatch.setattr(media, "_run_ffmpeg_guarded", lambda cmd, **kw: cmds.append(cmd))

    media._download_audio(
        "https://archive-video.granicus.com/x.mp4", Path("/tmp/nope.mka"), max_media_bytes=100
    )
    assert len(cmds) == 1


def test_download_audio_skips_preflight_for_truncated_probe_fetch(monkeypatch):
    """A ``max_seconds``-truncated fetch (the live contract check) is already bounded regardless
    of the source's real size, so it shouldn't pay for (or be rejected by) a preflight."""
    import citypods.media as media

    calls: list[str] = []
    monkeypatch.setattr(
        media, "preflight_media_size", lambda *a, **k: calls.append("called") or None
    )
    monkeypatch.setattr(media, "_run_ffmpeg_guarded", lambda cmd, **kw: None)

    media._download_audio(
        "https://x/y.mp4", Path("/tmp/nope.mka"), max_seconds=3, max_media_bytes=100
    )
    assert calls == []


def test_source_cache_media_too_large_does_not_fall_back_to_direct_render(tmp_path):
    """Issue #497 acceptance criterion: a guard-triggered rejection must not silently fall back to
    an unguarded direct ffmpeg stream of the same oversized URL."""
    from citypods.security import MediaSourceTooLargeError

    class _TooLargeCache:
        def get_or_fetch(self, uid, url):
            raise MediaSourceTooLargeError(f"source {url} advertises 999 bytes, exceeds cap 100")

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
        source_cache=_TooLargeCache(),
    )

    assert ff.calls == []  # no unguarded direct-stream fallback
    assert ep.materialize_error == "media-too-large"
    assert len(stats.errors) == 1


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


class TestSourceCacheStopAware:
    """A caller queued behind another thread's fetch of the same uid yields ``StopRequested``
    once the run's wall-clock budget expires, instead of blocking out that fetch's lock."""

    def test_stop_firing_raises_instead_of_blocking(self, tmp_path):
        with SourceCache(stop=lambda: True) as cache:
            with cache._guard:
                lock = cache._locks["ep1"]
            lock.acquire()  # simulate another thread already fetching uid="ep1"
            try:
                with pytest.raises(StopRequested):
                    cache.get_or_fetch("ep1", "https://example.com/a.mp4")
            finally:
                lock.release()

    def test_stop_never_firing_proceeds_to_fetch(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "citypods.media._download_audio", lambda *a, **k: False
        )  # avoid real ffmpeg
        with SourceCache(stop=lambda: False) as cache:
            assert cache.get_or_fetch("ep1", "https://example.com/a.mp4") is None

    def test_no_stop_predicate_behaves_as_before(self, monkeypatch, tmp_path):
        monkeypatch.setattr("citypods.media._download_audio", lambda *a, **k: False)
        with SourceCache() as cache:
            assert cache.get_or_fetch("ep1", "https://example.com/a.mp4") is None


def _source(id_: str, ref: str, duration: float | None) -> SourceMedia:
    return SourceMedia(
        id=id_, provider="swagit", ref=ref, media_kind="direct", duration=duration, watch_url=None
    )


class TestSourceCacheGetOrFetchConcat:
    """Multi-source (``SwagitConcatPlanner``) episodes: download each segment individually
    (its own bounded timeout, releasing the rate-limit slot between segments) then concatenate
    once into a cached local file, instead of re-streaming every segment on every encode."""

    def test_downloads_each_segment_once_and_caches_the_combined_result(self, monkeypatch):
        fetch_calls: list[str] = []

        def _fake_download(url, dest, *_a, **_k):
            fetch_calls.append(url)
            dest.write_bytes(b"stub")
            return True

        concat_calls: list[tuple[list[Path], list[float]]] = []

        def _fake_concat(paths, durations, dest, *_a, **_k):
            concat_calls.append((list(paths), list(durations)))
            dest.write_bytes(b"combined")
            return True

        monkeypatch.setattr("citypods.media._download_audio", _fake_download)
        monkeypatch.setattr("citypods.media._concat_local_sources", _fake_concat)

        sources = [
            _source("s0", "https://example.com/seg0.mp4", 10.0),
            _source("s1", "https://example.com/seg1.mp4", 20.0),
        ]
        with SourceCache() as cache:
            combined = cache.get_or_fetch_concat("ep1", sources)
            assert combined is not None
            assert combined.read_bytes() == b"combined"
            assert fetch_calls == [s.ref for s in sources]
            assert concat_calls == [(concat_calls[0][0], [10.0, 20.0])]

            # A second call for the same uid reuses the cached combined file — no re-download,
            # no re-concat.
            again = cache.get_or_fetch_concat("ep1", sources)
            assert again is combined
            assert fetch_calls == [s.ref for s in sources]
            assert len(concat_calls) == 1

    def test_returns_none_when_a_segment_duration_is_unknown(self, monkeypatch):
        monkeypatch.setattr(
            "citypods.media._download_audio", lambda *a, **k: pytest.fail("should not fetch")
        )
        sources = [_source("s0", "https://example.com/seg0.mp4", None)]
        with SourceCache() as cache:
            assert cache.get_or_fetch_concat("ep1", sources) is None

    def test_returns_none_when_a_segment_fails_to_download(self, monkeypatch):
        calls: list[str] = []

        def _fake_download(url, dest, *_a, **_k):
            calls.append(url)
            return url.endswith("seg0.mp4")  # first segment "succeeds", second fails

        monkeypatch.setattr("citypods.media._download_audio", _fake_download)
        monkeypatch.setattr(
            "citypods.media._concat_local_sources",
            lambda *a, **k: pytest.fail("should not concat after a segment failure"),
        )
        sources = [
            _source("s0", "https://example.com/seg0.mp4", 10.0),
            _source("s1", "https://example.com/seg1.mp4", 20.0),
        ]
        with SourceCache() as cache:
            assert cache.get_or_fetch_concat("ep1", sources) is None
            # Both segments were attempted (no short-circuit before the failing one's fetch).
            assert calls == [s.ref for s in sources]

    def test_returns_none_when_concat_itself_fails(self, monkeypatch, tmp_path):
        def _fake_download(url, dest, *_a, **_k):
            dest.write_bytes(b"stub")
            return True

        monkeypatch.setattr("citypods.media._download_audio", _fake_download)
        monkeypatch.setattr("citypods.media._concat_local_sources", lambda *a, **k: False)
        sources = [_source("s0", "https://example.com/seg0.mp4", 10.0)]
        with SourceCache() as cache:
            assert cache.get_or_fetch_concat("ep1", sources) is None


class TestConcatRenderTimeline:
    def test_builds_one_monotonic_segment_spanning_the_combined_duration(self, tmp_path):
        sources = [
            _source("s0", "https://example.com/seg0.mp4", 10.0),
            _source("s1", "https://example.com/seg1.mp4", 20.5),
        ]
        combined = tmp_path / "combined.mka"

        timeline, by_id = _concat_render_timeline(sources, combined)

        assert by_id == {"combined": str(combined)}
        assert len(timeline.segments) == 1
        seg = timeline.segments[0]
        assert seg.kind == "source"
        assert seg.source_id == "combined"
        assert seg.served_start == 0.0
        assert seg.served_end == pytest.approx(30.5)
        assert seg.source_start == 0.0
        assert seg.source_end == pytest.approx(30.5)

    def test_synthesized_timeline_is_identity_shaped(self, tmp_path):
        """The synthesized segment is exactly full-span (served_* == source_*), so
        ``timeline_digest`` treats it as identity — correct here since there are no cuts left
        to apply once the segments are already concatenated; see the function's docstring for
        which encoder path that implies."""
        sources = [_source("s0", "https://example.com/seg0.mp4", 10.0)]
        timeline, _ = _concat_render_timeline(sources, tmp_path / "combined.mka")

        assert timeline_digest(timeline) == ""


class TestConcatLocalSourcesRealFfmpeg:
    def test_real_ffmpeg_concatenates_segments_to_the_summed_duration(self, tmp_path):
        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            pytest.skip("ffmpeg/ffprobe required for this integration check")

        durations = [1.5, 2.25]
        paths = []
        for i, dur in enumerate(durations):
            p = tmp_path / f"seg{i}.mka"
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    f"sine=frequency=440:sample_rate=48000:duration={dur}",
                    "-c:a",
                    "flac",
                    "-f",
                    "matroska",
                    str(p),
                ],
                check=True,
                timeout=30,
            )
            paths.append(p)

        dest = tmp_path / "combined.mka"
        assert _concat_local_sources(paths, durations, dest, "ffmpeg", timeout=30)
        assert dest.exists()

        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(dest),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        assert float(probe.stdout.strip()) == pytest.approx(sum(durations), abs=0.05)
