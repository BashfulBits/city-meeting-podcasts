"""B2-resident registry of pending/completed policy-bearing LLM requests (R13).

Unlike the CAS quota ledger (``llm_budget.py``), this is not contended coordination state: only
the original caller ever writes a "pending" record, and only the sweep (or a later call for the
same ``recipe_hash``) ever writes a "completed" one -- so it lives on the primary (B2) backend as
a plain, listable record, not an R2 CAS object. Listability is required: the sweep discovers
pending records without knowing recipe hashes in advance, and R2 coordination prefixes are
explicitly never listed in this codebase (``storage/routing.py``).

This is what makes Mistral-dispatch and deferred-direct requests look identical to a caller: both
produce a ``JobHandle``, both get written here, and a caller that just calls ``run_inference()``
again with the *same job* later transparently gets back whatever the sweep has completed in the
meantime -- no need to hold onto or explicitly reconcile a handle itself.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from citypods.compute.base import JobHandle, JobResult
from citypods.compute.llm_policy import DeferredLLMRequest, LLMRequestPolicy

DEFERRED_PREFIX = "state/llm_deferred/"
DEFERRED_INDEX_PREFIX = "state/llm_deferred_index/"
DEFERRED_INDEX_PENDING_PREFIX = f"{DEFERRED_INDEX_PREFIX}pending/"
DEFERRED_INDEX_MIGRATION_KEY = f"{DEFERRED_INDEX_PREFIX}migration-complete.json"
DEFERRED_FAILURE_PREFIX = "state/llm_deferred_failures/"

# A malformed terminal response is worth retrying: providers occasionally produce a bad JSON
# object or fail an individual request.  It must not, however, turn every future producer pass
# into an unbounded loop for the same immutable recipe.  Keep one compact audit record per recipe
# and stop automatic re-submission after this many terminal failures.  A recipe/input-version
# change has a new hash and starts a fresh lineage; an operator can also clear the one audit object
# when a route-level incident is resolved.
MAX_TERMINAL_FAILURE_RETRIES = 3

# Worst case a record should ever need to sit here: a caller with no deadline_at (or a caller
# that never asks again, like a Stage whose own recipe_hash changes run to run) waiting out a
# full monthly cost-cycle reset (review/33 §10's cost_cycle_key -- up to ~31 days if the cap was
# hit right after a rollover) plus slack for the sweep's own daily cadence. `prune_expired_deferred`
# never deletes a record before this *or* the caller's own `deadline_at`, whichever is later.
DEFAULT_TTL_DAYS = 38
# Failure markers are audit records rather than live work, but must not grow indefinitely for
# recipe lineages which are never revisited. Maintenance passes prune only markers whose latest
# failure is older than this retention period.
DEFAULT_FAILURE_MARKER_TTL_DAYS = 90


@dataclass
class DeferredSnapshotEntry:
    """One decoded registry object retained by a sweep snapshot."""

    key: str
    last_modified: Any
    data: Mapping[str, Any] | None
    decoded: JobResult | JobHandle | None
    deleted: bool = False


@dataclass
class DeferredSnapshot:
    """A single LIST plus body reads of the deferred registry.

    The sweep mutates ``decoded`` as reconciliation completes, so its final pending count does
    not require downloading the registry again.  ``data`` remains the original body for TTL and
    reservation-cleanup decisions.
    """

    entries: list[DeferredSnapshotEntry]

    def pending(self):
        for entry in self.entries:
            if not entry.deleted and isinstance(entry.decoded, JobHandle):
                yield entry.decoded

    def mark_completed(self, recipe_hash: str, result: JobResult) -> None:
        for entry in self.entries:
            if isinstance(entry.decoded, JobHandle) and entry.decoded.recipe_hash == recipe_hash:
                entry.decoded = result
                if isinstance(entry.data, Mapping):
                    entry.data = {**entry.data, "status": "completed"}
                return

    def mark_terminal_failure(self, recipe_hash: str) -> DeferredSnapshotEntry | None:
        """Remove a terminally unusable handle from this snapshot.

        The caller persists a compact failure marker and deletes the canonical pending record.
        Keeping the snapshot in sync is important: the end-of-run ``remaining`` count must not
        report a record which this same sweep deliberately made eligible for a clean resubmit.
        """
        for entry in self.entries:
            if isinstance(entry.decoded, JobHandle) and entry.decoded.recipe_hash == recipe_hash:
                entry.deleted = True
                return entry
        return None

    def replace_pending(self, recipe_hash: str, handle: JobHandle) -> bool:
        """Replace one pending reference after a successful in-sweep corrective retry."""
        for entry in self.entries:
            if isinstance(entry.decoded, JobHandle) and entry.decoded.recipe_hash == recipe_hash:
                entry.decoded = handle
                entry.data = None
                return True
        return False


def deferred_key(recipe_hash: str) -> str:
    return f"{DEFERRED_PREFIX}{recipe_hash}.json"


def deferred_failure_key(recipe_hash: str) -> str:
    """The compact, per-recipe terminal-failure audit object."""
    return f"{DEFERRED_FAILURE_PREFIX}{recipe_hash}.json"


def _index_models(data: Mapping[str, Any]) -> tuple[str, ...]:
    """Return configured route keys a pending record may select.

    A policy record is deliberately indexed in every candidate route partition.  The pointer is
    only a cheap prefilter; the canonical record and scheduler still perform the authoritative
    policy/transport/capacity checks after the pointer is read.
    """
    if data.get("status") != "pending":
        return ()
    from citypods.compute.llm_policy import ROUTES, canonical_model

    resolved = data.get("model")
    if isinstance(resolved, str):
        resolved = canonical_model(resolved)
        if resolved in ROUTES:
            return (resolved,)
    policy = data.get("policy")
    allowed = policy.get("allowed_models") if isinstance(policy, Mapping) else None
    candidates = (
        {canonical_model(model) for model in allowed}
        if isinstance(allowed, list | tuple)
        else set(ROUTES)
    )
    allow_paid = bool(policy.get("allow_paid", False)) if isinstance(policy, Mapping) else False
    return tuple(
        sorted(
            model
            for model, route in ROUTES.items()
            if model in candidates and (allow_paid or route.free)
        )
    )


def _index_keys(data: Mapping[str, Any], *, recipe_hash: str) -> tuple[str, ...]:
    """Pointer keys for every route ``data`` could currently be reconciled against.

    No time-bucket layer: nothing in this codebase computes a genuine future retry/backoff time
    for a persisted record today (route pacing/backoff lives in-memory for the duration of one
    sweep -- ``llm_scheduler.Selection.retry_at`` -- and is never written back to the registry),
    so every pending record is always "due now." Partitioning by day would only add one LIST per
    (model, day) with nothing to skip, which is pure overhead. If a real scheduled-retry feature
    is added later, bucket by its actual due time then, not preemptively.
    """
    return tuple(
        f"{DEFERRED_INDEX_PENDING_PREFIX}{model}/{recipe_hash}.json"
        for model in _index_models(data)
    )


def _delete_pointer_keys(storage, keys) -> None:
    for key in keys:
        try:
            storage.delete(key)
        except Exception:  # noqa: BLE001 -- an advisory index never blocks canonical state
            pass


def _write_pointer_keys(storage, keys, recipe_hash: str) -> None:
    body = (json.dumps({"recipe_hash": recipe_hash}) + "\n").encode()
    for key in keys:
        try:
            _write_json(storage, key, body)
        except Exception:  # noqa: BLE001 -- canonical state is authoritative
            pass


def _best_effort_delete_index(storage, data: Mapping[str, Any], recipe_hash: str) -> None:
    _delete_pointer_keys(storage, _index_keys(data, recipe_hash=recipe_hash))


def terminal_failure_retry_allowed(storage, recipe_hash: str) -> bool:
    """Whether another fresh submission is allowed for this recipe lineage.

    The marker is deliberately separate from the canonical deferred record so a failed handle no
    longer looks pending to the sweep or to ``look_up_deferred``.  Malformed old records are
    ignored rather than blocking work.
    """
    data = _read_json(storage, deferred_failure_key(recipe_hash))
    if not isinstance(data, Mapping):
        return True
    if data.get("status") == "exhausted":
        return False
    try:
        return int(data.get("failure_count", 0)) < MAX_TERMINAL_FAILURE_RETRIES
    except (TypeError, ValueError):
        return True


def schema_correction_attempted(storage, recipe_hash: str) -> bool:
    """Whether this exact recipe has already received its one corrective retry."""
    data = _read_json(storage, deferred_failure_key(recipe_hash))
    return bool(isinstance(data, Mapping) and data.get("schema_correction_attempted"))


def prune_expired_failure_markers(storage, *, now: datetime | None = None) -> int:
    """Delete terminal-failure audit markers beyond their bounded retention period."""
    current = now or datetime.now(UTC)
    deleted = 0
    try:
        listing = storage.list_objects(DEFERRED_FAILURE_PREFIX)
    except Exception:  # noqa: BLE001 -- audit cleanup must never block a sweep
        return 0
    for key, _ in listing:
        data = _read_json(storage, key)
        if not isinstance(data, Mapping):
            continue
        raw = data.get("last_failed_at")
        try:
            last_failed_at = datetime.fromisoformat(raw) if isinstance(raw, str) else None
        except ValueError:
            last_failed_at = None
        if last_failed_at is None or current <= last_failed_at + timedelta(
            days=DEFAULT_FAILURE_MARKER_TTL_DAYS
        ):
            continue
        try:
            storage.delete(key)
        except Exception:  # noqa: BLE001 -- maintenance cleanup remains best-effort
            continue
        deleted += 1
    return deleted


def record_schema_correction(storage, handle: JobHandle, error: BaseException, *, now=None) -> None:
    """Record the one schema-correction attempt without blocking its new handle."""
    now = now or datetime.now(UTC)
    prior = _read_json(storage, deferred_failure_key(handle.recipe_hash))
    marker = {
        "recipe_hash": handle.recipe_hash,
        "task": handle.task,
        "failure_count": prior.get("failure_count", 0) if isinstance(prior, Mapping) else 0,
        "first_failed_at": (
            prior.get("first_failed_at")
            if isinstance(prior, Mapping) and isinstance(prior.get("first_failed_at"), str)
            else now.isoformat()
        ),
        "last_failed_at": now.isoformat(),
        "last_error_type": type(error).__name__,
        "last_error": str(error).replace("\n", " ")[:500],
        "schema_correction_attempted": True,
        "status": "corrective_retry_submitted",
    }
    _write_json(
        storage,
        deferred_failure_key(handle.recipe_hash),
        (json.dumps(marker, indent=2, sort_keys=True) + "\n").encode(),
    )


def discard_terminal_failure(
    storage,
    snapshot: DeferredSnapshot,
    handle: JobHandle,
    error: BaseException,
    *,
    backend=None,
    exhausted: bool = False,
    now: datetime | None = None,
) -> int:
    """Persist one bounded audit marker and remove a terminally bad pending handle.

    This is intentionally limited to errors whose terminality is established by the Worker
    (failed dispatch record) or local validation (a completed response violating its contract).
    A transient transport failure must stay pending for the ordinary retry path.
    """
    entry = snapshot.mark_terminal_failure(handle.recipe_hash)
    if entry is None or not isinstance(entry.data, Mapping):
        return 0
    # Do not delete a newer record a producer may have written after this snapshot was loaded.
    if _read_json(storage, entry.key) != entry.data:
        entry.deleted = False
        return 0
    now = now or datetime.now(UTC)
    prior = _read_json(storage, deferred_failure_key(handle.recipe_hash))
    try:
        prior_count = int(prior.get("failure_count", 0)) if isinstance(prior, Mapping) else 0
    except (TypeError, ValueError):
        prior_count = 0
    count = max(0, prior_count) + 1
    # Adapter exceptions intentionally omit model output/provider bodies.  Cap the stored reason
    # anyway: this is durable operational metadata, not a transcript/error dump.
    reason = str(error).replace("\n", " ")[:500]
    marker = {
        "recipe_hash": handle.recipe_hash,
        "task": handle.task,
        "failure_count": count,
        "first_failed_at": (
            prior.get("first_failed_at")
            if isinstance(prior, Mapping) and isinstance(prior.get("first_failed_at"), str)
            else now.isoformat()
        ),
        "last_failed_at": now.isoformat(),
        "last_error_type": type(error).__name__,
        "last_error": reason,
        "schema_correction_attempted": bool(
            isinstance(prior, Mapping) and prior.get("schema_correction_attempted")
        ),
        "status": (
            "exhausted" if exhausted or count >= MAX_TERMINAL_FAILURE_RETRIES else "retryable"
        ),
    }
    # Audit first: a storage interruption can leave a stale handle to retry, but cannot make the
    # terminal failure disappear without a record of why it was removed.
    _write_json(
        storage,
        deferred_failure_key(handle.recipe_hash),
        (json.dumps(marker, indent=2, sort_keys=True) + "\n").encode(),
    )
    if backend is not None and handle.ref:
        try:
            backend.delete_dispatched_ref(handle.ref)
        except Exception:  # noqa: BLE001 -- stale Worker state must not retain the B2 handle
            pass
    storage.delete(entry.key)
    _best_effort_delete_index(storage, entry.data, handle.recipe_hash)
    return count


def _write_index(storage, data: Mapping[str, Any], recipe_hash: str) -> None:
    _write_pointer_keys(storage, _index_keys(data, recipe_hash=recipe_hash), recipe_hash)


def _index_ready(storage) -> bool:
    try:
        return bool(storage.exists(DEFERRED_INDEX_MIGRATION_KEY))
    except Exception:  # noqa: BLE001 -- old/local storage doubles may lack exists
        return False


def _serialize_policy(policy: LLMRequestPolicy) -> dict[str, Any]:
    return {
        "allowed_models": (
            list(policy.allowed_models) if policy.allowed_models is not None else None
        ),
        "allow_paid": policy.allow_paid,
        "deadline_at": policy.deadline_at.isoformat() if policy.deadline_at is not None else None,
        "purpose": policy.purpose,
        "timeout_class": policy.timeout_class,
        "queue_only": policy.queue_only,
    }


def _deserialize_policy(data: Mapping[str, Any]) -> LLMRequestPolicy:
    allowed = data.get("allowed_models")
    deadline = data.get("deadline_at")
    return LLMRequestPolicy(
        allowed_models=tuple(allowed) if allowed is not None else None,
        allow_paid=bool(data.get("allow_paid", False)),
        deadline_at=datetime.fromisoformat(deadline) if deadline else None,
        purpose=str(data.get("purpose", "")),
        timeout_class=("fast" if data.get("timeout_class") == "fast" else "long"),
        queue_only=bool(data.get("queue_only", False)),
    )


def _record_for(result: JobResult | JobHandle) -> dict[str, Any]:
    if isinstance(result, JobResult):
        return {
            "status": "completed",
            "task": result.task,
            "recipe_hash": result.recipe_hash,
            "output": result.output,
            "model": result.model,
        }
    record: dict[str, Any] = {
        "status": "pending",
        "task": result.task,
        "recipe_hash": result.recipe_hash,
        "backend": result.backend,
        "ref": result.ref,
        "structured_output": result.structured_output,
        "model": result.model,
        "route_id": result.route_id,
        "owner": result.owner,
        "input_per_token": result.input_per_token,
        "output_per_token": result.output_per_token,
        "attempted_requests": result.attempted_requests,
    }
    deferred = result.deferred_request
    if isinstance(deferred, DeferredLLMRequest):
        record["messages"] = [dict(m) for m in deferred.messages]
        record["policy"] = _serialize_policy(deferred.policy)
    return record


def _decode_record(data: Any) -> JobResult | JobHandle | None:
    """``None`` for anything that isn't a well-formed record -- a missing required field or an
    unparseable policy must not raise, since one corrupt record must not abort a caller iterating
    the whole registry (``list_pending_deferred``, the sweep)."""
    if not isinstance(data, Mapping):
        return None
    status = data.get("status")
    try:
        if status == "completed":
            return JobResult(
                task=data["task"],
                recipe_hash=data["recipe_hash"],
                output=data.get("output"),
                model=data.get("model"),
            )
        if status == "pending":
            # `None` for a record predating this field; a non-negative int for a real one. Any
            # other persisted shape (a corrupt write, or a future format this code doesn't know
            # about) isolates the whole record as corrupt rather than letting a bad value reach
            # ledger settlement math.
            attempted_requests = data.get("attempted_requests")
            if attempted_requests is not None and (
                type(attempted_requests) is not int or attempted_requests < 0
            ):
                return None
            deferred = None
            if "messages" in data and "policy" in data:
                deferred = DeferredLLMRequest(
                    messages=tuple(data["messages"]), policy=_deserialize_policy(data["policy"])
                )
            return JobHandle(
                task=data["task"],
                recipe_hash=data["recipe_hash"],
                backend=data.get("backend", ""),
                ref=data.get("ref", ""),
                structured_output=data.get("structured_output"),
                model=data.get("model"),
                route_id=data.get("route_id"),
                owner=data.get("owner"),
                input_per_token=data.get("input_per_token"),
                output_per_token=data.get("output_per_token"),
                attempted_requests=attempted_requests,
                deferred_request=deferred,
            )
    except (LookupError, TypeError, ValueError):
        return None
    return None


def _write_json(storage, key: str, body: bytes) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "record.json"
        path.write_bytes(body)
        storage.put_file(key, path, "application/json")


def _read_json(storage, key: str) -> Any | None:
    """Read and parse a JSON object from storage, returning None on fetch or parse errors."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "record.json"
        try:
            if not storage.get_file(key, path):
                return None
            return json.loads(path.read_text())
        except Exception:  # noqa: BLE001
            # Transient storage errors or corrupted JSON for an individual key must not abort the
            # whole snapshot load / sweep.
            return None


