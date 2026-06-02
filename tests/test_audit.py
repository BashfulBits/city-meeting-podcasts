"""Tests for the pure feed-health checks and the audit_city orchestrator."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from citypods.audit import (
    ArchiveDiff,
    audit_city,
    check_dead_audio_aggregate,
    check_deferred_audio_aggregate,
    check_empty,
    check_enclosures,
    check_rehost_backlog,
    check_staleness,
    check_view_cap,
    compute_archive_diff,
    count_audio_failures,
)
from citypods.models import Episode

NOW = datetime(2026, 5, 30, tzinfo=UTC)


def _ep(days_ago, guid=None, kind="direct", url="https://x/a.mp4", hosted=None):
    d = NOW - timedelta(days=days_ago)
    e = Episode(
        guid=guid or f"g{days_ago}",
        title="City Council",
        published=d,
        video_url=url,
        media_kind=kind,
        body="City Council",
    )
    e.hosted_audio_url = hosted
    return e


def _diff(fetched=5, archived=5, materialized=5, dropped=0, backlog=0):
    return ArchiveDiff(
        fetched=fetched,
        archived=archived,
        materialized=materialized,
        dropped=dropped,
        backlog=backlog,
    )


# ---------------------------------------------------------------------------
# check_empty — three-way triage (issue #109)
# ---------------------------------------------------------------------------


def test_check_empty_no_diff_baseline():
    assert check_empty("s", [], 3).check == "drift"
    assert check_empty("s", [_ep(1), _ep(8)], 3).check == "empty"
    assert check_empty("s", [_ep(1), _ep(8), _ep(15)], 3) is None


def test_check_empty_suppresses_drift_when_archive_has_materialized_episodes():
    # (b) Provider window empty but archive has hosted audio → expected, suppress.
    big_diff = _diff(fetched=0, archived=10, materialized=10, dropped=10)
    assert check_empty("s", [], 3, diff=big_diff) is None


def test_check_empty_suppresses_sparse_window_when_archive_meets_bar():
    # (a/b) Only 1 in window but 5 in archive → transient, suppress.
    assert check_empty("s", [_ep(1)], 3, diff=_diff(fetched=1, archived=5, materialized=5)) is None


def test_check_empty_files_genuine_regression():
    # (c) Both provider and archive are empty → real problem, file.
    f = check_empty("s", [], 3, diff=_diff(fetched=0, archived=0, materialized=0))
    assert f is not None and f.check == "drift"
    assert "fetched=0" in f.message and "inferred:" in f.message


def test_check_empty_message_includes_counts_and_cause():
    f = check_empty("s", [_ep(1)], 3, diff=_diff(fetched=1, archived=2, materialized=0, backlog=1))
    assert f is not None  # archive < min_meetings, so still files
    assert "fetched=1" in f.message
    assert "inferred:" in f.message


# ---------------------------------------------------------------------------
# check_staleness — archive_newest correction (issue #109)
# ---------------------------------------------------------------------------


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


def test_check_staleness_suppressed_by_archive_newest():
    # Provider window shows old episodes (looks stale), but archive has a recent one.
    # archive_newest = 2 days ago → not actually stale.
    eps = [_ep(60), _ep(67), _ep(74), _ep(81), _ep(88)]
    recent = NOW - timedelta(days=2)
    assert check_staleness("s", eps, NOW, archive_newest=recent) is None


def test_check_staleness_still_fires_when_archive_newest_also_old():
    eps = [_ep(60), _ep(67), _ep(74), _ep(81), _ep(88)]
    still_old = NOW - timedelta(days=55)
    f = check_staleness("s", eps, NOW, archive_newest=still_old)
    assert f is not None and f.check == "stale"


# ---------------------------------------------------------------------------
# check_view_cap / check_rehost_backlog (unchanged behaviour)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# audio-failure tally + aggregate alert (issue #120)
# ---------------------------------------------------------------------------


def _rec(error=None):
    return {"audio": {"error": error}}


def test_count_audio_failures_splits_by_category():
    records = {
        "a": _rec("deferred"),
        "b": _rec("dead"),
        "c": _rec("dead"),
        "d": _rec(None),  # healthy
        "e": _rec("error"),  # transient, counts as neither
    }
    assert count_audio_failures(records) == (1, 2)


def test_dead_audio_aggregate_fires_above_threshold():
    f = check_dead_audio_aggregate(deferred_total=4, dead_total=12, threshold=10)
    assert f is not None
    assert f.slug == "(all)" and f.check == "dead-audio" and f.severity == "error"
    assert "12 episode(s)" in f.message and "4 await" in f.message


def test_dead_audio_aggregate_silent_below_threshold():
    assert check_dead_audio_aggregate(deferred_total=99, dead_total=9, threshold=10) is None


def test_deferred_audio_aggregate_tracks_prevalence():
    f = check_deferred_audio_aggregate(
        7, examples=[("dallas-tx-city-council", 5), ("denton-tx-council", 2)]
    )
    assert f is not None
    assert f.slug == "(all)" and f.check == "deferred-audio" and f.severity == "warn"
    assert "7 episode(s)" in f.message and "#122" in f.message
    # examples sorted by count desc, most-affected first
    assert "dallas-tx-city-council (5)" in f.message
    assert f.message.index("dallas-tx-city-council") < f.message.index("denton-tx-council")


def test_deferred_audio_aggregate_silent_when_none():
    assert check_deferred_audio_aggregate(0) is None


# ---------------------------------------------------------------------------
# check_enclosures — dead-enclosure re-resolve (issue #109, former #45)
# ---------------------------------------------------------------------------


def test_check_enclosures_uses_injected_head():
    statuses = {"https://x/a.mp4": 200, "https://x/b.mp4": 403}
    eps = [_ep(1, "g1", url="https://x/a.mp4"), _ep(8, "g2", url="https://x/b.mp4")]
    findings = check_enclosures("s", eps, lambda u: statuses[u])
    assert [f.message for f in findings] == ["g2: HTTP 403"]


def test_check_enclosures_suppresses_when_resolve_heals():
    # First URL is dead; resolve() returns a fresh working one → suppress the finding.
    def head(url):
        return 200 if "new" in url else 403

    def resolve(ep):
        return "https://x/new.mp4"

    eps = [_ep(1, "g1", url="https://x/old.mp4")]
    findings = check_enclosures("s", eps, head, resolve=resolve)
    assert findings == []  # suppressed — self-healed


def test_check_enclosures_files_when_resolve_also_dead():
    # Both the original and re-resolved URL are dead → still file, with re-resolve note.
    def head(url):
        return 403

    def resolve(ep):
        return "https://x/also-dead.mp4"

    eps = [_ep(1, "g1", url="https://x/dead.mp4")]
    findings = check_enclosures("s", eps, head, resolve=resolve)
    assert len(findings) == 1
    assert "re-resolved" in findings[0].message


def test_check_enclosures_files_when_resolve_raises():
    # resolve() itself throws (network error on re-resolve) → still file, with failure note.
    def head(url):
        return 403

    def resolve(ep):
        raise OSError("timeout")

    eps = [_ep(1, "g1", url="https://x/dead.mp4")]
    findings = check_enclosures("s", eps, head, resolve=resolve)
    assert len(findings) == 1
    assert "re-resolve failed" in findings[0].message


def test_check_enclosures_no_resolve_files_as_before():
    # Without resolve= the behaviour is identical to the pre-#109 code path.
    eps = [_ep(1, "g1", url="https://x/dead.mp4")]
    findings = check_enclosures("s", eps, lambda u: 404)
    assert len(findings) == 1
    assert "re-resolve" not in findings[0].message


# ---------------------------------------------------------------------------
# compute_archive_diff
# ---------------------------------------------------------------------------


def test_compute_archive_diff():
    e1 = _ep(1, "g1")
    e1.uid = "u1"
    e2 = _ep(8, "g2", kind="hls")
    e2.uid = "u2"
    records = {
        "u1": {"audio": {"url": "https://cdn/u1.m4a", "key": "k1", "spec_hash": "s"}},
        "u3": {"audio": {"url": "https://cdn/u3.m4a", "key": "k3", "spec_hash": "s"}},
    }
    diff = compute_archive_diff([e1, e2], records)
    assert diff.fetched == 2  # e1 and e2
    assert diff.archived == 2  # u1 and u3
    assert diff.materialized == 2  # both have hosted audio
    assert diff.dropped == 1  # u3 not in fetched
    assert diff.backlog == 1  # e2 is HLS with no hosted_audio_url


# ---------------------------------------------------------------------------
# audit_city integration — all three triage scenarios
# ---------------------------------------------------------------------------


class _FakeProvider:
    def __init__(self, episodes):
        self._eps = episodes

    def fetch_episodes(self, source):
        return list(self._eps)

    def resolve_media_url(self, episode, source):
        return episode.video_url + "?refreshed=1"


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


def test_audit_city_triage_a_pending_backlog_suppresses_empty():
    # (a) Provider returns 0 but archive shows materialized episodes → suppress drift.
    records = {"u1": {"audio": {"url": "https://cdn/u1.m4a", "key": "k", "spec_hash": "s"}}}
    findings = audit_city(_city(), provider=_FakeProvider([]), now=NOW, records=records)
    assert not any(f.check == "drift" for f in findings)


def test_audit_city_triage_b_dropped_but_archived_suppresses_sparse_empty():
    # (b) Only 1 episode in the provider window but 5 in the archive → suppress empty.
    records = {
        f"u{i}": {"audio": {"url": f"https://cdn/u{i}.m4a", "key": f"k{i}", "spec_hash": "s"}}
        for i in range(5)
    }
    city = _city()
    city = city.__class__(
        **{**city.__dict__, "max_episodes": 50},
    )
    findings = audit_city(
        city, provider=_FakeProvider([_ep(1)]), now=NOW, records=records, min_meetings=3
    )
    assert not any(f.check in ("drift", "empty") for f in findings)


def test_audit_city_triage_c_genuine_regression_files_ticket():
    # (c) No episodes in provider AND no archive → genuine regression.
    findings = audit_city(_city(), provider=_FakeProvider([]), now=NOW, records={})
    drift = [f for f in findings if f.check == "drift"]
    assert len(drift) == 1
    assert "inferred:" in drift[0].message


def test_compute_archive_diff_scoped_to_body():
    # A shared-view source: records hold two bodies, but the diff scopes to "Planning".
    e1 = _ep(1, "g1")
    e1.uid = "u1"
    e1.body = "Planning and Zoning Commission"
    records = {
        "u1": {"body": "Planning and Zoning Commission", "audio": {"url": "https://cdn/u1.m4a"}},
        "u2": {"body": "City Council", "audio": {"url": "https://cdn/u2.m4a"}},
    }
    diff = compute_archive_diff([e1], records, body="Planning")
    assert diff.archived == 1  # only the Planning record
    assert diff.materialized == 1  # the City Council record is excluded


def test_audit_city_shared_view_broken_body_not_suppressed():
    # Shared Swagit view: City Council is materialized, but the Planning feed's body filter
    # stopped matching so it has 0 episodes. The materialized council episodes live in the same
    # shared store — but must NOT suppress the Planning regression (per-body scoping).
    council = _ep(1, "g1", hosted="https://cdn/u1.m4a")
    council.uid = "u1"
    council.body = "City Council"
    records = {
        "u1": {"body": "City Council", "audio": {"url": "https://cdn/u1.m4a"}},
        "u2": {"body": "City Council", "audio": {"url": "https://cdn/u2.m4a"}},
    }
    city = _city()
    city = city.__class__(**{**city.__dict__, "source": {"feed_url": "u", "body": "Planning"}})
    findings = audit_city(city, provider=_FakeProvider([council]), now=NOW, records=records)
    # Planning resolves to 0 episodes; the diff is scoped to Planning (empty), so don't suppress.
    drift = [f for f in findings if f.check == "drift"]
    assert len(drift) == 1
    assert "inferred:" in drift[0].message


def test_audit_city_staleness_suppressed_by_archive_newest():
    # Provider window looks stale, but archive has a recent episode → suppress.
    eps = [_ep(60), _ep(67), _ep(74), _ep(81), _ep(88)]
    recent = (NOW - timedelta(days=2)).isoformat()
    records = {"recent": {"published": recent, "audio": {}}}
    findings = audit_city(_city(), provider=_FakeProvider(eps), now=NOW, records=records)
    assert not any(f.check == "stale" for f in findings)
