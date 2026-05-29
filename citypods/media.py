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

import hashlib
import json
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from citypods.models import City, Episode
from citypods.providers.base import ProviderError
from citypods.storage.base import StorageBackend

CONTENT_TYPE = "audio/mp4"


class FfmpegRunner(Protocol):
    def extract_audio(self, source_url: str, dest: Path) -> None:
        """Demux/encode audio from ``source_url`` (URL or HLS manifest) into ``dest`` (.m4a)."""
        ...


class CommandFfmpeg:
    """Runs the real ffmpeg binary."""

    def __init__(self, binary: str = "ffmpeg"):
        self.binary = binary

    def extract_audio(self, source_url: str, dest: Path) -> None:
        # Copy the (typically AAC) audio track losslessly into an MP4/M4A container.
        cmd = [
            self.binary,
            "-y",
            "-loglevel",
            "error",
            "-i",
            source_url,
            "-vn",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            str(dest),
        ]
        subprocess.run(cmd, check=True, capture_output=True)


@dataclass
class MaterializeStats:
    hosted: int = 0  # newly uploaded this run
    reused: int = 0  # already in manifest / storage
    skipped_budget: int = 0  # deferred to a later run
    errors: list[str] = field(default_factory=list)


def _audio_key(slug: str, guid: str) -> str:
    digest = hashlib.sha1(guid.encode()).hexdigest()[:16]
    return f"{slug}/{digest}.m4a"


def _should_host(episode: Episode, city: City) -> bool:
    if episode.media_kind == "hls":
        return True
    return city.extract_audio  # direct source, opt-in extraction


def manifest_path(output_dir: Path, slug: str) -> Path:
    return Path(output_dir) / slug / "audio_manifest.json"


def load_manifest(output_dir: Path, slug: str) -> dict:
    path = manifest_path(output_dir, slug)
    return json.loads(path.read_text()) if path.exists() else {}


def save_manifest(output_dir: Path, slug: str, manifest: dict) -> None:
    path = manifest_path(output_dir, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def materialize_audio(
    city: City,
    episodes: list[Episode],
    *,
    storage: StorageBackend,
    manifest: dict,
    ffmpeg: FfmpegRunner,
    budget: int,
    resolve_media_url: Callable[[Episode], str],
) -> MaterializeStats:
    """Populate ``episode.hosted_audio_url`` for episodes that need hosting.

    Mutates ``episodes`` (sets ``hosted_audio_url``) and ``manifest`` in place. Episodes
    that still need hosting but exceed ``budget`` are left unhosted; the caller drops
    them from this run's feed and they get picked up next run.
    """
    stats = MaterializeStats()
    remaining = budget

    for ep in episodes:
        if not _should_host(ep, city):
            continue

        cached = manifest.get(ep.guid)
        if cached and cached.get("url"):
            ep.hosted_audio_url = cached["url"]
            stats.reused += 1
            continue

        if remaining <= 0:
            stats.skipped_budget += 1
            continue

        key = _audio_key(city.slug, ep.guid)
        try:
            if storage.exists(key):
                url = storage.public_url(key)
            else:
                with tempfile.TemporaryDirectory() as tmp:
                    dest = Path(tmp) / "audio.m4a"
                    source_url = resolve_media_url(ep)
                    ffmpeg.extract_audio(source_url, dest)
                    url = storage.put_file(key, dest, CONTENT_TYPE)
            ep.hosted_audio_url = url
            manifest[ep.guid] = {"key": key, "url": url}
            stats.hosted += 1
            remaining -= 1
        except (subprocess.CalledProcessError, OSError, ProviderError) as exc:
            stats.errors.append(f"{ep.guid}: {exc}")

    return stats
