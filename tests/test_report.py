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


def test_extract_audio_true_by_default_counts_all_as_hosted():
    # When extract_audio=True on all cities (as the site default now sets), host_frac == 1.0
    cities = [_city(s, "granicus", extract=True) for s in ("a", "b", "c", "d")]
    rep = build_report(cities, site_config=SITE)
    assert rep["current"]["inputs"]["host_frac"] == 1.0


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


def test_admin_html_escapes_json_for_script_context():
    payload = '</script><script>alert("x")</script>'
    report = build_report(_cities(), site_config=SITE)
    report["notes"]["egress"] = payload
    report["current"]["inputs"]["label"] = payload

    html = to_admin_html(report)

    m = re.search(r'<script id="report" type="application/json">(.*?)</script>', html, re.S)
    assert m
    assert payload not in m.group(1)
    assert json.loads(m.group(1))["notes"]["egress"] == payload

    seed = re.search(r"const SEED = (.*?);\nconst B2_GB_MO", html, re.S)
    assert seed
    assert payload not in seed.group(1)
    assert json.loads(seed.group(1))["label"] == payload


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
    source_duration_seconds=None,
    served_duration_seconds=None,
    bytes_val=None,
    published="2026-05-01T00:00:00+00:00",
):
    rec = {
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
            "duration_served": None,
            "attempts": 1 if error else 0,
            "last_attempt": "2026-05-01T00:00:00+00:00" if error else None,
            "error": error,
        },
    }
    if source_duration_seconds is not None:
        rec["source_duration_seconds"] = source_duration_seconds
    if served_duration_seconds is not None:
        rec["served_duration_seconds"] = served_duration_seconds
    return rec


def _h15_blind(a_role: str, b_role: str) -> dict:
    return {
        "A": {"role": a_role, "recipe_hash": f"{a_role}-hash"},
        "B": {"role": b_role, "recipe_hash": f"{b_role}-hash"},
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


def test_build_status_transcript_quality_empty(tmp_path):
    """Fresh deployments with no H15 state files still render a well-formed empty panel."""
    tq = build_status([], site_config=SITE, state_dir=tmp_path)["transcript_quality"]
    assert tq == {
        "rows": [],
        "distribution": {
            "total": 0,
            "route_mode": {"provider-align": 0, "fresh-asr": 0, "unknown": 0},
            "calibrated": {"true": 0, "false": 0},
        },
        "needs_attention": [],
        "gold_summary": {
            "total": 0,
            "both_correct": 0,
            "reviewer_correction": 0,
        },
        "latest_calibration": None,
    }


def test_build_status_last_deploy_uses_github_env(monkeypatch):
    """LAST RUN block reports the Build & Deploy action that rendered the page, from the env."""
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "424242")
    monkeypatch.setenv("GITHUB_WORKFLOW", "Build & Deploy")

    deploy = build_status([], site_config=SITE)["kpis"]["last_deploy"]
    assert deploy["status"] == "deployed"
    assert deploy["workflow"] == "Build & Deploy"
    assert deploy["github_run_id"] == "424242"
    assert deploy["github_run_url"] == "https://github.com/example/repo/actions/runs/424242"
    assert deploy["ts"]


def test_build_status_last_deploy_off_ci(monkeypatch):
    """Off-CI (no run id) the deploy block degrades to a local, link-less status."""
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("GITHUB_WORKFLOW", raising=False)

    deploy = build_status([], site_config=SITE)["kpis"]["last_deploy"]
    assert deploy["status"] == "local"
    assert deploy["github_run_url"] is None
    assert deploy["workflow"] == "Build & Deploy"


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


def test_build_status_retained_per_feed_preserves_measured_zero(tmp_path):
    # CodeRabbit (PR #877): `archive_items or max_episodes_default` treated a genuinely-measured
    # 0 (e.g. a fresh deployment with no persisted records yet) the same as "unmeasured," silently
    # reporting the 50-episode default instead of the real 0.
    from citypods.records import save_records, source_key

    city = _hls_city("empty-city")
    save_records(tmp_path, source_key(city), {})

    status = build_status([city], site_config=SITE, state_dir=tmp_path)
    assert status["storage"]["retained_per_feed"] == 0


def test_retained_per_feed_reports_metadata_tier_not_audio_cap(tmp_path):
    # CodeRabbit: retained_per_feed must reflect the real (metadata-tier) record count, not the
    # audio-capped full_artifact_episodes value used for storage/cost modeling — a source keeping
    # 10 metadata rows should not be reported as capped down to its 5-record full-artifact tier.
    from citypods.records import save_records, source_key

    site = {
        "defaults": {
            "max_episodes": 2,
            "full_artifact_episodes": 5,
            "metadata_retention_episodes": 20,
            "audio_max_kbps": 96,
        }
    }
    city = _hls_city("many-records")
    save_records(tmp_path, source_key(city), {f"u{i}": _rec(f"u{i}") for i in range(10)})

    status = build_status([city], site_config=site, state_dir=tmp_path)
    assert status["storage"]["retained_per_feed"] == 10

    report = build_report([city], site_config=site, state_dir=tmp_path)
    assert report["retention"]["retained_per_feed"] == 10


