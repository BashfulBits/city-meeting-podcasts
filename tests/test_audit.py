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
    check_agenda_quality,
    check_dead_audio_aggregate,
    check_deferred_audio_aggregate,
    check_dormant_resumed,
    check_empty,
    check_enclosures,
    check_meetings_url,
    check_provider_error_rates,
    check_rehost_backlog,
    check_roster_quality,
    check_staleness,
    check_unexpected_bodies,
    check_view_cap,
    compute_archive_diff,
    count_audio_failures,
)
from citypods.bodies import BodyInclusion
from citypods.models import Episode, FeedLifecycle
from citypods.providers.base import ProviderError

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


def test_check_agenda_quality_requires_repeated_rejection():
    records = {
        f"u{i}": {
            "uid": f"u{i}",
            "title": f"Meeting {i}",
            "body": "City Council",
            "agenda_text": {
                "quality": {
                    "status": "rejected",
                    "reason": "ambiguous-native-and-ocr",
                    "assessment_attempts": 3,
                    "source_url": (
                        "https://user:password@example.test/agenda.pdf?"
                        "X-Amz-Signature=secret#page=1"
                    ),
                }
            },
        }
        for i in range(3)
    }
    finding = check_agenda_quality("council", records, body="City Council")
    assert finding is not None
    assert finding.check == "agenda-quality"
    assert "3 of 3" in finding.message
    assert "source=https://example.test/agenda.pdf" in finding.message
    assert "X-Amz-Signature" not in finding.message
    assert "password@" not in finding.message
    assert "body=City Council" in finding.message


def test_check_agenda_quality_suppresses_one_off_rejection():
    records = {
        "u1": {
            "uid": "u1",
            "body": "City Council",
            "agenda_text": {
                "quality": {
                    "status": "rejected",
                    "reason": "ambiguous-native-and-ocr",
                    "assessment_attempts": 1,
                }
            },
        }
    }
    assert check_agenda_quality("council", records, body="City Council") is None


def test_check_agenda_quality_alerts_for_one_episode_after_three_attempts():
    records = {
        "u1": {
            "uid": "u1",
            "body": "City Council",
            "agenda_text": {
                "quality": {
                    "status": "rejected",
                    "reason": "ocr-unavailable",
                    "assessment_attempts": 3,
                    "source_url": "https://example.test/agenda.pdf",
                }
            },
        }
    }
    finding = check_agenda_quality("council", records, body="City Council")
    assert finding is not None
    assert "1 of 1" in finding.message


def test_check_agenda_quality_preserves_active_artifact_but_alerts_on_last_rejection():
    records = {
        "u1": {
            "uid": "u1",
            "body": "City Council",
            "agenda_text": {
                "quality": {
                    "status": "accepted",
                    "eligibility": "agenda",
                    "assessment_attempts": 0,
                    "source_url": "https://example.test/good.pdf",
                    "last_assessment": {
                        "status": "rejected",
                        "reason": "ambiguous-native-and-ocr",
                        "assessment_attempts": 3,
                        "source_url": "https://example.test/shell.pdf?token=secret",
                    },
                }
            },
        }
    }
    finding = check_agenda_quality("council", records, body="City Council")
    assert finding is not None
    assert "source=https://example.test/shell.pdf" in finding.message
    assert "token=secret" not in finding.message


def test_check_agenda_quality_applies_body_inclusions():
    records = {
        "included": {
            "uid": "included",
            "guid": "included",
            "body": "Other Body",
            "agenda_text": {
                "quality": {
                    "status": "rejected",
                    "reason": "ambiguous-native-and-ocr",
                    "assessment_attempts": 3,
                }
            },
        },
        "excluded": {
            "uid": "excluded",
            "guid": "excluded",
            "body": "Other Body",
            "agenda_text": {
                "quality": {
                    "status": "rejected",
                    "reason": "ambiguous-native-and-ocr",
                    "assessment_attempts": 3,
                }
            },
        },
    }
    finding = check_agenda_quality(
        "council",
        records,
        inclusions=(BodyInclusion("included", "City Council"),),
    )
    assert finding is not None
    assert "uid=included" in finding.message
    assert "uid=excluded" not in finding.message


def test_check_agenda_quality_ignores_stale_assessment_history():
    records = {
        "u1": {
            "uid": "u1",
            "body": "City Council",
            "agenda_text": {
                "quality": {
                    "status": "rejected",
                    "reason": "ambiguous-native-and-ocr",
                    "assessment_attempts": 3,
                    "last_seen": (NOW - timedelta(days=91)).isoformat(),
                }
            },
        }
    }
    assert check_agenda_quality("council", records, body="City Council", now=NOW) is None


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


def test_check_staleness_allows_five_intervals_for_biweekly_feed():
    # Five 14-day intervals gives a 70-day threshold; a 69-day-old feed is still within it.
    healthy = [_ep(69), _ep(83), _ep(97), _ep(111), _ep(125)]
    assert check_staleness("s", healthy, NOW) is None

    overdue = [_ep(71), _ep(85), _ep(99), _ep(113), _ep(127)]
    assert check_staleness("s", overdue, NOW) is not None


