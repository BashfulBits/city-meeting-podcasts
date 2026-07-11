"""Unit tests for citypods/ctc_align.py — all torch/torchaudio calls are mocked."""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest

import citypods.ctc_align as ctc_align
from citypods.ctc_align import (
    CtcFitResult,
    UnsupportedLanguageError,
    ctc_fit,
    normalize_words,
)

DICTIONARY = {
    "-": 0,
    "a": 1,
    "i": 2,
    "e": 3,
    "n": 4,
    "o": 5,
    "u": 6,
    "t": 7,
    "s": 8,
    "r": 9,
    "m": 10,
    "k": 11,
    "l": 12,
    "d": 13,
    "g": 14,
    "h": 15,
    "y": 16,
    "b": 17,
    "p": 18,
    "w": 19,
    "c": 20,
    "v": 21,
    "j": 22,
    "z": 23,
    "f": 24,
    "'": 25,
    "q": 26,
    "x": 27,
    "*": 28,
}


class _FakeSpan:
    def __init__(self, token: int, length: int, score: float):
        self.token = token
        self._length = length
        self.score = score

    def __len__(self) -> int:
        return self._length


class _FakeWaveform:
    """Minimal tensor-like stand-in supporting exactly the ops ctc_align.py performs."""

    def __init__(self, channels: int, samples: int):
        self.shape = (channels, samples)

    def mean(self, dim, keepdim):  # noqa: ARG002 - signature mirrors torch.Tensor.mean
        return _FakeWaveform(1, self.shape[1])

    def numel(self) -> int:
        return self.shape[0] * self.shape[1]


class _FakeEmission:
    """Stand-in for the model's (1, time, labels) output; emission[0] is what's passed on."""

    def __getitem__(self, idx):
        return self


def _inject_fake_torch_and_torchaudio(
    *,
    model=None,
    tokenizer=None,
    aligner=None,
    sample_rate: int = 16000,
    dictionary: dict | None = None,
    native_sample_rate: int = 16000,
    resample=None,
):
    """Install fake `torch`/`torchaudio` modules so ctc_align.py's lazy imports resolve to
    controllable stand-ins, mirroring test_asr.py's _inject_fw/_inject_sw pattern."""
    torch_mod = ModuleType("torch")
    torch_mod.inference_mode = MagicMock()
    torch_mod.inference_mode.return_value.__enter__ = MagicMock(return_value=None)
    torch_mod.inference_mode.return_value.__exit__ = MagicMock(return_value=False)
    sys.modules["torch"] = torch_mod

    torchaudio_mod = ModuleType("torchaudio")

    bundle = MagicMock()
    bundle.get_model.return_value = model if model is not None else MagicMock()
    bundle.get_tokenizer.return_value = tokenizer if tokenizer is not None else MagicMock()
    bundle.get_aligner.return_value = aligner if aligner is not None else MagicMock()
    bundle.get_dict.return_value = dictionary if dictionary is not None else dict(DICTIONARY)
    bundle.sample_rate = sample_rate

    pipelines_mod = ModuleType("torchaudio.pipelines")
    pipelines_mod.MMS_FA = bundle
    torchaudio_mod.pipelines = pipelines_mod

    functional_mod = ModuleType("torchaudio.functional")
    functional_mod.resample = resample if resample is not None else (lambda wf, sr, target: wf)
    torchaudio_mod.functional = functional_mod

    info_result = MagicMock()
    info_result.sample_rate = native_sample_rate
    torchaudio_mod.info = MagicMock(return_value=info_result)
    torchaudio_mod.load = MagicMock(
        return_value=(_FakeWaveform(1, native_sample_rate * 2), native_sample_rate)
    )

    sys.modules["torchaudio"] = torchaudio_mod
    return bundle


@pytest.fixture(autouse=True)
def _reset_model_cache():
    ctc_align._model_cache.clear()
    yield
    ctc_align._model_cache.clear()
    sys.modules.pop("torch", None)
    sys.modules.pop("torchaudio", None)


class TestNormalizeWords:
    def test_lowercases_and_strips_punctuation(self):
        assert normalize_words("Hello, World!") == ["hello", "world"]

    def test_keeps_apostrophes(self):
        assert normalize_words("It's raining") == ["it's", "raining"]

    def test_drops_digits_and_empty_result(self):
        assert normalize_words("12345 67890") == []

    def test_vocabulary_filters_out_of_vocabulary_words(self):
        vocab = set("abcdefghijklmnopqrstuvwxyz'")
        # "café" contains é, outside the plain a-z' vocabulary -> word_re already only grabs
        # "caf", but if it contained a char inside [a-z'] plus an out-of-vocab char this would
        # still be excluded by the vocabulary filter.
        assert normalize_words("hello café world", vocabulary=vocab) == ["hello", "caf", "world"]


