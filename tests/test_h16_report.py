from __future__ import annotations

import json

from citypods.h16_report import (
    _configured_ceilings,
    build_h16_report,
    to_markdown,
    write_h16_report,
)


def _ceiling_config(tmp_path, *, slots: int, declared: str = "") -> object:
    cfg = tmp_path / "site.yml"
    cfg.write_text(
        "provider_rate_limits:\n  granicus.com: 1\n"
        f"provider_distributed_leases:\n  granicus.com:\n    slots: {slots}\n" + declared
    )
    return cfg


def test_concurrency_ceiling_defaults_to_one_two(tmp_path):
    # No declared ceiling → the historical 1-local / 2-distributed envelope is expected.
    result = _configured_ceilings(_ceiling_config(tmp_path, slots=2))
    assert result["status"] == "pass"
    assert result["expected"] == {"process_local": 1, "distributed": 2}


def test_concurrency_ceiling_accepts_declared_intent(tmp_path):
    # A deliberate bump to slots=4 passes when the declared ceiling is updated in lockstep.
    cfg = _ceiling_config(
        tmp_path,
        slots=4,
        declared="provider_audio_concurrency_ceiling:\n  process_local: 1\n  distributed: 4\n",
    )
    result = _configured_ceilings(cfg)
    assert result["status"] == "pass"
    assert result["distributed"] == 4
    assert result["expected"] == {"process_local": 1, "distributed": 4}


def test_concurrency_ceiling_flags_drift_from_declared(tmp_path):
    # Operative slots=4 but the declared intent is still 2 → caught (a typo in one of the knobs).
    cfg = _ceiling_config(
        tmp_path,
        slots=4,
        declared="provider_audio_concurrency_ceiling:\n  process_local: 1\n  distributed: 2\n",
    )
    assert _configured_ceilings(cfg)["status"] == "fail"


def _write_config(path) -> None:
    path.write_text(
        """
provider_rate_limits:
  granicus.com: 1
provider_distributed_leases:
  granicus.com:
    slots: 2
"""
    )


def _write_event(
    root,
    *,
    shard: str,
    telemetry: dict,
    identity: dict | None = None,
    availability: dict | None = None,
    stage_errors: int = 0,
) -> None:
    event = {
        "ts": f"2026-06-21T0{shard[0]}:00:00+00:00",
        "phase": "enrich",
        "lane": "audio",
        "shard": shard,
        "scoped": True,
        "github_run_id": "123",
        "provider_rate_limit_telemetry": telemetry,
        "provider_errors": {},
        "stages": {"audio": {"errors": stage_errors}},
    }
    if identity is not None:
        event["h16_identity"] = identity
    if availability is not None:
        event["h16_availability"] = availability
    (root / f"event-{shard[0]}.json").write_text(json.dumps(event))


def test_report_passes_direct_and_worker_recovery_with_identity(tmp_path):
    config = tmp_path / "site.yml"
    _write_config(config)
    tenant = "granicus.com/tenant:fortworthgov"
    for shard in ("0/4", "1/4", "2/4", "3/4"):
        telemetry = {}
        if shard == "0/4":
            telemetry = {
                tenant: {
                    "direct_fetch_successes": 1,
                    "direct_fetch_403s": 1,
                    "worker_fallback_attempts": 1,
                    "worker_fallback_successes": 1,
                },
                "granicus.com": {
                    "lease_acquisitions": 2,
                    "lease_wait_seconds": 4.5,
                    "lease_max_wait_seconds": 4.0,
                },
            }
        _write_event(
            tmp_path,
            shard=shard,
            telemetry=telemetry,
            identity={"checked": 1, "mismatches": 0},
            stage_errors=2 if shard == "0/4" else 0,
        )
    for shard in range(4):
        (tmp_path / f"secret-scan-{shard}.json").write_text(
            json.dumps(
                {
                    "kind": "h16_log_scan",
                    "status": "pass",
                    "finding_count": 0,
                    "samples": [],
                    "scanned_files": 1,
                }
            )
        )

    report = build_h16_report(
        tmp_path,
        run_id="123",
        site_config_path=config,
        expected_shards=4,
    )

    assert report["status"] == "pass"
    assert report["complete"] is True
    assert report["criteria"]["transport"]["status"] == "pass"
    assert report["criteria"]["identity"]["checked"] == 4
    assert report["criteria"]["identity"]["artifact_checked"] == 0
    assert report["criteria"]["secret_scan"]["status"] == "pass"
    assert report["tenants"]["fortworthgov"]["worker_successes"] == 1
    assert report["lease"]["acquisitions"] == 2
    assert report["run_stage_errors"] == 2
    assert "Worker OK/attempts" in to_markdown(report)


