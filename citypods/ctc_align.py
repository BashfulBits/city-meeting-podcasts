"""H15 Layer 2 — independent CTC forced-alignment judge (review/12 §H15).

Layer 1 (``citypods/asr.py``) and the periodic A/B sampler (``citypods/transcript_quality.py``)
both score candidates using the same *kind* of model that produced them: a Whisper-family
decoder (stable-ts for the provider-align candidate, faster-whisper for the ASR-challenger
candidate) grading its own output. Review/12 calls this "same-generator-biased" — a real fit
signal, but never a fair verdict on which transcript the audio actually supports.

This module is the fair judge: it scores a transcript's fit to audio using
``torchaudio.pipelines.MMS_FA``, a wav2vec2 CTC forced-aligner trained purely for alignment, from
a completely different model family than either candidate generator. It never produces text
itself, so it cannot be biased toward whichever generator wrote the words being scored.

Scope (v1, see review/12 §H15 "Open decisions"): English only. MMS_FA's public torchaudio bundle
ships a fixed western-Latin character dictionary (see ``get_dict()``); scoring other languages
against it would silently produce meaningless low scores rather than a fair judgment, so
:func:`ctc_fit` refuses non-English input explicitly instead.

All third-party imports (torch, torchaudio) are lazy, mirroring asr.py — the module loads cleanly
without the ``asr-align2`` optional extra; a missing import surfaces only when :func:`ctc_fit` is
actually called. The pretrained bundle (~1.2 GB) downloads once per process via
``torch.hub``'s on-disk cache (``TORCH_HOME``), then stays cached in the model-object cache below
for the rest of the process, same pattern as ``asr.py``'s ``_model_cache``.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path

_model_cache: dict[str, tuple] = {}
_model_lock = threading.Lock()

# MMS_FA's dictionary is lowercase a-z plus apostrophe (see torchaudio.pipelines.MMS_FA.get_dict());
# words containing any other character (digits, other scripts, punctuation) cannot be represented
# and are dropped by _normalize_words rather than crashing the whole alignment.
_WORD_RE = re.compile(r"[a-z']+")


class UnsupportedLanguageError(RuntimeError):
    """Raised when asked to score a source whose configured language isn't English.

    MMS_FA's public torchaudio bundle is trained on a fixed western-Latin character dictionary;
    silently scoring non-English text against it would produce meaningless near-zero fit scores
    that look like "the caption doesn't match the audio" rather than "wrong tool for this
    language" — so this refuses explicitly instead of returning a misleading number.
    """


@dataclass(frozen=True)
class CtcFitResult:
    """Independent-judge fit score for one (text, audio-clip) pair.

    ``mean_score`` — length-weighted average per-word alignment confidence, a linear ~0-1 value
    (the CTC path's per-token log-probabilities, exponentiated — see
    ``torchaudio.pipelines._wav2vec2.aligner._align_emission_and_tokens``). Answers "how well
    does this independent model's forced alignment fit the audio at the word positions it found?"
    ``coverage`` — fraction of words that received a non-degenerate (nonzero-length) alignment
    span. Answers "how much of the text could the aligner place in the audio at all?" Neither
    number is biased toward either candidate generator: this model never produced either
    candidate's text, unlike Layer 1's same-generator coverage/word-logprob.
    """

    mean_score: float
    coverage: float
    word_count: int
    aligned_word_count: int


_EMPTY_FIT = CtcFitResult(mean_score=0.0, coverage=0.0, word_count=0, aligned_word_count=0)


def normalize_words(text: str, *, vocabulary: set[str] | None = None) -> list[str]:
    """Lowercase and split into MMS_FA-vocabulary word tokens.

    Words containing a character outside ``vocabulary`` (numerals, other scripts, stray
    punctuation the regex didn't already strip) are dropped rather than raising — degrades
    ``coverage`` gracefully instead of crashing the whole sample on one odd word.
    """
    words = _WORD_RE.findall(text.lower())
    if vocabulary is None:
        return [w for w in words if w]
    return [w for w in words if w and all(c in vocabulary for c in w)]


def _load_bundle():
    """Load (and process-cache) the MMS_FA model, tokenizer, aligner, sample rate, dictionary."""
    key = "mms_fa"
    if key in _model_cache:
        return _model_cache[key]
    with _model_lock:
        if key not in _model_cache:
            import torchaudio

            bundle = torchaudio.pipelines.MMS_FA
            model = bundle.get_model()
            model.eval()
            tokenizer = bundle.get_tokenizer()
            aligner = bundle.get_aligner()
            dictionary = bundle.get_dict()
            _model_cache[key] = (model, tokenizer, aligner, bundle.sample_rate, dictionary)
    return _model_cache[key]


def _load_audio_clip(audio_path: Path, sample_rate: int, *, clip_start: float, clip_end: float):
    """Load just the [clip_start, clip_end) window, resampled to mono at ``sample_rate``.

    Reads only the requested slice from disk (``torchaudio.load``'s ``frame_offset``/
    ``num_frames``, in the file's *native* sample rate) rather than decoding the full episode —
    L2 is deliberately clip-scoped like the rest of H15's per-sample scoring, not a full-meeting
    pass, to keep the CTC forced-alignment DP (`O(frames * text_length)`) cheap.
    """
    import torchaudio

    info = torchaudio.info(str(audio_path))
    native_sr = info.sample_rate
    duration = max(0.0, clip_end - clip_start)
    frame_offset = max(0, int(clip_start * native_sr))
    num_frames = max(1, int(duration * native_sr)) if duration > 0 else -1
    waveform, sr = torchaudio.load(
        str(audio_path), frame_offset=frame_offset, num_frames=num_frames
    )
    if waveform.shape[0] > 1:
        waveform = waveform.mean(dim=0, keepdim=True)
    if sr != sample_rate:
        waveform = torchaudio.functional.resample(waveform, sr, sample_rate)
    return waveform


def ctc_fit(
    audio_path: Path,
    text: str,
    *,
    clip_start: float = 0.0,
    clip_end: float = 0.0,
    language: str | None = None,
) -> CtcFitResult:
    """Force-align ``text`` (English) against the ``[clip_start, clip_end)`` window of
    ``audio_path`` using an independent (non-Whisper) wav2vec2 CTC aligner.

    Raises :class:`UnsupportedLanguageError` for a non-English ``language``, and any of
    torchaudio's own exceptions on model-load/inference failure (missing extra, download
    failure, malformed audio) — the caller is expected to catch broadly and fall back to the
    same-generator Layer 1 signal, the same resilience pattern H15's per-sample evaluation
    already uses for align()/transcribe() failures.
    """
    if language and not language.lower().startswith("en"):
        raise UnsupportedLanguageError(
            f"L2 CTC aligner (torchaudio MMS_FA) is English-only in v1; got language={language!r}"
        )
    import torch

    model, tokenizer, aligner, sample_rate, dictionary = _load_bundle()
    vocabulary = set(dictionary) - {"-", "*"}
    words = normalize_words(text, vocabulary=vocabulary)
    if not words:
        return _EMPTY_FIT

    waveform = _load_audio_clip(audio_path, sample_rate, clip_start=clip_start, clip_end=clip_end)
    if waveform.numel() == 0:
        return _EMPTY_FIT

    with torch.inference_mode():
        emission, _ = model(waveform)

    tokens = tokenizer(words)
    token_spans = aligner(emission[0], tokens)

    word_scores: list[float] = []
    aligned_word_count = 0
    for spans in token_spans:
        span_len = sum(len(s) for s in spans)
        if span_len <= 0:
            continue
        aligned_word_count += 1
        word_scores.append(sum(s.score * len(s) for s in spans) / span_len)

    mean_score = sum(word_scores) / len(word_scores) if word_scores else 0.0
    coverage = aligned_word_count / len(words) if words else 0.0
    return CtcFitResult(
        mean_score=round(mean_score, 4),
        coverage=round(coverage, 4),
        word_count=len(words),
        aligned_word_count=aligned_word_count,
    )
