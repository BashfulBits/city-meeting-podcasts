"""Audio materialization: source media -> M4A -> object storage.

Used for two cases:
  - CivicPlus/CivicMedia episodes (``media_kind == "hls"``): the only way to get a
    playable enclosure, since the source is tokenized/expiring HLS.
  - Granicus episodes when the city sets ``extract_audio: true``.

ffmpeg invocation is injectable (``FfmpegRunner``) so the pipeline is unit-testable
offline with a fake. A per-run ``budget`` caps how many *new* episodes are processed,
so a large first-time backfill is spread over successive scheduled runs rather than
blowing the Actions 6-hour job limit.

Timeline-aware rendering (INFRA-3, #144)
-----------------------------------------
``FfmpegRunner.extract_audio`` now accepts a ``Timeline | None`` and a ``sources_by_id``
dict (``source_id -> resolved_url``) instead of a bare ``source_url``.  The identity
path (``timeline is None`` or ``timeline_digest == ""``) uses the same copy/re-encode
args as before.  Non-identity timelines are rendered via an ffmpeg ``filter_complex``
that assembles ``atrim``/``concat``/insert segments and optionally applies ``loudnorm``.
"""

from __future__ import annotations

import collections
import hashlib
import json
import re
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from citypods.http import HOST_LIMITER, USER_AGENT
from citypods.models import City, Episode
from citypods.provider_leases import DISTRIBUTED_PROVIDER_LEASES
from citypods.providers.base import ProviderError
from citypods.records import audio_object_key, audio_spec_hash, source_key
from citypods.resources import (
    MemoryReservation,
    NativeWorkGate,
    ResourceSnapshot,
    current_snapshot,
    format_bytes,
)
from citypods.storage.base import StorageBackend
from citypods.timeline import Segment, Timeline, timeline_digest

CONTENT_TYPE = "audio/mp4"

# Exponential backoff for repeatedly-failing materializations (issue #120): a source whose audio
# won't resolve (e.g. a Swagit meeting with no usable media) must stop being re-tried every run,
# or it churns the run's time + budget forever. Wait ``BACKOFF_BASE * 2**(attempts-1)``, capped at
# ``BACKOFF_MAX``, before re-attempting. A successful host resets the counter.
BACKOFF_BASE = timedelta(days=1)
BACKOFF_MAX = timedelta(days=30)

# Truncation guard (issue #39). A throttled/rate-limited provider can return a short response that
# ffmpeg ``-c:a copy`` copies and exits 0 on — hosting a ~5-second clip of a multi-hour meeting that
# passes a naive "file size > 0" check (the H6b sharding regression: every sharded fetch "succeeded"
# in ~5s, zero real audio). When the source's duration is declared (Granicus itunes:duration,
# CivicClerk durationHrs/Min) we reject an encode shorter than this fraction of it — silence-trim +
# loudnorm never remove that much, so real meetings pass while a truncated stub is failed into the
# #120 backoff and retried (with the per-host cap now keeping the next fetch from being throttled).
# The ratio check needs a declared duration (Granicus itunes:duration, CivicClerk durationHrs/Min);
# the absolute byte floor below catches an empty/near-empty encode for ANY provider — including
# Swagit, which declares no duration (the first sharded cron run hosted 27 such 258-byte stubs).
_TRUNCATION_MIN_RATIO = 0.5
# No real meeting encodes to fewer than this many bytes (a 1-minute 96kbps mono AAC is ~700 KB; the
# observed empties were 258 B). A floor this low can only ever reject a broken/empty container.
_MIN_PLAUSIBLE_AUDIO_BYTES = 4096

# ffmpeg/ffprobe read the (remote) source directly, so a server that accepts the connection then
# stalls would block a worker forever — and the shared ``stop()`` can't preempt a thread parked in
# ``subprocess.run``, so one stalled source pins the whole build until GitHub's 6h job cap. Bound it
# two ways: ``-rw_timeout`` lets ffmpeg abort a stalled read itself (clean non-zero exit), and the
# subprocess ``timeout=`` is the hard backstop that guarantees the worker returns. Both surface as a
# materialization failure → the #120 backoff, so a chronically-stalling source stops being retried.
_STALL_TIMEOUT_US = 120_000_000  # ffmpeg aborts after 120s with zero I/O progress (microseconds)
_PROBE_TIMEOUT_S = 120.0  # ffprobe reads only stream headers; 2 min is generous
# How often the guard wakes to poll the child + sample memory. Kept short so reported ``seconds``
# reflects the child's *real* runtime: at 5s a fast/throttled fetch that exits in 0.3s was logged as
# "done seconds=5.0", which masked the H6b truncation regression (every fetch looked like ~5s). The
# per-iteration /proc reads are cheap (a couple per wake), so a fine cadence costs nothing.
_FFMPEG_GUARD_POLL_SECONDS = 0.5


def _ua_args(url: str) -> list[str]:
    """ffmpeg/ffprobe ``-user_agent`` flag — but ONLY for remote inputs. It's an HTTP(S) protocol
    option, so ffmpeg errors ``Option user_agent not found`` if it's passed for a local-file input
    — and the source-cache hands the encode pass a *local* copy (`/tmp/citypods_src_*`). So gate it
    on the scheme: remote sources need the browser-compatible UA (Granicus CDN blocks others, see
    http.USER_AGENT); local files must not get it."""
    return ["-user_agent", USER_AGENT] if url.startswith(("http://", "https://")) else []


def _log_ffmpeg_event(log: Callable[[str], None] | None, message: str) -> None:
    if log is None:
        return
    try:
        log(message, flush=True)  # type: ignore[call-arg]
    except TypeError:
        log(message)


def _format_optional_bytes(value: int | None) -> str:
    return "unknown" if value is None else format_bytes(value)


def _stderr_tail(stderr: bytes | str | None, *, limit: int = 1200) -> str:
    if stderr is None:
        return ""
    text = stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else str(stderr)
    text = text.strip()
    if not text:
        return ""
    text = text[-limit:]
    return " ".join(text.split())


def _process_rss_bytes(pid: int) -> int | None:
    """Return resident memory for a child process on Linux, or ``None`` when unavailable."""
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    return int(parts[1]) * 1024
                return None
    except OSError:
        return None
    return None


class FfmpegMemoryLimitExceeded(OSError):
    """Raised when an ffmpeg child is terminated to preserve runner memory."""

    code = "memory"

    def __init__(self, *, phase: str, cmd: list[str], floor: int, available: int):
        self.phase = phase
        self.cmd = cmd
        self.floor = floor
        self.available = available
        super().__init__(
            f"ffmpeg {phase} stopped: mem_avail {format_bytes(available)} below "
            f"{format_bytes(floor)}"
        )


class TruncatedAudioError(ProviderError):
    """Encoded audio is implausibly short vs. the feed-declared duration (a throttled/truncated
    source fetch, issue #39). A ``ProviderError`` so the encode loop's existing handler records the
    failed attempt and backs the episode off (#120) instead of hosting the stub."""

    code = "truncated"


class RateLimitedMediaFetchError(ProviderError):
    """ffmpeg/ffprobe saw an HTTP throttling response while reading provider media."""

    code = "rate_limited"


@dataclass(frozen=True)
class RateLimitCircuitRule:
    threshold: int = 3
    cooldown_seconds: float = 1800.0


