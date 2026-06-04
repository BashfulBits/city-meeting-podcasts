"""Tests for the resource report (JSON/Markdown/admin page) + JS↔Python model parity."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from citypods.models import City
from citypods.report import build_report, build_status, to_admin_html, to_markdown, to_status_html


def _city(slug, provider, extract=False):
    return City(
        slug=slug,
        provider=provider,
        source={"feed_url": "u"} if provider != "swagit" else {"list_url": "u", "body": "b"},
        podcast_title="t",
        podcast_author="a",
        podcast_email="",
        podcast_description="d",
        extract_audio=extract,
    )


def _cities():
    # 2 hosted (swagit HLS + extract_audio granicus) + 2 not hosted (plain granicus)
    return [
        _city("a", "swagit"),
        _city("b", "granicus", extract=True),
        _city("c", "granicus"),
        _city("d", "granicus"),
    ]


SITE = {"defaults": {"max_episodes": 50, "audio_max_kbps": 96, "materialize_budget_per_run": 25}}


def test_build_report_measures_host_fraction():
    rep = build_report(_cities(), site_config=SITE)
    assert rep["generated_for_feeds"] == 4
    # 2 of 4 hosted -> host_frac 0.5
    assert rep["current"]["inputs"]["host_frac"] == 0.5
    assert rep["current"]["cap_is_bottleneck"] is True
    assert "1000" in rep["scale_scenarios"]


def test_markdown_summary_has_cost_and_bottleneck():
    md = to_markdown(build_report(_cities(), site_config=SITE))
    assert "$" in md and "/mo" in md
    assert "bottleneck" in md.lower()
    assert "| Feeds |" in md


def test_audio_failures_surface_in_report_and_markdown(tmp_path):
    from citypods.records import save_records, source_key

    city = _city("a", "swagit")
    save_records(
        tmp_path,
        source_key(city),
        {
            "u1": {"audio": {"error": "dead"}},
            "u2": {"audio": {"error": "dead"}},
            "u3": {"audio": {"error": "deferred"}},
            "u4": {"audio": {"error": None}},
        },
    )
    rep = build_report([city], site_config=SITE, state_dir=tmp_path)
    assert rep["audio_failures"] == {"deferred": 1, "dead": 2, "examples": ["a (2 dead)"]}
    md = to_markdown(rep)
    assert "Un-materializable audio" in md and "2 dead" in md and "MEDIA_DEFERRED" in md


def test_truncation_counts_per_body_not_whole_shared_archive(tmp_path):
    """Boards sharing one Swagit view share a record store holding all bodies' episodes.
    Truncation must count only each feed's own body, not the whole shared archive."""
    from citypods.records import save_records, source_key

    def board(slug, body):
        c = _city(slug, "swagit")
        c.source = {"list_url": "shared", "body": body}
        c.max_episodes = 25
        return c

    ethics = board("ethics", "Board of Ethics")
    council = board("council", "City Council")
    # Shared archive: 5 ethics meetings + 100 council meetings (same source_key).
    records = {f"e{i}": {"body": "Board of Ethics"} for i in range(5)}
    records.update({f"c{i}": {"body": "City Council"} for i in range(100)})
    save_records(tmp_path, source_key(ethics), records)

    rep = build_report([ethics, council], site_config=SITE, state_dir=tmp_path)
    trunc = rep["truncation"]
    # Only council (100 > 25) is truncated; ethics (5 < 25) is not flagged with a bogus 105.
    assert trunc["truncated"] == 1
    assert trunc["max_gap"] == 75
    assert trunc["examples"] == ["council (100 archived, 25 shown)"]


