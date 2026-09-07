"""Lazy sherpa-onnx adapter for R7 native diarization.

Engine: pyannote-segmentation-3.0 (VAD/segmentation) + a swappable speaker-embedding model,
both non-gated ONNX exports from sherpa-onnx's own model releases -- no Hugging Face auth
needed. Superseded the pyannote-audio engine on 2026-09-06 (review/31 §A.1a): an offline
trial found NeMo TitaNet-Small matches pyannote's measured accuracy at 8-13x its CPU speed,
which is what actually removes the long-meeting CPU budget ceiling that motivated the switch.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

# The overall recipe id: segmentation model + embedding model + clustering threshold are all
# pinned together here, so a change to any of them changes this string, which changes the
# content-addressed spec hash (`_diarize_spec_hash`, citypods/stages.py) -- old artifacts from
# the prior pyannote recipe are correctly never confused with these.
DEFAULT_DIARIZE_MODEL = "sherpa-onnx/pyannote-segmentation-3.0"
DEFAULT_EMBEDDING_MODEL = "nemo-titanet-small"
TIMED_WORDS_VALIDATION_VERSION = "1"

_SEGMENTATION_RELEASE_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
)

# Every embedding recipe this project's own offline trial (2026-09-06) actually measured
# accuracy and a calibrated clustering threshold for -- see review/31 §A.1a. The library's
# threshold default (0.5) only ever fit wespeaker-resnet34 by coincidence; the other two
# badly over- or under-segmented at it. Keeping all three selectable (not just the winner)
# costs nothing and preserves the option to re-evaluate without re-deriving thresholds.
_EMBEDDING_RECIPES: dict[str, dict[str, Any]] = {
    "nemo-titanet-small": {
        "url": (
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
            "speaker-recongition-models/nemo_en_titanet_small.onnx"
        ),
        "clustering_threshold": 1.05,
    },
    "wespeaker-resnet34": {
        "url": (
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
            "speaker-recongition-models/wespeaker_en_voxceleb_resnet34.onnx"
        ),
        "clustering_threshold": 0.5,
    },
    "wespeaker-campp": {
        "url": (
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
            "speaker-recongition-models/wespeaker_en_voxceleb_CAM%2B%2B.onnx"
        ),
        "clustering_threshold": 0.65,
    },
}

_DEFAULT_CACHE_DIR = Path.home() / ".cache" / "citypods-diarize"

# sherpa-onnx's own default (0.1 -- a 1s shift over pyannote's 10s window, 90% overlap) batches
# roughly one window per audio-second into the segmentation encoder. Any continuous span of
# audio (no VAD-detected pause) whose window count crosses a fixed internal buffer inside the
# exported ONNX graph (~12288, empirically ~123s at the default shift) makes onnxruntime log a
# "Where node" broadcast failure (sequential_executor.cc, BroadcastIterator::Init) -- reproduced
# locally against the exact pinned sherpa-onnx==1.13.7, and observed in production (denton-tx run
# #59, 2026-09-07) on a 5.16h meeting. No upstream fix exists as of 1.13.7 (the current release);
# no matching issue was found in k2-fsa/sherpa-onnx's tracker. `window_shift_ratio` is a real,
# publicly exposed C-API parameter (since 1.13.5) rather than an undocumented workaround: fewer,
# less-overlapping windows for the same audio avoids the error outright on synthetic continuous
# speech (confirmed locally at 0.3/0.5/1.0, spans up to 300s) and is 2.9-3.0x faster (fewer
# windows to run). Accuracy validated against three real, licensed (CC BY 4.0) VoxConverse dev
# clips via speaker_benchmark.compare() -- turn_cluster_accuracy at 0.3 was within noise of 0.1
# on all three (typical: 0.814->0.825; complex, 17 speakers: 0.868->0.861; a 305.8s single-
# speaker continuous span: 1.0->1.0 both). That last clip never triggered the onnxruntime error
# at either ratio despite exceeding the ~123s condition, unlike the synthetic repro -- real
# speech apparently carries enough micro-pauses that the exact gapless-span condition is harder
# to hit than a synthetic worst case, so this default is validated as a safe, faster baseline,
# not confirmed to eliminate the error on arbitrary real audio. See review/31 §A.1a addendum
# (2026-09-07) for the full comparison table.
DEFAULT_WINDOW_SHIFT_RATIO = 0.3

# Predicted peak RSS for one diarize worker, fitted to measurements taken on three different
# GH Actions runner CPUs (AMD Zen4 / Intel Xeon 6973P-C / AMD Zen3): 5min->~377MB,
# 20min->~509MB, 60min->~929MB, near-identical on all three because the footprint tracks
# model + decoded-audio size, not CPU microarchitecture (review/31 §A.4). Rounded up for
# headroom. Feeds `MemoryReservation` so concurrent workers are admitted by *predicted* peak
# rather than trailing `mem_available` -- the same leading-signal discipline H8 uses for audio.
#
# Re-validated, not re-derived, 2026-09-07 (review/31 §A.4 addendum): the original data only
# went to 60min, and run #59's crash near 15h raised the question of whether real usage
# accelerates past that. Measured 5min-8h under DEFAULT_WINDOW_SHIFT_RATIO=0.3: the true
# relationship is 368MB + 461MB/hr (R^2=0.9954, i.e. genuinely linear, not accelerating) -- this
# formula's own 350MB + 650MB/hr overestimates real usage at every point past 5min (up to +40%),
# so it stays conservative rather than needing tightening upward. Left unchanged rather than
# tightened toward the measured fit: that re-validation ran on local Apple Silicon, not the GH
# Actions Linux runners production actually uses, and this same section's own §A.4 already
# documents a case where Apple Silicon numbers gave the wrong answer when cross-checked against
# real runner hardware -- tightening a memory-safety budget on unvalidated-platform data would
# repeat exactly that mistake. The genuinely untested point is real GH Actions data past 60min.
DIARIZE_RSS_BASE_BYTES = 350 * 1024 * 1024
DIARIZE_RSS_PER_HOUR_BYTES = 650 * 1024 * 1024

# Recordings longer than this get processed in multiple chunks instead of one call, each at or
# under this ceiling. review/31 §A.4's 2026-09-07 re-validation only measured (and found the RSS
# formula safely conservative) up to 8h; a 15.09h outlier crashed a live run (R7 runs #59/61/62/
# 63) with a lost-comms SIGTERM ~53min in -- most likely OOM, the same historically-confirmed
# failure mode as GH#377/review#12's H-B. Chunking bounds peak memory to one chunk's worth
# regardless of total length, and incidentally bounds the maximum possible single continuous
# turn to one chunk too, which directly caps the mechanism behind the recurring onnxruntime
# "Where node" broadcast error (a degenerate, anomalously long turn fed whole into embedding
# extraction -- see `_attach_embeddings`'s own error detection, added alongside this).
DIARIZE_CHUNK_THRESHOLD_SECONDS = 8 * 3600

# How far from a naive even split point (duration / n_chunks) to look for an existing chapter
# boundary (an agenda-item change) before giving up on chapter-awareness for that split and
# falling back to the naive point itself. Splitting near a chapter change rather than at an
# arbitrary timestamp keeps a non-recurring speaker (a public commenter, a one-time staff
# presenter) from having their few turns land on both sides of a chunk boundary, where
# cross-chunk speaker merging (`CHUNK_MERGE_MIN_COSINE`) is least reliable for someone who never
# appears more than once.
_CHAPTER_SEARCH_WINDOW_SECONDS = 45 * 60

# Once an anchor is chosen (a nearby chapter boundary, or the naive split point if none
# qualified), how far around it to search for the longest detected silence to actually cut on.
# Splitting mid-utterance is worth avoiding even at an otherwise-good anchor.
_SILENCE_SEARCH_WINDOW_SECONDS = 6 * 60
_SILENCE_MIN_DURATION_SECONDS = 1.5
_SILENCE_NOISE_DB = -35.0

# Extra audio decoded on each side of an internal chunk boundary so the segmentation model has
# real context right at the cut. Turns starting in this padding are discarded during merge, not
# kept twice -- the neighboring chunk that actually owns that region already covers it.
_CHUNK_OVERLAP_SECONDS = 20.0

# Cosine similarity between two chunks' per-cluster mean embeddings, at or above which they are
# merged into one global speaker. A new, separately-calibrated threshold -- NOT
# `clustering_threshold` (sherpa-onnx's internal FastClusteringConfig value, e.g. 1.05 for
# nemo-titanet-small): that is a distance over a different internal embedding pathway and does
# not transfer here, confirmed empirically (same/different-speaker separation on real per-turn
# embeddings under that metric was barely better than chance). Also NOT
# `speakers.minimum_match_score` (0.75, site_config default): that is calibrated for matching a
# single turn's embedding against an established multi-meeting *reference* embedding, a cleaner
# signal than one chunk-half's mean of a handful of turns. Calibrated instead directly against
# this comparison -- per-chunk cluster-centroid cosine similarity -- on real, licensed (CC BY
# 4.0) VoxConverse speakers split into two halves to simulate two chunk appearances:
# same-speaker-half-pairs mean cosine 0.642, different-speaker mean 0.111, clean separation in
# that range (n=13 same-pairs across 2 clips -- real but small; revisit with more data if
# cross-chunk merge quality becomes a concern). Set on the conservative side of that range
# (favoring precision over recall): a false *merge* of two different people into one identity is
# worse for downstream naming than a false split, which just leaves the same person under two
# anonymous labels -- no worse than clustering already occasionally does within one chunk.
CHUNK_MERGE_MIN_COSINE = 0.45


def estimate_diarize_rss_bytes(recording_seconds: float) -> int:
    """Predict one diarize worker's peak RSS for `recording_seconds` of audio."""
    hours = max(0.0, float(recording_seconds)) / 3600.0
    return DIARIZE_RSS_BASE_BYTES + int(DIARIZE_RSS_PER_HOUR_BYTES * hours)