def test_build_status_direct_extract_audio_counts_as_pending_not_linked(tmp_path):
    """Direct providers become audio backlog when extract_audio is enabled."""
    from citypods.records import save_records, source_key

    city = _city2("direct-hosted", extract=True)
    save_records(tmp_path, source_key(city), {"d1": _rec("d1", media_kind="direct")})

    status = build_status([city], site_config=SITE, state_dir=tmp_path)

    assert status["kpis"]["linked_video"] == 0
    assert status["kpis"]["audio_missing"] == 1
    assert status["backlog"]["by_work_class"]["audio"]["queued"] == 1
    row = status["feeds_by_feed"][0]
    assert row["linked_video"] == 0
    assert row["pending"] == 1


def test_build_status_kpis_use_unique_source_actuals(tmp_path):
    """Headline actuals are source-record counts, not sums of overlapping feed/body rows."""
    from citypods.records import save_records, source_key

    combined = _hls_city("shared-combined", author="City of Shared, TX")
    board = _hls_city("shared-board", author="City of Shared, TX")
    board.source["body"] = "Planning Commission"
    assert source_key(combined) == source_key(board)

    planning = _rec(
        "planning",
        hosted_url="http://cdn/planning.m4a",
        spec_hash="legacy",
        bytes_val=10_000_000,
    )
    planning["body"] = "Planning Commission"
    council = _rec(
        "council",
        hosted_url="http://cdn/council.m4a",
        spec_hash="legacy",
        bytes_val=20_000_000,
    )
    council["body"] = "City Council"
    save_records(tmp_path, source_key(combined), {"planning": planning, "council": council})

    status = build_status([combined, board], site_config=SITE, state_dir=tmp_path)
    assert status["feeds_by_feed"][0]["episodes"] == 2
    assert status["feeds_by_feed"][1]["episodes"] == 1
    assert status["kpis"]["meetings_archived"] == 2
    assert status["kpis"]["hosted_audio"] == 2
    assert status["kpis"]["gb_stored"] == pytest.approx(0.03)


def test_build_status_backlog_by_work_class(tmp_path):
    """H5: the actionable backlog is bucketed by output artifact + alignment-disabled split out."""
    from citypods.records import save_records, source_key

    city = _hls_city("wc-city")  # asr_enabled True, asr_alignment_enabled False by default
    a1 = _rec("a1", media_kind="hls", hosted_url=None)  # audio queued
    a2 = _rec("a2", media_kind="hls", hosted_url="http://cdn/a2.m4a")  # audio done + transcript-asr
    a3 = _rec("a3", media_kind="hls", hosted_url="http://cdn/a3.m4a")
    a3["links"] = {"transcript": "http://cdn/a3.vtt"}  # provider text → provider alignment
    save_records(tmp_path, source_key(city), {"a1": a1, "a2": a2, "a3": a3})

    status = build_status([city], site_config=SITE, state_dir=tmp_path)
    bl = status["backlog"]
    wc = bl["by_work_class"]
    assert wc["audio"]["queued"] == 1
    assert wc["audio"]["done"] == 2
    assert status["kpis"]["audio_missing"] == 1
    assert status["kpis"]["audio_coverage_pct"] == pytest.approx(66.7)
    assert status["kpis"]["audio_coverage_scope"] == "feed_visible"
    assert wc["transcript-asr"]["queued"] == 1
    assert wc["provider-transcript-align"]["queued"] == 1
    assert bl["alignment_disabled"] == 0
    assert bl["work_pending"] == 3  # audio a1 + both transcript lanes
    assert bl["deep_archive_items"] == 0


def test_build_status_provider_transcript_rollout_metrics(tmp_path):
    from citypods.records import save_records, source_key

    city = _hls_city("provider-status")
    done = _rec("done", media_kind="hls", hosted_url="http://cdn/done.m4a")
    done["links"] = {"transcript": "http://city/done.vtt"}
    done["provider_transcript"] = {
        "known_good": {
            "key": "transcripts/src/done-provider.vtt",
            "synced": True,
            "format": "vtt",
            "confidence": 0.96,
            "align_spec_hash": "align-done",
            "align_pipeline_version": "5",
            "diarize_spec_hash": "speaker-done",
            "diarize_confidence": 1.0,
        }
    }
    done["transcript"] = {
        "key": "transcripts/src/done-provider-align-align-done.vtt",
        "spec_hash": "align-done",
        "synced": True,
        "words_key": "transcripts/src/done-provider-align-align-done.words.json",
        "selection": "provider-aligned",
        "text_source": "provider",
        "timing_source": "computed",
    }
    done["speakers"] = {
        "key": "transcripts/src/done-provider-diarize-speaker-done.speakers.json",
        "spec_hash": "speaker-done",
        "synced": True,
        "confidence": 1.0,
    }

    candidate = _rec("candidate", media_kind="hls", hosted_url="http://cdn/candidate.m4a")
    candidate["links"] = {"transcript": "http://city/candidate.vtt"}
    candidate["provider_transcript"] = {
        "candidate": {
            "key": "transcripts/src/candidate-provider.vtt",
            "synced": True,
            "format": "vtt",
            "confidence": 0.4,
            "align_spec_hash": "align-candidate",
        },
        "history": [{"key": "transcripts/src/old-provider.vtt", "status": "rejected"}],
    }

    failed_diarize = _rec("failed", media_kind="hls", hosted_url="http://cdn/failed.m4a")
    failed_diarize["links"] = {"transcript": "http://city/failed.vtt"}
    failed_diarize["provider_transcript"] = {
        "known_good": {
            "key": "transcripts/src/failed-provider.vtt",
            "synced": True,
            "format": "vtt",
            "confidence": 0.8,
            "align_spec_hash": "align-failed",
            "align_pipeline_version": "5",
            "diarize_status": "no-speaker-labels",
        }
    }
    failed_diarize["transcript"] = {
        "key": "transcripts/src/failed-provider-align-align-failed.vtt",
        "spec_hash": "align-failed",
        "synced": True,
        "words_key": "transcripts/src/failed-provider-align-align-failed.words.json",
        "selection": "provider-aligned",
        "text_source": "provider",
        "timing_source": "computed",
    }
    failed_diarize["speakers"] = {
        "spec_hash": "speaker-failed",
        "synced": False,
        "error": "no-speaker-labels",
    }

    save_records(
        tmp_path,
        source_key(city),
        {"done": done, "candidate": candidate, "failed": failed_diarize},
    )

    pt = build_status([city], site_config=SITE, state_dir=tmp_path)["backlog"][
        "provider_transcripts"
    ]
    assert pt["fetch"] == {
        "linked": 3,
        "stored": 3,
        "known_good": 2,
        "candidate": 1,
        "history_entries": 1,
        "rollback_candidates": 2,
        "rejected_history": 1,
    }
    assert pt["align"]["work"] == {"done": 2, "queued": 1}
    assert pt["align"]["confidence"]["count"] == 2
    assert pt["align"]["confidence"]["high"] == 1
    assert pt["align"]["confidence"]["medium"] == 1
    assert pt["active_provenance"]["provider-aligned"] == 2
    assert pt["active_text_timing"] == {"provider+computed": 2}
    assert pt["diarize"]["work"] == {"done": 1, "queued": 1}
    assert pt["diarize"]["done"] == 1
    assert pt["diarize"]["errors"] == {"no-speaker-labels": 1}
    assert pt["diarize"]["confidence"] == {
        "count": 1,
        "low": 0,
        "medium": 0,
        "high": 1,
        "min": 1.0,
        "avg": 1.0,
    }