class MediaRateLimitCircuitBreaker:
    """Run-local provider circuit breaker for repeated media throttling failures."""

    def __init__(self, config: Mapping[str, object] | None = None) -> None:
        self._rules: dict[str, RateLimitCircuitRule] = {}
        for domain, raw in (config or {}).items():
            rule = _parse_circuit_rule(raw)
            if rule is not None:
                self._rules[str(domain).strip().lower()] = rule
        self._failures: dict[str, int] = {}
        self._open_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def has_rules(self) -> bool:
        return bool(self._rules)

    def open_for(self, urls: Sequence[str]) -> str | None:
        now = time.monotonic()
        with self._lock:
            for domain in self._domains_for(urls):
                until = self._open_until.get(domain)
                if until is None:
                    continue
                if now < until:
                    return domain
                self._open_until.pop(domain, None)
                self._failures[domain] = 0
        return None

    def record_success(self, urls: Sequence[str]) -> None:
        with self._lock:
            for domain in self._domains_for(urls):
                self._failures[domain] = 0

    def record_rate_limited(self, urls: Sequence[str]) -> str | None:
        now = time.monotonic()
        opened: str | None = None
        with self._lock:
            for domain in self._domains_for(urls):
                rule = self._rules[domain]
                count = self._failures.get(domain, 0) + 1
                self._failures[domain] = count
                if count >= rule.threshold:
                    self._open_until[domain] = now + rule.cooldown_seconds
                    opened = domain
        return opened

    def _domains_for(self, urls: Sequence[str]) -> list[str]:
        domains: set[str] = set()
        for url in urls:
            host = (urlsplit(url).hostname or "").lower()
            domain = self._domain_for(host)
            if domain is not None:
                domains.add(domain)
        return sorted(domains)

    def _domain_for(self, host: str) -> str | None:
        best: str | None = None
        for domain in self._rules:
            if host == domain or host.endswith("." + domain):
                if best is None or len(domain) > len(best):
                    best = domain
        return best


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
        )
    try:
        threshold = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return RateLimitCircuitRule(threshold=threshold) if threshold > 0 else None


def _rate_limited_status(stderr: bytes | str | None) -> str | None:
    text = _stderr_tail(stderr).lower()
    if not text:
        return None
    if "403 forbidden" in text or "http error 403" in text or "server returned 403" in text:
        return "HTTP 403"
    if "429 too many" in text or "http error 429" in text or "server returned 429" in text:
        return "HTTP 429"
    return None


def _raise_if_rate_limited(*, phase: str, stderr: bytes | str | None) -> None:
    status = _rate_limited_status(stderr)
    if status is not None:
        raise RateLimitedMediaFetchError(f"ffmpeg {phase} hit provider throttle ({status})")


def _guard_against_truncated_audio(
    ep: Episode, probed: float | None, *, size_bytes: int | None = None
) -> None:
    """Raise :class:`TruncatedAudioError` if the encode looks like a throttled/truncated fetch
    rather than the real meeting: either an empty/near-empty output (``size_bytes`` below
    :data:`_MIN_PLAUSIBLE_AUDIO_BYTES` — works for any provider, incl. duration-less Swagit) or,
    when the feed declares a duration, an output under :data:`_TRUNCATION_MIN_RATIO` of it."""
    if size_bytes is not None and size_bytes < _MIN_PLAUSIBLE_AUDIO_BYTES:
        raise TruncatedAudioError(
            f"encoded audio is {size_bytes}B (empty/near-empty) — source fetch likely "
            f"throttled/truncated; backing off"
        )
    expected = ep.duration
    if not expected or expected <= 0 or probed is None:
        return
    if probed < _TRUNCATION_MIN_RATIO * expected:
        raise TruncatedAudioError(
            f"encoded audio {probed:.0f}s is under {_TRUNCATION_MIN_RATIO:.0%} of the declared "
            f"{expected}s — source fetch likely throttled/truncated; backing off"
        )


def _run_ffmpeg_guarded(
    cmd: list[str],
    *,
    phase: str,
    timeout: float | None = None,
    memory_floor_bytes: int | None = None,
    rate_limit_urls: Sequence[str] = (),
    poll_seconds: float = _FFMPEG_GUARD_POLL_SECONDS,
    snapshot: Callable[[], ResourceSnapshot] = current_snapshot,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] | None = print,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    child_rss: Callable[[int], int | None] = _process_rss_bytes,
) -> None:
    """Run ffmpeg with wall-clock and available-memory guardrails.

    ``subprocess.run(timeout=...)`` can only bound elapsed time. The Actions failure mode we are
    seeing is available memory collapsing while a child ffmpeg process is still active, so poll the
    whole-runner ``MemAvailable`` and terminate ffmpeg before the runner agent is killed.

    ``rate_limit_urls`` are the *remote* sources this invocation reads; the per-host concurrency cap
    (issue #39, :data:`citypods.http.HOST_LIMITER`) is held for the whole subprocess so a sharded
    burst of workers never opens more than the configured number of simultaneous connections to one
    provider tenant. Local-file inputs need not be passed (they resolve to no host → no-op).
    """
    if not memory_floor_bytes:
        _log_ffmpeg_event(log, f"[enrich] ffmpeg {phase} start")
        try:
            with (
                DISTRIBUTED_PROVIDER_LEASES.slots(rate_limit_urls),
                HOST_LIMITER.slots(rate_limit_urls),
            ):
                subprocess.run(cmd, check=True, capture_output=True, timeout=timeout)
        except subprocess.CalledProcessError as exc:
            stderr = _stderr_tail(exc.stderr)
            detail = f" stderr={stderr}" if stderr else ""
            _log_ffmpeg_event(
                log,
                f"[enrich] ffmpeg {phase} error returncode={exc.returncode}{detail}",
            )
            _raise_if_rate_limited(phase=phase, stderr=exc.stderr)
            raise
        except subprocess.TimeoutExpired as exc:
            stderr = _stderr_tail(exc.stderr)
            detail = f" stderr={stderr}" if stderr else ""
            _log_ffmpeg_event(log, f"[enrich] ffmpeg {phase} timeout seconds={timeout}{detail}")
            raise
        _log_ffmpeg_event(log, f"[enrich] ffmpeg {phase} done")
        return

    # Memory-floor path: hold the per-host rate-limit slot (#39) for the whole monitored run so a
    # sharded burst can't open more than the configured number of simultaneous connections per host.
    with DISTRIBUTED_PROVIDER_LEASES.slots(rate_limit_urls), HOST_LIMITER.slots(rate_limit_urls):
        _run_ffmpeg_popen_monitored(
            cmd,
            phase=phase,
            timeout=timeout,
            memory_floor_bytes=memory_floor_bytes,
            poll_seconds=poll_seconds,
            snapshot=snapshot,
            sleep=sleep,
            log=log,
            popen=popen,
            child_rss=child_rss,
        )