def test_admin_html_substitutes_and_embeds_valid_json():
    html = to_admin_html(build_report(_cities(), site_config=SITE))
    assert "__REPORT_JSON__" not in html and "__SEED_JSON__" not in html
    m = re.search(r'<script id="report" type="application/json">(.*?)</script>', html, re.S)
    assert m and json.loads(m.group(1))["generated_for_feeds"] == 4


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available for JS parity")
def test_js_python_parity():
    """The admin page re-implements project() in JS; assert it matches Python for sample inputs."""
    from citypods.projection import ModelInputs, project

    html = to_admin_html(build_report(_cities(), site_config=SITE))
    js_fn = re.search(r"function project\(i\)\{.*?\n\}", html, re.S).group(0)
    # project() references module-level consts in the page; include them so node can run it.
    consts = re.search(r"const B2_GB_MO\s*=.*?;", html, re.S).group(0)
    js_fn = consts + "\n" + js_fn
    cases = [
        dict(
            feeds=1000,
            episodes_per_feed=50,
            duration_hours=2,
            kbps=96,
            host_frac=1,
            sec_per_ep=90,
            cycle_hours=6,
            time_budget_hours=5,
            safety=0.8,
            per_run_cap=0,
            meetings_per_week=1,
        ),
        dict(
            feeds=80,
            episodes_per_feed=50,
            duration_hours=2,
            kbps=96,
            host_frac=0.5,
            sec_per_ep=120,
            cycle_hours=6,
            time_budget_hours=5,
            safety=0.8,
            per_run_cap=25,
            meetings_per_week=1,
        ),
    ]
    emit = (
        "console.log(JSON.stringify(cases.map(project).map("
        "r=>[Math.round(r.monthly*100),r.through,Math.round(r.backfillDays)])));"
    )
    script = js_fn + "\nconst cases=" + json.dumps(cases) + ";\n" + emit
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout
    js = json.loads(out)
    for case, (jcost, jthrough, jdays) in zip(cases, js, strict=True):
        cap = case["per_run_cap"] or None
        p = project(ModelInputs(**{**case, "per_run_cap": cap}))
        assert round(p.monthly_cost_usd * 100) == jcost
        assert p.per_run_throughput == jthrough
        assert round(p.full_backfill_days) == jdays


# ---------------------------------------------------------------------------
# build_status / to_status_html tests (issue #124)
# ---------------------------------------------------------------------------


def _city2(slug, provider="granicus", author="City of Test, TX", state="TX", extract=False):
    return City(
        slug=slug,
        provider=provider,
        source={"feed_url": "http://example.com/feed"},
        podcast_title=f"{slug} meetings",
        podcast_author=author,
        podcast_email="",
        podcast_description="d",
        state=state,
        extract_audio=extract,
    )


def _hls_city(slug, author="City of Test, TX"):
    return City(
        slug=slug,
        provider="civicplus",
        source={"feed_url": "http://example.com/feed"},
        podcast_title=f"{slug} meetings",
        podcast_author=author,
        podcast_email="",
        podcast_description="d",
    )


def _rec(
    uid,
    *,
    media_kind="hls",
    hosted_url=None,
    spec_hash=None,
    error=None,
    duration=3600,
    bytes_val=None,
    published="2026-05-01T00:00:00+00:00",
):
    return {
        "uid": uid,
        "title": f"Meeting {uid}",
        "published": published,
        "body": None,
        "media_kind": media_kind,
        "video_url": f"http://example.com/{uid}",
        "duration": duration,
        "links": {},
        "chapters": [],
        "summary": "",
        "transcript_url": None,
        "audio": {
            "key": f"k/{uid}.m4a" if hosted_url else None,
            "url": hosted_url,
            "spec_hash": spec_hash,
            "bytes": bytes_val,
            "attempts": 1 if error else 0,
            "last_attempt": "2026-05-01T00:00:00+00:00" if error else None,
            "error": error,
        },
    }


def test_build_status_empty():
    """build_status with no cities and no state_dir returns valid zero-counts."""
    status = build_status([], site_config=SITE)
    assert status["kpis"]["feeds"] == 0
    assert status["kpis"]["meetings_archived"] == 0
    assert status["kpis"]["hosted_audio"] == 0
    assert status["kpis"]["linked_video"] == 0
    assert status["issues"]["deferred"] == 0
    assert status["issues"]["dead"] == 0


