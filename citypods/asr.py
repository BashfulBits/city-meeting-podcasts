"""ASR (Automatic Speech Recognition) helpers for transcript generation.

Two public entry points:

  transcribe(audio_path, ...)  — Path B: fresh Whisper generation.
  align(audio_path, text, ...) — Path A: forced alignment via stable-ts, preserving
                                  the exact wording of an existing source transcript.

Both return a :class:`TranscriptArtifacts` (a clean segment-level VTT + a word-level JSON
sidecar) in *served time* (caller downloads the hosted M4A before calling).

All third-party imports (faster_whisper, stable_whisper) are lazy — the module loads
cleanly without the [asr] optional extras; a missing import surfaces only when
transcribe() / align() is actually called.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path

# ── Module-level model cache ─────────────────────────────────────────────────
# The Whisper model is large (~800 MB) and downloading / loading it is expensive.
# We cache it here (keyed by model name + compute type + cpu_threads) so it is
# loaded at most ONCE per process, regardless of how many sources are processed in
# parallel.  The lock ensures only one thread downloads/loads on a cold start;
# all others wait and then reuse the cached instance.
_model_cache: dict[tuple, object] = {}
_model_lock = threading.Lock()


@dataclass(frozen=True)
class _LoadedAsrModel:
    """A loaded faster-whisper model plus the recipe needed for stable-ts alignment."""

    model_or_path: str
    compute_type: str
    cpu_threads: int
    transcriber: object


def _configured_model_or_path(model: str) -> str:
    """Resolve the configured model through the runtime ASR_MODEL_PATH override."""
    return os.environ.get("ASR_MODEL_PATH") or model


# ── VTT helpers ─────────────────────────────────────────────────────────────


def _fmt_ts(seconds: float) -> str:
    """Format seconds as HH:MM:SS.mmm for WebVTT cue timestamps."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


# Schema version of the word-level JSON sidecar (independent of ASR_PIPELINE_VERSION, which
# keys re-transcription). Bump only when the JSON *shape* changes.
_WORDS_SCHEMA_VERSION = "1"


@dataclass(frozen=True)
class TranscriptArtifacts:
    """The two outputs of ASR for one episode:

    * ``vtt``   — clean **segment-level** WebVTT, served to podcast apps via
      ``<podcast:transcript>`` (one readable cue per utterance; small).
    * ``words`` — a **word-level** JSON sidecar consumed server-side only (phrase search,
      clip selection, diarization). Never served as the podcast transcript.
    """

    vtt: bytes
    words: bytes


