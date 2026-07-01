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
args as before. Non-identity timelines are rendered via an ffmpeg ``filter_complex``
that assembles ``atrim``/``concat``/insert segments. The production speech profile
streams that program through high-pass/dynamic leveling/compression into a measured
FLAC, then applies final linear EBU R128 normalization in a bounded-memory second pass.
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
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Protocol

from citypods.granicus_proxy import redact_worker_endpoint, worker_fallback_command
from citypods.http import HOST_LIMITER, USER_AGENT, StopRequested
from citypods.models import City, Episode
from citypods.progress import PROGRESS
from citypods.provider_leases import DISTRIBUTED_PROVIDER_LEASES
from citypods.provider_transport import ProviderTransportTelemetry
from citypods.providers.base import ProviderError
from citypods.records import (
    _in_backoff,
    audio_spec_hash,
    source_key,
)
from citypods.resources import (
    MemoryReservation,
    NativeWorkGate,
    ResourceSnapshot,
    current_snapshot,
    format_bytes,
)
from citypods.security import redact_subprocess_command, redact_subprocess_text
from citypods.storage.base import StorageBackend
from citypods.timeline import Segment, SourceMedia, Timeline, edl_duration, timeline_digest

CONTENT_TYPE = "audio/mp4"

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

# Production speech-mastering recipe. Keep the profile name/version in config and
# ``audio_spec_hash``: changing any value here requires a new profile id so existing objects are
# gradually re-encoded instead of silently claiming to match a different byte recipe.
PODCAST_SPEECH_PROFILE = "podcast-speech-v2"
_PODCAST_SPEECH_FILTERS = (
    "aresample=48000,aformat=channel_layouts=mono",
    "highpass=f=80",
    "dynaudnorm=f=500:g=21:p=0.80:m=6:r=0.08:t=0.015:o=0.5",
    "acompressor=threshold=0.125:ratio=2.5:attack=20:release=300:knee=4:makeup=1",
)
_LOUDNORM_LRA = 11.0
_LOUDNORM_TRUE_PEAK = -1.5
_LIMITER_CEILING_DB = -2.5
_TRUE_PEAK_SAMPLE_RATE = 192_000
_TIMELINE_SAMPLE_RATE = 48_000
_TIMELINE_FRAME_SAMPLES = 1024
_TIMELINE_BOUNDARY_GUARD_SECONDS = 0.05
_MIN_PROCESSABLE_AUDIO_SECONDS = 1.0


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
    redacted = redact_subprocess_text(stderr)
    text = (
        redacted.decode("utf-8", errors="replace") if isinstance(redacted, bytes) else str(redacted)
    )
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


class UnusableAudioError(ProviderError):
    """The planned audio contains no meaningful program material."""

    code = "dead"


class LoudnessMeasurementError(ProviderError):
    """The bounded speech-mastering pass could not produce usable EBU R128 measurements."""

    code = "loudness_measurement"


class RateLimitedMediaFetchError(ProviderError):
    """ffmpeg/ffprobe saw an HTTP throttling response (403/429) while reading provider media.

    Raised so the materialization caller records the normal per-episode backoff (#120); the episode
    retries next run. A Granicus 403 normally never reaches here — the direct-first fetch falls back
    to the Cloudflare Worker first.
    """

    code = "rate_limited"


def record_materialize_failure(
    ep: Episode,
    code: str,
    *,
    now: datetime | None = None,
) -> None:
    """Persist one direct materialization failure on an episode.

    Planner/source-cache fetches are part of materialization even when they fail before AudioStage.
    Recording them here makes the normal exponential backoff and stuck-download accounting apply
    without letting AudioStage immediately issue the same provider request again.
    """
    ep.materialize_attempts += 1
    ep.materialize_last_attempt = (now or datetime.now(UTC)).isoformat()
    ep.materialize_error = code


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
    """Raise :class:`RateLimitedMediaFetchError` if a failed ffmpeg/ffprobe run was provider
    throttling (HTTP 403/429), so the caller records the per-episode backoff (#120)."""
    status = _rate_limited_status(stderr)
    if status is None:
        return
    raise RateLimitedMediaFetchError(f"ffmpeg {phase} hit provider throttle ({status})")


def _redacted_process_error(
    exc: subprocess.CalledProcessError,
    command: list[str] | None = None,
) -> subprocess.CalledProcessError:
    """Keep media credentials out of higher-level exception strings and logs."""
    return subprocess.CalledProcessError(
        exc.returncode,
        redact_subprocess_command(command if command is not None else exc.cmd),
        output=redact_subprocess_text(exc.output),
        stderr=redact_subprocess_text(exc.stderr),
    )


def _redacted_timeout_error(
    exc: subprocess.TimeoutExpired,
    command: list[str] | None = None,
) -> subprocess.TimeoutExpired:
    return subprocess.TimeoutExpired(
        redact_subprocess_command(command if command is not None else exc.cmd),
        exc.timeout,
        output=redact_subprocess_text(exc.output),
        stderr=redact_subprocess_text(exc.stderr),
    )


_WORKER_FALLBACK_MISCONFIG_LOGGED = False


def _worker_fallback_for_403(
    *,
    command: list[str],
    stderr: bytes | str | None,
    rate_limit_urls: Sequence[str],
    log: Callable[[str], None] | None = None,
) -> list[str] | None:
    if _rate_limited_status(stderr) != "HTTP 403":
        return None
    try:
        return worker_fallback_command(command, tuple(rate_limit_urls))
    except ValueError:
        # A half-set/invalid GRANICUS_PROXY_* config must not convert an already-handled provider
        # 403 (which the per-episode backoff path knows how to absorb) into an uncaught error that
        # aborts the shard. Disable the fallback for this run and warn once. The message is kept
        # generic so a malformed endpoint value cannot leak into logs.
        global _WORKER_FALLBACK_MISCONFIG_LOGGED
        if not _WORKER_FALLBACK_MISCONFIG_LOGGED:
            _WORKER_FALLBACK_MISCONFIG_LOGGED = True
            _log_ffmpeg_event(
                log,
                "[enrich] granicus worker fallback disabled: set both GRANICUS_PROXY_BASE_URL "
                "and GRANICUS_PROXY_TOKEN to a valid HTTPS origin",
            )
        return None


def _record_worker_fallback_outcome(
    telemetry: ProviderTransportTelemetry | None,
    rate_limit_urls: Sequence[str],
    *,
    outcome: str,
) -> None:
    """Record one Worker fallback attempt + outcome on the per-tenant transport telemetry (#337)."""
    if telemetry is not None:
        telemetry.record_worker_fallback(rate_limit_urls, outcome=outcome)