def test_build_status_episode_taxonomy(tmp_path):
    """Classify records into the correct states from the issue taxonomy."""
    from citypods.records import audio_spec_hash, record_to_episode, save_records, source_key

    city = _hls_city("test-city")

    # Derive the correct spec hash by going through record_to_episode, matching _classify_record.
    _r1_raw = _rec("r1", hosted_url="http://cdn/r1.m4a", spec_hash=None, bytes_val=10_000_000)
    good_spec = audio_spec_hash(record_to_episode(_r1_raw), max_kbps=96)
    _r1_raw["audio"]["spec_hash"] = good_spec

    records = {
        "r1": _r1_raw,  # served/current
        "r2": _rec(
            "r2", hosted_url="http://cdn/r2.m4a", spec_hash="deadbeef000a", bytes_val=10_000_000
        ),  # stale (different spec)
        "r3": _rec("r3"),  # pending (HLS, no enclosure, no error)
        "r4": _rec("r4", error="deferred"),  # deferred (#122)
        "r5": _rec("r5", error="dead"),  # dead (#120)
        "r6": _rec("r6", error="error"),  # transient error
        "r7": _rec("r7", media_kind="direct"),  # linked video
    }
    save_records(tmp_path, source_key(city), records)

    status = build_status([city], site_config=SITE, state_dir=tmp_path)
    k = status["kpis"]
    assert k["meetings_archived"] == 7
    assert k["hosted_audio"] == 2  # r1 (served) + r2 (stale)
    assert k["linked_video"] == 1  # r7

    row = status["feeds_by_feed"][0]
    assert row["served"] == 1
    assert row["stale"] == 1
    assert row["pending"] == 1
    assert row["deferred"] == 1
    assert row["dead"] == 1
    assert row["transient_errors"] == 1
    assert row["linked_video"] == 1
    assert row["health"] == "error"  # dead > 0

    issues = status["issues"]
    assert issues["deferred"] == 1
    assert issues["dead"] == 1
    assert issues["transient_errors"] == 1


def test_build_status_stale_detection(tmp_path):
    """Stale = hosted audio whose stored spec hash differs from the current desired spec."""
    from citypods.records import audio_spec_hash, record_to_episode, save_records, source_key

    city = _hls_city("stale-city")

    # Build raw records first so we can derive the spec hash the same way _classify_record will.
    rec_a = _rec("a", hosted_url="http://cdn/a.m4a", spec_hash=None)
    current_spec = audio_spec_hash(record_to_episode(rec_a), max_kbps=96)
    rec_a["audio"]["spec_hash"] = current_spec  # served: spec matches

    records = {
        "a": rec_a,
        "b": _rec("b", hosted_url="http://cdn/b.m4a", spec_hash="000000000000"),  # stale
        "c": _rec("c", hosted_url="http://cdn/c.m4a", spec_hash="legacy"),  # legacy → served
    }
    save_records(tmp_path, source_key(city), records)

    status = build_status([city], site_config=SITE, state_dir=tmp_path)
    row = status["feeds_by_feed"][0]
    assert row["served"] == 2  # a + c (legacy)
    assert row["stale"] == 1  # b
    assert status["backlog"]["stale"] == 1


def test_build_status_audio_bytes_exact_flag(tmp_path):
    """gb_exact is False when any hosted record is missing audio.bytes."""
    from citypods.records import save_records, source_key

    city = _hls_city("bytes-city")
    records = {
        "a": _rec("a", hosted_url="http://cdn/a.m4a", spec_hash="legacy", bytes_val=20_000_000),
        # bytes_val=None simulates an older record without stored size
        "b": _rec("b", hosted_url="http://cdn/b.m4a", spec_hash="legacy", bytes_val=None),
    }
    save_records(tmp_path, source_key(city), records)

    status = build_status([city], site_config=SITE, state_dir=tmp_path)
    assert status["kpis"]["gb_exact"] is False
    assert status["kpis"]["gb_stored"] == pytest.approx(0.02, abs=1e-4)  # only a's bytes counted


