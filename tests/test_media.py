"""Tests for the audio materialization pipeline."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

from citypods.media import _audio_key, encode_args, materialize_audio
from citypods.models import City, Episode
from citypods.storage.local import LocalStorage


class FakeFfmpeg:
    def __init__(self, fail: bool = False):
        self.calls: list[str] = []
        self.fail = fail

    def extract_audio(self, source_url: str, dest: Path) -> None:
        self.calls.append(source_url)
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
        title=f"Meeting {guid}",
        published=datetime(2026, 5, 20, tzinfo=UTC),
        video_url=url,
        media_kind=kind,
    )


def _store(tmp_path):
    return LocalStorage(root=tmp_path / "audio", url_prefix="https://cdn/audio")


def test_hls_episode_is_hosted(tmp_path):
    eps = [_ep("g1")]
    manifest = {}
    ff = FakeFfmpeg()
    stats = materialize_audio(
        _city(),
        eps,
        storage=_store(tmp_path),
        manifest=manifest,
        ffmpeg=ff,
        budget=5,
        resolve_media_url=lambda e: e.video_url,
    )
    city = _city()
    assert stats.hosted == 1
    assert eps[0].hosted_audio_url == f"https://cdn/audio/{_audio_key(city, 'g1')}"
    assert manifest["g1"]["url"] == eps[0].hosted_audio_url
    assert ff.calls == ["https://src/manifest.m3u8"]
    assert (tmp_path / "audio" / _audio_key(city, "g1")).exists()


def test_encode_args_copies_or_reencodes_by_cap():
    # At/under cap -> lossless copy.
    assert encode_args(64_000, 96) == ["-c:a", "copy"]
    assert encode_args(96_000, 96) == ["-c:a", "copy"]
    # Over cap (e.g. 128k stereo) -> re-encode to mono at the cap.
    assert encode_args(128_000, 96) == ["-c:a", "aac", "-b:a", "96k", "-ac", "1"]
    # Unknown source bitrate -> re-encode (safe).
    assert encode_args(None, 96) == ["-c:a", "aac", "-b:a", "96k", "-ac", "1"]


def test_audio_key_dedups_across_slug_and_body():
    # Same provider + source (ignoring body) + guid -> same key, even with different slugs,
    # so a combined feed and a per-board feed share one hosted file.
    combined = City(
        slug="denton-tx",
        provider="granicus",
        source={"feed_url": "F"},
        podcast_title="t",
        podcast_author="a",
        podcast_email="",
        podcast_description="d",
    )
    per_board = City(
        slug="denton-tx-city-council",
        provider="granicus",
        source={"feed_url": "F", "body": "City Council"},
        podcast_title="t",
        podcast_author="a",
        podcast_email="",
        podcast_description="d",
    )
    assert _audio_key(combined, "clip-1") == _audio_key(per_board, "clip-1")
    # Different source -> different key.
    other = City(
        slug="x",
        provider="granicus",
        source={"feed_url": "OTHER"},
        podcast_title="t",
        podcast_author="a",
        podcast_email="",
        podcast_description="d",
    )
    assert _audio_key(other, "clip-1") != _audio_key(combined, "clip-1")


def test_direct_not_hosted_unless_extract_audio(tmp_path):
    eps = [_ep("g1", kind="direct", url="https://src/v.mp4")]
    ff = FakeFfmpeg()
    stats = materialize_audio(
        _city(extract_audio=False),
        eps,
        storage=_store(tmp_path),
        manifest={},
        ffmpeg=ff,
        budget=5,
        resolve_media_url=lambda e: e.video_url,
    )
    assert stats.hosted == 0 and ff.calls == []
    assert eps[0].hosted_audio_url is None


def test_direct_hosted_when_extract_audio(tmp_path):
    eps = [_ep("g1", kind="direct", url="https://src/v.mp4")]
    ff = FakeFfmpeg()
    stats = materialize_audio(
        _city(extract_audio=True),
        eps,
        storage=_store(tmp_path),
        manifest={},
        ffmpeg=ff,
        budget=5,
        resolve_media_url=lambda e: e.video_url,
    )
    assert stats.hosted == 1 and eps[0].hosted_audio_url


def test_budget_defers_extra_episodes(tmp_path):
    eps = [_ep("g1"), _ep("g2"), _ep("g3")]
    ff = FakeFfmpeg()
    stats = materialize_audio(
        _city(),
        eps,
        storage=_store(tmp_path),
        manifest={},
        ffmpeg=ff,
        budget=1,
        resolve_media_url=lambda e: e.video_url,
    )
    assert stats.hosted == 1 and stats.skipped_budget == 2
    assert sum(e.hosted_audio_url is not None for e in eps) == 1


def test_manifest_reuse_skips_ffmpeg(tmp_path):
    eps = [_ep("g1")]
    manifest = {"g1": {"key": "k", "url": "https://cdn/audio/existing.m4a"}}
    ff = FakeFfmpeg()
    stats = materialize_audio(
        _city(),
        eps,
        storage=_store(tmp_path),
        manifest=manifest,
        ffmpeg=ff,
        budget=5,
        resolve_media_url=lambda e: e.video_url,
    )
    assert stats.reused == 1 and ff.calls == []
    assert eps[0].hosted_audio_url == "https://cdn/audio/existing.m4a"


def test_ffmpeg_error_recorded(tmp_path):
    eps = [_ep("g1")]
    stats = materialize_audio(
        _city(),
        eps,
        storage=_store(tmp_path),
        manifest={},
        ffmpeg=FakeFfmpeg(fail=True),
        budget=5,
        resolve_media_url=lambda e: e.video_url,
    )
    assert stats.errors and eps[0].hosted_audio_url is None
