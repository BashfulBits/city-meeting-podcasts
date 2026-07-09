from __future__ import annotations

from datetime import UTC, datetime

from citypods.durations import (
    episode_duration_hours,
    episode_served_duration_seconds,
    episode_source_duration_seconds,
    record_duration_hours,
    record_served_duration_seconds,
    record_source_duration_seconds,
    set_served_duration_seconds,
    set_source_duration_seconds,
)
from citypods.models import Episode


def _episode() -> Episode:
    return Episode(
        guid="g1",
        uid="u1",
        title="Meeting",
        published=datetime(2026, 7, 9, tzinfo=UTC),
        video_url="https://example.com/video.mp4",
    )


def test_episode_helpers_prefer_canonical_fields() -> None:
    ep = _episode()
    ep.duration = 3600
    ep.source_duration_seconds = 3900.0
    ep.audio_duration_served = 1800.0
    ep.served_duration_seconds = 2100.0

    assert episode_source_duration_seconds(ep) == 3900.0
    assert episode_served_duration_seconds(ep) == 2100.0
    assert episode_duration_hours(ep) == (2100.0 / 3600.0, "served")


def test_episode_helpers_fall_back_to_legacy_fields() -> None:
    ep = _episode()
    ep.duration = 5400
    ep.audio_duration_served = 2700.0

    assert episode_source_duration_seconds(ep) == 5400.0
    assert episode_served_duration_seconds(ep) == 2700.0
    assert episode_duration_hours(ep) == (0.75, "served")


def test_record_helpers_prefer_canonical_fields() -> None:
    rec = {
        "source_duration_seconds": 3900.0,
        "served_duration_seconds": 2100.0,
        "duration": 3600,
        "audio": {"duration_served": 1800.0},
    }

    assert record_source_duration_seconds(rec) == 3900.0
    assert record_served_duration_seconds(rec) == 2100.0
    assert record_duration_hours(rec) == (2100.0 / 3600.0, "served")


def test_helpers_ignore_zero_negative_and_bad_values() -> None:
    rec = {
        "source_duration_seconds": 0,
        "served_duration_seconds": -1,
        "duration": "bad",
        "audio": {"duration_served": 0},
    }
    ep = _episode()
    ep.source_duration_seconds = 0
    ep.served_duration_seconds = -1
    ep.duration = None
    ep.audio_duration_served = 0

    assert record_source_duration_seconds(rec) is None
    assert record_served_duration_seconds(rec) is None
    assert record_duration_hours(rec) == (0.0, "unknown")
    assert episode_source_duration_seconds(ep) is None
    assert episode_served_duration_seconds(ep) is None
    assert episode_duration_hours(ep) == (0.0, "unknown")


def test_setters_update_episode_legacy_fields_but_record_canonical_only() -> None:
    ep = _episode()
    rec = {}

    set_source_duration_seconds(ep, 3600.4)
    set_served_duration_seconds(ep, 1800.5)
    set_source_duration_seconds(rec, 3600.4)
    set_served_duration_seconds(rec, 1800.5)

    assert ep.source_duration_seconds == 3600.4
    assert ep.duration == 3600
    assert ep.served_duration_seconds == 1800.5
    assert ep.audio_duration_served == 1800.5
    assert rec["source_duration_seconds"] == 3600.4
    assert rec["served_duration_seconds"] == 1800.5
    assert "duration" not in rec
    assert "audio" not in rec


def test_record_reads_do_not_mutate_input() -> None:
    rec = {"duration": 3600, "audio": {"duration_served": 1800.0}}
    audio_before = rec["audio"]

    assert record_source_duration_seconds(rec) == 3600.0
    assert record_served_duration_seconds(rec) == 1800.0
    assert rec == {"duration": 3600, "audio": {"duration_served": 1800.0}}
    assert rec["audio"] is audio_before