def has_valid_timed_words(value: bytes | Mapping[str, Any]) -> bool:
    """Return whether a word-sidecar payload contains at least one usable timed word."""
    if isinstance(value, bytes):
        try:
            value = json.loads(value.decode("utf-8-sig"))
        except (UnicodeDecodeError, TypeError, ValueError):
            return False
    if not isinstance(value, Mapping):
        return False
    return any(True for _ in _timed_words(value))


@dataclass(frozen=True)
class DiarizeArtifacts:
    """Engine-neutral, source/served-time speaker clustering output."""

    turns: list[dict[str, Any]]
    clusters: list[dict[str, Any]]
    engine: str
    model: str


def _cache_dir() -> Path:
    return Path(os.environ.get("CITYPODS_DIARIZE_MODEL_CACHE") or _DEFAULT_CACHE_DIR)


def _download(url: str, dest: Path, *, attempts: int = 3) -> None:
    """Fetch a fixed, pinned, source-controlled release URL to `dest` atomically.

    Not a caller/provider-supplied URL (every value passed here comes from the recipe
    tables above, not request input), so this intentionally skips the SSRF-guarded
    session machinery `citypods.stages._download_audio_file` uses for untrusted URLs.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    last_exc: Exception | None = None
    for _ in range(attempts):
        # Staged inside dest's own directory so the final rename is same-filesystem (atomic),
        # and so a concurrent reader never observes a half-written model file -- the worker
        # pool (review/31 §A.4) can have several processes racing this same cache.
        tmp_path: Path | None = None
        try:
            with requests.get(url, stream=True, timeout=60) as response:
                response.raise_for_status()
                with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as tmp:
                    tmp_path = Path(tmp.name)
                    for chunk in response.iter_content(chunk_size=1 << 20):
                        tmp.write(chunk)
            tmp_path.replace(dest)
            return
        except (requests.RequestException, OSError) as exc:  # noqa: PERF203
            last_exc = exc
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
    raise RuntimeError(f"failed to download {url!r} after {attempts} attempts") from last_exc


def _ensure_segmentation_model() -> Path:
    root = _cache_dir() / "sherpa-onnx-pyannote-segmentation-3-0"
    dest = root / "model.onnx"
    if dest.exists():
        return dest
    root.mkdir(parents=True, exist_ok=True)
    # Extract into the cache directory itself, not a system temp dir: Path.replace() cannot
    # cross filesystems, and TMPDIR is routinely a different mount from the cache root
    # (containers especially). Staging here keeps the final rename same-filesystem.
    with tempfile.TemporaryDirectory(dir=root) as tmp_dir:
        archive = Path(tmp_dir) / "segmentation.tar.bz2"
        _download(_SEGMENTATION_RELEASE_URL, archive)
        with tarfile.open(archive, "r:bz2") as tar:
            tar.extractall(tmp_dir, filter="data")  # noqa: S202 -- fixed, pinned archive
        extracted = Path(tmp_dir) / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx"
        if not extracted.exists():
            raise RuntimeError(
                f"segmentation archive did not contain the expected model at {extracted.name!r}"
            )
        extracted.replace(dest)
    return dest


def _ensure_embedding_model(name: str) -> Path:
    recipe = _EMBEDDING_RECIPES.get(name)
    if recipe is None:
        raise ValueError(
            f"unknown diarize embedding model {name!r}; choose one of {sorted(_EMBEDDING_RECIPES)}"
        )
    dest = _cache_dir() / f"{name}.onnx"
    if not dest.exists():
        _download(recipe["url"], dest)
    return dest


# Far above any legitimate decode (a 6h meeting decodes to PCM in single-digit minutes) and far
# below the diarize job's own 330-minute timeout, so a stuck decode surfaces as an actionable
# per-episode error instead of silently consuming the run.
DECODE_TIMEOUT_SECONDS = 1800

# ffmpeg echoes its input URL in errors. The diarize path passes a local temp file today, but a
# stderr blob is not the place to find that out -- strip anything credential-shaped before it
# reaches a log or a stored `speakers_error`.
_FFMPEG_SECRET_RE = re.compile(
    r"([?&](?:x-amz-[\w-]+|sig|signature|token|key|password|api[_-]?key)=)[^&\s]+", re.I
)


def _ffmpeg_detail(stderr: bytes | None, *, limit: int = 500) -> str:
    """Return ffmpeg's error text with query-string credentials redacted and length bounded."""
    text = (stderr or b"").decode("utf-8", "replace").strip()
    text = _FFMPEG_SECRET_RE.sub(r"\1<redacted>", text)
    return text[:limit]


