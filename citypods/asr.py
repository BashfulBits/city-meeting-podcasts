"""ASR (Automatic Speech Recognition) helpers for transcript generation.

Two public entry points:

  transcribe(audio_path, ...)  — Path B: fresh Whisper generation.
  align_known_text(audio_path, sections, ...) — Path A: WhisperX CTC alignment, preserving
                                                 the exact wording of an existing source transcript.

Both return a :class:`TranscriptArtifacts` (a clean segment-level VTT + a word-level JSON
sidecar) in *served time* (caller downloads the hosted M4A before calling).

All third-party imports (faster_whisper, whisperx) are lazy — the module loads
cleanly without the ASR optional extras; a missing import surfaces only when
transcribe() / align() is actually called.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

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
    """A loaded faster-whisper transcription model."""

    model_or_path: str
    compute_type: str
    cpu_threads: int
    transcriber: object


@dataclass(frozen=True)
class _LoadedAlignmentModel:
    """WhisperX acoustic model and metadata used for known-text alignment."""

    model_name: str
    language: str
    model: object
    metadata: object
    device: str = "cpu"


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
      per-word ``.probability`` faster-whisper populates (a linear 0-1 confidence,
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
            if _valid_time(getattr(w, "start", None)) and _valid_time(getattr(w, "end", None)):
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


def _valid_time(value) -> bool:
    return isinstance(value, int | float) and math.isfinite(float(value))


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
    faster-whisper and normalized WhisperX segments (both expose ``.words`` with
    ``.word``/``.start``/``.end``)."""
    out_segments = []
    for seg in segments:
        words = []
        for w in getattr(seg, "words", None) or []:
            start = getattr(w, "start", None)
            end = getattr(w, "end", None)
            token = (getattr(w, "word", "") or "").strip()
            if token and _valid_time(start) and _valid_time(end):
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


