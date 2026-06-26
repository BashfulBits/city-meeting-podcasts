"""Stage-2 pull-based work-lease ledger (H17 / review/18 §4).

Per-item compare-and-swap objects on R2 — ``work-leases/<source_key>/<uid>.json`` — that let
heterogeneous workers **competitively claim** transcribe work from a shared ledger instead of being
handed a static ``--shard K/N`` slice. This is the work-distribution half of H14: a worker is a
*puller* (read the discovery index → CAS-claim → infer → durable commit → release/reap), and the
claim is the ownership token the §3.2 owned-block merge commits against. In-Actions matrix shards
keep using the Stage-1 static plan for now (review/18 §6); this substrate exists so external workers
(H14b/H14c) build against the **fixed contract** from their first version, with no later inversion.

Per-item objects give **independent ETags**, so concurrent claims of *different* uids never
contend — the explicit mitigation for the CAS retry-storm a single monolithic ``work.json`` under
CAS would cause (review/17 §6; review/18 §4.1).

**Cost discipline** (review/18 §4.6) keeps per-item granularity at ≈1 R2 Class-A op per *completed*
transcript:

1. **Never list the R2 lease prefix.** Discover candidate uids from the B2 discovery index
   (``work.json``) and **derive** each lease key. Listing is itself a Class-A op on R2.
2. **Read-before-claim + per-worker scan offset.** GET the lease (cheap Class-B) and ``put_cas``
   only when it looks claimable; start scanning at a worker-specific offset so N workers target N
   different items first → ≈1 Class-A per *claimed* item, not per *attempt*.
3. **Infer completion.** The content-addressed transcript artifact + the free B2 record write ARE
   the durable completion signal, so a worker need not write a ``done`` lease — the reaper sweeps
   "leased but artifact present → done".
4. **Generous TTL.** Renew is the exception (only long-audio jobs renew mid-inference, §5).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

LEASE_PREFIX = "work-leases"
LEASE_SCHEMA_VERSION = 1

# Claimable states: a fresh/abandoned item. ``leased`` is claimable only once expired; ``done`` and
# ``failed`` are terminal (``failed`` needs operator/attempts attention, never auto-reclaimed here).
LeaseState = Literal["queued", "leased", "done", "failed"]
_LEASE_STATES: frozenset[str] = frozenset(("queued", "leased", "done", "failed"))
# A held lease may only be settled to a terminal state — releasing to ``queued``/``leased`` would
# write a non-terminal object (e.g. ``leased`` with no expiry) that can wedge the item forever.
TerminalLeaseState = Literal["done", "failed"]


def lease_key(source_key: str, uid: str) -> str:
    """Deterministic per-item key, derived (never listed) from the discovery index."""
    return f"{LEASE_PREFIX}/{source_key}/{uid}.json"


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _parse_dt(raw) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass
class WorkLease:
    """One item's competitive-claim state (review/18 §4.1)."""

    source_key: str
    uid: str
    state: LeaseState = "queued"
    owner: str = ""
    lease_expiry: datetime | None = None
    attempts: int = 0
    pipeline_version: str = ""
    updated_at: datetime | None = None

    def is_expired(self, now: datetime) -> bool:
        return self.lease_expiry is not None and self.lease_expiry <= now

    def is_claimable(self, now: datetime) -> bool:
        """Claimable when fresh (``queued``) or when a held lease has expired (a dead worker)."""
        return self.state == "queued" or (self.state == "leased" and self.is_expired(now))

    def to_dict(self) -> dict:
        return {
            "schema_version": LEASE_SCHEMA_VERSION,
            "source_key": self.source_key,
            "uid": self.uid,
            "state": self.state,
            "owner": self.owner,
            "lease_expiry": _iso(self.lease_expiry),
            "attempts": self.attempts,
            "pipeline_version": self.pipeline_version,
            "updated_at": _iso(self.updated_at),
        }

    @classmethod
    def from_dict(cls, data: dict) -> WorkLease:
        state = data.get("state", "queued")
        if state not in _LEASE_STATES:
            raise ValueError(f"invalid lease state {state!r}")
        return cls(
            source_key=str(data.get("source_key", "")),
            uid=str(data.get("uid", "")),
            state=state,
            owner=str(data.get("owner", "")),
            lease_expiry=_parse_dt(data.get("lease_expiry")),
            attempts=int(data.get("attempts", 0)),
            pipeline_version=str(data.get("pipeline_version", "")),
            updated_at=_parse_dt(data.get("updated_at")),
        )


def _serialize(lease: WorkLease) -> bytes:
    return (json.dumps(lease.to_dict(), indent=2, sort_keys=True) + "\n").encode()