def test_report_fails_worker_errors_truncations_and_signed_query(tmp_path):
    config = tmp_path / "site.yml"
    _write_config(config)
    tenant = "granicus.com/tenant:dentoncounty"
    for shard in ("0/4", "1/4", "2/4", "3/4"):
        _write_event(
            tmp_path,
            shard=shard,
            telemetry={
                tenant: {
                    "direct_fetch_403s": 1,
                    "worker_fallback_attempts": 1,
                    "worker_fallback_failures": 1,
                    "truncations": 1,
                }
            }
            if shard == "0/4"
            else {},
            identity={
                "checked": 1,
                "mismatches": 1 if shard == "0/4" else 0,
                "artifact_checked": 1,
                "mismatch_categories": {"audio_key": 1} if shard == "0/4" else {},
            },
        )
    (tmp_path / "audio-0.log").write_text(
        "https://example.test/a.mp4?X-Amz-Credential=abc&X-Amz-Signature=secret\n"
    )
    for shard in range(1, 4):
        (tmp_path / f"audio-{shard}.log").write_text("clean\n")

    report = build_h16_report(
        tmp_path,
        run_id="123",
        site_config_path=config,
        expected_shards=4,
    )

    assert report["status"] == "fail"
    assert report["criteria"]["transport"]["status"] == "fail"
    assert report["criteria"]["identity"]["status"] == "fail"
    assert report["criteria"]["identity"]["artifact_checked"] == 4
    assert report["criteria"]["identity"]["mismatch_categories"] == {"audio_key": 1}
    assert report["criteria"]["secret_scan"]["finding_count"] == 1
    assert report["criteria"]["secret_scan"]["samples"][0]["category"] == "signed-query"


def test_report_is_insufficient_without_complete_shards_or_identity(tmp_path):
    config = tmp_path / "site.yml"
    _write_config(config)
    _write_event(
        tmp_path,
        shard="0/4",
        telemetry={
            "granicus.com/tenant:arlingtontx": {
                "direct_fetch_successes": 1,
            }
        },
    )

    report = build_h16_report(
        tmp_path,
        run_id="123",
        site_config_path=config,
        expected_shards=4,
    )

    assert report["status"] == "insufficient_activity"
    assert report["complete"] is False
    assert report["criteria"]["identity"]["status"] == "not_reported"


def test_report_uses_latest_event_per_shard_on_workflow_rerun(tmp_path):
    config = tmp_path / "site.yml"
    _write_config(config)
    _write_event(
        tmp_path,
        shard="0/4",
        telemetry={
            "granicus.com/tenant:arlingtontx": {
                "direct_fetch_successes": 1,
            }
        },
    )
    old_event = json.loads((tmp_path / "event-0.json").read_text())
    old_event["ts"] = "2026-06-20T00:00:00+00:00"
    old_event["provider_rate_limit_telemetry"]["granicus.com/tenant:arlingtontx"][
        "direct_fetch_successes"
    ] = 99
    (tmp_path / "prior-attempt.json").write_text(json.dumps(old_event))

    report = build_h16_report(
        tmp_path,
        run_id="123",
        site_config_path=config,
        expected_shards=4,
    )

    assert report["shards"] == ["0/4"]
    assert report["tenants"]["arlingtontx"]["direct_successes"] == 1


def test_report_surfaces_availability_census_without_affecting_verdict(tmp_path):
    # H16 PR3: availability is informational observability — it is summed across shards and shown,
    # but never flips the transport/identity/secret verdict.
    config = tmp_path / "site.yml"
    _write_config(config)
    tenant = "granicus.com/tenant:fortworthgov"
    for shard in ("0/4", "1/4", "2/4", "3/4"):
        telemetry = (
            {
                tenant: {
                    "direct_fetch_successes": 1,
                    "worker_fallback_attempts": 0,
                }
            }
            if shard == "0/4"
            else {}
        )
        _write_event(
            tmp_path,
            shard=shard,
            telemetry=telemetry,
            identity={"checked": 1, "mismatches": 0},
            availability={
                "classified": 3,
                "withheld": 1,
                "by_state": {"available": 2, "confirmed_empty": 1},
            },
        )
    for shard in range(4):
        (tmp_path / f"secret-scan-{shard}.json").write_text(
            json.dumps(
                {"kind": "h16_log_scan", "status": "pass", "finding_count": 0, "scanned_files": 1}
            )
        )

    report = build_h16_report(tmp_path, run_id="123", site_config_path=config, expected_shards=4)

    assert report["status"] == "pass"  # availability did not change the verdict
    assert report["availability"]["withheld"] == 4  # 1 per shard, summed
    assert report["availability"]["by_state"]["confirmed_empty"] == 4
    assert "Media availability" in to_markdown(report)


def test_write_report_emits_json_and_markdown(tmp_path):
    output = tmp_path / "out"
    report = {
        "github_run_id": "123",
        "status": "insufficient_activity",
        "shards": [],
        "expected_shards": ["0/1"],
        "tenants": {},
        "criteria": {
            "transport": {"status": "insufficient_activity"},
            "identity": {"status": "not_reported", "checked": 0, "mismatches": 0},
            "secret_scan": {"status": "pass", "finding_count": 0},
            "concurrency_ceiling": {
                "status": "pass",
                "process_local": 1,
                "distributed": 2,
            },
        },
        "lease": {
            "acquisitions": 0,
            "wait_seconds": 0.0,
            "stale_reaped": 0,
        },
        "run_stage_errors": 0,
        "non_granicus_provider_errors": {},
    }

    write_h16_report(report, output)

    assert json.loads((output / "h16-report.json").read_text())["github_run_id"] == "123"
    assert "insufficient_activity" in (output / "h16-report.md").read_text()