def test_build_status_transcript_quality_metrics(tmp_path):
    from citypods.transcript_quality import save_rollups

    save_rollups(
        tmp_path,
        {
            "version": 1,
            "rows": [
                {
                    "source_key": "src-1",
                    "body_key": "city-council",
                    "body_name": "City Council",
                    "evidence": {
                        "sample-1": {
                            "sample_id": "sample-1",
                            "manual_decision": "a_better",
                            "auto_outcome": "a_better",
                            "l2_used": True,
                            "gold_text": "approved transcript",
                            "gold_source": "both_correct",
                            "blind_mapping": _h15_blind("provider-align", "asr-challenger"),
                            "metrics": {"A": {"auto_score": 0.9}, "B": {"auto_score": 0.4}},
                        },
                        "sample-2": {
                            "sample_id": "sample-2",
                            "manual_decision": "a_better",
                            "auto_outcome": "a_better",
                            "l2_used": False,
                            "blind_mapping": _h15_blind("provider-align", "asr-challenger"),
                            "metrics": {"A": {"auto_score": 0.85}, "B": {"auto_score": 0.35}},
                        },
                    },
                },
                {
                    "source_key": "src-2",
                    "body_key": "planning",
                    "body_name": "Planning Commission",
                    "evidence": {
                        "sample-3": {
                            "sample_id": "sample-3",
                            "auto_outcome": "b_better",
                            "l2_used": True,
                            "gold_text": "reviewer corrected text",
                            "gold_source": "reviewer_correction",
                            "blind_mapping": _h15_blind("provider-align", "asr-challenger"),
                            "metrics": {"A": {"auto_score": 0.2}, "B": {"auto_score": 0.9}},
                        }
                    },
                },
            ],
        },
    )
    (tmp_path / "transcript_quality_log.json").write_text(
        json.dumps(
            {
                "version": 1,
                "events": [
                    {
                        "id": "l1:src-1:ep-1:hash",
                        "kind": "l1_sample",
                        "source_key": "src-1",
                        "body_key": "city-council",
                        "created_at": "2026-07-01T00:00:00+00:00",
                    }
                ],
            }
        )
    )
    (tmp_path / "transcript_quality_calibration_trend.json").write_text(
        json.dumps(
            {
                "version": 1,
                "runs": [
                    {
                        "run_at": "2026-07-10T12:00:00+00:00",
                        "point_count": 7,
                        "wer_vs_auto_score_correlation": 0.81,
                        "l2_coverage_rate": 0.5,
                        "both_correct_accepted": 3,
                        "both_correct_rejected": 1,
                    }
                ],
            }
        )
    )

    tq = build_status([], site_config=SITE, state_dir=tmp_path)["transcript_quality"]

    assert tq["distribution"] == {
        "total": 2,
        "route_mode": {"provider-align": 1, "fresh-asr": 0, "unknown": 1},
        "calibrated": {"true": 1, "false": 1},
    }
    assert tq["gold_summary"] == {
        "total": 2,
        "both_correct": 1,
        "reviewer_correction": 1,
    }
    assert tq["latest_calibration"] == {
        "run_at": "2026-07-10T12:00:00+00:00",
        "point_count": 7,
        "wer_vs_auto_score_correlation": 0.81,
        "l2_coverage_rate": 0.5,
        "both_correct_accepted": 3,
        "both_correct_rejected": 1,
    }
    assert [row["body_name"] for row in tq["rows"]] == ["City Council", "Planning Commission"]
    council, planning = tq["rows"]
    assert council["route_mode"] == "provider-align"
    assert council["calibrated"] is True
    assert council["reviewed"] == 2
    assert council["l2_coverage"] == {"used": 1, "total": 2, "rate": 0.5}
    assert isinstance(council["last_sampled_age_seconds"], float)
    assert council["last_sampled_age_seconds"] > 0
    assert planning["route_mode"] == "unknown"
    assert planning["calibrated"] is False
    assert planning["reviewed"] == 0
    assert planning["l2_coverage"] == {"used": 1, "total": 1, "rate": 1.0}
    assert planning["last_sampled_age_seconds"] is None
    assert [row["body_name"] for row in tq["needs_attention"]] == ["Planning Commission"]


