"""Tests for citypods.diarize -- the sherpa-onnx/TitaNet-Small adapter (review/31 §A.1a).

sherpa_onnx and network access are mocked throughout; nothing here downloads a real model or
runs real inference (that's exercised by the offline trial this engine was chosen from, not
CI). What's tested is this module's own glue: config validation, turn/cluster normalization,
overlap marking, and the best-effort embedding contract.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from citypods.diarize import (
    _EMBEDDING_RECIPES,
    DEFAULT_EMBEDDING_MODEL,
    _ensure_embedding_model,
    diarize,
    has_valid_timed_words,
)


class _FakeSegment:
    def __init__(self, start: float, end: float, speaker: int):
        self.start = start
        self.end = end
        self.speaker = speaker


class _FakeResult:
    def __init__(self, segments: list[_FakeSegment]):
        self._segments = segments

    def sort_by_start_time(self):
        return sorted(self._segments, key=lambda s: s.start)


class _FakeDiarizer:
    sample_rate = 16000

    def __init__(self, segments: list[_FakeSegment]):
        self._segments = segments

    def process(self, samples):
        return _FakeResult(self._segments)


def _install_fake_sherpa_onnx(monkeypatch, segments: list[_FakeSegment]):
    fake = types.ModuleType("sherpa_onnx")
    fake.OfflineSpeakerDiarizationConfig = MagicMock(return_value=MagicMock(validate=lambda: True))
    fake.OfflineSpeakerSegmentationModelConfig = MagicMock()
    fake.OfflineSpeakerSegmentationPyannoteModelConfig = MagicMock()
    fake.SpeakerEmbeddingExtractorConfig = MagicMock()
    fake.FastClusteringConfig = MagicMock()
    fake.OfflineSpeakerDiarization = MagicMock(return_value=_FakeDiarizer(segments))
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake)
    return fake


def test_ensure_embedding_model_rejects_unknown_recipe():
    with pytest.raises(ValueError, match="unknown diarize embedding model"):
        _ensure_embedding_model("not-a-real-model")


def test_diarize_rejects_unknown_embedding_model_before_any_network_activity(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "citypods.diarize._ensure_segmentation_model",
        lambda: pytest.fail("should not fetch models for a rejected config"),
    )
    with pytest.raises(ValueError, match="unknown diarize embedding model"):
        diarize(tmp_path / "audio.m4a", embedding_model="bogus")


def test_diarize_normalizes_turns_and_clusters(monkeypatch, tmp_path):
    segments = [
        _FakeSegment(0.0, 5.0, 0),
        _FakeSegment(4.5, 9.0, 1),  # overlaps the first turn
        _FakeSegment(9.0, 12.0, 0),
    ]
    _install_fake_sherpa_onnx(monkeypatch, segments)
    monkeypatch.setattr(
        "citypods.diarize._ensure_segmentation_model", lambda: Path("/fake/seg.onnx")
    )
    monkeypatch.setattr(
        "citypods.diarize._ensure_embedding_model", lambda name: Path("/fake/emb.onnx")
    )
    monkeypatch.setattr(
        "citypods.diarize._load_waveform", lambda path, sr: np.zeros(12 * sr, dtype=np.float32)
    )
    monkeypatch.setattr("citypods.diarize._attach_embeddings", lambda *a, **k: None)

    artifact = diarize(tmp_path / "audio.m4a", num_threads=1)

    assert artifact.engine == "sherpa-onnx"
    assert DEFAULT_EMBEDDING_MODEL in artifact.model
    assert [t["cluster"] for t in artifact.turns] == ["0", "1", "0"]
    # The first two turns genuinely overlap (4.5-5.0s); the third does not overlap anything.
    assert artifact.turns[0]["overlap"] is True
    assert artifact.turns[1]["overlap"] is True
    assert artifact.turns[2]["overlap"] is False
    clusters_by_id = {c["cluster"]: c for c in artifact.clusters}
    assert clusters_by_id["0"]["turn_count"] == 2
    assert clusters_by_id["1"]["turn_count"] == 1


def test_diarize_uses_the_recipes_own_calibrated_threshold_by_default(monkeypatch, tmp_path):
    _install_fake_sherpa_onnx(monkeypatch, [])
    monkeypatch.setattr(
        "citypods.diarize._ensure_segmentation_model", lambda: Path("/fake/seg.onnx")
    )
    monkeypatch.setattr(
        "citypods.diarize._ensure_embedding_model", lambda name: Path("/fake/emb.onnx")
    )
    monkeypatch.setattr(
        "citypods.diarize._load_waveform", lambda path, sr: np.zeros(sr, dtype=np.float32)
    )
    monkeypatch.setattr("citypods.diarize._attach_embeddings", lambda *a, **k: None)
    fake = sys.modules["sherpa_onnx"]

    diarize(tmp_path / "audio.m4a", embedding_model="wespeaker-campp")

    used_threshold = fake.FastClusteringConfig.call_args.kwargs["threshold"]
    assert used_threshold == _EMBEDDING_RECIPES["wespeaker-campp"]["clustering_threshold"]


def test_diarize_lets_caller_override_the_clustering_threshold(monkeypatch, tmp_path):
    _install_fake_sherpa_onnx(monkeypatch, [])
    monkeypatch.setattr(
        "citypods.diarize._ensure_segmentation_model", lambda: Path("/fake/seg.onnx")
    )
    monkeypatch.setattr(
        "citypods.diarize._ensure_embedding_model", lambda name: Path("/fake/emb.onnx")
    )
    monkeypatch.setattr(
        "citypods.diarize._load_waveform", lambda path, sr: np.zeros(sr, dtype=np.float32)
    )
    monkeypatch.setattr("citypods.diarize._attach_embeddings", lambda *a, **k: None)
    fake = sys.modules["sherpa_onnx"]

    diarize(tmp_path / "audio.m4a", clustering_threshold=0.42)

    assert fake.FastClusteringConfig.call_args.kwargs["threshold"] == 0.42


def test_diarize_passes_num_threads_through_to_both_model_configs(monkeypatch, tmp_path):
    _install_fake_sherpa_onnx(monkeypatch, [])
    monkeypatch.setattr(
        "citypods.diarize._ensure_segmentation_model", lambda: Path("/fake/seg.onnx")
    )
    monkeypatch.setattr(
        "citypods.diarize._ensure_embedding_model", lambda name: Path("/fake/emb.onnx")
    )
    monkeypatch.setattr(
        "citypods.diarize._load_waveform", lambda path, sr: np.zeros(sr, dtype=np.float32)
    )
    monkeypatch.setattr("citypods.diarize._attach_embeddings", lambda *a, **k: None)
    fake = sys.modules["sherpa_onnx"]

    diarize(tmp_path / "audio.m4a", num_threads=1)

    assert fake.OfflineSpeakerSegmentationModelConfig.call_args.kwargs["num_threads"] == 1
    assert fake.SpeakerEmbeddingExtractorConfig.call_args.kwargs["num_threads"] == 1


def test_diarize_raises_on_invalid_config(monkeypatch, tmp_path):
    fake = _install_fake_sherpa_onnx(monkeypatch, [])
    fake.OfflineSpeakerDiarizationConfig = MagicMock(return_value=MagicMock(validate=lambda: False))
    monkeypatch.setattr(
        "citypods.diarize._ensure_segmentation_model", lambda: Path("/fake/seg.onnx")
    )
    monkeypatch.setattr(
        "citypods.diarize._ensure_embedding_model", lambda name: Path("/fake/emb.onnx")
    )

    with pytest.raises(RuntimeError, match="invalid sherpa-onnx diarize config"):
        diarize(tmp_path / "audio.m4a")


def test_attach_embeddings_is_best_effort_on_extractor_failure(monkeypatch, tmp_path):
    """A broken embedding extractor must not fail diarization -- turns just stay anonymous,
    matching the prior pyannote adapter's contract."""
    segments = [_FakeSegment(0.0, 1.0, 0)]
    fake = _install_fake_sherpa_onnx(monkeypatch, segments)
    fake.SpeakerEmbeddingExtractor = MagicMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(
        "citypods.diarize._ensure_segmentation_model", lambda: Path("/fake/seg.onnx")
    )
    monkeypatch.setattr(
        "citypods.diarize._ensure_embedding_model", lambda name: Path("/fake/emb.onnx")
    )
    monkeypatch.setattr(
        "citypods.diarize._load_waveform", lambda path, sr: np.zeros(sr, dtype=np.float32)
    )

    artifact = diarize(tmp_path / "audio.m4a")

    assert "embedding" not in artifact.turns[0]


def test_has_valid_timed_words_unchanged_by_the_engine_swap():
    assert has_valid_timed_words({"word_segments": [{"start": 0.0, "end": 1.0, "word": "hi"}]})
    assert not has_valid_timed_words({"word_segments": []})