def _load_waveform(
    audio_path: Path,
    sample_rate: int,
    *,
    start_seconds: float = 0.0,
    duration_seconds: float | None = None,
):
    """Decode any audio format ffmpeg understands (hosted audio is AAC/M4A) to mono float32
    PCM at the model's expected rate. Reuses the ffmpeg binary this project already requires
    for encoding rather than adding a second audio-decoding dependency.

    `start_seconds`/`duration_seconds` decode only that slice, via ffmpeg's own input-side seek
    (`-ss`/`-t` placed before `-i`) -- the chunked path (`DIARIZE_CHUNK_THRESHOLD_SECONDS`) uses
    this so a long recording is never decoded, and held, in full; the default (the whole file)
    is unchanged from before this parameter existed.
    """
    import numpy as np

    cmd = ["ffmpeg", "-v", "error"]
    if start_seconds > 0:
        cmd += ["-ss", f"{start_seconds:.3f}"]
    if duration_seconds is not None:
        cmd += ["-t", f"{max(0.0, duration_seconds):.3f}"]
    cmd += [
        # Every other ffmpeg call site in this project pins a protocol whitelist; this one is a
        # local temp file, so it gets the *narrowest* form -- no network protocols at all. Without
        # it, a downloaded artifact that is really a manifest (HLS, concat) could make ffmpeg
        # fetch whatever URLs it names, turning a decode into an SSRF primitive.
        "-protocol_whitelist",
        "file,crypto,data",
        "-i",
        str(audio_path),
        "-f",
        "f32le",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-",
    ]
    try:
        result = subprocess.run(  # noqa: S603
            cmd, capture_output=True, check=True, timeout=DECODE_TIMEOUT_SECONDS
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "ffmpeg is required to decode audio for diarization but was not found on PATH"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        # This runs inside the worker before the next `ctx.stop()` check, so an unbounded decode
        # of malformed media would hold its admission slot until the job's own 330-minute timeout.
        raise RuntimeError(
            f"ffmpeg did not finish decoding {audio_path.name!r} within "
            f"{DECODE_TIMEOUT_SECONDS}s; treating the media as undecodable"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"ffmpeg could not decode {audio_path.name!r}: {_ffmpeg_detail(exc.stderr)}"
        ) from exc
    samples = np.frombuffer(result.stdout, dtype=np.float32)
    if samples.size == 0:
        raise RuntimeError(f"ffmpeg decoded no audio samples from {audio_path.name!r}")
    return samples


# onnxruntime's own C++ logger (inside sherpa_onnx's compiled extension) writes severity-
# prefixed lines like "[E:onnxruntime:, sequential_executor.cc:620 ExecuteKernel] ..." straight
# to the process's stderr file descriptor -- it bypasses Python's sys.stderr/logging entirely, and
# there is no severity/callback hook for it anywhere in sherpa_onnx's Python bindings (checked
# against the installed package's `__init__.py` and the compiled `_sherpa_onnx` extension: no
# config class -- `OfflineSpeakerDiarizationConfig`, `OfflineSpeakerSegmentationModelConfig`,
# `SpeakerEmbeddingExtractorConfig` included -- exposes one). A session-level kernel failure (a
# broadcast-shape mismatch inside the segmentation model, seen in production: R7 Diarization run
# #59, GH Actions run 34072536373, "Diarize Denton pilot meetings", 2026-09-07) can log an
# "[E:onnxruntime" line here and *still* return a normal-looking result from `process()` -- no
# Python exception, no visible change to the reported outcome. `_capture_onnxruntime_stderr`
# redirects fd 2 around exactly that call so this module can scan for the marker itself.
_ONNXRUNTIME_ERROR_MARKERS = ("[E:onnxruntime", "[F:onnxruntime")
_ONNXRUNTIME_WARNING_MARKER = "[W:onnxruntime"


@contextlib.contextmanager
def _capture_onnxruntime_stderr():
    """Redirect the OS-level fd 2 to a temp file for the duration of the block.

    Yields a dict that gains a `"data"` key (the raw bytes written to fd 2) once the block
    exits. Only safe to use inside the isolated diarize worker process (`run_diarize_job`,
    run by `NativeDiarizeStage` in its own `ProcessPoolExecutor` worker) -- redirecting fd 2 in
    the parent process would also swallow every other thread's stderr.

    The real fd is restored in a `finally` immediately on exit from the `with` block, before
    control returns to the caller -- so if the wrapped call itself raises, the exception's
    traceback is printed to the real stderr, never captured into the temp file and lost.
    """
    captured: dict[str, bytes] = {}
    sys.stderr.flush()
    original_fd = os.dup(2)
    capture_file = tempfile.TemporaryFile(mode="w+b")
    try:
        os.dup2(capture_file.fileno(), 2)
        try:
            yield captured
        finally:
            sys.stderr.flush()
            os.dup2(original_fd, 2)
    finally:
        os.close(original_fd)
        capture_file.seek(0)
        captured["data"] = capture_file.read()
        capture_file.close()


def _scan_onnxruntime_log(data: bytes) -> tuple[list[str], list[str]]:
    """Split captured stderr bytes into onnxruntime error/fatal- and warning-level lines."""
    lines = data.decode("utf-8", "replace").splitlines()
    errors = [
        line for line in lines if any(marker in line for marker in _ONNXRUNTIME_ERROR_MARKERS)
    ]
    warnings = [line for line in lines if _ONNXRUNTIME_WARNING_MARKER in line]
    return errors, warnings


def _diarize_chunk_count(duration_seconds: float) -> int:
    """ceil(duration / threshold) chunks, so a recording just over the threshold still splits,
    and each chunk lands at or under it -- divided evenly rather than fixed-size, so e.g. a 9h
    file becomes two ~4.5h chunks, not one 8h chunk plus a 1h leftover."""
    return max(1, math.ceil(duration_seconds / DIARIZE_CHUNK_THRESHOLD_SECONDS))


def _naive_split_points(duration_seconds: float, n_chunks: int) -> list[float]:
    return [duration_seconds * k / n_chunks for k in range(1, n_chunks)]


def _nearest_chapter_time(naive: float, chapter_times: Sequence[float]) -> float | None:
    candidates = [t for t in chapter_times if abs(t - naive) <= _CHAPTER_SEARCH_WINDOW_SECONDS]
    return min(candidates, key=lambda t: abs(t - naive)) if candidates else None


def _detect_silences_local(
    audio_path: Path, start: float, duration: float
) -> list[tuple[float, float]]:
    """`ffmpeg silencedetect` over a narrow local window, offset back to the full recording's
    own timeline. A local, already-downloaded temp file, not a remote URL, so this is
    deliberately its own minimal call rather than `citypods.silence.detect_silences` -- that
    carries PTS/container-duration correction machinery for hosted-URL fetches this doesn't
    need. Reuses that module's pure parser (`parse_silences`) for the actual stderr parsing.
    Returns `[]` on any failure (timeout, missing ffmpeg) rather than raising -- a silence probe
    that can't run is not a reason to fail the whole chunk split; the caller falls back to
    splitting at the anchor point directly.
    """
    from citypods.silence import parse_silences

    window_start = max(0.0, start)
    cmd = [
        "ffmpeg",
        "-v",
        "error",
        "-ss",
        f"{window_start:.3f}",
        "-t",
        f"{max(0.0, duration):.3f}",
        "-protocol_whitelist",
        "file,crypto,data",
        "-i",
        str(audio_path),
        "-af",
        f"silencedetect=noise={_SILENCE_NOISE_DB}dB:d={_SILENCE_MIN_DURATION_SECONDS}",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(  # noqa: S603
            cmd, capture_output=True, timeout=DECODE_TIMEOUT_SECONDS
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []
    pairs = parse_silences(result.stderr.decode("utf-8", "replace"))
    return [(s + window_start, e + window_start) for s, e in pairs]


def _find_split_point(audio_path: Path, anchor: float, duration_seconds: float) -> float:
    """The midpoint of the longest detected silence within `_SILENCE_SEARCH_WINDOW_SECONDS` of
    `anchor`, or `anchor` itself if none is found -- splitting somewhere is always better than
    not chunking a recording this long, but silence-free audio right around the exact anchor
    should not block it."""
    window_start = max(0.0, anchor - _SILENCE_SEARCH_WINDOW_SECONDS)
    window_end = min(duration_seconds, anchor + _SILENCE_SEARCH_WINDOW_SECONDS)
    silences = _detect_silences_local(audio_path, window_start, window_end - window_start)
    if not silences:
        return anchor
    longest = max(silences, key=lambda pair: pair[1] - pair[0])
    return (longest[0] + longest[1]) / 2.0


def _pick_split_points(
    audio_path: Path,
    duration_seconds: float,
    n_chunks: int,
    chapter_times: Sequence[float] | None,
) -> list[float]:
    """Return `n_chunks - 1` sorted internal split points: for each naive even split, prefer a
    nearby chapter boundary as the anchor (falling back to the naive point itself), then the
    longest detected silence near that anchor (falling back to the anchor itself)."""
    chapters = sorted(t for t in (chapter_times or []) if 0 < t < duration_seconds)
    points = []
    for naive in _naive_split_points(duration_seconds, n_chunks):
        anchor = _nearest_chapter_time(naive, chapters)
        if anchor is None:
            anchor = naive
        points.append(_find_split_point(audio_path, anchor, duration_seconds))
    return sorted(points)


def _diarize_chunk_ranges(
    duration_seconds: float, split_points: Sequence[float]
) -> list[tuple[float, float]]:
    bounds = [0.0, *split_points, duration_seconds]
    return list(zip(bounds[:-1], bounds[1:], strict=True))


def _mean_embedding(vectors: list[list[float]]):
    import numpy as np

    return np.mean(np.array(vectors), axis=0)


def _cosine_similarity(a, b) -> float:
    import numpy as np

    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b) / denom) if denom else -1.0


def _merge_chunk_clusters(
    chunk_cluster_embeddings: dict[tuple[int, str], Any],
) -> dict[tuple[int, str], str]:
    """Union-find over a cosine-similarity graph: connect `(chunk_idx, local_cluster)` pairs
    whose mean embeddings are similar enough (`CHUNK_MERGE_MIN_COSINE`), then take connected
    components as global speaker ids. A pair with no embedding at all (extraction failed, or was
    never attempted, for every turn in that cluster) never gets an edge, so it always keeps its
    own unmerged id -- conservative, matching "no embedding, no identity" elsewhere in this
    module rather than guessing at a merge with nothing to compare.
    """
    keys = list(chunk_cluster_embeddings.keys())
    parent: dict[tuple[int, str], tuple[int, str]] = {k: k for k in keys}

    def find(k: tuple[int, str]) -> tuple[int, str]:
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    def union(a: tuple[int, str], b: tuple[int, str]) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(len(keys)):
        embedding_i = chunk_cluster_embeddings[keys[i]]
        if embedding_i is None:
            continue
        for j in range(i + 1, len(keys)):
            embedding_j = chunk_cluster_embeddings[keys[j]]
            if embedding_j is None:
                continue
            if _cosine_similarity(embedding_i, embedding_j) >= CHUNK_MERGE_MIN_COSINE:
                union(keys[i], keys[j])

    # Stable, readable global ids in order of first appearance, not the arbitrary root key.
    global_id_by_root: dict[tuple[int, str], str] = {}
    result: dict[tuple[int, str], str] = {}
    for k in keys:
        root = find(k)
        if root not in global_id_by_root:
            global_id_by_root[root] = str(len(global_id_by_root))
        result[k] = global_id_by_root[root]
    return result


def _run_diarize_pass(diarizer: Any, samples: Any, audio_path: Path) -> list[Any]:
    """`diarizer.process(samples)` with onnxruntime error detection; returns the segments,
    sorted by start time. Shared by the single-pass and chunked paths so both get the exact
    same check."""
    with _capture_onnxruntime_stderr() as captured:
        result = diarizer.process(samples)
    error_lines, warning_lines = _scan_onnxruntime_log(captured.get("data", b""))
    if warning_lines:
        # Not fatal on its own, but worth surfacing: at minimum a maintainer reading the job's
        # output should see that onnxruntime itself flagged something during this run.
        print(
            f"[diarize] onnxruntime warning during process() for {audio_path.name!r}: "
            f"{warning_lines[0][:500]}",
            flush=True,
        )
    if error_lines:
        # A session-level kernel failure like this does not raise a Python exception and does
        # not change `process()`'s return value -- without this check the job below would be
        # accepted and published as a normal success (production evidence: R7 run #59, GH
        # Actions run 34072536373, review/31 §A.1b). Raising here routes it through the same
        # per-item exception handling `NativeDiarizeStage._run_one()` already has for every
        # other diarize failure, marking the episode `speakers_error` instead of `speakers_synced`.
        raise RuntimeError(
            f"onnxruntime reported {len(error_lines)} error-level diagnostic(s) during "
            f"diarize() for {audio_path.name!r}: {error_lines[0][:500]}"
        )
    return list(result.sort_by_start_time())


def _segments_to_turns(segments: list[Any]) -> tuple[list[dict[str, Any]], dict[str, dict]]:
    turns: list[dict[str, Any]] = []
    clusters: dict[str, dict[str, Any]] = {}
    for segment in segments:
        cluster = str(segment.speaker)
        turns.append(
            {
                "start": float(segment.start),
                "end": float(segment.end),
                "cluster": cluster,
                "overlap": False,
            }
        )
        clusters.setdefault(cluster, {"cluster": cluster, "turn_count": 0})["turn_count"] += 1
    return turns, clusters


def _build_diarize_config(
    segmentation_path: Path,
    embedding_path: Path,
    *,
    threshold: float,
    shift_ratio: float,
    num_threads: int,
) -> Any:
    import sherpa_onnx

    return sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=str(segmentation_path), window_shift_ratio=shift_ratio
            ),
            num_threads=num_threads,
            provider="cpu",
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(embedding_path), num_threads=num_threads, provider="cpu"
        ),
        # -1 = auto-detect speaker count from the distance threshold -- production doesn't
        # know true speaker counts in advance any more than the trial did.
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=threshold),
    )