def test_build_status_per_city_rollup(tmp_path):
    """Per-city rows roll up correctly by podcast_author."""
    from citypods.records import save_records, source_key

    dallas1 = _city2("dallas-council", author="City of Dallas, TX")
    dallas2 = _city2("dallas-planning", author="City of Dallas, TX")
    ft_worth = _city2("fort-worth", author="City of Fort Worth, TX")

    for city in [dallas1, dallas2, ft_worth]:
        save_records(
            tmp_path,
            source_key(city),
            {
                "ep1": _rec("ep1", media_kind="direct"),  # linked video
            },
        )

    status = build_status([dallas1, dallas2, ft_worth], site_config=SITE, state_dir=tmp_path)
    city_rows = {r["city"]: r for r in status["feeds_by_city"]}
    assert "City of Dallas, TX" in city_rows
    assert city_rows["City of Dallas, TX"]["feeds"] == 2
    assert city_rows["City of Dallas, TX"]["linked_video"] == 2
    assert "City of Fort Worth, TX" in city_rows
    assert city_rows["City of Fort Worth, TX"]["feeds"] == 1


def test_build_status_health_histogram(tmp_path):
    """City health histogram counts feeds per health state."""
    from citypods.records import save_records, source_key

    # Distinct source URLs → distinct source keys for each feed even within the same city.
    def _feed(slug, author, feed_url):
        return City(
            slug=slug,
            provider="granicus",
            source={"feed_url": feed_url},
            podcast_title=slug,
            podcast_author=author,
            podcast_email="",
            podcast_description="d",
        )

    ok_city = _feed("ok-feed", "City A, TX", "http://example.com/ok")
    warn_city = _feed("warn-feed", "City A, TX", "http://example.com/warn")  # same city → 2 feeds
    err_city = _feed("err-feed", "City B, TX", "http://example.com/err")

    save_records(
        tmp_path,
        source_key(ok_city),
        {"e1": _rec("e1", hosted_url="http://cdn/e1.m4a", spec_hash="legacy")},
    )
    save_records(tmp_path, source_key(warn_city), {"e2": _rec("e2")})  # pending → warn
    save_records(tmp_path, source_key(err_city), {"e3": _rec("e3", error="dead")})  # dead → error

    status = build_status([ok_city, warn_city, err_city], site_config=SITE, state_dir=tmp_path)
    city_rows = {r["city"]: r for r in status["feeds_by_city"]}
    assert city_rows["City A, TX"]["health_ok"] == 1
    assert city_rows["City A, TX"]["health_warn"] == 1
    assert city_rows["City A, TX"]["health_error"] == 0
    assert city_rows["City B, TX"]["health_error"] == 1


def test_to_status_html_substitution():
    """to_status_html replaces the placeholder and embeds valid JSON."""
    status = build_status([], site_config=SITE)
    html = to_status_html(status)
    assert "__STATUS_JSON__" not in html
    m = re.search(r'<script id="status-data" type="application/json">(.*?)</script>', html, re.S)
    assert m, "status-data script tag not found"
    parsed = json.loads(m.group(1))
    assert "kpis" in parsed and "feeds_by_feed" in parsed


def test_audio_bytes_round_trip():
    """audio.bytes is persisted and restored through episode_to_record / record_to_episode."""
    from datetime import UTC, datetime

    from citypods.models import Episode
    from citypods.records import episode_to_record, record_to_episode

    ep = Episode(
        guid="g",
        title="t",
        published=datetime.now(UTC),
        video_url="http://example.com/ep",
        audio_bytes=12_345_678,
    )
    rec = episode_to_record(ep)
    assert rec["audio"]["bytes"] == 12_345_678

    ep2 = record_to_episode(rec)
    assert ep2.audio_bytes == 12_345_678


