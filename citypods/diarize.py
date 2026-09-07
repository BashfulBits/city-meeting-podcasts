"""Lazy sherpa-onnx adapter for R7 native diarization.

Engine: pyannote-segmentation-3.0 (VAD/segmentation) + a swappable speaker-embedding model,
both non-gated ONNX exports from sherpa-onnx's own model releases -- no Hugging Face auth
needed. Superseded the pyannote-audio engine on 2026-09-06 (review/31 §A.1a): an offline
trial found NeMo TitaNet-Small matches pyannote's measured accuracy at 8-13x its CPU speed,
which is what actually removes the long-meeting CPU budget ceiling that motivated the switch.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterable, Mapping
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

# A single onnxruntime kernel failure can attempt a large, transient allocation before it fails
# or logs an error -- on top of whatever steady-state RSS the job already holds at that moment,
# which the linear model above has no way to see coming. Concrete evidence this isn't
# hypothetical: R7 runs #59/#61/#62/#63 each logged the same "Where node" broadcast error with
# dimensions large enough to imply a sizeable attempted allocation -- run #59's `12288 by 15974`
# and runs #61-63's `12288 by 50599` would each need up to `12288 * 50599 * 4` bytes (float32)
# =~ 2.49GiB for that one intermediate tensor, and run #63's own heartbeat showed only ~3.0GiB of
# genuinely free memory at the moment its error fired -- close enough to that figure that a spike
# of roughly this size landing right then plausibly would have been the tipping point. Subtracted
# once from the diarize memory ceiling (not once per worker -- the trigger is data-dependent and
# rare, not a certainty every worker hits simultaneously) so admission always keeps this much
# genuinely spare, regardless of how full the steady-state accounting says the budget already is.
DIARIZE_RSS_SPIKE_MARGIN_BYTES = 3 * 1024 * 1024 * 1024


def estimate_diarize_rss_bytes(recording_seconds: float) -> int:
    """Predict one diarize worker's peak RSS for `recording_seconds` of audio."""
    hours = max(0.0, float(recording_seconds)) / 3600.0
    return DIARIZE_RSS_BASE_BYTES + int(DIARIZE_RSS_PER_HOUR_BYTES * hours)


def diarize_memory_ceiling_bytes(configured_budget_mb: float) -> int:
    """The diarize `MemoryReservation` ceiling actually used, after reserving
    `DIARIZE_RSS_SPIKE_MARGIN_BYTES` of headroom for a spike the steady-state model can't see
    coming (that constant's own comment has the evidence). `configured_budget_mb <= 0` means
    "admission disabled" (the existing `speakers.memory_budget_mb: 0` contract) and passes
    through unchanged -- it is not a budget to apply a safety margin to. A configured budget
    smaller than the margin clamps to 0 (also disabled) rather than going negative.
    """
    if configured_budget_mb <= 0:
        return 0
    margin_mb = DIARIZE_RSS_SPIKE_MARGIN_BYTES / (1024 * 1024)
    return int(max(0.0, configured_budget_mb - margin_mb) * 1024 * 1024)


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


