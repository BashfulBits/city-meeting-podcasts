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

import tempfile
import threading
import uuid
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
    release_reservation,
    reserve_if_available,
    save_budget,
    storage_supports_cas,
)
from citypods.diarize import has_valid_timed_words
from citypods.ops.workqueue import (
    WORK_CLASSES,
    WorkItem,
    is_leased,
    lease,
    load_manifest,
    release,
    save_manifest,
)

REAPABLE_WORK_CLASSES = frozenset(WORK_CLASSES) - {"audio"}
ASR_WORK_CLASSES = frozenset({"transcript-asr", "transcript-asr-comparison"})
ALIGN_WORK_CLASSES = frozenset({"transcript-align", "provider-transcript-align"})

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
        lock; the heavy synchronous fallback runs in the caller, outside the lock, fully concurrent.

        **CAS path: reserve *before* submitting.** The budget gates the irreversible side effect,
        so we atomically check-and-reserve against the freshest durable ledger first
        (:func:`reserve_if_available`, which re-checks availability on every CAS retry — two shards
        selecting the same backend from a stale snapshot can't both commit). Only on a successful
        reservation do we submit; if the submit fails we release it. This is what keeps the
        free-tier ``$0`` cap holding across concurrent shards (review/17 §5)."""
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
            est = backend.estimate_gpu_seconds(job)

            if self._cas:
                # A locally-minted owner so the reservation can precede the submit (the real job ref
                # is on the returned handle / logs). reserve_if_available is the atomic, fresh check
                # + decrement; a failure here means another shard took the slot → overflow to local.
                owner = f"{target.backend.name}:{uuid.uuid4().hex}"
                if not reserve_if_available(
                    self._storage,
                    owner,
                    target.backend.name,
                    est=est,
                    cap=target.monthly_gpu_seconds,
                    max_inflight=target.max_inflight,
                    now=now,
                ):
                    return None
                try:
                    result = backend.run_inference(job)
                except Exception:
                    release_reservation(self._storage, owner, target.backend.name, now=now)
                    raise
                if not isinstance(result, JobHandle):
                    release_reservation(self._storage, owner, target.backend.name, now=now)
                    return None  # a dispatch backend must return a JobHandle; treat as no-dispatch
                self._budget = load_budget_cas(self._storage)[0]  # refresh the in-memory view
                # KNOWN BOUNDED GAP (H14 go-live): the reservation is durable here, but the
                # owner→item link only becomes durable when work.json is saved at run end (lease
                # overlay). A crash in this window orphans an inflight reservation reconcile can't
                # attribute — it leaks until the monthly roll_month resets the ledger. Acceptable
                # while external dispatch is dormant (no adapter); recovering it (e.g. an
                # owner-encoded source/uid + a reconcile sweep, or unifying with the Stage-2
                # work-lease ledger) is H14b/c work.
            else:
                # Single-process, lock-guarded: no cross-shard race, so submit-then-reserve is safe.
                result = backend.run_inference(job)
                if not isinstance(result, JobHandle):
                    return None
                owner = lease_owner_for(result)
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


def _transcript_artifact_present(
    storage, src_key: str, uid: str, work_classes: set[str] | frozenset[str] | None = None
) -> bool:
    """True if a content-addressed transcript for *uid* is already in the bucket.

    Work-lease keys do not carry the work class, so callers pass the classes represented by their
    manifest candidate(s). The provider-align and ASR lanes use different filename prefixes; using
    the wrong one would make reconcile requeue a completed provider-align lease.
    """
    if storage is None or not hasattr(storage, "list_objects"):
        return False
    classes = set(work_classes or ())
    if not classes:
        # Legacy work.json dispatch leases were ASR-only before provider alignment joined the
        # pull-based lane. Preserve that callback's old behavior when no manifest class is known.
        classes = {"transcript-asr"}
    prefixes: list[str] = []
    if classes & ASR_WORK_CLASSES:
        prefixes.append(f"{uid}-asr-")
    if classes & ALIGN_WORK_CLASSES:
        prefixes.append(f"{uid}-provider-align-")
    if "provider-transcript-diarize" in classes:
        prefixes.append(f"{uid}-provider-diarize-")
    if not prefixes:
        return False
    try:
        for key, _ in storage.list_objects(f"transcripts/{src_key}/"):
            fname = key.rsplit("/", 1)[-1]
            if any(fname.startswith(prefix) for prefix in prefixes) and fname.endswith(".vtt"):
                if classes & (ASR_WORK_CLASSES | ALIGN_WORK_CLASSES):
                    words_key = f"{key[:-4]}.words.json"
                    if not storage.exists(words_key) or not hasattr(storage, "get_file"):
                        continue
                    with tempfile.TemporaryDirectory() as td:
                        words_path = Path(td) / "words.json"
                        if not storage.get_file(words_key, words_path):
                            continue
                        if not has_valid_timed_words(words_path.read_bytes()):
                            continue
                return True
    except Exception:  # noqa: BLE001 — a listing failure must not crash reconcile
        return False
    return False


def _asr_artifact_present(storage, src_key: str, uid: str) -> bool:
    """True if a content-addressed ASR transcript for *uid* is already in the bucket."""
    return _transcript_artifact_present(storage, src_key, uid, ASR_WORK_CLASSES)


def reconcile_compute(
    state_dir: str | Path,
    storage,
    *,
    now: datetime | None = None,
    sweep_work_leases: bool = False,
    use_lease_index: bool = True,
) -> dict:
    """Reap dead workers and settle completed jobs from the persisted ``work.json`` + budget ledger.

    For each leased manifest item:
      * **completed** (its artifact is present in the bucket) → settle the budget (free the slot;
        keep the estimate charged, or swap in ``observed_seconds`` actuals if the worker reported
        them) and clear the lease;
      * **expired** lease with no artifact (the worker died) → return its budget reservation and
        re-queue the item;
      * **still running** (unexpired, no artifact yet) → leave it.

    Persists the budget ledger always and the manifest when a lease changed. Returns per-outcome
    counts for the CLI summary. Idempotent: a second run with no live leases is a no-op.

    ``sweep_work_leases`` gates the Stage-2 work-lease ledger sweep (review/18 §4.2). It is **off by
    default** because that ledger is *dormant* until external pull workers (H14b/H14c) claim against
    it: with no claims, sweeping means nothing to reap either way — the reaper is lossless to skip
    while dormant, so deployments enable it (``work_lease_reaper_enabled``) only once external
    workers exist.

    ``use_lease_index`` picks which sweep runs once ``sweep_work_leases`` is on: the GH#1018
    active-lease index (:func:`work_leases.reap_indexed`, default) reads a bounded number of index
    objects instead of GETting every pending transcript candidate, with a rotating one-run
    integrity-sweep partition to recover from a crash between a claim and its index write. Set to
    ``False`` to fall back to the original :func:`work_leases.reap` candidate-probe (``config``
    rollback path, no code change needed). All transcript work classes are included so a provider-
    alignment worker's lease is reconciled just like a fresh-ASR worker's lease."""
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
        if _transcript_artifact_present(storage, wi.source_key, wi.episode_uid, {wi.work_class}):
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

    # Stage-2 work-lease ledger reaping (review/18 §4.2, GH#1018): reclaim expired claims and settle
    # done ones, derived from the discovery index — never listing the R2 lease prefix. CAS-only (the
    # ledger lives on R2); a non-CAS backend has no ledger. Gated behind ``sweep_work_leases``
    # because the ledger is dormant until external pull workers exist (see the docstring).
    # Candidates are the not-yet-done transcript items, so the sweep tracks backlog, not all
    # records. This must include provider-align: the scheduled matrix claims that class too.
    leases: dict = {"completed": 0, "requeued": 0, "in_flight": 0}
    if cas and sweep_work_leases:
        from citypods.compute.budget import release_reservation, settle_reservation
        from citypods.ops import work_leases
        from citypods.ops.work_leases import reap as reap_work_leases
        from citypods.ops.work_leases import reap_indexed as reap_work_leases_indexed

        def _backend(owner: str) -> str:
            return owner.split(":", 1)[0]

        candidates = [
            (wi.source_key, wi.episode_uid)
            for wi in items
            if wi.work_class in REAPABLE_WORK_CLASSES and wi.state != "done"
        ]
        work_classes_by_key: dict[tuple[str, str], set[str]] = {}
        for wi in items:
            if wi.work_class in REAPABLE_WORK_CLASSES and wi.state != "done":
                work_classes_by_key.setdefault((wi.source_key, wi.episode_uid), set()).add(
                    wi.work_class
                )

        def artifact_present(source_key: str, uid: str) -> bool:
            return _transcript_artifact_present(
                storage, source_key, uid, work_classes_by_key.get((source_key, uid))
            )

        def on_completed(owner: str) -> None:
            settle_reservation(storage, owner, _backend(owner), now=now)

        def on_requeued(owner: str) -> None:
            release_reservation(storage, owner, _backend(owner), now=now)

        if use_lease_index:
            # One bounded partition of the candidate keyspace per run — rotated per minute so the
            # full backlog is eventually re-checked (review/18 §4.2 / GH#1018 acceptance criterion
            # 4) without ever probing more than a fixed slice in a single reconcile.
            leases = reap_work_leases_indexed(
                storage,
                artifact_present=artifact_present,
                on_completed=on_completed,
                on_requeued=on_requeued,
                now=now,
                integrity_candidates=candidates,
                integrity_partition=work_leases.integrity_partition_for(now),
            )
        else:
            leases = reap_work_leases(
                storage,
                candidates,
                artifact_present=artifact_present,
                on_completed=on_completed,
                on_requeued=on_requeued,
                now=now,
            )
    return {"reaped": reaped, "settled": settled, "in_flight": in_flight, "leases": leases}


def requeue_failed_work_leases(
    state_dir: str | Path,
    storage,
    *,
    work_class: str,
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Explicitly reopen failed leases for one manifest work class.

    This is an operator recovery path for a transient incident that exhausted a worker's retries.
    It is deliberately separate from :func:`reconcile_compute`: normal reconciliation must not
    silently retry poison items that a worker marked failed.
    """
    if work_class not in REAPABLE_WORK_CLASSES:
        raise ValueError(f"unsupported requeue work class: {work_class!r}")
    if not storage_supports_cas(storage):
        raise RuntimeError("failed work-lease recovery requires CAS-capable storage")
    candidates = [
        (wi.source_key, wi.episode_uid)
        for wi in load_manifest(state_dir)
        if wi.work_class == work_class and wi.state != "done"
    ]
    from citypods.ops import work_leases

    return work_leases.requeue_failed(storage, candidates, now=now, dry_run=dry_run)