def test_audio_bytes_none_round_trip():
    """audio.bytes=None (older records) round-trips without error."""
    from datetime import UTC, datetime

    from citypods.models import Episode
    from citypods.records import episode_to_record, record_to_episode

    ep = Episode(
        guid="g",
        title="t",
        published=datetime.now(UTC),
        video_url="http://example.com/ep",
        audio_bytes=None,
    )
    rec = episode_to_record(ep)
    assert rec["audio"]["bytes"] is None

    ep2 = record_to_episode(rec)
    assert ep2.audio_bytes is None


def _distinct_feed(slug, author, provider="granicus", feed_url=None):
    """City with a unique source URL so each feed gets its own source key."""
    return City(
        slug=slug,
        provider=provider,
        source={"feed_url": feed_url or f"http://example.com/{slug}"},
        podcast_title=slug,
        podcast_author=author,
        podcast_email="",
        podcast_description="d",
    )


def test_city_row_provider_and_latest(tmp_path):
    """City rows carry the provider and the latest publication date across all feeds."""
    from citypods.records import save_records, source_key

    a1 = _distinct_feed("a1", "City A, TX", provider="granicus")
    a2 = _distinct_feed("a2", "City A, TX", provider="granicus")
    b1 = _distinct_feed("b1", "City B, TX", provider="civicplus")

    # a1 published earlier; a2 later — city A's latest should be a2's date.
    def _direct(uid, pub):
        return _rec(uid, media_kind="direct", published=pub)

    save_records(tmp_path, source_key(a1), {"e1": _direct("e1", "2026-03-01T00:00:00+00:00")})
    save_records(tmp_path, source_key(a2), {"e2": _direct("e2", "2026-05-15T00:00:00+00:00")})
    save_records(tmp_path, source_key(b1), {"e3": _direct("e3", "2026-01-20T00:00:00+00:00")})

    status = build_status([a1, a2, b1], site_config=SITE, state_dir=tmp_path)
    city_map = {r["city"]: r for r in status["feeds_by_city"]}

    assert city_map["City A, TX"]["provider"] == "granicus"
    assert city_map["City A, TX"]["last_published"] == "2026-05-15"  # max across a1, a2

    assert city_map["City B, TX"]["provider"] == "civicplus"
    assert city_map["City B, TX"]["last_published"] == "2026-01-20"


def test_city_row_mixed_providers(tmp_path):
    """A city with feeds from different providers shows a comma-joined provider string."""
    from citypods.records import save_records, source_key

    g = _distinct_feed("mixed-g", "Mixed City, TX", provider="granicus")
    c = _distinct_feed("mixed-cp", "Mixed City, TX", provider="civicplus")
    save_records(tmp_path, source_key(g), {"e1": _rec("e1", media_kind="direct")})
    save_records(tmp_path, source_key(c), {"e2": _rec("e2")})

    status = build_status([g, c], site_config=SITE, state_dir=tmp_path)
    prov = status["feeds_by_city"][0]["provider"]
    assert "civicplus" in prov and "granicus" in prov
    assert ", " in prov  # joined, not collapsed


def test_city_row_latest_none_when_no_records(tmp_path):
    """last_published is None for a city whose feeds have no records."""
    from citypods.records import save_records, source_key

    city = _distinct_feed("empty-feed", "Empty City, TX")
    save_records(tmp_path, source_key(city), {})

    status = build_status([city], site_config=SITE, state_dir=tmp_path)
    assert status["feeds_by_city"][0]["last_published"] is None


def test_oldest_publication_year(tmp_path):
    """build_status extracts the oldest publication year from all record stores."""
    from citypods.records import save_records, source_key

    city1 = _hls_city("pub-city1")
    city2 = _distinct_feed("pub-city2", "City B, TX")

    save_records(
        tmp_path,
        source_key(city1),
        {
            "new": _rec("new", published="2025-06-01T00:00:00+00:00"),
            "old": _rec("old", published="2019-03-15T00:00:00+00:00"),  # oldest
        },
    )
    save_records(
        tmp_path,
        source_key(city2),
        {
            "mid": _rec("mid", media_kind="direct", published="2022-09-01T00:00:00+00:00"),
        },
    )

    status = build_status([city1, city2], site_config=SITE, state_dir=tmp_path)
    assert status["storage"]["oldest_publication_year"] == 2019


