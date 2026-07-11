"""ASR (Automatic Speech Recognition) helpers for transcript generation.

Two public entry points:

  transcribe(audio_path, ...)  — Path B: fresh Whisper generation.
  align(audio_path, text, ...) — Path A: forced alignment via stable-ts, preserving
                                  the exact wording of an existing source transcript.

Both return a :class:`TranscriptArtifacts` (a clean segment-level VTT + a word-level JSON
sidecar) in *served time* (caller downloads the hosted M4A before calling).

All third-party imports (faster_whisper, stable_whisper) are lazy — the module loads
cleanly without the ASR optional extras; a missing import surfaces only when
transcribe() / align() is actually called.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path

# ── Pinned Whisper model repos + revisions ───────────────────────────────────
# Canonical single source of truth for the model bytes, shared by the GitHub-Actions
# runner (scripts/prepare_whisper.py downloads these) and the external Modal/Beam
# worker images (which bake the same pinned revision). Renovate tracks the revisions
# via the `# renovate` markers (see .github/renovate.json5). Pinning the *current*
# revision is a no-op reproducibility fix — it does NOT bump ASR_PIPELINE_VERSION or
# reprocess transcripts (GH#498). See review/22.
HF_PREFERRED = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"  # renovate
HF_PREFERRED_REVISION = "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf"
HF_FALLBACK = "distil-whisper/distil-large-v3"  # renovate
HF_FALLBACK_REVISION = "8031d2e6ce6631b7fc45629dddfc00271116d981"

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
    """Format seconds as HH:MM:SS.mmm for WebVTT cue timestamps.

    Rounds to whole milliseconds *before* splitting into h/m/s (CR2-CP-19): deriving h/m from
    unrounded ``seconds`` and only rounding the formatted fractional-second field can carry a
    value like 119.9996 into a displayed "SS=60.000" when the rounding crosses a minute
    boundary post-split. Working in integer milliseconds throughout makes the carry exact.
    """
    total_ms = round(seconds * 1000)
    h, rem_ms = divmod(total_ms, 3_600_000)
    m, rem_ms = divmod(rem_ms, 60_000)
    s, ms = divmod(rem_ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


# Schema version of the word-level JSON sidecar (independent of ASR_PIPELINE_VERSION, which
# keys re-transcription). Bump only when the JSON *shape* changes.
# v2 (H15): adds an optional per-word "p" (probability) field, additive-only.
_WORDS_SCHEMA_VERSION = "2"


@dataclass(frozen=True)
class TranscriptArtifacts:
    """The two outputs of ASR for one episode, plus H15's free per-run acoustic-fit signal.

    * ``vtt``   — clean **segment-level** WebVTT, served to podcast apps via
      ``<podcast:transcript>`` (one readable cue per utterance; small).
    * ``words`` — a **word-level** JSON sidecar consumed server-side only (phrase search,
      clip selection, diarization). Never served as the podcast transcript.
    * ``coverage`` — fraction of words that received a valid timestamp (H15 Layer 1).
      For ``align()`` this is the same gate value used for :class:`AlignmentQualityError`;
      for ``transcribe()`` it is near-1.0 in practice since faster-whisper always times
      generated words, but is still recorded for completeness.
    * ``word_logprob_mean`` / ``word_logprob_p10`` — mean and 10th-percentile of the
      per-word ``.probability`` faster-whisper/stable-ts populate (a linear 0-1 confidence,
      despite the "logprob" name inherited from review/12's field naming), or ``None`` when
      no word carried a probability value. Answers "how confidently?" where coverage answers
      "did the words land in the audio at all?".
    """

    vtt: bytes
    words: bytes
    coverage: float | None = None
    word_logprob_mean: float | None = None
    word_logprob_p10: float | None = None


def _word_fit_stats(segments) -> dict:
    """H15 Layer 1: reference-free acoustic-fit stats from one transcript's own segments.

    Must run on the raw segment/word objects (not the serialized JSON), because "coverage"
    needs the count of words *attempted*, not just the ones that ended up with a timestamp.
    """
    total_words = 0
    timed_words = 0
    probabilities: list[float] = []
    for seg in segments:
        for w in getattr(seg, "words", None) or []:
            total_words += 1
            if getattr(w, "start", None) is not None and getattr(w, "end", None) is not None:
                timed_words += 1
            prob = getattr(w, "probability", None)
            if isinstance(prob, int | float):
                probabilities.append(float(prob))
    coverage = timed_words / total_words if total_words > 0 else 0.0
    mean_p = sum(probabilities) / len(probabilities) if probabilities else None
    p10 = None
    if probabilities:
        ordered = sorted(probabilities)
        idx = max(0, min(len(ordered) - 1, round(0.10 * (len(ordered) - 1))))
        p10 = ordered[idx]
    return {
        "coverage": round(coverage, 4),
        "word_logprob_mean": round(mean_p, 4) if mean_p is not None else None,
        "word_logprob_p10": round(p10, 4) if p10 is not None else None,
    }


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
                entry = {"w": token, "s": round(float(start), 3), "e": round(float(end), 3)}
                prob = getattr(w, "probability", None)
                if isinstance(prob, int | float):
                    entry["p"] = round(float(prob), 4)
                words.append(entry)
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
            "faster-whisper is required for ASR. "
            "Install it with: pip install 'citypods[asr-transcribe]'"
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
            "Install it with: pip install 'citypods[asr-align]'"
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
) -> TranscriptArtifacts:
    """Run faster-whisper transcription on *audio_path*; return a ``TranscriptArtifacts``
    (segment WebVTT + word-level JSON sidecar).

    *model_or_name* may be a pre-loaded ``WhisperModel`` instance (preferred — avoids
    reloading weights on every call) or a model-name string (loads inline, kept for
    backward compatibility with tests that pass a name directly).
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise ImportError(
            "faster-whisper is required for ASR transcription. "
            "Install it with: pip install 'citypods[asr-transcribe]'"
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
    fit = _word_fit_stats(segs)
    return TranscriptArtifacts(
        vtt=_to_vtt(segs),
        words=_to_words_json(segs),
        coverage=fit["coverage"],
        word_logprob_mean=fit["word_logprob_mean"],
        word_logprob_p10=fit["word_logprob_p10"],
    )


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
) -> TranscriptArtifacts:
    """Force-align *text* to *audio_path* using stable-ts; return a ``TranscriptArtifacts``
    (segment WebVTT + word-level JSON sidecar).

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
    # CR2-CP-18: *text* is only ever non-empty when this path is chosen (stages.py picks
    # the "align" lane only when align_text is truthy), so total_words == 0 means the aligner
    # produced no words at all — a total failure, not a vacuously-passing edge case. Treat it
    # as 0% coverage rather than skipping the gate.
    coverage = timed_words / total_words if total_words > 0 else 0.0
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
    # H15 Layer 1: reuse the gate's own `coverage` (identical semantics to the pass/fail check
    # above); only the word-probability aggregate needs the shared helper.
    fit = _word_fit_stats(result.segments)
    return TranscriptArtifacts(
        vtt=vtt_str.encode("utf-8"),
        words=_to_words_json(result.segments),
        coverage=round(coverage, 4),
        word_logprob_mean=fit["word_logprob_mean"],
        word_logprob_p10=fit["word_logprob_p10"],
    )


# ── Spec hash ────────────────────────────────────────────────────────────────


def asr_spec_hash(
    media_spec_hash: str,
    model: str,
    align_text_hash: str | None,
    version: str,
    *,
    language: str | None = None,
    compute_type: str | None = None,
    beam_size: int | None = None,
    initial_prompt: str | None = None,
) -> str:
    """Recipe hash for an ASR transcript: changes when any transcript input changes.

    Keyed on *inputs* (not output bytes) so the storage key can be computed before
    running inference, enabling a cheap ``_present(key)`` reuse check.

    ``media_spec_hash`` is deliberately the transcript media/timeline identity, not the audio
    mastering byte hash. Codec, loudness, chapter, or audio-processing recipe changes should not
    invalidate completed ASR artifacts when the served timeline is unchanged.

    ``align_text_hash`` is the SHA-1 prefix of the source text used for alignment
    (Path A); ``None`` means fresh transcription (Path B). Fresh-transcription prompt and
    decoding hints are included because they can change Whisper output even for identical audio.
    """
    spec = {
        "v": version,
        "media": media_spec_hash,
        "model": model,
        "align": align_text_hash,
        "language": language,
        "compute_type": compute_type,
        "beam_size": beam_size,
        "initial_prompt": initial_prompt,
    }
    blob = json.dumps(spec, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha1(blob).hexdigest()[:12]


def asr_initial_prompt(author: str, body: str | None, title: str) -> str:
    """Stable fresh-ASR prompt shared by aliases of the same real-world meeting."""
    return ". ".join(part for part in (author, body, title) if part)


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