def _record_direct_fetch_outcome(
    telemetry: ProviderTransportTelemetry | None,
    rate_limit_urls: Sequence[str],
    *,
    outcome: str,
) -> None:
    if telemetry is not None:
        telemetry.record_direct_fetch(rate_limit_urls, outcome=outcome)


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
    transport_telemetry: ProviderTransportTelemetry | None = None,
    stop: Callable[[], bool] | None = None,
    poll_seconds: float = _FFMPEG_GUARD_POLL_SECONDS,
    snapshot: Callable[[], ResourceSnapshot] = current_snapshot,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] | None = print,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    child_rss: Callable[[int], int | None] = _process_rss_bytes,
) -> tuple[bytes, bytes]:
    """Run ffmpeg with wall-clock and available-memory guardrails.

    ``subprocess.run(timeout=...)`` can only bound elapsed time. The Actions failure mode we are
    seeing is available memory collapsing while a child ffmpeg process is still active, so poll the
    whole-runner ``MemAvailable`` and terminate ffmpeg before the runner agent is killed.

    ``rate_limit_urls`` are the *remote* sources this invocation reads; the per-host concurrency cap
    (issue #39, :data:`citypods.http.HOST_LIMITER`) is held for the whole subprocess so a sharded
    burst of workers never opens more than the configured number of simultaneous connections to one
    provider tenant. Local-file inputs need not be passed (they resolve to no host → no-op).

    The process-local :data:`citypods.http.HOST_LIMITER` slot is acquired *before* the distributed
    lease (issue #342): a thread that's still queued behind this process's own local cap must not
    already be holding a cross-shard slot, or one early-starting process can win every distributed
    candidate while its other threads just wait locally, starving other shards of capacity they
    could otherwise use.

    ``stop``, if given, lets either wait yield with :class:`~citypods.http.StopRequested` once the
    run's wall-clock budget has expired, instead of waiting out a full queue/lease cycle.
    """
    if not memory_floor_bytes:
        _log_ffmpeg_event(log, f"[enrich] ffmpeg {phase} start")
        with (
            HOST_LIMITER.slots(rate_limit_urls, stop=stop),
            DISTRIBUTED_PROVIDER_LEASES.slots(rate_limit_urls, stop=stop),
        ):
            try:
                result = subprocess.run(cmd, check=True, capture_output=True, timeout=timeout)
            except subprocess.CalledProcessError as exc:
                if _rate_limited_status(exc.stderr) == "HTTP 403":
                    _record_direct_fetch_outcome(
                        transport_telemetry, rate_limit_urls, outcome="403"
                    )
                stderr = _stderr_tail(exc.stderr)
                detail = f" stderr={stderr}" if stderr else ""
                _log_ffmpeg_event(
                    log,
                    f"[enrich] ffmpeg {phase} error returncode={exc.returncode}{detail}",
                )
                fallback_cmd = _worker_fallback_for_403(
                    command=cmd,
                    stderr=exc.stderr,
                    rate_limit_urls=rate_limit_urls,
                    log=log,
                )
                if fallback_cmd is not None:
                    _log_ffmpeg_event(
                        log,
                        f"[enrich] granicus transport fallback phase={phase} "
                        "direct=HTTP403 strategy=cloudflare-worker",
                    )
                    worker_ok = False
                    try:
                        result = subprocess.run(
                            fallback_cmd,
                            check=True,
                            capture_output=True,
                            timeout=timeout,
                        )
                        worker_ok = True
                    except subprocess.TimeoutExpired as fallback_exc:
                        raise _redacted_timeout_error(fallback_exc, cmd) from fallback_exc
                    except subprocess.CalledProcessError as fallback_exc:
                        fallback_stderr = redact_worker_endpoint(
                            _stderr_tail(fallback_exc.stderr), fallback_cmd
                        )
                        fallback_detail = f" stderr={fallback_stderr}" if fallback_stderr else ""
                        _log_ffmpeg_event(
                            log,
                            f"[enrich] granicus transport fallback error phase={phase} "
                            f"strategy=cloudflare-worker returncode={fallback_exc.returncode}"
                            f"{fallback_detail}",
                        )
                        _raise_if_rate_limited(phase=phase, stderr=fallback_exc.stderr)
                        raise _redacted_process_error(fallback_exc, cmd) from fallback_exc
                    finally:
                        _record_worker_fallback_outcome(
                            transport_telemetry,
                            rate_limit_urls,
                            outcome="success" if worker_ok else "failure",
                        )
                    _log_ffmpeg_event(
                        log,
                        f"[enrich] granicus transport fallback done phase={phase} "
                        "strategy=cloudflare-worker",
                    )
                    _log_ffmpeg_event(log, f"[enrich] ffmpeg {phase} done")
                    return (
                        getattr(result, "stdout", b"") or b"",
                        getattr(result, "stderr", b"") or b"",
                    )
                _raise_if_rate_limited(phase=phase, stderr=exc.stderr)
                raise _redacted_process_error(exc, cmd) from exc
            except subprocess.TimeoutExpired as exc:
                stderr = _stderr_tail(exc.stderr)
                detail = f" stderr={stderr}" if stderr else ""
                _log_ffmpeg_event(log, f"[enrich] ffmpeg {phase} timeout seconds={timeout}{detail}")
                raise _redacted_timeout_error(exc, cmd) from exc
            else:
                _record_direct_fetch_outcome(
                    transport_telemetry, rate_limit_urls, outcome="success"
                )
        _log_ffmpeg_event(log, f"[enrich] ffmpeg {phase} done")
        return getattr(result, "stdout", b"") or b"", getattr(result, "stderr", b"") or b""

    # Memory-floor path: hold the per-host rate-limit slot (#39) for the whole monitored run so a
    # sharded burst can't open more than the configured number of simultaneous connections per host.
    with (
        HOST_LIMITER.slots(rate_limit_urls, stop=stop),
        DISTRIBUTED_PROVIDER_LEASES.slots(rate_limit_urls, stop=stop),
    ):
        try:
            result = _run_ffmpeg_popen_monitored(
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
                classify_rate_limit=False,
            )
            _record_direct_fetch_outcome(transport_telemetry, rate_limit_urls, outcome="success")
            return result
        except subprocess.CalledProcessError as exc:
            if _rate_limited_status(exc.stderr) == "HTTP 403":
                _record_direct_fetch_outcome(transport_telemetry, rate_limit_urls, outcome="403")
            fallback_cmd = _worker_fallback_for_403(
                command=cmd,
                stderr=exc.stderr,
                rate_limit_urls=rate_limit_urls,
                log=log,
            )
            if fallback_cmd is None:
                _raise_if_rate_limited(phase=phase, stderr=exc.stderr)
                raise _redacted_process_error(exc, cmd) from exc
            _log_ffmpeg_event(
                log,
                f"[enrich] granicus transport fallback phase={phase} "
                "direct=HTTP403 strategy=cloudflare-worker",
            )
            worker_ok = False
            try:
                result = _run_ffmpeg_popen_monitored(
                    fallback_cmd,
                    phase=f"{phase}-worker",
                    timeout=timeout,
                    memory_floor_bytes=memory_floor_bytes,
                    poll_seconds=poll_seconds,
                    snapshot=snapshot,
                    sleep=sleep,
                    log=log,
                    popen=popen,
                    child_rss=child_rss,
                    rate_limit_urls=rate_limit_urls,
                    transport_telemetry=transport_telemetry,
                )
                worker_ok = True
            except subprocess.TimeoutExpired as fallback_exc:
                raise _redacted_timeout_error(fallback_exc, cmd) from fallback_exc
            except subprocess.CalledProcessError as fallback_exc:
                raise _redacted_process_error(fallback_exc, cmd) from fallback_exc
            finally:
                # The worker run above uses classify_rate_limit=True, so a Worker 403/429 leaves as
                # RateLimitedMediaFetchError (not caught here); ``finally`` still records that as a
                # Worker-fallback failure in the transport telemetry exactly once.
                _record_worker_fallback_outcome(
                    transport_telemetry,
                    rate_limit_urls,
                    outcome="success" if worker_ok else "failure",
                )
            _log_ffmpeg_event(
                log,
                f"[enrich] granicus transport fallback done phase={phase} "
                "strategy=cloudflare-worker",
            )
            return result


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
    rate_limit_urls: Sequence[str] = (),
    transport_telemetry: ProviderTransportTelemetry | None = None,
    classify_rate_limit: bool = True,
) -> tuple[bytes, bytes]:
    """Popen + poll/sample loop for :func:`_run_ffmpeg_guarded` (memory-floor path). The caller
    holds the per-host rate-limit slot — and the distributed provider lease — for the whole
    monitored run."""
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
                stderr_text = redact_worker_endpoint(_stderr_tail(stderr), cmd)
                detail = f" stderr={stderr_text}" if stderr_text else ""
                _log_ffmpeg_event(
                    log,
                    f"[enrich] ffmpeg {phase} error pid={proc.pid} seconds={elapsed:.1f} "
                    f"returncode={returncode} peak_rss={_format_optional_bytes(peak_child_rss)} "
                    f"min_mem_avail={_format_optional_bytes(min_mem_available)} "
                    f"samples={samples}{detail}",
                )
                if classify_rate_limit:
                    _raise_if_rate_limited(phase=phase, stderr=stderr)
                raise subprocess.CalledProcessError(
                    returncode,
                    redact_subprocess_command(cmd),
                    output=redact_subprocess_text(stdout),
                    stderr=redact_subprocess_text(stderr),
                )
            _log_ffmpeg_event(
                log,
                f"[enrich] ffmpeg {phase} done pid={proc.pid} seconds={elapsed:.1f} "
                f"peak_rss={_format_optional_bytes(peak_child_rss)} "
                f"min_mem_avail={_format_optional_bytes(min_mem_available)} samples={samples}",
            )
            return stdout, stderr

        elapsed = time.monotonic() - started
        if timeout is not None and elapsed >= timeout:
            proc.terminate()
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
            stderr_text = redact_worker_endpoint(_stderr_tail(stderr), cmd)
            detail = f" stderr={stderr_text}" if stderr_text else ""
            _log_ffmpeg_event(
                log,
                f"[enrich] ffmpeg {phase} timeout pid={proc.pid} seconds={elapsed:.1f} "
                f"peak_rss={_format_optional_bytes(peak_child_rss)} "
                f"min_mem_avail={_format_optional_bytes(min_mem_available)} "
                f"samples={samples}{detail}",
            )
            raise subprocess.TimeoutExpired(
                redact_subprocess_command(cmd),
                timeout,
                output=redact_subprocess_text(stdout),
                stderr=redact_subprocess_text(stderr),
            )

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
                cmd=redact_subprocess_command(cmd),
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
    transport_telemetry: ProviderTransportTelemetry | None = None,
    stop: Callable[[], bool] | None = None,
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
            transport_telemetry=transport_telemetry,
            stop=stop,
            log=log,
        )
        return dest.exists() and dest.stat().st_size > 0
    except RateLimitedMediaFetchError:
        raise
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, OSError):
        return False


