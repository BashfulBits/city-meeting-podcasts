"""Tests for the pure feed-health checks and the audit_city orchestrator."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from citypods.audit import (
    WARN,
    ArchiveDiff,
    Finding,
    aggregate_view_cap_findings,
    audit_all,
    audit_city,
    check_dead_audio_aggregate,
    check_deferred_audio_aggregate,
    check_dormant_resumed,
    check_empty,
    check_enclosures,
    check_meetings_url,
    check_provider_error_rates,
    check_rehost_backlog,
    check_staleness,
    check_view_cap,
    compute_archive_diff,
    count_audio_failures,
)
from citypods.models import Episode, FeedLifecycle

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


def test_aggregate_view_cap_findings_groups_by_provider():
    findings = [
        Finding("fort-worth-tx-city-council", "view-cap", WARN, "5 view(s) at the 100-item cap"),
        Finding("arlington-tx-council", "view-cap", WARN, "1 view(s) at the 100-item cap"),
        Finding("gainesville-tx", "empty", WARN, "only 1 episode"),
    ]

    aggregated = aggregate_view_cap_findings(
        findings,
        {
            "fort-worth-tx-city-council": ("granicus", "fort-worth-tx"),
            "arlington-tx-council": ("granicus", "arlington-tx"),
        },
    )

    assert [f.check for f in aggregated] == ["empty", "view-cap"]
    view_cap = aggregated[1]
    assert view_cap.slug == "granicus"
    assert view_cap.severity == WARN
    assert "`fort-worth-tx` / `fort-worth-tx-city-council`" in view_cap.message
    assert "`arlington-tx` / `arlington-tx-council`" in view_cap.message


def _run_history(n_active: int, n_idle: int = 0) -> list[dict]:
    """Build a synthetic run_history with n_active runs that encoded audio + n_idle that didn't."""
    active = [{"materialize_encoded": 2, "materialize_seconds": 60.0}] * n_active
    idle = [{"materialize_encoded": 0, "materialize_seconds": 0.0}] * n_idle
    return idle + active  # newest-last


def test_check_rehost_backlog_no_hls_never_flags():
    assert check_rehost_backlog("s", [_ep(1)]) is None


def test_check_rehost_backlog_some_hosted_suppresses():
    """Catching-up: at least one episode hosted → suppress regardless of history."""
    hls = [_ep(1, "g1", kind="hls"), _ep(8, "g2", kind="hls")]
    hls[0].hosted_audio_url = "https://cdn/a.m4a"
    assert check_rehost_backlog("s", hls, run_history=_run_history(5)) is None


def test_check_rehost_backlog_no_history_suppresses():
    """No run history → can't distinguish new city from stalled → suppress."""
    hls = [_ep(1, "g1", kind="hls")]
    assert check_rehost_backlog("s", hls) is None
    assert check_rehost_backlog("s", hls, run_history=[]) is None


def test_check_rehost_backlog_insufficient_active_runs_suppresses():
    """Fewer than STALL_THRESHOLD active runs → too early to call stalled → suppress."""
    hls = [_ep(1, "g1", kind="hls")]
    assert check_rehost_backlog("s", hls, run_history=_run_history(n_active=2)) is None


def test_check_rehost_backlog_stalled_warns():
    """Zero hosted AND pipeline is actively encoding (other feeds) → warn."""
    from citypods.audit import WARN

    hls = [_ep(1, "g1", kind="hls"), _ep(8, "g2", kind="hls")]
    f = check_rehost_backlog("s", hls, run_history=_run_history(n_active=3))
    assert f is not None
    assert f.check == "rehost-backlog"
    assert f.severity == WARN
    assert "stalled" in f.message.lower() or "stall" in f.message.lower()


def test_check_rehost_backlog_provider_errors_stay_error():
    """dead-enclosure and unreachable checks remain ERROR — rehost_backlog never touches them."""
    from citypods.audit import ERROR, WARN, check_enclosures

    # Confirm dead-enclosure is still ERROR (separate check, unchanged severity).
    ep = _ep(1, "g1", kind="hls")
    ep.hosted_audio_url = "https://cdn/a.m4a"
    findings = check_enclosures("s", [ep], lambda url: 404)
    assert findings and findings[0].severity == ERROR

    # CR2-TS-02: actually exercise check_rehost_backlog over the same stalled-backlog shape and
    # confirm it produces its own independent WARN finding rather than touching/overriding the
    # ERROR above — the claim this test's name/docstring makes but previously never checked.
    hls = [_ep(1, "g1", kind="hls"), _ep(8, "g2", kind="hls")]
    backlog_finding = check_rehost_backlog("s", hls, run_history=_run_history(n_active=3))
    assert backlog_finding is not None
    assert backlog_finding.check == "rehost-backlog"
    assert backlog_finding.severity == WARN
    assert findings[0].severity == ERROR  # unaffected by the separate backlog check


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
    assert "12 episode(s)" in f.message
    assert "4 episode(s) are in materialization backoff" in f.message