def test_build_status_overlays_persisted_work_manifest_state(tmp_path):
    """Live work-list sidecar state (leases/backoff) survives the derived status manifest."""
    from datetime import UTC, datetime, timedelta

    from citypods.ops.workqueue import WorkItem, save_manifest
    from citypods.records import save_records, source_key

    city = _hls_city("lease-city")
    rec = _rec("needs-tx", media_kind="hls", hosted_url="http://cdn/needs-tx.m4a")
    save_records(tmp_path, source_key(city), {"needs-tx": rec})
    save_manifest(
        tmp_path,
        [
            WorkItem(
                source_key(city),
                "needs-tx",
                "transcript-asr",
                state="running",
                lease_owner="modal:job-1",
                lease_expires=datetime.now(UTC) + timedelta(hours=1),
            )
        ],
    )

    bl = build_status([city], site_config=SITE, state_dir=tmp_path)["backlog"]
    assert bl["by_work_class"]["transcript-asr"]["running"] == 1
    assert "queued" not in bl["by_work_class"]["transcript-asr"]
    assert bl["transcript_backlog"]["total_pending"] == 1
    assert bl["work_manifest"]["source"] == "derived+work.json"
    assert bl["work_manifest"]["overlaid_items"] == 1


def test_build_status_transcript_coverage_from_records(tmp_path):
    """Transcript KPI/backlog counts are derived from source records, not run summaries."""
    from citypods.records import save_records, source_key

    city = _hls_city("tx-city")
    synced = _rec("synced", media_kind="hls", hosted_url="http://cdn/synced.m4a")
    synced["transcript"] = {"key": "transcripts/synced.vtt", "synced": True}
    text = _rec("text", media_kind="hls", hosted_url="http://cdn/text.m4a")
    text["transcript"] = {"key": "transcripts/text.txt", "synced": False}
    missing = _rec("missing", media_kind="hls", hosted_url="http://cdn/missing.m4a")
    no_audio = _rec("no-audio", media_kind="hls", hosted_url=None)
    save_records(
        tmp_path,
        source_key(city),
        {"synced": synced, "text": text, "missing": missing, "no-audio": no_audio},
    )

    status = build_status([city], site_config=SITE, state_dir=tmp_path)
    assert status["kpis"]["transcripts_synced"] == 1
    assert status["kpis"]["transcripts_text"] == 1
    assert status["kpis"]["transcripts_missing"] == 1
    assert status["kpis"]["transcripts_hosted"] == 3
    assert status["kpis"]["transcript_coverage_pct"] == pytest.approx(33.3)
    assert status["backlog"]["transcript_backlog"]["synced"] == 1
    assert status["backlog"]["transcript_backlog"]["text_only"] == 1
    assert status["backlog"]["transcript_backlog"]["total_pending"] == 1


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


def test_status_marks_legacy_audio_stale_when_processing_profile_enabled(tmp_path):
    from citypods.records import save_records, source_key

    city = _hls_city("legacy-profile")
    save_records(
        tmp_path,
        source_key(city),
        {"a": _rec("a", hosted_url="http://cdn/a.m4a", spec_hash="legacy")},
    )
    site = {
        "defaults": {
            "audio_max_kbps": 96,
            "audio_loudness_profile": "ebuR128:-16LUFS",
            "audio_processing_profile": "podcast-speech-v2",
        }
    }

    status = build_status([city], site_config=site, state_dir=tmp_path)

    assert status["feeds_by_feed"][0]["stale"] == 1


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
    assert "AUDIO RUN" in html and "TRANSCRIBE RUN" in html and "DIARIZE RUN" in html
    assert "runSeverity(run, totals)" in html
    # CR2-CP-26: CITY_COLS' Health column key ('health') has no matching row field (only the
    # health_ok/health_warn/health_error histogram counts), so sortRows's plain `a[sort.key]`
    # lookup was always undefined for both sides — a composite fallback makes the sort meaningful.
    assert "function sortValue(r, key)" in html
    assert "health_error||0)*1e6" in html
    assert "action_alert_level" in html
    assert "⚠ warnings" in html
    assert "✗ errors" in html
    m = re.search(r'<script id="status-data" type="application/json">(.*?)</script>', html, re.S)
    assert m, "status-data script tag not found"
    parsed = json.loads(m.group(1))
    assert "kpis" in parsed and "feeds_by_feed" in parsed


def test_status_html_escapes_json_for_script_context():
    payload = '</script><script>alert("x")</script>'
    status = build_status([], site_config=SITE)
    status["config"]["github_repo"] = payload

    html = to_status_html(status)

    m = re.search(r'<script id="status-data" type="application/json">(.*?)</script>', html, re.S)
    assert m
    assert payload not in m.group(1)
    assert json.loads(m.group(1))["config"]["github_repo"] == payload


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


