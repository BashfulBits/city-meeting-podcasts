"""Lazy pyannote adapter for R7 native diarization."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_DIARIZE_MODEL = "pyannote/speaker-diarization-community-1"
DEFAULT_EMBEDDING_MODEL = "pyannote/embedding"


@dataclass(frozen=True)
class DiarizeArtifacts:
    """Engine-neutral, source/served-time speaker clustering output."""

    turns: list[dict[str, Any]]
    clusters: list[dict[str, Any]]
    engine: str
    model: str


def diarize(
    audio_path: Path,
    model: str = DEFAULT_DIARIZE_MODEL,
    *,
    embedding_model: str | None = DEFAULT_EMBEDDING_MODEL,
    token: str | None = None,
    device: str | None = None,
) -> DiarizeArtifacts:
    """Run pyannote lazily and normalize its labels to meeting-local clusters.

    The pyannote model remains an implementation detail.  R7 stores no provider-specific
    annotation object, so a later WeSpeaker benchmark/fallback can return this same shape.
    """
    try:
        from pyannote.audio import Pipeline
    except ImportError as exc:  # pragma: no cover - exercised in ASR image, mocked in unit tests.
        raise RuntimeError("R7 diarization requires the pinned pyannote ASR dependency") from exc
    pipeline = Pipeline.from_pretrained(model, token=token)
    if device:
        try:
            import torch

            pipeline.to(torch.device(device))
        except (ImportError, AttributeError):
            pass
    output = pipeline(str(audio_path))
    # pyannote.audio 3.x returned an Annotation directly; Community-1 (pyannote.audio 4.x)
    # returns a structured output with regular and exclusive diarization annotations.
    annotation = getattr(output, "speaker_diarization", output)
    turns: list[dict[str, Any]] = []
    clusters: dict[str, dict[str, Any]] = {}
    for segment, _, label in annotation.itertracks(yield_label=True):
        cluster = str(label)
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
    if embedding_model:
        _attach_embeddings(audio_path, turns, embedding_model, token=token, device=device)
    return DiarizeArtifacts(
        turns=turns, clusters=list(clusters.values()), engine="pyannote", model=model
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
        if isinstance(start, int | float) and isinstance(end, int | float) and str(text).strip():
            yield float(start), float(end), str(text).strip()


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
    audio_path: Path,
    turns: list[dict[str, Any]],
    model: str,
    *,
    token: str | None,
    device: str | None,
) -> None:
    """Best-effort per-turn embeddings for the separate R7 identity layer.

    Diarization is still useful when an embedding model is unavailable (for example a model
    access approval has not yet been accepted), so this intentionally leaves turns anonymous
    rather than failing the content-addressed diarization artifact.
    """
    try:
        import torch
        from pyannote.audio import Inference, Model
        from pyannote.core import Segment

        inference = Inference(
            Model.from_pretrained(model, token=token),
            window="whole",
            device=torch.device(device) if device else None,
        )
        for turn in turns:
            vector = inference.crop(
                str(audio_path), Segment(float(turn["start"]), float(turn["end"]))
            )
            values = vector.tolist() if hasattr(vector, "tolist") else list(vector)
            if isinstance(values, list) and values and not isinstance(values[0], list):
                turn["embedding"] = [float(value) for value in values]
    except Exception:  # noqa: BLE001 - no embedding means no identity, not failed diarization.
        return


__all__ = [
    "DEFAULT_DIARIZE_MODEL",
    "DEFAULT_EMBEDDING_MODEL",
    "DiarizeArtifacts",
    "diarize",
]
