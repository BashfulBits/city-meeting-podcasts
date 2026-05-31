"""Tests for the enrichment-stage pipeline."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from citypods.media import GlobalBudget
from citypods.models import City, Episode
from citypods.stages import AudioStage, StageContext, default_stages, run_stages
from citypods.storage.local import LocalStorage


class FakeFfmpeg:
    def __init__(self):
        self.calls: list[str] = []

    def extract_audio(self, source_url: str, dest: Path) -> None:
        self.calls.append(source_url)
        dest.write_bytes(b"fake")


class FakeProvider:
    def resolve_media_url(self, episode, source):
        return episode.video_url


def _city():
    return City(
        slug="x-tx",
        provider="civicplus",
        source={"feed_url": "x"},
        podcast_title="X",
        podcast_author="City of X",
        podcast_email="",
        podcast_description="d",
    )


def _ep(guid):
    return Episode(
        guid=guid,
        uid=f"uid-{guid}",
        title=f"M {guid}",
        published=datetime(2026, 5, 20, tzinfo=UTC),
        video_url="https://src/x.m3u8",
        media_kind="hls",
        body="City Council",
    )


def _ctx(tmp_path, *, dry_run=False, audio_budget=10, per_source=10, storage=True):
    return StageContext(
        storage=LocalStorage(root=tmp_path / "a", url_prefix="https://cdn") if storage else None,
        ffmpeg=FakeFfmpeg(),
        max_kbps=96,
        per_source_budget=per_source,
        dry_run=dry_run,
        budgets={"audio": GlobalBudget(audio_budget)},
    )


def test_default_stages_starts_with_audio():
    stages = default_stages()
    assert [s.name for s in stages][0] == "audio"


def test_audio_stage_hosts_within_budget(tmp_path):
    eps = [_ep("g1"), _ep("g2"), _ep("g3")]
    ctx = _ctx(tmp_path, audio_budget=2)  # global cap of 2 this run
    stats = AudioStage().process(FakeProvider(), _city(), eps, ctx)
    assert stats.ran == 2 and stats.skipped == 1
    assert sum(e.hosted_audio_url is not None for e in eps) == 2


def test_audio_stage_noop_in_dry_run(tmp_path):
    eps = [_ep("g1")]
    stats = AudioStage().process(FakeProvider(), _city(), eps, _ctx(tmp_path, dry_run=True))
    assert stats.ran == 0 and eps[0].hosted_audio_url is None


def test_audio_stage_noop_without_storage(tmp_path):
    eps = [_ep("g1")]
    stats = AudioStage().process(FakeProvider(), _city(), eps, _ctx(tmp_path, storage=False))
    assert stats.ran == 0 and eps[0].hosted_audio_url is None


def test_run_stages_returns_stats_per_stage(tmp_path):
    eps = [_ep("g1")]
    stats = run_stages(FakeProvider(), _city(), eps, default_stages(), _ctx(tmp_path))
    assert [s.name for s in stats] == ["audio"]
    assert stats[0].ran == 1
    assert "audio" in stats[0].note()