def _run_ffmpeg_popen_monitored(
    cmd: list[str],
    *,
    phase: str,
    timeout: float | None,
    memory_floor_bytes: int | None,
    poll_seconds: float,
    snapshot: Callable[[], ResourceSnapshot],
    sleep: Callable[[float], None],
    log: Callable[[str], None] | None,
    popen: Callable[..., subprocess.Popen],
    child_rss: Callable[[int], int | None],
) -> None:
    """Popen + poll/sample loop for :func:`_run_ffmpeg_guarded` (memory-floor path). The caller
    holds the per-host rate-limit slot for the whole monitored run."""
    started = time.monotonic()
    proc = popen(  # noqa: S603 - command is assembled by this module from validated URLs/paths.
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )

    _log_ffmpeg_event(log, f"[enrich] ffmpeg {phase} start pid={proc.pid}")
    peak_child_rss: int | None = None
    min_mem_available: int | None = None
    samples = 0

    while True:
        samples += 1
        rss = child_rss(proc.pid)
        if rss is not None:
            peak_child_rss = rss if peak_child_rss is None else max(peak_child_rss, rss)

        snap = snapshot()
        available = snap.mem_available_bytes
        if available is not None:
            min_mem_available = (
                available if min_mem_available is None else min(min_mem_available, available)
            )

        returncode = proc.poll()
        if returncode is not None:
            stdout, stderr = proc.communicate()
            elapsed = time.monotonic() - started
            if returncode != 0:
                stderr_text = _stderr_tail(stderr)
                detail = f" stderr={stderr_text}" if stderr_text else ""
                _log_ffmpeg_event(
                    log,
                    f"[enrich] ffmpeg {phase} error pid={proc.pid} seconds={elapsed:.1f} "
                    f"returncode={returncode} peak_rss={_format_optional_bytes(peak_child_rss)} "
                    f"min_mem_avail={_format_optional_bytes(min_mem_available)} "
                    f"samples={samples}{detail}",
                )
                _raise_if_rate_limited(phase=phase, stderr=stderr)
                raise subprocess.CalledProcessError(
                    returncode,
                    cmd,
                    output=stdout,
                    stderr=stderr,
                )
            _log_ffmpeg_event(
                log,
                f"[enrich] ffmpeg {phase} done pid={proc.pid} seconds={elapsed:.1f} "
                f"peak_rss={_format_optional_bytes(peak_child_rss)} "
                f"min_mem_avail={_format_optional_bytes(min_mem_available)} samples={samples}",
            )
            return

        elapsed = time.monotonic() - started
        if timeout is not None and elapsed >= timeout:
            proc.terminate()
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
            stderr_text = _stderr_tail(stderr)
            detail = f" stderr={stderr_text}" if stderr_text else ""
            _log_ffmpeg_event(
                log,
                f"[enrich] ffmpeg {phase} timeout pid={proc.pid} seconds={elapsed:.1f} "
                f"peak_rss={_format_optional_bytes(peak_child_rss)} "
                f"min_mem_avail={_format_optional_bytes(min_mem_available)} "
                f"samples={samples}{detail}",
            )
            raise subprocess.TimeoutExpired(cmd, timeout, output=stdout, stderr=stderr)

        if (
            memory_floor_bytes is not None
            and memory_floor_bytes > 0
            and available is not None
            and available < memory_floor_bytes
        ):
            _log_ffmpeg_event(
                log,
                f"[enrich] ffmpeg {phase} memory-stop pid={proc.pid} "
                f"mem_avail={format_bytes(available)} floor={format_bytes(memory_floor_bytes)} "
                f"peak_rss={_format_optional_bytes(peak_child_rss)} "
                f"min_mem_avail={_format_optional_bytes(min_mem_available)} samples={samples}",
            )
            proc.terminate()
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
            raise FfmpegMemoryLimitExceeded(
                phase=phase,
                cmd=cmd,
                floor=memory_floor_bytes,
                available=available,
            )

        remaining = None if timeout is None else max(0.1, timeout - elapsed)
        sleep_for = poll_seconds if remaining is None else min(poll_seconds, remaining)
        sleep(sleep_for)


def _download_audio(
    url: str,
    dest: Path,
    ffmpeg_binary: str = "ffmpeg",
    timeout: float | None = None,
    memory_floor_bytes: int | None = None,
    max_seconds: float | None = None,
    log: Callable[[str], None] | None = print,
) -> bool:
    """Copy the source audio stream from *url* to *dest* without re-encoding.

    Returns True on success; callers fall back to streaming *url* directly on False. ``max_seconds``
    bounds the copy to the first N seconds (a *truncated* fetch) — used by the live media-fetch
    contract check to verify an endpoint is reachable without pulling a whole meeting.

    The source cache uses a Matroska audio container so it can preserve common provider codecs
    (AAC, MP3, MP2, PCM, AC-3, etc.) without pretending they are already podcast-ready M4A files.
    """
    cmd = [
        ffmpeg_binary,
        "-y",
        "-loglevel",
        "error",
        # Browser-compatible UA for remote fetches (Granicus CDN blocks others); omitted for a local
        # input, where ffmpeg would reject the http-only option (see _ua_args).
        *_ua_args(url),
        "-protocol_whitelist",
        "file,crypto,data,http,https,tcp,tls",
        "-rw_timeout",
        str(_STALL_TIMEOUT_US),
        "-i",
        url,
        "-vn",
        "-c:a",
        "copy",
        *(["-t", str(max_seconds)] if max_seconds else []),
        "-f",
        "matroska",
        str(dest),
    ]
    try:
        _run_ffmpeg_guarded(
            cmd,
            phase="source-cache",
            timeout=timeout,
            memory_floor_bytes=memory_floor_bytes,
            rate_limit_urls=(url,),  # the remote source — cap concurrent hits per provider (#39)
            log=log,
        )
        return dest.exists() and dest.stat().st_size > 0
    except RateLimitedMediaFetchError:
        raise
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, OSError):
        return False


class SourceCache:
    """Thread-safe per-run cache: episode uid → locally downloaded audio file.

    SilencePlanner downloads each source once; AudioStage reads the local copy rather
    than streaming the same rate-limited source a second time.

    Use as a context manager so the TemporaryDirectory is cleaned up after the run:

        with SourceCache(ffmpeg_binary="ffmpeg", timeout_seconds=2700) as cache:
            ctx = StageContext(..., source_cache=cache)
    """

    def __init__(
        self,
        ffmpeg_binary: str = "ffmpeg",
        timeout_seconds: float | None = None,
        memory_floor_bytes: int | None = None,
    ):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="citypods_src_")
        self._paths: dict[str, Path] = {}
        self._locks: dict[str, threading.Lock] = collections.defaultdict(threading.Lock)
        self._guard = threading.Lock()
        self.ffmpeg_binary = ffmpeg_binary
        self.timeout_seconds = timeout_seconds
        self.memory_floor_bytes = memory_floor_bytes

    def __enter__(self) -> SourceCache:
        return self

    def __exit__(self, *_: object) -> None:
        self._tmpdir.cleanup()

    def get(self, uid: str) -> Path | None:
        """Return the cached local path for *uid*, or None if not yet downloaded."""
        with self._guard:
            return self._paths.get(uid)

    def get_or_fetch(self, uid: str, url: str) -> Path | None:
        """Return a local audio copy keyed by *uid*, downloading *url* on first call.

        Thread-safe: concurrent callers for the same *uid* block until the first
        download completes, then all receive the same path. Returns None on generic failure
        (caller may fall back to streaming *url* directly); provider throttling propagates as
        ``RateLimitedMediaFetchError`` so the caller does not immediately retry the same URL.
        """
        with self._guard:
            lock = self._locks[uid]
        with lock:
            if uid in self._paths:
                return self._paths[uid]
            dest = Path(self._tmpdir.name) / f"{hashlib.md5(uid.encode()).hexdigest()}.mka"
            if _download_audio(
                url,
                dest,
                self.ffmpeg_binary,
                self.timeout_seconds,
                self.memory_floor_bytes,
            ):
                self._paths[uid] = dest
                return dest
            return None


