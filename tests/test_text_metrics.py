from __future__ import annotations

import pytest

from citypods.text_metrics import require_jiwer, wer_cer

jiwer = pytest.importorskip("jiwer")


def test_wer_cer_identical_text_is_zero():
    result = wer_cer("the quick brown fox", "the quick brown fox")
    assert result == {"wer": 0.0, "cer": 0.0}


def test_wer_cer_normalizes_case_and_punctuation():
    result = wer_cer("The Quick, Brown Fox!", "the quick brown fox")
    assert result == {"wer": 0.0, "cer": 0.0}


def test_wer_cer_scores_a_word_substitution():
    result = wer_cer("the quick brown fox", "the quick brown dog")
    assert 0.0 < result["wer"] < 1.0
    assert 0.0 < result["cer"] < 1.0


def test_wer_cer_empty_hypothesis_scores_as_fully_wrong():
    result = wer_cer("the quick brown fox", "")
    assert result == {"wer": 1.0, "cer": 1.0}


def test_wer_cer_empty_reference_scores_as_fully_wrong():
    result = wer_cer("", "something was said")
    assert result == {"wer": 1.0, "cer": 1.0}


def test_wer_cer_both_empty_is_a_trivial_match():
    result = wer_cer("", "")
    assert result == {"wer": 0.0, "cer": 0.0}


def test_wer_cer_matches_bench_pys_original_inline_transform():
    """Regression guard for the bench.py extraction: the word-level transform stack here must
    produce the exact same WER bench.py's own (now-removed) inline Compose used to compute."""
    original_transform = jiwer.transforms.Compose(
        [
            jiwer.transforms.ToLowerCase(),
            jiwer.transforms.RemovePunctuation(),
            jiwer.transforms.RemoveMultipleSpaces(),
            jiwer.transforms.Strip(),
            jiwer.transforms.ReduceToListOfListOfWords(),
        ]
    )
    ref = "The Mayor called the meeting to order at 6:00 PM."
    hyp = "the mayor called the meeting to order at six pm"
    expected = jiwer.wer(
        ref, hyp, reference_transform=original_transform, hypothesis_transform=original_transform
    )
    assert wer_cer(ref, hyp)["wer"] == expected


def test_require_jiwer_returns_the_module():
    assert require_jiwer() is jiwer
