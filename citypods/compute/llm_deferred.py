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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from citypods.compute.base import JobHandle, JobResult
from citypods.compute.llm_policy import DeferredLLMRequest, LLMRequestPolicy

DEFERRED_PREFIX = "state/llm_deferred/"

# Worst case a record should ever need to sit here: a caller with no deadline_at (or a caller
# that never asks again, like a Stage whose own recipe_hash changes run to run) waiting out a
# full monthly cost-cycle reset (review/33 §10's cost_cycle_key -- up to ~31 days if the cap was
# hit right after a rollover) plus slack for the sweep's own daily cadence. `prune_expired_deferred`
# never deletes a record before this *or* the caller's own `deadline_at`, whichever is later.
DEFAULT_TTL_DAYS = 38


def deferred_key(recipe_hash: str) -> str:
    return f"{DEFERRED_PREFIX}{recipe_hash}.json"


def _serialize_policy(policy: LLMRequestPolicy) -> dict[str, Any]:
    return {
        "allowed_models": (
            list(policy.allowed_models) if policy.allowed_models is not None else None
        ),
        "allow_paid": policy.allow_paid,
        "deadline_at": policy.deadline_at.isoformat() if policy.deadline_at is not None else None,
        "purpose": policy.purpose,
    }


def _deserialize_policy(data: Mapping[str, Any]) -> LLMRequestPolicy:
    allowed = data.get("allowed_models")
    deadline = data.get("deadline_at")
    return LLMRequestPolicy(
        allowed_models=tuple(allowed) if allowed is not None else None,
        allow_paid=bool(data.get("allow_paid", False)),
        deadline_at=datetime.fromisoformat(deadline) if deadline else None,
        purpose=str(data.get("purpose", "")),
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
                owner=data.get("owner"),
                input_per_token=data.get("input_per_token"),
                output_per_token=data.get("output_per_token"),
                attempted_requests=data.get("attempted_requests"),
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
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "record.json"
        if not storage.get_file(key, path):
            return None
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
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
    _write_json(storage, deferred_key(recipe_hash), body)


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


def list_pending_deferred(storage) -> list[JobHandle]:
    """Every currently-pending record in the registry -- what the sweep processes."""
    return list(iter_pending_deferred(storage))


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
    now = now or datetime.now(UTC)
    deleted = 0
    for key, _ in storage.list_objects(DEFERRED_PREFIX):
        data = _read_json(storage, key)
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
            _release_abandoned_reservation(storage, data, now=now)
            storage.delete(key)
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
        from citypods.compute.llm_policy import ROUTES

        route = ROUTES.get(model)
        if route is None:
            return
        release_route_reservation(storage, owner, model, route=route, now=now)
    except Exception:  # noqa: BLE001 -- best-effort cleanup must never block pruning
        pass


__all__ = [
    "DEFAULT_TTL_DAYS",
    "DEFERRED_PREFIX",
    "deferred_key",
    "list_pending_deferred",
    "look_up_deferred",
    "iter_pending_deferred",
    "prune_expired_deferred",
    "write_deferred",
]