def write_deferred(
    storage, recipe_hash: str, result: JobResult | JobHandle, *, now: datetime | None = None
) -> None:
    """Persist a handle's pending state, or a completed result, to the registry.

    Never downgrades an already-completed record back to pending: a stale writer racing behind a
    completion (its own or the sweep's) must not make a finished result look pending again.

    Preserves ``created_at`` across re-defers (a record re-written as still-pending keeps the
    timestamp of its *first* write, not this one) -- TTL cleanup measures the age of the whole
    request lineage, not just the most recent attempt.
    """
    now = now or datetime.now(UTC)
    existing_raw = _read_json(storage, deferred_key(recipe_hash))
    if (
        isinstance(existing_raw, Mapping)
        and existing_raw.get("status") == "completed"
        and isinstance(result, JobHandle)
    ):
        return
    record = _record_for(result)
    created_at = existing_raw.get("created_at") if isinstance(existing_raw, Mapping) else None
    record["created_at"] = (
        created_at if isinstance(created_at, str) and created_at else now.isoformat()
    )
    body = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode()
    old_keys = (
        set(_index_keys(existing_raw, recipe_hash=recipe_hash))
        if isinstance(existing_raw, Mapping)
        else set()
    )
    new_keys = set(_index_keys(record, recipe_hash=recipe_hash))
    # New pointers first, then the canonical record, then only the now-stale old pointers --
    # never the reverse. A crash between any two of these steps leaves either a pointer with
    # nothing (yet) behind it (harmless: a canonical GET on a missing/stale-status key is just
    # treated as absent) or a stale extra pointer (harmless: advisory, cleaned up by the next
    # write or `repair_deferred_index`) -- but never a valid pending canonical record reachable
    # by zero pointers, which the old delete-old/write-canonical/write-new order could produce.
    _write_pointer_keys(storage, new_keys - old_keys, recipe_hash)
    _write_json(storage, deferred_key(recipe_hash), body)
    _delete_pointer_keys(storage, old_keys - new_keys)