def _concat_local_sources(
    paths: list[Path],
    durations: list[float],
    dest: Path,
    ffmpeg_binary: str = "ffmpeg",
    timeout: float | None = None,
    memory_floor_bytes: int | None = None,
    stop: Callable[[], bool] | None = None,
) -> bool:
    """Decode and concatenate already-downloaded local segment files into one local file.

    Reuses :func:`build_filter_complex`'s N-input concat graph (the same one a live
    multi-source encode would otherwise build against remote URLs) so the result is the
    same audio, just rendered once from local disk instead of re-streamed from the provider
    on every encode attempt. All inputs are local, so there is no provider host to rate-limit.
    """
    segs: list[Segment] = []
    offset = 0.0
    for i, dur in enumerate(durations):
        segs.append(
            Segment(
                served_start=offset,
                served_end=offset + dur,
                kind="source",
                source_id=f"s{i}",
                source_start=0.0,
                source_end=dur,
            )
        )
        offset += dur
    source_input_idx = {f"s{i}": i for i in range(len(paths))}
    filter_str, out_label = build_filter_complex(tuple(segs), source_input_idx, {})

    inputs: list[str] = []
    for p in paths:
        inputs += ["-i", str(p)]
    cmd = [
        ffmpeg_binary,
        "-y",
        "-loglevel",
        "error",
        *inputs,
        "-filter_complex",
        filter_str,
        "-map",
        out_label,
        "-vn",
        "-c:a",
        "flac",
        "-f",
        "matroska",
        str(dest),
    ]
    try:
        _run_ffmpeg_guarded(
            cmd,
            phase="source-concat",
            timeout=timeout,
            memory_floor_bytes=memory_floor_bytes,
            stop=stop,
        )
        return dest.exists() and dest.stat().st_size > 0
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
        transport_telemetry: ProviderTransportTelemetry | None = None,
        stop: Callable[[], bool] | None = None,
    ):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="citypods_src_")
        self._paths: dict[str, Path] = {}
        self._locks: dict[str, threading.Lock] = collections.defaultdict(threading.Lock)
        self._guard = threading.Lock()
        self.ffmpeg_binary = ffmpeg_binary
        self.timeout_seconds = timeout_seconds
        self.memory_floor_bytes = memory_floor_bytes
        self.transport_telemetry = transport_telemetry
        # Shared run-budget predicate (set once for the whole run); lets a caller queued behind
        # another thread's fetch of the same uid yield once the budget expires instead of waiting
        # out that fetch's full timeout.
        self._stop = stop

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
        Raises :class:`~citypods.http.StopRequested` if the run's wall-clock budget expires while
        queued behind another thread's fetch of the same *uid*.
        """
        with self._guard:
            lock = self._locks[uid]
        if self._stop is None:
            lock.acquire()
        else:
            while not lock.acquire(timeout=1.0):
                if self._stop():
                    raise StopRequested(f"source cache wait for uid={uid!r} stopped")
        try:
            if uid in self._paths:
                return self._paths[uid]
            dest = Path(self._tmpdir.name) / f"{hashlib.md5(uid.encode()).hexdigest()}.mka"
            if _download_audio(
                url,
                dest,
                self.ffmpeg_binary,
                self.timeout_seconds,
                self.memory_floor_bytes,
                transport_telemetry=self.transport_telemetry,
                stop=self._stop,
            ):
                self._paths[uid] = dest
                return dest
            return None
        finally:
            lock.release()

    def get_or_fetch_concat(self, uid: str, sources: list[SourceMedia]) -> Path | None:
        """Download each of *sources* individually, then concatenate them into one cached
        local file keyed by *uid*.

        Each segment is fetched through :meth:`get_or_fetch` (keyed ``f"{uid}:{source.id}"``),
        which gives each segment its own bounded timeout and releases the rate-limit slot
        between segments — instead of holding one slot for an entire multi-input
        ``filter_complex`` subprocess (review/11 "Per-segment source caching for multi-source
        concat episodes"). Returns ``None`` (caller falls back to the live multi-input render)
        if any segment fails to download, a segment's duration is unknown, or the concat itself
        fails.
        """
        with self._guard:
            lock = self._locks[uid]
        if self._stop is None:
            lock.acquire()
        else:
            while not lock.acquire(timeout=1.0):
                if self._stop():
                    raise StopRequested(f"source cache wait for uid={uid!r} stopped")
        try:
            if uid in self._paths:
                return self._paths[uid]
            if any(src.duration is None for src in sources):
                return None
            local_paths: list[Path] = []
            for src in sources:
                local = self.get_or_fetch(f"{uid}:{src.id}", src.ref)
                if local is None:
                    return None
                local_paths.append(local)
            dest = Path(self._tmpdir.name) / f"{hashlib.md5(uid.encode()).hexdigest()}-concat.mka"
            durations = [src.duration for src in sources]
            if _concat_local_sources(
                local_paths,
                durations,  # type: ignore[arg-type]
                dest,
                self.ffmpeg_binary,
                self.timeout_seconds,
                self.memory_floor_bytes,
                stop=self._stop,
            ):
                self._paths[uid] = dest
                return dest
            return None
        finally:
            lock.release()


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
        sources: tuple[SourceMedia, ...] | list[SourceMedia] | None = None,
        loudness_profile: str | None = None,
        processing_profile: str | None = None,
        asset_resolver: Callable[[str, str | None], Path] | None = None,
    ) -> None:
        """Render ``timeline`` into ``dest`` (.m4a).

        Args:
            timeline: The episode's EDL, or ``None`` for the identity (full-copy) path.
            sources_by_id: Maps ``source_id`` → resolved playable URL.  For identity
                episodes this has exactly one entry; for concat it has N.
            dest: Output file path (will be created/overwritten).
            chapters: Served-time chapter markers embedded as M4A chapter atoms.
            sources: Optional ``SourceMedia`` registry for source-duration-aware identity
                classification. Real call sites should pass this whenever available.
            loudness_profile: e.g. ``"ebuR128:-16LUFS"``; ``None`` = no loudnorm.
            processing_profile: Named pre-mastering recipe, currently
                ``"podcast-speech-v2"`` for bounded multi-mic speech leveling.
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


@dataclass(frozen=True)
class LoudnessMeasurements:
    integrated: float
    threshold: float
    loudness_range: float
    true_peak: float


def _parse_ebur128_summary(stderr: bytes | str | None) -> LoudnessMeasurements:
    """Parse the final ``ebur128=peak=true`` summary emitted by ffmpeg.

    ``ebur128`` is a streaming meter, so it can measure arbitrarily long recordings without the
    memory growth observed in one-pass dynamic ``loudnorm``. The measured values feed the final
    linear loudnorm pass.
    """
    text = (
        stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else str(stderr or "")
    )
    summary = text.rsplit("Summary:", 1)[-1]
    patterns = {
        "integrated": r"Integrated loudness:\s*I:\s*(-?(?:\d+(?:\.\d+)?|inf))\s+LUFS",
        "threshold": r"Integrated loudness:.*?Threshold:\s*(-?(?:\d+(?:\.\d+)?|inf))\s+LUFS",
        "loudness_range": r"Loudness range:\s*LRA:\s*(-?(?:\d+(?:\.\d+)?|inf))\s+LU",
        "true_peak": r"True peak:\s*Peak:\s*(-?(?:\d+(?:\.\d+)?|inf))\s+dBFS",
    }
    values: dict[str, float] = {}
    for name, pattern in patterns.items():
        match = re.search(pattern, summary, re.DOTALL)
        if match is None:
            raise LoudnessMeasurementError(f"missing {name} in ebur128 summary")
        try:
            values[name] = float(match.group(1))
        except ValueError as exc:
            raise LoudnessMeasurementError(f"non-finite {name} in ebur128 summary") from exc
    if any(value != value or value in (float("inf"), float("-inf")) for value in values.values()):
        raise UnusableAudioError("audio has no measurable program loudness")
    return LoudnessMeasurements(**values)


def _linear_loudnorm_filter(profile: str, measured: LoudnessMeasurements) -> str:
    """Build a measured linear loudnorm filter.

    The speech profile's compressor leaves peak headroom so linear normalization should normally
    be feasible. Refuse to let ffmpeg silently fall back to dynamic mode when it is not: that
    fallback is the length-proportional memory path this flow is designed to eliminate.
    """
    target_i = float(_parse_lufs(profile))
    required_gain = target_i - measured.integrated
    # EBU linear mode requires target LRA >= measured LRA. ``dynaudnorm`` owns local speaker
    # leveling; final loudnorm must not silently switch back to the memory-heavy dynamic mode just
    # to squeeze an unusually broad recording into 11 LU.
    target_lra = max(_LOUDNORM_LRA, measured.loudness_range)
    if measured.true_peak + required_gain > _LOUDNORM_TRUE_PEAK + 0.1:
        raise LoudnessMeasurementError(
            "linear loudnorm lacks peak headroom "
            f"({measured.true_peak + required_gain:.2f} dBTP predicted)"
        )
    return (
        f"loudnorm=I={target_i:g}:TP={_LOUDNORM_TRUE_PEAK:g}:LRA={target_lra:g}:"
        f"measured_I={measured.integrated:g}:measured_LRA={measured.loudness_range:g}:"
        f"measured_TP={measured.true_peak:g}:measured_thresh={measured.threshold:g}:"
        "offset=0:linear=true"
    )


def _peak_limited_linear_filter(profile: str, measured: LoudnessMeasurements) -> str:
    """Build the bounded fallback for peak-constrained linear normalization.

    The loudness gain remains one constant multiplier. A short-lookahead limiter follows it only
    to catch peaks that would otherwise exceed the true-peak ceiling. Running the limiter at
    192 kHz approximates true-peak detection without the whole-recording state of dynamic
    ``loudnorm``; its memory is bounded by the resampler and millisecond lookahead buffers.

    Peak limiting can leave unusually high-crest-factor material a little quieter than the
    integrated target. That is preferable to clipping, dynamic-loudnorm memory growth, or dropping
    the episode entirely.
    """
    target_i = float(_parse_lufs(profile))
    required_gain = target_i - measured.integrated
    # Leave 1 dB beyond the -1.5 dBTP program target for AAC reconstruction overshoot.
    limit = 10 ** (_LIMITER_CEILING_DB / 20.0)
    return (
        f"volume={required_gain:g}dB:precision=double,"
        f"aresample={_TRUE_PEAK_SAMPLE_RATE},"
        f"alimiter=limit={limit:.9f}:level=false:latency=true,"
        "aresample=48000"
    )


def _append_audio_filters(
    filtergraph: str, output_label: str, filters: Sequence[str]
) -> tuple[str, str]:
    """Append a serial filter chain to an existing labeled filtergraph."""
    parts = [filtergraph] if filtergraph else []
    current = output_label
    for index, audio_filter in enumerate(filters):
        label = f"post{index}"
        parts.append(f"{current}{audio_filter}[{label}]")
        current = f"[{label}]"
    return ";".join(parts), current


def _build_streaming_single_source_filter(
    segments: tuple[Segment, ...],
    source_input_idx: dict[str, int],
) -> tuple[str, str] | None:
    """Build a sample-accurate bounded-memory filter for monotonic cuts from one source.

    The generic trim graph fans one input into N parallel ``atrim`` branches and concatenates them.
    FFmpeg can retain decoded frames for branches whose source span starts far in the future, making
    RSS grow with recording duration and cut count. Silence timelines are simpler: one source,
    monotonic non-overlapping keep spans, no inserts or reordering.

    ``aselect`` normally keeps or rejects whole decoder frames, which accumulated visible duration
    drift across many cuts. This graph first fixes the stream at 48 kHz and rewrites source PTS to
    the contiguous decoded-sample clock used by ``SilencePlanner``. It then changes the frame size
    to one sample only in short windows around each cut boundary. The selector compares integer
    sample PTS values, a post-select ``asetpts`` packs retained samples onto the served clock, and a
    final ``asetnsamples`` coalesces them back into normal frames. Work and memory outside the
    boundary windows remain independent of meeting duration and cut count.

    Return ``None`` for timelines that need the generic graph (multi-source concat, inserts,
    reordering, open-ended non-final spans).
    """
    if not segments or any(segment.kind != "source" for segment in segments):
        return None
    source_ids = {segment.source_id for segment in segments}
    if len(source_ids) != 1 or None in source_ids:
        return None
    source_id = next(iter(source_ids))
    previous_end_sample = -1
    terms: list[str] = []
    boundaries: set[int] = set()
    for index, segment in enumerate(segments):
        start = segment.source_start
        end = segment.source_end
        if start is None:
            return None
        start_sample = round(start * _TIMELINE_SAMPLE_RATE)
        if start_sample < previous_end_sample:
            return None
        boundaries.add(start_sample)
        if end is None:
            if index != len(segments) - 1:
                return None
            terms.append(f"gte(pts\\,{start_sample})")
            previous_end_sample = start_sample
        else:
            end_sample = round(end * _TIMELINE_SAMPLE_RATE)
            if end_sample <= start_sample:
                return None
            terms.append(f"gte(pts\\,{start_sample})*lt(pts\\,{end_sample})")
            boundaries.add(end_sample)
            previous_end_sample = end_sample

    # ``asendcmd`` sees the fixed 1024-sample frames immediately upstream. Enter fine framing far
    # enough before each boundary that the next command-check frame still precedes it, and merge
    # overlapping windows so nearby cuts do not thrash the runtime option.
    guard_samples = round(_TIMELINE_BOUNDARY_GUARD_SECONDS * _TIMELINE_SAMPLE_RATE)
    windows: list[list[int]] = []
    for boundary in sorted(boundaries):
        start = max(0, boundary - guard_samples)
        end = boundary + guard_samples
        if windows and start <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], end)
        else:
            windows.append([start, end])
    commands = ";".join(
        command
        for start, end in windows
        for command in (
            f"{start / _TIMELINE_SAMPLE_RATE:.6f} asetnsamples@timeline_cut_frames n 1",
            f"{end / _TIMELINE_SAMPLE_RATE:.6f} "
            f"asetnsamples@timeline_cut_frames n {_TIMELINE_FRAME_SAMPLES}",
        )
    )
    input_idx = source_input_idx[source_id]
    expression = "+".join(terms)
    return (
        f"[{input_idx}:a]"
        f"aresample={_TIMELINE_SAMPLE_RATE},"
        "aformat=channel_layouts=mono,"
        "asetpts=N/SR/TB,"
        f"asetnsamples=n={_TIMELINE_FRAME_SAMPLES}:p=0,"
        f"asettb=1/{_TIMELINE_SAMPLE_RATE},"
        f"asendcmd=c='{commands}',"
        f"asetnsamples@timeline_cut_frames=n={_TIMELINE_FRAME_SAMPLES}:p=0,"
        f"aselect='{expression}',"
        "asetpts=N/SR/TB,"
        f"asetnsamples=n={_TIMELINE_FRAME_SAMPLES}:p=0[program]",
        "[program]",
    )


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


