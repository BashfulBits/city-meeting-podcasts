"""Free-tier external-compute budget ledger — the **$0 guarantee** for dispatch (H14a/H14d).

External workers (Modal/Beam today, future diarize/combined flows later) add real capacity at
**$0 — but only while usage stays inside each provider's monthly free allotment**. This module
persists, per backend, how much of that allotment the dispatcher has spent this month and how many
jobs are in flight, so the dispatcher can refuse to exceed it: a backend with no remaining budget
(or no free in-flight slot) is simply skipped, and work overflows to the next backend / ``local``.

**The cap is structurally unbreachable.** A reservation only happens after :meth:`Budget.available`
confirms ``used + estimate <= cap``; ``used`` already includes every outstanding reservation, so
even back-to-back dispatches (serialized under the dispatcher's lock) cannot both slip past. We
decrement **pessimistically on dispatch** (by the backend's estimate) and **reconcile to actuals**
when the item completes (``settle``, swapping the estimate for the worker-reported
``observed_seconds``) or when a dead worker's lease is reaped (``release``, returning the estimate).

Caps (``monthly_units`` / ``max_inflight``) are **config** (``defaults.compute_backends``), the
source of truth — not persisted here. The ledger persists only spend + reservations, and rides
statesync automatically (any ``state/*.json`` syncs; see :mod:`citypods.statesync`).
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

BUDGET_NAME = "compute_budget.json"
BUDGET_SCHEMA_VERSION = 2

# Durable key on the coordination backend. The ledger needs an atomic decrement (the overspend
# guard) so it lives on R2 and is read/written by compare-and-swap, **not** the bulk B2 state sync
# (``statesync`` excludes coordination keys). Must match a ``storage.routing.COORDINATION_PREFIXES``
# entry so ``RoutingStorage`` routes it to R2.
BUDGET_STATE_KEY = "state/compute_budget.json"


def month_key(now: datetime | None = None) -> str:
    """The ``YYYY-MM`` allotment key for *now* (UTC). Backends reset when this rolls over."""
    now = now or datetime.now(UTC)
    return now.strftime("%Y-%m")


@dataclass
class BackendLedger:
    """One backend's spend this month: settled actuals plus outstanding reservations.

    ``used_units`` is the running total **including** every in-flight reservation, so it is
    the single value the cap is checked against. ``inflight`` maps a lease owner
    (``"<backend>:<ref>"``) to the budget units reserved for it, so a reaped or completed job
    returns/settles exactly its own reservation; ``len(inflight)`` is the in-flight count checked
    against ``max_inflight``.
    """

    used_units: float = 0.0
    inflight: dict[str, float] = field(default_factory=dict)

    @property
    def inflight_count(self) -> int:
        return len(self.inflight)

    @property
    def used_gpu_seconds(self) -> float:
        """Backward-compatible alias for older reporting/tests/docs."""
        return self.used_units


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
        return led.used_units + est <= cap and led.inflight_count < max_inflight

    def reserve(self, owner: str, backend: str, est: float) -> None:
        """Decrement-on-dispatch: charge *est* GPU-seconds to *backend* against owner *owner*."""
        led = self._ledger(backend)
        led.used_units += est
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
            led.used_units = max(0.0, led.used_units - reserved + actual)

    def release(self, owner: str, backend: str) -> None:
        """Reap-on-expiry: return a **dead** worker's reservation to the pool (it never ran). No-op
        if already gone. Distinct from :meth:`settle` — only work that did *not* run returns
        budget."""
        led = self._ledger(backend)
        reserved = led.inflight.pop(owner, None)
        if reserved is not None:
            led.used_units = max(0.0, led.used_units - reserved)

    # ── serialization ──────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "schema_version": BUDGET_SCHEMA_VERSION,
            "month": self.month,
            "backends": {
                name: {"used_units": led.used_units, "inflight": dict(led.inflight)}
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
                used_units=float(raw.get("used_units", raw.get("used_gpu_seconds", 0.0))),
                inflight=inflight,
            )
        return cls(month=data.get("month") or month_key(), backends=backends)


def load_budget(state_dir: str | Path) -> Budget:
    """Load the persisted ledger from ``state/compute_budget.json`` (empty if absent/corrupt)."""
    path = Path(state_dir) / BUDGET_NAME
    if not path.exists():
        return Budget()
    try:
        data = json.loads(path.read_text())
        return Budget.from_dict(data if isinstance(data, dict) else {})
    except (AttributeError, OSError, TypeError, ValueError):
        return Budget()


def save_budget(state_dir: str | Path, budget: Budget) -> Path:
    """Persist the ledger to the local ``state_dir`` — the no-CAS fallback path (local dev / dry run
    / no R2). With a CAS backend the durable ledger lives on R2; see :func:`mutate_budget`.
    """
    path = Path(state_dir) / BUDGET_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_serialize(budget))
    return path


# ── CAS-backed durable ledger (H17 PR3) ──────────────────────────────────────────


def _serialize(budget: Budget) -> str:
    return json.dumps(budget.to_dict(), indent=2, sort_keys=True) + "\n"


def storage_supports_cas(storage) -> bool:
    """True if ``storage`` can compare-and-swap the budget key. Use this, not ``hasattr``:
    ``RoutingStorage`` always exposes ``put_cas`` but it only works with an R2 backend attached."""
    return bool(getattr(storage, "cas_capable", False))


def load_budget_cas(storage) -> tuple[Budget, str | None]:
    """Read the durable ledger and its current ETag from the coordination backend.

    Returns ``(budget, etag)``; ``etag`` is ``None`` when the object does not exist yet (a first
    write conditions on ``If-None-Match: *``). A present-but-corrupt object is treated as empty but
    keeps its ETag so the next CAS write replaces it cleanly."""
    got = storage.get_bytes(BUDGET_STATE_KEY)
    if got is None:
        return Budget(), None
    data, etag = got
    try:
        parsed = json.loads(data)
        return Budget.from_dict(parsed if isinstance(parsed, dict) else {}), etag
    except (AttributeError, TypeError, ValueError):
        # Corrupt object (bad JSON *or* valid JSON with a malformed schema) is treated as empty,
        # not fatal — keep the ETag so the next CAS write cleanly replaces it.
        return Budget(), etag


def mutate_budget(
    storage,
    mutate,
    *,
    now: datetime | None = None,
    max_attempts: int = 8,
    base_sleep: float = 0.05,
    max_sleep: float = 1.0,
    sleep=time.sleep,
    rng: random.Random | None = None,
) -> Budget:
    """Atomic read-modify-write of the durable budget via CAS — the overspend guard.

    Loads the freshest ledger + ETag, rolls the allotment month, applies ``mutate(budget)`` (the
    reserve/settle/release), and conditionally writes it back (``If-Match`` on the ETag, or
    ``If-None-Match: *`` for a first write). On a :class:`CASConflict` — a sibling shard committed
    first — it re-reads and retries with bounded exponential backoff + jitter, so concurrent
    reservations serialize and the free-tier cap can't be overspent across shards (review/17 §3/§5).
    Returns the committed ``Budget``. Raises :class:`CASConflict` if it can't commit within
    ``max_attempts``.
    """
    from citypods.storage import CASConflict

    rng = rng or random
    last: CASConflict | None = None
    for attempt in range(max_attempts):
        budget, etag = load_budget_cas(storage)
        budget.roll_month(now)
        mutate(budget)
        body = _serialize(budget).encode()
        try:
            if etag is None:
                storage.put_cas(BUDGET_STATE_KEY, body, "application/json", if_none_match="*")
            else:
                storage.put_cas(BUDGET_STATE_KEY, body, "application/json", if_match=etag)
            return budget
        except CASConflict as exc:  # a sibling wrote first → re-read and retry
            last = exc
            sleep(min(base_sleep * 2**attempt, max_sleep) * (0.5 + rng.random()))
    assert last is not None
    raise last


def reserve_if_available(
    storage,
    owner: str,
    backend: str,
    *,
    est: float,
    cap: float,
    max_inflight: int,
    now: datetime | None = None,
    max_attempts: int = 8,
    base_sleep: float = 0.05,
    max_sleep: float = 1.0,
    sleep=time.sleep,
    rng: random.Random | None = None,
) -> bool:
    """Atomic **check-and-reserve** against the durable ledger: reserve ``est`` GPU-seconds for
    ``owner`` on ``backend`` iff the *freshest* ledger still has cap + an open slot. Returns True if
    reserved, False if not (caller overflows to local).

    The availability check is re-evaluated **on every CAS attempt** against the reloaded ledger, so
    two shards selecting the same backend from a stale snapshot cannot both commit — the loser's
    re-check sees the winner's reservation and refuses, instead of blindly merging both and
    breaching the cap (the overspend race ``mutate_budget`` alone does not close). This keeps the
    free-tier ``$0`` guarantee holding across concurrent shards; callers therefore reserve *before*
    the irreversible remote submit and :func:`release_reservation` on a submit failure.
    """
    from citypods.storage import CASConflict

    rng = rng or random
    for attempt in range(max_attempts):
        budget, etag = load_budget_cas(storage)
        budget.roll_month(now)
        if not budget.available(backend, cap=cap, max_inflight=max_inflight, est=est):
            return False  # authoritative fresh check → no room; no write spent
        budget.reserve(owner, backend, est)
        body = _serialize(budget).encode()
        try:
            if etag is None:
                storage.put_cas(BUDGET_STATE_KEY, body, "application/json", if_none_match="*")
            else:
                storage.put_cas(BUDGET_STATE_KEY, body, "application/json", if_match=etag)
            return True
        except CASConflict:
            sleep(min(base_sleep * 2**attempt, max_sleep) * (0.5 + rng.random()))
    return False  # exhausted retries → treat as no reservation (overflow to local)


def release_reservation(
    storage, owner: str, backend: str, *, now: datetime | None = None, **retry
) -> Budget:
    """Return ``owner``'s reservation to the pool (a submit that failed after a successful
    :func:`reserve_if_available`). CAS read-modify-write; idempotent if already gone."""
    return mutate_budget(storage, lambda b: b.release(owner, backend), now=now, **retry)


def settle_reservation(
    storage,
    owner: str,
    backend: str,
    *,
    actual: float | None = None,
    now: datetime | None = None,
    **retry,
) -> Budget:
    """Settle ``owner``'s completed reservation and free its in-flight slot.

    Pull workers (H14b/H14c) use the Stage-2 work-lease owner as the budget owner. If a
    worker is preempted after reserving, ``compute reconcile`` can settle or release the
    same owner idempotently when it sees the artifact or expired lease.
    """
    return mutate_budget(storage, lambda b: b.settle(owner, backend, actual), now=now, **retry)