def _in_backoff(ep: Episode, now: datetime) -> bool:
    """True if ``ep`` failed recently enough to still be inside its materialization backoff."""
    if ep.materialize_attempts <= 0 or not ep.materialize_last_attempt:
        return False
    try:
        last = datetime.fromisoformat(ep.materialize_last_attempt)
    except ValueError:
        return False
    delay = min(BACKOFF_MAX, BACKOFF_BASE * 2 ** (ep.materialize_attempts - 1))
    return now < last + delay


class FfmpegRunner(Protocol):
    """Renders a Timeline's audio segments into a single M4A file.

    Implementations receive the episode's EDL (``timeline``) and a dict mapping
    each ``source_id`` to the resolved, playable URL for that source.  The identity
    path (``timeline is None``) must produce the same bytes as the pre-INFRA-3
    copy/re-encode behaviour — no re-encode storm for existing un-manipulated audio.
    """

    def extract_audio(
        self,
        timeline: Timeline | None,
        sources_by_id: dict[str, str],
        dest: Path,
        chapters: list[dict] | None = None,
        *,
        loudness_profile: str | None = None,
        asset_resolver: Callable[[str, str | None], Path] | None = None,
    ) -> None:
        """Render ``timeline`` into ``dest`` (.m4a).

        Args:
            timeline: The episode's EDL, or ``None`` for the identity (full-copy) path.
            sources_by_id: Maps ``source_id`` → resolved playable URL.  For identity
                episodes this has exactly one entry; for concat it has N.
            dest: Output file path (will be created/overwritten).
            chapters: Served-time chapter markers embedded as M4A chapter atoms.
            loudness_profile: e.g. ``"ebuR128:-16LUFS"``; ``None`` = no loudnorm.
            asset_resolver: Required when ``timeline`` contains insert segments; maps
                ``(asset_id, asset_version)`` to a local file path.
        """
        ...


# ---------------------------------------------------------------------------
# ffmetadata (chapters)
# ---------------------------------------------------------------------------


def _ffmetadata(chapters: list[dict]) -> str:
    """Render chapter markers as an ffmpeg metadata file (millisecond timebase). The end of a
    chapter is its own ``end`` when known, else the next chapter's start; the last falls back to
    start+1s so ffmpeg always has a valid span."""
    out = [";FFMETADATA1"]
    ordered = sorted(chapters, key=lambda c: c["start"])
    for i, ch in enumerate(ordered):
        start = int(ch["start"]) * 1000
        nxt = int(ordered[i + 1]["start"]) * 1000 if i + 1 < len(ordered) else None
        end_s = ch.get("end")
        end = int(end_s) * 1000 if end_s is not None else (nxt if nxt is not None else start + 1000)
        if end <= start:
            end = start + 1000
        title = re.sub(r"([=;#\\\n])", r"\\\1", ch.get("title", "").strip())
        out += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={start}", f"END={end}", f"title={title}"]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Codec / bitrate helpers
# ---------------------------------------------------------------------------


_M4A_COPY_CODECS = {"aac"}


def encode_args(
    source_bitrate: int | None, max_kbps: int, source_codec: str | None = "aac"
) -> list[str]:
    """ffmpeg audio codec args for the podcast M4A output.

    Copy only when the source is already AAC and under the bitrate cap. Non-AAC sources may be
    cheap enough by bitrate, but stream-copying them into the iPod/M4A muxer can fail with
    incompatible-tag errors; transcode those to the normalized AAC output.
    """
    codec = source_codec.lower() if source_codec else None
    if (
        codec in _M4A_COPY_CODECS
        and source_bitrate is not None
        and source_bitrate <= max_kbps * 1000
    ):
        return ["-c:a", "copy"]
    return ["-c:a", "aac", "-b:a", f"{max_kbps}k", "-ac", "1"]


def _parse_lufs(profile: str) -> str:
    """Extract the integrated loudness target from a profile string.

    e.g. ``"ebuR128:-16LUFS"`` → ``"-16"``.  Falls back to ``"-16"`` when unparseable.
    """
    if ":" in profile:
        _, part = profile.rsplit(":", 1)
        val = part.replace("LUFS", "").strip()
        if val:
            return val
    return "-16"


# ---------------------------------------------------------------------------
# Filtergraph builder (pure — no subprocess, fully testable)
# ---------------------------------------------------------------------------


def build_filter_complex(
    segments: tuple[Segment, ...],
    source_input_idx: dict[str, int],
    asset_input_idx: dict[tuple[str | None, str | None], int],
    loudness_profile: str | None = None,
) -> tuple[str, str]:
    """Build an ffmpeg ``filter_complex`` string from Timeline segments.

    Returns ``(filter_complex_string, output_stream_label)`` where
    ``output_stream_label`` is the label to pass to ``-map`` (e.g. ``"[outa]"``).

    Each source segment uses ``atrim+asetpts`` to extract the correct span; insert
    segments copy their asset input directly.  Multiple segments are joined with
    ``concat``.  An optional ``loudnorm`` is appended when ``loudness_profile`` is set.

    ``source_input_idx`` maps ``source_id -> ffmpeg input index`` (0-based).
    ``asset_input_idx`` maps ``(asset_id, asset_version) -> ffmpeg input index``.
    """
    parts: list[str] = []
    labels: list[str] = []

    for i, seg in enumerate(segments):
        lbl = f"a{i}"
        if seg.kind == "source":
            idx = source_input_idx[seg.source_id]  # type: ignore[index]
            start = seg.source_start or 0.0
            end = seg.source_end
            trim = f"atrim=start={start}:end={end}" if end is not None else f"atrim=start={start}"
            parts.append(f"[{idx}:a]{trim},asetpts=PTS-STARTPTS[{lbl}]")
        elif seg.kind == "insert":
            key = (seg.asset_id, seg.asset_version)
            idx = asset_input_idx[key]
            parts.append(f"[{idx}:a]acopy[{lbl}]")
        labels.append(f"[{lbl}]")

    n = len(labels)

    if n == 1:
        # Single segment: no concat needed.
        joined = "".join(labels)
        if loudness_profile:
            lufs = _parse_lufs(loudness_profile)
            parts.append(f"{joined}loudnorm=I={lufs}:TP=-1.5:LRA=11[outa]")
            return ";".join(parts), "[outa]"
        return ";".join(parts), labels[0]

    # n > 1: concatenate. When branches can differ in format — multiple distinct sources
    # (#122) or an insert asset (#25) spliced beside source audio — normalize every branch to
    # a common mono rate first so ``concat`` never has to negotiate mismatched sample rates /
    # channel layouts (deterministic across ffmpeg versions; complements the pinned-ffmpeg CI
    # decision). Same-source trims (#111) are already uniform, so we skip the extra resample
    # there to keep that graph — and its bytes — minimal. Mono matches the final ``-ac 1``.
    distinct_sources = {s.source_id for s in segments if s.kind == "source"}
    needs_norm = len(distinct_sources) > 1 or any(s.kind == "insert" for s in segments)
    if needs_norm:
        norm_labels: list[str] = []
        for i, lbl in enumerate(labels):
            nlbl = f"n{i}"
            parts.append(f"{lbl}aresample=48000,aformat=channel_layouts=mono[{nlbl}]")
            norm_labels.append(f"[{nlbl}]")
        joined = "".join(norm_labels)
    else:
        joined = "".join(labels)

    if loudness_profile:
        lufs = _parse_lufs(loudness_profile)
        parts.append(f"{joined}concat=n={n}:v=0:a=1[preln]")
        parts.append(f"[preln]loudnorm=I={lufs}:TP=-1.5:LRA=11[outa]")
    else:
        parts.append(f"{joined}concat=n={n}:v=0:a=1[outa]")
    return ";".join(parts), "[outa]"


