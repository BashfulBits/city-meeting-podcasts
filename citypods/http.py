"""Shared HTTP session: polite User-Agent, transient-error retries, and an SSRF/abuse guard.

Every session returned by :func:`make_session` validates each outbound request — and each
redirect hop — against :func:`citypods.security.validate_source_url` (https-only, resolve the
host and reject private/loopback/link-local IPs), caps redirects, refuses oversized responses,
and retries transient failures (429/5xx, connection resets) with exponential backoff. See
``citypods/security.py`` for the trust-boundary rationale (audit #S1).

It also enforces a **per-host concurrency cap** (issue #39 / per-provider rate limiting) via the
process-global :data:`HOST_LIMITER`: every adapter ``send`` acquires the target host's slot, and
the ffmpeg fetch paths (``citypods/media.py``) acquire the *same* limiter, so a sharded burst of
workers never opens more than the configured number of simultaneous connections to one provider
tenant (the cause of the Granicus/Swagit ``403`` + truncated-fetch storm under H6b sharding).
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from citypods.security import (
    MAX_REDIRECTS,
    MAX_RESPONSE_BYTES,
    SecurityError,
    validate_source_url,
)

# A ``Mozilla/5.0 (compatible; …)`` prefix is load-bearing, not vanity: the Granicus media CDN
# (archive-video.granicus.com) returns 403 to a non-browser User-Agent — our old bare
# ``citypods/0.1`` UA (and ffmpeg's default ``Lavf/…``) were blocked, which is why Granicus audio
# never downloaded. The ``(compatible; citypods/…; +url)`` form passes the CDN filter while staying
# honest about who we are. ``citypods/media.py`` passes this same string to ffmpeg/ffprobe via
# ``-user_agent``. The ``tests/live`` media-fetch contract check guards against a regression.
USER_AGENT = (
    "Mozilla/5.0 (compatible; citypods/0.1; +https://github.com/BashfulBits/city-meeting-podcasts)"
)
DEFAULT_TIMEOUT = 30


# Retry transient failures (connection resets, 429s, 5xx) with exponential backoff. At ~80+
# feeds across a handful of shared provider tenants, an occasional blip shouldn't mark a city
# `error` for the whole run (which can file a false-positive feed-health issue). GET/HEAD only;
# we never retry non-idempotent verbs.
class _ClampedRetry(Retry):
    """Honor ``Retry-After`` but **cap** it. A provider returning ``Retry-After: 3600`` would
    otherwise hang the whole build for an hour inside urllib3's retry sleep (observed from a
    Granicus 429). Clamping keeps politeness for short, legitimate delays without letting one
    hostile/misconfigured header stall the run; a request that still fails after the bounded
    retries surfaces as a ``ProviderError`` so the next scheduled run retries cleanly."""

    # Longest we'll wait on a single Retry-After before falling back to plain backoff.
    MAX_RETRY_AFTER_SECONDS = 120

    def get_retry_after(self, response):
        retry_after = super().get_retry_after(response)
        if retry_after is None:
            return None
        return min(retry_after, self.MAX_RETRY_AFTER_SECONDS)


# ``403`` is in the forcelist on purpose (issue #39): media bytes go to ffmpeg, never through this
# session, so every ``403`` a ``requests`` call sees here is a provider *rate-limit* signal — not an
# auth failure (Granicus ``DownloadFile.php`` throttles a concurrent caller with 403, not 429).
# Retrying it with backoff generalizes the bespoke Granicus retry that used to live in
# ``providers/granicus.py``.
_RETRY = _ClampedRetry(
    total=3,
    backoff_factor=0.5,  # 0.5s, 1s, 2s
    status_forcelist=(403, 429, 500, 502, 503, 504),
    allowed_methods=frozenset({"GET", "HEAD"}),
    raise_on_status=False,
    respect_retry_after_header=True,  # honored, but clamped to MAX_RETRY_AFTER_SECONDS above
)


class HostRateLimiter:
    """Process-global per-registrable-domain concurrency cap (issue #39).

    Sharded ``audio.yml``/``asr.yml`` (H6b) run several workers per job, each concentrated on a few
    sources that share one provider CDN — which throttles the burst (Granicus ``403``; Swagit short
    responses ffmpeg copies and "succeeds" on). This bounds the number of *simultaneous* in-flight
    requests to each provider host across all worker threads in the process, so the burst stays
    polite. Per-process; cross-shard coordination needs distributed leases (H14), out of scope.

    Keyed by **registrable domain** (e.g. ``granicus.com``), not a provider short-name, because
    Swagit is Granicus-owned and serves media from ``*.granicus.com`` — keying by the host the
    tenant actually sees makes the shared-CDN case correct. Configured once at run start via
    :meth:`configure`; both :class:`GuardedHTTPAdapter` and the ffmpeg fetch paths in
    ``citypods/media.py`` acquire :meth:`slot` so a single cap covers requests *and* ffmpeg.
    """

    def __init__(self) -> None:
        self._limits: dict[str, int] = {}
        self._sems: dict[str, threading.BoundedSemaphore] = {}
        self._guard = threading.Lock()

    def configure(self, limits: Mapping[str, int] | None) -> None:
        """Set the per-domain caps (``{"granicus.com": 2, ...}``); a non-positive cap is ignored.

        Replaces any prior config and drops cached semaphores so they're rebuilt under the new caps.
        Call once before workers start (resettable for tests)."""
        cleaned: dict[str, int] = {}
        for key, value in (limits or {}).items():
            try:
                cap = int(value)
            except (TypeError, ValueError):
                continue
            if cap > 0:
                cleaned[str(key).strip().lower()] = cap
        with self._guard:
            self._limits = cleaned
            self._sems = {}

    def _key_for(self, host: str) -> str | None:
        """The configured domain governing ``host`` (exact or dotted-suffix; longest wins)."""
        host = (host or "").lower()
        best: str | None = None
        for key in self._limits:  # _limits is rebound atomically by configure(), safe to iterate
            if host == key or host.endswith("." + key):
                if best is None or len(key) > len(best):
                    best = key
        return best

    def _sem_for_key(self, key: str) -> threading.BoundedSemaphore:
        """The (lazily created) semaphore for an already-resolved configured domain ``key``."""
        with self._guard:
            sem = self._sems.get(key)
            if sem is None:
                sem = threading.BoundedSemaphore(self._limits[key])
                self._sems[key] = sem
            return sem

    def slot(self, url: str) -> _Slots:
        """Hold the cap slot for ``url``'s host. No-op when the host has no configured cap."""
        return self.slots([url])

    def slots(self, urls: Iterable[str]) -> _Slots:
        """Hold the cap slots for every distinct configured host among ``urls`` (ffmpeg may read
        several remote sources in one invocation). Domains are deduped and acquired in a fixed
        (sorted) order so two concurrent multi-source renders can't deadlock. Local-file inputs and
        unconfigured hosts contribute nothing."""
        keys: set[str] = set()
        for url in urls:
            key = self._key_for((urlsplit(url).hostname or "").lower())
            if key is not None:
                keys.add(key)
        return _Slots(self, sorted(keys))


class _Slots:
    """Context manager that acquires/releases a fixed, sorted set of host semaphores together."""

    def __init__(self, limiter: HostRateLimiter, keys: list[str]) -> None:
        self._limiter = limiter
        self._keys = keys

    def __enter__(self) -> None:
        self._acquired: list[threading.BoundedSemaphore] = []
        for key in self._keys:
            sem = self._limiter._sem_for_key(key)
            sem.acquire()
            self._acquired.append(sem)

    def __exit__(self, *_exc: object) -> None:
        for sem in reversed(self._acquired):
            sem.release()


# One limiter for the whole process; configured from site_config (``provider_rate_limits``) at the
# start of ``citypods.run.build`` before any fetching, and shared with the ffmpeg paths in media.py.
HOST_LIMITER = HostRateLimiter()


class GuardedHTTPAdapter(HTTPAdapter):
    """An adapter that validates the target of every request it sends (and retries transient
    failures via the shared ``_RETRY`` policy).

    requests routes the initial request *and* each redirect through the mounted adapter, so
    checking here blocks redirect-to-internal as well as the first hop. The host allowlist is
    enforced separately at config load (it needs the provider/city); here we enforce the
    universal invariants: https-only and no private/loopback/link-local destination.
    """

    def send(self, request, **kwargs):
        validate_source_url(request.url, resolve=True)
        # Per-host concurrency cap (#39): hold the provider's slot only for the network round-trip.
        with HOST_LIMITER.slot(request.url):
            response = super().send(request, **kwargs)
        length = response.headers.get("Content-Length")
        if length is not None and length.isdigit() and int(length) > MAX_RESPONSE_BYTES:
            response.close()
            raise SecurityError(
                f"response from {request.url} is {length} bytes, exceeds cap {MAX_RESPONSE_BYTES}"
            )
        return response


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    session.max_redirects = MAX_REDIRECTS
    adapter = GuardedHTTPAdapter(max_retries=_RETRY)
    session.mount("https://", adapter)
    session.mount("http://", adapter)  # guarded too: rejected at send (https-only)
    return session
