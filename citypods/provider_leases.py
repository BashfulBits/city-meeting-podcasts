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
from dataclasses import dataclass, field
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


@dataclass
class ProviderLease:
    """One acquired distributed slot with a renewable storage-backed claim."""

    pool: DistributedProviderLeasePool
    domain: str
    key: str
    rule: ProviderLeaseRule
    wait_seconds: float
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def start_renewal(self) -> None:
        if not self.key:
            return
        self._thread = threading.Thread(
            target=self.pool._renew_while_held,
            args=(self,),
            name=f"provider-lease-{self.domain}",
            daemon=True,
        )
        self._thread.start()

    def release(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.pool._renew_interval(self.rule) + 1.0))
            self._thread = None
        self.pool._release(self)


class DistributedProviderLeasePool:
    """Cross-process provider concurrency slots stored as object-storage lease files."""

    def __init__(self, *, prefix: str = "provider-leases") -> None:
        self._prefix = prefix.strip("/")
        self._storage: StorageBackend | None = None
        self._rules: dict[str, ProviderLeaseRule] = {}
        self._owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}"
        self._log: Callable[[str], None] | None = None
        self._guard = threading.Lock()
        self._telemetry: dict[str, dict[str, int | float]] = {}
        self._expiry_cache: dict[str, tuple[datetime | None, datetime | None, str | None]] = {}

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
        ``get_file`` is optional: when available it makes payload expiry authoritative; otherwise
        object modification time plus TTL is the compatibility fallback.
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
            self._telemetry = {}
            self._expiry_cache = {}
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

    def telemetry(self) -> dict[str, dict[str, int | float]]:
        """Return cumulative per-domain lease activity for the current run."""
        with self._guard:
            return {
                domain: {
                    "lease_acquisitions": int(values.get("lease_acquisitions", 0)),
                    "lease_wait_seconds": round(float(values.get("lease_wait_seconds", 0.0)), 1),
                    "lease_max_wait_seconds": round(
                        float(values.get("lease_max_wait_seconds", 0.0)), 1
                    ),
                    "stale_leases_reaped": int(values.get("stale_leases_reaped", 0)),
                    "lease_renewals": int(values.get("lease_renewals", 0)),
                }
                for domain, values in self._telemetry.items()
            }

    def _acquire(self, domain: str) -> ProviderLease:
        key: str | None = None
        started = time.monotonic()
        last_renewed = started
        waiting_logged = False
        try:
            while True:
                with self._guard:
                    storage = self._storage
                    rule = self._rules[domain]
                    log = self._log
                if storage is None:
                    return ProviderLease(self, domain, "", rule, 0.0)

                if key is None:
                    key = self._candidate_key(domain)
                    self._write_candidate(storage, key, self._payload(domain, rule, acquired=False))
                    if rule.settle_seconds > 0:
                        time.sleep(rule.settle_seconds)

                now = time.monotonic()
                if now - last_renewed >= self._renew_interval(rule):
                    self._write_candidate(storage, key, self._payload(domain, rule, acquired=False))
                    self._increment(domain, "lease_renewals")
                    last_renewed = now

                self._drop_stale(storage, domain, rule)
                winners = self._winners(storage, domain, rule)
                if key in winners:
                    wait_seconds = time.monotonic() - started
                    lease = ProviderLease(self, domain, key, rule, wait_seconds)
                    self._write_candidate(storage, key, self._payload(domain, rule, acquired=True))
                    self._record_acquisition(domain, wait_seconds)
                    if log is not None:
                        _emit(
                            log,
                            f"[enrich] provider lease acquired domain={domain} "
                            f"wait_seconds={wait_seconds:.1f}",
                        )
                    lease.start_renewal()
                    return lease

                if not waiting_logged and log is not None:
                    _emit(
                        log,
                        f"[enrich] provider lease wait domain={domain} slots={rule.slots}",
                    )
                    waiting_logged = True
                time.sleep(max(0.1, rule.poll_seconds))
        except BaseException:
            if key is not None:
                self._delete_key(key)
            raise

    def _write_candidate(self, storage: StorageBackend, key: str, payload: str) -> None:
        with tempfile.TemporaryDirectory(prefix="citypods_lease_") as tmp:
            path = Path(tmp) / "lease.json"
            path.write_text(payload, encoding="utf-8")
            storage.put_file(key, path, "application/json; charset=utf-8")

    def _release(self, lease: ProviderLease) -> None:
        if not lease.key:
            return
        with self._guard:
            storage = self._storage
            log = self._log
        if storage is None:
            return
        try:
            storage.delete(lease.key)
            with self._guard:
                self._expiry_cache.pop(lease.key, None)
            if log is not None:
                _emit(log, f"[enrich] provider lease released domain={lease.domain}")
        except Exception as exc:  # noqa: BLE001 - release should not mask the encode result
            if log is not None:
                _emit(
                    log,
                    f"[enrich] provider lease release failed domain={lease.domain}: {exc}",
                )

    def _delete_key(self, key: str) -> None:
        with self._guard:
            storage = self._storage
        if storage is not None:
            try:
                storage.delete(key)
                with self._guard:
                    self._expiry_cache.pop(key, None)
            except Exception:  # noqa: BLE001 - cleanup must preserve the original exception
                pass

    def _renew_while_held(self, lease: ProviderLease) -> None:
        interval = self._renew_interval(lease.rule)
        while not lease._stop.wait(interval):
            with self._guard:
                storage = self._storage
                log = self._log
            if storage is None:
                return
            try:
                self._write_candidate(
                    storage,
                    lease.key,
                    self._payload(lease.domain, lease.rule, acquired=True),
                )
                self._increment(lease.domain, "lease_renewals")
            except Exception as exc:  # noqa: BLE001 - the holder continues until normal release
                if log is not None:
                    _emit(
                        log,
                        f"[enrich] provider lease renewal failed domain={lease.domain}: {exc}",
                    )

    @staticmethod
    def _renew_interval(rule: ProviderLeaseRule) -> float:
        return max(0.05, min(60.0, max(0.15, rule.ttl_seconds) / 3.0))

    def _drop_stale(self, storage: StorageBackend, domain: str, rule: ProviderLeaseRule) -> None:
        now = datetime.now(UTC)
        prefix = self._domain_prefix(domain)
        for key, modified in storage.list_objects(prefix):
            expires_at, owner = self._candidate_expiry(storage, key, modified, rule)
            if expires_at is not None and expires_at < now:
                storage.delete(key)
                with self._guard:
                    self._expiry_cache.pop(key, None)
                self._increment(domain, "stale_leases_reaped")
                with self._guard:
                    log = self._log
                if log is not None:
                    owner_detail = f" owner={owner}" if owner else ""
                    _emit(
                        log,
                        f"[enrich] provider lease stale reaped domain={domain}{owner_detail}",
                    )

    def _winners(self, storage: StorageBackend, domain: str, rule: ProviderLeaseRule) -> set[str]:
        prefix = self._domain_prefix(domain)
        # Candidate keys begin with time.time_ns(), so key order is stable FIFO order. Renewal
        # rewrites the object and changes its modification time; sorting by modification time would
        # incorrectly demote an active holder behind waiters and allow overlapping winners.
        candidates = sorted(key for key, _modified in storage.list_objects(prefix))
        return set(candidates[: rule.slots])

    def _candidate_expiry(
        self,
        storage: StorageBackend,
        key: str,
        modified: datetime | None,
        rule: ProviderLeaseRule,
    ) -> tuple[datetime | None, str | None]:
        if hasattr(storage, "get_file"):
            with self._guard:
                cached = self._expiry_cache.get(key)
            if cached is not None and modified is not None and cached[0] == modified:
                return cached[1], cached[2]
            try:
                with tempfile.TemporaryDirectory(prefix="citypods_lease_read_") as tmp:
                    path = Path(tmp) / "lease.json"
                    if storage.get_file(key, path):
                        payload = json.loads(path.read_text(encoding="utf-8"))
                        expires_raw = payload.get("expires_at")
                        expires = datetime.fromisoformat(expires_raw) if expires_raw else None
                        if expires is not None and expires.tzinfo is None:
                            expires = expires.replace(tzinfo=UTC)
                        owner = str(payload.get("owner") or "") or None
                        with self._guard:
                            self._expiry_cache[key] = (modified, expires, owner)
                        return expires, owner
            except Exception:  # noqa: BLE001 - optional payload read falls back to object metadata
                pass
        if modified is None:
            return None, None
        if modified.tzinfo is None:
            modified = modified.replace(tzinfo=UTC)
        return modified + timedelta(seconds=max(0.1, rule.ttl_seconds)), None

    def _payload(self, domain: str, rule: ProviderLeaseRule, *, acquired: bool) -> str:
        now = datetime.now(UTC)
        metadata = {
            key: value
            for key, value in {
                "github_run_id": os.environ.get("GITHUB_RUN_ID"),
                "github_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
                "github_job": os.environ.get("GITHUB_JOB"),
            }.items()
            if value
        }
        return json.dumps(
            {
                "owner": self._owner,
                "domain": domain,
                "state": "acquired" if acquired else "waiting",
                "renewed_at": now.isoformat(),
                "expires_at": (now + timedelta(seconds=rule.ttl_seconds)).isoformat(),
                **metadata,
            },
            sort_keys=True,
        )

    def _record_acquisition(self, domain: str, wait_seconds: float) -> None:
        with self._guard:
            values = self._telemetry.setdefault(domain, {})
            values["lease_acquisitions"] = int(values.get("lease_acquisitions", 0)) + 1
            values["lease_wait_seconds"] = (
                float(values.get("lease_wait_seconds", 0.0)) + wait_seconds
            )
            values["lease_max_wait_seconds"] = max(
                float(values.get("lease_max_wait_seconds", 0.0)), wait_seconds
            )

    def _increment(self, domain: str, key: str) -> None:
        with self._guard:
            values = self._telemetry.setdefault(domain, {})
            values[key] = int(values.get(key, 0)) + 1

    def _domain_prefix(self, domain: str) -> str:
        return f"{self._prefix}/{quote(domain, safe='')}/"

    def _candidate_key(self, domain: str) -> str:
        token = quote(f"{time.time_ns()}:{self._owner}:{uuid.uuid4().hex}", safe="")
        return f"{self._domain_prefix(domain)}{token}.json"


class _DistributedSlots:
    def __init__(self, pool: DistributedProviderLeasePool, domains: list[str]) -> None:
        self._pool = pool
        self._domains = domains
        self._leases: list[ProviderLease] = []

    def __enter__(self) -> None:
        self._leases = []
        try:
            for domain in self._domains:
                self._leases.append(self._pool._acquire(domain))
        except BaseException:
            for lease in reversed(self._leases):
                lease.release()
            self._leases = []
            raise

    def __exit__(self, *_exc: object) -> None:
        for lease in reversed(self._leases):
            lease.release()


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