def test_build_status_merges_scoped_run_events(tmp_path):
    """Scoped ASR shards publish unique run_events; status aggregates sibling shards."""
    import json

    events = tmp_path / "run_events"
    events.mkdir()
    for shard, transcribed in (("0/4", 5), ("1/4", 7)):
        payload = {
            "ts": f"2026-06-15T12:0{shard[0]}:00+00:00",
            "schema_version": 2,
            "phase": "enrich",
            "lane": "transcribe",
            "shard": shard,
            "scoped": True,
            "cities": 1,
            "built": 1,
            "skipped": 0,
            "errors": 0,
            "materialized": 0,
            "materialize_encoded": 0,
            "materialize_seconds": 0.0,
            "stages": {
                "transcript": {
                    "ran": transcribed,
                    "encoded": 0,
                    "credited": 0,
                    "aligned": 0,
                    "transcribed": transcribed,
                    "reused": 2,
                    "backlog": 10,
                    "defer_reasons": {"insufficient-budget": transcribed},
                    "seconds": 120.0,
                    "bytes": 0,
                    "errors": 0,
                }
            },
            "github_run_id": "12345",
            "github_run_url": "https://github.com/example/repo/actions/runs/12345",
        }
        (events / f"event-{shard.replace('/', '-')}.json").write_text(json.dumps(payload))

    status = build_status([], site_config=SITE, state_dir=tmp_path)
    latest = status["kpis"]["last_build"]
    assert latest["lane"] == "transcribe"
    assert latest["shards"] == ["0/4", "1/4"]
    assert latest["complete"] is False
    tx = status["backlog"]["stage_totals"]["transcript"]
    assert tx["transcribed"] == 12
    assert tx["ran"] == 12
    assert tx["reused"] == 4
    assert tx["backlog"] == 20
    assert tx["defer_reasons"] == {"insufficient-budget": 12}


def test_scoped_run_merge_aggregates_provider_lease_telemetry():
    from citypods.report import _merge_logical_run_group

    rows = [
        {
            "ts": "2026-06-18T12:00:00+00:00",
            "provider_rate_limit_telemetry": {
                "granicus.com/tenant:fortworthgov": {
                    "direct_fetch_403s": 3,
                    "worker_fallback_attempts": 3,
                    "worker_fallback_successes": 3,
                    "truncations": 1,
                    "lease_acquisitions": 4,
                    "lease_wait_seconds": 12.5,
                    "lease_max_wait_seconds": 8.0,
                    "stale_leases_reaped": 1,
                    "lease_renewals": 2,
                }
            },
        },
        {
            "ts": "2026-06-18T12:01:00+00:00",
            "provider_rate_limit_telemetry": {
                "granicus.com/tenant:fortworthgov": {
                    "direct_fetch_403s": 2,
                    "worker_fallback_attempts": 2,
                    "worker_fallback_successes": 2,
                    "truncations": 0,
                    "lease_acquisitions": 3,
                    "lease_wait_seconds": 7.5,
                    "lease_max_wait_seconds": 9.0,
                    "stale_leases_reaped": 2,
                    "lease_renewals": 4,
                }
            },
        },
    ]

    merged = _merge_logical_run_group(rows)["provider_rate_limit_telemetry"]
    values = merged["granicus.com/tenant:fortworthgov"]
    assert values == {
        "direct_fetch_403s": 5,
        "worker_fallback_attempts": 5,
        "worker_fallback_successes": 5,
        "truncations": 1,
        "lease_acquisitions": 7,
        "lease_wait_seconds": 20.0,
        "lease_max_wait_seconds": 9.0,
        "stale_leases_reaped": 3,
        "lease_renewals": 6,
    }


def test_build_status_reports_latest_run_per_stage(tmp_path):
    """A later ASR-only run must not hide the latest audio-lane telemetry."""
    events = tmp_path / "run_events"
    events.mkdir()

    def write_event(
        name: str, *, ts: str, run_id: str, lane: str, shard: str, stages: dict
    ) -> None:
        payload = {
            "ts": ts,
            "schema_version": 2,
            "phase": "enrich",
            "lane": lane,
            "shard": shard,
            "scoped": True,
            "cities": 1,
            "built": 1,
            "skipped": 0,
            "errors": 0,
            "materialized": stages.get("audio", {}).get("ran", 0),
            "materialize_encoded": stages.get("audio", {}).get("encoded", 0),
            "materialize_seconds": stages.get("audio", {}).get("seconds", 0.0),
            "stages": stages,
            "github_run_id": run_id,
            "github_run_url": f"https://github.com/example/repo/actions/runs/{run_id}",
        }
        (events / name).write_text(json.dumps(payload))

    write_event(
        "audio-0.json",
        ts="2026-06-15T10:00:00+00:00",
        run_id="audio1",
        lane="audio",
        shard="0/2",
        stages={
            "audio": {
                "ran": 2,
                "encoded": 1,
                "credited": 1,
                "aligned": 0,
                "transcribed": 0,
                "reused": 3,
                "backlog": 4,
                "seconds": 50.0,
                "bytes": 100,
                "errors": 0,
            }
        },
    )
    write_event(
        "audio-1.json",
        ts="2026-06-15T10:05:00+00:00",
        run_id="audio1",
        lane="audio",
        shard="1/2",
        stages={
            "audio": {
                "ran": 5,
                "encoded": 5,
                "credited": 0,
                "aligned": 0,
                "transcribed": 0,
                "reused": 7,
                "backlog": 8,
                "seconds": 70.0,
                "bytes": 200,
                "errors": 1,
            }
        },
    )
    write_event(
        "asr-0.json",
        ts="2026-06-15T12:00:00+00:00",
        run_id="asr1",
        lane="transcribe",
        shard=None,
        stages={
            "transcript": {
                "ran": 11,
                "encoded": 0,
                "credited": 0,
                "aligned": 0,
                "transcribed": 11,
                "reused": 13,
                "backlog": 17,
                "seconds": 90.0,
                "bytes": 0,
                "errors": 0,
            }
        },
    )

    status = build_status([], site_config=SITE, state_dir=tmp_path)

    # Backwards-compatible latest-overall telemetry remains ASR-shaped.
    assert status["kpis"]["last_build"]["lane"] == "transcribe"
    assert set(status["backlog"]["stage_totals"]) == {"transcript"}

    # The new per-stage telemetry keeps the older audio lane visible and aggregates its shards.
    audio_run = status["backlog"]["stage_runs"]["audio"]
    assert audio_run["lane"] == "audio"
    assert audio_run["shards"] == ["0/2", "1/2"]
    assert audio_run["totals"]["ran"] == 7
    assert audio_run["totals"]["encoded"] == 6
    assert audio_run["totals"]["bytes"] == 300
    assert audio_run["totals"]["errors"] == 1

    tx_run = status["backlog"]["stage_runs"]["transcript"]
    assert tx_run["lane"] == "transcribe"
    assert tx_run["totals"]["transcribed"] == 11