def _diarize_chunked(
    audio_path: Path,
    *,
    model: str,
    embedding_name: str,
    config: Any,
    embedding_path: Path,
    num_threads: int,
    recording_seconds: float,
    chapter_times: Sequence[float] | None,
) -> DiarizeArtifacts:
    """Process a recording longer than `DIARIZE_CHUNK_THRESHOLD_SECONDS` in multiple chunks, so
    peak memory (and the maximum possible single continuous turn) is bounded to one chunk's
    worth regardless of total length -- see that constant's comment for why. External contract
    is identical to the single-pass path: one `DiarizeArtifacts` for the whole recording, same
    content-addressed shape; chunking is entirely internal to this function.
    """
    import sherpa_onnx

    n_chunks = _diarize_chunk_count(recording_seconds)
    split_points = _pick_split_points(audio_path, recording_seconds, n_chunks, chapter_times)
    ranges = _diarize_chunk_ranges(recording_seconds, split_points)
    print(
        f"[diarize] {recording_seconds:.0f}s recording split into {len(ranges)} chunk(s) at "
        f"{[round(p, 1) for p in split_points]}",
        flush=True,
    )

    all_turns: list[dict[str, Any]] = []
    # (chunk_idx, local_cluster) -> mean embedding (or None if that cluster never got one), fed
    # to _merge_chunk_clusters once every chunk has run.
    chunk_cluster_embeddings: dict[tuple[int, str], Any] = {}

    for chunk_idx, (true_start, true_end) in enumerate(ranges):
        # Padding on internal boundaries only -- the recording's own true start/end never need
        # extra context, and padding either would just decode past the recording's own edges.
        pad_before = 0.0 if chunk_idx == 0 else _CHUNK_OVERLAP_SECONDS
        pad_after = 0.0 if chunk_idx == len(ranges) - 1 else _CHUNK_OVERLAP_SECONDS
        decode_start = max(0.0, true_start - pad_before)
        decode_end = min(recording_seconds, true_end + pad_after)

        diarizer = sherpa_onnx.OfflineSpeakerDiarization(config)
        sample_rate = diarizer.sample_rate
        samples = _load_waveform(
            audio_path,
            sample_rate,
            start_seconds=decode_start,
            duration_seconds=decode_end - decode_start,
        )
        segments = _run_diarize_pass(diarizer, samples, audio_path)

        # Keep chunk-local times (matching `samples`' own indexing) through embedding
        # extraction; only remap to the full recording's timeline afterward.
        local_turns: list[dict[str, Any]] = []
        for segment in segments:
            local_start = float(segment.start)
            global_start = local_start + decode_start
            # Drop anything starting in this chunk's own overlap padding -- the neighboring
            # chunk that actually owns that region already covers it (or will).
            if global_start < true_start or global_start >= true_end:
                continue
            local_turns.append(
                {
                    "start": local_start,
                    "end": float(segment.end),
                    "cluster": str(segment.speaker),
                }
            )
        _attach_embeddings(
            samples, sample_rate, local_turns, embedding_path, num_threads=num_threads
        )

        by_local_cluster: dict[str, list] = {}
        for turn in local_turns:
            if "embedding" in turn:
                by_local_cluster.setdefault(turn["cluster"], []).append(turn["embedding"])
            turn["start"] += decode_start
            turn["end"] += decode_start
            turn["overlap"] = False
            turn["_chunk_idx"] = chunk_idx
        for local_cluster in {t["cluster"] for t in local_turns}:
            embeddings = by_local_cluster.get(local_cluster)
            chunk_cluster_embeddings[(chunk_idx, local_cluster)] = (
                _mean_embedding(embeddings) if embeddings else None
            )
        all_turns.extend(local_turns)

    global_id_by_key = _merge_chunk_clusters(chunk_cluster_embeddings)
    clusters: dict[str, dict[str, Any]] = {}
    for turn in all_turns:
        key = (turn.pop("_chunk_idx"), turn["cluster"])
        turn["cluster"] = global_id_by_key[key]
        clusters.setdefault(turn["cluster"], {"cluster": turn["cluster"], "turn_count": 0})[
            "turn_count"
        ] += 1
    all_turns.sort(key=lambda t: t["start"])
    # Assessed globally, once, on the fully merged timeline -- a per-chunk pass would miss a
    # turn from one chunk overlapping a turn from its neighbor right at the boundary.
    _mark_overlap(all_turns)
    return DiarizeArtifacts(
        turns=all_turns,
        clusters=list(clusters.values()),
        engine="sherpa-onnx",
        model=f"{model}+{embedding_name}",
    )


