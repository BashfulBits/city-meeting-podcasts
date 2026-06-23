"""Per-tenant Granicus transport telemetry for the H16 acceptance report.

Records, per Granicus tenant, the outcome of each media fetch: direct success vs direct HTTP 403,
Cloudflare-Worker fallback attempts and their success/failure, and completed-but-truncated results.
``citypods.h16_report`` folds these counters into the audio run event so the ``transport`` criterion
can prove the direct-first + single-Worker-attempt route is healthy.

This module previously also coordinated a storage-backed rate-limit *circuit breaker* (trip / park /
canary recovery). That machinery was removed in GH#353: across audio runs #51–#56 it never tripped —
the Actions-runner 403s were shared-egress IP reputation (handled by the Worker), not request-shape
or concurrency throttling, and the provider-lease ceiling plus per-episode materialize backoff
already bound aggregate load. Only the lightweight, no-shared-state transport accounting remains.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from urllib.parse import unquote, urlsplit

_TELEMETRY_FIELDS = (
    "worker_fallback_attempts",
    "worker_fallback_successes",
    "worker_fallback_failures",
    "direct_fetch_successes",
    "direct_fetch_403s",
    "truncations",
)


class ProviderTransportTelemetry:
    """Per-tenant direct/Worker/truncation counters for configured provider domains."""

    def __init__(self, domains: Sequence[str] | Mapping[str, object] | None = None) -> None:
        # Accept either a plain list of domains or the legacy mapping shape (domain -> config); only
        # the domain keys matter now that the circuit thresholds are gone.
        names = domains.keys() if isinstance(domains, Mapping) else (domains or ())
        self._domains: set[str] = {str(name).strip().lower() for name in names if str(name).strip()}
        self._telemetry: dict[str, dict[str, int]] = {}
        self._lock = threading.Lock()

    def has_domains(self) -> bool:
        return bool(self._domains)

    def telemetry(self) -> dict[str, dict[str, int]]:
        with self._lock:
            return {
                key: {field: int(values.get(field, 0)) for field in _TELEMETRY_FIELDS}
                for key, values in self._telemetry.items()
            }

    def record_worker_fallback(self, urls: Sequence[str], *, outcome: str) -> None:
        """Count one Granicus Worker fallback attempt and its outcome on the tenant scope.

        ``outcome`` is ``"success"`` or ``"failure"``; per scope, ``worker_fallback_attempts``
        always equals ``successes`` + ``failures`` (the attempt and outcome counters are bumped
        under one lock so a concurrent ``telemetry()`` read never sees them out of step). Scopes
        without a configured domain (non-Granicus URLs) are ignored, like the other ``record_*``
        methods.
        """
        if outcome not in {"success", "failure"}:
            raise ValueError(f"unsupported worker fallback outcome: {outcome}")
        field = "worker_fallback_successes" if outcome == "success" else "worker_fallback_failures"
        for _domain, tenant_key in self._domain_tenant_pairs(self._scope_keys(urls)):
            self._increment(tenant_key, "worker_fallback_attempts", field)

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

    def _domain_for(self, host: str) -> str | None:
        best: str | None = None
        for domain in self._domains:
            if host == domain or host.endswith("." + domain):
                if best is None or len(domain) > len(best):
                    best = domain
        return best

    def _increment(self, key: str, *fields: str) -> None:
        with self._lock:
            values = self._telemetry.setdefault(key, {})
            for field in fields:
                values[field] = int(values.get(field, 0)) + 1


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
