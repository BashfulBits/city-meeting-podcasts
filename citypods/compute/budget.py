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

import calendar
import json
import random
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

BUDGET_NAME = "compute_budget.json"
BUDGET_SCHEMA_VERSION = 3

# Durable key on the coordination backend. The ledger needs an atomic decrement (the overspend
# guard) so it lives on R2 and is read/written by compare-and-swap, **not** the bulk B2 state sync
# (``statesync`` excludes coordination keys). Must match a ``storage.routing.COORDINATION_PREFIXES``
# entry so ``RoutingStorage`` routes it to R2.
BUDGET_STATE_KEY = "state/compute_budget.json"


def month_key(now: datetime | None = None) -> str:
    """The ``YYYY-MM`` allotment key for *now* (UTC). Backends reset when this rolls over."""
    now = now or datetime.now(UTC)
    return now.strftime("%Y-%m")


def cycle_key(now: datetime | None = None, *, rollover_day_of_month: int = 1) -> str:
    """Return the provider-cycle key for a backend whose credits restore on
    ``rollover_day_of_month``.

    The key is the UTC date of the current cycle start (`YYYY-MM-DD`). If *now* is before this
    month's rollover day, the cycle started on the prior month's rollover date.
    """
    now = now or datetime.now(UTC)
    rollover = max(1, min(31, int(rollover_day_of_month)))

    def _anchor(year: int, month: int) -> datetime:
        last_day = calendar.monthrange(year, month)[1]
        day = min(rollover, last_day)
        return datetime(year, month, day, tzinfo=UTC)

    current_anchor = _anchor(now.year, now.month)
    if now >= current_anchor:
        return current_anchor.strftime("%Y-%m-%d")
    if now.month == 1:
        return _anchor(now.year - 1, 12).strftime("%Y-%m-%d")
    return _anchor(now.year, now.month - 1).strftime("%Y-%m-%d")