def diarize(
    audio_path: Path,
    model: str = DEFAULT_DIARIZE_MODEL,
    *,
    embedding_model: str | None = DEFAULT_EMBEDDING_MODEL,
    token: str | None = None,
    device: str | None = None,
    num_threads: int = 2,
    clustering_threshold: float | None = None,
    window_shift_ratio: float | None = None,
    recording_seconds: float | None = None,
    chapter_times: Sequence[float] | None = None,
) -> DiarizeArtifacts:
    """Run sherpa-onnx lazily and normalize its labels to meeting-local clusters.

    `token` and `device` are accepted but unused: no model here is Hugging-Face-gated, and this
    engine is CPU-only by design (the throughput win is many single-threaded worker processes,
    not GPU offload -- review/31 §A.4). They stay in the signature so an already-registered
    dispatch backend's `InferenceJob` input shape doesn't have to change in lockstep.

    `num_threads` defaults to 2 -- the measured single-job latency optimum (review/31 §A.4) --
    for a bare/ad-hoc call. The concurrent worker-pool scheduler (`NativeDiarizeStage`) passes
    `num_threads=1` explicitly: throughput across many concurrent single-threaded workers beat
    every other split tested, including this same 2-thread-per-job optimum run four-wide.

    `window_shift_ratio` defaults to `DEFAULT_WINDOW_SHIFT_RATIO`, not sherpa-onnx's own 0.1 --
    see that constant's comment for why.

    `recording_seconds`, when supplied and above `DIARIZE_CHUNK_THRESHOLD_SECONDS`, switches to
    the chunked path (`_diarize_chunked`) instead of this function's own single-pass one --
    `None` (an ad-hoc/test call with no duration hint) always takes the single-pass path,
    the same safe, already-validated behavior as before this parameter existed.
    `chapter_times` (episode chapter-marker seconds, if any) only affects where chunk
    boundaries land when chunking is used; it is otherwise ignored.
    """
    del token, device  # documented above; named for call-site compatibility only

    # Validate the recipe name before importing sherpa-onnx: a typo in config should say so,
    # not report the heavy optional dependency as missing (and CI, which installs `[dev]` and
    # not `[diarize]`, can then test this branch at all).
    embedding_name = embedding_model or DEFAULT_EMBEDDING_MODEL
    recipe = _EMBEDDING_RECIPES.get(embedding_name)
    if recipe is None:
        raise ValueError(
            f"unknown diarize embedding model {embedding_name!r}; "
            f"choose one of {sorted(_EMBEDDING_RECIPES)}"
        )
    # Only one segmentation model exists today, and `_ensure_segmentation_model()` loads it
    # unconditionally -- so a different `model` value would change the content-addressed spec hash
    # and the reported `DiarizeArtifacts.model` without changing a single inference. That is false
    # provenance plus a pointless re-diarization of the whole catalog. Reject it until a validated
    # segmentation-recipe mapping exists; `scripts/preflight_diarization.py` already refuses the
    # same value, but only in the workflow, and this is the call every path goes through.
    if model != DEFAULT_DIARIZE_MODEL:
        raise ValueError(
            f"unknown diarize segmentation model {model!r}; only {DEFAULT_DIARIZE_MODEL!r} is "
            "implemented, and a different value would change artifact keys without changing "
            "inference"
        )

    threshold = (
        clustering_threshold if clustering_threshold is not None else recipe["clustering_threshold"]
    )
    shift_ratio = (
        window_shift_ratio if window_shift_ratio is not None else DEFAULT_WINDOW_SHIFT_RATIO
    )

    segmentation_path = _ensure_segmentation_model()
    embedding_path = _ensure_embedding_model(embedding_name)
    config = _build_diarize_config(
        segmentation_path,
        embedding_path,
        threshold=threshold,
        shift_ratio=shift_ratio,
        num_threads=num_threads,
    )
    if not config.validate():
        raise RuntimeError(f"invalid sherpa-onnx diarize config for embedding {embedding_name!r}")

    if recording_seconds is not None and recording_seconds > DIARIZE_CHUNK_THRESHOLD_SECONDS:
        return _diarize_chunked(
            audio_path,
            model=model,
            embedding_name=embedding_name,
            config=config,
            embedding_path=embedding_path,
            num_threads=num_threads,
            recording_seconds=recording_seconds,
            chapter_times=chapter_times,
        )

    import sherpa_onnx

    diarizer = sherpa_onnx.OfflineSpeakerDiarization(config)
    samples = _load_waveform(audio_path, diarizer.sample_rate)
    segments = _run_diarize_pass(diarizer, samples, audio_path)
    turns, clusters = _segments_to_turns(segments)
    _mark_overlap(turns)
    _attach_embeddings(
        samples, diarizer.sample_rate, turns, embedding_path, num_threads=num_threads
    )
    return DiarizeArtifacts(
        turns=turns,
        clusters=list(clusters.values()),
        engine="sherpa-onnx",
        model=f"{model}+{embedding_name}",
    )


