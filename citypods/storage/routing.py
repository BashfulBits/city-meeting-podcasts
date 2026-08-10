"""Prefix-routing storage that splits state across two backends (review/17 §5).

Most state — content-addressed audio/transcripts and append-only logs — stays on the
*primary* backend (B2), where writes are free and consistency is strong. Only the
coordination control-plane (the GPU budget ledger, compact catalog manifest, Stage-2
work-lease ledger, and provider concurrency slots), which genuinely needs compare-and-swap,
routes to the *coordination* backend (R2).

``RoutingStorage`` implements the ``StorageBackend`` Protocol and dispatches each call by
key prefix, so callers keep using one storage object unchanged. CAS-only methods
(``get_bytes``/``put_cas``) delegate to the coordination backend.

The set of coordination prefixes is passed in (``COORDINATION_PREFIXES`` is the production
default). Non-coordination state continues to route to B2, while each listed control-plane
object is addressed on R2 and excluded from the bulk B2 state sync.
"""

from __future__ import annotations

from pathlib import Path

from citypods.storage.base import StorageBackend

# Keys whose prefix matches route to the coordination (CAS) backend. Each migration appends its
# prefix here with its own change:
#   - ``state/compute_budget.json`` (H17 PR3): free-tier GPU ledger — atomic-decrement overspend
#     guard; lives on R2, accessed by CAS, excluded from the bulk B2 state sync.
#   - ``state/catalog/manifest.json`` (GH#1015): compact durable-state index — conditionally
#     published on R2; rebuildable from B2 state objects and excluded from the bulk B2 sync.
#   - ``work-leases/`` (H17 PR4): the Stage-2 per-item competitive-claim ledger (review/18 §4); each
#     ``work-leases/<source>/<uid>.json`` is an independent CAS object. Derived/GET by key, never
#     listed (listing is a Class-A op on R2).
#   - ``work-leases-index/`` (GH#1018): the bounded active-lease index — a fixed set of
#     ``work-leases-index/bucket-<NNNN>.json`` CAS objects tracking which leases are currently
#     claimed, so the reaper sweeps O(active leases) instead of O(backlog). Derived/GET by key,
#     never listed.
#   - ``state/asr_worker_telemetry.json`` (H14b/H14c): bounded, non-secret worker memory samples
#     used by ``asr-worker-report`` and H14d admission tuning. One fixed CAS object avoids listing
#     a telemetry prefix on R2.
#   - ``state/transcript_quality_ledger.json`` (H15): the human-review mutation ledger. The durable
#     mirror remains on B2 (`state/transcript_quality_rollups.json`); the CAS object serializes
#     concurrent issue-ingest writes so votes are not lost.
#   - ``provider-leases/`` (H17 PR6): the cross-process provider concurrency slots; each
#     ``provider-leases/<domain>/slot-<i>.json`` is an independent CAS object claimed by
#     ``put_cas``. Slot keys are derived (``0..N-1``), never listed.
COORDINATION_PREFIXES: tuple[str, ...] = (
    "state/compute_budget.json",
    "state/llm_budget.json",
    "state/asr_worker_telemetry.json",
    "state/transcript_quality_ledger.json",
    "state/catalog/manifest.json",
    "work-leases/",
    "work-leases-index/",
    "provider-leases/",
)