@dataclass
class RuntimeEstimate:
    """Learned runtime model for one `(backend, task, hardware, model, compute)` profile."""

    seconds_per_audio_second: float
    samples: int = 0
    updated_at: str | None = None
    last_estimated_seconds: float | None = None
    last_actual_seconds: float | None = None

    def observe(
        self,
        *,
        audio_seconds: float,
        estimated_seconds: float | None,
        actual_seconds: float,
        now: datetime | None = None,
    ) -> None:
        if audio_seconds <= 0 or actual_seconds <= 0:
            return
        observed_ratio = actual_seconds / audio_seconds
        # Exponential smoothing with a bounded floor on history so one outlier does not dominate,
        # while still letting the coefficient move as provider/runtime behavior drifts.
        alpha = 0.3 if self.samples < 10 else 0.2
        self.seconds_per_audio_second = max(
            0.0,
            (1.0 - alpha) * self.seconds_per_audio_second + alpha * observed_ratio,
        )
        self.samples += 1
        self.updated_at = (now or datetime.now(UTC)).isoformat()
        self.last_estimated_seconds = estimated_seconds
        self.last_actual_seconds = actual_seconds


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
    cycle_key: str = ""

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
    runtime_estimates: dict[str, RuntimeEstimate] = field(default_factory=dict)

    def _cycle_matches(self, existing: str, target: str) -> bool:
        if existing == target:
            return True
        # Legacy ledgers stored only YYYY-MM. Treat the migrated day-1 provider-cycle key as the
        # same cycle so the first read under the new schema does not zero live state.
        return len(existing) == 7 and target == f"{existing}-01"

    def _ledger(self, backend: str, *, cycle: str | None = None) -> BackendLedger:
        led = self.backends.setdefault(backend, BackendLedger())
        target_cycle = cycle or self.month
        if led.cycle_key and not self._cycle_matches(led.cycle_key, target_cycle):
            led.used_units = 0.0
            led.inflight = {}
        led.cycle_key = target_cycle
        return led

    def current_ledger(self, backend: str, *, cycle: str | None = None) -> BackendLedger:
        """Public read path for *backend*'s ledger, applying the same stale-cycle reset check
        ``available``/``reserve``/``settle``/``release`` apply on every real dispatch.

        Diagnostics (the ASR worker report) load a ``Budget`` straight off storage and — unlike
        dispatch — never call those methods, so they'd otherwise print whatever total was left
        over from the last time this backend was actually touched, mislabeled as the current
        cycle. A backend dispatched only rarely (e.g. Modal's even-day-only, 4h+-only schedule)
        can go a long time between touches, so that stale total can be wildly larger than its real
        current-cycle spend. Calling this first makes the reported figures match what the next
        real dispatch attempt would see."""
        return self._ledger(backend, cycle=cycle)

    def roll_month(self, now: datetime | None = None) -> bool:
        """Advance the schema's month marker when the UTC month rolls over.

        Per-backend spend is no longer reset here; provider-cycle resets are keyed from each
        backend's explicit cycle anchor in :meth:`_ledger`.
        """
        mk = month_key(now)
        if mk == self.month:
            return False
        self.month = mk
        return True

    def available(
        self,
        backend: str,
        *,
        cap: float,
        max_inflight: int,
        est: float,
        cycle: str | None = None,
    ) -> bool:
        """True if *backend* can take a job estimated at *est* GPU-seconds without breaching its
        free-tier cap or its in-flight slot limit. The gate every reservation passes through."""
        led = self._ledger(backend, cycle=cycle)
        return led.used_units + est <= cap and led.inflight_count < max_inflight

    def reserve(self, owner: str, backend: str, est: float, *, cycle: str | None = None) -> None:
        """Decrement-on-dispatch: charge *est* GPU-seconds to *backend* against owner *owner*."""
        led = self._ledger(backend, cycle=cycle)
        led.used_units += est
        led.inflight[owner] = est

    def settle(
        self,
        owner: str,
        backend: str,
        actual: float | None = None,
        *,
        cycle: str | None = None,
        estimate_key: str | None = None,
        audio_seconds: float | None = None,
        actual_runtime_seconds: float | None = None,
        estimated_runtime_seconds: float | None = None,
        now: datetime | None = None,
    ) -> None:
        """Reconcile a **completed** job and free its in-flight slot. With a worker-reported
        *actual* GPU-seconds, swap the estimate for it (actuals are ≤ a conservative estimate, so
        ``used`` can only drop). With ``actual=None`` (no actuals channel yet — H14b/c) the estimate
        stays charged: a job that *ran* must never return budget, or the cap could be overspent.
        Idempotent / no-op if the reservation is already gone (e.g. the month rolled)."""
        led = self._ledger(backend, cycle=cycle)
        reserved = led.inflight.pop(owner, None)
        if reserved is None:
            return
        if actual is not None:
            led.used_units = max(0.0, led.used_units - reserved + actual)
        if (
            estimate_key
            and isinstance(audio_seconds, (int, float))
            and isinstance(actual_runtime_seconds, (int, float))
        ):
            self.observe_runtime(
                estimate_key,
                audio_seconds=float(audio_seconds),
                actual_runtime_seconds=float(actual_runtime_seconds),
                estimated_runtime_seconds=(
                    float(estimated_runtime_seconds)
                    if isinstance(estimated_runtime_seconds, (int, float))
                    else None
                ),
                now=now,
            )

    def release(self, owner: str, backend: str, *, cycle: str | None = None) -> None:
        """Reap-on-expiry: return a **dead** worker's reservation to the pool (it never ran). No-op
        if already gone. Distinct from :meth:`settle` — only work that did *not* run returns
        budget."""
        led = self._ledger(backend, cycle=cycle)
        reserved = led.inflight.pop(owner, None)
        if reserved is not None:
            led.used_units = max(0.0, led.used_units - reserved)

    def runtime_seconds_per_audio_second(self, key: str, default: float) -> float:
        estimate = self.runtime_estimates.get(key)
        if estimate is None:
            return default
        return max(0.0, float(estimate.seconds_per_audio_second))

    def observe_runtime(
        self,
        key: str,
        *,
        audio_seconds: float,
        actual_runtime_seconds: float,
        estimated_runtime_seconds: float | None = None,
        default_seconds_per_audio_second: float | None = None,
        now: datetime | None = None,
    ) -> None:
        estimate = self.runtime_estimates.get(key)
        if estimate is None:
            estimate = RuntimeEstimate(
                seconds_per_audio_second=max(
                    0.0,
                    default_seconds_per_audio_second
                    if default_seconds_per_audio_second is not None
                    else (actual_runtime_seconds / audio_seconds if audio_seconds > 0 else 0.0),
                )
            )
            self.runtime_estimates[key] = estimate
        estimate.observe(
            audio_seconds=audio_seconds,
            estimated_seconds=estimated_runtime_seconds,
            actual_seconds=actual_runtime_seconds,
            now=now,
        )

    # ── serialization ──────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "schema_version": BUDGET_SCHEMA_VERSION,
            "month": self.month,
            "backends": {
                name: {
                    "used_units": led.used_units,
                    "inflight": dict(led.inflight),
                    "cycle_key": led.cycle_key,
                }
                for name, led in self.backends.items()
            },
            "runtime_estimates": {
                key: {
                    "seconds_per_audio_second": estimate.seconds_per_audio_second,
                    "samples": estimate.samples,
                    "updated_at": estimate.updated_at,
                    "last_estimated_seconds": estimate.last_estimated_seconds,
                    "last_actual_seconds": estimate.last_actual_seconds,
                }
                for key, estimate in self.runtime_estimates.items()
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
                cycle_key=str(raw.get("cycle_key") or ""),
            )
        runtime_estimates: dict[str, RuntimeEstimate] = {}
        for key, raw in (data.get("runtime_estimates") or {}).items():
            if not isinstance(raw, dict):
                continue
            runtime_estimates[str(key)] = RuntimeEstimate(
                seconds_per_audio_second=float(raw.get("seconds_per_audio_second", 0.0)),
                samples=int(raw.get("samples", 0)),
                updated_at=raw.get("updated_at"),
                last_estimated_seconds=(
                    float(raw["last_estimated_seconds"])
                    if raw.get("last_estimated_seconds") is not None
                    else None
                ),
                last_actual_seconds=(
                    float(raw["last_actual_seconds"])
                    if raw.get("last_actual_seconds") is not None
                    else None
                ),
            )
        return cls(
            month=data.get("month") or month_key(),
            backends=backends,
            runtime_estimates=runtime_estimates,
        )


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
    cycle: str | None = None,
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
        if not budget.available(
            backend,
            cap=cap,
            max_inflight=max_inflight,
            est=est,
            cycle=cycle,
        ):
            return False  # authoritative fresh check → no room; no write spent
        budget.reserve(owner, backend, est, cycle=cycle)
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
    storage,
    owner: str,
    backend: str,
    *,
    cycle: str | None = None,
    now: datetime | None = None,
    **retry,
) -> Budget:
    """Return ``owner``'s reservation to the pool (a submit that failed after a successful
    :func:`reserve_if_available`). CAS read-modify-write; idempotent if already gone."""
    return mutate_budget(
        storage,
        lambda b: b.release(owner, backend, cycle=cycle),
        now=now,
        **retry,
    )