def test_check_staleness_uses_longer_absolute_floor_for_weekly_feed():
    # Five weekly intervals would be 35 days, so the 45-day floor controls the threshold.
    healthy = [_ep(44), _ep(51), _ep(58), _ep(65), _ep(72)]
    assert check_staleness("s", healthy, NOW) is None

    overdue = [_ep(46), _ep(53), _ep(60), _ep(67), _ep(74)]
    assert check_staleness("s", overdue, NOW) is not None


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


def test_check_unexpected_bodies_distinguishes_one_off_recurrence_and_new_labels():
    city = _city()
    city.source = {
        "feed_url": "u",
        "body": "City Council",
        "body_includes": [
            {"provider_guid": "https://example/old-work-session", "body": "Work Session"}
        ],
    }
    current_one_off = _ep(1, "https://example/new-work-session")
    current_one_off.body = "Work Session"
    current_one_off.title = "Work Session"
    new_committee = _ep(2, "https://example/new-committee")
    new_committee.body = "New Committee"
    new_committee.title = "New Committee"
    historical = _ep(3, "https://example/old-planning")
    historical.body = "Planning and Zoning Commission"
    records = {
        "old-work": {
            "provider_guid": "https://example/old-work-session",
            "body": "Work Session",
        },
        "old-planning": {
            "provider_guid": "https://example/old-planning",
            "body": "Planning and Zoning Commission",
        },
    }

    finding = check_unexpected_bodies(
        city.slug,
        [current_one_off, new_committee, historical],
        records,
        related_cities=[city],
    )

    assert finding is not None
    assert finding.check == "unexpected-body"
    assert "same label as a configured one-off inclusion" in finding.message
    assert "https://example/old-work-session" in finding.message
    assert "new label not present in the append-only archive" in finding.message
    assert "Planning and Zoning Commission" not in finding.message


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


def test_audit_city_reports_new_excluded_label_before_archive_merge():
    city = _city()
    city.source = {"feed_url": "u", "body": "City Council"}
    episode = _ep(1, "new-committee")
    episode.body = "New Committee"
    episode.title = "New Committee"

    findings = audit_city(
        city,
        provider=_FakeProvider([episode]),
        now=NOW,
        records={},
        related_cities=[city],
    )

    unexpected = [finding for finding in findings if finding.check == "unexpected-body"]
    assert len(unexpected) == 1
    assert "New Committee" in unexpected[0].message


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


def test_audit_all_unexpected_evidence_only_deduplicates_sources(monkeypatch, tmp_path):
    city1 = _city()
    city1.slug = "c1"
    city1.source = {"feed_url": "https://example.com/source", "body": "Council"}

    city2 = _city()
    city2.slug = "c2"
    city2.source = {"feed_url": "https://example.com/source", "body": "Zoning"}

    fetch_counts: list[str] = []

    class MockProvider:
        def fetch_episodes(self, source, **kwargs):
            fetch_counts.append(source.get("feed_url"))
            return []

    monkeypatch.setattr("citypods.providers.get_provider", lambda _name: MockProvider())

    audit_all(
        [city1, city2],
        site_config={},
        output_dir=tmp_path,
        unexpected_evidence_only=True,
        now=NOW,
    )
    assert len(fetch_counts) == 1


def test_audit_all_unexpected_evidence_only_deduplicates_unreachable_sources(monkeypatch, tmp_path):
    city1 = _city()
    city1.slug = "c1"
    city1.source = {"feed_url": "https://example.com/source", "body": "Council"}

    city2 = _city()
    city2.slug = "c2"
    city2.source = {"feed_url": "https://example.com/source", "body": "Zoning"}

    fetch_counts: list[str] = []

    class FailingProvider:
        def fetch_episodes(self, source, **kwargs):
            fetch_counts.append(source.get("feed_url"))
            raise ProviderError("Connection failed")

    monkeypatch.setattr("citypods.providers.get_provider", lambda _name: FailingProvider())

    audit_all(
        [city1, city2],
        site_config={},
        output_dir=tmp_path,
        unexpected_evidence_only=True,
        now=NOW,
    )
    assert len(fetch_counts) == 1


def test_audit_all_unexpected_evidence_only_skips_view_counts(monkeypatch, tmp_path):
    city = _city()
    city.source = {"feed_url": "https://example.com/source", "body": "Council"}

    view_count_calls: list[dict] = []

    class MockProviderWithViews:
        def fetch_episodes(self, source, **kwargs):
            return []

        def fetch_view_counts(self, source):
            view_count_calls.append(source)
            return {}

    monkeypatch.setattr("citypods.providers.get_provider", lambda _name: MockProviderWithViews())

    audit_all(
        [city],
        site_config={},
        output_dir=tmp_path,
        unexpected_evidence_only=True,
        now=NOW,
    )
    assert len(view_count_calls) == 0


