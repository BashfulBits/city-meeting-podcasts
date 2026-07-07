"""Shared compute-backend policy parsing for H14/H14d.

The initial H14 rollout modeled provider budgets purely as GPU-seconds. H14d needs a broader
policy surface so pacing, duration preference, and future task-specific tuning (for diarize /
combined flows) can be configured without changing the lease contract.

This module reads both the legacy flat config shape::

    compute_backends:
      modal: { monthly_gpu_seconds: 108000, max_inflight: 8, max_claims: 1 }

and the richer H14d shape::

    compute_backends:
      modal:
        budget: { monthly_units: 108000, reserve_units: 3600, unit_label: gpu-second }
        dispatch: { max_inflight: 8, max_claims_per_run: 1 }
        tasks:
          transcript-asr:
            prefer_min_duration_hours: 4
            budget_units_per_audio_second: 0.25
            min_budget_units: 60

Unknown / missing values degrade conservatively to the original behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BackendBudgetPolicy:
    monthly_units: float = 0.0
    reserve_units: float = 0.0
    unit_label: str = "gpu-second"

    @property
    def spendable_units(self) -> float:
        return max(0.0, self.monthly_units - self.reserve_units)


@dataclass(frozen=True)
class BackendDispatchPolicy:
    max_inflight: int = 0
    max_claims_per_run: int = 1
    max_scan: int | None = None


@dataclass(frozen=True)
class TaskPolicy:
    prefer_min_duration_hours: float = 0.0
    fresh_within_days: float = 7.0
    budget_units_per_audio_second: float = 0.25
    min_budget_units: float = 60.0
    fixed_budget_units_per_claim: float = 0.0


@dataclass(frozen=True)
class BackendPolicy:
    name: str
    budget: BackendBudgetPolicy
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
    dispatch_raw = _as_dict(raw.get("dispatch"))
    tasks_raw = _as_dict(raw.get("tasks"))
    task_raw = _as_dict(tasks_raw.get(work_class))

    monthly_units = _as_float(
        budget_raw.get("monthly_units", raw.get("monthly_gpu_seconds")),
        0.0,
    )
    reserve_units = _as_float(budget_raw.get("reserve_units"), 0.0)
    unit_label = str(budget_raw.get("unit_label") or "gpu-second")

    max_inflight = _as_int(dispatch_raw.get("max_inflight", raw.get("max_inflight")), 0)
    max_claims = _as_int(dispatch_raw.get("max_claims_per_run", raw.get("max_claims")), 1)
    max_scan_raw = dispatch_raw.get("max_scan", raw.get("max_scan"))
    max_scan = None if max_scan_raw in (None, "") else _as_int(max_scan_raw, 0)

    task = TaskPolicy(
        prefer_min_duration_hours=max(
            0.0,
            _as_float(task_raw.get("prefer_min_duration_hours"), 0.0),
        ),
        fresh_within_days=max(
            0.0,
            _as_float(task_raw.get("fresh_within_days"), 7.0),
        ),
        budget_units_per_audio_second=max(
            0.0,
            _as_float(
                task_raw.get(
                    "budget_units_per_audio_second",
                    raw.get("gpu_seconds_per_audio_second", 0.25),
                ),
                0.25,
            ),
        ),
        min_budget_units=max(
            0.0,
            _as_float(task_raw.get("min_budget_units", raw.get("min_gpu_seconds", 60.0)), 60.0),
        ),
        fixed_budget_units_per_claim=max(
            0.0,
            _as_float(task_raw.get("fixed_budget_units_per_claim"), 0.0),
        ),
    )
    return BackendPolicy(
        name=backend,
        budget=BackendBudgetPolicy(
            monthly_units=max(0.0, monthly_units),
            reserve_units=max(0.0, reserve_units),
            unit_label=unit_label,
        ),
        dispatch=BackendDispatchPolicy(
            max_inflight=max(0, max_inflight),
            max_claims_per_run=max(0, max_claims),
            max_scan=max_scan if max_scan is None or max_scan > 0 else None,
        ),
        task=task,
    )
