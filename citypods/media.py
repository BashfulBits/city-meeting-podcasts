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
import re
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from citypods.models import City, Episode
from citypods.providers.base import ProviderError
from citypods.records import audio_object_key, audio_spec_hash, source_key
from citypods.resources import NativeWorkGate, ResourceSnapshot, current_snapshot, format_bytes
from citypods.storage.base import StorageBackend
from citypods.timeline import Segment, Timeline, timeline_digest

CONTENT_TYPE = "audio/mp4"

# Exponential backoff for repeatedly-failing materializations (issue #120): a source whose audio
# won't resolve (e.g. a Swagit meeting with no usable media) must stop being re-tried every run,
# or it churns the run's time + budget forever. Wait ``BACKOFF_BASE * 2**(attempts-1)``, capped at
# ``BACKOFF_MAX``, before re-attempting. A successful host resets the counter.
BACKOFF_BASE = timedelta(days=1)
BACKOFF_MAX = timedelta(days=30)

# ffmpeg/ffprobe read the (remote) source directly, so a server that accepts the connection then
# stalls would block a worker forever — and the shared ``stop()`` can't preempt a thread parked in
# ``subprocess.run``, so one stalled source pins the whole build until GitHub's 6h job cap. Bound it
# two ways: ``-rw_timeout`` lets ffmpeg abort a stalled read itself (clean non-zero exit), and the
# subprocess ``timeout=`` is the hard backstop that guarantees the worker returns. Both surface as a
# materialization failure → the #120 backoff, so a chronically-stalling source stops being retried.
_STALL_TIMEOUT_US = 120_000_000  # ffmpeg aborts after 120s with zero I/O progress (microseconds)
_PROBE_TIMEOUT_S = 120.0  # ffprobe reads only stream headers; 2 min is generous
_FFMPEG_GUARD_POLL_SECONDS = 5.0


def _log_ffmpeg_event(log: Callable[[str], None] | None, message: str) -> None:
    if log is None:
        return
    try:
        log(message, flush=True)  # type: ignore[call-arg]
    except TypeError:
        log(message)


def _format_optional_bytes(value: int | None) -> str:
    return "unknown" if value is None else format_bytes(value)


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


def _run_ffmpeg_guarded(
    cmd: list[str],
    *,
    phase: str,
    timeout: float | None = None,
    memory_floor_bytes: int | None = None,
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
    """
    if not memory_floor_bytes:
        _log_ffmpeg_event(log, f"[enrich] ffmpeg {phase} start")
        subprocess.run(cmd, check=True, capture_output=True, timeout=timeout)
        _log_ffmpeg_event(log, f"[enrich] ffmpeg {phase} done")
        return

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
) -> bool:
    """Copy the audio stream from *url* to *dest* (.m4a) without re-encoding.

    Returns True on success; callers fall back to streaming *url* directly on False.
    """
    cmd = [
        ffmpeg_binary,
        "-y",
        "-loglevel",
        "error",
        "-protocol_whitelist",
        "file,crypto,data,http,https,tcp,tls",
        "-rw_timeout",
        str(_STALL_TIMEOUT_US),
        "-i",
        url,
        "-vn",
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        str(dest),
    ]
    try:
        _run_ffmpeg_guarded(
            cmd,
            phase="source-cache",
            timeout=timeout,
            memory_floor_bytes=memory_floor_bytes,
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
        download completes, then all receive the same path. Returns None on failure
        (caller should fall back to streaming *url* directly).
        """
        with self._guard:
            lock = self._locks[uid]
        with lock:
            if uid in self._paths:
                return self._paths[uid]
            dest = Path(self._tmpdir.name) / f"{hashlib.md5(uid.encode()).hexdigest()}.m4a"
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


def encode_args(source_bitrate: int | None, max_kbps: int) -> list[str]:
    """ffmpeg audio codec args: copy if the source is already <= the cap, else re-encode
    to ``max_kbps`` mono AAC. Unknown source bitrate -> re-encode (safe upper bound)."""
    if source_bitrate is not None and source_bitrate <= max_kbps * 1000:
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
        codec_args = encode_args(
            _probe_audio_bitrate(source_url, self.binary, timeout=probe_timeout), self.max_kbps
        )
        thread_args = self._thread_args(codec_args)
        with tempfile.TemporaryDirectory() as tmp:
            inputs = ["-rw_timeout", str(_STALL_TIMEOUT_US), "-i", source_url]
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
            # Build input flags
            inputs: list[str] = []
            for sid in source_ids:
                inputs += ["-rw_timeout", str(_STALL_TIMEOUT_US), "-i", sources_by_id[sid]]
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
            )


# ---------------------------------------------------------------------------
# ffprobe helper
# ---------------------------------------------------------------------------


def _probe_audio_bitrate(
    url: str, ffmpeg_binary: str = "ffmpeg", timeout: float | None = None
) -> int | None:
    """Return the source's audio bitrate in bits/sec via ffprobe, or None if unknown.

    ``timeout`` (seconds) bounds the probe so a stalled source can't hang it; on timeout (or any
    other probe failure) we return None, which encode_args treats as "unknown" → safe re-encode."""
    ffprobe = "ffprobe" if ffmpeg_binary == "ffmpeg" else ffmpeg_binary.replace("ffmpeg", "ffprobe")
    try:
        out = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=bit_rate",
                "-of",
                "default=nw=1:nk=1",
                url,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        ).stdout.strip()
        return int(out) if out.isdigit() else None
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, OSError, ValueError):
        return None


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


def _backfill_served_duration(ep: Episode) -> str:
    if ep.audio_duration_served is not None and ep.audio_duration_served > 0:
        return "existing"
    served = _served_duration(ep)
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
        if resource_admission is not None:
            admitted = resource_admission.wait(kind="audio", label=str(label), stop=stop)
            if not admitted:
                with lock:
                    stats.skipped_budget += 1
                return
        gate_acquired = False
        if native_work_gate is not None:
            gate_acquired = native_work_gate.acquire(kind="audio", label=str(label), stop=stop)
            if not gate_acquired:
                with lock:
                    stats.skipped_budget += 1
                return
        t0 = time.perf_counter()
        print(
            f"[enrich] audio encode start slug={city.slug} provider={city.provider} "
            f"source={src_key} uid={label} guid={ep.guid}",
            flush=True,
        )
        try:
            with tempfile.TemporaryDirectory() as tmp:
                dest = Path(tmp) / "audio.m4a"
                source_url = resolve_media_url(ep)
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
                try:
                    probed = _probe_duration_secs(dest, ffmpeg_binary)
                    if probed is not None:
                        ep.audio_duration_served = probed
                except Exception:  # noqa: BLE001
                    pass
                url = storage.put_file(key, dest, CONTENT_TYPE)
                try:
                    size = dest.stat().st_size
                    ep.audio_bytes = size
                except OSError:
                    size = 0
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

    if max_workers > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            list(pool.map(_encode_one, to_encode))
    else:
        for item in to_encode:
            _encode_one(item)

    return stats
