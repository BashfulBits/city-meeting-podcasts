"""GH#353 production acceptance report for one sharded Audio workflow run."""

from __future__ import annotations

import json
import re
from pathlib import Path

REPORT_SCHEMA_VERSION = 1
_TENANT_PREFIX = "granicus.com/tenant:"
_SECRET_PATTERNS = {
    "signed-query": re.compile(
        r"(?i)[?&](?:x-amz-(?:credential|security-token|signature)|"
        r"signature|credential|token|key)="
    ),
    "bearer-token": re.compile(r"(?i)authorization:\s*bearer\s+(?!\*{3})(?!<redacted>)\S+"),
    "worker-endpoint": re.compile(r"(?i)https://[^\s\"']+(?:workers\.dev|/v1/archive/)"),
}


def _load_events(input_dir: Path, run_id: str) -> list[dict]:
    events_by_shard: dict[str, dict] = {}
    for path in sorted(input_dir.rglob("*.json")):
        try:
            row = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if (
            str(row.get("github_run_id") or "") == str(run_id)
            and row.get("phase") == "enrich"
            and row.get("lane") == "audio"
        ):
            shard = str(row.get("shard") or "")
            previous = events_by_shard.get(shard)
            if previous is None or str(row.get("ts") or "") > str(previous.get("ts") or ""):
                events_by_shard[shard] = row
    return list(events_by_shard.values())


def scan_h16_logs(input_dir: Path) -> dict:
    findings: list[dict[str, str | int]] = []
    scanned_files = 0
    paths = [input_dir] if input_dir.is_file() else sorted(input_dir.rglob("*.log"))
    for path in paths:
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        scanned_files += 1
        for line_number, line in enumerate(lines, 1):
            for category, pattern in _SECRET_PATTERNS.items():
                if pattern.search(line):
                    findings.append(
                        {
                            "category": category,
                            "file": path.name
                            if input_dir.is_file()
                            else str(path.relative_to(input_dir)),
                            "line": line_number,
                        }
                    )
    return {
        "kind": "h16_log_scan",
        "status": "pass" if not findings else "fail",
        "finding_count": len(findings),
        "samples": findings[:10],
        "scanned_files": scanned_files,
    }


def _scan_secrets(input_dir: Path, expected_shards: int) -> dict:
    manifests = []
    for path in sorted(input_dir.rglob("*.json")):
        try:
            row = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if row.get("kind") == "h16_log_scan":
            manifests.append(row)
    if not manifests:
        result = scan_h16_logs(input_dir)
        if result["status"] == "pass" and result["scanned_files"] < expected_shards:
            result["status"] = "insufficient_activity"
        return result
    findings = [sample for manifest in manifests for sample in (manifest.get("samples") or [])][:10]
    count = sum(int(manifest.get("finding_count", 0)) for manifest in manifests)
    scanned_files = sum(int(manifest.get("scanned_files", 0)) for manifest in manifests)
    return {
        "status": (
            "fail"
            if count
            else ("pass" if scanned_files >= expected_shards else "insufficient_activity")
        ),
        "finding_count": count,
        "samples": findings,
        "scanned_files": scanned_files,
    }


def _configured_ceilings(site_config_path: Path) -> dict:
    import yaml

    config = yaml.safe_load(site_config_path.read_text()) or {}
    local = (config.get("provider_rate_limits") or {}).get("granicus.com")
    distributed = ((config.get("provider_distributed_leases") or {}).get("granicus.com") or {}).get(
        "slots"
    )
    expected = local == 1 and distributed == 2
    return {
        "status": "pass" if expected else "fail",
        "process_local": local,
        "distributed": distributed,
        "expected": {"process_local": 1, "distributed": 2},
    }


def _sum_telemetry(events: list[dict]) -> dict[str, dict[str, int | float]]:
    merged: dict[str, dict[str, int | float]] = {}
    for event in events:
        for scope, values in (event.get("provider_rate_limit_telemetry") or {}).items():
            target = merged.setdefault(scope, {})
            for key, value in (values or {}).items():
                if key == "lease_max_wait_seconds":
                    target[key] = max(float(target.get(key, 0.0)), float(value or 0.0))
                elif key.endswith("_seconds"):
                    target[key] = round(float(target.get(key, 0.0)) + float(value or 0.0), 1)
                else:
                    target[key] = int(target.get(key, 0)) + int(value or 0)
    return merged


def _identity_result(events: list[dict]) -> dict:
    rows = [event.get("h16_identity") for event in events if event.get("h16_identity")]
    if not rows:
        return {"status": "not_reported", "checked": 0, "mismatches": 0}
    checked = sum(int(row.get("checked", 0)) for row in rows)
    mismatches = sum(int(row.get("mismatches", 0)) for row in rows)
    return {
        "status": "pass" if checked > 0 and mismatches == 0 else "fail",
        "checked": checked,
        "mismatches": mismatches,
    }


def _tenant_results(telemetry: dict[str, dict[str, int | float]]) -> dict[str, dict]:
    tenants: dict[str, dict] = {}
    for scope, values in sorted(telemetry.items()):
        if not scope.startswith(_TENANT_PREFIX):
            continue
        direct_successes = int(values.get("direct_fetch_successes", 0))
        direct_403s = int(values.get("direct_fetch_403s", 0))
        attempts = int(values.get("worker_fallback_attempts", 0))
        successes = int(values.get("worker_fallback_successes", 0))
        failures = int(values.get("worker_fallback_failures", 0))
        trips = int(values.get("circuit_trips", 0))
        deferred = int(values.get("circuit_deferred", 0))
        truncations = int(values.get("truncations", 0))
        activity = direct_successes + direct_403s + attempts
        status = "insufficient_activity"
        if activity:
            status = (
                "pass"
                if failures == 0
                and attempts == successes
                and trips == 0
                and deferred == 0
                and truncations == 0
                else "fail"
            )
        tenants[scope.removeprefix(_TENANT_PREFIX)] = {
            "status": status,
            "direct_successes": direct_successes,
            "direct_403s": direct_403s,
            "worker_attempts": attempts,
            "worker_successes": successes,
            "worker_failures": failures,
            "circuit_trips": trips,
            "circuit_deferred": deferred,
            "recovery_probes": int(values.get("recovery_probes", 0)),
            "recoveries": int(values.get("recoveries", 0)),
            "truncations": truncations,
        }
    return tenants


