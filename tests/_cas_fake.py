"""A thread-safe in-memory compare-and-swap store for tests (stands in for R2).

The H17 control plane (work-lease ledger, provider concurrency slots) needs real CAS —
``get_bytes``/``put_cas``/``delete`` plus a truthy ``cas_capable`` — which LocalStorage (a B2-like
primary) deliberately does not provide. This fake supplies exactly that surface and counts ops by
billing class so cost discipline can be asserted. It is intentionally **listing-hostile**:
``list_objects`` raises, because the control plane must never list a coordination prefix on R2.
"""

from __future__ import annotations

import threading
from pathlib import Path

from citypods.storage import CASConflict


class MemCAS:
    """Thread-safe in-memory CAS store. ``class_a`` = writes/deletes; ``class_b`` = reads."""

    cas_capable = True

    def __init__(self) -> None:
        self.objs: dict[str, bytes] = {}
        self.etags: dict[str, str] = {}
        self._n = 0
        self._lock = threading.Lock()
        self.class_a = 0
        self.class_b = 0

    def _bump(self, key: str) -> str:
        self._n += 1
        self.etags[key] = f'"e{self._n}"'
        return self.etags[key]

    def get_bytes(self, key):
        with self._lock:
            self.class_b += 1
            if key not in self.objs:
                return None
            return self.objs[key], self.etags[key]

    def put_cas(self, key, data, content_type, *, if_none_match=None, if_match=None):
        with self._lock:
            self.class_a += 1
            exists = key in self.objs
            if if_none_match == "*" and exists:
                raise CASConflict(key)
            if if_match is not None and (not exists or self.etags.get(key) != if_match):
                raise CASConflict(key)
            self.objs[key] = data if isinstance(data, bytes) else data.encode()
            return "mem://" + key, self._bump(key)

    def delete(self, key):
        with self._lock:
            self.class_a += 1
            self.objs.pop(key, None)
            self.etags.pop(key, None)

    def list_objects(self, prefix=""):
        raise AssertionError("provider leases / work-lease ledger must never list the prefix")

    # --- test inspection only (not part of the surface the control plane uses) ---
    def keys(self, prefix: str = "") -> list[str]:
        with self._lock:
            return sorted(k for k in self.objs if k.startswith(prefix))

    def seed(self, key: str, body: bytes) -> None:
        """Place an object directly (bypassing CAS) to simulate a pre-existing/foreign holder."""
        with self._lock:
            self.objs[key] = body
            self._n += 1
            self.etags[key] = f'"seed{self._n}"'


class MemStorage(MemCAS):
    """``MemCAS`` plus the bulk (B2-like) file interface -- for tests needing a storage double
    that supports both at once, the way production ``RoutingStorage`` dispatches by key prefix to
    R2 (CAS) or B2 (bulk). `LiteLLMBackend` is the concrete case: it uses CAS for its quota ledger
    (``state/llm_budget.json``, a coordination prefix) and the bulk interface for its
    deferred-request registry (``state/llm_deferred/*.json``, not one).

    Unlike ``MemCAS``, ``list_objects`` here is **not** listing-hostile: only R2 coordination
    state must never be listed (that invariant is what ``MemCAS.list_objects`` itself guards for
    every CAS-only caller); this fake's bulk surface represents B2, where listing pending records
    is the normal, expected pattern the deferred-request sweep relies on.
    """

    def put_file(self, key, local_path, content_type):
        with self._lock:
            self.class_a += 1
            self.objs[key] = Path(local_path).read_bytes()
            self._bump(key)
            return "mem://" + key

    def get_file(self, key, local_path):
        with self._lock:
            self.class_b += 1
            if key not in self.objs:
                return False
            data = self.objs[key]
        path = Path(local_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return True

    def list_objects(self, prefix=""):
        with self._lock:
            keys = sorted(k for k in self.objs if k.startswith(prefix))
        for key in keys:
            yield key, None

    def exists(self, key):
        with self._lock:
            return key in self.objs