def _roster_record(status, *, body="City Council", uid_date=None, members=()):
    # Recent by default: the aggregate empty test is bounded to `_ROSTER_AUDIT_RECENT_DAYS`, so a
    # fixed historical date would silently place a fixture outside the window under test.
    return {
        "published": uid_date or datetime.now(UTC).date().isoformat(),
        "body": body,
        "minutes_roster": {
            "status": status,
            "members": [{"name": name} for name in members],
        },
        "minutes_text": {"url": "https://city.example/minutes.pdf?Signature=secret"},
    }


def test_roster_quality_reports_a_disjoint_roster_on_sight():
    """A disjoint roster -- names sharing nobody with this body's own earlier meetings -- is a
    parse that succeeded on the wrong text. It is worse than no roster, because it narrows the
    allowed speaker set and suppresses correct attribution, so one is enough to report.

    Derived from records rather than persisted by the stage: `minutes_roster` belongs to the audio
    lane, so a diagnostic written by the speaker-identity lane never survives its push.
    """
    records = {
        "u1": _roster_record("parsed", uid_date="2026-01-01", members=["Jane Doe", "Bob Chair"]),
        "u2": _roster_record("parsed", uid_date="2026-02-01", members=["Jane Doe", "New Member"]),
        "u3": _roster_record("parsed", uid_date="2026-03-01", members=["Wrong One", "Other Wrong"]),
    }
    finding = check_roster_quality("council", records, body="City Council")
    assert finding is not None
    assert finding.check == "roster-quality"
    assert "share nobody" in finding.message
    assert "Resolution:" in finding.message
    # The minutes URL is diagnostic, but must not carry signed material into a public issue.
    assert "secret" not in finding.message


def test_roster_quality_ignores_a_single_unparsed_document():
    """One unparsed minutes document is normal variation across civic document formats; only a
    feed-wide pattern indicates a parser gap worth a maintainer's time."""
    records = {"u1": _roster_record("empty"), "u2": _roster_record("parsed")}
    assert check_roster_quality("council", records, body="City Council") is None


def test_roster_quality_reports_a_feed_wide_parse_failure():
    records = {f"u{i}": _roster_record("empty") for i in range(4)}
    records["ok"] = _roster_record("parsed")
    finding = check_roster_quality("council", records, body="City Council")
    assert finding is not None
    assert "4 of 5 published minutes yielded no usable roster" in finding.message


def test_roster_quality_does_not_flag_a_bodys_first_roster():
    """Onboarding has nothing to be disjoint from; flagging it would fire the signal loudest
    exactly when new cities are added."""
    records = {"u1": _roster_record("parsed", members=["Jane Doe"])}
    assert check_roster_quality("council", records, body="City Council") is None


def test_roster_quality_reads_the_versioned_status_prefix():
    """Statuses carry the parser version that produced them, so a bump can re-extract cached
    minutes; the outcome is the part before it."""
    records = {f"u{i}": _roster_record("empty@2") for i in range(4)}
    records["ok"] = _roster_record("parsed@2", members=["Jane Doe"])
    finding = check_roster_quality("council", records, body="City Council")
    assert finding is not None and "4 of 5" in finding.message


def test_roster_quality_is_silent_when_minutes_were_never_fetched():
    """Absent minutes are not a roster defect -- they are the normal weeks-long publication lag,
    and flagging them would fire on every recent meeting in the catalog."""
    assert check_roster_quality("council", {"u1": {"published": "2026-05-01"}}) is None


def test_roster_quality_counts_only_the_body_it_was_asked_about():
    """One source's record store holds every body, so an unscoped count would report another
    body's parser gap against this feed."""
    records = {
        f"c{i}": _roster_record("empty@2", body="City Council", uid_date="2026-09-01")
        for i in range(4)
    }
    records["ok"] = _roster_record(
        "parsed@2", body="City Council", uid_date="2026-09-02", members=["Jane Doe"]
    )
    records.update(
        {
            f"e{i}": _roster_record("empty@2", body="Board of Ethics", uid_date="2026-09-01")
            for i in range(9)
        }
    )
    finding = check_roster_quality(
        "council", records, body="Board of Ethics", now=datetime(2026, 9, 6, tzinfo=UTC)
    )
    assert finding is not None and "9 of 9" in finding.message


def test_roster_quality_bounds_the_empty_ratio_by_recency():
    """An append-only archive would otherwise dilute the ratio permanently: hundreds of historical
    rosters swamp a genuine regression in the last few meetings, and records left by a parser gap
    that has since been fixed keep the finding alive until every uid is re-run."""
    now = datetime(2026, 9, 6, tzinfo=UTC)
    archive = {f"o{i}": _roster_record("empty@1", uid_date="2020-01-01") for i in range(50)}
    assert check_roster_quality("council", archive, body="City Council", now=now) is None

    recent = {f"r{i}": _roster_record("empty@2", uid_date="2026-09-01") for i in range(4)}
    recent["ok"] = _roster_record("parsed@2", uid_date="2026-09-02", members=["Jane Doe"])
    finding = check_roster_quality("council", {**archive, **recent}, body="City Council", now=now)
    assert finding is not None and "4 of 5" in finding.message