def test_dead_audio_aggregate_silent_below_threshold():
    assert check_dead_audio_aggregate(deferred_total=99, dead_total=9, threshold=10) is None


def test_deferred_audio_aggregate_tracks_prevalence():
    f = check_deferred_audio_aggregate(
        7, examples=[("dallas-tx-city-council", 5), ("denton-tx-council", 2)]
    )
    assert f is not None
    assert f.slug == "(all)" and f.check == "deferred-audio" and f.severity == "warn"
    assert "7 episode(s)" in f.message and "MEDIA_DEFERRED" in f.message
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


def test_check_enclosures_redacts_presigned_query_in_re_resolved_url():
    # MR-CP-03: a presigned re-resolved URL must not reach the Finding.message verbatim — this
    # gets posted into a public GitHub issue by scripts/audit_feeds.py.
    def head(url):
        return 403

    def resolve(ep):
        return "https://x/also-dead.mp4?AWSAccessKeyId=AKIA&Signature=topsecret&Expires=1"

    eps = [_ep(1, "g1", url="https://x/dead.mp4")]
    findings = check_enclosures("s", eps, head, resolve=resolve)
    assert len(findings) == 1
    assert "topsecret" not in findings[0].message
    assert "re-resolved to 'https://x/also-dead.mp4': HTTP 403" in findings[0].message


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


def test_check_enclosures_skips_withheld_media():
    # A withheld episode is excluded from the feed, so a stale hosted URL must not file a
    # dead-enclosure finding even when the HEAD comes back 4xx (GH#795).
    from citypods.availability import CONFIRMED_EMPTY, MediaAvailability

    # Identical to test_check_enclosures_no_resolve_files_as_before (which files 1) except for the
    # withheld verdict, so an empty result proves the skip rather than a missing enclosure URL.
    ep = _ep(1, "g1", url="https://x/dead.mp4")
    ep.media_availability = MediaAvailability(state=CONFIRMED_EMPTY)
    findings = check_enclosures("s", [ep], lambda u: 404)
    assert findings == []


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


def test_audit_city_counts_seed_episodes_against_empty_threshold():
    city = _city()
    city.extra = {
        "seed_episodes": [
            {
                "title": "Seed 1",
                "published": (NOW - timedelta(days=8)).isoformat(),
                "video_url": "https://x/seed1",
                "body": "City Council",
            },
            {
                "title": "Seed 2",
                "published": (NOW - timedelta(days=15)).isoformat(),
                "video_url": "https://x/seed2",
                "body": "City Council",
            },
        ]
    }

    findings = audit_city(city, provider=_FakeProvider([_ep(1)]), now=NOW, min_meetings=3)

    assert not any(f.check == "empty" for f in findings)


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


def test_audit_city_inactive_lifecycle_suppresses_empty_and_stale():
    city = _city()
    city.extra = {"audit": {"lifecycle": {"status": "inactive"}}}
    eps = [_ep(60), _ep(67), _ep(74), _ep(81), _ep(88)]

    findings = audit_city(city, provider=_FakeProvider(eps), now=NOW, min_meetings=10)

    checks = {f.check for f in findings}
    assert "empty" not in checks
    assert "stale" not in checks


def test_audit_city_superseded_lifecycle_still_allows_other_checks():
    city = _city()
    city.extra = {"audit": {"lifecycle": {"status": "superseded"}}}
    eps = [_ep(60), _ep(67), _ep(74), _ep(81), _ep(88)]

    findings = audit_city(city, provider=_FakeProvider(eps), now=NOW, view_counts=[100])

    checks = {f.check for f in findings}
    assert "stale" not in checks
    assert "view-cap" in checks


def test_audit_city_review37_lifecycle_suppresses_stale():
    eps = [_ep(60), _ep(67), _ep(74), _ep(81), _ep(88)]
    cases = (
        FeedLifecycle(status="paused", recheck_after=date(2026, 8, 1), reason="recess"),
        FeedLifecycle(status="dormant", reason="irregular"),
        FeedLifecycle(status="retired", reason="dissolved"),
    )
    for lifecycle in cases:
        city = _city()
        city.lifecycle = lifecycle
        findings = audit_city(city, provider=_FakeProvider(eps), now=NOW)
        assert not any(f.check == "stale" for f in findings)


def test_audit_all_retired_feed_never_resolves_provider(monkeypatch, tmp_path):
    city = _city()
    city.lifecycle = FeedLifecycle(status="retired", reason="body dissolved")

    def fail_provider_lookup(_name):
        raise AssertionError("retired feed must not resolve or fetch its provider")

    monkeypatch.setattr("citypods.providers.get_provider", fail_provider_lookup)

    assert audit_all([city], site_config={}, output_dir=tmp_path, now=NOW) == []


def test_audit_city_expired_pause_resumes_stale_check():
    city = _city()
    city.lifecycle = FeedLifecycle(
        status="paused", recheck_after=NOW.date() - timedelta(days=1), reason="recheck"
    )
    eps = [_ep(60), _ep(67), _ep(74), _ep(81), _ep(88)]

    findings = audit_city(city, provider=_FakeProvider(eps), now=NOW)

    assert any(f.check == "stale" for f in findings)