# INVARIANT — R2 holds ONLY ephemeral, derivable objects. Two forces make a canonical record on R2
# unrecoverable: (a) the storage-reclaim lifecycle rules aggressively expire R2 scratch (GH#496),
# (b) R2 coordination keys are excluded from the bulk B2 state sync (``statesync._is_cas_managed``),
# so they have no backup on B2. Every prefix routed to R2 must therefore be reconstructible from the
# B2-resident record store / discovery index if it vanishes. This allow-list is the *declaration*
# that each coordination prefix satisfies that: adding a new R2 prefix without a matching entry
# ``assert_coordination_prefixes_ephemeral`` (called at import) and a test, forcing a reviewer to
# confirm the new data carries nothing canonical. Never add a prefix that stores the sole copy of
# anything (audio, transcripts, records, or state that isn't recomputable).
_EPHEMERAL_R2_PREFIXES: dict[str, str] = {
    # Free-tier GPU budget ledger: derivable — a lost/expired budget object re-initializes to the
    # period cap; worst case is one over-count window, never lost artifacts.
    "state/compute_budget.json": "GPU budget ledger; re-initializes to the period cap if lost",
    "state/llm_budget.json": (
        "LLM quota/cost ledger; re-initializes to zero spend/quota-used if lost"
    ),
    # Bounded, non-secret worker memory samples for admission tuning; a lost object just restarts
    # sampling. No durable/canonical data.
    "state/asr_worker_telemetry.json": "bounded worker telemetry samples; restarts if lost",
    # H15 decision ledger: authoritative writes serialize through CAS, but every committed state is
    # mirrored to the B2 durable-state snapshot (`state/transcript_quality_rollups.json`), so the
    # R2 object is recoverable and not the only copy of human review history.
    "state/transcript_quality_ledger.json": "CAS write-serialization cache mirrored durably to B2",
    # Compact state index: every listed object is a B2-resident durable state file, so the index can
    # be rebuilt by the full-list fallback if R2 expires or loses this coordination object.
    "state/catalog/manifest.json": "compact index; rebuilt from B2 durable state objects if lost",
    # Stage-2 per-item work-lease ledger: a claim token, not a work product. If lost, the item
    # looks unclaimed and is re-derived from the B2 discovery index and re-claimed.
    "work-leases/": "per-item claim tokens; re-derived from the B2 discovery index",
    "work-leases-index/": (
        "active-lease index buckets; re-derived from the lease objects (claim authority) via "
        "the integrity sweep if lost"
    ),
    # Provider concurrency slots: transient N-fixed CAS objects; a lost slot just frees capacity and
    # is re-created on the next claim. Carries no durable state.
    "provider-leases/": "transient concurrency slots; re-created on next claim",
}


def assert_coordination_prefixes_ephemeral(
    prefixes: tuple[str, ...] = COORDINATION_PREFIXES,
    *,
    allow: dict[str, str] = _EPHEMERAL_R2_PREFIXES,
) -> None:
    """Guard the R2-is-ephemeral invariant: every coordination prefix routed to R2 must be declared
    ephemeral/derivable in ``_EPHEMERAL_R2_PREFIXES``. Raises ``AssertionError`` naming the offender
    so a new R2 prefix can't ship without a reviewer consciously affirming it stores no canonical
    (backup-less) data. See the invariant note above ``_EPHEMERAL_R2_PREFIXES``."""
    undeclared = [p for p in prefixes if p not in allow]
    if undeclared:
        raise AssertionError(
            "R2 coordination prefix(es) not declared ephemeral in _EPHEMERAL_R2_PREFIXES "
            f"(routing.py): {undeclared}. R2 has no backup and is aggressively expired — a "
            "canonical record here would be unrecoverable. Add each prefix to the allow-list with "
            "the reason it is derivable, or route it to the B2 primary instead."
        )


assert_coordination_prefixes_ephemeral()