# ---------------------------------------------------------------------------
# CommandFfmpeg
# ---------------------------------------------------------------------------


class CommandFfmpeg:
    """Runs the real ffmpeg binary.

    **Identity path** (``timeline is None`` or identity digest): re-encodes only when
    the source exceeds the bitrate cap, exactly as before INFRA-3.

    **Filter path** (non-identity timeline or loudnorm): builds a ``filter_complex``
    that trims/concatenates/inserts segments; always re-encodes (copy is incompatible
    with ``filter_complex``).
    """

    def __init__(
        self,
        binary: str = "ffmpeg",
        max_kbps: int = 96,
        timeout_seconds: float | None = None,
        threads: int | None = None,
        memory_floor_bytes: int | None = None,
    ):
        self.binary = binary
        self.max_kbps = max_kbps
        # Hard wall-clock cap for one probe+encode (None = uncapped, e.g. tests). See module
        # constants above for why an in-flight encode must be bounded.
        self.timeout_seconds = timeout_seconds
        # ffmpeg defaults to "all cores" for AAC. Pin it so concurrent encodes do not
        # multiply into CPU oversubscription on the 4-core Actions runner.
        self.threads = max(1, int(threads)) if threads is not None else None
        self.memory_floor_bytes = memory_floor_bytes

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def extract_audio(
        self,
        timeline: Timeline | None,
        sources_by_id: dict[str, str],
        dest: Path,
        chapters: list[dict] | None = None,
        *,
        loudness_profile: str | None = None,
        asset_resolver: Callable[[str, str | None], Path] | None = None,
    ) -> None:
        use_filter = (timeline is not None and timeline_digest(timeline) != "") or bool(
            loudness_profile
        )

        if use_filter:
            self._render_filter(
                timeline,
                sources_by_id,
                dest,
                chapters=chapters,
                loudness_profile=loudness_profile,
                asset_resolver=asset_resolver,
            )
        else:
            self._render_identity(sources_by_id, dest, chapters=chapters)

    # ------------------------------------------------------------------
    # Identity path: same args as pre-INFRA-3
    # ------------------------------------------------------------------

    def _render_identity(
        self,
        sources_by_id: dict[str, str],
        dest: Path,
        chapters: list[dict] | None = None,
    ) -> None:
        source_url = next(iter(sources_by_id.values()))
        probe_timeout = (
            None if self.timeout_seconds is None else min(self.timeout_seconds, _PROBE_TIMEOUT_S)
        )
        stream_info = _probe_audio_stream(source_url, self.binary, timeout=probe_timeout)
        codec_args = encode_args(
            stream_info.bit_rate, self.max_kbps, source_codec=stream_info.codec_name
        )
        thread_args = self._thread_args(codec_args)
        with tempfile.TemporaryDirectory() as tmp:
            # -user_agent only for a remote source (the source-cache may hand us a local file, where
            # ffmpeg rejects the http-only option — see _ua_args).
            inputs = [
                *_ua_args(source_url),
                "-rw_timeout",
                str(_STALL_TIMEOUT_US),
                "-i",
                source_url,
            ]
            chapter_args: list[str] = []
            if chapters:
                meta = Path(tmp) / "chapters.ffmeta"
                meta.write_text(_ffmetadata(chapters))
                inputs += ["-i", str(meta)]
                chapter_args = ["-map_chapters", "1", "-map", "0:a:0"]
            cmd = [
                self.binary,
                "-y",
                "-loglevel",
                "error",
                "-protocol_whitelist",
                "file,crypto,data,http,https,tcp,tls",
                *inputs,
                "-vn",
                *chapter_args,
                *codec_args,
                *thread_args,
                "-movflags",
                "+faststart",
                str(dest),
            ]
            _run_ffmpeg_guarded(
                cmd,
                phase="identity-render",
                timeout=self.timeout_seconds,
                memory_floor_bytes=self.memory_floor_bytes,
                rate_limit_urls=(source_url,),  # no-op if it's a local cached copy (#39)
            )

    def _thread_args(self, codec_args: list[str]) -> list[str]:
        if self.threads is None or "aac" not in codec_args:
            return []
        return ["-threads", str(self.threads)]

    def _filter_thread_args(self) -> list[str]:
        if self.threads is None:
            return []
        n = str(self.threads)
        return ["-filter_threads", n, "-filter_complex_threads", n]

    # ------------------------------------------------------------------
    # Filter path: atrim + concat + optional loudnorm
    # ------------------------------------------------------------------

    def _render_filter(
        self,
        timeline: Timeline | None,
        sources_by_id: dict[str, str],
        dest: Path,
        chapters: list[dict] | None = None,
        loudness_profile: str | None = None,
        asset_resolver: Callable[[str, str | None], Path] | None = None,
    ) -> None:
        segs: tuple[Segment, ...] = timeline.segments if timeline is not None else ()

        # --- collect ordered source inputs (in order of first appearance) ---
        source_ids: list[str] = []
        for seg in segs:
            if seg.kind == "source" and seg.source_id not in source_ids:
                source_ids.append(seg.source_id)  # type: ignore[arg-type]

        source_input_idx = {sid: i for i, sid in enumerate(source_ids)}
        next_idx = len(source_ids)

        # --- collect insert asset inputs ---
        asset_keys: list[tuple[str | None, str | None]] = []
        asset_input_idx: dict[tuple[str | None, str | None], int] = {}
        for seg in segs:
            if seg.kind == "insert":
                key = (seg.asset_id, seg.asset_version)
                if key not in asset_input_idx:
                    if asset_resolver is None:
                        raise ValueError(
                            f"insert segment (asset_id={seg.asset_id!r}) requires an asset_resolver"
                        )
                    asset_input_idx[key] = next_idx
                    asset_keys.append(key)
                    next_idx += 1

        with tempfile.TemporaryDirectory() as tmp:
            # Build input flags. -user_agent only for remote sources (Granicus CDN blocks others);
            # a cached/local source — or the insert assets below — must not get it (_ua_args).
            inputs: list[str] = []
            for sid in source_ids:
                inputs += [
                    *_ua_args(sources_by_id[sid]),
                    "-rw_timeout",
                    str(_STALL_TIMEOUT_US),
                    "-i",
                    sources_by_id[sid],
                ]
            for asset_id, asset_version in asset_keys:
                asset_path = asset_resolver(asset_id, asset_version)  # type: ignore[misc]
                inputs += ["-i", str(asset_path)]

            filter_str, out_label = build_filter_complex(
                segs, source_input_idx, asset_input_idx, loudness_profile
            )

            # Chapter metadata (served-time; comes in as a separate input)
            chapter_args: list[str] = []
            if chapters:
                meta = Path(tmp) / "chapters.ffmeta"
                meta.write_text(_ffmetadata(chapters))
                inputs += ["-i", str(meta)]
                chapter_args = ["-map_chapters", str(next_idx + len(asset_keys))]

            cmd = [
                self.binary,
                "-y",
                "-loglevel",
                "error",
                "-protocol_whitelist",
                "file,crypto,data,http,https,tcp,tls",
                *inputs,
                *self._filter_thread_args(),
                "-filter_complex",
                filter_str,
                "-map",
                out_label,
                "-vn",
                *chapter_args,
                "-c:a",
                "aac",
                "-b:a",
                f"{self.max_kbps}k",
                "-ac",
                "1",
                *self._thread_args(["-c:a", "aac"]),
                "-movflags",
                "+faststart",
                str(dest),
            ]
            _run_ffmpeg_guarded(
                cmd,
                phase="filter-render",
                timeout=self.timeout_seconds,
                memory_floor_bytes=self.memory_floor_bytes,
                # Cap concurrent hits per provider for any remote source inputs (#39); local
                # cached copies / insert assets resolve to no host → no-op.
                rate_limit_urls=tuple(sources_by_id[sid] for sid in source_ids),
            )