def test_build_status_skips_partially_reported_shard_run(tmp_path):
    """A sharded run whose slow shards haven't pushed yet must not show as the audio tally.

    The audio lane shards by source_key (4-way). Fast shards (little/no new work) can push
    their run_events within minutes while slow shards (heavy encoding) take hours (#H6b). A
    status build landing in that window must keep reporting the previous *fully* reported run
    rather than passing off the fast shards' partial totals as the latest run's final tally.
    """
    events = tmp_path / "run_events"
    events.mkdir()

    def write_event(name: str, *, ts: str, run_id: str, shard: str, audio: dict) -> None:
        payload = {
            "ts": ts,
            "schema_version": 2,
            "phase": "enrich",
            "lane": "audio",
            "shard": shard,
            "scoped": True,
            "cities": 1,
            "built": 1,
            "skipped": 0,
            "errors": 0,
            "stages": {"audio": audio},
            "github_run_id": run_id,
            "github_run_url": f"https://github.com/example/repo/actions/runs/{run_id}",
        }
        (events / name).write_text(json.dumps(payload))

    def audio_totals(*, ran, encoded, credited, reused, backlog, errors) -> dict:
        return {
            "ran": ran,
            "encoded": encoded,
            "credited": credited,
            "aligned": 0,
            "transcribed": 0,
            "reused": reused,
            "backlog": backlog,
            "seconds": 0.0,
            "bytes": 0,
            "errors": errors,
        }

    # An earlier run where all 4 shards reported in.
    for shard, totals in (
        ("0/4", audio_totals(ran=132, encoded=7, credited=125, reused=1934, backlog=715, errors=7)),
        ("1/4", audio_totals(ran=1, encoded=0, credited=1, reused=81, backlog=238, errors=4)),
        ("2/4", audio_totals(ran=0, encoded=0, credited=0, reused=107, backlog=111, errors=3)),
        ("3/4", audio_totals(ran=56, encoded=56, credited=0, reused=887, backlog=598, errors=6)),
    ):
        write_event(
            f"complete-{shard.replace('/', '-')}.json",
            ts=f"2026-06-17T05:3{shard[0]}:00+00:00",
            run_id="run-complete",
            shard=shard,
            audio=totals,
        )

    # A later run whose two fast shards have reported but whose two slow shards (still
    # encoding) have not pushed their run_events yet.
    for shard, totals in (
        ("1/4", audio_totals(ran=1, encoded=0, credited=1, reused=81, backlog=238, errors=4)),
        ("2/4", audio_totals(ran=0, encoded=0, credited=0, reused=107, backlog=111, errors=3)),
    ):
        write_event(
            f"partial-{shard.replace('/', '-')}.json",
            ts=f"2026-06-17T12:3{shard[0]}:00+00:00",
            run_id="run-partial",
            shard=shard,
            audio=totals,
        )

    status = build_status([], site_config=SITE, state_dir=tmp_path)

    audio_run = status["backlog"]["stage_runs"]["audio"]
    assert audio_run["shards"] == ["0/4", "1/4", "2/4", "3/4"]
    assert audio_run["totals"]["credited"] == 126
    assert audio_run["totals"]["reused"] == 3009
    assert audio_run["totals"]["backlog"] == 1662
    assert audio_run["totals"]["errors"] == 20


def test_latest_build_kpi_skips_partial_logical_run(tmp_path):
    events = tmp_path / "run_events"
    events.mkdir()

    def write(name: str, *, run_id: str, shard: str, ts: str, transcribed: int) -> None:
        (events / name).write_text(
            json.dumps(
                {
                    "ts": ts,
                    "phase": "enrich",
                    "lane": "transcribe",
                    "shard": shard,
                    "scoped": True,
                    "cities": 1,
                    "built": 1,
                    "skipped": 0,
                    "errors": 0,
                    "stages": {
                        "transcript": {
                            "ran": transcribed,
                            "transcribed": transcribed,
                            "reused": 0,
                            "backlog": 0,
                            "seconds": 1,
                            "bytes": 0,
                            "errors": 0,
                        }
                    },
                    "github_run_id": run_id,
                }
            )
        )

    for shard in ("0/2", "1/2"):
        write(
            f"complete-{shard[0]}.json",
            run_id="complete",
            shard=shard,
            ts=f"2026-06-20T10:0{shard[0]}:00+00:00",
            transcribed=3,
        )
    write(
        "partial.json",
        run_id="partial",
        shard="0/2",
        ts="2026-06-20T12:00:00+00:00",
        transcribed=99,
    )

    status = build_status([], site_config=SITE, state_dir=tmp_path)

    latest = status["kpis"]["last_build"]
    assert latest["github_run_id"] == "complete"
    assert latest["shards"] == ["0/2", "1/2"]
    assert latest["complete"] is True
    assert status["backlog"]["stage_totals"]["transcript"]["transcribed"] == 6