def _is_single_source_fanout(segments: tuple[Segment, ...]) -> bool:
    """True for the OOM-prone shape: a single-source, all-source, multi-cut timeline.

    This is exactly what :func:`_build_streaming_single_source_filter` renders in bounded memory.
    The generic :func:`build_filter_complex` fans it into N parallel ``atrim`` branches whose
    retained decoded frames make RSS grow with cut count — the OOM that motivated the streaming
    graph (GH#702). The generic graph is legitimate only for multi-source concat and insert
    timelines, so the render dispatch treats this shape reaching the generic graph as a regression.
    """
    if len(segments) <= 1:
        return False
    if any(s.kind != "source" for s in segments):
        return False  # inserts (intro/outro) legitimately use the generic graph
    if any(s.source_end is None for s in segments[:-1]):
        return False  # a non-final open-ended span is a documented generic-path case
    return len({s.source_id for s in segments}) == 1


class StreamingFilterBypassedError(AssertionError):
    """A single-source many-cut timeline reached the generic fan-out graph (GH#702 OOM guard)."""


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
        phase_gate: NativeWorkGate | None = None,
        finalize_workers: int = 0,
        transport_telemetry: ProviderTransportTelemetry | None = None,
        stop: Callable[[], bool] | None = None,
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
        self.phase_gate = phase_gate
        self.transport_telemetry = transport_telemetry
        self.manages_audio_phases = phase_gate is not None
        # Shared run-budget predicate; used for the pre-subprocess coordination waits (phase-gate
        # slot, per-host rate limit, distributed lease) so a queued encode yields once the run's
        # wall-clock budget expires instead of waiting out the queue. The ffmpeg subprocess call
        # itself stays bounded only by ``timeout_seconds`` (stop() can't preempt subprocess.run).
        self._stop = stop
        self._finalize_executor = (
            ThreadPoolExecutor(
                max_workers=max(1, int(finalize_workers)),
                thread_name_prefix="audio-finalize",
            )
            if finalize_workers > 0
            else None
        )

    def close(self) -> None:
        """Drain and stop the optional local-finalization executor."""
        if self._finalize_executor is not None:
            self._finalize_executor.shutdown(wait=True)
            self._finalize_executor = None

    def _run_audio_phase(
        self,
        *,
        kind: str,
        label: str,
        stop: Callable[[], bool] | None,
        run: Callable[[], tuple[bytes, bytes]],
    ) -> tuple[bytes, bytes]:
        acquired = False
        if self.phase_gate is not None:
            acquired = self.phase_gate.acquire(kind=kind, label=label, stop=stop)
            if not acquired:
                # acquire() only returns False because ``stop`` fired while queued for a gate
                # slot — not a subprocess hang, so this must not surface as a 2700s ffmpeg
                # timeout. StopRequested routes it to the same skipped_budget / retry-next-run
                # path as the other coordination waits (host rate limit, distributed lease,
                # source cache).
                raise StopRequested(f"native work gate wait for {label!r} stopped")
        try:
            return run()
        finally:
            if acquired and self.phase_gate is not None:
                self.phase_gate.release(kind=kind)

    def _submit_finalize(self, run: Callable[[], tuple[bytes, bytes]]) -> Future | None:
        if self._finalize_executor is None:
            return None
        return self._finalize_executor.submit(run)

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
        sources: tuple[SourceMedia, ...] | list[SourceMedia] | None = None,
        loudness_profile: str | None = None,
        processing_profile: str | None = None,
        asset_resolver: Callable[[str, str | None], Path] | None = None,
    ) -> None:
        use_filter = (
            timeline is not None and timeline_digest(timeline, tuple(sources or ())) != ""
        ) or bool(loudness_profile or processing_profile)

        if use_filter:
            self._render_filter(
                timeline,
                sources_by_id,
                dest,
                chapters=chapters,
                loudness_profile=loudness_profile,
                processing_profile=processing_profile,
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
        stream_info = _probe_audio_stream(
            source_url,
            self.binary,
            timeout=probe_timeout,
            transport_telemetry=self.transport_telemetry,
            stop=self._stop,
        )
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
            self._run_audio_phase(
                kind="audio",
                label="identity-render",
                stop=self._stop,
                run=lambda: _run_ffmpeg_guarded(
                    cmd,
                    phase="identity-render",
                    timeout=self.timeout_seconds,
                    memory_floor_bytes=self.memory_floor_bytes,
                    rate_limit_urls=(source_url,),  # no-op if it's a local cached copy (#39)
                    transport_telemetry=self.transport_telemetry,
                    stop=self._stop,
                ),
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
        processing_profile: str | None = None,
        asset_resolver: Callable[[str, str | None], Path] | None = None,
    ) -> None:
        segs: tuple[Segment, ...] = timeline.segments if timeline is not None else ()
        if processing_profile and processing_profile != PODCAST_SPEECH_PROFILE:
            raise ValueError(f"unknown audio processing profile: {processing_profile}")
        if processing_profile and segs:
            served = sum(segment.served_end - segment.served_start for segment in segs)
            if 0 < served < _MIN_PROCESSABLE_AUDIO_SECONDS:
                raise UnusableAudioError(
                    f"planned audio is only {served:.3f}s after timeline edits"
                )

        # --- collect ordered source inputs (in order of first appearance) ---
        source_ids: list[str] = []
        for seg in segs:
            if seg.kind == "source" and seg.source_id not in source_ids:
                source_ids.append(seg.source_id)  # type: ignore[arg-type]
        if not source_ids:
            source_ids = [next(iter(sources_by_id))]

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

            if segs:
                # Always try the bounded-memory single-source graph first, regardless of profile
                # (GH#702): it — not the generic fan-out — must own the OOM-prone single-source
                # many-cut shape. It returns None for multi-source concat / inserts / reordering,
                # which the generic graph still handles.
                streaming_filter = _build_streaming_single_source_filter(segs, source_input_idx)
                if streaming_filter is not None:
                    filter_str, out_label = streaming_filter
                    # The speech profile appends its measured loudnorm in _render_speech_profile.
                    # On the legacy (no-profile) path loudnorm is otherwise baked into
                    # build_filter_complex, so append it to the streaming output here to keep parity
                    # now that single-source always takes the streaming graph.
                    if not processing_profile and loudness_profile:
                        lufs = _parse_lufs(loudness_profile)
                        filter_str = (
                            f"{filter_str};{out_label}loudnorm=I={lufs}:TP=-1.5:LRA=11[outa]"
                        )
                        out_label = "[outa]"
                else:
                    if _is_single_source_fanout(segs):
                        raise StreamingFilterBypassedError(
                            "single-source many-cut timeline reached build_filter_complex; it must "
                            "render via _build_streaming_single_source_filter (GH#702 OOM guard)"
                        )
                    filter_str, out_label = build_filter_complex(
                        segs,
                        source_input_idx,
                        asset_input_idx,
                        None if processing_profile else loudness_profile,
                    )
            else:
                filter_str, out_label = "[0:a]anull[program]", "[program]"

            if processing_profile:
                self._render_speech_profile(
                    inputs=inputs,
                    base_filter=filter_str,
                    base_output=out_label,
                    dest=dest,
                    tmp=Path(tmp),
                    chapters=chapters,
                    loudness_profile=loudness_profile,
                    rate_limit_urls=tuple(sources_by_id[sid] for sid in source_ids),
                )
                return

            # Chapter metadata (served-time; comes in as a separate input)
            chapter_args: list[str] = []
            if chapters:
                meta = Path(tmp) / "chapters.ffmeta"
                meta.write_text(_ffmetadata(chapters))
                inputs += ["-i", str(meta)]
                chapter_args = ["-map_chapters", str(next_idx)]

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
            self._run_audio_phase(
                kind="audio",
                label="filter-render",
                stop=self._stop,
                run=lambda: _run_ffmpeg_guarded(
                    cmd,
                    phase="filter-render",
                    timeout=self.timeout_seconds,
                    memory_floor_bytes=self.memory_floor_bytes,
                    # Cap concurrent hits per provider for any remote source inputs (#39); local
                    # cached copies / insert assets resolve to no host → no-op.
                    rate_limit_urls=tuple(sources_by_id[sid] for sid in source_ids),
                    transport_telemetry=self.transport_telemetry,
                    stop=self._stop,
                ),
            )

    def _render_speech_profile(
        self,
        *,
        inputs: list[str],
        base_filter: str,
        base_output: str,
        dest: Path,
        tmp: Path,
        chapters: list[dict] | None,
        loudness_profile: str | None,
        rate_limit_urls: tuple[str, ...],
    ) -> None:
        """Bounded-memory speech mastering.

        Pass 1 applies the timeline and local speech leveling once, writes a local lossless FLAC,
        and measures that exact mono signal with streaming ``ebur128``. Pass 2 applies measured
        *linear* loudnorm and AAC encoding. Provider media is read only in pass 1.
        """
        started = time.monotonic()

        def _remaining_timeout() -> float | None:
            if self.timeout_seconds is None:
                return None
            remaining = self.timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise subprocess.TimeoutExpired(["ffmpeg", "speech-master"], self.timeout_seconds)
            return remaining

        measured_audio = tmp / "speech-leveled.flac"
        measure_graph, measure_out = _append_audio_filters(
            base_filter,
            base_output,
            (*_PODCAST_SPEECH_FILTERS, "ebur128=framelog=quiet:peak=true"),
        )
        measure_cmd = [
            self.binary,
            "-y",
            "-nostats",
            "-loglevel",
            "info",
            "-protocol_whitelist",
            "file,crypto,data,http,https,tcp,tls",
            *inputs,
            *self._filter_thread_args(),
            "-filter_complex",
            measure_graph,
            "-map",
            measure_out,
            "-vn",
            "-c:a",
            "flac",
            *self._thread_args(["-c:a", "flac"]),
            str(measured_audio),
        ]
        _, measure_stderr = self._run_audio_phase(
            kind="audio",
            label="speech-measure",
            stop=self._stop,
            run=lambda: _run_ffmpeg_guarded(
                measure_cmd,
                phase="speech-measure",
                timeout=_remaining_timeout(),
                memory_floor_bytes=self.memory_floor_bytes,
                rate_limit_urls=rate_limit_urls,
                transport_telemetry=self.transport_telemetry,
                stop=self._stop,
            ),
        )
        measured = _parse_ebur128_summary(measure_stderr)

        final_phase = "loudness-render"
        if loudness_profile:
            try:
                final_filter = _linear_loudnorm_filter(loudness_profile, measured)
            except LoudnessMeasurementError:
                final_filter = _peak_limited_linear_filter(loudness_profile, measured)
                final_phase = "loudness-limit-render"
        else:
            final_filter = "anull"
        final_inputs = ["-i", str(measured_audio)]
        chapter_args: list[str] = []
        if chapters:
            meta = tmp / "chapters.ffmeta"
            meta.write_text(_ffmetadata(chapters))
            final_inputs += ["-i", str(meta)]
            chapter_args = ["-map_chapters", "1"]
        final_cmd = [
            self.binary,
            "-y",
            "-nostats",
            "-loglevel",
            "error",
            *final_inputs,
            *self._filter_thread_args(),
            "-filter_complex",
            f"[0:a]{final_filter}[outa]",
            "-map",
            "[outa]",
            "-vn",
            *chapter_args,
            "-c:a",
            "aac",
            "-b:a",
            f"{self.max_kbps}k",
            "-ac",
            "1",
            "-ar",
            "48000",
            *self._thread_args(["-c:a", "aac"]),
            "-movflags",
            "+faststart",
            str(dest),
        ]

        def _finalize() -> tuple[bytes, bytes]:
            return self._run_audio_phase(
                kind="audio-finalize",
                label=final_phase,
                stop=self._stop,
                run=lambda: _run_ffmpeg_guarded(
                    final_cmd,
                    phase=final_phase,
                    timeout=_remaining_timeout(),
                    memory_floor_bytes=self.memory_floor_bytes,
                ),
            )

        future = self._submit_finalize(_finalize)
        if future is None:
            _finalize()
        else:
            future.result()


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
    url: str,
    ffmpeg_binary: str = "ffmpeg",
    timeout: float | None = None,
    transport_telemetry: ProviderTransportTelemetry | None = None,
    stop: Callable[[], bool] | None = None,
) -> AudioStreamInfo:
    """Return source audio codec/bitrate via ffprobe, or unknown fields on failure."""
    # Replace only the trailing path component so a parent dir named "ffmpeg" is preserved.
    ffprobe = "ffprobe".join(ffmpeg_binary.rsplit("ffmpeg", 1))
    try:
        with (
            HOST_LIMITER.slot(url, stop=stop),
            DISTRIBUTED_PROVIDER_LEASES.slots([url], stop=stop),
        ):
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
    # Replace only the trailing path component so a parent dir named "ffmpeg" is preserved.
    ffprobe = "ffprobe".join(ffmpeg_binary.rsplit("ffmpeg", 1))
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


