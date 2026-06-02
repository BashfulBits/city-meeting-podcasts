"""Audio materialization: source media -> M4A -> object storage.

Used for two cases:
  - CivicPlus/CivicMedia episodes (``media_kind == "hls"``): the only way to get a
    playable enclosure, since the source is tokenized/expiring HLS.
  - Granicus episodes when the city sets ``extract_audio: true``.

ffmpeg invocation is injectable (``FfmpegRunner``) so the pipeline is unit-testable
offline with a fake. A per-run ``budget`` caps how many *new* episodes are processed,
so a large first-time backfill is spread over successive scheduled runs rather than
blowing the Actions 6-hour job limit.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from citypods.models import City, Episode
from citypods.providers.base import ProviderError
from citypods.records import audio_object_key, audio_spec_hash, source_key
from citypods.storage.base import StorageBackend

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
    def extract_audio(
        self, source_url: str, dest: Path, chapters: list[dict] | None = None
    ) -> None:
        """Demux/encode audio from ``source_url`` (URL or HLS manifest) into ``dest`` (.m4a).

        ``chapters`` (``[{"start": secs, "end": secs|None, "title": str}, ...]``) are embedded
        as M4A chapter markers when provided.
        """
        ...


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


def encode_args(source_bitrate: int | None, max_kbps: int) -> list[str]:
    """ffmpeg audio codec args: copy if the source is already <= the cap, else re-encode
    to ``max_kbps`` mono AAC. Unknown source bitrate -> re-encode (safe upper bound)."""
    if source_bitrate is not None and source_bitrate <= max_kbps * 1000:
        return ["-c:a", "copy"]
    return ["-c:a", "aac", "-b:a", f"{max_kbps}k", "-ac", "1"]


class CommandFfmpeg:
    """Runs the real ffmpeg binary, re-encoding only when the source exceeds the cap."""

    def __init__(
        self, binary: str = "ffmpeg", max_kbps: int = 96, timeout_seconds: float | None = None
    ):
        self.binary = binary
        self.max_kbps = max_kbps
        # Hard wall-clock cap for one probe+encode (None = uncapped, e.g. tests). See module
        # constants above for why an in-flight encode must be bounded.
        self.timeout_seconds = timeout_seconds

    def extract_audio(
        self, source_url: str, dest: Path, chapters: list[dict] | None = None
    ) -> None:
        probe_timeout = (
            None if self.timeout_seconds is None else min(self.timeout_seconds, _PROBE_TIMEOUT_S)
        )
        args = encode_args(
            _probe_audio_bitrate(source_url, self.binary, timeout=probe_timeout), self.max_kbps
        )
        with tempfile.TemporaryDirectory() as tmp:
            # ``-rw_timeout`` (microseconds) is an *input* option, so it must precede the source
            # ``-i`` it guards; it makes ffmpeg give up on a stalled read rather than hang.
            inputs = ["-rw_timeout", str(_STALL_TIMEOUT_US), "-i", source_url]
            chapter_args: list[str] = []
            if chapters:
                meta = Path(tmp) / "chapters.ffmeta"
                meta.write_text(_ffmetadata(chapters))
                inputs += ["-i", str(meta)]
                # take chapters from the metadata input; keep audio from the media input
                chapter_args = ["-map_chapters", "1", "-map", "0:a:0"]
            cmd = [
                self.binary,
                "-y",
                "-loglevel",
                "error",
                # Restrict ffmpeg to the protocols HLS/MP4-over-HTTPS actually need, so a hostile
                # manifest/redirect can't coax it into reading local files or other schemes.
                "-protocol_whitelist",
                "file,crypto,data,http,https,tcp,tls",
                *inputs,
                "-vn",
                *chapter_args,
                *args,
                "-movflags",
                "+faststart",
                str(dest),
            ]
            # ``timeout`` is the hard backstop if ``-rw_timeout`` doesn't trip: it kills ffmpeg and
            # raises subprocess.TimeoutExpired, which materialize_audio records as a failed attempt.
            subprocess.run(cmd, check=True, capture_output=True, timeout=self.timeout_seconds)


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


def materialize_audio(
    city: City,
    episodes: list[Episode],
    *,
    storage: StorageBackend,
    ffmpeg: FfmpegRunner,
    max_kbps: int,
    resolve_media_url: Callable[[Episode], str],
    stop: Callable[[], bool] | None = None,
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
    """
    stats = MaterializeStats()
    hosted_keys = _hosted_keys(city, storage)
    now = datetime.now(UTC)

    def _present(key: str) -> bool:
        return key in hosted_keys if hosted_keys is not None else storage.exists(key)

    for ep in episodes:
        if not _should_host(ep, city):
            continue

        spec = audio_spec_hash(ep, max_kbps=max_kbps)
        # Already hosted with a matching spec (or carried over from the legacy manifest)?
        # Trust the record only when its object is actually in storage — otherwise a stale
        # record (e.g. a Swagit episode whose presigned source expired before the object was
        # ever written) would short-circuit forever and never re-materialize (issue #116).
        spec_ok = bool(ep.hosted_audio_url) and ep.audio_spec_hash in (spec, "legacy")
        present = bool(ep.audio_key) and _present(ep.audio_key)
        if spec_ok and present:
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
            ep.materialize_attempts = 0
            ep.materialize_last_attempt = None
            ep.materialize_error = None
            stats.hosted += 1
            stats.credited += 1
            continue

        # Encode path: download + ffmpeg + upload — the work that actually consumes the time
        # window, so it's the only thing gated by ``stop()`` (wall-clock spent or superseded). The
        # rest of the scan keeps running, so cheap credits/reuse still reconcile records this run.
        if stop is not None and stop():
            stats.skipped_budget += 1
            continue
        try:
            with tempfile.TemporaryDirectory() as tmp:
                dest = Path(tmp) / "audio.m4a"
                source_url = resolve_media_url(ep)
                ffmpeg.extract_audio(source_url, dest, ep.chapters or None)
                url = storage.put_file(key, dest, CONTENT_TYPE)
                try:
                    stats.bytes_written += dest.stat().st_size
                except OSError:
                    pass
            ep.audio_key = key
            ep.audio_spec_hash = spec
            ep.hosted_audio_url = url
            ep.materialize_attempts = 0  # success clears the backoff state (#120)
            ep.materialize_last_attempt = None
            ep.materialize_error = None
            stats.hosted += 1
            stats.encoded += 1
        except (
            subprocess.TimeoutExpired,
            subprocess.CalledProcessError,
            OSError,
            ProviderError,
        ) as exc:
            stats.errors.append(f"{ep.uid or ep.guid}: {exc}")
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

    return stats
