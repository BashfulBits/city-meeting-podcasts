"""Tests for the audio materialization pipeline (content-addressed, spec-aware)."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from citypods.media import encode_args, materialize_audio
from citypods.models import City, Episode
from citypods.records import audio_object_key, audio_spec_hash, source_key
from citypods.storage.local import LocalStorage

MAX_KBPS = 96


class FakeFfmpeg:
    def __init__(self, fail: bool = False):
        self.calls: list[str] = []
        self.chapters: list[list[dict] | None] = []
        self.fail = fail

    def extract_audio(self, source_url: str, dest: Path, chapters=None) -> None:
        self.calls.append(source_url)
        self.chapters.append(chapters)
        if self.fail:
            raise subprocess.CalledProcessError(1, "ffmpeg")
        dest.write_bytes(b"fake-m4a")


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

    def extract_audio(self, source_url, dest, chapters=None):
        self.calls.append(source_url)
        if self.marker in source_url:
            raise subprocess.CalledProcessError(1, "ffmpeg")
        dest.write_bytes(b"fake-m4a")


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
        def extract_audio(self, source_url, dest, chapters=None):
            raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=2700)

    ep = _ep("slow")
    stats = _materialize(_city(), [ep], _store(tmp_path), _TimeoutFfmpeg())
    assert stats.hosted == 0 and len(stats.errors) == 1
    assert ep.materialize_error == "timeout" and ep.materialize_attempts == 1
    assert ep.materialize_last_attempt is not None


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
        "https://src/manifest.m3u8", tmp_path / "out.m4a"
    )
    (_probe_cmd, probe_kw), (enc_cmd, enc_kw) = calls[0], calls[1]
    assert probe_kw["timeout"] == 120.0  # capped to _PROBE_TIMEOUT_S
    assert "-rw_timeout" in enc_cmd and enc_kw["timeout"] == 2700