@dataclass(frozen=True)
class AudioDurationProbe:
    """Cheap duration views from ffprobe.

    ``container_duration`` is ``format.duration``: the whole container's advertised duration.
    ``stream_sample_duration`` prefers the first audio stream's ``duration_ts * time_base``, which
    is the endpoint in the stream's sample clock after container edit-list semantics. It is not a
    substitute for decoding PCM when a format omits stream timing, but it avoids a full decode for
    the common M4A/Matroska cases the audit needs to classify first.
    """

    container_duration: float | None = None
    stream_sample_duration: float | None = None
    stream_duration_source: str | None = None
    probe_error: str | None = None


def _parse_positive_float(raw: object) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _duration_from_stream(stream: dict) -> tuple[float | None, str | None]:
    duration_ts = stream.get("duration_ts")
    time_base = stream.get("time_base")
    try:
        if duration_ts is not None and time_base:
            duration = int(duration_ts) * Fraction(str(time_base))
            if duration > 0:
                return float(duration), "stream-duration-ts"
    except (TypeError, ValueError, ZeroDivisionError):
        pass

    duration = _parse_positive_float(stream.get("duration"))
    if duration is not None:
        return duration, "stream-duration"
    return None, None


def _probe_audio_duration_details(path: Path, ffmpeg_binary: str = "ffmpeg") -> AudioDurationProbe:
    """Read container and stream-clock duration without decoding the whole file.

    This is the low-cost first pass for timeline/audio integrity checks. A later audit can fall
    back to bounded PCM decoding only when stream timing is unavailable or contradicts other
    evidence.
    """
    # Replace only the trailing path component so a parent dir named "ffmpeg" is preserved.
    ffprobe = "ffprobe".join(ffmpeg_binary.rsplit("ffmpeg", 1))
    try:
        out = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "format=duration:stream=duration_ts,time_base,duration",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10.0,
        ).stdout
        data = json.loads(out or "{}")
    except (
        subprocess.TimeoutExpired,
        subprocess.CalledProcessError,
        OSError,
        ValueError,
        TypeError,
    ):
        return AudioDurationProbe(probe_error="ffprobe-error")

    container = _parse_positive_float((data.get("format") or {}).get("duration"))
    streams = data.get("streams") or []
    stream_duration = None
    stream_source = None
    if streams and isinstance(streams[0], dict):
        stream_duration, stream_source = _duration_from_stream(streams[0])
    probe_error = None
    if container is None and stream_duration is None:
        probe_error = "no-duration-metadata"
    return AudioDurationProbe(
        container_duration=container,
        stream_sample_duration=stream_duration,
        stream_duration_source=stream_source,
        probe_error=probe_error,
    )


# ---------------------------------------------------------------------------
# Header-only duration probe (range reads, no full download)
# ---------------------------------------------------------------------------
#
# Every hosted episode is a single-moov MP4/M4A written by this project's own ffmpeg finalize
# pass with ``-movflags +faststart`` (moov before mdat) — see the encode call sites in this
# module. ``format.duration`` and the stream's ``duration_ts``/``time_base`` (the exact fields
# ``_probe_audio_duration_details`` reads) live entirely in ``moov``; ffprobe never touches
# ``mdat`` to answer that query, whether it's given the whole file or just the header. So
# fetching only ``ftyp``+``moov`` via range reads yields bit-identical values to a full
# download, at a fraction of the bytes (moov is typically well under 1% of file size). See
# review's timeline-audio-integrity notes for the empirical check that backs this claim, and
# ``check_timeline_integrity``'s ``probe_audio_full`` reconciliation for the ongoing guard.