def read_lease(storage, source_key: str, uid: str) -> tuple[WorkLease | None, str | None]:
    """Read a lease + its ETag (the read half of read-before-claim). ``(None, None)`` if absent."""
    got = storage.get_bytes(lease_key(source_key, uid))
    if got is None:
        return None, None
    data, etag = got
    try:
        parsed = json.loads(data)
        return WorkLease.from_dict(parsed if isinstance(parsed, dict) else {}), etag
    except (AttributeError, TypeError, ValueError):
        # Corrupt (bad JSON *or* valid JSON with a malformed schema) → treat as claimable, but keep
        # the ETag so the next CAS write cleanly replaces it rather than crashing the worker.
        return None, etag


def claim(
    storage,
    source_key: str,
    uid: str,
    *,
    owner: str,
    ttl_seconds: float,
    pipeline_version: str = "",
    now: datetime | None = None,
) -> WorkLease | None:
    """Competitively claim ``uid`` for ``owner`` via CAS. Returns the held :class:`WorkLease`, or
    ``None`` when the item is not claimable (already leased+unexpired / terminal) or another worker
    won the race (CAS 412). Read-before-claim: a non-claimable item costs only a Class-B GET, not a
    failed Class-A write (review/18 §4.6)."""
    from citypods.storage import CASConflict

    now = now or datetime.now(UTC)
    existing, etag = read_lease(storage, source_key, uid)
    if existing is not None and not existing.is_claimable(now):
        return None  # held + unexpired, or terminal — skip without spending a Class-A op
    held = WorkLease(
        source_key=source_key,
        uid=uid,
        state="leased",
        owner=owner,
        lease_expiry=now + timedelta(seconds=ttl_seconds),
        attempts=(existing.attempts if existing else 0) + 1,
        pipeline_version=pipeline_version,
        updated_at=now,
    )
    try:
        if etag is None:
            storage.put_cas(
                lease_key(source_key, uid), _serialize(held), "application/json", if_none_match="*"
            )
        else:
            storage.put_cas(
                lease_key(source_key, uid), _serialize(held), "application/json", if_match=etag
            )
    except CASConflict:
        return None  # a sibling claimed it first
    return held


def renew(
    storage,
    source_key: str,
    uid: str,
    *,
    owner: str,
    ttl_seconds: float,
    now: datetime | None = None,
) -> WorkLease | None:
    """Extend our own held lease (long-running inference, §5). ``None`` if we no longer hold it
    (owner changed / not leased / reaped) or the CAS write lost."""
    from citypods.storage import CASConflict

    now = now or datetime.now(UTC)
    existing, etag = read_lease(storage, source_key, uid)
    # Once expired we no longer hold it — the reaper (or a re-claim) owns it now, so refuse to
    # extend dead work even if our owner string still matches the not-yet-reaped object.
    if (
        existing is None
        or etag is None
        or existing.owner != owner
        or existing.state != "leased"
        or existing.is_expired(now)
    ):
        return None
    existing.lease_expiry = now + timedelta(seconds=ttl_seconds)
    existing.updated_at = now
    try:
        storage.put_cas(
            lease_key(source_key, uid), _serialize(existing), "application/json", if_match=etag
        )
    except CASConflict:
        return None
    return existing


def release(
    storage,
    source_key: str,
    uid: str,
    *,
    owner: str,
    state: TerminalLeaseState = "done",
    now: datetime | None = None,
) -> bool:
    """Settle our held lease to a terminal state (``done``/``failed``). Returns False if we don't
    hold it or the CAS write lost. Completion can also be *inferred* (review/18 §4.6 lever 3): the
    artifact + record are the durable signal, so the loop skips the ``done`` write and lets the
    reaper settle it — ``release`` is used for ``failed`` (so a poison item isn't re-claimed and its
    ``attempts`` are recorded) and is available when a prompt explicit ``done`` is wanted."""
    from citypods.storage import CASConflict

    if state not in ("done", "failed"):  # guard the type hint at runtime — never write non-terminal
        raise ValueError(f"release state must be terminal (done/failed), got {state!r}")
    now = now or datetime.now(UTC)
    existing, etag = read_lease(storage, source_key, uid)
    # Refuse once expired: a stale worker must not stamp ``failed`` (or ``done``) on a lease the
    # reaper should requeue. A worker that genuinely finished an over-TTL job still wrote the
    # artifact, so the reaper settles it ``done`` from artifact presence regardless.
    if (
        existing is None
        or etag is None
        or existing.owner != owner
        or existing.state != "leased"
        or existing.is_expired(now)
    ):
        return False
    existing.state = state
    existing.lease_expiry = None
    existing.updated_at = now
    try:
        storage.put_cas(
            lease_key(source_key, uid), _serialize(existing), "application/json", if_match=etag
        )
    except CASConflict:
        return False
    return True