def look_up_deferred(storage, recipe_hash: str) -> JobResult | JobHandle | None:
    """The registry's current record for ``recipe_hash``, or ``None`` if it has none."""
    return _decode_record(_read_json(storage, deferred_key(recipe_hash)))


def iter_pending_deferred(storage):
    """Yield currently-pending records one at a time, oldest-first.

    The sweep may stop early -- its own wall-clock deadline passes, or a signal requests a
    graceful stop -- before reaching the end of a large registry. Streaming means whatever it
    never reaches is never read at all. It does *not* avoid the read for a record the sweep does
    reach and then skips (e.g. one sharing an already-proven-exhausted route pool with an earlier
    record) -- each yielded record has already had its body fetched by the time the caller can
    check that; only the reconcile *attempt* is what such a skip avoids, not this read.

    ``storage.list_objects`` hands back each key's ``last_modified`` for free (no per-object
    read), so the listing -- cheap key/timestamp pairs, not record bodies -- is sorted by it
    before any body is downloaded. A record's ``last_modified`` moves forward every time it's
    rewritten (e.g. a prior sweep's own retry), so this is "least recently touched", not strictly
    "oldest created" -- but it's a free proxy for which records have been waiting longest without a
    successful attempt, which is a better spend of a capacity-limited run than the arbitrary
    listing order (typically lexicographic by key) the un-sorted stream would otherwise process in.
    A missing/``None`` timestamp sorts last rather than raising.
    """
    listing = sorted(
        storage.list_objects(DEFERRED_PREFIX),
        key=lambda item: (item[1] is None, item[1]),
    )
    for key, _ in listing:
        data = _read_json(storage, key)
        if not isinstance(data, Mapping) or data.get("status") != "pending":
            continue
        decoded = _decode_record(data)
        if isinstance(decoded, JobHandle):
            yield decoded