def settle_reservation(
    storage,
    owner: str,
    backend: str,
    *,
    actual: float | None = None,
    cycle: str | None = None,
    estimate_key: str | None = None,
    audio_seconds: float | None = None,
    actual_runtime_seconds: float | None = None,
    estimated_runtime_seconds: float | None = None,
    now: datetime | None = None,
    **retry,
) -> Budget:
    """Settle ``owner``'s completed reservation and free its in-flight slot.

    Pull workers (H14b/H14c) use the Stage-2 work-lease owner as the budget owner. If a
    worker is preempted after reserving, ``compute reconcile`` can settle or release the
    same owner idempotently when it sees the artifact or expired lease.
    """
    return mutate_budget(
        storage,
        lambda b: b.settle(
            owner,
            backend,
            actual,
            cycle=cycle,
            estimate_key=estimate_key,
            audio_seconds=audio_seconds,
            actual_runtime_seconds=actual_runtime_seconds,
            estimated_runtime_seconds=estimated_runtime_seconds,
            now=now,
        ),
        now=now,
        **retry,
    )


def observe_runtime_estimate(
    storage,
    key: str,
    *,
    audio_seconds: float,
    actual_runtime_seconds: float,
    estimated_runtime_seconds: float | None = None,
    default_seconds_per_audio_second: float | None = None,
    now: datetime | None = None,
    **retry,
) -> Budget:
    """Persist a runtime observation without creating a reservation.

    Internal pull workers use the same learned runtime-estimate substrate as external workers, but
    they do not reserve provider budget. This hook updates only ``runtime_estimates`` in the CAS
    ledger, leaving the per-backend spend/inflight ledgers untouched.
    """
    return mutate_budget(
        storage,
        lambda b: b.observe_runtime(
            key,
            audio_seconds=audio_seconds,
            actual_runtime_seconds=actual_runtime_seconds,
            estimated_runtime_seconds=estimated_runtime_seconds,
            default_seconds_per_audio_second=default_seconds_per_audio_second,
            now=now,
        ),
        now=now,
        **retry,
    )
