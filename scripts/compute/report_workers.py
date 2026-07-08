"""Report ASR worker completion/capacity without invoking external GPU providers."""

from __future__ import annotations

import argparse
import json
import tempfile
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from citypods.compute.budget import load_budget, load_budget_cas, storage_supports_cas
from citypods.compute.policy import backend_policy
from citypods.compute.worker_telemetry import load_worker_telemetry, telemetry_report
from citypods.config import load_city_configs, load_site_config
from citypods.ops.work_leases import read_lease
from citypods.ops.workqueue import (
    BUCKET_FEED_VISIBLE,
    MANIFEST_NAME,
    build_manifest,
    load_manifest,
    manifest_counts,
    overlay_persisted_operational_state,
)
from citypods.records import load_records, source_key
from citypods.report import _load_run_history
from citypods.resources import format_bytes
from citypods.state import resolve_state_dir
from citypods.statesync import STATE_PREFIX, pull_state
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


def _recent_samples(storage, n: int) -> list[dict]:
    """The last *n* raw telemetry samples (newest first), for identifying which episode a live
    canary actually claimed — the aggregated ``telemetry_report()`` counts don't retain identity,
    but ``append_worker_telemetry`` already writes ``source_key``/``episode_uid``/``duration_hours``
    per sample (external_worker.py's ``_append_telemetry_sample``); this just surfaces them."""
    samples = load_worker_telemetry(storage).get("samples") or []
    ordered = sorted(samples, key=_sample_sort_key, reverse=True)
    keys = (
        "backend",
        "source_key",
        "episode_uid",
        "outcome",
        "duration_hours",
        "elapsed_seconds",
        "finished_at",
    )
    return [{k: s.get(k) for k in keys} for s in ordered[:n]]


def _sample_sort_key(sample: dict) -> str:
    return str(sample.get("finished_at") or sample.get("started_at") or "")


def _manifest_last_modified(storage) -> str | None:
    """When ``state/work.json`` was last written, straight from the storage layer's own
    metadata — cheap (an exact-key list, not a broad prefix scan) and avoids the trap of inferring
    manifest freshness from GitHub Actions run *start* times, which was wrong once already: a job
    starting after a config merge can still finish (and rebuild the manifest) well after a canary
    that ran in between already read the stale pre-merge manifest."""
    if not hasattr(storage, "list_objects"):
        return None
    key = f"{STATE_PREFIX}/{MANIFEST_NAME}"
    try:
        for obj_key, last_modified in storage.list_objects(key):
            if obj_key == key and last_modified is not None:
                return (
                    last_modified.isoformat()
                    if hasattr(last_modified, "isoformat")
                    else str(last_modified)
                )
    except Exception:
        # Some storage-like fakes deliberately implement list_objects only to raise (e.g.
        # tests/_cas_fake.py's MemCAS, enforcing "never list the R2 lease prefix" elsewhere in
        # this codebase). A raise here must degrade to "unknown," not crash every other
        # unrelated diagnostic in this report.
        return None
    return None


def _duration_band(items: list, threshold: float) -> dict:
    """How much of the current *feed-visible, queued* transcript-asr backlog the ``long_first``
    comparator actually has to work with — the missing piece that made ``long_first`` floating
    nothing look like a bug when it may just be an empty band. ``unknown`` (duration_hours <= 0)
    is called out separately: those items can never be floated by ``long_first`` regardless of
    their true length, since an unknown duration reads as 0 hours."""
    over = sum(1 for wi in items if wi.duration_hours > threshold)
    unknown = sum(1 for wi in items if wi.duration_hours <= 0)
    known = [wi.duration_hours for wi in items if wi.duration_hours > 0]
    return {
        "threshold_hours": threshold,
        "total": len(items),
        "over_threshold": over,
        "unknown_duration": unknown,
        "max_known_hours": round(max(known), 2) if known else None,
    }


