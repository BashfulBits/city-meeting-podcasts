"""Storage-coordinated media throttle circuits shared by Audio shards.

Provider leases cap concurrent reads, while this module coordinates what happens after those reads
start returning 403/429. Granicus circuits are tenant-scoped (derived from the archive path or
tenant subdomain); a domain-wide emergency circuit opens only after multiple tenant circuits trip
within the same cooldown window.

The storage backend has no compare-and-swap primitive. Mutations therefore use the same ordinary
upload/list/delete FIFO lease protocol as provider concurrency, under a separate one-slot prefix.
Open/failure state uses deterministic JSON objects and is safe because every writer holds that
mutex.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from citypods.provider_leases import DistributedProviderLeasePool
from citypods.storage.base import StorageBackend


@dataclass(frozen=True)
class RateLimitCircuitRule:
    threshold: int = 3
    cooldown_seconds: float = 1800.0
    emergency_tenants: int = 2
    probe_ttl_seconds: float = 900.0


class MediaRateLimitCircuitBreaker:
    """Provider throttle circuit with local fallback and optional shared storage state."""

    def __init__(
        self,
        config: Mapping[str, object] | None = None,
        *,
        storage: StorageBackend | None = None,
        log: Callable[[str], None] | None = None,
        shard: str | None = None,
        prefix: str = "provider-circuits",
    ) -> None:
        self._rules: dict[str, RateLimitCircuitRule] = {}
        for domain, raw in (config or {}).items():
            rule = _parse_circuit_rule(raw)
            if rule is not None:
                self._rules[str(domain).strip().lower()] = rule
        required = ("exists", "put_file", "get_file", "delete")
        self._storage = (
            storage
            if storage is not None and all(hasattr(storage, name) for name in required)
            else None
        )
        self._prefix = prefix.strip("/")
        self._owner = f"{os.getpid()}:{shard or 'local'}:{uuid.uuid4().hex}"
        self._log = log
        self._lock = threading.RLock()
        self._failures: dict[str, int] = {}
        self._open_until: dict[str, float] = {}
        self._half_open: set[str] = set()
        self._known_open: set[str] = set()
        self._claimed_probes: set[str] = set()
        self._telemetry: dict[str, dict[str, int]] = {}
        self._mutation_pool = DistributedProviderLeasePool(prefix=f"{self._prefix}-locks")
        lock_config = {
            domain: {
                "slots": 1,
                # Mutations are only a few object reads/writes. Keep crash recovery short and let
                # the lease's normal renewal protect a legitimately slow storage call.
                "ttl_seconds": 60.0,
                "poll_seconds": 0.25,
                "settle_seconds": 0.05,
            }
            for domain, rule in self._rules.items()
        }
        self._mutation_pool.configure(storage, lock_config, log=None, shard=shard)
        if self._rules and storage is not None and self._storage is None and log is not None:
            _emit(
                log,
                "[enrich] provider circuit shared state disabled: storage lacks read support",
            )

    def has_rules(self) -> bool:
        return bool(self._rules)

    def telemetry(self) -> dict[str, dict[str, int]]:
        fields = (
            "rate_limited",
            "circuit_trips",
            "circuit_deferred",
            "recovery_probes",
            "recoveries",
            "worker_fallback_attempts",
            "worker_fallback_successes",
            "worker_fallback_failures",
            "direct_fetch_successes",
            "direct_fetch_403s",
            "truncations",
        )
        with self._lock:
            return {
                key: {field: int(values.get(field, 0)) for field in fields}
                for key, values in self._telemetry.items()
            }

    def open_for(self, urls: Sequence[str], *, refresh: bool = False) -> str | None:
        scopes = self._scope_keys(urls)
        for key in self._ordered_keys(scopes):
            if key in self._claimed_probes:
                continue
            if refresh and self._storage is not None:
                marker = self._read_json(self._open_key(key))
                if marker is None:
                    self._known_open.discard(key)
                    continue
                self._known_open.add(key)
                if marker.get("state") == "probing" and marker.get("owner") == self._owner:
                    self._claimed_probes.add(key)
                    continue
                return key
            if key in self._known_open:
                return key
        if self._storage is None:
            with self._lock:
                for key in self._ordered_keys(scopes):
                    if key in self._open_until and key not in self._half_open:
                        return key
        return None

    def record_success(self, urls: Sequence[str]) -> None:
        scopes = self._scope_keys(urls)
        if self._storage is None:
            with self._lock:
                for key in self._ordered_keys(scopes):
                    if key in self._half_open:
                        self._increment(key, "recoveries")
                        self._half_open.discard(key)
                    self._failures[key] = 0
            return
        for domain, tenant_key in self._domain_tenant_pairs(scopes):
            with self._mutation_pool.slots([f"https://{domain}/"]):
                self._delete(self._failure_key(tenant_key))
                for key in (tenant_key, domain):
                    if key not in self._claimed_probes:
                        continue
                    self._delete(self._open_key(key))
                    if key == domain:
                        self._delete(self._domain_trip_key(domain))
                    self._claimed_probes.discard(key)
                    self._known_open.discard(key)
                    self._increment(key, "recoveries")

    def record_rate_limited(self, urls: Sequence[str]) -> str | None:
        scopes = self._scope_keys(urls)
        opened: str | None = None
        if self._storage is None:
            now = time.monotonic()
            with self._lock:
                for domain, tenant_key in self._domain_tenant_pairs(scopes):
                    rule = self._rules[domain]
                    self._increment(tenant_key, "rate_limited")
                    half_open = tenant_key in self._half_open
                    if half_open:
                        self._half_open.discard(tenant_key)
                    count = rule.threshold if half_open else self._failures.get(tenant_key, 0) + 1
                    self._failures[tenant_key] = count
                    if count >= rule.threshold and tenant_key not in self._open_until:
                        self._open_until[tenant_key] = now + rule.cooldown_seconds
                        self._increment(tenant_key, "circuit_trips")
                        opened = tenant_key
            return opened

        for domain, tenant_key in self._domain_tenant_pairs(scopes):
            rule = self._rules[domain]
            self._increment(tenant_key, "rate_limited")
            with self._mutation_pool.slots([f"https://{domain}/"]):
                domain_marker = self._active_marker(domain)
                if domain_marker is not None and domain not in self._claimed_probes:
                    continue
                tenant_marker = self._active_marker(tenant_key)
                domain_canary_failed = domain in self._claimed_probes
                canary_failed = tenant_key in self._claimed_probes or domain_canary_failed
                if tenant_marker is not None and not canary_failed:
                    continue

                state = self._read_json(self._failure_key(tenant_key)) or {}
                updated = _parse_datetime(state.get("updated_at"))
                count = int(state.get("count", 0) or 0)
                if updated is None or datetime.now(UTC) - updated > timedelta(
                    seconds=rule.cooldown_seconds
                ):
                    count = 0
                count = rule.threshold if canary_failed else count + 1
                self._write_json(
                    self._failure_key(tenant_key),
                    {
                        "scope": tenant_key,
                        "count": count,
                        "updated_at": datetime.now(UTC).isoformat(),
                    },
                )
                if count < rule.threshold:
                    continue

                self._open_shared(tenant_key, rule)
                self._increment(tenant_key, "circuit_trips")
                opened = tenant_key
                self._claimed_probes.discard(tenant_key)
                self._claimed_probes.discard(domain)

                trip_state = self._read_json(self._domain_trip_key(domain)) or {}
                trips = {
                    str(key): value
                    for key, value in (trip_state.get("tenant_trips") or {}).items()
                    if _recent(value, rule.cooldown_seconds)
                }
                trips[tenant_key] = datetime.now(UTC).isoformat()
                self._write_json(
                    self._domain_trip_key(domain),
                    {"domain": domain, "tenant_trips": trips},
                )
                if domain_canary_failed or (
                    rule.emergency_tenants > 0 and len(trips) >= rule.emergency_tenants
                ):
                    if domain_canary_failed or self._active_marker(domain) is None:
                        self._open_shared(domain, rule)
                        self._increment(domain, "circuit_trips")
                        opened = domain
                    # The emergency domain marker supersedes tenant markers. Removing them lets
                    # the eventual domain canary run; tenant state rebuilds if one remains bad.
                    for scope in trips:
                        self._delete(self._open_key(scope))
                        self._known_open.discard(scope)
        return opened

    def record_circuit_deferred(self, urls: Sequence[str]) -> str | None:
        key = self.open_for(urls)
        if key is None:
            return None
        self._increment(key, "circuit_deferred")
        return key

    def record_worker_fallback(self, urls: Sequence[str], *, outcome: str) -> None:
        """Count one Granicus Worker fallback attempt and its outcome on the tenant scope.

        Telemetry only — no shared-state mutation. The fallback runs inside the same provider
        lease and circuit admission as the direct attempt, and a Worker *success* deliberately does
        not touch circuit failure state (a direct 403 the Worker recovered must not trip the
        breaker). ``outcome`` is ``"success"`` or ``"failure"``; per scope, the recorded
        ``worker_fallback_attempts`` always equals ``successes`` + ``failures``. These counters are
        the post-activation GH#337 evaluation signal per Granicus tenant (see review/12 §Granicus
        follow-up). Scopes without a configured rule (non-Granicus URLs) are ignored, like the
        other ``record_*`` methods.
        """
        field = "worker_fallback_successes" if outcome == "success" else "worker_fallback_failures"
        for _domain, tenant_key in self._domain_tenant_pairs(self._scope_keys(urls)):
            self._increment(tenant_key, "worker_fallback_attempts")
            self._increment(tenant_key, field)

    def record_direct_fetch(self, urls: Sequence[str], *, outcome: str) -> None:
        """Count a direct Granicus fetch outcome on each configured tenant scope."""
        if outcome not in {"success", "403"}:
            raise ValueError(f"unsupported direct fetch outcome: {outcome}")
        field = "direct_fetch_successes" if outcome == "success" else "direct_fetch_403s"
        for _domain, tenant_key in self._domain_tenant_pairs(self._scope_keys(urls)):
            self._increment(tenant_key, field)

    def record_truncation(self, urls: Sequence[str]) -> None:
        """Count a completed-but-truncated Granicus media result for H16 acceptance."""
        for _domain, tenant_key in self._domain_tenant_pairs(self._scope_keys(urls)):
            self._increment(tenant_key, "truncations")

    def wait_for_recovery_probe(
        self,
        *,
        keys: Sequence[str] | None = None,
        stop: Callable[[], bool] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        poll_seconds: float = 1.0,
    ) -> str | None:
        wanted = list(dict.fromkeys(keys or self._known_open or self._open_until))
        if not wanted:
            return None
        if self._storage is None:
            return self._wait_local(wanted, stop=stop, sleep=sleep, poll_seconds=poll_seconds)

        observed = set(wanted)
        while True:
            if stop is not None and stop():
                return None
            earliest: tuple[datetime, str] | None = None
            for key in wanted:
                domain = key.split("/", 1)[0]
                with self._mutation_pool.slots([f"https://{domain}/"]):
                    marker_key = key
                    marker = self._read_json(self._open_key(key))
                    if key != domain:
                        domain_marker = self._read_json(self._open_key(domain))
                        if domain_marker is not None:
                            # A domain emergency supersedes a tenant circuit. Claim its canary using
                            # this tenant's parked item, but return the tenant key so the queue can
                            # select the matching work bucket.
                            marker_key = domain
                            marker = domain_marker
                    if marker is None:
                        if key in observed:
                            self._known_open.discard(key)
                            return key  # another shard's canary recovered this scope
                        continue
                    observed.add(key)
                    expires = _parse_datetime(marker.get("expires_at"))
                    state = str(marker.get("state") or "open")
                    if expires is None:
                        continue
                    now = datetime.now(UTC)
                    if expires <= now:
                        if state == "probing":
                            # Reclaim an abandoned canary after its probe TTL.
                            state = "open"
                        if state == "open":
                            self._write_json(
                                self._open_key(marker_key),
                                {
                                    **marker,
                                    "state": "probing",
                                    "owner": self._owner,
                                    "expires_at": (
                                        now
                                        + timedelta(seconds=self._rules[domain].probe_ttl_seconds)
                                    ).isoformat(),
                                },
                            )
                            self._claimed_probes.add(marker_key)
                            self._known_open.discard(marker_key)
                            self._increment(marker_key, "recovery_probes")
                            return key
                    if earliest is None or expires < earliest[0]:
                        earliest = (expires, key)
            if earliest is None:
                return None
            remaining = max(0.01, (earliest[0] - datetime.now(UTC)).total_seconds())
            sleep(min(poll_seconds, remaining))

    def recovery_succeeded(self, key: str) -> bool:
        if self._storage is None:
            with self._lock:
                return key not in self._half_open and key not in self._open_until
        domain = key.split("/", 1)[0]
        effective = (domain, key) if key != domain else (domain,)
        if any(scope in self._claimed_probes for scope in effective):
            return False
        return all(self._read_json(self._open_key(scope)) is None for scope in effective)

    def scope_keys_for(self, urls: Sequence[str]) -> list[str]:
        """Public stable scope vocabulary used by stage/queue telemetry."""
        return self._scope_keys(urls)

    def _wait_local(self, wanted, *, stop, sleep, poll_seconds):
        while True:
            if stop is not None and stop():
                return None
            now = time.monotonic()
            with self._lock:
                pending = [
                    (until, key)
                    for key, until in self._open_until.items()
                    if key in wanted and key not in self._half_open
                ]
                if not pending:
                    return None
                until, key = min(pending)
                remaining = until - now
                if remaining <= 0:
                    self._open_until.pop(key, None)
                    self._failures[key] = 0
                    self._half_open.add(key)
                    self._increment(key, "recovery_probes")
                    return key
            sleep(max(0.01, min(poll_seconds, remaining)))

    def _scope_keys(self, urls: Sequence[str]) -> list[str]:
        scopes: set[str] = set()
        for url in urls:
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
            domain = self._domain_for(host)
            if domain is None:
                continue
            tenant = _tenant_for(parts, domain)
            scopes.add(f"{domain}/tenant:{tenant}" if tenant else domain)
        return sorted(scopes)

    def _domain_tenant_pairs(self, scopes: Sequence[str]) -> list[tuple[str, str]]:
        return [(key.split("/", 1)[0], key) for key in scopes]

    def _ordered_keys(self, scopes: Sequence[str]) -> list[str]:
        domains = sorted({key.split("/", 1)[0] for key in scopes})
        return [*domains, *scopes]

    def _domain_for(self, host: str) -> str | None:
        best: str | None = None
        for domain in self._rules:
            if host == domain or host.endswith("." + domain):
                if best is None or len(domain) > len(best):
                    best = domain
        return best

    def _active_marker(self, key: str) -> dict | None:
        marker = self._read_json(self._open_key(key))
        if marker is None:
            return None
        self._known_open.add(key)
        return marker

    def _open_shared(self, key: str, rule: RateLimitCircuitRule) -> None:
        now = datetime.now(UTC)
        self._write_json(
            self._open_key(key),
            {
                "scope": key,
                "state": "open",
                "owner": self._owner,
                "opened_at": now.isoformat(),
                "expires_at": (now + timedelta(seconds=rule.cooldown_seconds)).isoformat(),
            },
        )
        self._known_open.add(key)

    def _increment(self, key: str, field: str) -> None:
        with self._lock:
            values = self._telemetry.setdefault(key, {})
            values[field] = int(values.get(field, 0)) + 1

    def _open_key(self, key: str) -> str:
        return f"{self._prefix}/open/{quote(key, safe='')}.json"

    def _failure_key(self, key: str) -> str:
        return f"{self._prefix}/failures/{quote(key, safe='')}.json"

    def _domain_trip_key(self, domain: str) -> str:
        return f"{self._prefix}/domain-trips/{quote(domain, safe='')}.json"

    def _read_json(self, key: str) -> dict | None:
        storage = self._storage
        if storage is None or not storage.exists(key):
            return None
        try:
            with tempfile.TemporaryDirectory(prefix="citypods_circuit_read_") as tmp:
                path = Path(tmp) / "state.json"
                if not storage.get_file(key, path):
                    return None
                value = json.loads(path.read_text(encoding="utf-8"))
                return value if isinstance(value, dict) else None
        except Exception:  # noqa: BLE001 - shared state failure falls back to local safety
            return None

    def _write_json(self, key: str, value: Mapping[str, object]) -> None:
        storage = self._storage
        if storage is None:
            return
        with tempfile.TemporaryDirectory(prefix="citypods_circuit_write_") as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
            storage.put_file(key, path, "application/json; charset=utf-8")

    def _delete(self, key: str) -> None:
        if self._storage is not None:
            self._storage.delete(key)


def _tenant_for(parts, domain: str) -> str | None:
    host = (parts.hostname or "").lower()
    if domain == "granicus.com" and host == "archive-video.granicus.com":
        path_parts = [unquote(part) for part in parts.path.split("/") if part]
        # Real Granicus archive URLs are /<tenant>/<object>.mp4. A bare one-component test or
        # legacy path has no tenant identity, so retain domain-level behavior.
        return path_parts[0].lower() if len(path_parts) >= 2 else None
    suffix = "." + domain
    if host.endswith(suffix):
        subdomain = host[: -len(suffix)]
        if subdomain:
            return subdomain
    return host or None


def _parse_circuit_rule(raw: object) -> RateLimitCircuitRule | None:
    if isinstance(raw, Mapping):
        try:
            threshold = int(raw.get("threshold", 0))
        except (TypeError, ValueError):
            return None
        if threshold <= 0:
            return None
        return RateLimitCircuitRule(
            threshold=threshold,
            cooldown_seconds=float(raw.get("cooldown_seconds", 1800.0)),
            emergency_tenants=int(raw.get("emergency_tenants", 2)),
            probe_ttl_seconds=float(raw.get("probe_ttl_seconds", 900.0)),
        )
    try:
        threshold = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return RateLimitCircuitRule(threshold=threshold) if threshold > 0 else None


def _parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _recent(value: object, window_seconds: float) -> bool:
    parsed = _parse_datetime(value)
    return parsed is not None and datetime.now(UTC) - parsed <= timedelta(seconds=window_seconds)


def _emit(log: Callable[[str], None], message: str) -> None:
    try:
        log(message, flush=True)  # type: ignore[call-arg]
    except TypeError:
        log(message)
