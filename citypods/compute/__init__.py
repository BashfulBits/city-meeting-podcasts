"""Execution-backend selection for heavy inference (sibling of ``storage/``).

``make_compute`` picks a backend from the ``COMPUTE_BACKEND`` env var (handy for local dev /
tests) or ``site_config.defaults.compute_backend``:
  - ``"local"`` (default-safe): runs faster-whisper / stable-ts in this process
    (:class:`LocalBackend`).
  - ``"auto"``: returns a :class:`DispatchCoordinator` that fills each configured external backend's
    free-tier budget first (Modal, then Beam — H14b/H14c) and **overflows to ``local``**. Until
    those adapters register, no dispatch backend exists, so ``auto`` overflows everything to
    ``local`` — behavior-identical to ``local`` today, but with the dispatch/lease/budget machinery
    live.

External dispatch backends register via :func:`register_dispatch_backend` (the Modal/Beam adapters
do this in H14b/H14c); the first LLM-API backend lands with R3/R4. All slot in without touching the
:mod:`~citypods.compute.base` contract — that shape is the pre-1.0 lock.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from citypods.compute.base import (
    Backend,
    DispatchBackend,
    InferenceJob,
    JobHandle,
    JobResult,
    Task,
    lease_owner_for,
)
from citypods.compute.budget import Budget, load_budget, save_budget
from citypods.compute.dispatch import (
    DispatchCoordinator,
    DispatchTarget,
    reconcile_compute,
    select_backend,
)
from citypods.compute.local import LocalBackend
from citypods.compute.local_process import ProcessLocalBackend

# Registry of external dispatch backends, keyed by the name used in ``compute_backends`` config.
# Empty in H14a (overflow-to-local only); H14b/H14c register ``modal`` / ``beam`` factories here.
DispatchFactory = Callable[[str, dict], DispatchBackend]
_DISPATCH_REGISTRY: dict[str, DispatchFactory] = {}


def register_dispatch_backend(name: str, factory: DispatchFactory) -> None:
    """Register an external dispatch backend factory (``factory(name, caps) -> DispatchBackend``).
    Called at import time by the Modal/Beam adapter modules (H14b/H14c)."""
    _DISPATCH_REGISTRY[name] = factory


def _make_auto(site_config: dict, *, state_dir: str | Path | None) -> DispatchCoordinator:
    """Build the dispatcher: registered backends (in config order) with budget, overflowing to
    ``local``. Backends named in config but not yet registered are skipped (no adapter yet)."""
    if state_dir is None:
        raise ValueError("compute_backend 'auto' requires a state_dir for the budget ledger")
    defaults = site_config.get("defaults", {})
    caps_by_name: dict = defaults.get("compute_backends") or {}
    targets: list[DispatchTarget] = []
    for name, caps in caps_by_name.items():
        factory = _DISPATCH_REGISTRY.get(name)
        if factory is None:
            continue  # named in config but no adapter registered yet (H14a) — skip, overflow later
        targets.append(
            DispatchTarget(
                backend=factory(name, dict(caps or {})),
                monthly_gpu_seconds=float((caps or {}).get("monthly_gpu_seconds", 0.0)),
                max_inflight=int((caps or {}).get("max_inflight", 0)),
            )
        )
    budget = load_budget(state_dir)
    budget.roll_month()
    return DispatchCoordinator(
        local=ProcessLocalBackend(), targets=targets, budget=budget, state_dir=state_dir
    )


def make_compute(site_config: dict, *, state_dir: str | Path | None = None) -> Backend:
    """Return the configured execution backend (defaults to ``local``).

    ``state_dir`` is required for ``auto`` (it backs the budget ledger) and ignored for ``local``.
    """
    defaults = site_config.get("defaults", {})
    backend = os.environ.get("COMPUTE_BACKEND") or defaults.get("compute_backend", "local")
    if backend == "local":
        return ProcessLocalBackend()
    if backend == "auto":
        return _make_auto(site_config, state_dir=state_dir)
    raise ValueError(f"unknown compute_backend: {backend!r}")


__all__ = [
    "Backend",
    "Budget",
    "DispatchBackend",
    "DispatchCoordinator",
    "DispatchTarget",
    "InferenceJob",
    "JobHandle",
    "JobResult",
    "LocalBackend",
    "ProcessLocalBackend",
    "Task",
    "lease_owner_for",
    "load_budget",
    "make_compute",
    "reconcile_compute",
    "register_dispatch_backend",
    "save_budget",
    "select_backend",
]
