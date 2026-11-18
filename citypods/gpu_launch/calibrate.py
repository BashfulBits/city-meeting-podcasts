"""Turn measured runs into planner calibrations.

A *record* is one measured execution: model, GPU, the micro-batch seconds (cold, and warm when the
run repeated them or restored a cache) and load time. ``record_from_telemetry`` adapts the worker's
completion telemetry; the generic backbone probe emits records directly. ``calibrate`` takes the
median over records so one noisy run cannot move a price estimate.
"""

from __future__ import annotations

import dataclasses
import json
import math
import statistics
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from citypods.gpu_launch.profiles import Calibration

DEFAULT_STARTUP_SECONDS = 24.0
GIB = 1024**3


def record_from_telemetry(
    telemetry: Mapping[str, Any], *, model_id: str, gpu: str, startup_seconds: float | None = None
) -> dict[str, Any]:
    """Adapt ``run_batch`` telemetry (with its ``profile`` object) to a measurement record."""
    profile = telemetry["profile"]
    calls = profile["forward"]["calls"]
    peak = telemetry.get("gpu_peak_bytes")
    return {
        "model_id": model_id,
        "gpu": gpu,
        "token_budget": profile["tokens"].get("token_budget"),
        "load_seconds": telemetry["load_seconds"],
        "startup_seconds": startup_seconds,
        "peak_gpu_gib": peak / GIB if peak else None,
        "kernel_cache_restored": bool((telemetry.get("kernel_cache") or {}).get("restored")),
        "micro_batches": [
            {
                "padded_tokens": c["padded_tokens"],
                "cold_seconds": c["seconds"],
                "warm_seconds": c.get("warm_seconds"),
            }
            for c in calls
        ],
    }


def _rates(record: Mapping[str, Any]) -> tuple[float | None, float]:
    """(warm tokens/second or None, cold overhead seconds) for one record."""
    batches = record["micro_batches"]
    warm = [b for b in batches if b.get("warm_seconds")]
    if warm:
        tokens = sum(b["padded_tokens"] for b in warm)
        warm_seconds = sum(b["warm_seconds"] for b in warm)
        cold = sum(b["cold_seconds"] - b["warm_seconds"] for b in warm)
        return tokens / warm_seconds, max(0.0, cold)
    if record.get("kernel_cache_restored"):
        tokens = sum(b["padded_tokens"] for b in batches)
        return tokens / sum(b["cold_seconds"] for b in batches), 0.0
    return None, 0.0


SMALL_SAMPLE_TAIL = 1.3  # fewer than five loads cannot estimate a p90: inflate the worst one


def _load_tail(loads: Sequence[float]) -> float:
    if len(loads) < 5:
        return max(loads) * SMALL_SAMPLE_TAIL
    return sorted(loads)[math.ceil(0.9 * len(loads)) - 1]


def calibrate(
    records: Sequence[Mapping[str, Any]],
    *,
    source: str,
    confidence: str = "measured",
    config_overhead: float | None = None,
) -> Calibration:
    """Median calibration over records for one (model, GPU, token budget).

    ``config_overhead`` is the measured extra forward time with fla prebaked configs only. Without
    a measurement the cache-less overhead is used, since configs are not assumed to help.
    """
    if not records:
        raise ValueError("no measurement records")
    keys = {(r["model_id"], r["gpu"], r["token_budget"]) for r in records}
    if len(keys) != 1:
        raise ValueError("records must share model, GPU and token budget")
    rates = [_rates(r) for r in records]
    speeds = [tps for tps, _ in rates if tps]
    if not speeds:
        raise ValueError("no record has warm timings: repeat micro-batches or restore a cache")
    colds = [cold for tps, cold in rates if tps and cold]
    peaks = [r["peak_gpu_gib"] for r in records if r.get("peak_gpu_gib")]
    loads = [r["load_seconds"] for r in records]
    startups = [r["startup_seconds"] for r in records if r.get("startup_seconds")]
    cold_seconds = statistics.median(colds) if colds else 0.0
    config_seconds = cold_seconds if config_overhead is None else config_overhead
    return Calibration(
        load_seconds=round(statistics.median(loads), 1),
        load_seconds_p90=round(_load_tail(loads), 1),
        startup_seconds=round(statistics.median(startups), 1)
        if startups
        else DEFAULT_STARTUP_SECONDS,
        tokens_per_second=round(statistics.median(speeds), 1),
        cold_overhead_seconds=round(cold_seconds, 1),
        config_overhead_seconds=round(config_seconds, 1),
        token_budget=int(next(iter(keys))[2]),
        peak_gpu_gib=round(max(peaks), 2),
        confidence=confidence,
        source=source,
    )


def records_from_result(
    data: Mapping[str, Any],
    *,
    model_id: str,
    gpu: str,
    startup_seconds: float | None = None,
    token_budget: int | None = None,
) -> list[dict[str, Any]]:
    """Measurement records from a probe/completion file; accepts both known layouts.

    ``telemetry`` layout: a GLiDE-style run (``run_batch`` telemetry with its ``profile``), either
    bare or wrapped as ``{"telemetry": ...}``. ``records`` layout: the backbone probe, one record
    per token budget with ``micro_batches`` carrying cold and warm seconds.
    """
    if "records" in data:
        return [
            {
                "model_id": model_id,
                "gpu": gpu,
                "token_budget": r["token_budget"],
                "load_seconds": data["load_seconds"],
                "startup_seconds": startup_seconds,
                "peak_gpu_gib": r["peak_gpu_gib"],
                "kernel_cache_restored": False,
                "micro_batches": [
                    {k: b[k] for k in ("padded_tokens", "cold_seconds", "warm_seconds")}
                    for b in r["micro_batches"]
                ],
            }
            for r in data["records"]
            if token_budget is None or r["token_budget"] == token_budget
        ]
    telemetry = data.get("telemetry", data)
    return [
        record_from_telemetry(
            telemetry, model_id=model_id, gpu=gpu, startup_seconds=startup_seconds
        )
    ]


def merge_calibration(
    path: Path,
    model_id: str,
    gpu: str,
    calibration: Calibration,
    *,
    new_model: Mapping[str, Any] | None = None,
) -> None:
    """Write ``calibration`` into the profiles file; a new model needs its descriptive fields."""
    document = json.loads(Path(path).read_text())
    models = document["models"]
    if model_id not in models:
        if new_model is None:
            raise ValueError(f"unknown model {model_id}; pass its family, revision and sizes")
        models[model_id] = {**new_model, "calibrations": {}}
    models[model_id]["calibrations"][gpu] = dataclasses.asdict(calibration)
    Path(path).write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
