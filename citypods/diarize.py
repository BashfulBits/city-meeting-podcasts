"""Lazy sherpa-onnx adapter for R7 native diarization.

Engine: pyannote-segmentation-3.0 (VAD/segmentation) + a swappable speaker-embedding model,
both non-gated ONNX exports from sherpa-onnx's own model releases -- no Hugging Face auth
needed. Superseded the pyannote-audio engine on 2026-09-06 (review/31 §A.1a): an offline
trial found NeMo TitaNet-Small matches pyannote's measured accuracy at 8-13x its CPU speed,
which is what actually removes the long-meeting CPU budget ceiling that motivated the switch.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
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


def _load_waveform(audio_path: Path, sample_rate: int):
    """Decode any audio format ffmpeg understands (hosted audio is AAC/M4A) to mono float32
    PCM at the model's expected rate. Reuses the ffmpeg binary this project already requires
    for encoding rather than adding a second audio-decoding dependency."""
    import numpy as np

    cmd = [
        "ffmpeg",
        "-v",
        "error",
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
        result = subprocess.run(cmd, capture_output=True, check=True)  # noqa: S603
    except FileNotFoundError as exc:
        raise RuntimeError(
            "ffmpeg is required to decode audio for diarization but was not found on PATH"
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(f"ffmpeg could not decode {audio_path.name!r}: {detail}") from exc
    samples = np.frombuffer(result.stdout, dtype=np.float32)
    if samples.size == 0:
        raise RuntimeError(f"ffmpeg decoded no audio samples from {audio_path.name!r}")
    return samples


def diarize(
    audio_path: Path,
    model: str = DEFAULT_DIARIZE_MODEL,
    *,
    embedding_model: str | None = DEFAULT_EMBEDDING_MODEL,
    token: str | None = None,
    device: str | None = None,
    num_threads: int = 2,
    clustering_threshold: float | None = None,
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
    import sherpa_onnx

    threshold = (
        clustering_threshold if clustering_threshold is not None else recipe["clustering_threshold"]
    )

    segmentation_path = _ensure_segmentation_model()
    embedding_path = _ensure_embedding_model(embedding_name)

    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=str(segmentation_path)
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
    result = diarizer.process(samples)

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
    "DiarizeArtifacts",
    "diarize",
]