def _claimable_mix(items: list, *, long_threshold: float, fresh_within_days: float) -> dict:
    now = datetime.now(UTC)
    fresh_after = now - timedelta(days=max(0.0, fresh_within_days))
    mix = Counter()
    for wi in items:
        is_long = wi.duration_hours > long_threshold if long_threshold > 0 else False
        is_fresh = wi.published is not None and wi.published >= fresh_after
        if is_long and is_fresh:
            mix["fresh_long"] += 1
        elif is_long:
            mix["backlog_long"] += 1
        elif is_fresh:
            mix["fresh_short"] += 1
        else:
            mix["backlog_short"] += 1
    return dict(mix)


def build_report(
    *, site_config_path: str, output_dir: str, base_url: str | None = None, recent: int = 0
) -> dict:
    site_config = load_site_config(site_config_path)
    output = Path(output_dir)
    storage = make_storage(site_config, base_url or site_config.get("base_url", ""), output)
    if storage is None:
        raise RuntimeError("storage is not configured")
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td)
        pull_state(storage, state_dir)
        city_by_source = {}
        for city in load_city_configs("config", site_config.get("defaults", {})):
            city_by_source.setdefault(source_key(city), city)
        manifest_sources = [
            (key, city, load_records(state_dir, key)) for key, city in city_by_source.items()
        ]
        manifest = overlay_persisted_operational_state(
            build_manifest(manifest_sources),
            load_manifest(state_dir),
        )
        counts = manifest_counts(manifest)
        pending = [
            wi for wi in manifest if wi.work_class == "transcript-asr" and wi.state != "done"
        ]
        # Exactly external_worker.py's own candidate filter (work_class/state/bucket) — so this
        # diagnostic answers "what would a canary actually see," not a broader/looser count.
        claimable = [
            wi
            for wi in manifest
            if wi.work_class == "transcript-asr"
            and wi.state == "queued"
            and wi.priority_bucket == BUCKET_FEED_VISIBLE
        ]
        threshold = float(
            (site_config.get("defaults") or {}).get("asr_local_max_duration_hours", 4)
        )
        modal_policy = backend_policy(site_config, "modal")
        beam_policy = backend_policy(site_config, "beam")
        leases = Counter()
        lease_states = Counter()
        recent_samples: list[dict] = []
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
            if recent > 0:
                recent_samples = _recent_samples(storage, recent)
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
            "recent_samples": recent_samples,
            "manifest_last_modified": _manifest_last_modified(storage),
            "transcript_asr_duration_band": _duration_band(claimable, threshold),
            "transcript_asr_claimable_mix": {
                "modal": _claimable_mix(
                    claimable,
                    long_threshold=modal_policy.task.prefer_min_duration_hours,
                    fresh_within_days=modal_policy.task.fresh_within_days,
                ),
                "beam": _claimable_mix(
                    claimable,
                    long_threshold=beam_policy.task.prefer_min_duration_hours,
                    fresh_within_days=beam_policy.task.fresh_within_days,
                ),
            },
            "backend_policies": {
                "modal": {
                    "budget": {
                        "monthly_units": modal_policy.budget.monthly_units,
                        "reserve_units": modal_policy.budget.reserve_units,
                        "unit_label": modal_policy.budget.unit_label,
                    },
                    "dispatch": {
                        "max_inflight": modal_policy.dispatch.max_inflight,
                        "min_claims_per_run": modal_policy.dispatch.min_claims_per_run,
                        "max_claims_per_run": modal_policy.dispatch.max_claims_per_run,
                        "preferred_days": modal_policy.dispatch.preferred_days,
                    },
                    "task": {
                        "prefer_min_duration_hours": modal_policy.task.prefer_min_duration_hours,
                        "fresh_within_days": modal_policy.task.fresh_within_days,
                    },
                },
                "beam": {
                    "budget": {
                        "monthly_units": beam_policy.budget.monthly_units,
                        "reserve_units": beam_policy.budget.reserve_units,
                        "unit_label": beam_policy.budget.unit_label,
                    },
                    "dispatch": {
                        "max_inflight": beam_policy.dispatch.max_inflight,
                        "min_claims_per_run": beam_policy.dispatch.min_claims_per_run,
                        "max_claims_per_run": beam_policy.dispatch.max_claims_per_run,
                        "preferred_days": beam_policy.dispatch.preferred_days,
                    },
                    "task": {
                        "prefer_min_duration_hours": beam_policy.task.prefer_min_duration_hours,
                        "fresh_within_days": beam_policy.task.fresh_within_days,
                    },
                },
            },
        }


