"""Tests for the audio materialization pipeline (content-addressed, spec-aware)."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
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


def _materialize(city, eps, store, ff, budget=5):
    return materialize_audio(
        city,
        eps,
        storage=store,
        ffmpeg=ff,
        budget=budget,
        max_kbps=MAX_KBPS,
        resolve_media_url=lambda e: e.video_url,
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


def test_budget_defers_extra_episodes(tmp_path):
    eps = [_ep("g1"), _ep("g2"), _ep("g3")]
    ff = FakeFfmpeg()
    stats = _materialize(_city(), eps, _store(tmp_path), ff, budget=1)
    assert stats.hosted == 1 and stats.skipped_budget == 2
    assert sum(e.hosted_audio_url is not None for e in eps) == 1


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


def test_stale_record_cleared_when_budget_exhausted(tmp_path):
    """A dead record under no budget must drop its pointer rather than keep advertising audio
    that isn't in storage."""
    ep = _ep("g1")
    city = _city()
    store = _store(tmp_path)
    spec = audio_spec_hash(ep, max_kbps=MAX_KBPS)
    key = audio_object_key(city, ep, spec)
    ep.audio_key = key
    ep.hosted_audio_url = store.public_url(key)
    ep.audio_spec_hash = spec
    stats = _materialize(city, [ep], store, FakeFfmpeg(), budget=0)
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
        budget=5,
        max_kbps=96,
        resolve_media_url=lambda e: e.video_url,
    )
    assert ffmpeg.chapters == [[{"start": 0, "end": 10, "title": "Intro"}]]
