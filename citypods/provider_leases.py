"""Storage-backed provider leases shared by sharded workflow jobs.

The in-process ``HostRateLimiter`` is still useful for threads inside one Python process, but
GitHub Actions audio shards are separate processes and cannot see each other's semaphores.  This
module adds a tiny object-storage soft lease: a worker writes a unique candidate object, lists the
active candidates, and proceeds only when its candidate sorts into the configured winner set.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit

from citypods.storage.base import StorageBackend


@dataclass(frozen=True)
class ProviderLeaseRule:
    slots: int
    ttl_seconds: float = 3600.0
    poll_seconds: float = 2.0
    settle_seconds: float = 0.25


class DistributedProviderLeasePool:
    """Cross-process provider concurrency slots stored as object-storage lease files."""

    def __init__(self, *, prefix: str = "provider-leases") -> None:
        self._prefix = prefix.strip("/")
        self._storage: StorageBackend | None = None
        self._rules: dict[str, ProviderLeaseRule] = {}
        self._owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}"
        self._log: Callable[[str], None] | None = None
        self._guard = threading.Lock()
        self._wait_logged: set[str] = set()

    def configure(
        self,
        storage: StorageBackend | None,
        config: Mapping[str, object] | None,
        *,
        log: Callable[[str], None] | None = None,
    ) -> None:
        """Replace lease configuration.

        ``config`` accepts either ``{"granicus.com": 6}`` or
        ``{"granicus.com": {"slots": 6, "ttl_seconds": 3600, "poll_seconds": 2}}``. Missing
        storage or a backend without the required methods disables the pool. The protocol uses only
        ordinary object upload/list/delete operations so it works with B2's S3-compatible API.
        """
        rules: dict[str, ProviderLeaseRule] = {}
        for domain, raw in (config or {}).items():
            rule = _parse_rule(raw)
            if rule is not None:
                rules[str(domain).strip().lower()] = rule

        required = ("put_file", "list_objects", "delete")
        usable_storage = (
            storage if storage is not None and all(hasattr(storage, m) for m in required) else None
        )
        with self._guard:
            self._storage = usable_storage
            self._rules = rules if usable_storage is not None else {}
            self._log = log
            self._wait_logged = set()
        if rules and usable_storage is None and log is not None:
            _emit(log, "[enrich] provider lease disabled: storage backend lacks lease support")

    def slots(self, urls: Iterable[str]) -> _DistributedSlots:
        keys: set[str] = set()
        for url in urls:
            host = (urlsplit(url).hostname or "").lower()
            key = self._key_for(host)
            if key is not None:
                keys.add(key)
        return _DistributedSlots(self, sorted(keys))

    def _key_for(self, host: str) -> str | None:
        host = (host or "").lower()
        with self._guard:
            rules = self._rules
        best: str | None = None
        for key in rules:
            if host == key or host.endswith("." + key):
                if best is None or len(key) > len(best):
                    best = key
        return best

    def _acquire(self, domain: str) -> str:
        key: str | None = None
        while True:
            with self._guard:
                storage = self._storage
                rule = self._rules[domain]
                log = self._log
            if storage is None:
                return ""

            if key is None:
                key = self._candidate_key(domain)
                self._write_candidate(storage, key, self._payload(domain, rule))
                if rule.settle_seconds > 0:
                    time.sleep(rule.settle_seconds)

            self._drop_stale(storage, domain, rule)
            winners = self._winners(storage, domain, rule)
            if key in winners:
                return key

            with self._guard:
                first_wait = domain not in self._wait_logged
                self._wait_logged.add(domain)
            if first_wait and log is not None:
                _emit(
                    log,
                    f"[enrich] provider lease wait domain={domain} slots={rule.slots}",
                )
            time.sleep(max(0.1, rule.poll_seconds))

    def _write_candidate(self, storage: StorageBackend, key: str, payload: str) -> None:
        with tempfile.TemporaryDirectory(prefix="citypods_lease_") as tmp:
            path = Path(tmp) / "lease.json"
            path.write_text(payload, encoding="utf-8")
            storage.put_file(key, path, "application/json; charset=utf-8")

    def _release(self, key: str) -> None:
        if not key:
            return
        with self._guard:
            storage = self._storage
            log = self._log
        if storage is None:
            return
        try:
            storage.delete(key)
        except Exception as exc:  # noqa: BLE001 - release should not mask the encode result
            if log is not None:
                _emit(log, f"[enrich] provider lease release failed key={key}: {exc}")

    def _drop_stale(self, storage: StorageBackend, domain: str, rule: ProviderLeaseRule) -> None:
        cutoff = datetime.now(UTC) - timedelta(seconds=max(1.0, rule.ttl_seconds))
        prefix = self._domain_prefix(domain)
        for key, modified in storage.list_objects(prefix):
            if modified is None:
                continue
            if modified.tzinfo is None:
                modified = modified.replace(tzinfo=UTC)
            if modified < cutoff:
                storage.delete(key)

    def _winners(self, storage: StorageBackend, domain: str, rule: ProviderLeaseRule) -> set[str]:
        prefix = self._domain_prefix(domain)
        now = datetime.now(UTC)
        candidates: list[tuple[datetime, str]] = []
        for key, modified in storage.list_objects(prefix):
            if modified is None:
                modified = now
            elif modified.tzinfo is None:
                modified = modified.replace(tzinfo=UTC)
            candidates.append((modified, key))
        candidates.sort(key=lambda item: (item[0], item[1]))
        return {key for _, key in candidates[: rule.slots]}

    def _payload(self, domain: str, rule: ProviderLeaseRule) -> str:
        now = datetime.now(UTC)
        return json.dumps(
            {
                "owner": self._owner,
                "domain": domain,
                "acquired_at": now.isoformat(),
                "expires_at": (now + timedelta(seconds=rule.ttl_seconds)).isoformat(),
            },
            sort_keys=True,
        )

    def _domain_prefix(self, domain: str) -> str:
        return f"{self._prefix}/{quote(domain, safe='')}/"

    def _candidate_key(self, domain: str) -> str:
        token = quote(f"{time.time_ns()}:{self._owner}:{uuid.uuid4().hex}", safe="")
        return f"{self._domain_prefix(domain)}{token}.json"


class _DistributedSlots:
    def __init__(self, pool: DistributedProviderLeasePool, domains: list[str]) -> None:
        self._pool = pool
        self._domains = domains
        self._keys: list[str] = []

    def __enter__(self) -> None:
        self._keys = []
        try:
            for domain in self._domains:
                self._keys.append(self._pool._acquire(domain))
        except Exception:
            for key in reversed(self._keys):
                self._pool._release(key)
            self._keys = []
            raise

    def __exit__(self, *_exc: object) -> None:
        for key in reversed(self._keys):
            self._pool._release(key)


def _parse_rule(raw: object) -> ProviderLeaseRule | None:
    if isinstance(raw, Mapping):
        try:
            slots = int(raw.get("slots", 0))
        except (TypeError, ValueError):
            return None
        if slots <= 0:
            return None
        return ProviderLeaseRule(
            slots=slots,
            ttl_seconds=float(raw.get("ttl_seconds", 3600.0)),
            poll_seconds=float(raw.get("poll_seconds", 2.0)),
            settle_seconds=float(raw.get("settle_seconds", 0.25)),
        )
    try:
        slots = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return ProviderLeaseRule(slots=slots) if slots > 0 else None


def _emit(log: Callable[[str], None], message: str) -> None:
    try:
        log(message, flush=True)  # type: ignore[call-arg]
    except TypeError:
        log(message)


DISTRIBUTED_PROVIDER_LEASES = DistributedProviderLeasePool()
