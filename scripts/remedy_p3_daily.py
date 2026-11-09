#!/usr/bin/env python3
"""Run one fail-closed daily P3 pass through the shared deferred Worker."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

from citypods.config import load_site_config
from citypods.ops.maintenance_leases import acquire
from citypods.remedy_evaluation import RemedyConfig, TrustedProjectEvidence, _read_trusted_artifact
from citypods.remedy_ledger import REMEDY_LEASE_KEY
from citypods.remedy_p3_runner import (
    P3DailyRunner,
    P3LocalReportRunner,
    make_default_backend,
    p3_enabled,
)
from citypods.storage import make_storage
from citypods.storage.local import LocalStorage


def _write_exclusive(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")


def _collect_evidence(repo_root: Path, path: Path) -> None:
    command = [
        sys.executable,
        "scripts/audit_feeds.py",
        "--dry-run",
        "--unexpected-body-evidence",
        str(path),
    ]
    subprocess.run(command, cwd=repo_root, check=True, timeout=3600)


def run_daily(
    *,
    repo_root: Path,
    output: Path,
    evidence_file: Path | None = None,
    evidence_output: Path | None = None,
    report_only_plan: Path | None = None,
    plan_sha256: str | None = None,
) -> dict:
    if report_only_plan is not None:
        if evidence_file is not None or evidence_output is not None:
            raise ValueError("report-only plan supplies its own frozen evidence")
        return run_report_only_plan(
            repo_root=repo_root, output=output, plan_path=report_only_plan, plan_sha256=plan_sha256
        )
    if plan_sha256 is not None:
        raise ValueError("plan hash requires report-only plan")
    site_config = load_site_config(repo_root / "config/site_config.yml")
    remedy_config = RemedyConfig.model_validate(
        yaml.safe_load((repo_root / "config/remedy.yml").read_text(encoding="utf-8"))
    )
    if not p3_enabled(remedy_config, repo_root=repo_root):
        result = {
            "schema_version": 1,
            "status": "policy_hold",
            "reason": "P3 is shadowed or exact route admissions do not match the catalog",
            "findings": [],
        }
        _write_exclusive(output, result)
        return result

    output_dir = repo_root / site_config.get("output_dir", "docs")
    storage = make_storage(site_config, site_config.get("base_url", ""), output_dir)
    if storage is None or not getattr(storage, "cas_capable", False):
        raise RuntimeError("daily P3 requires configured B2 storage and R2 CAS coordination")

    temporary = None
    if evidence_file is None:
        temporary = tempfile.TemporaryDirectory(prefix="citypods-p3-evidence-")
        evidence_file = Path(temporary.name) / "unexpected-body-evidence.json"
        _collect_evidence(repo_root, evidence_file)
    payload = json.loads(evidence_file.read_text(encoding="utf-8"))
    sources = payload.get("sources") if isinstance(payload, dict) else None
    if not isinstance(sources, list):
        raise ValueError("unexpected-body evidence file must contain a sources array")
    if evidence_output is not None:
        _write_exclusive(evidence_output, payload)

    def lease_factory():
        return acquire(
            storage,
            owner="remedy-p3-daily",
            key=REMEDY_LEASE_KEY,
        )

    runner = P3DailyRunner(
        storage=storage,
        backend=make_default_backend(storage),
        lease_factory=lease_factory,
        config=remedy_config,
        repo_root=repo_root,
    )
    findings = []
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("unexpected-body evidence source must be an object")
        for finding in source.get("unexpected_findings", []):
            if not isinstance(finding, dict):
                raise ValueError("unexpected-body finding must be an object")
            findings.append(runner.run_finding(source, finding))
    result = {
        "schema_version": 1,
        "status": (
            "completed"
            if all(row.get("status") == "report_ready" for row in findings)
            else "pending"
        ),
        "findings": findings,
    }
    _write_exclusive(output, result)
    if temporary is not None:
        temporary.cleanup()
    return result


def run_report_only_plan(
    *, repo_root: Path, output: Path, plan_path: Path, plan_sha256: str | None
):
    """One explicit frozen source, local receipts and Worker inference; no production ledger."""
    if not plan_sha256 or not re.fullmatch(r"[0-9a-f]{64}", plan_sha256):
        raise ValueError("report-only plan requires an exact lowercase --plan-sha256")
    if output.exists():
        raise FileExistsError(output)
    try:
        relative = plan_path.absolute().relative_to(repo_root.resolve())
    except ValueError as exc:
        raise ValueError("report-only plan must be inside the repository") from exc
    ref = TrustedProjectEvidence(path=str(relative), sha256=plan_sha256)
    plan = json.loads(_read_trusted_artifact(repo_root.resolve(), ref))
    if (
        set(plan) != {"version", "config", "evidence"}
        or type(plan["version"]) is not int
        or plan["version"] != 1
    ):
        raise ValueError("invalid isolated report plan")
    config = RemedyConfig.model_validate(plan["config"])
    if config.mode != "manual" or config.publication_mode != "report_only":
        raise ValueError("isolated run must be manual/report_only")
    if config.p3_role_plan not in {"flash-deepseek-v2", "flash-deepseek-human-v3"}:
        raise ValueError("isolated plan must select the reviewed Flash/DeepSeek flow")
    if not p3_enabled(config, repo_root=repo_root, for_report_only=True):
        result = {
            "schema_version": 1,
            "status": "policy_hold",
            "findings": [],
            "reason": "Exact runtime evidence, identity or route admission missing",
        }
        _write_exclusive(output, result)
        return result
    evidence_ref = TrustedProjectEvidence.model_validate(plan["evidence"])
    evidence = json.loads(_read_trusted_artifact(repo_root.resolve(), evidence_ref))
    sources = evidence.get("sources")
    if not isinstance(sources, list) or len(sources) != 1:
        raise ValueError("isolated cycle requires exactly one frozen source")
    source = sources[0]
    if not isinstance(source, dict) or len(source.get("unexpected_findings", [])) != 1:
        raise ValueError("isolated cycle requires exactly one finding")
    artifacts = output.parent / ("p3-run-" + plan_sha256)
    storage = LocalStorage(artifacts / "worker-handles", "")
    runner = P3LocalReportRunner(
        storage=storage,
        backend=make_default_backend(storage),
        config=config,
        repo_root=repo_root,
        artifact_root=artifacts / "observations",
    )
    finding = runner.run_finding(source, source["unexpected_findings"][0])
    result = {
        "schema_version": 1,
        "status": "completed" if finding["status"] == "report_ready" else "pending",
        "findings": [finding],
        "publication_mode": "report_only",
    }
    _write_exclusive(output, result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evidence-file", type=Path)
    parser.add_argument("--evidence-output", type=Path)
    parser.add_argument("--report-only-plan", type=Path)
    parser.add_argument("--plan-sha256")
    args = parser.parse_args(argv)
    result = run_daily(
        repo_root=args.repo_root.resolve(),
        output=args.output.resolve(),
        evidence_file=args.evidence_file.resolve() if args.evidence_file else None,
        evidence_output=args.evidence_output.resolve() if args.evidence_output else None,
        report_only_plan=args.report_only_plan,
        plan_sha256=args.plan_sha256,
    )
    print(
        json.dumps(
            {
                "status": result["status"],
                "findings": len(result.get("findings", [])),
                "output": str(args.output),
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