def _load_waveform(audio_path: Path, sample_rate: int):
    """Decode any audio format ffmpeg understands (hosted audio is AAC/M4A) to mono float32
    PCM at the model's expected rate. Reuses the ffmpeg binary this project already requires
    for encoding rather than adding a second audio-decoding dependency."""
    import numpy as np

    cmd = [
        "ffmpeg",
        "-v",
        "error",
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

    import sherpa_onnx

    threshold = (
        clustering_threshold if clustering_threshold is not None else recipe["clustering_threshold"]
    )
    shift_ratio = (
        window_shift_ratio if window_shift_ratio is not None else DEFAULT_WINDOW_SHIFT_RATIO
    )

    segmentation_path = _ensure_segmentation_model()
    embedding_path = _ensure_embedding_model(embedding_name)

    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
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
    if not config.validate():
        raise RuntimeError(f"invalid sherpa-onnx diarize config for embedding {embedding_name!r}")
    diarizer = sherpa_onnx.OfflineSpeakerDiarization(config)

    samples = _load_waveform(audio_path, diarizer.sample_rate)
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

    turns: list[dict[str, Any]] = []
    clusters: dict[str, dict[str, Any]] = {}
    for segment in result.sort_by_start_time():
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


def _peak_rss_mb() -> float | None:
    """This worker process's peak RSS so far, in MiB, or `None` if unavailable (see
    `citypods.resources.process_peak_rss_bytes` for why peak rather than current RSS)."""
    from citypods.resources import process_peak_rss_bytes

    peak_bytes = process_peak_rss_bytes()
    return peak_bytes / (1024 * 1024) if peak_bytes is not None else None


def run_diarize_job(
    audio_path: str,
    *,
    model: str = DEFAULT_DIARIZE_MODEL,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    num_threads: int = 1,
    clustering_threshold: float | None = None,
    window_shift_ratio: float | None = None,
    log_label: str = "",
) -> DiarizeArtifacts:
    """Module-level worker entry point for the diarize process pool.

    Must stay importable and take only picklable arguments: the pool uses a `spawn` context, so
    this is re-imported in a fresh interpreter rather than inherited by fork (`fork` from a
    process with live threads is exactly the deadlock hazard CPython warns about, and the enrich
    run always has a heartbeat thread running).

    Deliberately calls `diarize()` directly rather than routing through
    `citypods.compute.local.LocalBackend`: that adapter lazily imports `citypods.asr` (and with
    it faster-whisper) for its transcribe/align verbs, which a diarize-only worker has no reason
    to pay for, and its dispatch seam does not yet materialize R7 artifacts anyway.

    `log_label` (normally the episode `uid`) is only for the peak-RSS log line below -- purely
    diagnostic, no effect on the diarize call itself, so existing callers that don't pass it are
    unaffected. Peak RSS is logged (not just returned) because a hard kernel OOM-kill of this
    process never lets any of this code run at all -- the log line only covers the case where
    the process survives long enough to return or raise normally; see the accompanying
    workflow-level `dmesg` capture for the case where it doesn't (review/31 §A.4 addendum).
    """
    try:
        artifact = diarize(
            Path(audio_path),
            model,
            embedding_model=embedding_model,
            num_threads=num_threads,
            clustering_threshold=clustering_threshold,
            window_shift_ratio=window_shift_ratio,
        )
    except (MemoryError, OSError) as exc:
        # `MemoryError` is CPython failing to satisfy a `malloc` and recovering enough to raise
        # it -- genuine evidence of memory exhaustion, not a guess. `OSError` is included for the
        # same allocation failure surfacing from native code (numpy/onnxruntime) as errno ENOMEM
        # instead of a Python-level MemoryError; any other OSError re-raises unchanged rather than
        # being misreported as a memory issue it may have nothing to do with.
        if isinstance(exc, OSError) and getattr(exc, "errno", None) != errno.ENOMEM:
            raise
        peak_mb = _peak_rss_mb()
        raise RuntimeError(
            f"diarize worker {log_label!r} hit {type(exc).__name__} "
            f"(peak_rss_mb={peak_mb if peak_mb is None else round(peak_mb, 1)}): {exc}"
        ) from exc
    peak_mb = _peak_rss_mb()
    print(
        f"[diarize] peak_rss_mb={peak_mb if peak_mb is None else round(peak_mb, 1)} "
        f"label={log_label!r}",
        flush=True,
    )
    return artifact


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
    """Best-effort per-turn embeddings for the separate R7 identity layer.

    Diarization is still useful when embedding extraction fails for any reason, so this
    intentionally leaves turns anonymous rather than failing the content-addressed diarization
    artifact -- same contract the prior pyannote adapter made.
    """
    try:
        import sherpa_onnx

        extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(embedding_path), num_threads=num_threads, provider="cpu"
            )
        )
        for turn in turns:
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
    except Exception:  # noqa: BLE001 - no embedding means no identity, not failed diarization.
        return


__all__ = [
    "DEFAULT_DIARIZE_MODEL",
    "DEFAULT_EMBEDDING_MODEL",
    "DIARIZE_RSS_BASE_BYTES",
    "DIARIZE_RSS_PER_HOUR_BYTES",
    "DIARIZE_RSS_SPIKE_MARGIN_BYTES",
    "DiarizeArtifacts",
    "diarize_memory_ceiling_bytes",
    "diarize",
    "estimate_diarize_rss_bytes",
    "prepare_models",
    "run_diarize_job",
]
