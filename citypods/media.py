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

import subprocess
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from citypods.models import City, Episode
from citypods.providers.base import ProviderError
from citypods.records import audio_object_key, audio_spec_hash
from citypods.storage.base import StorageBackend

CONTENT_TYPE = "audio/mp4"


class FfmpegRunner(Protocol):
    def extract_audio(self, source_url: str, dest: Path) -> None:
        """Demux/encode audio from ``source_url`` (URL or HLS manifest) into ``dest`` (.m4a)."""
        ...


def encode_args(source_bitrate: int | None, max_kbps: int) -> list[str]:
    """ffmpeg audio codec args: copy if the source is already <= the cap, else re-encode
    to ``max_kbps`` mono AAC. Unknown source bitrate -> re-encode (safe upper bound)."""
    if source_bitrate is not None and source_bitrate <= max_kbps * 1000:
        return ["-c:a", "copy"]
    return ["-c:a", "aac", "-b:a", f"{max_kbps}k", "-ac", "1"]


class CommandFfmpeg:
    """Runs the real ffmpeg binary, re-encoding only when the source exceeds the cap."""

    def __init__(self, binary: str = "ffmpeg", max_kbps: int = 96):
        self.binary = binary
        self.max_kbps = max_kbps

    def extract_audio(self, source_url: str, dest: Path) -> None:
        args = encode_args(_probe_audio_bitrate(source_url, self.binary), self.max_kbps)
        cmd = [
            self.binary,
            "-y",
            "-loglevel",
            "error",
            "-i",
            source_url,
            "-vn",
            *args,
            "-movflags",
            "+faststart",
            str(dest),
        ]
        subprocess.run(cmd, check=True, capture_output=True)


def _probe_audio_bitrate(url: str, ffmpeg_binary: str = "ffmpeg") -> int | None:
    """Return the source's audio bitrate in bits/sec via ffprobe, or None if unknown."""
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
        ).stdout.strip()
        return int(out) if out.isdigit() else None
    except (subprocess.CalledProcessError, OSError, ValueError):
        return None


class GlobalBudget:
    """Thread-safe cap on total new materializations per build run (across all cities)."""

    def __init__(self, total: int):
        self._remaining = total
        self._lock = threading.Lock()

    def take(self) -> bool:
        with self._lock:
            if self._remaining <= 0:
                return False
            self._remaining -= 1
            return True


@dataclass
class MaterializeStats:
    hosted: int = 0  # newly uploaded this run
    reused: int = 0  # already in manifest / storage
    skipped_budget: int = 0  # deferred to a later run
    errors: list[str] = field(default_factory=list)


def _should_host(episode: Episode, city: City) -> bool:
    if episode.media_kind == "hls":
        return True
    return city.extract_audio  # direct source, opt-in extraction


def materialize_audio(
    city: City,
    episodes: list[Episode],
    *,
    storage: StorageBackend,
    ffmpeg: FfmpegRunner,
    budget: int,
    max_kbps: int,
    resolve_media_url: Callable[[Episode], str],
    global_budget: GlobalBudget | None = None,
) -> MaterializeStats:
    """(Re-)host audio for episodes that need it, content-addressed by audio spec.

    Mutates each episode in place (``audio_key`` / ``audio_spec_hash`` / ``hosted_audio_url``);
    the caller persists these onto the record store. An episode is re-encoded only when its
    audio spec changed (e.g. chapters added, bitrate policy bumped) — otherwise the existing
    object (matched by content-addressed key, or carried over as ``"legacy"``) is reused for
    free. New hosts are gated by the per-source ``budget`` and the shared ``global_budget`` so
    a large backfill spreads over successive runs.
    """
    stats = MaterializeStats()
    remaining = budget

    for ep in episodes:
        if not _should_host(ep, city):
            continue

        spec = audio_spec_hash(ep, max_kbps=max_kbps)
        # Already hosted with a matching spec (or carried over from the legacy manifest)?
        if ep.hosted_audio_url and ep.audio_spec_hash in (spec, "legacy"):
            stats.reused += 1
            continue

        if remaining <= 0 or (global_budget is not None and not global_budget.take()):
            stats.skipped_budget += 1
            continue

        key = audio_object_key(city, ep, spec)
        try:
            if storage.exists(key):
                url = storage.public_url(key)
            else:
                with tempfile.TemporaryDirectory() as tmp:
                    dest = Path(tmp) / "audio.m4a"
                    source_url = resolve_media_url(ep)
                    ffmpeg.extract_audio(source_url, dest)
                    url = storage.put_file(key, dest, CONTENT_TYPE)
            ep.audio_key = key
            ep.audio_spec_hash = spec
            ep.hosted_audio_url = url
            stats.hosted += 1
            remaining -= 1
        except (subprocess.CalledProcessError, OSError, ProviderError) as exc:
            stats.errors.append(f"{ep.uid or ep.guid}: {exc}")

    return stats
