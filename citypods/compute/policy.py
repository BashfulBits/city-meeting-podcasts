"""Shared compute-backend policy parsing for H14/H14d.

H14 started with generic budget "units" that were treated as effective runtime seconds. H14d moves
to provider-cycle dollars plus task-level runtime-estimate knobs, while still accepting the older
shape so stale local config degrades conservatively.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BackendBudgetPolicy:
    monthly_dollars: float = 0.0
    reserve_dollars: float = 0.0
    rollover_day_of_month: int = 1
    estimated_dollars_per_runtime_second: float = 0.0
    unit_label: str = "dollar"

    @property
    def spendable_dollars(self) -> float:
        return max(0.0, self.monthly_dollars - self.reserve_dollars)

    @property
    def monthly_units(self) -> float:
        return self.monthly_dollars

    @property
    def reserve_units(self) -> float:
        return self.reserve_dollars

    @property
    def spendable_units(self) -> float:
        return self.spendable_dollars


@dataclass(frozen=True)
class BackendHardwarePolicy:
    gpu_type: str = ""


@dataclass(frozen=True)
class BackendDispatchPolicy:
    max_inflight: int = 0
    min_claims_per_run: int = 1
    max_claims_per_run: int = 1
    max_scan: int | None = None
    preferred_days: str = "all"


@dataclass(frozen=True)
class TaskPolicy:
    prefer_min_duration_hours: float = 0.0
    fresh_within_days: float = 7.0
    estimated_runtime_seconds_per_audio_second: float = 0.25
    min_runtime_seconds: float = 60.0
    fixed_runtime_seconds_per_run: float = 0.0
    fixed_runtime_seconds_per_claim: float = 0.0

    @property
    def budget_units_per_audio_second(self) -> float:
        return self.estimated_runtime_seconds_per_audio_second

    @property
    def min_budget_units(self) -> float:
        return self.min_runtime_seconds

    @property
    def fixed_budget_units_per_run(self) -> float:
        return self.fixed_runtime_seconds_per_run

    @property
    def fixed_budget_units_per_claim(self) -> float:
        return self.fixed_runtime_seconds_per_claim


@dataclass(frozen=True)
class BackendPolicy:
    name: str
    budget: BackendBudgetPolicy
    hardware: BackendHardwarePolicy
    dispatch: BackendDispatchPolicy
    task: TaskPolicy


def _as_dict(raw: Any) -> dict[str, Any]:
    return raw if isinstance(raw, dict) else {}


def _as_float(raw: Any, default: float) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _as_int(raw: Any, default: int) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def backend_settings(site_config: dict, backend: str) -> dict[str, Any]:
    defaults = _as_dict(site_config.get("defaults") if isinstance(site_config, dict) else {})
    backends = _as_dict(defaults.get("compute_backends"))
    return _as_dict(backends.get(backend))


def backend_policy(
    site_config: dict,
    backend: str,
    *,
    work_class: str = "transcript-asr",
) -> BackendPolicy:
    raw = backend_settings(site_config, backend)
    budget_raw = _as_dict(raw.get("budget"))
    hardware_raw = _as_dict(raw.get("hardware"))
    dispatch_raw = _as_dict(raw.get("dispatch"))
    tasks_raw = _as_dict(raw.get("tasks"))
    task_raw = _as_dict(tasks_raw.get(work_class))

    monthly_dollars = _as_float(
        budget_raw.get(
            "monthly_dollars",
            budget_raw.get("monthly_units", raw.get("monthly_gpu_seconds")),
        ),
        0.0,
    )
    reserve_dollars = _as_float(
        budget_raw.get("reserve_dollars", budget_raw.get("reserve_units")),
        0.0,
    )
    rollover_day_of_month = max(
        1,
        min(31, _as_int(budget_raw.get("rollover_day_of_month"), 1)),
    )
    estimated_rate_raw = budget_raw.get("estimated_dollars_per_runtime_second")
    estimated_dollars_per_runtime_second = max(
        0.0,
        _as_float(estimated_rate_raw, 1.0),
    )
    unit_label = str(budget_raw.get("unit_label") or "dollar")
    gpu_type = str(hardware_raw.get("gpu_type") or raw.get("gpu_type") or "")

    max_inflight = _as_int(dispatch_raw.get("max_inflight", raw.get("max_inflight")), 0)
    min_claims = _as_int(dispatch_raw.get("min_claims_per_run"), 1)
    max_claims = _as_int(dispatch_raw.get("max_claims_per_run", raw.get("max_claims")), 1)
    max_scan_raw = dispatch_raw.get("max_scan", raw.get("max_scan"))
    max_scan = None if max_scan_raw in (None, "") else _as_int(max_scan_raw, 0)
    preferred_days = str(dispatch_raw.get("preferred_days") or "all")
    if preferred_days not in {"all", "even", "odd"}:
        preferred_days = "all"

    task = TaskPolicy(
        prefer_min_duration_hours=max(
            0.0,
            _as_float(task_raw.get("prefer_min_duration_hours"), 0.0),
        ),
        fresh_within_days=max(
            0.0,
            _as_float(task_raw.get("fresh_within_days"), 7.0),
        ),
        estimated_runtime_seconds_per_audio_second=max(
            0.0,
            _as_float(
                task_raw.get(
                    "estimated_runtime_seconds_per_audio_second",
                    task_raw.get(
                        "budget_units_per_audio_second",
                        raw.get("gpu_seconds_per_audio_second", 0.25),
                    ),
                ),
                0.25,
            ),
        ),
        min_runtime_seconds=max(
            0.0,
            _as_float(
                task_raw.get(
                    "min_runtime_seconds",
                    task_raw.get("min_budget_units", raw.get("min_gpu_seconds", 60.0)),
                ),
                60.0,
            ),
        ),
        fixed_runtime_seconds_per_run=max(
            0.0,
            _as_float(
                task_raw.get(
                    "fixed_runtime_seconds_per_run",
                    task_raw.get("fixed_budget_units_per_run"),
                ),
                0.0,
            ),
        ),
        fixed_runtime_seconds_per_claim=max(
            0.0,
            _as_float(
                task_raw.get(
                    "fixed_runtime_seconds_per_claim",
                    task_raw.get("fixed_budget_units_per_claim"),
                ),
                0.0,
            ),
        ),
    )
    return BackendPolicy(
        name=backend,
        budget=BackendBudgetPolicy(
            monthly_dollars=max(0.0, monthly_dollars),
            reserve_dollars=max(0.0, reserve_dollars),
            rollover_day_of_month=rollover_day_of_month,
            estimated_dollars_per_runtime_second=estimated_dollars_per_runtime_second,
            unit_label=unit_label,
        ),
        hardware=BackendHardwarePolicy(gpu_type=gpu_type),
        dispatch=BackendDispatchPolicy(
            max_inflight=max(0, max_inflight),
            min_claims_per_run=max(0, min_claims),
            max_claims_per_run=max(0, max_claims),
            max_scan=max_scan if max_scan is None or max_scan > 0 else None,
            preferred_days=preferred_days,
        ),
        task=task,
    )