def prepare_models(embedding_model: str | None = DEFAULT_EMBEDDING_MODEL) -> None:
    """Populate the model cache once, before any worker process needs it.

    The worker pool (review/31 §A.4) spawns several processes that would otherwise each miss the
    cache and download the same ~46MB concurrently. `_download` is atomic so a race is safe, just
    wasteful; warming here in the parent makes it a single fetch.
    """
    _ensure_segmentation_model()
    _ensure_embedding_model(embedding_model or DEFAULT_EMBEDDING_MODEL)


def run_diarize_job(
    audio_path: str,
    *,
    model: str = DEFAULT_DIARIZE_MODEL,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    num_threads: int = 1,
    clustering_threshold: float | None = None,
    window_shift_ratio: float | None = None,
    recording_seconds: float | None = None,
    chapter_times: Sequence[float] | None = None,
) -> DiarizeArtifacts:
    """Module-level worker entry point for the diarize process pool.

    Must stay importable and take only picklable arguments: the pool uses a `spawn` context, so
    this is re-imported in a fresh interpreter rather than inherited by fork (`fork` from a
    process with live threads is exactly the deadlock hazard CPython warns about, and the enrich
    run always has a heartbeat thread running). `recording_seconds`/`chapter_times` are plain
    `float`s, so they stay picklable too.

    Deliberately calls `diarize()` directly rather than routing through
    `citypods.compute.local.LocalBackend`: that adapter lazily imports `citypods.asr` (and with
    it faster-whisper) for its transcribe/align verbs, which a diarize-only worker has no reason
    to pay for, and its dispatch seam does not yet materialize R7 artifacts anyway.
    """
    return diarize(
        Path(audio_path),
        model,
        embedding_model=embedding_model,
        num_threads=num_threads,
        clustering_threshold=clustering_threshold,
        window_shift_ratio=window_shift_ratio,
        recording_seconds=recording_seconds,
        chapter_times=chapter_times,
    )