def _load_snapshot_from_keys(storage, listing) -> DeferredSnapshot:
    """Read canonical records named by a listing, de-duplicating multi-route pointers."""
    entries = []
    seen: set[str] = set()
    for key, last_modified in sorted(listing, key=lambda item: (item[1] is None, item[1])):
        if key in seen:
            continue
        seen.add(key)
        data = _read_json(storage, key)
        entries.append(
            DeferredSnapshotEntry(
                key=key,
                last_modified=last_modified,
                data=data if isinstance(data, Mapping) else None,
                decoded=_decode_record(data),
            )
        )
    return DeferredSnapshot(entries)


def _indexed_listing(storage, *, now: datetime) -> list[tuple[str, Any]]:
    """List pending pointers only for route models that currently have capacity.

    One LIST per eligible model (pointer count is bounded by actual pending volume, since
    completion/expiry always removes a record's pointers -- see ``write_deferred``/
    ``prune_expired_deferred_snapshot``), instead of one LIST across the whole registry.

    Every other index operation in this module (write/delete/``_index_ready``) is best-effort --
    this one wasn't, so a single transient ledger read or LIST would abort the whole sweep instead
    of degrading gracefully (``scripts/llm_deferred_sweep.py`` has no try/except around its
    ``load_deferred_snapshot`` call). A capacity-check failure just means "assume no known
    capacity for this model"; a listing failure just means "no pointers found in this partition
    this run" -- both self-heal on the next sweep, same as every other advisory-index miss.
    """
    from citypods.compute.llm_budget import load_llm_budget_cas
    from citypods.compute.llm_policy import ROUTES

    try:
        budget, _ = load_llm_budget_cas(storage)
    except Exception:  # noqa: BLE001 -- a transient ledger read must not sink the whole sweep
        budget = None
    listing: list[tuple[str, Any]] = []
    for model, route in ROUTES.items():
        if budget is not None and not budget.available(
            model, route=route, requests=1, tokens=0, cost=0.0, now=now
        ):
            continue
        try:
            listing.extend(storage.list_objects(f"{DEFERRED_INDEX_PENDING_PREFIX}{model}/"))
        except Exception:  # noqa: BLE001 -- one bad partition must not sink the whole sweep
            continue
    return listing


