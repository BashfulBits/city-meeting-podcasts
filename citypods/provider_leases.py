"""Cross-process provider concurrency slots, held as per-slot CAS objects (H17 PR6).

The in-process ``HostRateLimiter`` is still useful for threads inside one Python process, but
GitHub Actions audio shards are separate processes and cannot see each other's semaphores. This
module adds a small object-storage lease so an N-slot host cap (e.g. "≤6 concurrent fetches to
granicus.com") holds *across* those processes.

**Model — per-slot compare-and-swap (review/17 §5, review/18 §4).** A domain's N slots are N fixed
keys ``provider-leases/<domain>/slot-<i>.json`` (``i`` in ``0..N-1``), each an independent CAS
object. To acquire, a worker walks the slots from a per-owner offset and ``get_bytes`` each (a cheap
Class-B read); it claims a free slot with ``put_cas(if_none_match="*")`` or an *expired* slot (a
dead worker) with ``put_cas(if_match=<etag>)``. The independent ETags mean two workers claiming two
*different* slots never contend — the same retry-storm mitigation the Stage-2 ledger uses for
per-item leases, applied here to a fixed slot set. A held slot is renewed by a background CAS
rewrite and released by a delete.

This replaces the previous list-and-sort FIFO emulation (write a candidate object per waiter, then
**list** the prefix every poll to find the winners). Listing is itself a Class-A op on R2, so a
blocked waiter used to burn Class-A continuously; the CAS model spends Class-A only on a *claim*,
*renewal*, or *release* — never while merely waiting. The trade-off is that waiters no longer
acquire in strict FIFO arrival order (they poll and take whatever frees); the contract is the
concurrency *cap*, not fairness, so this is intended. The cap is **soft**: a reap-vs-release race
can briefly allow N+1 holders, which is acceptable for a rate limiter.

The pool needs a CAS-capable backend (R2, via ``RoutingStorage`` routing the ``provider-leases/``
prefix). On a non-CAS backend (b2-only / local dev) the distributed layer disables and only the
in-process ``HostRateLimiter`` applies — typically single-process there, so the cap still holds.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlsplit

from citypods.http import StopRequested
from citypods.storage.base import StorageBackend


@dataclass(frozen=True)
class ProviderLeaseRule:
    slots: int
    ttl_seconds: float = 3600.0
    poll_seconds: float = 2.0
    # ``settle_seconds`` is retained for config back-compat but is unused by the CAS model (there is
    # no write-then-settle race to absorb — a claim is a single atomic compare-and-swap).
    settle_seconds: float = 0.25


@dataclass(frozen=True)
class _LeaseMetadata:
    """Ownership/run context for a slot holder, parsed from its payload (or ``None`` fields for a
    legacy/unreadable payload). Used only to enrich stale-reap log lines."""

    owner: str | None = None
    state: str | None = None
    run_id: str | None = None
    run_attempt: str | None = None
    job: str | None = None
    shard: str | None = None

    def detail(self) -> str:
        """Concise ``" key=value ..."`` suffix for a log line; empty when nothing is known."""
        parts = [
            f"{label}={value}"
            for label, value in (
                ("owner", self.owner),
                ("run_id", self.run_id),
                ("run_attempt", self.run_attempt),
                ("job", self.job),
                ("shard", self.shard),
                ("state", self.state),
            )
            if value
        ]
        return f" {' '.join(parts)}" if parts else ""


@dataclass
class ProviderLease:
    """One acquired distributed slot with a renewable CAS-backed claim."""

    pool: DistributedProviderLeasePool
    domain: str
    key: str
    etag: str | None
    rule: ProviderLeaseRule
    wait_seconds: float
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None
    _lost: bool = False

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
    """Cross-process provider concurrency slots, held as per-slot CAS objects on R2."""

    def __init__(self, *, prefix: str = "provider-leases") -> None:
        self._prefix = prefix.strip("/")
        self._storage: StorageBackend | None = None
        self._rules: dict[str, ProviderLeaseRule] = {}
        self._owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}"
        self._log: Callable[[str], None] | None = None
        self._shard: str | None = None
        self._guard = threading.Lock()
        self._telemetry: dict[str, dict[str, int | float]] = {}
        self._waiting: dict[str, int] = {}

    def configure(
        self,
        storage: StorageBackend | None,
        config: Mapping[str, object] | None,
        *,
        log: Callable[[str], None] | None = None,
        shard: str | None = None,
    ) -> None:
        """Replace lease configuration.

        ``config`` accepts either ``{"granicus.com": 6}`` or
        ``{"granicus.com": {"slots": 6, "ttl_seconds": 3600, "poll_seconds": 2}}``. The distributed
        layer needs a **CAS-capable** backend (``get_bytes``/``put_cas``/``delete`` and a truthy
        ``cas_capable``) — in production that is R2 via ``RoutingStorage`` routing the
        ``provider-leases/`` prefix. A missing storage, a backend without those methods, or a
        non-CAS backend disables the pool, and only the in-process ``HostRateLimiter`` applies.
        ``shard`` is this process's ``K/N`` matrix shard label (the H6b sharded workflows), recorded
        in slot payloads/logs alongside the GitHub run/job env vars.
        """
        rules: dict[str, ProviderLeaseRule] = {}
        for domain, raw in (config or {}).items():
            rule = _parse_rule(raw)
            if rule is not None:
                rules[str(domain).strip().lower()] = rule

        required = ("get_bytes", "put_cas", "delete")
        cas_ok = bool(getattr(storage, "cas_capable", False))
        usable_storage = (
            storage
            if storage is not None and cas_ok and all(hasattr(storage, m) for m in required)
            else None
        )
        with self._guard:
            self._storage = usable_storage
            self._rules = rules if usable_storage is not None else {}
            self._log = log
            self._shard = shard
            self._telemetry = {}
            self._waiting = {}
        if rules and usable_storage is None and log is not None:
            _emit(log, "[enrich] provider lease disabled: storage backend lacks CAS support")

    def slots(
        self, urls: Iterable[str], *, stop: Callable[[], bool] | None = None
    ) -> _DistributedSlots:
        """``stop``, if given, is polled once per acquire iteration (every ``rule.poll_seconds``);
        if it fires before this process claims a slot, raises
        :class:`~citypods.http.StopRequested` instead of waiting out the queue."""
        keys: set[str] = set()
        for url in urls:
            host = (urlsplit(url).hostname or "").lower()
            key = self._key_for(host)
            if key is not None:
                keys.add(key)
        return _DistributedSlots(self, sorted(keys), stop=stop)

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

    def current_waiting_counts(self) -> dict[str, int]:
        """Live count of callers currently blocked in :meth:`_acquire` per domain, for
        heartbeat/stall diagnostics — ``telemetry()`` is cumulative and can't show a live queue."""
        with self._guard:
            return {domain: count for domain, count in self._waiting.items() if count > 0}

    def _acquire(self, domain: str, *, stop: Callable[[], bool] | None = None) -> ProviderLease:
        started = time.monotonic()
        waiting_logged = False
        counted = False
        try:
            while True:
                if stop is not None and stop():
                    raise StopRequested(f"provider lease wait for domain={domain!r} stopped")
                with self._guard:
                    storage = self._storage
                    rule = self._rules[domain]
                    log = self._log
                if storage is None:
                    return ProviderLease(self, domain, "", None, rule, 0.0)
                if not counted:
                    with self._guard:
                        self._waiting[domain] = self._waiting.get(domain, 0) + 1
                    counted = True

                lease = self._try_one_pass(storage, domain, rule, started, log)
                if lease is not None:
                    return lease

                if not waiting_logged and log is not None:
                    _emit(
                        log,
                        f"[enrich] provider lease wait domain={domain} slots={rule.slots}",
                    )
                    waiting_logged = True
                time.sleep(max(0.05, rule.poll_seconds))
        finally:
            if counted:
                with self._guard:
                    self._waiting[domain] = max(0, self._waiting.get(domain, 0) - 1)

    def _try_one_pass(
        self,
        storage: StorageBackend,
        domain: str,
        rule: ProviderLeaseRule,
        started: float,
        log: Callable[[str], None] | None,
    ) -> ProviderLease | None:
        """One sweep over the domain's slots from this owner's offset; claims the first free or
        expired slot via CAS. Returns the held lease, or ``None`` if every slot is occupied."""
        offset = self._scan_offset(domain, rule.slots)
        now = datetime.now(UTC)
        for j in range(rule.slots):
            index = (offset + j) % rule.slots
            key = self._slot_key(domain, index)
            got = storage.get_bytes(key)  # Class-B read-before-claim
            if got is None:
                etag = self._try_claim(storage, key, domain, rule, if_none_match="*")
                if etag is not None:
                    return self._finish(domain, key, etag, rule, started, log)
                continue  # a sibling claimed this empty slot first
            data, current_etag = got
            expired, metadata = self._expiry(data, now, rule)
            if not expired:
                continue  # held by a live worker
            etag = self._try_claim(storage, key, domain, rule, if_match=current_etag)
            if etag is not None:
                self._increment(domain, "stale_leases_reaped")
                if log is not None:
                    detail = metadata.detail() if metadata is not None else ""
                    _emit(log, f"[enrich] provider lease stale reaped domain={domain}{detail}")
                return self._finish(domain, key, etag, rule, started, log)
        return None

    def _finish(
        self,
        domain: str,
        key: str,
        etag: str,
        rule: ProviderLeaseRule,
        started: float,
        log: Callable[[str], None] | None,
    ) -> ProviderLease:
        wait_seconds = time.monotonic() - started
        lease = ProviderLease(self, domain, key, etag, rule, wait_seconds)
        self._record_acquisition(domain, wait_seconds)
        if log is not None:
            _emit(
                log,
                f"[enrich] provider lease acquired domain={domain} wait_seconds={wait_seconds:.1f}",
            )
        lease.start_renewal()
        return lease

    def _try_claim(
        self,
        storage: StorageBackend,
        key: str,
        domain: str,
        rule: ProviderLeaseRule,
        *,
        if_none_match: str | None = None,
        if_match: str | None = None,
    ) -> str | None:
        """Write our claim to ``key`` via CAS; return the new ETag, or ``None`` on a 412 conflict
        (a sibling won the slot between our read and our write)."""
        from citypods.storage import CASConflict

        try:
            _url, etag = storage.put_cas(
                key,
                self._payload(domain, rule),
                "application/json",
                if_none_match=if_none_match,
                if_match=if_match,
            )
            return etag
        except CASConflict:
            return None

    def _release(self, lease: ProviderLease) -> None:
        if not lease.key:
            return
        with self._guard:
            storage = self._storage
            log = self._log
        if storage is None:
            return
        if lease._lost:
            # The renewal thread observed a CAS conflict — the slot was reaped and re-claimed by
            # another worker, so it is no longer ours to delete.
            if log is not None:
                detail = self._self_metadata(state="released").detail()
                _emit(
                    log,
                    f"[enrich] provider lease release skipped (slot reaped) "
                    f"domain={lease.domain}{detail}",
                )
            return
        try:
            storage.delete(lease.key)
            if log is not None:
                detail = self._self_metadata(state="released").detail()
                _emit(log, f"[enrich] provider lease released domain={lease.domain}{detail}")
        except Exception as exc:  # noqa: BLE001 - release should not mask the encode result
            if log is not None:
                detail = self._self_metadata(state="released").detail()
                _emit(
                    log,
                    f"[enrich] provider lease release failed domain={lease.domain}{detail}: {exc}",
                )

    def _renew_while_held(self, lease: ProviderLease) -> None:
        from citypods.storage import CASConflict

        interval = self._renew_interval(lease.rule)
        while not lease._stop.wait(interval):
            with self._guard:
                storage = self._storage
                log = self._log
            if storage is None:
                return
            try:
                _url, etag = storage.put_cas(
                    lease.key,
                    self._payload(lease.domain, lease.rule),
                    "application/json",
                    if_match=lease.etag,
                )
                lease.etag = etag
                self._increment(lease.domain, "lease_renewals")
            except CASConflict:
                # We over-ran the TTL: a reaper declared us stale and another worker took the slot.
                # Stop renewing and flag the lease so ``release`` does not delete a slot we no
                # longer own. (Generous TTL + a renew cadence of TTL/3 makes this rare.)
                lease._lost = True
                if log is not None:
                    detail = self._self_metadata(state="acquired").detail()
                    _emit(
                        log,
                        f"[enrich] provider lease renewal lost slot (reaped) "
                        f"domain={lease.domain}{detail}",
                    )
                return
            except Exception as exc:  # noqa: BLE001 - the holder continues until normal release
                if log is not None:
                    detail = self._self_metadata(state="acquired").detail()
                    _emit(
                        log,
                        f"[enrich] provider lease renewal failed domain={lease.domain}{detail}: "
                        f"{exc}",
                    )

    @staticmethod
    def _renew_interval(rule: ProviderLeaseRule) -> float:
        return max(0.05, min(60.0, max(0.15, rule.ttl_seconds) / 3.0))

    def _expiry(
        self, data: bytes, now: datetime, rule: ProviderLeaseRule
    ) -> tuple[bool, _LeaseMetadata | None]:
        """Decide whether a slot payload represents an expired (reclaimable) holder, and parse its
        ownership metadata for logging. An unreadable payload or one missing ``expires_at`` is
        treated as expired so a corrupt slot can never wedge the domain forever."""
        try:
            payload = json.loads(data)
            raw = payload.get("expires_at")
            expires = datetime.fromisoformat(raw) if raw else None
            if expires is not None and expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            metadata = _LeaseMetadata(
                owner=str(payload.get("owner") or "") or None,
                state=str(payload.get("state") or "") or None,
                run_id=str(payload.get("github_run_id") or "") or None,
                run_attempt=str(payload.get("github_run_attempt") or "") or None,
                job=str(payload.get("github_job") or "") or None,
                shard=str(payload.get("github_shard") or "") or None,
            )
        except (AttributeError, TypeError, ValueError):
            return True, None
        if expires is None:
            return True, metadata
        return expires <= now, metadata

    def _self_metadata(self, *, state: str) -> _LeaseMetadata:
        """Ownership/run context for a slot this process holds or just released."""
        with self._guard:
            shard = self._shard
        return _LeaseMetadata(
            owner=self._owner,
            state=state,
            run_id=os.environ.get("GITHUB_RUN_ID") or None,
            run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT") or None,
            job=os.environ.get("GITHUB_JOB") or None,
            shard=shard,
        )

    def _payload(self, domain: str, rule: ProviderLeaseRule) -> bytes:
        now = datetime.now(UTC)
        metadata = self._self_metadata(state="acquired")
        fields = {
            "owner": metadata.owner,
            "domain": domain,
            "state": metadata.state,
            "renewed_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=rule.ttl_seconds)).isoformat(),
        }
        extra = {
            "github_run_id": metadata.run_id,
            "github_run_attempt": metadata.run_attempt,
            "github_job": metadata.job,
            "github_shard": metadata.shard,
        }
        fields.update({key: value for key, value in extra.items() if value})
        return json.dumps(fields, sort_keys=True).encode("utf-8")

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

    def _slot_key(self, domain: str, index: int) -> str:
        return f"{self._domain_prefix(domain)}slot-{index}.json"

    def _scan_offset(self, domain: str, slots: int) -> int:
        """Per-owner start offset over the slot set, so N workers target N different slots first
        instead of all colliding on slot 0 (review/18 §4.6 lever 2, applied to slots)."""
        if slots <= 1:
            return 0
        digest = hashlib.sha1(f"{self._owner}:{domain}".encode()).hexdigest()
        return int(digest, 16) % slots


class _DistributedSlots:
    def __init__(
        self,
        pool: DistributedProviderLeasePool,
        domains: list[str],
        *,
        stop: Callable[[], bool] | None = None,
    ) -> None:
        self._pool = pool
        self._domains = domains
        self._stop = stop
        self._leases: list[ProviderLease] = []

    def __enter__(self) -> None:
        self._leases = []
        try:
            for domain in self._domains:
                self._leases.append(self._pool._acquire(domain, stop=self._stop))
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
