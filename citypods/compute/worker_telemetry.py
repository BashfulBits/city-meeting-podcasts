"""Resource telemetry for external ASR workers.

The Modal/Beam pull workers need memory observations that survive provider log retention and can be
reported from GitHub without provider credentials. Keep this deliberately small and non-secret:
one CAS-managed rolling JSON object stores per-claim operational metrics only.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from citypods.resources import format_bytes, process_rss_bytes

TELEMETRY_STATE_KEY = "state/asr_worker_telemetry.json"
TELEMETRY_SCHEMA_VERSION = 1
DEFAULT_MAX_SAMPLES = 200


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def gpu_vram_bytes(
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[int | None, int | None]:
    """Return ``(used_bytes, total_bytes)`` across visible NVIDIA GPUs, if ``nvidia-smi`` works."""

    try:
        proc = runner(
            [
                "nvidia-smi",
                "--query-gpu=memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None, None
    if proc.returncode != 0:
        return None, None
    used_mib = 0
    total_mib = 0
    seen = False
    for line in proc.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            used_mib += int(float(parts[0]))
            total_mib += int(float(parts[1]))
        except ValueError:
            continue
        seen = True
    if not seen:
        return None, None
    return used_mib * 1024 * 1024, total_mib * 1024 * 1024


def resource_snapshot(
    label: str,
    *,
    rss_probe: Callable[[], int | None] = process_rss_bytes,
    gpu_probe: Callable[[], tuple[int | None, int | None]] = gpu_vram_bytes,
) -> dict[str, Any]:
    gpu_used, gpu_total = gpu_probe()
    return {
        "label": label,
        "captured_at": _now_iso(),
        "rss_bytes": rss_probe(),
        "gpu_vram_used_bytes": gpu_used,
        "gpu_vram_total_bytes": gpu_total,
    }


@dataclass
class ResourceTracker:
    """Capture labeled snapshots and retain peak RSS/GPU memory for one worker scope."""

    log: Callable[[str], None] | None = None
    rss_probe: Callable[[], int | None] = process_rss_bytes
    gpu_probe: Callable[[], tuple[int | None, int | None]] = gpu_vram_bytes
    snapshots: list[dict[str, Any]] = field(default_factory=list)
    peak_rss_bytes: int | None = None
    peak_gpu_vram_used_bytes: int | None = None
    gpu_vram_total_bytes: int | None = None

    def record(self, label: str) -> dict[str, Any]:
        snap = resource_snapshot(label, rss_probe=self.rss_probe, gpu_probe=self.gpu_probe)
        self.snapshots.append(snap)
        rss = snap.get("rss_bytes")
        gpu_used = snap.get("gpu_vram_used_bytes")
        gpu_total = snap.get("gpu_vram_total_bytes")
        if isinstance(rss, int):
            self.peak_rss_bytes = (
                rss if self.peak_rss_bytes is None else max(self.peak_rss_bytes, rss)
            )
        if isinstance(gpu_used, int):
            self.peak_gpu_vram_used_bytes = (
                gpu_used
                if self.peak_gpu_vram_used_bytes is None
                else max(self.peak_gpu_vram_used_bytes, gpu_used)
            )
        if isinstance(gpu_total, int):
            self.gpu_vram_total_bytes = (
                gpu_total
                if self.gpu_vram_total_bytes is None
                else max(self.gpu_vram_total_bytes, gpu_total)
            )
        if self.log is not None:
            self.log(
                "[external-worker] resource "
                f"label={label} rss={format_bytes(rss)} "
                f"gpu_vram={format_bytes(gpu_used)}/{format_bytes(gpu_total)}"
            )
        return snap

    def update_from(self, other: ResourceTracker) -> None:
        if other.peak_rss_bytes is not None:
            self.peak_rss_bytes = (
                other.peak_rss_bytes
                if self.peak_rss_bytes is None
                else max(self.peak_rss_bytes, other.peak_rss_bytes)
            )
        if other.peak_gpu_vram_used_bytes is not None:
            self.peak_gpu_vram_used_bytes = (
                other.peak_gpu_vram_used_bytes
                if self.peak_gpu_vram_used_bytes is None
                else max(self.peak_gpu_vram_used_bytes, other.peak_gpu_vram_used_bytes)
            )
        if other.gpu_vram_total_bytes is not None:
            self.gpu_vram_total_bytes = (
                other.gpu_vram_total_bytes
                if self.gpu_vram_total_bytes is None
                else max(self.gpu_vram_total_bytes, other.gpu_vram_total_bytes)
            )


def empty_telemetry() -> dict[str, Any]:
    return {
        "schema_version": TELEMETRY_SCHEMA_VERSION,
        "updated_at": None,
        "samples": [],
        "by_backend": {},
    }


def load_worker_telemetry(storage) -> dict[str, Any]:
    got = storage.get_bytes(TELEMETRY_STATE_KEY)
    if got is None:
        return empty_telemetry()
    data, _etag = got
    try:
        parsed = json.loads(data)
    except (TypeError, ValueError):
        return empty_telemetry()
    if not isinstance(parsed, dict):
        return empty_telemetry()
    samples = parsed.get("samples")
    if not isinstance(samples, list):
        parsed["samples"] = []
    by_backend = parsed.get("by_backend")
    return {
        "schema_version": TELEMETRY_SCHEMA_VERSION,
        "updated_at": parsed.get("updated_at"),
        "samples": [s for s in parsed["samples"] if isinstance(s, dict)],
        "by_backend": by_backend if isinstance(by_backend, dict) else {},
    }


def _sort_key(sample: dict[str, Any]) -> str:
    return str(sample.get("finished_at") or sample.get("started_at") or "")


def _backend_summary(samples: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for sample in samples:
        backend = str(sample.get("backend") or "unknown")
        grouped.setdefault(backend, []).append(sample)
    out: dict[str, dict[str, Any]] = {}
    for backend, rows in grouped.items():
        rows = sorted(rows, key=_sort_key)
        outcomes = Counter(str(r.get("outcome") or "unknown") for r in rows)
        peak_rss = max(
            (r["peak_rss_bytes"] for r in rows if isinstance(r.get("peak_rss_bytes"), int)),
            default=None,
        )
        peak_gpu = max(
            (
                r["peak_gpu_vram_used_bytes"]
                for r in rows
                if isinstance(r.get("peak_gpu_vram_used_bytes"), int)
            ),
            default=None,
        )
        gpu_total = max(
            (
                r["gpu_vram_total_bytes"]
                for r in rows
                if isinstance(r.get("gpu_vram_total_bytes"), int)
            ),
            default=None,
        )
        duration_buckets = Counter(str(r.get("duration_bucket") or "unknown") for r in rows)
        budget_estimates = [
            float(r["budget_units_estimate"])
            for r in rows
            if isinstance(r.get("budget_units_estimate"), (int, float))
        ]
        out[backend] = {
            "samples": len(rows),
            "success": outcomes.get("success", 0),
            "failed": outcomes.get("failed", 0),
            "outcomes": dict(outcomes),
            "duration_buckets": dict(sorted(duration_buckets.items())),
            "latest_finished_at": rows[-1].get("finished_at"),
            "max_budget_units_estimate": max(budget_estimates) if budget_estimates else None,
            "avg_budget_units_estimate": (
                sum(budget_estimates) / len(budget_estimates) if budget_estimates else None
            ),
            "peak_rss_bytes": peak_rss,
            "peak_gpu_vram_used_bytes": peak_gpu,
            "gpu_vram_total_bytes": gpu_total,
        }
    return out


def append_worker_telemetry(
    storage,
    sample: dict[str, Any],
    *,
    max_samples: int = DEFAULT_MAX_SAMPLES,
    max_attempts: int = 8,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Append one sample to the CAS telemetry object. Returns False when CAS is unavailable."""

    if not getattr(storage, "cas_capable", False):
        return False
    from citypods.storage import CASConflict

    max_samples = max(1, int(max_samples))
    for attempt in range(max_attempts):
        got = storage.get_bytes(TELEMETRY_STATE_KEY)
        if got is None:
            current, etag = empty_telemetry(), None
        else:
            data, etag = got
            try:
                current = json.loads(data)
            except (TypeError, ValueError):
                current = empty_telemetry()
        samples = current.get("samples") if isinstance(current, dict) else []
        if not isinstance(samples, list):
            samples = []
        rows = [s for s in samples if isinstance(s, dict)]
        rows.append(dict(sample))
        rows = sorted(rows, key=_sort_key)[-max_samples:]
        body = {
            "schema_version": TELEMETRY_SCHEMA_VERSION,
            "updated_at": _now_iso(),
            "samples": rows,
            "by_backend": _backend_summary(rows),
        }
        encoded = (json.dumps(body, indent=2, sort_keys=True) + "\n").encode()
        try:
            if etag is None:
                storage.put_cas(
                    TELEMETRY_STATE_KEY,
                    encoded,
                    "application/json",
                    if_none_match="*",
                )
            else:
                storage.put_cas(
                    TELEMETRY_STATE_KEY,
                    encoded,
                    "application/json",
                    if_match=etag,
                )
            return True
        except CASConflict:
            sleep(min(0.05 * 2**attempt, 1.0))
    return False


def telemetry_report(telemetry: dict[str, Any]) -> dict[str, Any]:
    samples = telemetry.get("samples") if isinstance(telemetry, dict) else []
    rows = [s for s in samples if isinstance(s, dict)] if isinstance(samples, list) else []
    return {
        "samples": len(rows),
        "by_backend": _backend_summary(rows),
        "latest": sorted(rows, key=_sort_key)[-5:],
    }