def test_build_status_skips_partial_shard_run_for_every_stage_row(tmp_path):
    """The completeness gate covers every stage name a shard reports, not just ``audio``.

    A single enrich shard event reports several stages at once (audio, transcript, chapters,
    links, remap, timeline). The "Latest run stage activity" table renders one row per stage
    name, all sourced from the same ``backlog.stage_runs`` dict, so a partially-reported run
    must be skipped identically for every one of those rows.
    """
    events = tmp_path / "run_events"
    events.mkdir()
    stage_names = ("audio", "transcript", "chapters", "links", "remap", "timeline")

    def write_event(name: str, *, ts: str, run_id: str, shard: str, credited: int) -> None:
        stage_totals = {
            "ran": 1,
            "encoded": 0,
            "credited": credited,
            "aligned": 0,
            "transcribed": 0,
            "reused": 1,
            "backlog": 1,
            "seconds": 0.0,
            "bytes": 0,
            "errors": 0,
        }
        payload = {
            "ts": ts,
            "schema_version": 2,
            "phase": "enrich",
            "lane": "audio",
            "shard": shard,
            "scoped": True,
            "cities": 1,
            "built": 1,
            "skipped": 0,
            "errors": 0,
            "stages": {stage: dict(stage_totals) for stage in stage_names},
            "github_run_id": run_id,
            "github_run_url": f"https://github.com/example/repo/actions/runs/{run_id}",
        }
        (events / name).write_text(json.dumps(payload))

    for shard in ("0/4", "1/4", "2/4", "3/4"):
        write_event(
            f"complete-{shard.replace('/', '-')}.json",
            ts=f"2026-06-17T05:3{shard[0]}:00+00:00",
            run_id="run-complete",
            shard=shard,
            credited=1,
        )

    # A later run whose two fast shards have reported but two slow shards have not.
    for shard in ("1/4", "2/4"):
        write_event(
            f"partial-{shard.replace('/', '-')}.json",
            ts=f"2026-06-17T12:3{shard[0]}:00+00:00",
            run_id="run-partial",
            shard=shard,
            credited=99,
        )

    status = build_status([], site_config=SITE, state_dir=tmp_path)
    stage_runs = status["backlog"]["stage_runs"]

    for stage in stage_names:
        run = stage_runs[stage]
        assert run["shards"] == ["0/4", "1/4", "2/4", "3/4"], stage
        assert run["totals"]["credited"] == 4, stage


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
            sources=None,
            loudness_profile=None,
            processing_profile=None,
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


# ---------------------------------------------------------------------------
# H2: projection / per-run telemetry tests
# ---------------------------------------------------------------------------


def test_per_run_cap_none_when_key_absent():
    """build_report uses per_run_cap=None (wall-clock bound) when materialize_budget_per_run
    is not present in site config defaults."""
    site_no_cap = {"defaults": {"max_episodes": 50, "audio_max_kbps": 96}}
    rep = build_report(_cities(), site_config=site_no_cap)
    assert rep["current"]["inputs"]["per_run_cap"] is None
    assert rep["current"]["cap_is_bottleneck"] is False


def test_to_markdown_wall_clock_framing_no_cap():
    """to_markdown with no per-run cap should not mention a bottleneck warning."""
    site_no_cap = {"defaults": {"max_episodes": 50, "audio_max_kbps": 96}}
    md = to_markdown(build_report(_cities(), site_config=site_no_cap))
    assert "bottleneck" not in md.lower()
    assert "materialize_budget_per_run" not in md


def test_to_markdown_bottleneck_message_no_legacy_key():
    """When there is a bottleneck the suggestion says to delete the cap, not set a new value."""
    md = to_markdown(build_report(_cities(), site_config=SITE))
    assert "bottleneck" in md.lower()
    # Old message suggested setting materialize_budget_per_run = X; new message says delete it.
    assert "= " not in md.split("bottleneck")[1].split("\n")[0] or "delete" in md.lower()
    assert "delete" in md.lower() or "Remove the cap" in md


def test_feed_row_hours_hosted_bytes_fallback(tmp_path):
    """hours_hosted uses bytes-based estimate for records with no duration_served (Swagit-style)."""
    from citypods.records import save_records, source_key

    city = _hls_city("swagit-city")
    max_kbps = 96
    # 96 kbps × 3600s = 43_200_000 bytes for 1 hour
    expected_bytes = max_kbps * 1000 * 3600 // 8  # 43_200_000
    records = {
        "ep1": _rec(
            "ep1",
            hosted_url="http://cdn/ep1.m4a",
            spec_hash="legacy",
            duration=0,  # no provider duration (Swagit never provides it)
            bytes_val=expected_bytes,
        ),
    }
    # Ensure duration_served is also absent
    records["ep1"]["audio"]["duration_served"] = None

    save_records(tmp_path, source_key(city), records)
    status = build_status([city], site_config=SITE, state_dir=tmp_path)
    assert status["kpis"]["hours_hosted"] == pytest.approx(1.0, abs=0.01)