def _markdown(report: dict) -> str:
    budget = report.get("budget", {})
    lines = ["## ASR worker report", ""]
    lines.append(
        "- manifest (`state/work.json`) last modified: "
        f"`{report.get('manifest_last_modified') or 'unknown'}`"
    )
    lines.append(f"- transcript-asr pending: `{report.get('transcript_asr_pending', 0)}`")
    band = report.get("transcript_asr_duration_band") or {}
    if band:
        lines.append(
            f"- transcript-asr claimable (feed-visible, queued): `{band.get('total', 0)}` total, "
            f"`{band.get('over_threshold', 0)}` over `{band.get('threshold_hours')}`h "
            f"(what `long_first` floats), `{band.get('unknown_duration', 0)}` unknown duration, "
            f"max known `{band.get('max_known_hours')}`h"
        )
    mixes = report.get("transcript_asr_claimable_mix") or {}
    for backend, mix in sorted(mixes.items()):
        if mix:
            lines.append(
                f"- {backend} claimable mix: fresh long `{mix.get('fresh_long', 0)}`, "
                f"backlog long `{mix.get('backlog_long', 0)}`, fresh short "
                f"`{mix.get('fresh_short', 0)}`, backlog short `{mix.get('backlog_short', 0)}`"
            )
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
                f"{format_bytes(row.get('gpu_vram_total_bytes'))}`, duration buckets "
                f"`{row.get('duration_buckets', {})}`"
            )
    lines.append(f"- compute budget month: `{budget.get('month', '')}`")
    policies = report.get("backend_policies") or {}
    for name, led in sorted((budget.get("backends") or {}).items()):
        used = float((led or {}).get("used_units", (led or {}).get("used_gpu_seconds", 0.0))
        )
        inflight = len((led or {}).get("inflight") or {})
        policy = policies.get(name) or {}
        unit_label = ((policy.get("budget") or {}).get("unit_label")) or "unit"
        monthly = ((policy.get("budget") or {}).get("monthly_units"))
        reserve = ((policy.get("budget") or {}).get("reserve_units"))
        dispatch = policy.get("dispatch") or {}
        task = policy.get("task") or {}
        lines.append(
            f"- {name}: `{used:.1f}` {unit_label}(s) used"
            + (f" / `{monthly:.1f}` budget" if isinstance(monthly, (int, float)) else "")
            + (
                f", reserve `{reserve:.1f}`"
                if isinstance(reserve, (int, float)) and reserve > 0
                else ""
            )
            + f", `{inflight}` in flight, max_claims/run "
            f"`{dispatch.get('min_claims_per_run')}`-`{dispatch.get('max_claims_per_run')}`, "
            f"preferred_days `{dispatch.get('preferred_days')}`, max_inflight "
            f"`{dispatch.get('max_inflight')}`, prefer duration >= "
            f"`{task.get('prefer_min_duration_hours')}`h"
        )
    recent_samples = report.get("recent_samples") or []
    if recent_samples:
        lines.append("")
        lines.append("### Recent worker-telemetry samples (identity, for live-canary spot-checks)")
        for s in recent_samples:
            lines.append(
                f"- `{s.get('backend')}` {s.get('source_key')}/{s.get('episode_uid')} "
                f"outcome=`{s.get('outcome')}` duration_h=`{s.get('duration_hours')}` "
                f"elapsed_s=`{s.get('elapsed_seconds')}` finished_at=`{s.get('finished_at')}`"
            )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site-config", default="config/site_config.yml")
    parser.add_argument("--output-dir", default="docs")
    parser.add_argument("--base-url")
    parser.add_argument("--markdown", action="store_true")
    parser.add_argument(
        "--recent",
        type=int,
        default=0,
        help="also list the last N raw telemetry samples (source_key/episode_uid/duration/outcome) "
        "for identifying which episode a live canary claimed",
    )
    args = parser.parse_args()
    report = build_report(
        site_config_path=args.site_config,
        output_dir=args.output_dir,
        base_url=args.base_url,
        recent=args.recent,
    )
    if args.markdown:
        print(_markdown(report))
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