def build_h16_report(
    input_dir: Path,
    *,
    run_id: str,
    site_config_path: Path,
    expected_shards: int = 4,
) -> dict:
    events = _load_events(input_dir, run_id)
    shards = sorted(str(event.get("shard")) for event in events if event.get("shard"))
    expected = [f"{index}/{expected_shards}" for index in range(expected_shards)]
    complete = shards == expected
    telemetry = _sum_telemetry(events)
    tenants = _tenant_results(telemetry)
    active_tenants = [row for row in tenants.values() if row["status"] != "insufficient_activity"]
    if not complete or not active_tenants:
        transport_status = "insufficient_activity"
    elif any(row["status"] == "fail" for row in active_tenants):
        transport_status = "fail"
    else:
        transport_status = "pass"

    identity = _identity_result(events)
    secret_scan = _scan_secrets(input_dir, expected_shards)
    ceilings = _configured_ceilings(site_config_path)
    criteria = {
        "transport": {"status": transport_status},
        "identity": identity,
        "secret_scan": secret_scan,
        "concurrency_ceiling": ceilings,
    }
    statuses = {criterion["status"] for criterion in criteria.values()}
    if "fail" in statuses:
        status = "fail"
    elif "insufficient_activity" in statuses or "not_reported" in statuses:
        status = "insufficient_activity"
    else:
        status = "pass"

    domain = telemetry.get("granicus.com", {})
    non_granicus_errors: dict[str, int] = {}
    for event in events:
        for provider, count in (event.get("provider_errors") or {}).items():
            if provider != "granicus":
                non_granicus_errors[provider] = non_granicus_errors.get(provider, 0) + int(
                    count or 0
                )
    stage_errors = sum(
        int(stage.get("errors", 0))
        for event in events
        for stage in (event.get("stages") or {}).values()
    )
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "github_run_id": str(run_id),
        "status": status,
        "complete": complete,
        "shards": shards,
        "expected_shards": expected,
        "criteria": criteria,
        "tenants": tenants,
        "lease": {
            "acquisitions": int(domain.get("lease_acquisitions", 0)),
            "wait_seconds": float(domain.get("lease_wait_seconds", 0.0)),
            "max_wait_seconds": float(domain.get("lease_max_wait_seconds", 0.0)),
            "stale_reaped": int(domain.get("stale_leases_reaped", 0)),
            "renewals": int(domain.get("lease_renewals", 0)),
        },
        "run_stage_errors": stage_errors,
        "non_granicus_provider_errors": dict(sorted(non_granicus_errors.items())),
    }


def to_markdown(report: dict) -> str:
    lines = [
        "## GH #353 — H16 Audio acceptance",
        "",
        f"- **Run:** `{report['github_run_id']}`",
        f"- **Verdict:** **{report['status']}**",
        f"- **Shard evidence:** {len(report['shards'])}/{len(report['expected_shards'])}",
        "",
        "| Tenant | Verdict | Direct OK | Direct 403 | Worker OK/attempts | Failures | "
        "Trips | Deferred | Truncations |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    if report["tenants"]:
        for tenant, row in report["tenants"].items():
            lines.append(
                f"| `{tenant}` | {row['status']} | {row['direct_successes']} | "
                f"{row['direct_403s']} | {row['worker_successes']}/{row['worker_attempts']} | "
                f"{row['worker_failures']} | {row['circuit_trips']} | "
                f"{row['circuit_deferred']} | {row['truncations']} |"
            )
    else:
        lines.append("| _none observed_ | insufficient_activity | 0 | 0 | 0/0 | 0 | 0 | 0 | 0 |")

    criteria = report["criteria"]
    ceiling = criteria["concurrency_ceiling"]
    lease = report["lease"]
    lines.extend(
        [
            "",
            "### Acceptance criteria",
            "",
            f"- Transport recovery: **{criteria['transport']['status']}**",
            f"- Identity stability: **{criteria['identity']['status']}** "
            f"({criteria['identity']['checked']} checked, "
            f"{criteria['identity']['mismatches']} mismatches)",
            f"- Secret scan: **{criteria['secret_scan']['status']}** "
            f"({criteria['secret_scan']['finding_count']} findings)",
            f"- Concurrency ceiling: **{ceiling['status']}** "
            f"(local={ceiling['process_local']}, distributed={ceiling['distributed']})",
            f"- Granicus leases: {lease['acquisitions']} acquisitions, "
            f"{lease['wait_seconds']:.1f}s wait, {lease['stale_reaped']} stale reaped",
            "- Other run-stage errors "
            "(informational, not an H16 transport criterion): "
            f"{report['run_stage_errors']}",
        ]
    )
    if report["non_granicus_provider_errors"]:
        rendered = ", ".join(
            f"{provider}={count}"
            for provider, count in report["non_granicus_provider_errors"].items()
        )
        lines.append(f"- Non-Granicus provider warnings: {rendered}")
    return "\n".join(lines) + "\n"


def write_h16_report(report: dict, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "h16-report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    (output_dir / "h16-report.md").write_text(to_markdown(report))
