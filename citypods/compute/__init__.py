"""Execution-backend selection for heavy inference (sibling of ``storage/``).

``make_compute`` picks a backend from the ``COMPUTE_BACKEND`` env var (handy for local dev /
tests) or ``site_config.defaults.compute_backend``:
  - ``"local"`` (default): runs faster-whisper / stable-ts in this process (:class:`LocalBackend`).

The external dispatch backends (Modal/Beam) land in H14 as additional cases here; the first
LLM-API backend lands with R3/R4. Both slot in without touching the :mod:`~citypods.compute.base`
contract — that shape is the pre-1.0 lock.
"""

from __future__ import annotations

import os

from citypods.compute.base import (
    Backend,
    InferenceJob,
    JobHandle,
    JobResult,
    Task,
)
from citypods.compute.local import LocalBackend


def make_compute(site_config: dict) -> Backend:
    """Return the configured execution backend (defaults to ``local``)."""
    defaults = site_config.get("defaults", {})
    backend = os.environ.get("COMPUTE_BACKEND") or defaults.get("compute_backend", "local")
    if backend == "local":
        return LocalBackend()
    raise ValueError(f"unknown compute_backend: {backend!r}")


__all__ = [
    "Backend",
    "InferenceJob",
    "JobHandle",
    "JobResult",
    "Task",
    "LocalBackend",
    "make_compute",
]