def attach_transcript_words(turns: list[dict[str, Any]], words: Mapping[str, Any]) -> None:
    """Attach transcript-derived evidence hashes to served-time turns.

    The hosted audio and ASR/aligned word sidecar are both already on the served clock.  The
    artifact records only the count and SHA-256 of words intersecting each turn, which lets a
    reviewed golden reference prove the exact text version without duplicating transcript text.
    """
    timed_words = list(_timed_words(words))
    for turn in turns:
        start, end = turn.get("start"), turn.get("end")
        if not isinstance(start, int | float) or not isinstance(end, int | float):
            continue
        selected = [
            text
            for word_start, word_end, text in timed_words
            if word_end > float(start) and word_start < float(end)
        ]
        if selected:
            normalized = " ".join(selected)
            turn["transcript_word_count"] = len(selected)
            turn["transcript_text_hash"] = hashlib.sha256(normalized.encode()).hexdigest()


def _timed_words(words: Mapping[str, Any]) -> Iterable[tuple[float, float, str]]:
    """Read both compact Citypods and WhisperX word-sidecar shapes."""
    rows = words.get("word_segments") or words.get("words") or []
    if not isinstance(rows, list):
        rows = []
    if not rows:
        for segment in words.get("segments") or []:
            if isinstance(segment, Mapping) and isinstance(segment.get("words"), list):
                rows.extend(segment["words"])
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        start = row.get("start", row.get("s"))
        end = row.get("end", row.get("e"))
        text = row.get("word", row.get("w", row.get("text", "")))
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, int | float)
            or not isinstance(end, int | float)
            or not isinstance(text, str)
            or not text.strip()
        ):
            continue
        try:
            start_value = float(start)
            end_value = float(end)
        except OverflowError:
            continue
        if math.isfinite(start_value) and math.isfinite(end_value) and end_value > start_value:
            yield start_value, end_value, text.strip()