def load_deferred_snapshot(storage, *, now: datetime | None = None) -> DeferredSnapshot:
    """Read canonical records once, using the advisory index after migration.

    Before the repair pass marks migration complete, the old full listing remains active. This
    makes rollout safe for existing records and for a repair that is interrupted halfway through.
    """
    if now is not None:
        current = now
    else:
        current = datetime.now(UTC)
    if _index_ready(storage):
        listing = _indexed_listing(storage, now=current)
        return _load_snapshot_from_keys(
            storage,
            ((deferred_key(key.rsplit("/", 1)[-1][:-5]), modified) for key, modified in listing),
        )
    # Canonical keys from a full-registry listing are already unique, so the de-dup in
    # `_load_snapshot_from_keys` is a harmless no-op here -- same helper, no duplicated loop.
    return _load_snapshot_from_keys(storage, storage.list_objects(DEFERRED_PREFIX))


def repair_deferred_index(storage, *, now: datetime | None = None) -> int:
    """Rebuild pending pointers from canonical B2 records and atomically finish migration.

    The pass is idempotent. It intentionally lists the canonical prefix only when invoked by an
    operator/maintenance run; ordinary sweeps use the narrow index listings.
    """
    current = now or datetime.now(UTC)
    repaired = 0
    desired: set[str] = set()
    for key, _ in storage.list_objects(DEFERRED_PREFIX):
        data = _read_json(storage, key)
        if not isinstance(data, Mapping):
            continue
        recipe_hash = key[len(DEFERRED_PREFIX) : -len(".json")]
        _best_effort_delete_index(storage, data, recipe_hash)
        _write_index(storage, data, recipe_hash)
        desired.update(_index_keys(data, recipe_hash=recipe_hash))
        repaired += 1
    # Compaction is safe after the canonical listing: every valid pointer for the current
    # registry is in ``desired``. Orphans are only advisory, so deleting them cannot lose work.
    for key, _ in storage.list_objects(DEFERRED_INDEX_PENDING_PREFIX):
        if key not in desired:
            try:
                storage.delete(key)
            except Exception:  # noqa: BLE001 -- cleanup remains best-effort
                pass
    prune_expired_failure_markers(storage, now=current)
    _write_json(storage, DEFERRED_INDEX_MIGRATION_KEY, b'{"version": 1}\n')
    return repaired


