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
from citypods.compute.policy import backend_policy

# Registry of external dispatch backends, keyed by the name used in ``compute_backends`` config.
# Empty in H14a (overflow-to-local only); H14b/H14c register ``modal`` / ``beam`` factories here.
DispatchFactory = Callable[[str, dict], DispatchBackend]
_DISPATCH_REGISTRY: dict[str, DispatchFactory] = {}


def register_dispatch_backend(name: str, factory: DispatchFactory) -> None:
    """Register an external dispatch backend factory (``factory(name, caps) -> DispatchBackend``).
    Called at import time by the Modal/Beam adapter modules (H14b/H14c)."""
    _DISPATCH_REGISTRY[name] = factory


def _make_auto(
    site_config: dict, *, state_dir: str | Path | None, storage=None
) -> DispatchCoordinator:
    """Build the dispatcher: registered backends (in config order) with budget, overflowing to
    ``local``. Backends named in config but not yet registered are skipped (no adapter yet).

    ``storage``, when CAS-capable (R2), makes the budget ledger an atomic compare-and-swap object so
    concurrent shards can't overspend the free-tier cap (H17); otherwise the local-file ledger is
    used (review/17 §3)."""
    if state_dir is None:
        raise ValueError("compute_backend 'auto' requires a state_dir for the budget ledger")
    defaults = site_config.get("defaults", {})
    caps_by_name: dict = defaults.get("compute_backends") or {}
    targets: list[DispatchTarget] = []
    for name, caps in caps_by_name.items():
        factory = _DISPATCH_REGISTRY.get(name)
        if factory is None:
            continue  # named in config but no adapter registered yet (H14a) — skip, overflow later
        policy = backend_policy(site_config, name)
        targets.append(
            DispatchTarget(
                backend=factory(name, dict(caps or {})),
                monthly_gpu_seconds=policy.budget.spendable_units,
                max_inflight=policy.dispatch.max_inflight,
            )
        )
    budget = load_budget(state_dir)
    budget.roll_month()
    return DispatchCoordinator(
        local=ProcessLocalBackend(),
        targets=targets,
        budget=budget,
        state_dir=state_dir,
        storage=storage,
    )


def make_compute(
    site_config: dict, *, state_dir: str | Path | None = None, storage=None
) -> Backend:
    """Return the configured execution backend (defaults to ``local``).

    ``state_dir`` is required for ``auto`` (it backs the budget ledger) and ignored for ``local``.
    ``storage`` (when CAS-capable) makes the ``auto`` budget ledger an atomic R2 object (H17).
    """
    defaults = site_config.get("defaults", {})
    backend = os.environ.get("COMPUTE_BACKEND") or defaults.get("compute_backend", "local")
    if backend == "local":
        return ProcessLocalBackend()
    if backend == "auto":
        return _make_auto(site_config, state_dir=state_dir, storage=storage)
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