def load_alignment_model(model_name: str, language: str, cpu_threads: int = 1):
    """Load/cache a WhisperX CTC model for known-text alignment.

    ``cpu_threads`` is part of the cache identity for API compatibility, although WhisperX's
    torchaudio model controls its own low-level thread pool.
    """
    try:
        import whisperx
    except ImportError as exc:
        raise ImportError(
            "whisperx is required for known-text alignment. "
            "Install it with: pip install 'citypods[asr-align]'"
        ) from exc

    model_name = model_name or "WAV2VEC2_ASR_BASE_960H"
    language = language or "en"
    cache_key = ("whisperx-align", model_name, language, cpu_threads)
    if cache_key in _model_cache:
        return _model_cache[cache_key]

    with _model_lock:
        if cache_key not in _model_cache:
            model, metadata = whisperx.load_align_model(
                language_code=language,
                device="cpu",
                model_name=model_name,
            )
            _model_cache[cache_key] = _LoadedAlignmentModel(
                model_name=model_name,
                language=language,
                model=model,
                metadata=metadata,
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

    The caller (``TranscriptStage``) marks the provider candidate ineligible and lets the normal
    full-ASR queue produce a usable timed VTT on a later pass.
    """


# Minimum fraction of words that must receive a timestamp for the alignment to be
# considered usable.  Below this threshold we raise AlignmentQualityError so the
# caller can mark the provider candidate ineligible for the current pass.
_MIN_ALIGN_COVERAGE = 0.90

# WhisperX's CTC model is not memory-bounded internally: the convolutional feature extractor
# materializes tensors proportional to the audio window passed for each section. A full
# 1.7-hour recording can therefore request ~40 GB on a hosted CPU runner even though the model
# itself is small. Provider markers are coarse, so keep a generous five-minute ceiling while
# refusing an unsafe unbounded/oversized section and letting the normal ASR route handle it.
_MAX_ALIGN_SECTION_SECONDS = 5 * 60.0


def _mapping_value(value, key: str, default=None):
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _whisperx_segments(result: dict, sections: list[dict]) -> list[SimpleNamespace]:
    """Normalize WhisperX dictionaries into the internal segment/word shape."""
    raw_segments = result.get("segments") or []
    normalized: list[SimpleNamespace] = []
    for index, raw in enumerate(raw_segments):
        section = sections[index] if index < len(sections) else {}
        raw_words = raw.get("words") or []
        words = [
            SimpleNamespace(
                word=_mapping_value(word, "word", ""),
                start=_mapping_value(word, "start"),
                end=_mapping_value(word, "end"),
                probability=None,
            )
            for word in raw_words
        ]
        start = _mapping_value(raw, "start", section.get("start", 0.0))
        end = _mapping_value(raw, "end", section.get("end", start or 0.0))
        if not _valid_time(start):
            start = section.get("start", 0.0)
        if not _valid_time(end):
            end = section.get("end", start or 0.0)
        normalized.append(
            SimpleNamespace(
                start=float(start or 0.0),
                end=float(end if end is not None else start or 0.0),
                text=str(section.get("text") or _mapping_value(raw, "text", "")),
                words=words,
            )
        )
    # Some WhisperX versions expose word_segments without nested words. Preserve the input
    # section boundaries/text and attach those words rather than silently dropping them.
    if not normalized and result.get("word_segments"):
        words = [
            SimpleNamespace(
                word=_mapping_value(word, "word", ""),
                start=_mapping_value(word, "start"),
                end=_mapping_value(word, "end"),
                probability=None,
            )
            for word in result["word_segments"]
        ]
        if not sections:
            return normalized
        buckets: list[list[SimpleNamespace]] = [[] for _ in sections]
        # Use exact source-position quotas for words without timestamps. Timestamped words take
        # precedence, so a partial WhisperX result cannot spill every word into every paragraph.
        quotas = [max(1, len(str(section.get("text") or "").split())) for section in sections]
        boundaries: list[int] = []
        total = 0
        for quota in quotas:
            total += quota
            boundaries.append(total)
        for word_index, word in enumerate(words):
            if _valid_time(word.start):
                section_index = next(
                    (
                        index
                        for index, section in enumerate(sections)
                        if word.start >= float(section.get("start") or 0.0)
                        and (
                            section.get("end") is None
                            or word.start < float(section["end"])
                            or index == len(sections) - 1
                        )
                    ),
                    len(sections) - 1,
                )
            else:
                section_index = min(bisect_left(boundaries, word_index + 1), len(sections) - 1)
            buckets[section_index].append(word)
        normalized = [
            SimpleNamespace(
                start=float(section.get("start") or 0.0),
                end=float(section.get("end") or section.get("start") or 0.0),
                text=str(section.get("text") or ""),
                words=buckets[index],
            )
            for index, section in enumerate(sections)
        ]
    return normalized


def _interpolate_words(segments: list[SimpleNamespace], method: str) -> None:
    """Fill missing word bounds after the raw coverage gate.

    This mirrors WhisperX's nearest/linear semantics but keeps the pre-interpolation coverage
    observable. ``nearest`` copies the closest located word; ``linear`` interpolates by word
    position between the surrounding located words.
    """
    if method not in {"nearest", "linear", "ignore"}:
        raise ValueError("interpolate_method must be one of: nearest, linear, ignore")
    if method == "ignore":
        return
    for segment in segments:
        words = segment.words or []
        valid = [
            i for i, word in enumerate(words) if _valid_time(word.start) and _valid_time(word.end)
        ]
        if not valid:
            continue
        valid_set = set(valid)
        for index, word in enumerate(words):
            if index in valid_set:
                continue
            position = bisect_left(valid, index)
            left = valid[position - 1] if position > 0 else None
            right = valid[position] if position < len(valid) else None
            if left is None:
                source = words[right]
                word.start, word.end = source.start, source.end
            elif right is None:
                source = words[left]
                word.start, word.end = source.start, source.end
            elif method == "nearest":
                source = words[left] if index - left <= right - index else words[right]
                word.start, word.end = source.start, source.end
            else:
                ratio = (index - left) / (right - left)
                word.start = words[left].start + ratio * (words[right].start - words[left].start)
                word.end = words[left].end + ratio * (words[right].end - words[left].end)


def _bounded_alignment_sections(sections: list[dict], audio) -> list[dict]:
    """Validate alignment windows after loading audio, closing an open final section."""
    try:
        audio_duration = len(audio) / 16_000.0  # whisperx.load_audio returns mono 16 kHz samples
    except TypeError as exc:
        raise ValueError("WhisperX audio did not expose a sample length") from exc
    bounded: list[dict] = []
    for section in sections:
        start = section.get("start")
        end = section.get("end")
        if not _valid_time(start):
            raise ValueError(f"alignment section has invalid start: {start!r}")
        if end is None:
            end = audio_duration
        if not _valid_time(end):
            continue
        end = min(float(end), audio_duration)
        if end <= float(start):
            continue
        window_seconds = end - float(start)
        if window_seconds > _MAX_ALIGN_SECTION_SECONDS:
            raise AlignmentQualityError(
                "alignment section is too long for WhisperX's CPU memory envelope: "
                f"{window_seconds:.1f}s > {_MAX_ALIGN_SECTION_SECONDS:.1f}s"
            )
        bounded.append({**section, "start": float(start), "end": float(end)})
    if not bounded:
        raise ValueError("provider source text produced no bounded alignment sections")
    return bounded


def align_known_text(
    audio_path: Path,
    sections: list[dict],
    model_or_name: object,
    language: str | None,
    cpu_threads: int,
    interpolate_method: str = "linear",
) -> TranscriptArtifacts:
    """Align known text to audio with WhisperX while preserving provider wording.

    WhisperX is first asked for ``interpolate_method='ignore'``.  The 90% fallback gate is
    computed from those raw word timestamps, then the requested interpolation is applied only to
    the successful result. This prevents interpolation from masking a bad acoustic alignment.
    """
    if isinstance(model_or_name, str):
        loaded = load_alignment_model(model_or_name, language or "en", cpu_threads)
    elif isinstance(model_or_name, _LoadedAlignmentModel):
        loaded = model_or_name
    else:
        loaded = model_or_name
    try:
        import whisperx
    except ImportError as exc:
        raise ImportError(
            "whisperx is required for known-text alignment. "
            "Install it with: pip install 'citypods[asr-align]'"
        ) from exc

    audio = whisperx.load_audio(str(audio_path))
    bounded_sections = _bounded_alignment_sections(sections, audio)
    result = whisperx.align(
        bounded_sections,
        loaded.model,
        loaded.metadata,
        audio,
        loaded.device,
        return_char_alignments=False,
        interpolate_method="ignore",
        print_progress=False,
    )
    segments = _whisperx_segments(result, bounded_sections)
    fit = _word_fit_stats(segments)
    if fit["coverage"] < _MIN_ALIGN_COVERAGE:
        raise AlignmentQualityError(
            f"alignment coverage {fit['coverage']:.0%} < {_MIN_ALIGN_COVERAGE:.0%} "
            f"— provider candidate is ineligible for alignment"
        )
    _interpolate_words(segments, interpolate_method)
    return TranscriptArtifacts(
        vtt=_to_vtt(segments),
        words=_to_words_json(segments),
        coverage=fit["coverage"],
        word_logprob_mean=None,
        word_logprob_p10=None,
    )


def align(
    audio_path: Path,
    text: str,
    model_or_name: object,
    language: str | None,
    cpu_threads: int,
    compute_type: str = "int8",
    *,
    vad: bool = True,
    fast_mode: bool = True,
    timed_segments: list[dict] | None = None,
) -> TranscriptArtifacts:
    """Backward-compatible wrapper for one full-duration known-text section.

    The small legacy branch is retained only for downstream test doubles that inject
    ``stable_whisper``; production installs and calls WhisperX through ``align_known_text``.
    """
    try:
        import stable_whisper  # type: ignore[import-not-found]
    except ImportError:
        stable_whisper = None
    if stable_whisper is not None:
        if isinstance(model_or_name, str):
            legacy_model = stable_whisper.load_faster_whisper(
                _configured_model_or_path(model_or_name),
                device="cpu",
                cpu_threads=cpu_threads,
                compute_type=compute_type or "int8",
            )
        elif isinstance(model_or_name, _LoadedAsrModel):
            legacy_model = stable_whisper.load_faster_whisper(
                model_or_name.model_or_path,
                device="cpu",
                cpu_threads=model_or_name.cpu_threads,
                compute_type=model_or_name.compute_type,
            )
        else:
            legacy_model = model_or_name
        if hasattr(legacy_model, "align"):
            if timed_segments and hasattr(legacy_model, "align_words"):
                result = legacy_model.align_words(
                    str(audio_path), timed_segments, language=language or "en"
                )
            else:
                result = legacy_model.align(
                    str(audio_path), text, language=language or "en", vad=vad, fast_mode=fast_mode
                )
            segments = result.segments
            fit = _word_fit_stats(segments)
            if fit["coverage"] < _MIN_ALIGN_COVERAGE:
                raise AlignmentQualityError(
                    f"alignment coverage {fit['coverage']:.0%} < {_MIN_ALIGN_COVERAGE:.0%} "
                    f"({fit['coverage']:.0%}) — provider candidate is ineligible for alignment"
                )
            to_vtt = getattr(result, "to_vtt", None)
            if callable(to_vtt):
                vtt = to_vtt(segment_level=True, word_level=False)
            else:
                vtt = result.to_srt_vtt(segment_level=True, word_level=False, vtt=True)
            if not vtt.startswith("WEBVTT"):
                vtt = "WEBVTT\n\n" + vtt
            return TranscriptArtifacts(
                vtt=vtt.encode("utf-8"),
                words=_to_words_json(segments),
                coverage=fit["coverage"],
                word_logprob_mean=fit["word_logprob_mean"],
                word_logprob_p10=fit["word_logprob_p10"],
            )
    from citypods.known_text import provider_sections

    # The external worker already extracted/remapped coarse provider windows. Preserve them
    # here; dropping them turns every long meeting into one full-duration WhisperX section.
    sections = timed_segments if timed_segments else provider_sections(text)
    return align_known_text(
        audio_path,
        sections,
        model_or_name,
        language,
        cpu_threads,
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
    align_model: str | None = None,
    interpolate_method: str | None = None,
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
    if align_text_hash is not None:
        spec["align_model"] = align_model
        spec["interpolate_method"] = interpolate_method
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