def list_pending_deferred(storage) -> list[JobHandle]:
    """Every currently-pending record in the registry -- what the sweep processes."""
    return list(load_deferred_snapshot(storage).pending())


def prune_expired_deferred(
    storage, *, now: datetime | None = None, ttl_days: float = DEFAULT_TTL_DAYS
) -> int:
    """Delete registry records old enough to be considered abandoned. Applies to both pending and
    completed records -- a completed one nobody ever looks up again (e.g. a caller whose own
    recipe_hash changes every run) is just as much clutter.

    Never deletes a record before ``max(created_at + ttl_days, deadline_at)``: a caller that set
    its own longer deadline (deliberately waiting out something like a monthly cost cap) must not
    have its still-pending request silently vanish before that deadline arrives. Returns the
    number of records deleted.
    """
    return prune_expired_deferred_snapshot(
        storage, load_deferred_snapshot(storage), now=now, ttl_days=ttl_days
    )


def prune_expired_deferred_snapshot(
    storage,
    snapshot: DeferredSnapshot,
    *,
    now: datetime | None = None,
    ttl_days: float = DEFAULT_TTL_DAYS,
    backend=None,
) -> int:
    """Prune records using an already-loaded snapshot, without a second registry traversal.

    If *backend* is supplied (a ``LiteLLMBackend`` instance), any expired handle whose ``ref``
    points to a Cloudflare Worker dispatch object is deleted from R2 via a best-effort
    ``DELETE /v1/requests/{id}`` call (Layer 3 sweep orphan reaping).
    """
    now = now or datetime.now(UTC)
    deleted = 0
    for entry in snapshot.entries:
        if entry.deleted:
            continue
        key, data = entry.key, entry.data
        if not isinstance(data, Mapping):
            continue
        created_raw = data.get("created_at")
        try:
            created_at = datetime.fromisoformat(created_raw) if created_raw else None
        except (TypeError, ValueError):
            created_at = None
        if created_at is None:
            continue  # can't judge age -- leave it rather than guess
        expires_at = created_at + timedelta(days=ttl_days)
        policy_data = data.get("policy")
        if isinstance(policy_data, Mapping):
            deadline_raw = policy_data.get("deadline_at")
            if deadline_raw:
                try:
                    expires_at = max(expires_at, datetime.fromisoformat(deadline_raw))
                except (TypeError, ValueError):
                    pass
        if now > expires_at:
            # The snapshot is intentionally reused across the sweep, but another caller may have
            # completed or re-deferred this key since it was loaded.  Bulk storage has no
            # conditional-delete primitive, so reread only expiry candidates and compare the
            # complete decoded body before releasing/deleting anything.
            if _read_json(storage, key) != data:
                continue
            _release_abandoned_reservation(storage, data, now=now)
            # Layer 3 sweep orphan reaping: purge the R2 object for orphaned dispatch handles
            if backend is not None:
                ref = data.get("ref")
                if ref and isinstance(ref, str):
                    try:
                        backend.delete_dispatched_ref(ref)
                    except Exception:
                        pass
            storage.delete(key)
            _best_effort_delete_index(storage, data, key[len(DEFERRED_PREFIX) : -len(".json")])
            entry.deleted = True
            deleted += 1
    return deleted


