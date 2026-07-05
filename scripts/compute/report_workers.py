"""Report ASR worker completion/capacity without invoking external GPU providers."""

from __future__ import annotations

import argparse
import json
import tempfile
from collections import Counter
from pathlib import Path

from citypods.compute.budget import load_budget, load_budget_cas, storage_supports_cas
from citypods.compute.worker_telemetry import load_worker_telemetry, telemetry_report
from citypods.config import load_site_config
from citypods.ops.work_leases import read_lease
from citypods.ops.workqueue import load_manifest, manifest_counts
from citypods.report import _load_run_history
from citypods.resources import format_bytes
from citypods.state import resolve_state_dir
from citypods.statesync import pull_state
from citypods.storage import make_storage


def _stage_asr_totals(state_dir: Path) -> dict:
    rows = _load_run_history(state_dir)
    by_backend = Counter()
    total = 0
    for row in rows[-40:]:
        if row.get("lane") not in (None, "transcribe", "align"):
            continue
        transcript = (row.get("stages") or {}).get("transcript") or {}
        completed = int(transcript.get("transcribed", 0) or 0) + int(
            transcript.get("aligned", 0) or 0
        )
        if completed:
            by_backend["github"] += completed
            total += completed
    return {"recent_completed": total, "by_backend": dict(by_backend)}


def build_report(*, site_config_path: str, output_dir: str, base_url: str | None = None) -> dict:
    site_config = load_site_config(site_config_path)
    output = Path(output_dir)
    storage = make_storage(site_config, base_url or site_config.get("base_url", ""), output)
    if storage is None:
        raise RuntimeError("storage is not configured")
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td)
        pull_state(storage, state_dir)
        manifest = load_manifest(state_dir)
        counts = manifest_counts(manifest)
        pending = [
            wi for wi in manifest if wi.work_class == "transcript-asr" and wi.state != "done"
        ]
        leases = Counter()
        lease_states = Counter()
        if storage_supports_cas(storage):
            for wi in pending:
                lease, _etag = read_lease(storage, wi.source_key, wi.episode_uid)
                if lease is None:
                    lease_states["absent"] += 1
                    continue
                lease_states[lease.state] += 1
                if lease.owner:
                    leases[lease.owner.split(":", 1)[0]] += 1
            budget = load_budget_cas(storage)[0]
            worker_telemetry = telemetry_report(load_worker_telemetry(storage))
        else:
            budget = load_budget(resolve_state_dir(site_config, output))
            worker_telemetry = telemetry_report({})
        return {
            "work": counts,
            "transcript_asr_pending": len(pending),
            "work_leases": {"by_state": dict(lease_states), "by_backend": dict(leases)},
            "budget": budget.to_dict(),
            "github_asr": _stage_asr_totals(state_dir),
            "worker_telemetry": worker_telemetry,
        }


def _markdown(report: dict) -> str:
    budget = report.get("budget", {})
    lines = ["## ASR worker report", ""]
    lines.append(f"- transcript-asr pending: `{report.get('transcript_asr_pending', 0)}`")
    lines.append(f"- work leases by state: `{report.get('work_leases', {}).get('by_state', {})}`")
    lines.append(f"- in-flight by backend: `{report.get('work_leases', {}).get('by_backend', {})}`")
    lines.append(f"- recent GitHub ASR completions: `{report.get('github_asr', {})}`")
    telemetry = report.get("worker_telemetry") or {}
    if not telemetry.get("samples"):
        lines.append("- worker memory telemetry: `no samples yet`")
    else:
        lines.append(f"- worker memory telemetry samples: `{telemetry.get('samples', 0)}`")
        for name, row in sorted((telemetry.get("by_backend") or {}).items()):
            lines.append(
                f"- {name} telemetry: `{row.get('success', 0)}` success, "
                f"`{row.get('failed', 0)}` failed, peak RSS "
                f"`{format_bytes(row.get('peak_rss_bytes'))}`, peak GPU VRAM "
                f"`{format_bytes(row.get('peak_gpu_vram_used_bytes'))}/"
                f"{format_bytes(row.get('gpu_vram_total_bytes'))}`"
            )
    lines.append(f"- compute budget month: `{budget.get('month', '')}`")
    for name, led in sorted((budget.get("backends") or {}).items()):
        used = float((led or {}).get("used_gpu_seconds", 0.0))
        inflight = len((led or {}).get("inflight") or {})
        lines.append(f"- {name}: `{used:.1f}` GPU-second(s) used, `{inflight}` in flight")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site-config", default="config/site_config.yml")
    parser.add_argument("--output-dir", default="docs")
    parser.add_argument("--base-url")
    parser.add_argument("--markdown", action="store_true")
    args = parser.parse_args()
    report = build_report(
        site_config_path=args.site_config,
        output_dir=args.output_dir,
        base_url=args.base_url,
    )
    if args.markdown:
        print(_markdown(report))
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