# ---------------------------------------------------------------------------
# ffprobe helper
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AudioStreamInfo:
    codec_name: str | None = None
    bit_rate: int | None = None


def _parse_optional_int(value: object) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    return int(text) if text.isdigit() else None


def _probe_audio_stream(
    url: str, ffmpeg_binary: str = "ffmpeg", timeout: float | None = None
) -> AudioStreamInfo:
    """Return source audio codec/bitrate via ffprobe, or unknown fields on failure."""
    ffprobe = "ffprobe" if ffmpeg_binary == "ffmpeg" else ffmpeg_binary.replace("ffmpeg", "ffprobe")
    try:
        with DISTRIBUTED_PROVIDER_LEASES.slots([url]), HOST_LIMITER.slot(url):
            out = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    # Browser-compatible UA for a remote probe (else the Granicus CDN 403s it);
                    # omitted for a local cached file (invalid there) — _ua_args.
                    *_ua_args(url),
                    "-select_streams",
                    "a:0",
                    "-show_entries",
                    "stream=codec_name,bit_rate",
                    "-of",
                    "json",
                    url,
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            ).stdout.strip()
        data = json.loads(out or "{}")
        streams = data.get("streams")
        stream = streams[0] if isinstance(streams, list) and streams else {}
        codec_raw = stream.get("codec_name") if isinstance(stream, dict) else None
        codec = str(codec_raw).strip().lower() if codec_raw else None
        bit_rate = _parse_optional_int(stream.get("bit_rate") if isinstance(stream, dict) else None)
        return AudioStreamInfo(codec_name=codec or None, bit_rate=bit_rate)
    except (
        subprocess.TimeoutExpired,
        subprocess.CalledProcessError,
        OSError,
        ValueError,
        TypeError,
        AttributeError,
    ):
        return AudioStreamInfo()


def _probe_audio_bitrate(
    url: str, ffmpeg_binary: str = "ffmpeg", timeout: float | None = None
) -> int | None:
    """Return the source's audio bitrate in bits/sec via ffprobe, or None if unknown.

    ``timeout`` (seconds) bounds the probe so a stalled source can't hang it; on timeout (or any
    other probe failure) we return None, which encode_args treats as "unknown" → safe re-encode."""
    return _probe_audio_stream(url, ffmpeg_binary, timeout=timeout).bit_rate


def _probe_duration_secs(path: Path, ffmpeg_binary: str = "ffmpeg") -> float | None:
    """Read container duration from a local file via ffprobe (header-only, fast).

    Used after encoding to capture the served duration before the temp file is deleted,
    so the record carries ``audio_duration_served`` even for providers (Swagit, CivicPlus)
    that never set ``ep.duration``."""
    ffprobe = "ffprobe" if ffmpeg_binary == "ffmpeg" else ffmpeg_binary.replace("ffmpeg", "ffprobe")
    try:
        out = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10.0,
        ).stdout.strip()
        return float(out) if out else None
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Materialization stats + pipeline
# ---------------------------------------------------------------------------


@dataclass
class MaterializeStats:
    hosted: int = 0  # newly hosted this run (encoded + credited)
    encoded: int = 0  # downloaded + ffmpeg + uploaded — the expensive path; drives the time budget
    credited: int = 0  # object already in storage, only its URL (re)attached — near-free
    reused: int = 0  # already in manifest / storage
    skipped_budget: int = 0  # deferred to a later run
    skipped_backoff: int = 0  # deferred: still inside a post-failure backoff window (#120)
    errors: list[str] = field(default_factory=list)
    bytes_written: int = 0  # total bytes of objects uploaded this run (for cost accounting)


def _should_host(episode: Episode, city: City) -> bool:
    if episode.media_kind == "hls":
        return True
    return city.extract_audio  # direct source, opt-in extraction


def _hosted_keys(city: City, storage: StorageBackend) -> set[str] | None:
    """The set of audio object keys actually present in storage for this source, fetched in a
    single ``list_objects`` call over the source prefix. ``None`` when the backend can't list
    (callers then fall back to a per-episode ``exists()`` probe). This is what lets the reuse
    short-circuit trust the *storage*, not just the record: a record can carry a
    ``hosted_audio_url`` whose object was never written or has since been GC'd — most acutely for
    Swagit, whose presigned *source* URL expires while its stable spec hash keeps matching, so a
    record-only check concludes "already hosted" forever and never re-materializes (issue #116)."""
    if not hasattr(storage, "list_objects"):
        return None
    prefix = f"{city.provider}/{source_key(city)}/"
    return {key for key, _ in storage.list_objects(prefix)}


def _sources_by_id(ep: Episode, source_url: str) -> dict[str, str]:
    """Map source_id → resolved URL.

    For multi-source concat episodes (``len(ep.sources) > 1``), each ``SourceMedia.ref``
    is a stable direct-MP4 URL set by the concat planner — no re-resolution needed.
    For single-source episodes, the freshly-resolved ``source_url`` is used (it may be a
    short-lived presigned URL, so we don't cache it on the record).
    """
    if ep.sources and len(ep.sources) > 1:
        return {s.id: s.ref for s in ep.sources}
    if ep.sources:
        return {ep.sources[0].id: source_url}
    return {"s0": source_url}


def _served_duration(ep: Episode) -> float | None:
    """The served (enclosure) duration to record as ``audio_duration_served``.

    Derived from the EDL — the sum of served segment lengths — whenever the episode is
    actually manipulated, rather than trusting ``ep.duration`` (which stays the *source*
    duration until a planner overwrites it). For identity episodes the two are equal, so this
    is a no-op there; for trims/concats it keeps ``audio_duration_served`` correct by
    construction and makes the INFRA-9 ``timeline-duration-mismatch`` contract check meaningful
    (it compares the segment total against this field). Falls back to ``ep.duration`` when there
    is no timeline (identity) or no known duration."""
    tl = ep.timeline
    if tl is not None and timeline_digest(tl) != "":
        duration = sum(s.served_end - s.served_start for s in tl.segments)
        return duration if duration > 0 else None
    if ep.duration is None:
        return None
    duration = float(ep.duration)
    return duration if duration > 0 else None


def _has_non_identity_timeline(ep: Episode) -> bool:
    return ep.timeline is not None and timeline_digest(ep.timeline) != ""