def _mark_overlap(turns: list[dict[str, Any]]) -> None:
    """Flag every turn intersecting another diarization turn, in served time."""
    active: list[dict[str, Any]] = []
    for turn in sorted(turns, key=lambda row: float(row["start"])):
        start = float(turn["start"])
        active = [other for other in active if float(other["end"]) > start]
        for other in active:
            if float(other["end"]) > start:
                turn["overlap"] = True
                other["overlap"] = True
        active.append(turn)


def _attach_embeddings(
    samples,
    sample_rate: int,
    turns: list[dict[str, Any]],
    embedding_path: Path,
    *,
    num_threads: int,
) -> None:
    """Best-effort per-turn embeddings for the separate R7 identity layer -- except for an
    onnxruntime-internal error, which is not best-effort here.

    A missing/unusable embedding model, or a turn too short to extract from, leaves that turn
    (or, for a model failure, every turn) anonymous rather than failing the content-addressed
    diarization artifact -- same contract the prior pyannote adapter made. An onnxruntime
    error-level diagnostic during extraction is different and is deliberately NOT swallowed:
    the same reasoning as `diarize()`'s own check on `process()` (below) applies here too -- a
    session-level kernel failure can log to stderr without raising a catchable exception or
    changing the call's return value, so "no embedding, keep going" would silently accept a
    corrupted result. That is not just a lost identity signal here: a degenerate, anomalously
    long turn (a segmentation artifact) fed whole into one embedding-extraction call is the
    most likely real source of the recurring "Where node" broadcast error production has seen
    (review/31 §A.4's 2026-09-07 chunking addendum) -- `process()`'s own check never covers this
    call, since it happens afterward, so this was silently unprotected until now.
    """
    try:
        import sherpa_onnx

        extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(embedding_path), num_threads=num_threads, provider="cpu"
            )
        )
    except Exception:  # noqa: BLE001 - no extractor means no embeddings, not failed diarization.
        return

    with _capture_onnxruntime_stderr() as captured:
        for turn in turns:
            try:
                start_idx = max(0, int(float(turn["start"]) * sample_rate))
                end_idx = min(len(samples), int(float(turn["end"]) * sample_rate))
                if end_idx <= start_idx:
                    continue
                stream = extractor.create_stream()
                stream.accept_waveform(sample_rate, samples[start_idx:end_idx])
                stream.input_finished()
                if not extractor.is_ready(stream):
                    continue
                values = extractor.compute(stream)
                if values:
                    turn["embedding"] = [float(value) for value in values]
            except Exception:  # noqa: BLE001 - one bad turn stays anonymous; others still try.
                continue

    error_lines, warning_lines = _scan_onnxruntime_log(captured.get("data", b""))
    if warning_lines:
        print(
            f"[diarize] onnxruntime warning during embedding extraction: {warning_lines[0][:500]}",
            flush=True,
        )
    if error_lines:
        raise RuntimeError(
            f"onnxruntime reported {len(error_lines)} error-level diagnostic(s) during "
            f"embedding extraction: {error_lines[0][:500]}"
        )


__all__ = [
    "CHUNK_MERGE_MIN_COSINE",
    "DEFAULT_DIARIZE_MODEL",
    "DEFAULT_EMBEDDING_MODEL",
    "DIARIZE_CHUNK_THRESHOLD_SECONDS",
    "DIARIZE_RSS_BASE_BYTES",
    "DIARIZE_RSS_PER_HOUR_BYTES",
    "DiarizeArtifacts",
    "diarize",
    "estimate_diarize_rss_bytes",
    "prepare_models",
    "run_diarize_job",
]
