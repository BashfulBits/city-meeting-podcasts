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

import re
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
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

# The Granicus media CDN (archive-video.granicus.com) 403s non-browser User-Agents. The original
# ``citypods/0.1`` UA and ffmpeg's default ``Lavf/…`` were both blocked (#293). A first fix used
# ``Mozilla/5.0 (compatible; citypods/0.1; …)``, which passed briefly but Granicus CDN later also
# blocked the ``(compatible; citypods/…)`` bot-disclosure form. A plain Chrome-on-Linux UA is the
# only reliable option; citypods identity lives in the +URL comment that browsers include anyway.
# ``citypods/media.py`` passes this same string to ffmpeg/ffprobe via ``-user_agent``.
# The ``tests/live`` media-fetch contract check guards against a regression.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
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


class StopRequested(Exception):
    """Raised by a coordination wait (host rate limit, distributed lease, source cache) when the
    caller's ``stop`` predicate fires before the wait could acquire its slot.

    These waits are otherwise unbounded — or bounded only by *another* thread's own work timeout —
    so without this a worker idle past the run's wall-clock budget still blocks on a queue position
    instead of yielding immediately (audio workflow review, 2026-06)."""


class HostRateLimiter:
    """Process-global per-registrable-domain concurrency cap (issue #39).

    Sharded ``audio.yml``/``asr.yml`` (H6b) run several workers per job, each concentrated on a few
    sources that share one provider CDN — which throttles the burst (Granicus ``403``; Swagit short
    responses ffmpeg copies and "succeeds" on). This bounds the number of *simultaneous* in-flight
    requests to each provider host across all worker threads in the process, so the burst stays
    polite inside the process. Cross-shard media coordination is handled separately by
    ``provider_distributed_leases`` in the ffmpeg/ffprobe media paths.

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

    def slot(
        self, url: str, *, stop: Callable[[], bool] | None = None, poll_seconds: float = 1.0
    ) -> _Slots:
        """Hold the cap slot for ``url``'s host. No-op when the host has no configured cap.

        ``stop``, if given, is polled every ``poll_seconds`` while waiting; if it fires before the
        slot is acquired, raises :class:`StopRequested` instead of blocking further."""
        return self.slots([url], stop=stop, poll_seconds=poll_seconds)

    def slots(
        self,
        urls: Iterable[str],
        *,
        stop: Callable[[], bool] | None = None,
        poll_seconds: float = 1.0,
    ) -> _Slots:
        """Hold the cap slots for every distinct configured host among ``urls`` (ffmpeg may read
        several remote sources in one invocation). Domains are deduped and acquired in a fixed
        (sorted) order so two concurrent multi-source renders can't deadlock. Local-file inputs and
        unconfigured hosts contribute nothing.

        ``stop``/``poll_seconds``: see :meth:`slot`."""
        keys: set[str] = set()
        for url in urls:
            key = self._key_for((urlsplit(url).hostname or "").lower())
            if key is not None:
                keys.add(key)
        return _Slots(self, sorted(keys), stop=stop, poll_seconds=poll_seconds)


class _Slots:
    """Context manager that acquires/releases a fixed, sorted set of host semaphores together."""

    def __init__(
        self,
        limiter: HostRateLimiter,
        keys: list[str],
        *,
        stop: Callable[[], bool] | None = None,
        poll_seconds: float = 1.0,
    ) -> None:
        self._limiter = limiter
        self._keys = keys
        self._stop = stop
        self._poll_seconds = poll_seconds

    def __enter__(self) -> None:
        self._acquired: list[threading.BoundedSemaphore] = []
        for key in self._keys:
            sem = self._limiter._sem_for_key(key)
            if self._stop is None:
                sem.acquire()
            else:
                while not sem.acquire(timeout=self._poll_seconds):
                    if self._stop():
                        for held in reversed(self._acquired):
                            held.release()
                        self._acquired = []
                        raise StopRequested(f"host rate limit wait for {key!r} stopped")
            self._acquired.append(sem)

    def __exit__(self, *_exc: object) -> None:
        for sem in reversed(self._acquired):
            sem.release()


# One limiter for the whole process; configured from site_config (``provider_rate_limits``) at the
# start of ``citypods.run.build`` before any fetching, and shared with the ffmpeg paths in media.py.
HOST_LIMITER = HostRateLimiter()


def _redact_url(url: str) -> str:
    """Strip query/fragment so signed params (S3/B2 auth, tokens) never land in an error message."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