# Encode peak-RSS cost model (H8 reservation admission). Filter-render (loudnorm/trim/concat)
# encodes of long meetings dominate runner memory — observed 0.18–5.9 GiB on the 15.6 GiB runner,
# growing across the *whole* encode (memory-floor kills fired 220–1080 s in). The reservation
# accountant (``citypods/resources.py:MemoryReservation``) admits by this *predicted* footprint
# instead of the trailing ``mem_available`` signal. Coefficients are a first heuristic keyed on
# served length — known ahead from the EDL the TimelineStage already built, or the feed duration —
# and are meant to be calibrated from the per-encode ``peak_rss`` we log.
_ENCODE_RSS_COPY_BYTES = 300 * 1024 * 1024  # copy path (no filter): tiny, bounded
_ENCODE_RSS_BASE_BYTES = 350 * 1024 * 1024  # filtergraph + AAC encoder baseline
# 2026-06-15 telemetry from audio run #10 showed long loudnorm/filter jobs peaking around
# 9-13 GiB; the prior 32 MiB/min coefficient and 6.5 GiB clamp admitted too many long jobs.
_ENCODE_RSS_PER_MINUTE_BYTES = 64 * 1024 * 1024
_ENCODE_RSS_UNKNOWN_BYTES = 12_000 * 1024 * 1024
_ENCODE_RSS_MAX_BYTES = 12_000 * 1024 * 1024


def estimate_encode_rss_bytes(ep: Episode, *, loudness_profile: str = "") -> int:
    """Predict an encode's peak RSS so the reservation accountant can admit by future load.

    Copy-path (no filter) encodes are cheap. Filter encodes scale with the *served* length — known
    ahead from the EDL the preceding ``TimelineStage`` built, or the feed duration — clamped to the
    observed ceiling. When neither is known (a single-source episode with silence-trim disabled and
    no declared duration) we assume a large job and reserve conservatively, so an unknown encode
    runs alone rather than colliding; the mid-flight memory floor is the backstop for a wrong guess.
    """
    use_filter = _has_non_identity_timeline(ep) or bool(loudness_profile)
    if not use_filter:
        return _ENCODE_RSS_COPY_BYTES
    served = _served_duration(ep)
    if served is None or served <= 0:
        return _ENCODE_RSS_UNKNOWN_BYTES
    est = _ENCODE_RSS_BASE_BYTES + int(_ENCODE_RSS_PER_MINUTE_BYTES * (served / 60.0))
    return min(est, _ENCODE_RSS_MAX_BYTES)


def _backfill_served_duration(ep: Episode) -> str:
    served = _served_duration(ep)
    if _has_non_identity_timeline(ep):
        if served is None:
            return "existing" if ep.audio_duration_served else "unknown"
        if ep.audio_duration_served is None or abs(ep.audio_duration_served - served) > 0.001:
            ep.audio_duration_served = served
            return "metadata"
        return "existing"
    if ep.audio_duration_served is not None and ep.audio_duration_served > 0:
        return "existing"
    if served is not None:
        ep.audio_duration_served = served
        return "metadata"
    return "unknown"


