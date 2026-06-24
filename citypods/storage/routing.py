"""Prefix-routing storage that splits state across two backends (review/17 §5).

Most state — content-addressed audio/transcripts and append-only logs — stays on the
*primary* backend (B2), where writes are free and consistency is strong. Only the
coordination control-plane (provider circuits/leases, work/budget) and the Stage-2 lease
ledger, which genuinely need compare-and-swap, route to the *coordination* backend (R2).

``RoutingStorage`` implements the ``StorageBackend`` Protocol and dispatches each call by
key prefix, so callers keep using one storage object unchanged. CAS-only methods
(``get_bytes``/``put_cas``) delegate to the coordination backend.

The set of coordination prefixes is passed in (``COORDINATION_PREFIXES`` is the production
default). It is **empty today** — routing is a deliberate no-op so this substrate lands
without moving any artifact; later changes add prefixes as each artifact migrates to R2.
"""

from __future__ import annotations

from pathlib import Path

from citypods.storage.base import StorageBackend

# Keys whose prefix matches route to the coordination (CAS) backend. Each migration appends its
# prefix here with its own change:
#   - ``state/compute_budget.json`` (H17 PR3): free-tier GPU ledger — atomic-decrement overspend
#     guard; lives on R2, accessed by CAS, excluded from the bulk B2 state sync.
#   - ``work-leases/`` (H17 PR4): the Stage-2 per-item competitive-claim ledger (review/18 §4); each
#     ``work-leases/<source>/<uid>.json`` is an independent CAS object. Derived/GET by key, never
#     listed (listing is a Class-A op on R2).
COORDINATION_PREFIXES: tuple[str, ...] = ("state/compute_budget.json", "work-leases/")


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
        if not hasattr(backend, "get_bytes"):
            raise NotImplementedError(f"backend {getattr(backend, 'name', '?')!r} lacks get_bytes")
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
        if not hasattr(backend, "put_cas"):
            raise NotImplementedError(f"backend {getattr(backend, 'name', '?')!r} lacks put_cas")
        if self._is_coordination(key):
            self._class_a += 1
        return backend.put_cas(
            key, data, content_type, if_none_match=if_none_match, if_match=if_match
        )

    # --- telemetry -------------------------------------------------------------------

    def telemetry(self) -> dict[str, int]:
        """Cumulative R2 operation counts this process, by billing class (review/17 §4)."""
        return {"r2_class_a": self._class_a, "r2_class_b": self._class_b}