# First range read when locating `moov`. Comfortably covers `ftyp`+`moov` for most episodes in
# one round trip; `moov` grows with episode length (its per-frame stsz/stco tables dominate), so
# the longest meetings need a second, exactly-sized read (see ``_fetch_mp4_header``).
_MP4_INITIAL_RANGE_BYTES = 65536

# Sanity cap on a claimed `moov` size before trusting it enough to issue the second range read.
# Guards against parsing garbage as a box header (a non-MP4 or corrupt object) and turning that
# into an unbounded fetch — if the declared size is implausible, fall back to a full download.
_MP4_MAX_MOOV_BYTES = 32 * 1024 * 1024


def _mp4_moov_extent(buf: bytes) -> tuple[int, int] | None:
    """Scan top-level MP4 box headers in a *prefix* of a file to find ``moov``'s exact
    ``[start, end)`` byte extent.

    Returns ``None`` when ``moov``'s header isn't present in ``buf`` yet (need a larger initial
    read), or a box with an unresolvable size (0-sized "extends to EOF", or malformed) is hit
    before ``moov`` is found — e.g. ``mdat`` appears first, meaning the object was not written
    moov-before-mdat and this fast path does not apply.
    """
    pos = 0
    n = len(buf)
    while pos + 8 <= n:
        size = int.from_bytes(buf[pos : pos + 4], "big")
        box_type = buf[pos + 4 : pos + 8]
        header_len = 8
        if size == 1:
            if pos + 16 > n:
                return None  # 64-bit largesize field not fully read yet
            size = int.from_bytes(buf[pos + 8 : pos + 16], "big")
            header_len = 16
        if box_type == b"moov":
            if size < header_len:
                return None
            return pos, pos + size
        if size == 0 or size < header_len:
            return None  # box extends to EOF, or malformed — not resolvable from a prefix
        pos += size
    return None


def _fetch_mp4_header(get_range: Callable[[int, int], bytes | None]) -> bytes | None:
    """Fetch just the ``ftyp``+``moov`` bytes of an MP4/M4A object via ``get_range(start, end)``
    (inclusive byte offsets, HTTP Range semantics), or ``None`` if that isn't possible.

    At most two range reads: an initial chunk to locate ``moov``'s header, and — only if
    ``moov`` extends past that chunk — one more sized exactly to its declared length. Never
    reads ``mdat``.
    """
    buf = get_range(0, _MP4_INITIAL_RANGE_BYTES - 1)
    if not buf:
        return None
    extent = _mp4_moov_extent(buf)
    if extent is None:
        return None
    start, end = extent
    if end <= len(buf):
        return buf[:end]
    if end - start > _MP4_MAX_MOOV_BYTES:
        return None
    rest = get_range(len(buf), end - 1)
    if not rest:
        return None
    return buf + rest


def _probe_audio_duration_header(
    get_range: Callable[[int, int], bytes | None],
    ffmpeg_binary: str = "ffmpeg",
) -> AudioDurationProbe | None:
    """Header-only variant of :func:`_probe_audio_duration_details`: fetches only the
    ``ftyp``/``moov`` boxes (via range reads, see :func:`_fetch_mp4_header`) instead of
    downloading the whole object, then runs the identical ffprobe query against just those
    bytes.

    Returns ``None`` (not an error probe) when the header can't be isolated this way — e.g. the
    object isn't moov-before-mdat, or a range read failed — so the caller falls back to a full
    download + :func:`_probe_audio_duration_details` instead of reporting a false result.
    """
    header = _fetch_mp4_header(get_range)
    if header is None:
        return None
    with tempfile.TemporaryDirectory() as t:
        dest = Path(t) / "header.m4a"
        dest.write_bytes(header)
        return _probe_audio_duration_details(dest, ffmpeg_binary=ffmpeg_binary)


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
    rate_limited: int = 0  # encode attempts that hit HTTP 403 / provider throttle (GH#300)
    defer_reasons: dict[str, int] = field(default_factory=dict)
    defer_samples: list[str] = field(default_factory=list)

    def defer(
        self,
        reason: str,
        *,
        backoff: bool = False,
        sample: str | None = None,
    ) -> None:
        """Record restartable audio work left for a later run."""
        if backoff:
            self.skipped_backoff += 1
        else:
            self.skipped_budget += 1
        self.defer_reasons[reason] = self.defer_reasons.get(reason, 0) + 1
        if sample and len(self.defer_samples) < 5:
            self.defer_samples.append(sample)


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


class HostedKeysCache:
    """Thread-safe per-pass cache of :func:`_hosted_keys`, keyed by source.

    The H5 PR3 global queue dispatches ``AudioStage`` once per *episode* rather than once
    per source, so that an episode from any source can be processed in true
    newest-everywhere-first priority order. Without this cache, ``materialize_audio()``
    would call ``storage.list_objects()`` on the same source prefix once per episode —
    thousands of redundant listings for a single source, which is what stretched a
    stopped shard's drain to tens of minutes (issue #344). Each source's listing is
    fetched at most once per cache (i.e. once per global pass) and shared across however
    many worker threads end up processing that source's episodes concurrently.
    """

    def __init__(self) -> None:
        self._keys: dict[str, set[str] | None] = {}
        self._locks: dict[str, threading.Lock] = collections.defaultdict(threading.Lock)
        self._guard = threading.Lock()

    def get(self, city: City, storage: StorageBackend) -> set[str] | None:
        key = source_key(city)
        with self._guard:
            lock = self._locks[key]
        with lock:
            if key not in self._keys:
                self._keys[key] = _hosted_keys(city, storage)
            return self._keys[key]


@dataclass(frozen=True)
class AudioArtifact:
    """Successful audio result shared by duplicate stable-meeting source views."""

    key: str
    spec: str
    url: str
    duration: float | None
    size: int | None
    encoded_at: str | None