def _release_abandoned_reservation(storage, data: Mapping[str, Any], *, now: datetime) -> None:
    """Best-effort: a still-``pending`` genuine dispatch handle (no ``deferred_request`` -- an
    actual in-flight Mistral submission, not a deferred-direct retry candidate) being pruned past
    its TTL means the Worker never produced a terminal response in 38+ days. Its ledger
    reservation (``llm_budget.py``) would otherwise sit in ``inflight`` forever, since a window
    rollover never clears it -- only an explicit settle/release does. Never raises: an unreleased
    reservation is a latent quota-accounting nit, not a reason to abort pruning."""
    if data.get("status") != "pending" or "policy" in data:
        return
    model = data.get("model")
    owner = data.get("owner")
    if not model or not owner:
        return
    try:
        from citypods.compute.llm_budget import release_route_reservation
        from citypods.compute.llm_policy import ROUTE_CANDIDATES, ROUTE_REGISTRY, canonical_model

        route_id = data.get("route_id")
        route = ROUTE_REGISTRY.get(route_id) if isinstance(route_id, str) else None
        if route is None:
            route = next(iter(ROUTE_CANDIDATES.get(canonical_model(model), ())), None)
        if route is None:
            return
        release_route_reservation(storage, owner, route.route_id or model, route=route, now=now)
    except Exception:  # noqa: BLE001 -- best-effort cleanup must never block pruning
        pass


__all__ = [
    "DEFAULT_TTL_DAYS",
    "DEFAULT_FAILURE_MARKER_TTL_DAYS",
    "DEFERRED_FAILURE_PREFIX",
    "DEFERRED_PREFIX",
    "DEFERRED_INDEX_PREFIX",
    "DeferredSnapshot",
    "DeferredSnapshotEntry",
    "MAX_TERMINAL_FAILURE_RETRIES",
    "discard_terminal_failure",
    "deferred_key",
    "deferred_failure_key",
    "load_deferred_snapshot",
    "list_pending_deferred",
    "look_up_deferred",
    "iter_pending_deferred",
    "prune_expired_deferred",
    "prune_expired_deferred_snapshot",
    "prune_expired_failure_markers",
    "repair_deferred_index",
    "record_schema_correction",
    "schema_correction_attempted",
    "terminal_failure_retry_allowed",
    "write_deferred",
]