def test_feed_row_hours_hosted_uses_canonical_served_duration(tmp_path):
    from citypods.records import save_records, source_key

    city = _hls_city("canon-city")
    records = {
        "ep1": _rec(
            "ep1",
            hosted_url="http://cdn/ep1.m4a",
            spec_hash="canon",
            duration=None,
            source_duration_seconds=7200.0,
            served_duration_seconds=5400.0,
        )
    }

    save_records(tmp_path, source_key(city), records)
    status = build_status([city], site_config=SITE, state_dir=tmp_path)
    assert status["kpis"]["hours_hosted"] == pytest.approx(1.5, abs=0.01)


def test_run_history_telemetry_roundtrip(tmp_path):
    """_record_run_history writes new telemetry fields; build_status exposes them."""
    import json

    entry = {
        "ts": "2026-06-11T12:00:00+00:00",
        "built": 3,
        "skipped": 10,
        "errors": 0,
        "materialized": 3,
        "materialize_encoded": 3,
        "materialize_seconds": 240.0,
        "stages": {},
        "github_run_id": None,
        "github_run_url": None,
        "peak_load_per_cpu": 0.75,
        "min_mem_avail_mb": 4096.0,
        "window_used_pct": 62.5,
        "gate_wait_seconds": 12.3,
    }
    path = tmp_path / "run_history.jsonl"
    path.write_text(json.dumps(entry) + "\n")

    status = build_status([], site_config=SITE, state_dir=tmp_path)
    rec = status["run_history"][0]
    assert rec["peak_load_per_cpu"] == pytest.approx(0.75)
    assert rec["min_mem_avail_mb"] == pytest.approx(4096.0)
    assert rec["window_used_pct"] == pytest.approx(62.5)
    assert rec["gate_wait_seconds"] == pytest.approx(12.3)


def test_projection_calibration_uses_encoded_not_materialized(tmp_path):
    """measured_inputs uses materialize_encoded (not materialized) so cheap re-credits
    do not dilute the sec/ep estimate."""
    import json

    from citypods.projection import measured_inputs

    # 1 run: 100 materialized total, but only 10 were actual encodes — 90 were re-credits.
    entry = {
        "ts": "2026-06-11T00:00:00+00:00",
        "materialized": 100,
        "materialize_encoded": 10,
        "materialize_seconds": 100.0,
    }
    path = tmp_path / "run_history.jsonl"
    path.write_text(json.dumps(entry) + "\n")
    history = [json.loads(path.read_text())]

    inputs = measured_inputs([], run_history=history)
    # 100s / 10 encodes = 10.0 sec/ep; using materialized=100 would give 1.0
    assert inputs.sec_per_ep == pytest.approx(10.0)


def test_deferred_work_card_surfaces_defer_reasons_and_backlog(tmp_path):
    """The admin/status "Deferred Work" card (added for the tag-llm-oversized signal, but
    generic across any stage's defer_reasons) must have real data to render: build_status()'s
    run_history/backlog.stage_runs carry defer_reasons through, and to_status_html() embeds the
    new card's markup/JS alongside the underlying JSON."""
    import json

    entries = [
        {
            "ts": "2026-07-27T10:00:00+00:00",
            "built": 1,
            "skipped": 3,
            "errors": 0,
            "materialized": 1,
            "materialize_encoded": 0,
            "materialize_seconds": 1.0,
            "stages": {
                "tags": {
                    "ran": 1,
                    "reused": 4,
                    "backlog": 3,
                    "defer_reasons": {"tag-llm-oversized": 2, "tag-llm-dispatch": 1},
                    "seconds": 5.0,
                    "bytes": 0,
                    "errors": 0,
                }
            },
            "github_run_id": "1",
            "github_run_url": "https://github.com/example/repo/actions/runs/1",
        },
        {
            "ts": "2026-07-27T11:00:00+00:00",
            "built": 1,
            "skipped": 1,
            "errors": 0,
            "materialized": 1,
            "materialize_encoded": 0,
            "materialize_seconds": 1.0,
            "stages": {
                "tags": {
                    "ran": 3,
                    "reused": 4,
                    "backlog": 1,
                    "defer_reasons": {"tag-llm-oversized": 1},
                    "seconds": 5.0,
                    "bytes": 0,
                    "errors": 0,
                }
            },
            "github_run_id": "2",
            "github_run_url": "https://github.com/example/repo/actions/runs/2",
        },
    ]
    path = tmp_path / "run_history.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    status = build_status([], site_config=SITE, state_dir=tmp_path)
    assert len(status["run_history"]) == 2
    latest_tags = status["backlog"]["stage_runs"]["tags"]["totals"]
    assert latest_tags["backlog"] == 1
    assert latest_tags["defer_reasons"] == {"tag-llm-oversized": 1}

    html = to_status_html(status)
    assert "Deferred Work" in html
    assert "function renderDeferred()" in html
    assert "function renderBurndownChart(points)" in html
    m = re.search(r'<script id="status-data" type="application/json">(.*?)</script>', html, re.S)
    assert m
    parsed = json.loads(m.group(1))
    tag_reasons = {
        reason
        for row in parsed["run_history"]
        for reason in (row.get("stages", {}).get("tags", {}).get("defer_reasons") or {})
    }
    assert "tag-llm-oversized" in tag_reasons