class AudioArtifactCache:
    """Thread-safe run-local coalescing for identical stable-uid + audio-recipe work."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._canonical_sources: dict[tuple[str, str], str] = {}
        self._values: dict[tuple[str, str, str], AudioArtifact] = {}
        self._inflight: set[tuple[str, str, str]] = set()

    def register(self, provider: str, source: str, uid: str) -> None:
        with self._condition:
            identity = (provider, uid)
            current = self._canonical_sources.get(identity)
            if current is None or source < current:
                self._canonical_sources[identity] = source

    def canonical_source(self, provider: str, source: str, uid: str) -> str:
        with self._condition:
            return self._canonical_sources.get((provider, uid), source)

    def claim(self, key: tuple[str, str, str]) -> tuple[bool, AudioArtifact | None]:
        with self._condition:
            while key in self._inflight:
                self._condition.wait()
            value = self._values.get(key)
            if value is not None:
                return False, value
            self._inflight.add(key)
            return True, None

    def complete(self, key: tuple[str, str, str], value: AudioArtifact) -> None:
        with self._condition:
            self._values[key] = value
            self._inflight.discard(key)
            self._condition.notify_all()

    def abort(self, key: tuple[str, str, str]) -> None:
        with self._condition:
            self._inflight.discard(key)
            self._condition.notify_all()


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


def _concat_render_timeline(
    sources: list[SourceMedia], combined: Path
) -> tuple[Timeline, dict[str, str]]:
    """A synthesized single-segment Timeline + ``by_id`` pointing at *combined*.

    Spans the same total served duration as the persisted multi-segment EDL (the same
    sources in the same order), but shaped as one monotonic single-source segment that's
    exactly full-span (``served_*`` == ``source_*``) — ``timeline_digest`` treats that as the
    identity case (see ``timeline.py::_is_identity``), so with no loudness/processing profile
    configured the encoder takes the cheaper ``_render_identity`` copy path; with a profile set
    (production always sets ``audio_processing_profile``) it takes ``_render_filter`` ->
    ``_build_streaming_single_source_filter`` instead of the legacy multi-input
    ``filter_complex`` fan-out. Either way is correct here: there are no cuts left to apply once
    the segments are already concatenated, so identity-copy and the streaming path produce the
    same audio. Render-time only — never persisted onto the episode record; ``ep.timeline`` keeps
    the real per-segment EDL the concat planner built (clips/soundbites still need the original
    per-segment source URLs to extract a single clip without downloading the whole meeting).
    """
    total = sum(src.duration or 0.0 for src in sources)
    timeline = Timeline(
        version="",
        segments=(
            Segment(
                served_start=0.0,
                served_end=total,
                kind="source",
                source_id="combined",
                source_start=0.0,
                source_end=total,
            ),
        ),
    )
    return timeline, {"combined": str(combined)}


def _served_duration(ep: Episode) -> float | None:
    """The EDL (cue) clock for an edited episode, or the source duration as a fallback.

    For a manipulated episode this returns the EDL/cue clock (the planned served length),
    derived through the single :func:`citypods.timeline.edl_duration` primitive rather than a
    local re-implementation. For identity episodes (no timeline / digest ``""``) it falls back
    to ``ep.duration`` (the *source* duration, which equals the served length there).

    NOTE (review/20): the value this returns is the **EDL** clock, not the probed hosted-stream
    duration. It is still acceptable as a backfill estimate for ``audio_duration_served`` *when
    no probe is available*, and as the size input to encode-RSS estimation — but a follow-up PR
    makes the probed hosted-stream duration authoritative for ``audio_duration_served`` so the
    EDL and the real enclosure can no longer be conflated by construction."""
    tl = ep.timeline
    if tl is not None and timeline_digest(tl, ep.sources) != "":
        return edl_duration(tl)
    if ep.duration is None:
        return None
    duration = float(ep.duration)
    return duration if duration > 0 else None


def _has_non_identity_timeline(ep: Episode) -> bool:
    return ep.timeline is not None and timeline_digest(ep.timeline, ep.sources) != ""


# Encode peak-RSS cost model (H8 reservation admission). The legacy one-pass dynamic loudnorm path
# grows with recording length (observed 9–13 GiB on long meetings), so its conservative
# duration-scaled model remains for compatibility. ``podcast-speech-v2`` uses only streaming
# filters, a local FLAC intermediate, and measured *linear* loudnorm; its memory reservation is
# fixed instead of duration-scaled. The mid-flight floor remains the backstop for either path.
_ENCODE_RSS_COPY_BYTES = 300 * 1024 * 1024  # copy path (no filter): tiny, bounded
_ENCODE_RSS_BASE_BYTES = 350 * 1024 * 1024  # filtergraph + AAC encoder baseline
# 2026-06-15 telemetry from audio run #10 showed long loudnorm/filter jobs peaking around
# 9-13 GiB; the prior 32 MiB/min coefficient and 6.5 GiB clamp admitted too many long jobs.
_ENCODE_RSS_PER_MINUTE_BYTES = 64 * 1024 * 1024
_ENCODE_RSS_UNKNOWN_BYTES = 12_000 * 1024 * 1024
_ENCODE_RSS_MAX_BYTES = 12_000 * 1024 * 1024
_ENCODE_RSS_STREAMING_BYTES = 768 * 1024 * 1024


def estimate_encode_rss_bytes(
    ep: Episode, *, loudness_profile: str = "", processing_profile: str = ""
) -> int:
    """Predict an encode's peak RSS so the reservation accountant can admit by future load.

    The production speech profile has a fixed reservation because every filter in both passes is
    streaming and the intermediate is on disk. Legacy filter encodes retain the duration-scaled
    model: served length comes from the EDL or feed duration and is clamped to the observed ceiling.
    Unknown-length legacy encodes reserve conservatively so they run alone.
    """
    if processing_profile == PODCAST_SPEECH_PROFILE:
        return _ENCODE_RSS_STREAMING_BYTES
    use_filter = _has_non_identity_timeline(ep) or bool(loudness_profile or processing_profile)
    if not use_filter:
        return _ENCODE_RSS_COPY_BYTES
    served = _served_duration(ep)
    if served is None or served <= 0:
        return _ENCODE_RSS_UNKNOWN_BYTES
    est = _ENCODE_RSS_BASE_BYTES + int(_ENCODE_RSS_PER_MINUTE_BYTES * (served / 60.0))
    return min(est, _ENCODE_RSS_MAX_BYTES)


def _backfill_served_duration(ep: Episode) -> str:
    """Populate ``audio_duration_served`` only when no measured value is available.

    review/20: the authoritative served duration is the *probed* duration of the actual hosted
    object (set by the encode caller from the post-encode ffprobe, or adopted from a reused
    artifact's recorded duration). This function no longer overwrites a present value with the EDL
    sum — that conflated the served/hosted clock with the EDL/cue clock and masked renders that
    disagreed with the EDL. It now only *fills* a missing value, falling back to the EDL/source
    estimate so the enclosure still carries a duration when the caller could not measure one."""
    if ep.audio_duration_served is not None and ep.audio_duration_served > 0:
        return "existing"
    fallback = _served_duration(ep)
    if fallback is not None and fallback > 0:
        ep.audio_duration_served = fallback
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
    processing_profile: str = "",
    resolve_media_url: Callable[[Episode], str],
    stop: Callable[[], bool] | None = None,
    source_cache: SourceCache | None = None,
    max_workers: int = 1,
    resource_admission: object | None = None,
    native_work_gate: NativeWorkGate | None = None,
    memory_reservation: MemoryReservation | None = None,
    transport_telemetry: ProviderTransportTelemetry | None = None,
    hosted_keys_cache: HostedKeysCache | None = None,
    audio_artifact_cache: AudioArtifactCache | None = None,
) -> MaterializeStats:
    """(Re-)host audio for episodes that need it, content-addressed by audio spec.

    Mutates each episode in place (``audio_key`` / ``audio_spec_hash`` / ``hosted_audio_url``);
    the caller persists these onto the record store. An episode is re-encoded only when its
    audio spec changed (e.g. chapters added, bitrate policy bumped) *or* its referenced object
    has gone missing from storage — otherwise the existing object is reused for free. A
    ``"legacy"`` artifact is reusable only while no explicit loudness/processing recipe is active;
    named recipes invalidate it because its original byte recipe is unknown.

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

    ``hosted_keys_cache``, when provided, shares one ``list_objects`` listing per source across
    every call for that source during the cache's lifetime — needed because the global queue
    (H5 PR3) invokes this function once per *episode*, not once per source (issue #344).

    ``audio_artifact_cache`` lets duplicate source views share one successful artifact and encode
    while leaving their source-scoped records independent (GH#421).
    """
    stats = MaterializeStats()
    hosted_keys = (
        hosted_keys_cache.get(city, storage)
        if hosted_keys_cache is not None
        else _hosted_keys(city, storage)
    )
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
    encode_cache_keys: dict[int, tuple[str, str, str]] = {}
    ffmpeg_binary = getattr(ffmpeg, "binary", "ffmpeg")
    src_key = source_key(city)
    stats_lock = threading.Lock()

    def _defer_sample(ep: Episode, reason: str) -> str:
        return f"{ep.uid or ep.guid}:{reason}"

    def _log_audio_defer(
        ep: Episode,
        reason: str,
        *,
        backoff: bool = False,
        detail: str | None = None,
    ) -> None:
        with stats_lock:
            stats.defer(reason, backoff=backoff, sample=_defer_sample(ep, reason))
        label = ep.uid or ep.guid
        msg = (
            f"[enrich] audio materialize deferred slug={city.slug} provider={city.provider} "
            f"source={src_key} uid={label} guid={ep.guid} reason={reason}"
        )
        if detail:
            msg += f" detail={detail.replace(chr(10), ' ')[:200]}"
        print(msg, flush=True)

    def _stop_defer_reason(exc: StopRequested) -> str:
        detail = str(exc).lower()
        if "source cache" in detail:
            return "source-cache-stop"
        if "lease" in detail:
            return "provider-lease-stop"
        if "rate" in detail or "host" in detail or "throttle" in detail:
            return "provider-throttle-stop"
        return "stop-requested"

    def _artifact(ep: Episode) -> AudioArtifact:
        return AudioArtifact(
            key=str(ep.audio_key),
            spec=str(ep.audio_spec_hash),
            url=str(ep.hosted_audio_url),
            duration=ep.audio_duration_served,
            size=ep.audio_bytes,
            encoded_at=ep.audio_encode_time,
        )

    def _apply_artifact(ep: Episode, artifact: AudioArtifact) -> None:
        ep.audio_key = artifact.key
        ep.audio_spec_hash = artifact.spec
        ep.hosted_audio_url = artifact.url
        ep.audio_bytes = artifact.size
        ep.audio_encode_time = artifact.encoded_at
        # The audio is identical across a coalesced recipe, so the served duration is a content
        # property: adopt the shared artifact's value when it has one, but never downgrade a
        # follower's own probed duration to a missing/zero one. A *credited* canonical winner can
        # carry no probe, which previously regressed a follower to 0s and tripped H16
        # served_duration / current_artifact_changed (GH#421 follow-up). Backfill from the episode's
        # own timeline/source as a final fallback.
        if artifact.duration and artifact.duration > 0:
            ep.audio_duration_served = artifact.duration
        ep.materialize_attempts = 0
        ep.materialize_last_attempt = None
        ep.materialize_error = None
        _backfill_served_duration(ep)

    for ep in episodes:
        if not _should_host(ep, city):
            continue

        # ``loudness`` was the pre-fallback error code used by peak-headroom failures. Retry those
        # immediately once under the fixed profile instead of preserving up to 30 days of stale
        # exponential backoff. New measurement/parsing failures use ``loudness_measurement`` and
        # retain normal backoff behavior.
        if processing_profile == PODCAST_SPEECH_PROFILE and ep.materialize_error == "loudness":
            ep.materialize_attempts = 0
            ep.materialize_last_attempt = None
            ep.materialize_error = None

        spec = audio_spec_hash(
            ep,
            max_kbps=max_kbps,
            loudness_profile=loudness_profile,
            processing_profile=processing_profile,
        )
        uid = ep.uid or ep.guid
        cache_key = (city.provider, uid, spec)
        if audio_artifact_cache is not None:
            leader, cached = audio_artifact_cache.claim(cache_key)
            if not leader:
                assert cached is not None
                _apply_artifact(ep, cached)
                stats.reused += 1
                print(
                    f"[enrich] audio reused slug={city.slug} provider={city.provider} "
                    f"source={src_key} uid={uid} reason=deduplicated-run",
                    flush=True,
                )
                continue
        # Already hosted with a matching spec (or carried over from the legacy manifest while no
        # explicit audio recipe is configured)? A named loudness/processing profile is a real byte
        # recipe, so it intentionally invalidates legacy artifacts that cannot prove how they were
        # encoded.
        # Trust the record only when its object is actually in storage — otherwise a stale
        # record (e.g. a Swagit episode whose presigned source expired before the object was
        # ever written) would short-circuit forever and never re-materialize (issue #116).
        legacy_ok = ep.audio_spec_hash == "legacy" and not (loudness_profile or processing_profile)
        spec_ok = bool(ep.hosted_audio_url) and (ep.audio_spec_hash == spec or legacy_ok)
        present = bool(ep.audio_key) and _present(ep.audio_key)
        # An episode flagged with a materialization error is not "done" even if a stale object with
        # a matching spec is still in storage (e.g. an encode that uploaded bytes but failed to
        # probe a duration). Don't reuse/credit it: re-encode (subject to the backoff below) so a
        # successful pass clears the error + records the served duration. This is what surfaces the
        # ~600 errored episodes the ASR lane now skips back to the audio lane (run #25).
        errored = bool(ep.materialize_error)
        if spec_ok and present and not errored:
            _backfill_served_duration(ep)
            if audio_artifact_cache is not None:
                audio_artifact_cache.complete(cache_key, _artifact(ep))
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
            if audio_artifact_cache is not None:
                audio_artifact_cache.abort(cache_key)
            _log_audio_defer(
                ep,
                f"{ep.materialize_error or 'error'}-backoff",
                backoff=True,
                detail=f"attempts={ep.materialize_attempts}",
            )
            continue

        canonical_source = (
            audio_artifact_cache.canonical_source(city.provider, src_key, uid)
            if audio_artifact_cache is not None
            else src_key
        )
        key = f"{city.provider}/{canonical_source}/{uid}-{spec}.m4a"
        # Credit path: the object is already in storage (e.g. a prior run uploaded it but the
        # record drifted). (Re)attaching its URL is a near-free metadata op — ~10-100x cheaper than
        # an encode — so it does NOT draw from the budget. The budget meters the expensive encode
        # path only, which keeps its per-episode time estimate honest and lets a credit-heavy
        # catch-up run reconcile freely without deferring real encodes.
        if _present(key) and not errored:
            ep.audio_key = key
            ep.audio_spec_hash = spec
            ep.hosted_audio_url = storage.public_url(key)
            _backfill_served_duration(ep)
            ep.materialize_attempts = 0
            ep.materialize_last_attempt = None
            ep.materialize_error = None
            if audio_artifact_cache is not None:
                audio_artifact_cache.complete(cache_key, _artifact(ep))
            stats.hosted += 1
            stats.credited += 1
            continue

        to_encode.append((ep, spec, key))
        encode_cache_keys[id(ep)] = cache_key

    if not to_encode:
        return stats

    # Encode pass: parallel when max_workers > 1. Each worker is independent (per-episode state
    # mutations don't overlap); only stats updates require a lock.

    def _encode_one(item: tuple[Episode, str, str]) -> None:
        ep, spec, key = item
        cache_key = encode_cache_keys.get(id(ep))
        cache_completed = False

        def _abort_cache() -> None:
            if audio_artifact_cache is not None and cache_key is not None:
                audio_artifact_cache.abort(cache_key)

        # Re-check stop inside the worker: submitted-but-not-yet-started tasks yield gracefully
        # when the budget expires mid-batch.
        if stop is not None and stop():
            _abort_cache()
            _log_audio_defer(ep, "stop-signal")
            return
        label = ep.uid or ep.guid
        _progress_entry = PROGRESS.start(source=str(src_key), uid=str(label), phase="audio-encode")
        # Admission: reserve the encode's predicted peak RSS so it starts only with real budget
        # headroom — a *leading* signal. The reservation supersedes the instantaneous mem_available
        # gate for audio; that gate (resource_admission) is the fallback when no budget is set and
        # still governs ASR elsewhere. native_work_gate is the hard concurrency ceiling on top.
        reserved_bytes = 0
        if memory_reservation is not None:
            reserved_bytes = estimate_encode_rss_bytes(
                ep,
                loudness_profile=loudness_profile,
                processing_profile=processing_profile,
            )
            if not memory_reservation.reserve(reserved_bytes, label=str(label), stop=stop):
                _abort_cache()
                _log_audio_defer(ep, "memory-reservation")
                PROGRESS.finish(_progress_entry)
                return
        elif resource_admission is not None:
            if not resource_admission.wait(kind="audio", label=str(label), stop=stop):
                _abort_cache()
                _log_audio_defer(ep, "resource-admission")
                PROGRESS.finish(_progress_entry)
                return
        gate_acquired = False
        phase_managed = bool(getattr(ffmpeg, "manages_audio_phases", False))
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
                multi_source = bool(ep.sources and len(ep.sources) > 1)
                render_timeline = ep.timeline
                render_sources: tuple[SourceMedia, ...] | list[SourceMedia] = ep.sources
                if multi_source:
                    source_url = ep.sources[0].ref
                    source_urls = [src.ref for src in ep.sources]
                else:
                    source_url = resolve_media_url(ep)
                    source_urls = [source_url]
                    if ep.sources:
                        source_urls.extend(
                            src.ref for src in ep.sources if src.ref not in source_urls
                        )
                # For single-source episodes, use a locally cached copy when available so the
                # encode pass reads from disk rather than re-streaming the rate-limited source.
                # Multi-source concat episodes: download + concat each segment into one local
                # file once (per-segment timeout, releases the rate-limit slot between segments)
                # and render that single file as a single source instead of streaming N remote
                # URLs into one filter_complex on every encode attempt — ep.timeline is left
                # untouched (clips/soundbites still need the original per-segment URLs); only
                # this render's input changes. See _concat_render_timeline for which encoder
                # path that single file actually takes.
                if source_cache is not None and ep.uid and not multi_source:
                    local = source_cache.get_or_fetch(ep.uid, source_url)
                    by_id = _sources_by_id(ep, str(local) if local is not None else source_url)
                elif source_cache is not None and ep.uid and multi_source:
                    combined = source_cache.get_or_fetch_concat(ep.uid, ep.sources)
                    if combined is not None:
                        render_timeline, by_id = _concat_render_timeline(ep.sources, combined)
                        render_sources = ()
                    else:
                        by_id = _sources_by_id(ep, source_url)
                else:
                    by_id = _sources_by_id(ep, source_url)
                # Fetch/cache the provider source before occupying a native CPU slot. Production
                # ``CommandFfmpeg`` manages admission separately for its measure and finalize
                # subprocesses; third-party/test runners retain the legacy whole-render gate here.
                if native_work_gate is not None and not phase_managed:
                    gate_acquired = native_work_gate.acquire(
                        kind="audio",
                        label=str(label),
                        stop=stop,
                    )
                    if not gate_acquired:
                        _log_audio_defer(ep, "native-gate")
                        return
                render_options: dict[str, str | None] = {
                    "loudness_profile": loudness_profile or None
                }
                # Keep third-party/test FfmpegRunner implementations source-compatible when the
                # new profile is disabled; production passes the new keyword explicitly.
                if processing_profile:
                    render_options["processing_profile"] = processing_profile
                ffmpeg.extract_audio(
                    render_timeline,
                    by_id,
                    dest,
                    ep.chapters or None,
                    sources=render_sources,
                    **render_options,
                )
                probed: float | None = None
                try:
                    probed = _probe_duration_secs(dest, ffmpeg_binary)
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
            # Commit the encode result atomically: the artifact pointer AND the probed served
            # duration are written only after a successful upload. Setting audio_duration_served
            # before put_file (its prior home) left a failed upload partially mutated — the record
            # carried the new artifact's duration while still pointing at the prior artifact — which
            # H16IdentityTracker.verify then misreported as an identity mismatch (GH#353, Audio
            # #54/#56: a transient B2 ServiceUnavailable on a recipe-changed re-encode).
            ep.audio_key = key
            ep.audio_spec_hash = spec
            ep.hosted_audio_url = url
            ep.audio_encode_time = now.isoformat()
            if probed is not None:
                ep.audio_duration_served = probed
            _backfill_served_duration(ep)
            ep.materialize_attempts = 0  # success clears the backoff state (#120)
            ep.materialize_last_attempt = None
            ep.materialize_error = None
            if audio_artifact_cache is not None and cache_key is not None:
                audio_artifact_cache.complete(cache_key, _artifact(ep))
                cache_completed = True
            with stats_lock:
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
        except StopRequested as exc:
            # The run's wall-clock budget expired while queued on a coordination wait (host rate
            # limit, distributed lease, source cache) — not a source/provider failure, so no
            # backoff is recorded; this episode is simply retried from the top next run.
            _log_audio_defer(ep, _stop_defer_reason(exc), detail=str(exc))
        except (
            subprocess.TimeoutExpired,
            subprocess.CalledProcessError,
            OSError,
            ProviderError,
        ) as exc:
            if isinstance(exc, TruncatedAudioError) and transport_telemetry is not None:
                transport_telemetry.record_truncation(source_urls)
            if isinstance(exc, RateLimitedMediaFetchError):
                with stats_lock:
                    stats.rate_limited += 1
            # Record the failed attempt so this episode backs off (exponentially) instead of being
            # re-tried every run — otherwise a permanently-broken meeting churns budget/time (#120).
            # A timeout is transient (the source may recover), so like a generic ``error`` it backs
            # off + retries rather than counting as ``dead``; it gets its own code only so a stalled
            # source is distinguishable from other failures on the record.
            record_materialize_failure(
                ep,
                "timeout"
                if isinstance(exc, subprocess.TimeoutExpired)
                else getattr(exc, "code", None) or "error",
                now=now,
            )
            with stats_lock:
                stats.errors.append(f"{ep.uid or ep.guid}: {exc}")
            elapsed = time.perf_counter() - t0
            print(
                f"[enrich] audio encode error slug={city.slug} provider={city.provider} "
                f"source={src_key} uid={label} guid={ep.guid} "
                f"seconds={elapsed:.1f}: {exc}",
                flush=True,
            )
        finally:
            if not cache_completed:
                _abort_cache()
            if gate_acquired and native_work_gate is not None:
                native_work_gate.release(kind="audio")
            if memory_reservation is not None and reserved_bytes:
                memory_reservation.release(reserved_bytes)
            PROGRESS.finish(_progress_entry)

    if max_workers > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            list(pool.map(_encode_one, to_encode))
    else:
        for item in to_encode:
            _encode_one(item)

    return stats
