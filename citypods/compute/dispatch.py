"""External-dispatch routing, the live coordinator, and lease reconciliation (H14a).

This is the substrate that turns the H13 ``compute_backend`` seam into an off-runner dispatcher:

* :func:`select_backend` — **fill free tiers first, overflow to ``local``**. Walk the configured
  dispatch backends (Modal, then Beam) in order and pick the first one with remaining free-tier
  budget *and* an open in-flight slot; if none qualify, fall back to the synchronous ``local`` GPU.
* :class:`DispatchCoordinator` — the thread-safe object ``make_compute`` returns under
  ``compute_backend: auto``. ``TranscriptStage`` routes each transcribe/align job through
  :meth:`~DispatchCoordinator.dispatch`. On a dispatch backend it records a **live ``work.json``
  lease** (``lease_owner="modal:<job_id>"`` — the first competitive use of the H5 lease API) and
  decrements the budget; on the ``local`` fallback it returns a synchronous :class:`JobResult` and
  the stage's existing write path is unchanged.
* :func:`reconcile_compute` — run at ``asr.yml`` start. Reaps **expired** leases (a dead worker →
  return its budget + re-queue the item), settles **completed** ones (the artifact landed → free the
  in-flight slot), and leaves still-running ones alone. Content-addressing makes a re-dispatch
  idempotent (the artifact is already present ⇒ no-op).

Until the real Modal/Beam adapters register (H14b/H14c) there are **no** dispatch backends, so
``auto`` overflows every job to ``local`` — behavior-identical to today, no lease taken, no budget
consulted (a fake backend exercises the dispatch path in tests).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from citypods.compute.base import (
    Backend,
    DispatchBackend,
    InferenceJob,
    JobHandle,
    JobResult,
    lease_owner_for,
)
from citypods.compute.budget import (
    Budget,
    load_budget,
    load_budget_cas,
    mutate_budget,
    save_budget,
    storage_supports_cas,
)
from citypods.ops.workqueue import (
    WorkItem,
    is_leased,
    lease,
    load_manifest,
    release,
    save_manifest,
)

# How long a dispatched job may run before a still-held lease is treated as a dead worker and
# reaped. Real serverless-GPU transcription finishes in minutes; this generous default tolerates a
# slow provider while still reclaiming a crashed worker's slot within one daily ``asr.yml`` cycle.
DEFAULT_LEASE_TTL_SECONDS = 6 * 3600

# A manifest item's lease identity: (source_key, episode_uid, work_class).
LeaseKey = tuple[str, str, str]


@dataclass(frozen=True)
class DispatchTarget:
    """A configured dispatch backend + its free-tier caps (from ``defaults.compute_backends``)."""

    backend: DispatchBackend
    monthly_gpu_seconds: float
    max_inflight: int


def select_backend(
    job: InferenceJob,
    *,
    targets: list[DispatchTarget],
    local: Backend,
    budget: Budget,
    now: datetime | None = None,
) -> tuple[Backend, DispatchTarget | None]:
    """Pick the backend for *job*: the first dispatch target with budget + an open slot, else
    *local*. Returns ``(backend, target)``; ``target`` is ``None`` for the ``local`` fallback."""
    now = now or datetime.now(UTC)
    for t in targets:
        est = t.backend.estimate_gpu_seconds(job)
        if budget.available(
            t.backend.name, cap=t.monthly_gpu_seconds, max_inflight=t.max_inflight, est=est
        ):
            return t.backend, t
    return local, None


class DispatchCoordinator:
    """The live dispatch seam: selects a backend, records the lease + budget decrement on dispatch,
    and tracks in-flight work so the same item isn't dispatched twice (within or across runs).

    Thread-safe: ``TranscriptStage`` calls :meth:`dispatch` from a worker pool. The lock guards only
    the cheap selection + bookkeeping (and the cheap remote *submit*) — the synchronous ``local``
    inference, the heavy path, runs **outside** the lock so it stays fully concurrent.
    """

    name = "auto"

    def __init__(
        self,
        *,
        local: Backend,
        targets: list[DispatchTarget],
        budget: Budget,
        state_dir: str | Path,
        storage=None,
        lease_ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS,
    ) -> None:
        self._local = local
        self._targets = list(targets)
        self._state_dir = Path(state_dir)
        self._storage = storage
        # CAS path: the durable ledger lives on R2 and every reserve is an atomic compare-and-swap,
        # so concurrent shards can't overspend the free-tier cap. No-CAS path (local dev / dry run /
        # no R2): keep the prior local-file behavior. When CAS is available, ignore the passed-in
        # local snapshot and read the authoritative ledger from R2.
        self._cas = storage_supports_cas(storage)
        self._budget = load_budget_cas(storage)[0] if self._cas else budget
        self._lease_ttl = lease_ttl_seconds
        self._lock = threading.Lock()
        self._leases: dict[LeaseKey, tuple[str, datetime | None]] = {}

    @property
    def dispatch_enabled(self) -> bool:
        """True if any dispatch backend is configured (false in H14a — overflow-to-local only)."""
        return bool(self._targets)

    def seed_leases(self, items: list[WorkItem], *, now: datetime | None = None) -> None:
        """Adopt the unexpired leases already in ``work.json`` so a job a previous run dispatched
        (and whose worker is still in flight) is not dispatched again this run."""
        now = now or datetime.now(UTC)
        with self._lock:
            for wi in items:
                if wi.lease_owner and is_leased(wi, now=now):
                    self._leases[(wi.source_key, wi.episode_uid, wi.work_class)] = (
                        wi.lease_owner,
                        wi.lease_expires,
                    )

    def is_inflight(
        self, source_key: str, episode_uid: str, work_class: str, *, now: datetime | None = None
    ) -> bool:
        """True if this item already holds an unexpired dispatch lease (skip re-dispatching it)."""
        now = now or datetime.now(UTC)
        with self._lock:
            entry = self._leases.get((source_key, episode_uid, work_class))
        return bool(entry) and entry[1] is not None and entry[1] > now

    @property
    def local_backend(self) -> Backend:
        """The synchronous ``local`` GPU fallback. The stage runs its on-runner ASR path on this
        when :meth:`try_dispatch` declines (overflow to local), so that path is unchanged."""
        return self._local

    @property
    def isolates_inference(self) -> bool:
        return bool(getattr(self._local, "isolates_inference", False))

    def terminate_active(self) -> bool:
        terminate = getattr(self._local, "terminate_active", None)
        return bool(terminate()) if callable(terminate) else False

    def close(self) -> None:
        close = getattr(self._local, "close", None)
        if callable(close):
            close()

    def run_inference(self, job: InferenceJob) -> JobResult | JobHandle:
        """:class:`~citypods.compute.base.Backend`-protocol safety net for callers without a
        ``WorkItem`` (no lease context): run synchronously on ``local``. The lease-aware dispatch
        path is :meth:`try_dispatch`."""
        return self._local.run_inference(job)

    def try_dispatch(
        self, item: WorkItem, job: InferenceJob, *, now: datetime | None = None
    ) -> JobHandle | None:
        """Try to hand *job* (for manifest *item*) to an external backend. Returns its
        :class:`JobHandle` after recording the lease + decrementing the budget (all atomic under the
        lock), or ``None`` when it would **overflow to ``local``** — the caller then runs the
        synchronous on-runner path itself, so that path stays untouched.

        The remote submit is a cheap async hand-off (the GPU runs remotely), so it is kept under the
        lock to make the budget check-and-reserve atomic with it; the heavy synchronous fallback
        runs in the caller, outside the lock, fully concurrent."""
        now = now or datetime.now(UTC)
        with self._lock:
            # Read the authoritative ledger so the availability check sees other shards' spend
            # (CAS path); the no-CAS path uses the in-memory ledger as before.
            if self._cas:
                self._budget = load_budget_cas(self._storage)[0]
                self._budget.roll_month(now)
            backend, target = select_backend(
                job, targets=self._targets, local=self._local, budget=self._budget, now=now
            )
            if target is None:
                return None  # overflow to local — caller owns the synchronous path
            result = backend.run_inference(job)
            if not isinstance(result, JobHandle):
                return None  # a dispatch backend must hand back a JobHandle; treat as no-dispatch
            owner = lease_owner_for(result)
            est = backend.estimate_gpu_seconds(job)
            # Persist the decrement eagerly: a crash mid-run must not lose the spend, or the
            # free-tier cap could be breached on the next run. Leases ride the run-end manifest
            # overlay (work.json is rebuilt from records there).
            if self._cas:
                # Atomic compare-and-swap RMW: concurrent shards serialize, no lost reservation.
                self._budget = mutate_budget(
                    self._storage, lambda b: b.reserve(owner, result.backend, est), now=now
                )
            else:
                self._budget.reserve(owner, result.backend, est)
                save_budget(self._state_dir, self._budget)
            lease(item, owner, ttl_seconds=self._lease_ttl, now=now)
            self._leases[(item.source_key, item.episode_uid, item.work_class)] = (
                owner,
                item.lease_expires,
            )
            return result

    def live_leases(self, *, now: datetime | None = None) -> dict[LeaseKey, tuple[str, datetime]]:
        """The unexpired leases to overlay onto the run-end manifest (so the rebuilt ``work.json``
        keeps in-flight items marked ``running`` instead of resetting them to ``queued``)."""
        now = now or datetime.now(UTC)
        with self._lock:
            return {
                k: (owner, expires)
                for k, (owner, expires) in self._leases.items()
                if expires is not None and expires > now
            }

    def flush(self) -> None:
        """Persist the budget ledger (final write of the run). No-op on the CAS path — every reserve
        already committed atomically to R2."""
        if self._cas:
            return
        with self._lock:
            save_budget(self._state_dir, self._budget)


def _asr_artifact_present(storage, src_key: str, uid: str) -> bool:
    """True if a content-addressed ASR transcript for *uid* is already in the bucket. Mirrors
    ``stages.py``'s ``{uid}-asr-`` filename match (any recipe — presence, not version, is what
    tells reconcile the dispatched job completed)."""
    if storage is None or not hasattr(storage, "list_objects"):
        return False
    name_prefix = f"{uid}-asr-"
    try:
        for key, _ in storage.list_objects(f"transcripts/{src_key}/"):
            fname = key.rsplit("/", 1)[-1]
            if fname.startswith(name_prefix) and fname.endswith(".vtt"):
                return True
    except Exception:  # noqa: BLE001 — a listing failure must not crash reconcile
        return False
    return False


def reconcile_compute(state_dir: str | Path, storage, *, now: datetime | None = None) -> dict:
    """Reap dead workers and settle completed jobs from the persisted ``work.json`` + budget ledger.

    For each leased manifest item:
      * **completed** (its artifact is present in the bucket) → settle the budget (free the slot;
        keep the estimate charged, or swap in ``observed_seconds`` actuals if the worker reported
        them) and clear the lease;
      * **expired** lease with no artifact (the worker died) → return its budget reservation and
        re-queue the item;
      * **still running** (unexpired, no artifact yet) → leave it.

    Persists the budget ledger always and the manifest when a lease changed. Returns per-outcome
    counts for the CLI summary. Idempotent: a second run with no live leases is a no-op."""
    now = now or datetime.now(UTC)
    state_dir = Path(state_dir)
    items = load_manifest(state_dir)
    cas = storage_supports_cas(storage)

    reaped = settled = in_flight = 0
    manifest_changed = False
    # Collect ledger mutations and apply them in one atomic pass (a single CAS RMW on the CAS path),
    # so a concurrent shard's reconcile/dispatch can't lose a settle or release.
    budget_ops: list = []
    for wi in items:
        if not wi.lease_owner:
            continue
        owner = wi.lease_owner
        backend = owner.split(":", 1)[0]
        if _asr_artifact_present(storage, wi.source_key, wi.episode_uid):
            actual = wi.observed_seconds if wi.observed_seconds > 0 else None
            budget_ops.append(lambda b, o=owner, bk=backend, a=actual: b.settle(o, bk, a))
            release(wi)
            wi.state = "done"
            settled += 1
            manifest_changed = True
        elif not is_leased(wi, now=now):
            budget_ops.append(lambda b, o=owner, bk=backend: b.release(o, bk))
            release(wi)
            wi.state = "queued"
            reaped += 1
            manifest_changed = True
        else:
            in_flight += 1

    def _apply(b: Budget) -> None:
        for op in budget_ops:
            op(b)

    if cas:
        mutate_budget(storage, _apply, now=now)
    else:
        budget = load_budget(state_dir)
        budget.roll_month(now)
        _apply(budget)
        save_budget(state_dir, budget)
    if manifest_changed:
        save_manifest(state_dir, items)

    # Stage-2 work-lease ledger reaping (review/18 §4.2): reclaim expired claims and settle done
    # ones, derived from the discovery index — never listing the R2 lease prefix. CAS-only (the
    # ledger lives on R2); a non-CAS backend has no ledger, so this is skipped. Candidates are the
    # not-yet-done transcript-asr items, so the sweep tracks backlog, not the whole catalog.
    leases = {"completed": 0, "requeued": 0, "in_flight": 0}
    if cas:
        from citypods.ops.work_leases import reap as reap_work_leases

        candidates = [
            (wi.source_key, wi.episode_uid)
            for wi in items
            if wi.work_class == "transcript-asr" and wi.state != "done"
        ]
        leases = reap_work_leases(
            storage,
            candidates,
            artifact_present=lambda s, u: _asr_artifact_present(storage, s, u),
            now=now,
        )
    return {"reaped": reaped, "settled": settled, "in_flight": in_flight, "leases": leases}
