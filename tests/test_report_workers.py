"""Tests for the ASR-timeout-backoff watchdog in scripts/compute/report_workers.py (H19 hardening
follow-up): flags a recording stuck in the local ASR timeout backoff loop for `asr-worker-report`
to open/close a tracking issue against."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from citypods.models import City, Episode
from citypods.records import episode_to_record, save_records
from scripts.compute import report_workers as rw


def _city(slug: str = "example-city") -> City:
    return City(
        slug=slug,
        provider="granicus",
        source={"feed_url": "https://example.invalid/feed"},
        podcast_title="t",
        podcast_author="Example City",
        podcast_email="",
        podcast_description="d",
    )


def _episode(uid: str, *, attempts: int, last_attempt: str | None) -> Episode:
    ep = Episode(
        guid=uid,
        title=f"Episode {uid}",
        published=datetime(2026, 6, 1, tzinfo=UTC),
        video_url=f"https://example.invalid/{uid}.mp4",
    )
    ep.uid = uid
    ep.transcript_timeout_attempts = attempts
    ep.transcript_timeout_last_attempt = last_attempt
    return ep


def test_stuck_in_asr_backoff_flags_only_items_at_or_above_threshold(tmp_path):
    now = datetime(2026, 7, 10, tzinfo=UTC)
    city = _city()
    src = "example-city-src"
    records = {
        "below": episode_to_record(_episode("below", attempts=2, last_attempt=now.isoformat())),
        "at": episode_to_record(_episode("at", attempts=3, last_attempt=now.isoformat())),
        "clean": episode_to_record(_episode("clean", attempts=0, last_attempt=None)),
    }
    save_records(tmp_path, src, records)

    hits = rw.stuck_in_asr_backoff(tmp_path, {src: city}, min_attempts=3, now=now)

    assert [h["episode_uid"] for h in hits] == ["at"]
    assert hits[0]["attempts"] == 3
    assert hits[0]["still_backing_off"] is True
    assert hits[0]["backoff_until"] is not None


def test_stuck_in_asr_backoff_sorts_worst_first(tmp_path):
    now = datetime(2026, 7, 10, tzinfo=UTC)
    city = _city()
    src = "example-city-src"
    records = {
        "three": episode_to_record(_episode("three", attempts=3, last_attempt=now.isoformat())),
        "six": episode_to_record(_episode("six", attempts=6, last_attempt=now.isoformat())),
        "four": episode_to_record(_episode("four", attempts=4, last_attempt=now.isoformat())),
    }
    save_records(tmp_path, src, records)

    hits = rw.stuck_in_asr_backoff(tmp_path, {src: city}, min_attempts=3, now=now)

    assert [h["episode_uid"] for h in hits] == ["six", "four", "three"]


def test_stuck_in_asr_backoff_marks_lapsed_window_as_not_still_backing_off(tmp_path):
    now = datetime(2026, 7, 10, tzinfo=UTC)
    city = _city()
    src = "example-city-src"
    # attempts=1 -> 1 day backoff; long past that from `now`.
    long_ago = (now - timedelta(days=30)).isoformat()
    records = {"a": episode_to_record(_episode("a", attempts=1, last_attempt=long_ago))}
    save_records(tmp_path, src, records)

    hits = rw.stuck_in_asr_backoff(tmp_path, {src: city}, min_attempts=1, now=now)

    assert len(hits) == 1
    assert hits[0]["still_backing_off"] is False


def test_render_asr_backoff_issue_body_lists_every_hit():
    hits = [
        {
            "source_key": "src",
            "episode_uid": "a",
            "attempts": 5,
            "last_attempt": "2026-07-01T00:00:00+00:00",
            "backoff_until": "2026-07-17T00:00:00+00:00",
        }
    ]

    body = rw.render_asr_backoff_issue_body(hits, threshold=3)

    assert "1 recording(s) stuck" in body
    assert ">= 3 attempts" in body
    assert "`src`" in body
    assert "`a`" in body
    assert "5" in body


def test_write_asr_backoff_report_writes_flag_and_issue_body_only_when_stuck(tmp_path):
    out_dir = tmp_path / "watchdog"
    hits = [
        {
            "source_key": "src",
            "episode_uid": "a",
            "attempts": 4,
            "last_attempt": None,
            "backoff_until": None,
        }
    ]

    rw.write_asr_backoff_report(out_dir, hits, threshold=3)

    assert (out_dir / "has_asr_timeout_backoff").read_text() == "true"
    payload = json.loads((out_dir / "asr-timeout-backoff.json").read_text())
    assert payload == {"threshold": 3, "count": 1, "items": hits}
    assert (out_dir / "issue-body.md").exists()


def test_write_asr_backoff_report_clean_sweep_has_no_issue_body(tmp_path):
    out_dir = tmp_path / "watchdog"

    rw.write_asr_backoff_report(out_dir, [], threshold=3)

    assert (out_dir / "has_asr_timeout_backoff").read_text() == "false"
    assert not (out_dir / "issue-body.md").exists()