class _CappedRaw:
    """Proxies a urllib3 raw response so bytes are counted as they stream through.

    Hooking ``.stream``/``.read`` lets the cap ride along the normal, fully-supported
    ``requests.Response.content``/``.iter_content()`` reading path. This deliberately avoids
    writing ``Response._content``/``_content_consumed`` directly — those are private
    implementation details requests doesn't guarantee across releases.
    """

    def __init__(self, raw, url: str) -> None:
        self._raw = raw
        self._url = url
        self._total = 0

    def _account(self, n: int) -> None:
        self._total += n
        if self._total > MAX_RESPONSE_BYTES:
            raise SecurityError(
                f"response from {_redact_url(self._url)} exceeds cap {MAX_RESPONSE_BYTES} bytes"
            )

    def stream(self, amt=2**16, **kwargs):
        for chunk in self._raw.stream(amt, **kwargs):
            self._account(len(chunk))
            yield chunk

    def read(self, amt=None, *args, **kwargs):
        data = self._raw.read(amt, *args, **kwargs)
        self._account(len(data))
        return data

    def __getattr__(self, name):
        return getattr(self._raw, name)


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
        stream_requested = kwargs.get("stream", False)
        kwargs["stream"] = True
        # Per-host concurrency cap (#39): hold the provider's slot only for the network round-trip.
        with HOST_LIMITER.slot(request.url):
            response = super().send(request, **kwargs)
        length = response.headers.get("Content-Length")
        # Only fast-fail on the declared size when a body might actually get auto-buffered into
        # ``.content`` below — the case this cap exists to protect (see class docstring). Two
        # exemptions:
        #  * HEAD never has a body, regardless of the ``stream`` kwarg — the header only
        #    describes what a GET *would* return, so there's nothing here to buffer.
        #  * A caller that explicitly asked to stream (e.g. a Range preflight that reads no body
        #    at all — ``citypods.http.preflight_media_size``) takes responsibility for its own
        #    bytes; the incremental ``_CappedRaw`` wrapper below still bounds it if it *does* read.
        # Before this, a plain HEAD on a legitimately large media URL (which has no body to buffer)
        # tripped this the same as an oversized buffered GET.
        if (
            getattr(request, "method", None) != "HEAD"
            and not stream_requested
            and length is not None
            and length.isdigit()
            and int(length) > MAX_RESPONSE_BYTES
        ):
            response.close()
            raise SecurityError(
                f"response from {_redact_url(request.url)} is {length} bytes, "
                f"exceeds cap {MAX_RESPONSE_BYTES}"
            )
        raw = getattr(response, "raw", None)
        if raw is None:
            return response
        response.raw = _CappedRaw(raw, request.url)
        if not stream_requested:
            # Caller asked for a buffered response (the default): force the (now capped) read
            # through the public `.content` property so an oversized or missing/incorrect
            # Content-Length response can't be fully buffered into memory unchecked, and so
            # the cap is enforced at the same point a RequestException would otherwise surface.
            try:
                _ = response.content
            finally:
                response.close()
        return response


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    session.max_redirects = MAX_REDIRECTS
    adapter = GuardedHTTPAdapter(max_retries=_RETRY)
    session.mount("https://", adapter)
    session.mount("http://", adapter)  # guarded too: rejected at send (https-only)
    return session


_CONTENT_RANGE_TOTAL_RE = re.compile(r"bytes\s+\d+-\d+/(\d+)")


def _advertised_total_bytes(headers: Mapping[str, str]) -> int | None:
    """Total size a response's headers claim, from ``Content-Range`` (a ranged request's real
    total, e.g. ``bytes 0-0/123456``) or else ``Content-Length`` (a HEAD, or a GET the server
    chose not to range). Returns ``None`` when neither is present/parseable — some providers omit
    both or use chunked transfer, which this preflight cannot see through."""
    content_range = headers.get("Content-Range")
    if content_range:
        m = _CONTENT_RANGE_TOTAL_RE.search(content_range)
        if m:
            return int(m.group(1))
    length = headers.get("Content-Length")
    if length is not None and length.isdigit():
        return int(length)
    return None


@dataclass(frozen=True)
class MediaSizePreflight:
    """Result of :func:`preflight_media_size`.

    ``status`` is one of ``"known_ok"``, ``"known_too_large"``, or ``"unknown"`` — three outcomes
    on purpose (issue #497): a source can honestly disclose a safe size, honestly disclose an
    oversized one, or disclose nothing verifiable (no HEAD support, range ignored, chunked
    transfer, or a server that simply lies). Only the middle case can be rejected before ffmpeg
    ever starts; ``unknown`` is a policy choice for the caller, not a measurement.
    """

    status: str
    content_length: int | None = None


def preflight_media_size(
    url: str, max_bytes: int, *, session: requests.Session | None = None
) -> MediaSizePreflight:
    """Best-effort check of a remote media URL's advertised size before handing it to ffmpeg.

    ffmpeg reads media URLs directly via libavformat — never through this session — so nothing
    here can enforce a byte cap on the eventual fetch; it only refuses a source that *honestly
    discloses* an oversized total before any ffmpeg process starts (issue #497 Option A). Tries
    ``HEAD`` first (cheap, no body); falls back to a ``Range: bytes=0-0`` ``GET`` for the CDNs
    that reject/ignore HEAD. Both go through :func:`make_session`'s SSRF/host-allowlist gate, so a
    bad URL is rejected here exactly as it would be for any other guarded fetch.
    """
    sess = session or make_session()
    try:
        resp = sess.head(url, timeout=DEFAULT_TIMEOUT, allow_redirects=True)
        total = _advertised_total_bytes(resp.headers)
        if total is None:
            resp = sess.get(
                url,
                timeout=DEFAULT_TIMEOUT,
                headers={"Range": "bytes=0-0"},
                stream=True,
            )
            try:
                total = _advertised_total_bytes(resp.headers)
            finally:
                resp.close()
    except (requests.RequestException, SecurityError):
        return MediaSizePreflight(status="unknown")
    if total is None:
        return MediaSizePreflight(status="unknown")
    if total > max_bytes:
        return MediaSizePreflight(status="known_too_large", content_length=total)
    return MediaSizePreflight(status="known_ok", content_length=total)
