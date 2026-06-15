"""Free-tier GPU budget ledger — the **$0 guarantee** for external dispatch (H14a).

External serverless-GPU workers (Modal/Beam, H14b/H14c) add real capacity at **$0 — but only while
usage stays inside each provider's monthly free allotment**. This module persists, per backend, how
much of that allotment the dispatcher has spent this month and how many jobs are in flight, so the
dispatcher can refuse to exceed it: a backend with no remaining budget (or no free in-flight slot)
is simply skipped, and work overflows to the next backend / ``local``.

**The cap is structurally unbreachable.** A reservation only happens after :meth:`Budget.available`
confirms ``used + estimate <= cap``; ``used`` already includes every outstanding reservation, so
even back-to-back dispatches (serialized under the dispatcher's lock) cannot both slip past. We
decrement **pessimistically on dispatch** (by the backend's estimate) and **reconcile to actuals**
when the item completes (``settle``, swapping the estimate for the worker-reported
``observed_seconds``) or when a dead worker's lease is reaped (``release``, returning the estimate).

Caps (``monthly_gpu_seconds`` / ``max_inflight``) are **config** (``defaults.compute_backends``),
the source of truth — not persisted here. The ledger persists only spend + reservations, and rides
statesync automatically (any ``state/*.json`` syncs; see :mod:`citypods.statesync`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

BUDGET_NAME = "compute_budget.json"
BUDGET_SCHEMA_VERSION = 1


def month_key(now: datetime | None = None) -> str:
    """The ``YYYY-MM`` allotment key for *now* (UTC). Backends reset when this rolls over."""
    now = now or datetime.now(UTC)
    return now.strftime("%Y-%m")


@dataclass
class BackendLedger:
    """One backend's spend this month: settled actuals plus outstanding reservations.

    ``used_gpu_seconds`` is the running total **including** every in-flight reservation, so it is
    the single value the cap is checked against. ``inflight`` maps a lease owner
    (``"<backend>:<ref>"``) to the GPU-seconds reserved for it, so a reaped or completed job
    returns/settles exactly its own reservation; ``len(inflight)`` is the in-flight count checked
    against ``max_inflight``.
    """

    used_gpu_seconds: float = 0.0
    inflight: dict[str, float] = field(default_factory=dict)

    @property
    def inflight_count(self) -> int:
        return len(self.inflight)


@dataclass
class Budget:
    """Per-backend spend ledger for one month. Mutated under the dispatcher's lock."""

    month: str = field(default_factory=month_key)
    backends: dict[str, BackendLedger] = field(default_factory=dict)

    def _ledger(self, backend: str) -> BackendLedger:
        return self.backends.setdefault(backend, BackendLedger())

    def roll_month(self, now: datetime | None = None) -> bool:
        """Reset every backend's spend + in-flight set when the allotment month has rolled over.
        Returns True if a reset happened. A new month grants a fresh free allotment; reservations
        that straddle the boundary are dropped (a later ``settle``/``release`` for them no-ops)."""
        mk = month_key(now)
        if mk == self.month:
            return False
        self.month = mk
        self.backends = {}
        return True

    def available(self, backend: str, *, cap: float, max_inflight: int, est: float) -> bool:
        """True if *backend* can take a job estimated at *est* GPU-seconds without breaching its
        free-tier cap or its in-flight slot limit. The gate every reservation passes through."""
        led = self._ledger(backend)
        return led.used_gpu_seconds + est <= cap and led.inflight_count < max_inflight

    def reserve(self, owner: str, backend: str, est: float) -> None:
        """Decrement-on-dispatch: charge *est* GPU-seconds to *backend* against owner *owner*."""
        led = self._ledger(backend)
        led.used_gpu_seconds += est
        led.inflight[owner] = est

    def settle(self, owner: str, backend: str, actual: float | None = None) -> None:
        """Reconcile a **completed** job and free its in-flight slot. With a worker-reported
        *actual* GPU-seconds, swap the estimate for it (actuals are ≤ a conservative estimate, so
        ``used`` can only drop). With ``actual=None`` (no actuals channel yet — H14b/c) the estimate
        stays charged: a job that *ran* must never return budget, or the cap could be overspent.
        Idempotent / no-op if the reservation is already gone (e.g. the month rolled)."""
        led = self._ledger(backend)
        reserved = led.inflight.pop(owner, None)
        if reserved is None:
            return
        if actual is not None:
            led.used_gpu_seconds = max(0.0, led.used_gpu_seconds - reserved + actual)

    def release(self, owner: str, backend: str) -> None:
        """Reap-on-expiry: return a **dead** worker's reservation to the pool (it never ran). No-op
        if already gone. Distinct from :meth:`settle` — only work that did *not* run returns
        budget."""
        led = self._ledger(backend)
        reserved = led.inflight.pop(owner, None)
        if reserved is not None:
            led.used_gpu_seconds = max(0.0, led.used_gpu_seconds - reserved)

    # ── serialization ──────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "schema_version": BUDGET_SCHEMA_VERSION,
            "month": self.month,
            "backends": {
                name: {"used_gpu_seconds": led.used_gpu_seconds, "inflight": dict(led.inflight)}
                for name, led in self.backends.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> Budget:
        backends: dict[str, BackendLedger] = {}
        for name, raw in (data.get("backends") or {}).items():
            if not isinstance(raw, dict):
                continue
            inflight = {str(k): float(v) for k, v in (raw.get("inflight") or {}).items()}
            backends[name] = BackendLedger(
                used_gpu_seconds=float(raw.get("used_gpu_seconds", 0.0)), inflight=inflight
            )
        return cls(month=data.get("month") or month_key(), backends=backends)


def load_budget(state_dir: str | Path) -> Budget:
    """Load the persisted ledger from ``state/compute_budget.json`` (empty if absent/corrupt)."""
    path = Path(state_dir) / BUDGET_NAME
    if not path.exists():
        return Budget()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return Budget()
    return Budget.from_dict(data if isinstance(data, dict) else {})


def save_budget(state_dir: str | Path, budget: Budget) -> Path:
    """Persist the ledger; rides statesync via its ``.json`` suffix under ``state/``."""
    path = Path(state_dir) / BUDGET_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(budget.to_dict(), indent=2, sort_keys=True) + "\n")
    return path