def reap(
    storage,
    candidates: Iterable[tuple[str, str]],
    *,
    artifact_present: Callable[[str, str], bool],
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict:
    """Sweep the leases for ``candidates`` (``(source_key, uid)`` pairs DERIVED from the discovery
    index — never an R2 list). For each held lease: artifact present → settle ``done`` (completion
    inferred); else expired → reset to ``queued`` so a crashed worker's item re-enters the pool;
    else leave it (still running). Returns per-outcome counts. CAS-guarded, so a concurrent claim
    isn't clobbered (a lost CAS is simply counted as a skip and retried next sweep).

    ``dry_run`` previews the same counts WITHOUT writing (read-only) — what a real sweep would do —
    so ``compute reconcile --dry-run`` reports work-lease effects, not just legacy work.json leases.
    """
    from citypods.storage import CASConflict

    now = now or datetime.now(UTC)
    completed = requeued = in_flight = 0
    for source_key, uid in candidates:
        existing, etag = read_lease(storage, source_key, uid)
        if existing is None or etag is None or existing.state != "leased":
            continue
        if artifact_present(source_key, uid):
            existing.state, existing.lease_expiry, existing.updated_at = "done", None, now
            outcome = "completed"
        elif existing.is_expired(now):
            existing.state, existing.owner, existing.lease_expiry, existing.updated_at = (
                "queued",
                "",
                None,
                now,
            )
            outcome = "requeued"
        else:
            in_flight += 1
            continue
        if dry_run:  # read-only preview: count what would change, write nothing
            completed += outcome == "completed"
            requeued += outcome == "requeued"
            continue
        try:
            storage.put_cas(
                lease_key(source_key, uid), _serialize(existing), "application/json", if_match=etag
            )
        except CASConflict:
            continue  # a worker raced us (claimed/renewed) — leave it, re-sweep next cycle
        completed += outcome == "completed"
        requeued += outcome == "requeued"
    return {"completed": completed, "requeued": requeued, "in_flight": in_flight}


def _scan_offset(owner: str, n: int) -> int:
    """Per-worker start offset over the policy-ordered candidates, so N workers target N different
    head items first (review/18 §4.6 lever 2) instead of all colliding on the newest item."""
    if n <= 0:
        return 0
    import hashlib

    return int(hashlib.sha1(owner.encode()).hexdigest(), 16) % n


def run_claim_loop(
    storage,
    candidates: Sequence[tuple[str, str]],
    *,
    owner: str,
    transcribe: Callable[[str, str], None],
    ttl_seconds: float,
    pipeline_version: str = "",
    max_claims: int | None = None,
    should_stop: Callable[[], bool] | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> dict:
    """The frozen pull contract (review/18 §4.2) every worker runs — in-Actions or external GPU.

    Walks ``candidates`` (``(source_key, uid)``, policy-ordered) from this worker's scan offset:
    CAS-claim each claimable item; on a win, run the injected ``transcribe(source_key, uid)`` (fetch
    audio → infer → write the content-addressed artifact → commit the record with
    ``owned_uids={uid}``); on success leave completion to be inferred from the artifact (no ``done``
    write — cost lever 3); on failure settle the lease ``failed`` so it isn't at once re-claimed.
    Stops at ``max_claims`` or when ``should_stop()`` (the wall-clock budget). ``transcribe`` is the
    seam H14b/H14c fill with the real GPU path; the loop, claim/renew/reap, and keys are fixed.
    """
    now_fn = now_fn or (lambda: datetime.now(UTC))
    n = len(candidates)
    offset = _scan_offset(owner, n)
    ordered = [candidates[(offset + i) % n] for i in range(n)] if n else []

    claimed = completed = failed = 0
    for source_key, uid in ordered:
        if max_claims is not None and claimed >= max_claims:
            break
        if should_stop is not None and should_stop():
            break
        held = claim(
            storage,
            source_key,
            uid,
            owner=owner,
            ttl_seconds=ttl_seconds,
            pipeline_version=pipeline_version,
            now=now_fn(),
        )
        if held is None:
            continue  # not claimable or lost the race — cheap, no Class-A spent
        claimed += 1
        try:
            transcribe(source_key, uid)
            completed += 1  # completion is durable in the artifact/record; reaper settles the lease
        except Exception:  # noqa: BLE001 — one bad item must not kill the loop
            release(storage, source_key, uid, owner=owner, state="failed", now=now_fn())
            failed += 1
    return {"claimed": claimed, "completed": completed, "failed": failed}