def test_dormant_resumed_only_flags_recent_publication():
    assert check_dormant_resumed("x", [_ep(5)], NOW).check == "dormant-resumed"
    assert check_dormant_resumed("x", [_ep(45)], NOW) is None


def test_audit_city_dormant_resumed_is_separate_from_stale():
    city = _city()
    city.lifecycle = FeedLifecycle(status="dormant", reason="irregular")

    findings = audit_city(city, provider=_FakeProvider([_ep(5)]), now=NOW)

    checks = {finding.check for finding in findings}
    assert "dormant-resumed" in checks
    assert "stale" not in checks


# ---------------------------------------------------------------------------
# check_meetings_url
# ---------------------------------------------------------------------------


def test_check_meetings_url_clean():
    probe = lambda url: (200, url)  # noqa: E731
    assert check_meetings_url("x-tx", "https://x.gov/government/meetings/watch", probe) is None


def test_check_meetings_url_dead():
    probe = lambda url: (404, url)  # noqa: E731
    f = check_meetings_url("x-tx", "https://x.gov/government/meetings/watch", probe)
    assert f is not None and f.check == "meetings-url-dead" and f.severity == "error"
    assert "404" in f.message


def test_check_meetings_url_forbidden_is_inconclusive():
    probe = lambda url: (403, url)  # noqa: E731
    assert check_meetings_url("x-tx", "https://x.gov/government/meetings/watch", probe) is None


def test_check_meetings_url_server_error_is_inconclusive():
    probe = lambda url: (503, url)  # noqa: E731
    assert check_meetings_url("x-tx", "https://x.gov/government/meetings/watch", probe) is None


def test_check_meetings_url_redirected_to_homepage():
    # Deep link with ≥3 path segments bounces to root → meetings-url-changed warning.
    probe = lambda url: (200, "https://x.gov/")  # noqa: E731
    f = check_meetings_url("x-tx", "https://x.gov/government/meetings/watch", probe)
    assert f is not None and f.check == "meetings-url-changed" and f.severity == "warn"
    assert "reorganised" in f.message


def test_check_meetings_url_shallow_redirect_not_flagged():
    # A 2-segment configured URL redirecting somewhere is not "dramatically different".
    probe = lambda url: (200, "https://x.gov/other")  # noqa: E731
    assert check_meetings_url("x-tx", "https://x.gov/meetings", probe) is None


def test_check_meetings_url_probe_exception():
    def probe(url):
        raise ConnectionError("timeout")

    assert check_meetings_url("x-tx", "https://x.gov/government/meetings/watch", probe) is None


# ---------------------------------------------------------------------------
# check_provider_error_rates
# ---------------------------------------------------------------------------


def _perr(*counts: tuple[str, int]) -> dict:
    return {"provider_errors": dict(counts)}


def test_check_provider_error_rates_empty_history():
    assert check_provider_error_rates([]) == []


def test_check_provider_error_rates_no_errors_no_finding():
    history = [_perr(("granicus", 0)), _perr(("granicus", 0))]
    assert check_provider_error_rates(history) == []


def test_check_provider_error_rates_below_threshold_no_finding():
    # Only 1 run with errors — below the default threshold of 2.
    history = [_perr(("granicus", 3)), {"materialized": 5}]
    assert check_provider_error_rates(history) == []


def test_check_provider_error_rates_threshold_reached():
    history = [_perr(("granicus", 1)), _perr(("granicus", 2)), _perr(("granicus", 0))]
    findings = check_provider_error_rates(history)
    assert len(findings) == 1
    f = findings[0]
    assert f.check == "provider-errors:granicus"
    assert f.severity == WARN
    assert "granicus" in f.message
    assert "2" in f.message  # 2 of 3 runs


def test_check_provider_error_rates_only_flags_breaching_providers():
    history = [
        _perr(("granicus", 2), ("swagit", 0)),
        _perr(("granicus", 1), ("swagit", 1)),
    ]
    findings = check_provider_error_rates(history)
    assert len(findings) == 1
    assert findings[0].check == "provider-errors:granicus"


def test_check_provider_error_rates_missing_key_tolerated():
    # Runs without a provider_errors key should not crash or count as errors.
    history = [{"materialized": 5}, {"provider_errors": {"civicplus": 1}}]
    assert check_provider_error_rates(history) == []


def test_check_provider_error_rates_custom_threshold():
    history = [_perr(("granicus", 1)), _perr(("granicus", 1)), _perr(("granicus", 1))]
    assert check_provider_error_rates(history, threshold=4) == []
    assert len(check_provider_error_rates(history, threshold=2)) == 1


def test_check_provider_error_rates_slug_is_all():
    history = [_perr(("granicus", 1)), _perr(("granicus", 1))]
    f = check_provider_error_rates(history)[0]
    assert f.slug == "(all)"