class RoutingStorage:
    """Dispatch storage calls to ``primary`` or ``coordination`` by key prefix.

    When ``coordination`` is None (R2 creds absent), every call falls through to
    ``primary`` — so local dev and dry runs are unaffected and behavior degrades safely.
    """

    def __init__(
        self,
        *,
        primary: StorageBackend,
        coordination: StorageBackend | None = None,
        coordination_prefixes: tuple[str, ...] = COORDINATION_PREFIXES,
    ):
        self.name = "routing"
        self.primary = primary
        self.coordination = coordination
        self._prefixes = tuple(coordination_prefixes)
        # Class-A = writes + lists (metered on R2); Class-B = reads/HEAD. Counted only for
        # the coordination backend so we can watch the R2 free-tier budget (review/17 §4).
        self._class_a = 0
        self._class_b = 0

    @property
    def cas_capable(self) -> bool:
        """True when a coordination backend that supports CAS is attached. ``put_cas``/``get_bytes``
        exist on this class unconditionally, so callers must check this (not ``hasattr``) before
        relying on compare-and-swap — with R2 creds absent the router degrades to B2-only and CAS
        on a coordination key would raise ``NotImplementedError``."""
        return self.coordination is not None and bool(
            getattr(self.coordination, "cas_capable", False)
        )

    def is_cas_managed_key(self, key: str) -> bool:
        """True if THIS router manages ``key`` by CAS on the coordination backend, so the bulk state
        sync must skip it (``statesync`` asks the storage rather than the module constant, which can
        diverge from a router built with custom ``coordination_prefixes``)."""
        return self.cas_capable and key.startswith(self._prefixes)

    # --- routing ---------------------------------------------------------------------

    def _is_coordination(self, key: str) -> bool:
        return self.coordination is not None and key.startswith(self._prefixes)

    def _route(self, key: str) -> StorageBackend:
        return self.coordination if self._is_coordination(key) else self.primary  # type: ignore[return-value]

    # --- StorageBackend Protocol -----------------------------------------------------

    def exists(self, key: str) -> bool:
        if self._is_coordination(key):
            self._class_b += 1
        return self._route(key).exists(key)

    def put_file(self, key: str, local_path: Path, content_type: str) -> str:
        if self._is_coordination(key):
            self._class_a += 1
        return self._route(key).put_file(key, local_path, content_type)

    def public_url(self, key: str) -> str:
        return self._route(key).public_url(key)

    def get_file(self, key: str, local_path: Path) -> bool:
        if self._is_coordination(key):
            self._class_b += 1
        return self._route(key).get_file(key, local_path)

    def get_range(self, key: str, start: int, end: int) -> bytes | None:
        if self._is_coordination(key):
            self._class_b += 1
        return self._route(key).get_range(key, start, end)

    def list_objects(self, prefix: str = ""):
        """List under ``prefix`` on the single backend that owns it — **namespace-scoped**.

        A prefix fully inside the coordination namespace lists R2; everything else (including a
        broad prefix like ``""`` or ``"state/"`` that straddles both namespaces) lists the B2
        primary only. This is deliberate, not a partial-result bug: coordination/lease objects on R2
        are **key-addressed and never enumerated** — the discovery path reads the B2-resident index
        and derives each key (review/18 §4.6 lever 1), so a list never spends an R2 Class-A op.
        Merging both backends here would (a) add an R2 Class-A to every broad B2 listing
        (``pull_state``, orphan GC) and (b) pull coordination objects into flows that must not
        manage them. Callers needing coordination objects address them by key, or list that
        *specific* coordination prefix (which routes to R2 correctly).
        """
        if self._is_coordination(prefix):
            self._class_a += 1  # listing is a Class-A op on R2
        return self._route(prefix).list_objects(prefix)

    def iter_objects(self, prefix: str = ""):
        """Size-bearing listing on the backend that owns ``prefix`` (see ``list_objects``)."""
        if self._is_coordination(prefix):
            self._class_a += 1  # listing is a Class-A op on R2
        return self._route(prefix).iter_objects(prefix)

    def delete(self, key: str) -> None:
        if self._is_coordination(key):
            self._class_a += 1
        self._route(key).delete(key)

    # --- compare-and-swap (coordination backend only) --------------------------------

    def get_bytes(self, key: str) -> tuple[bytes, str] | None:
        backend = self._route(key)
        # Check the routed backend's own cas_capable flag, not hasattr: S3CompatibleStorage
        # defines get_bytes/put_cas unconditionally, so hasattr is always True even for a
        # cas_capable=False (B2) backend — including when coordination is absent and a
        # coordination-prefixed key silently falls through to the primary (CR2-CP-53/H2).
        if not getattr(backend, "cas_capable", False):
            name = getattr(backend, "name", "?")
            raise NotImplementedError(f"backend {name!r} is not cas_capable; get_bytes unavailable")
        if self._is_coordination(key):
            self._class_b += 1
        return backend.get_bytes(key)

    def put_cas(
        self,
        key: str,
        data: bytes,
        content_type: str,
        *,
        if_none_match: str | None = None,
        if_match: str | None = None,
    ) -> tuple[str, str]:
        backend = self._route(key)
        if not getattr(backend, "cas_capable", False):
            name = getattr(backend, "name", "?")
            raise NotImplementedError(f"backend {name!r} is not cas_capable; put_cas unavailable")
        if self._is_coordination(key):
            self._class_a += 1
        return backend.put_cas(
            key, data, content_type, if_none_match=if_none_match, if_match=if_match
        )

    # --- telemetry -------------------------------------------------------------------

    def telemetry(self) -> dict[str, int]:
        """Cumulative R2 operation counts this process, by billing class (review/17 §4)."""
        return {"r2_class_a": self._class_a, "r2_class_b": self._class_b}
