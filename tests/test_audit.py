"""Tests for the pure feed-health checks and the audit_city orchestrator."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from citypods.audit import (
    audit_city,
    check_empty,
    check_enclosures,
    check_rehost_backlog,
    check_staleness,
    check_view_cap,
)
from citypods.models import Episode

NOW = datetime(2026, 5, 30, tzinfo=UTC)


def _ep(days_ago, guid=None, kind="direct", url="https://x/a.mp4"):
    d = NOW - timedelta(days=days_ago)
    return Episode(
        guid=guid or f"g{days_ago}",
        title="City Council",
        published=d,
        video_url=url,
        media_kind=kind,
        body="City Council",
    )


def test_check_empty():
    assert check_empty("s", [], 3).check == "drift"
    assert check_empty("s", [_ep(1), _ep(8)], 3).check == "empty"
    assert check_empty("s", [_ep(1), _ep(8), _ep(15)], 3) is None


def test_check_staleness_flags_overdue_feed_against_own_cadence():
    # Weekly cadence but newest is 60 days old -> stale.
    eps = [_ep(60), _ep(67), _ep(74), _ep(81), _ep(88)]
    f = check_staleness("s", eps, NOW)
    assert f is not None and f.check == "stale"


def test_check_staleness_ignores_healthy_and_low_sample_feeds():
    weekly = [_ep(2), _ep(9), _ep(16), _ep(23), _ep(30)]
    assert check_staleness("s", weekly, NOW) is None
    assert check_staleness("s", [_ep(400), _ep(800)], NOW) is None  # too few samples


def test_check_staleness_floor_suppresses_bursty_false_positive():
    # Several same-day meetings -> near-zero median cadence; a normal 10-day gap since the
    # last one must NOT flag (the absolute floor dominates the tiny median).
    bursty = [_ep(10, "a"), _ep(10, "b"), _ep(11, "c"), _ep(11, "d"), _ep(12, "e")]
    assert check_staleness("s", bursty, NOW) is None


def test_check_view_cap():
    assert check_view_cap("s", [100]).check == "view-cap"
    assert check_view_cap("s", [42, 100, 7]).check == "view-cap"
    assert check_view_cap("s", [42, 7]) is None


def test_check_rehost_backlog_only_when_fully_stalled():
    hls = [_ep(1, "g1", kind="hls"), _ep(8, "g2", kind="hls")]
    assert check_rehost_backlog("s", hls).check == "rehost-backlog"
    # Some progress (one hosted) -> not flagged.
    hls[0].hosted_audio_url = "https://cdn/a.m4a"
    assert check_rehost_backlog("s", hls) is None
    # No HLS episodes -> never flagged.
    assert check_rehost_backlog("s", [_ep(1)]) is None


def test_check_enclosures_uses_injected_head():
    statuses = {"https://x/a.mp4": 200, "https://x/b.mp4": 403}
    eps = [_ep(1, "g1", url="https://x/a.mp4"), _ep(8, "g2", url="https://x/b.mp4")]
    findings = check_enclosures("s", eps, lambda u: statuses[u])
    assert [f.message for f in findings] == ["g2: HTTP 403"]


class _FakeProvider:
    def __init__(self, episodes):
        self._eps = episodes

    def fetch_episodes(self, source):
        return list(self._eps)


def _city():
    from citypods.models import City

    return City(
        slug="x-tx",
        provider="granicus",
        source={"feed_url": "u"},
        podcast_title="X",
        podcast_author="City of X",
        podcast_email="",
        podcast_description="d",
    )


def test_audit_city_skips_other_checks_when_feed_is_empty():
    findings = audit_city(_city(), provider=_FakeProvider([]), now=NOW, view_counts=[100])
    assert [f.check for f in findings] == ["drift"]  # view-cap not added on empty feed


def test_audit_city_aggregates_multiple_findings():
    eps = [_ep(60), _ep(67), _ep(74), _ep(81), _ep(88)]  # healthy count but stale
    findings = audit_city(_city(), provider=_FakeProvider(eps), now=NOW, view_counts=[100])
    checks = {f.check for f in findings}
    assert "stale" in checks and "view-cap" in checks