def _to_vtt(segments) -> bytes:
    """Convert ASR segments to **segment-level** WebVTT bytes (one readable cue per
    utterance). Word-level timing is emitted separately by :func:`_to_words_json`."""
    lines = ["WEBVTT", ""]
    for seg in segments:
        text = (seg.text or "").strip()
        if not text:
            continue
        lines.append(f"{_fmt_ts(seg.start)} --> {_fmt_ts(seg.end)}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines).encode("utf-8")


def _to_words_json(segments, basis: str = "served") -> bytes:
    """Build the word-level JSON sidecar from ASR segments.

    Shape: ``{"schema", "basis", "segments": [{"start","end","text",
    "words": [{"w","s","e"}]}]}`` — segment text for readable snippets, per-word
    ``(start, end)`` for deep-links / clip cuts / diarization alignment. Works for both
    faster-whisper and stable-ts segments (both expose ``.words`` with
    ``.word``/``.start``/``.end``)."""
    out_segments = []
    for seg in segments:
        words = []
        for w in getattr(seg, "words", None) or []:
            start = getattr(w, "start", None)
            end = getattr(w, "end", None)
            token = (getattr(w, "word", "") or "").strip()
            if token and start is not None and end is not None:
                words.append({"w": token, "s": round(float(start), 3), "e": round(float(end), 3)})
        text = (seg.text or "").strip()
        if not text and not words:
            continue
        out_segments.append(
            {
                "start": round(float(seg.start), 3),
                "end": round(float(seg.end), 3),
                "text": text,
                "words": words,
            }
        )
    payload = {"schema": _WORDS_SCHEMA_VERSION, "basis": basis, "segments": out_segments}
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


# ── Public API ───────────────────────────────────────────────────────────────


def load_model(model: str, compute_type: str, cpu_threads: int):
    """Load and return a cached ASR model bundle.

    The model is loaded at most ONCE per process regardless of how many sources are
    processed in parallel.  A threading lock ensures only one thread downloads/loads
    on a cold start; all others wait and then reuse the cached instance.  This
    reduces HuggingFace Hub API calls from one-per-source to one-per-process, which
    is critical on GitHub Actions where anonymous API calls are tightly rate-limited.

    ``ASR_MODEL_PATH`` env var (set by ``scripts/prepare_whisper.py``): when set to a
    local directory, loads from disk directly — bypassing the Hub entirely.  When set
    to a model name string (distil fallback), uses it in place of the *model* arg.
    This lets the pre-download cascade (HF → B2 mirror → distil fallback) choose the
    best available model at download time without touching config.

    Callers (``TranscriptStage.process``) should call this once before the episode
    loop and pass the returned instance to :func:`transcribe` / :func:`align`.
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise ImportError(
            "faster-whisper is required for ASR. Install it with: pip install 'citypods[asr]'"
        ) from exc

    # ASR_MODEL_PATH overrides the config model name: either a local directory
    # (preferred — no Hub call) or a fallback HF model name (e.g. "distil-large-v3").
    model_or_path = _configured_model_or_path(model)

    cache_key = ("faster-whisper", model_or_path, compute_type, cpu_threads)
    if cache_key in _model_cache:
        return _model_cache[cache_key]

    with _model_lock:
        # Double-checked locking: another thread may have loaded while we waited.
        if cache_key not in _model_cache:
            transcriber = WhisperModel(
                model_or_path, device="cpu", compute_type=compute_type, cpu_threads=cpu_threads
            )
            _model_cache[cache_key] = _LoadedAsrModel(
                model_or_path=model_or_path,
                compute_type=compute_type,
                cpu_threads=cpu_threads,
                transcriber=transcriber,
            )
    return _model_cache[cache_key]


def _load_alignment_model(model_or_path: str, compute_type: str | None, cpu_threads: int):
    """Load/cache a stable-ts faster-whisper model that supports ``align()``."""
    try:
        import stable_whisper
    except ImportError as exc:
        raise ImportError(
            "stable-ts is required for forced alignment. "
            "Install it with: pip install 'citypods[asr]'"
        ) from exc

    cache_key = ("stable-faster-whisper", model_or_path, compute_type, cpu_threads)
    if cache_key in _model_cache:
        return _model_cache[cache_key]

    with _model_lock:
        if cache_key not in _model_cache:
            options: dict[str, object] = {"device": "cpu", "cpu_threads": cpu_threads}
            if compute_type:
                options["compute_type"] = compute_type
            _model_cache[cache_key] = stable_whisper.load_faster_whisper(
                model_or_path,
                **options,
            )
    return _model_cache[cache_key]


def transcribe(
    audio_path: Path,
    model_or_name: object,
    language: str | None,
    compute_type: str,
    beam_size: int,
    initial_prompt: str | None,
    cpu_threads: int,
) -> bytes:
    """Run faster-whisper transcription on *audio_path*; return WebVTT bytes.

    *model_or_name* may be a pre-loaded ``WhisperModel`` instance (preferred — avoids
    reloading weights on every call) or a model-name string (loads inline, kept for
    backward compatibility with tests that pass a name directly).
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise ImportError(
            "faster-whisper is required for ASR transcription. "
            "Install it with: pip install 'citypods[asr]'"
        ) from exc

    if isinstance(model_or_name, str):
        wm = WhisperModel(
            _configured_model_or_path(model_or_name),
            device="cpu",
            compute_type=compute_type,
            cpu_threads=cpu_threads,
        )
    elif isinstance(model_or_name, _LoadedAsrModel):
        wm = model_or_name.transcriber
    else:
        wm = model_or_name  # pre-loaded instance

    segments, _ = wm.transcribe(
        str(audio_path),
        language=language or None,
        beam_size=beam_size,
        initial_prompt=initial_prompt or None,
        word_timestamps=True,
    )
    # Materialize the generator once: both the segment VTT and the word JSON consume it.
    segs = list(segments)
    return TranscriptArtifacts(vtt=_to_vtt(segs), words=_to_words_json(segs))


class AlignmentQualityError(RuntimeError):
    """Raised when forced alignment quality falls below the acceptable threshold.

    The caller (``TranscriptStage``) catches this and falls back to fresh
    transcription (Path B) so the episode still gets a usable timed VTT.
    """


# Minimum fraction of words that must receive a timestamp for the alignment to be
# considered usable.  Below this threshold we raise AlignmentQualityError so the
# caller can fall back to fresh transcription.
_MIN_ALIGN_COVERAGE = 0.60


def align(
    audio_path: Path,
    text: str,
    model_or_name: object,
    language: str | None,
    cpu_threads: int,
) -> bytes:
    """Force-align *text* to *audio_path* using stable-ts; return WebVTT bytes.

    *model_or_name* may be a pre-loaded stable-ts model instance or a name string.
    Preserves the official transcript wording exactly; faster than generation.

    Raises :exc:`AlignmentQualityError` when the fraction of successfully aligned
    words falls below ``_MIN_ALIGN_COVERAGE`` (default 60 %).  The caller should
    catch this and fall back to fresh :func:`transcribe`.
    """
    if isinstance(model_or_name, str):
        wm = _load_alignment_model(
            _configured_model_or_path(model_or_name),
            None,
            cpu_threads,
        )
    elif isinstance(model_or_name, _LoadedAsrModel):
        wm = _load_alignment_model(
            model_or_name.model_or_path,
            model_or_name.compute_type,
            model_or_name.cpu_threads,
        )
    else:
        wm = model_or_name  # pre-loaded instance

    result = wm.align(str(audio_path), text, language=language or "en")

    # Quality check: count words that received a valid timestamp.
    total_words = sum(len(seg.words) for seg in result.segments if hasattr(seg, "words"))
    timed_words = sum(
        1
        for seg in result.segments
        if hasattr(seg, "words")
        for w in seg.words
        if getattr(w, "start", None) is not None
    )
    if total_words > 0:
        coverage = timed_words / total_words
        if coverage < _MIN_ALIGN_COVERAGE:
            raise AlignmentQualityError(
                f"alignment coverage {coverage:.0%} < {_MIN_ALIGN_COVERAGE:.0%} "
                f"({timed_words}/{total_words} words timed) — falling back to transcription"
            )

    # Segment-level VTT (clean cue-per-utterance) for the podcast tag; word JSON sidecar
    # from the same aligned result for server-side features.
    vtt_str: str = result.to_vtt(segment_level=True, word_level=False)
    if not vtt_str.startswith("WEBVTT"):
        vtt_str = "WEBVTT\n\n" + vtt_str
    return TranscriptArtifacts(
        vtt=vtt_str.encode("utf-8"),
        words=_to_words_json(result.segments),
    )


# ── Spec hash ────────────────────────────────────────────────────────────────


def asr_spec_hash(
    audio_spec_hash: str,
    model: str,
    align_text_hash: str | None,
    version: str,
) -> str:
    """Recipe hash for an ASR transcript: changes when audio, model, or source text changes.

    Keyed on *inputs* (not output bytes) so the storage key can be computed before
    running inference, enabling a cheap ``_present(key)`` reuse check.

    ``align_text_hash`` is the SHA-1 prefix of the source text used for alignment
    (Path A); ``None`` means fresh transcription (Path B).
    """
    spec = {
        "v": version,
        "audio": audio_spec_hash,
        "model": model,
        "align": align_text_hash,
    }
    blob = json.dumps(spec, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha1(blob).hexdigest()[:12]


# ── Text extraction from VTT/SRT (for bench WER) ────────────────────────────


def vtt_to_text(vtt: str) -> str:
    """Strip WebVTT cue headers and return concatenated cue text (for WER comparison)."""
    lines = []
    in_cue = False
    for line in vtt.splitlines():
        line = line.strip()
        if not line:
            in_cue = False
            continue
        if line.startswith("WEBVTT") or line.startswith("NOTE"):
            continue
        if "-->" in line:
            in_cue = True
            continue
        if in_cue and line:
            lines.append(line)
    return " ".join(lines)


def srt_to_text(srt: str) -> str:
    """Strip SRT cue headers and return concatenated cue text (for WER comparison)."""
    import re

    lines = []
    for line in srt.splitlines():
        line = line.strip()
        if not line or line.isdigit():
            continue
        if re.match(r"\d{2}:\d{2}:\d{2},\d{3}\s*-->", line):
            continue
        lines.append(line)
    return " ".join(lines)
