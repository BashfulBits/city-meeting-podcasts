"""Shared WER/CER computation (``citypods asr-bench``; H15 Layer 3 calibration).

Requires the optional ``jiwer`` dependency — either the lean ``wer`` extra, or the heavier
``asr``/``asr-bench`` extras that already pull it in for their own purposes.
"""

from __future__ import annotations

import re


def require_jiwer():
    try:
        import jiwer
    except ImportError as exc:
        raise ImportError(
            "jiwer is required for WER/CER computation. Install: pip install 'citypods[wer]'"
        ) from exc
    return jiwer


def _normalize_for_cer(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def wer_cer(reference: str, hypothesis: str) -> dict:
    """Word Error Rate and Character Error Rate between ``reference`` (gold) and ``hypothesis``
    (candidate). Word-level comparison is normalized (lowercase, punctuation-stripped,
    whitespace-collapsed) via jiwer's own transform stack; character-level comparison uses the
    same normalization applied directly (jiwer's ``cer()`` scores raw strings, not word lists).

    An empty hypothesis against a non-empty reference scores as fully wrong (1.0) rather than
    raising — jiwer itself can't score an empty transcript meaningfully. Symmetric for an empty
    reference. Both empty scores as a perfect (trivial) match.
    """
    jiwer = require_jiwer()
    ref = (reference or "").strip()
    hyp = (hypothesis or "").strip()
    if not ref or not hyp:
        score = 1.0 if (ref or hyp) else 0.0
        return {"wer": score, "cer": score}
    word_transform = jiwer.transforms.Compose(
        [
            jiwer.transforms.ToLowerCase(),
            jiwer.transforms.RemovePunctuation(),
            jiwer.transforms.RemoveMultipleSpaces(),
            jiwer.transforms.Strip(),
            jiwer.transforms.ReduceToListOfListOfWords(),
        ]
    )
    wer = jiwer.wer(
        ref, hyp, reference_transform=word_transform, hypothesis_transform=word_transform
    )
    cer = jiwer.cer(_normalize_for_cer(ref), _normalize_for_cer(hyp))
    return {"wer": float(wer), "cer": float(cer)}