class TestCtcFitLanguageGate:
    def test_rejects_non_english_language_without_importing_torch(self):
        # No fake torch/torchaudio injected: if this reached _load_bundle() it would raise
        # ModuleNotFoundError instead, proving the language gate runs first.
        sys.modules.pop("torch", None)
        sys.modules.pop("torchaudio", None)
        with pytest.raises(UnsupportedLanguageError):
            ctc_fit("audio.m4a", "bonjour le monde", language="fr")

    def test_accepts_missing_language_as_english(self):
        _inject_fake_torch_and_torchaudio()
        # Empty text after normalization short-circuits before any real inference call.
        result = ctc_fit("audio.m4a", "1234", language=None)
        assert result == CtcFitResult(0.0, 0.0, 0, 0)

    def test_accepts_english_variants(self):
        _inject_fake_torch_and_torchaudio()
        result = ctc_fit("audio.m4a", "", language="en-US")
        assert result.word_count == 0


class TestCtcFitEmptyInput:
    def test_returns_empty_result_for_no_normalizable_words(self):
        _inject_fake_torch_and_torchaudio()
        result = ctc_fit("audio.m4a", "12345 !!! ---")
        assert result == CtcFitResult(
            mean_score=0.0, coverage=0.0, word_count=0, aligned_word_count=0
        )


class TestCtcFitScoring:
    def _setup(self, *, tokens_return, spans_return):
        model = MagicMock(return_value=(_FakeEmission(), None))
        tokenizer = MagicMock(return_value=tokens_return)
        aligner = MagicMock(return_value=spans_return)
        _inject_fake_torch_and_torchaudio(model=model, tokenizer=tokenizer, aligner=aligner)
        return model, tokenizer, aligner

    def test_length_weighted_mean_score_and_full_coverage(self):
        # Two words, each with two equal-length token spans -> mean_score is the simple average
        # of each word's own average score.
        spans = [
            [_FakeSpan(1, 2, 0.8), _FakeSpan(2, 2, 0.6)],  # word 1 avg = 0.7
            [_FakeSpan(3, 1, 0.4), _FakeSpan(4, 1, 0.2)],  # word 2 avg = 0.3
        ]
        self._setup(tokens_return=[[1, 2], [3, 4]], spans_return=spans)

        result = ctc_fit("audio.m4a", "hello world", clip_start=0.0, clip_end=2.0)

        assert result.word_count == 2
        assert result.aligned_word_count == 2
        assert result.coverage == 1.0
        assert result.mean_score == pytest.approx((0.7 + 0.3) / 2, abs=1e-4)

    def test_zero_length_span_excluded_from_coverage_and_score(self):
        spans = [
            [_FakeSpan(1, 2, 0.9)],  # word 1: real span
            [_FakeSpan(2, 0, 0.0)],  # word 2: degenerate (zero-length) span
        ]
        self._setup(tokens_return=[[1], [2]], spans_return=spans)

        result = ctc_fit("audio.m4a", "hello world")

        assert result.word_count == 2
        assert result.aligned_word_count == 1
        assert result.coverage == 0.5
        assert result.mean_score == pytest.approx(0.9, abs=1e-4)

    def test_all_degenerate_spans_yield_zero_score_not_crash(self):
        spans = [[_FakeSpan(1, 0, 0.0)]]
        self._setup(tokens_return=[[1]], spans_return=spans)

        result = ctc_fit("audio.m4a", "hello")

        assert result.aligned_word_count == 0
        assert result.coverage == 0.0
        assert result.mean_score == 0.0


class TestModelCaching:
    def test_bundle_loaded_once_across_calls(self):
        bundle = _inject_fake_torch_and_torchaudio()
        ctc_fit("audio.m4a", "12345")  # empty-words short-circuit still loads the bundle
        ctc_fit("audio.m4a", "67890")
        assert bundle.get_model.call_count == 1
        assert bundle.get_tokenizer.call_count == 1
        assert bundle.get_aligner.call_count == 1


class TestAudioClipLoading:
    def test_clip_window_converted_to_native_sample_rate_frame_offset(self):
        model = MagicMock(return_value=(_FakeEmission(), None))
        tokenizer = MagicMock(return_value=[[1]])
        aligner = MagicMock(return_value=[[_FakeSpan(1, 1, 0.5)]])
        _inject_fake_torch_and_torchaudio(
            model=model,
            tokenizer=tokenizer,
            aligner=aligner,
            native_sample_rate=44100,
            sample_rate=16000,
        )

        ctc_fit("audio.m4a", "hello", clip_start=2.0, clip_end=5.0)

        load_call = sys.modules["torchaudio"].load
        _args, kwargs = load_call.call_args
        assert kwargs["frame_offset"] == int(2.0 * 44100)
        assert kwargs["num_frames"] == int(3.0 * 44100)
