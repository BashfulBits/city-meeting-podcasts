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

**Frozen vs. not, precisely.** :func:`claim` / :func:`renew` / :func:`release` / :func:`reap` and
the R2 key layout are the frozen §4.2 contract — every worker (in-Actions or external) must use
these and only these to win/hold/release an item. :func:`run_claim_loop` is a reference
*orchestration* around that contract, not part of the frozen surface itself: it is missing
external-budget gating, lease renewal for long jobs, and retry — all of which
``citypods.compute.external_worker`` (the H14b Modal worker) needed and built directly on the
primitives instead. **Read `run_claim_loop`'s docstring before reusing it for a new worker class**,
especially the note on review/18 §6 step 4 (migrating in-Actions shards from the static plan to
this claim loop) — that migration is the moment to fold renewal/retry into one shared loop instead
of writing a third one.
"""

from __future__ import annotations

import hashlib
import json
import time as _time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, TypeVar

LEASE_PREFIX = "work-leases"
LEASE_SCHEMA_VERSION = 1

# --- Active-lease index (GH#1018) ---------------------------------------------------------------
# A fixed set of small CAS-managed "bucket" objects tracking which (source_key, uid) pairs are
# currently claimed. Reaping via this index costs O(active leases + bucket count) instead of the
# O(backlog) candidate-probe reap() below, which GETs every possible lease key even when almost none
# are ever claimed. See reap_indexed()'s docstring for the full design.
INDEX_PREFIX = "work-leases-index"
INDEX_SCHEMA_VERSION = 1
INDEX_BUCKET_COUNT = 64

T = TypeVar("T")

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


def _entry_id(source_key: str, uid: str) -> str:
    """The active-index's entry key for ``(source_key, uid)`` — one string, so bucket JSON stays a
    flat ``{entry_id: {"source_key", "uid", "lease_expiry"}}`` map instead of a nested structure."""
    return f"{source_key}|{uid}"


def index_bucket_for(source_key: str, uid: str, bucket_count: int = INDEX_BUCKET_COUNT) -> int:
    """Stable bucket assignment for an item, independent of claim order (same hash family as
    :func:`scan_offset` — sha1 of the derived key, not the raw strings, so bucket boundaries don't
    shift if ``source_key``/``uid`` happen to share a prefix)."""
    if bucket_count <= 0:
        return 0
    digest = hashlib.sha1(lease_key(source_key, uid).encode()).hexdigest()
    return int(digest, 16) % bucket_count


def index_bucket_key(n: int) -> str:
    return f"{INDEX_PREFIX}/bucket-{n:04d}.json"


def _load_index_bucket(storage, n: int) -> tuple[dict[str, dict], str | None]:
    """Read one bucket's entries + ETag. Absent or corrupt → ``({}, etag)`` (corrupt keeps the ETag
    so the next CAS write cleanly replaces it, same convention as :func:`read_lease`)."""
    got = storage.get_bytes(index_bucket_key(n))
    if got is None:
        return {}, None
    data, etag = got
    try:
        parsed = json.loads(data)
        entries = parsed.get("entries") if isinstance(parsed, dict) else None
        return (entries if isinstance(entries, dict) else {}), etag
    except (AttributeError, TypeError, ValueError):
        return {}, etag


def _serialize_index_bucket(entries: dict[str, dict]) -> bytes:
    body = {"schema_version": INDEX_SCHEMA_VERSION, "entries": entries}
    return (json.dumps(body, indent=2, sort_keys=True) + "\n").encode()


def _mutate_index_bucket(
    storage,
    n: int,
    mutate: Callable[[dict[str, dict]], None],
    *,
    max_attempts: int = 8,
    base_sleep: float = 0.05,
    max_sleep: float = 1.0,
    sleep=_time.sleep,
) -> None:
    """CAS read-modify-write of index bucket ``n`` (same retry shape as
    ``compute.budget.mutate_budget``): load the freshest entries + ETag, apply ``mutate`` in place,
    write back conditionally, and retry with backoff on a losing race so a sibling worker's
    concurrent add/remove in the same bucket is never silently dropped. Best-effort by design (the
    lease object, not the index, is claim authority — GH#1018 acceptance criterion 7): a caller that
    exhausts ``max_attempts`` should log and continue rather than fail the lease operation itself.
    """
    from citypods.storage import CASConflict

    last: CASConflict | None = None
    for attempt in range(max_attempts):
        entries, etag = _load_index_bucket(storage, n)
        mutate(entries)
        body = _serialize_index_bucket(entries)
        try:
            if etag is None:
                storage.put_cas(index_bucket_key(n), body, "application/json", if_none_match="*")
            else:
                storage.put_cas(index_bucket_key(n), body, "application/json", if_match=etag)
            return
        except CASConflict as exc:
            last = exc
            sleep(min(base_sleep * 2**attempt, max_sleep) * (0.5 + attempt % 3 / 3))
    assert last is not None
    raise last


def _index_upsert(
    storage,
    source_key: str,
    uid: str,
    *,
    lease_expiry: datetime | None,
    bucket_count: int = INDEX_BUCKET_COUNT,
) -> None:
    """Add/refresh this item's active-index entry (claim/renew). Best-effort: a failure here must
    never fail the caller's claim/renew — it only makes this item invisible to the *indexed* reap
    until the next integrity sweep finds it (acceptance criterion 7)."""
    n = index_bucket_for(source_key, uid, bucket_count)

    def _mutate(entries: dict[str, dict]) -> None:
        entries[_entry_id(source_key, uid)] = {
            "source_key": source_key,
            "uid": uid,
            "lease_expiry": _iso(lease_expiry),
        }

    try:
        _mutate_index_bucket(storage, n, _mutate)
    except Exception as exc:  # noqa: BLE001 — index maintenance is best-effort
        print(f"[work-leases] active-index upsert failed for {source_key}/{uid}: {exc}", flush=True)


def _index_remove(
    storage,
    source_key: str,
    uid: str,
    *,
    bucket_count: int = INDEX_BUCKET_COUNT,
) -> None:
    """Drop this item's active-index entry (release/abandon settled it to a non-``leased`` state).
    Best-effort, same rationale as :func:`_index_upsert`."""
    n = index_bucket_for(source_key, uid, bucket_count)
    entry_id = _entry_id(source_key, uid)

    def _mutate(entries: dict[str, dict]) -> None:
        entries.pop(entry_id, None)

    try:
        _mutate_index_bucket(storage, n, _mutate)
    except Exception as exc:  # noqa: BLE001 — index maintenance is best-effort
        print(f"[work-leases] active-index remove failed for {source_key}/{uid}: {exc}", flush=True)


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
    update_index: bool = False,
) -> WorkLease | None:
    """Competitively claim ``uid`` for ``owner`` via CAS. Returns the held :class:`WorkLease`, or
    ``None`` when the item is not claimable (already leased+unexpired / terminal) or another worker
    won the race (CAS 412). Read-before-claim: a non-claimable item costs only a Class-B GET, not a
    failed Class-A write (review/18 §4.6).

    ``update_index`` opts into maintaining the GH#1018 active-lease index alongside the claim (an
    extra CAS write on the item's index bucket) so :func:`reap_indexed` can find it without probing
    the whole backlog. Off by default: existing callers that don't sweep via the index keep the
    original ≈1-Class-A-per-claim cost unchanged."""
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
    if update_index:
        _index_upsert(storage, source_key, uid, lease_expiry=held.lease_expiry)
    return held


def renew(
    storage,
    source_key: str,
    uid: str,
    *,
    owner: str,
    ttl_seconds: float,
    now: datetime | None = None,
    update_index: bool = False,
) -> WorkLease | None:
    """Extend our own held lease (long-running inference, §5). ``None`` if we no longer hold it
    (owner changed / not leased / reaped) or the CAS write lost.

    ``update_index`` refreshes this item's GH#1018 active-index entry (new expiry) alongside the
    renewal — see :func:`claim`'s docstring for the tradeoff."""
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
    if update_index:
        _index_upsert(storage, source_key, uid, lease_expiry=existing.lease_expiry)
    return existing


def release(
    storage,
    source_key: str,
    uid: str,
    *,
    owner: str,
    state: TerminalLeaseState = "done",
    now: datetime | None = None,
    update_index: bool = False,
) -> bool:
    """Settle our held lease to a terminal state (``done``/``failed``). Returns False if we don't
    hold it or the CAS write lost. Completion can also be *inferred* (review/18 §4.6 lever 3): the
    artifact + record are the durable signal, so the loop skips the ``done`` write and lets the
    reaper settle it — ``release`` is used for ``failed`` (so a poison item isn't re-claimed and its
    ``attempts`` are recorded) and is available when a prompt explicit ``done`` is wanted.

    ``update_index`` drops this item's GH#1018 active-index entry on a successful settle (it is no
    longer active) — see :func:`claim`'s docstring for the tradeoff."""
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
    if update_index:
        _index_remove(storage, source_key, uid)
    return True


def abandon(
    storage,
    source_key: str,
    uid: str,
    *,
    owner: str,
    now: datetime | None = None,
    update_index: bool = False,
) -> bool:
    """Return our own unexpired claim to ``queued`` before inference starts.

    This is intentionally narrower than :func:`release`: it is only for pre-work declines such as
    "budget was available when we scanned, but the atomic reservation lost the race." It clears the
    owner/expiry so another worker can claim the item without waiting for TTL, but refuses expired
    or non-owned leases so stale workers cannot undo reaper/worker progress.

    ``update_index`` drops this item's GH#1018 active-index entry (it is back to ``queued``, no
    longer active) — see :func:`claim`'s docstring for the tradeoff.
    """
    from citypods.storage import CASConflict

    now = now or datetime.now(UTC)
    existing, etag = read_lease(storage, source_key, uid)
    if (
        existing is None
        or etag is None
        or existing.owner != owner
        or existing.state != "leased"
        or existing.is_expired(now)
    ):
        return False
    existing.state = "queued"
    existing.owner = ""
    existing.lease_expiry = None
    existing.updated_at = now
    try:
        storage.put_cas(
            lease_key(source_key, uid), _serialize(existing), "application/json", if_match=etag
        )
    except CASConflict:
        return False
    if update_index:
        _index_remove(storage, source_key, uid)
    return True


def _settle_leased(
    storage,
    source_key: str,
    uid: str,
    existing: WorkLease,
    etag: str,
    *,
    artifact_present: Callable[[str, str], bool],
    now: datetime,
    dry_run: bool,
) -> str:
    """Apply one already-read ``leased`` lease's reap decision. Returns ``"completed"``,
    ``"requeued"``, ``"in_flight"`` (still running, untouched), or ``"skip"`` (a sibling won the
    CAS race — leave it for the next sweep). Shared by :func:`reap` and :func:`reap_indexed`
    (GH#1018) so the candidate-probe and index-driven sweeps can never disagree on what "reap this
    lease" means — only how they choose *which* leases to look at differs."""
    from citypods.storage import CASConflict

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
        return "in_flight"
    if dry_run:  # read-only preview: count what would change, write nothing
        return outcome
    try:
        storage.put_cas(
            lease_key(source_key, uid), _serialize(existing), "application/json", if_match=etag
        )
    except CASConflict:
        return "skip"  # a worker raced us (claimed/renewed) — leave it, re-sweep next cycle
    return outcome


def _invoke_reap_callback(
    outcome: str,
    owner: str,
    on_completed: Callable[[str], None] | None,
    on_requeued: Callable[[str], None] | None,
) -> None:
    # The lease has already been moved off "leased", so a raising callback (e.g. a budget settle
    # that exhausts its CAS retries) must not abort the rest of the sweep — that item can't be
    # re-swept. Log and keep going; the budget update for this one item is best-effort.
    try:
        if outcome == "completed" and on_completed is not None:
            on_completed(owner)
        elif outcome == "requeued" and on_requeued is not None:
            on_requeued(owner)
    except Exception as exc:  # noqa: BLE001
        print(f"[work-leases] reap callback ({outcome}) failed for {owner}: {exc}", flush=True)


def reap(
    storage,
    candidates: Iterable[tuple[str, str]],
    *,
    artifact_present: Callable[[str, str], bool],
    on_completed: Callable[[str], None] | None = None,
    on_requeued: Callable[[str], None] | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict:
    """Sweep the leases for ``candidates`` (``(source_key, uid)`` pairs DERIVED from the discovery
    index — never an R2 list). For each held lease: artifact present → settle ``done`` (completion
    inferred); else expired → reset to ``queued`` so a crashed worker's item re-enters the pool;
    else leave it (still running). Returns per-outcome counts. CAS-guarded, so a concurrent claim
    isn't clobbered (a lost CAS is simply counted as a skip and retried next sweep).

    This is the original **candidate-probe** sweep (GH#1018's "before"): cost is O(len(candidates))
    GETs regardless of how many are actually leased, so it does not scale as the backlog grows.
    Kept as the documented rollout fallback for :func:`reap_indexed`, and as what a CAS backend
    without the active-lease index still uses.

    ``dry_run`` previews the same counts WITHOUT writing (read-only) — what a real sweep would do —
    so ``compute reconcile --dry-run`` reports work-lease effects, not just legacy work.json leases.
    """
    now = now or datetime.now(UTC)
    completed = requeued = in_flight = 0
    for source_key, uid in candidates:
        existing, etag = read_lease(storage, source_key, uid)
        if existing is None or etag is None or existing.state != "leased":
            continue
        owner = existing.owner
        outcome = _settle_leased(
            storage,
            source_key,
            uid,
            existing,
            etag,
            artifact_present=artifact_present,
            now=now,
            dry_run=dry_run,
        )
        if outcome == "in_flight":
            in_flight += 1
            continue
        if outcome == "skip":
            continue
        completed += outcome == "completed"
        requeued += outcome == "requeued"
        _invoke_reap_callback(outcome, owner, on_completed, on_requeued)
    return {"completed": completed, "requeued": requeued, "in_flight": in_flight}


def reap_indexed(
    storage,
    *,
    artifact_present: Callable[[str, str], bool],
    on_completed: Callable[[str], None] | None = None,
    on_requeued: Callable[[str], None] | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
    bucket_count: int = INDEX_BUCKET_COUNT,
    integrity_candidates: Sequence[tuple[str, str]] | None = None,
    integrity_partition: int | None = None,
) -> dict:
    """Sweep the GH#1018 active-lease index instead of probing every candidate.

    Reads exactly ``bucket_count`` index-bucket objects (bounded, independent of backlog size) and,
    for each indexed ``(source_key, uid)``, re-reads the **real lease object** — the index is never
    trusted as claim authority (acceptance criterion 7) — and applies the same
    :func:`_settle_leased` decision :func:`reap` would. With zero active leases this costs
    ``bucket_count`` Class-B reads and zero lease reads; with N active leases it costs
    ``O(N + bucket_count)``, not ``O(backlog)``.

    Settled (``completed``/``requeued``) and drifted (indexed but no longer actually ``leased``,
    e.g. an index-write that raced a settle elsewhere) entries are removed from the index;
    still-running entries are left indexed. Index maintenance is best-effort CAS (see
    :func:`_index_upsert` / :func:`_index_remove`) — a write that can't land is retried by
    whichever sweep next scans that bucket, never by failing this one.

    **Integrity sweep (crash recovery).** A crash between a successful lease claim and its index
    write would otherwise leave that lease un-indexed forever — invisible to this sweep even
    though it's genuinely held. Pass ``integrity_candidates`` (the same policy-ordered candidate
    list :func:`reap`'s caller already derives from the discovery index) and
    ``integrity_partition`` (an integer the caller rotates across runs, e.g.
    ``now.toordinal() % bucket_count``) to additionally candidate-probe **one bounded partition**
    of the keyspace per run — the same partition every candidate in it would hash to via
    :func:`index_bucket_for`, so this reuses the bucket assignment rather than inventing a second
    one. A found-but-unindexed active lease is repaired into the index (and reaped/left running
    same as any other). One partition per run keeps this a small, fixed add-on cost
    (``≈ backlog / bucket_count`` GETs), not a full backlog scan, while still recovering full
    backlog coverage every ``bucket_count`` runs.

    ``dry_run`` previews without writing (lease or index), same contract as :func:`reap`.
    """
    now = now or datetime.now(UTC)
    completed = requeued = in_flight = 0
    seen: set[tuple[str, str]] = set()

    for n in range(max(bucket_count, 0)):
        entries, _bucket_etag = _load_index_bucket(storage, n)
        for meta in entries.values():
            source_key, uid = str(meta.get("source_key", "")), str(meta.get("uid", ""))
            if not source_key or not uid:
                continue
            seen.add((source_key, uid))
            existing, etag = read_lease(storage, source_key, uid)
            if existing is None or etag is None or existing.state != "leased":
                if not dry_run:
                    _index_remove(storage, source_key, uid, bucket_count=bucket_count)
                continue  # index drift: no longer an active lease — nothing to reap
            owner = existing.owner
            outcome = _settle_leased(
                storage,
                source_key,
                uid,
                existing,
                etag,
                artifact_present=artifact_present,
                now=now,
                dry_run=dry_run,
            )
            if outcome == "in_flight":
                in_flight += 1
                continue  # still active — stays indexed
            if outcome == "skip":
                continue  # a sibling raced us — leave it indexed, re-sweep next cycle
            completed += outcome == "completed"
            requeued += outcome == "requeued"
            if not dry_run:
                _index_remove(storage, source_key, uid, bucket_count=bucket_count)
            _invoke_reap_callback(outcome, owner, on_completed, on_requeued)

    integrity_checked = 0
    if integrity_candidates is not None and integrity_partition is not None and bucket_count > 0:
        partition = integrity_partition % bucket_count
        for source_key, uid in integrity_candidates:
            if (source_key, uid) in seen:
                continue
            if index_bucket_for(source_key, uid, bucket_count) != partition:
                continue
            integrity_checked += 1
            existing, etag = read_lease(storage, source_key, uid)
            if existing is None or etag is None or existing.state != "leased":
                continue  # nothing active here to repair or reap
            owner = existing.owner
            outcome = _settle_leased(
                storage,
                source_key,
                uid,
                existing,
                etag,
                artifact_present=artifact_present,
                now=now,
                dry_run=dry_run,
            )
            if outcome == "in_flight":
                in_flight += 1
                # Found a genuinely held lease the index missed (e.g. a crash between the lease
                # write and the index write) — repair it in so the next sweep finds it directly.
                if not dry_run:
                    _index_upsert(
                        storage,
                        source_key,
                        uid,
                        lease_expiry=existing.lease_expiry,
                        bucket_count=bucket_count,
                    )
                continue
            if outcome == "skip":
                continue
            completed += outcome == "completed"
            requeued += outcome == "requeued"
            _invoke_reap_callback(outcome, owner, on_completed, on_requeued)

    return {
        "completed": completed,
        "requeued": requeued,
        "in_flight": in_flight,
        "indexed_buckets_read": bucket_count,
        "integrity_checked": integrity_checked,
    }


def scan_offset(owner: str, n: int) -> int:
    """Per-worker start offset over *n* policy-ordered candidates, so N workers target N different
    head items first (review/18 §4.6 lever 2) instead of all colliding on the newest item.

    Public: this is the one ordering primitive every claim-style worker anchors its scan to,
    whether or not it drives its loop through :func:`run_claim_loop` — see
    :func:`ordered_candidates` and that function's docstring for why a worker might compose the
    primitives itself instead of calling the loop wrapper."""
    if n <= 0:
        return 0
    return int(hashlib.sha1(owner.encode()).hexdigest(), 16) % n


def ordered_candidates(candidates: Sequence[T], owner: str) -> list[T]:
    """Rotate *candidates* (policy-ordered, e.g. read straight from the ``work.json`` discovery
    index) to *owner*'s deterministic :func:`scan_offset`.

    Generic over the candidate shape — :func:`run_claim_loop` calls this with
    ``(source_key, uid)`` tuples; a worker that composes the claim primitives itself (instead of
    calling that loop — see its docstring for when that's the right call) can call this directly
    on a richer sequence (e.g. full ``WorkItem`` objects) without re-deriving the rotation math.
    Do not reach for :func:`scan_offset` and re-write the modulo-rotation by hand — that is exactly
    the duplication this function exists to remove."""
    n = len(candidates)
    if n == 0:
        return []
    offset = scan_offset(owner, n)
    return [candidates[(offset + i) % n] for i in range(n)]


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
    update_index: bool = False,
) -> dict:
    """The reference pull loop (review/18 §4.2) over the claim/release primitives this module
    owns. Walks ``candidates`` (``(source_key, uid)``, policy-ordered) from this worker's
    :func:`ordered_candidates` rotation: CAS-claim each claimable item; on a win, run the injected
    ``transcribe(source_key, uid)`` (fetch audio → infer → write the content-addressed artifact →
    commit the record with ``owned_uids={uid}``); on success leave completion to be inferred from
    the artifact (no ``done`` write — cost lever 3); on failure settle the lease ``failed`` so it
    isn't at once re-claimed. Stops at ``max_claims`` or when ``should_stop()`` (the wall-clock
    budget). ``transcribe`` is the seam a real GPU path fills; the claim/renew/reap primitives and
    R2 key layout underneath are fixed (frozen, review/18 §4.2) — this function's *orchestration*
    around them is not.

    **What this loop does NOT do, by design — read before reusing it for a new worker.** It has no
    external-budget gating (no monthly-cap/in-flight check before claiming), no lease renewal for
    an inference that outlives ``ttl_seconds`` (a long job can be reaped and re-claimed by another
    worker mid-flight — content-addressing makes that wasteful, not corrupting, but it is a real
    gap for anything beyond a short job), and no retry on a transient failure. That was an
    acceptable scope when this was written because the only caller in mind was a same-process,
    no-budget GPU adapter. The Modal/H14b worker (``citypods.compute.external_worker``) needed all
    three of those and, rather than extend this reference wrapper, built its own loop directly on
    :func:`claim` / :func:`release` / :func:`renew` (still the same *frozen* contract — just not
    this wrapper) with budget-gating, a renewal thread, and retry layered on top.

    **The moment this will bite again: review/18 §6 step 4 — in-Actions shards flip from the
    Stage-1 static plan to this claim loop (GitHub Actions becomes "just another worker").** That
    migration's natural first move is to reach for this function by name. Before doing that,
    check whether in-Actions ASR jobs need the same renewal/retry external_worker.py already
    proved out (long meetings — review/12 §H5's ``long_first`` comparator deliberately prioritizes
    them — can easily outlive a short TTL). If so, this is the point to fold renewal/retry into
    this loop as optional hooks (budget-gating stays external-only — an in-Actions runner has no
    monthly dollar cap to protect), so both worker classes finally share one real implementation
    instead of three loops slowly drifting apart. Do not write a third loop.

    ``update_index`` maintains the GH#1018 active-lease index alongside every claim/release so
    ``reap_indexed`` can sweep this worker's leases in bounded time — see :func:`claim`'s docstring.
    """
    now_fn = now_fn or (lambda: datetime.now(UTC))
    ordered = ordered_candidates(candidates, owner)

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
            update_index=update_index,
        )
        if held is None:
            continue  # not claimable or lost the race — cheap, no Class-A spent
        claimed += 1
        try:
            transcribe(source_key, uid)
            completed += 1  # completion is durable in the artifact/record; reaper settles the lease
        except Exception:  # noqa: BLE001 — one bad item must not kill the loop
            release(
                storage,
                source_key,
                uid,
                owner=owner,
                state="failed",
                now=now_fn(),
                update_index=update_index,
            )
            failed += 1
    return {"claimed": claimed, "completed": completed, "failed": failed}