def test_oldest_publication_year_none_when_no_records():
    """oldest_publication_year is None when no cities or records exist."""
    status = build_status([], site_config=SITE)
    assert status["storage"]["oldest_publication_year"] is None


def test_feed_row_last_published_max_per_body(tmp_path):
    """last_published on a feed row is the newest publication date for that feed's records."""
    from citypods.records import save_records, source_key

    city = _distinct_feed("dated-feed", "Date City, TX")
    save_records(
        tmp_path,
        source_key(city),
        {
            "early": _rec("early", media_kind="direct", published="2024-01-01T00:00:00+00:00"),
            "mid": _rec("mid", media_kind="direct", published="2025-06-15T00:00:00+00:00"),
            "late": _rec("late", media_kind="direct", published="2026-05-30T00:00:00+00:00"),
        },
    )

    status = build_status([city], site_config=SITE, state_dir=tmp_path)
    assert status["feeds_by_feed"][0]["last_published"] == "2026-05-30"


def test_github_run_url_in_run_history(tmp_path):
    """github_run_url written by _record_run_history is included in build_status run_history."""
    import json

    entry = {
        "ts": "2026-06-02T14:37:00+00:00",
        "built": 5,
        "skipped": 82,
        "errors": 0,
        "materialized": 8,
        "materialize_encoded": 7,
        "materialize_seconds": 698.0,
        "stages": {
            "audio": {
                "ran": 8,
                "encoded": 7,
                "credited": 1,
                "reused": 41,
                "backlog": 0,
                "seconds": 698.0,
                "bytes": 12_900_000,
                "errors": 0,
            }
        },
        "github_run_id": "99999",
        "github_run_url": "https://github.com/example/repo/actions/runs/99999",
    }
    path = tmp_path / "run_history.jsonl"
    path.write_text(json.dumps(entry) + "\n")

    status = build_status([], site_config=SITE, state_dir=tmp_path)
    assert status["run_history"]
    assert status["run_history"][0]["github_run_url"] == entry["github_run_url"]
    assert status["run_history"][0]["github_run_id"] == "99999"
    # Also surfaced in kpis.last_build via run_summary (separate file; not tested here)


def test_audio_bytes_set_on_encode(tmp_path):
    """materialize_audio sets ep.audio_bytes from the encoded file size."""
    from datetime import UTC, datetime

    from citypods.media import materialize_audio
    from citypods.models import City, Episode

    city = City(
        slug="bytes-test",
        provider="civicplus",
        source={"feed_url": "http://x.com/f"},
        podcast_title="t",
        podcast_author="a",
        podcast_email="",
        podcast_description="d",
    )
    ep = Episode(
        guid="g",
        title="t",
        published=datetime.now(UTC),
        video_url="http://x.com/v",
        media_kind="hls",
    )

    fake_size = 5_432_100

    class FakeFfmpeg:
        def extract_audio(
            self,
            timeline,
            sources_by_id,
            dest,
            chapters=None,
            *,
            loudness_profile=None,
            asset_resolver=None,
        ):
            dest.write_bytes(b"x" * fake_size)

    class FakeStorage:
        def put_file(self, key, path, content_type):
            return f"https://cdn/{key}"

        def exists(self, key):
            return False

        def public_url(self, key):
            return f"https://cdn/{key}"

    stats = materialize_audio(
        city,
        [ep],
        storage=FakeStorage(),
        ffmpeg=FakeFfmpeg(),
        max_kbps=96,
        resolve_media_url=lambda ep: ep.video_url,
    )
    assert stats.encoded == 1
    assert ep.audio_bytes == fake_size