def materialize_audio(
    city: City,
    episodes: list[Episode],
    *,
    storage: StorageBackend,
    ffmpeg: FfmpegRunner,
    max_kbps: int,
    loudness_profile: str = "",
    resolve_media_url: Callable[[Episode], str],
    stop: Callable[[], bool] | None = None,
    source_cache: SourceCache | None = None,
    max_workers: int = 1,
    resource_admission: object | None = None,
    native_work_gate: NativeWorkGate | None = None,
    memory_reservation: MemoryReservation | None = None,
    rate_limit_circuit: MediaRateLimitCircuitBreaker | None = None,
) -> MaterializeStats:
    """(Re-)host audio for episodes that need it, content-addressed by audio spec.

    Mutates each episode in place (``audio_key`` / ``audio_spec_hash`` / ``hosted_audio_url``);
    the caller persists these onto the record store. An episode is re-encoded only when its
    audio spec changed (e.g. chapters added, bitrate policy bumped) *or* its referenced object
    has gone missing from storage — otherwise the existing object (matched by content-addressed
    key, or carried over as ``"legacy"``) is reused for free.

    A large backfill is spread over successive runs by ``stop()``: a shared predicate that goes
    True once the run's wall-clock window is spent *or* a newer Build & Deploy run is queued behind
    this one. Only the expensive encode path consults it — cheap reuse/credit/backoff bookkeeping
    always runs — so a superseded run still finishes its in-flight encode, persists, and deploys
    (graceful yield) rather than being hard-cancelled mid-deploy.

    ``source_cache``, when provided, supplies locally downloaded audio files so the encode pass
    can read from disk rather than streaming the rate-limited source a second time (the first
    download is done by SilencePlanner in the preceding TimelineStage).

    ``max_workers`` controls the inner ThreadPoolExecutor for the encode loop. Workers are
    almost entirely I/O-bound (rate-limited HLS streaming), so this can safely exceed the
    number of CPU cores.
    """
    stats = MaterializeStats()
    hosted_keys = _hosted_keys(city, storage)
    now = datetime.now(UTC)

    # Deferred-but-out-of-backoff episodes first: when a feature lands that unblocks
    # previously-deferred meetings (e.g. SwagitConcatPlanner), those episodes get encode
    # slots before fresh ones so the backlog drains within the current run window.
    episodes = sorted(
        episodes,
        key=lambda ep: 0 if (ep.materialize_attempts > 0 and not _in_backoff(ep, now)) else 1,
    )

    def _present(key: str) -> bool:
        return key in hosted_keys if hosted_keys is not None else storage.exists(key)

    # Cheap pass: handle reuse / credit / backoff inline (always sequential — fast).
    # Collect episodes that need the expensive encode into to_encode.
    to_encode: list[tuple[Episode, str, str]] = []  # (ep, spec, key)
    ffmpeg_binary = getattr(ffmpeg, "binary", "ffmpeg")
    src_key = source_key(city)

    for ep in episodes:
        if not _should_host(ep, city):
            continue

        spec = audio_spec_hash(ep, max_kbps=max_kbps, loudness_profile=loudness_profile)
        # Already hosted with a matching spec (or carried over from the legacy manifest)?
        # Trust the record only when its object is actually in storage — otherwise a stale
        # record (e.g. a Swagit episode whose presigned source expired before the object was
        # ever written) would short-circuit forever and never re-materialize (issue #116).
        spec_ok = bool(ep.hosted_audio_url) and ep.audio_spec_hash in (spec, "legacy")
        present = bool(ep.audio_key) and _present(ep.audio_key)
        if spec_ok and present:
            _backfill_served_duration(ep)
            stats.reused += 1
            continue
        if ep.hosted_audio_url and not present:
            # The record points at an object that no longer exists: drop the dead pointer so
            # the feed stops advertising missing audio, and re-materialize below (budget willing).
            ep.hosted_audio_url = None
            ep.audio_key = None
            ep.audio_spec_hash = None

        # Recently-failed episodes back off (exponential) so a permanently-broken source (e.g. a
        # Swagit meeting with no usable media — #120) stops re-trying and churning budget/time.
        if _in_backoff(ep, now):
            stats.skipped_backoff += 1
            continue

        key = audio_object_key(city, ep, spec)
        # Credit path: the object is already in storage (e.g. a prior run uploaded it but the
        # record drifted). (Re)attaching its URL is a near-free metadata op — ~10-100x cheaper than
        # an encode — so it does NOT draw from the budget. The budget meters the expensive encode
        # path only, which keeps its per-episode time estimate honest and lets a credit-heavy
        # catch-up run reconcile freely without deferring real encodes.
        if _present(key):
            ep.audio_key = key
            ep.audio_spec_hash = spec
            ep.hosted_audio_url = storage.public_url(key)
            _backfill_served_duration(ep)
            ep.materialize_attempts = 0
            ep.materialize_last_attempt = None
            ep.materialize_error = None
            stats.hosted += 1
            stats.credited += 1
            continue

        to_encode.append((ep, spec, key))

    if not to_encode:
        return stats

    # Encode pass: parallel when max_workers > 1. Each worker is independent (per-episode state
    # mutations don't overlap); only stats updates require a lock.
    lock = threading.Lock()

    def _encode_one(item: tuple[Episode, str, str]) -> None:
        ep, spec, key = item
        # Re-check stop inside the worker: submitted-but-not-yet-started tasks yield gracefully
        # when the budget expires mid-batch.
        if stop is not None and stop():
            with lock:
                stats.skipped_budget += 1
            return
        label = ep.uid or ep.guid
        # Admission: reserve the encode's predicted peak RSS so it starts only with real budget
        # headroom — a *leading* signal. The reservation supersedes the instantaneous mem_available
        # gate for audio; that gate (resource_admission) is the fallback when no budget is set and
        # still governs ASR elsewhere. native_work_gate is the hard concurrency ceiling on top.
        reserved_bytes = 0
        if memory_reservation is not None:
            reserved_bytes = estimate_encode_rss_bytes(ep, loudness_profile=loudness_profile)
            if not memory_reservation.reserve(reserved_bytes, label=str(label), stop=stop):
                with lock:
                    stats.skipped_budget += 1
                return
        elif resource_admission is not None:
            if not resource_admission.wait(kind="audio", label=str(label), stop=stop):
                with lock:
                    stats.skipped_budget += 1
                return
        gate_acquired = False
        if native_work_gate is not None:
            gate_acquired = native_work_gate.acquire(kind="audio", label=str(label), stop=stop)
            if not gate_acquired:
                with lock:
                    stats.skipped_budget += 1
                if memory_reservation is not None and reserved_bytes:
                    memory_reservation.release(reserved_bytes)
                return
        t0 = time.perf_counter()
        print(
            f"[enrich] audio encode start slug={city.slug} provider={city.provider} "
            f"source={src_key} uid={label} guid={ep.guid}",
            flush=True,
        )
        source_urls: list[str] = []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                dest = Path(tmp) / "audio.m4a"
                source_url = resolve_media_url(ep)
                source_urls = [source_url]
                if ep.sources:
                    source_urls.extend(src.ref for src in ep.sources if src.ref not in source_urls)
                if rate_limit_circuit is not None:
                    open_domain = rate_limit_circuit.open_for(source_urls)
                    if open_domain is not None:
                        with lock:
                            stats.skipped_budget += 1
                        print(
                            f"[enrich] audio encode skipped slug={city.slug} "
                            f"provider={city.provider} source={src_key} uid={label} "
                            f"guid={ep.guid}: provider throttle circuit open "
                            f"domain={open_domain}",
                            flush=True,
                        )
                        return
                # For single-source episodes, use a locally cached copy when available so the
                # encode pass reads from disk rather than re-streaming the rate-limited source.
                # Multi-source concat episodes use stable .ref URLs from the concat planner and
                # don't go through resolve_media_url, so skip the cache for them.
                if source_cache is not None and ep.uid and not (ep.sources and len(ep.sources) > 1):
                    local = source_cache.get_or_fetch(ep.uid, source_url)
                    by_id = _sources_by_id(ep, str(local) if local is not None else source_url)
                else:
                    by_id = _sources_by_id(ep, source_url)
                ffmpeg.extract_audio(
                    ep.timeline,
                    by_id,
                    dest,
                    ep.chapters or None,
                    loudness_profile=loudness_profile or None,
                )
                probed: float | None = None
                try:
                    probed = _probe_duration_secs(dest, ffmpeg_binary)
                    if probed is not None:
                        ep.audio_duration_served = probed
                except Exception:  # noqa: BLE001
                    pass
                try:
                    size = dest.stat().st_size
                except OSError:
                    size = 0
                # Don't host audio that's empty or implausibly shorter than the meeting (#39): a
                # throttled fetch yields a truncated stub that "encodes" fine (e.g. a 258-byte
                # Swagit container). Raise → #120 backoff + retry (now with the per-host cap).
                _guard_against_truncated_audio(ep, probed, size_bytes=size)
                url = storage.put_file(key, dest, CONTENT_TYPE)
                ep.audio_bytes = size
            if rate_limit_circuit is not None:
                rate_limit_circuit.record_success(source_urls)
            ep.audio_key = key
            ep.audio_spec_hash = spec
            ep.hosted_audio_url = url
            ep.audio_encode_time = now.isoformat()
            _backfill_served_duration(ep)
            ep.materialize_attempts = 0  # success clears the backoff state (#120)
            ep.materialize_last_attempt = None
            ep.materialize_error = None
            with lock:
                stats.bytes_written += size
                stats.hosted += 1
                stats.encoded += 1
            elapsed = time.perf_counter() - t0
            print(
                f"[enrich] audio encode done slug={city.slug} provider={city.provider} "
                f"source={src_key} uid={label} guid={ep.guid} "
                f"bytes={size} seconds={elapsed:.1f}",
                flush=True,
            )
        except (
            subprocess.TimeoutExpired,
            subprocess.CalledProcessError,
            OSError,
            ProviderError,
        ) as exc:
            if isinstance(exc, RateLimitedMediaFetchError) and rate_limit_circuit is not None:
                opened = rate_limit_circuit.record_rate_limited(source_urls)
                if opened is not None:
                    print(
                        f"[enrich] provider throttle circuit opened domain={opened}",
                        flush=True,
                    )
            # Record the failed attempt so this episode backs off (exponentially) instead of being
            # re-tried every run — otherwise a permanently-broken meeting churns budget/time (#120).
            # A timeout is transient (the source may recover), so like a generic ``error`` it backs
            # off + retries rather than counting as ``dead``; it gets its own code only so a stalled
            # source is distinguishable from other failures on the record.
            ep.materialize_attempts += 1
            ep.materialize_last_attempt = now.isoformat()
            ep.materialize_error = (
                "timeout"
                if isinstance(exc, subprocess.TimeoutExpired)
                else getattr(exc, "code", None) or "error"
            )
            with lock:
                stats.errors.append(f"{ep.uid or ep.guid}: {exc}")
            elapsed = time.perf_counter() - t0
            print(
                f"[enrich] audio encode error slug={city.slug} provider={city.provider} "
                f"source={src_key} uid={label} guid={ep.guid} "
                f"seconds={elapsed:.1f}: {exc}",
                flush=True,
            )
        finally:
            if gate_acquired and native_work_gate is not None:
                native_work_gate.release(kind="audio")
            if memory_reservation is not None and reserved_bytes:
                memory_reservation.release(reserved_bytes)

    if max_workers > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            list(pool.map(_encode_one, to_encode))
    else:
        for item in to_encode:
            _encode_one(item)

    return stats
