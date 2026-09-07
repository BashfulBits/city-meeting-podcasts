"""Tests for citypods.diarize -- the sherpa-onnx/TitaNet-Small adapter (review/31 §A.1a).

sherpa_onnx and network access are mocked throughout; nothing here downloads a real model or
runs real inference (that's exercised by the offline trial this engine was chosen from, not
CI). What's tested is this module's own glue: config validation, turn/cluster normalization,
overlap marking, and the best-effort embedding contract.
"""

from __future__ import annotations

import os
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


def test_estimate_diarize_rss_bytes_matches_the_documented_constants():
    """Pins DIARIZE_RSS_BASE_BYTES/DIARIZE_RSS_PER_HOUR_BYTES so a change to either is a
    deliberate, documented act, not a silent drift -- this formula gates the admission
    MemoryReservation, and an unnoticed change there is a memory-safety regression, not a
    cosmetic one.

    review/31 §A.4's 2026-09-07 addendum re-validated (not re-derived) this formula against real
    measurements from 5min to 8h -- under DEFAULT_WINDOW_SHIFT_RATIO, real peak RSS fit
    368MB + 461MB/hr (R^2=0.9954, genuinely linear) with no evidence of accelerating growth, and
    the shipped 350MB + 650MB/hr overestimates it at every point past 5min. Left unchanged
    deliberately: that re-validation ran on local Apple Silicon, not the GH Actions Linux runners
    production uses, and this file's own §A.1a already documents a case where Apple Silicon
    numbers gave the wrong answer against real runner hardware -- so the formula stays exactly
    what it was, proven conservative rather than tightened on unvalidated-platform data.
    """
    from citypods.diarize import (
        DIARIZE_RSS_BASE_BYTES,
        DIARIZE_RSS_PER_HOUR_BYTES,
        estimate_diarize_rss_bytes,
    )

    assert DIARIZE_RSS_BASE_BYTES == 350 * 1024 * 1024
    assert DIARIZE_RSS_PER_HOUR_BYTES == 650 * 1024 * 1024

    # Spot-check at the durations actually measured (review/31 §A.4 addendum), converted to MiB
    # for readable tolerances. The formula only needs to stay at or above real measured usage.
    measured_mb = {
        300: 425,  # 5min
        1200: 553,  # 20min
        3600: 714,  # 60min
        7200: 1428,  # 2h
        14400: 2112,  # 4h
        28800: 4082,  # 8h
    }
    for recording_seconds, measured in measured_mb.items():
        predicted_mb = estimate_diarize_rss_bytes(recording_seconds) / (1024 * 1024)
        assert predicted_mb >= measured * 0.9, (
            f"formula predicts {predicted_mb:.0f}MiB at {recording_seconds}s, "
            f"real measured usage was {measured}MiB -- formula should stay conservative"
        )


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


def test_diarize_uses_the_default_window_shift_ratio_by_default(monkeypatch, tmp_path):
    from citypods.diarize import DEFAULT_WINDOW_SHIFT_RATIO

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

    diarize(tmp_path / "audio.m4a")

    used_ratio = fake.OfflineSpeakerSegmentationPyannoteModelConfig.call_args.kwargs[
        "window_shift_ratio"
    ]
    assert used_ratio == DEFAULT_WINDOW_SHIFT_RATIO
    assert DEFAULT_WINDOW_SHIFT_RATIO != 0.1  # sherpa-onnx's own default -- must be overridden


def test_diarize_lets_caller_override_the_window_shift_ratio(monkeypatch, tmp_path):
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

    diarize(tmp_path / "audio.m4a", window_shift_ratio=0.5)

    used_ratio = fake.OfflineSpeakerSegmentationPyannoteModelConfig.call_args.kwargs[
        "window_shift_ratio"
    ]
    assert used_ratio == 0.5


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


def test_diarize_raises_when_onnxruntime_logs_an_error_during_process(monkeypatch, tmp_path):
    """Regression (R7 Diarization run #59, GH Actions run 34072536373, "Diarize Denton pilot
    meetings", 2026-09-07; review/31 §A.1b): onnxruntime's own C++ logger can write an
    "[E:onnxruntime" line straight to the process's stderr fd -- bypassing Python's
    sys.stderr/logging entirely -- while `process()` still returns a normal-looking result: no
    Python exception, no visible change to the reported outcome. A job like that must not be
    silently accepted and published as a success."""

    class _NoisyDiarizer(_FakeDiarizer):
        def process(self, samples):
            os.write(
                2,
                b"2026-09-07 02:49:35.9310813 [E:onnxruntime:, sequential_executor.cc:620 "
                b"ExecuteKernel] Non-zero status code returned while running Where node. "
                b"Name:'/encoder/encoder/encoder.0/mconv.3/Where_1'\n",
            )
            return super().process(samples)

    fake = _install_fake_sherpa_onnx(monkeypatch, [])
    fake.OfflineSpeakerDiarization = MagicMock(return_value=_NoisyDiarizer([]))
    monkeypatch.setattr(
        "citypods.diarize._ensure_segmentation_model", lambda: Path("/fake/seg.onnx")
    )
    monkeypatch.setattr(
        "citypods.diarize._ensure_embedding_model", lambda name: Path("/fake/emb.onnx")
    )
    monkeypatch.setattr(
        "citypods.diarize._load_waveform", lambda path, sr: np.zeros(sr, dtype=np.float32)
    )

    with pytest.raises(RuntimeError, match="onnxruntime reported"):
        diarize(tmp_path / "audio.m4a")


class _FakeEmbeddingStream:
    def accept_waveform(self, sample_rate, data):
        pass

    def input_finished(self):
        pass


def test_diarize_raises_when_onnxruntime_logs_an_error_during_embedding_extraction(
    monkeypatch, tmp_path
):
    """`process()`'s own onnxruntime check (above) never covers `_attach_embeddings`, which
    runs afterward. Confirmed in production, 2026-09-07 (this exact gap, on real Denton
    meetings, before this fix): five separate long recordings each logged the identical
    "Where node" broadcast error mid-embedding-extraction and then reported a normal `diarize
    done` completion moments later, with no exception at all -- this must not be silently
    accepted as "no embedding, keep going" either."""

    class _NoisyExtractor:
        def create_stream(self):
            return _FakeEmbeddingStream()

        def is_ready(self, stream):
            return True

        def compute(self, stream):
            os.write(
                2,
                b"[E:onnxruntime:, sequential_executor.cc:620 ExecuteKernel] Non-zero status "
                b"code returned while running Where node.\n",
            )
            return [0.1, 0.2, 0.3]

    segments = [_FakeSegment(0.0, 1.0, 0)]
    fake = _install_fake_sherpa_onnx(monkeypatch, segments)
    fake.SpeakerEmbeddingExtractor = MagicMock(return_value=_NoisyExtractor())
    monkeypatch.setattr(
        "citypods.diarize._ensure_segmentation_model", lambda: Path("/fake/seg.onnx")
    )
    monkeypatch.setattr(
        "citypods.diarize._ensure_embedding_model", lambda name: Path("/fake/emb.onnx")
    )
    monkeypatch.setattr(
        "citypods.diarize._load_waveform", lambda path, sr: np.zeros(sr, dtype=np.float32)
    )

    with pytest.raises(RuntimeError, match="onnxruntime reported"):
        diarize(tmp_path / "audio.m4a")


def test_attach_embeddings_keeps_trying_other_turns_after_one_turn_fails(monkeypatch, tmp_path):
    """One turn raising a plain (non-onnxruntime) exception during extraction must not sacrifice
    every other turn's embedding -- a refinement made while restructuring this function for the
    onnxruntime check above, not present in the prior implementation's single try/except around
    the whole loop."""
    from citypods.diarize import _attach_embeddings

    calls = {"n": 0}

    class _FlakyExtractor:
        def create_stream(self):
            return _FakeEmbeddingStream()

        def is_ready(self, stream):
            return True

        def compute(self, stream):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom on the first turn only")
            return [0.1, 0.2, 0.3]

    fake = _install_fake_sherpa_onnx(monkeypatch, [])
    fake.SpeakerEmbeddingExtractor = MagicMock(return_value=_FlakyExtractor())
    turns = [
        {"start": 0.0, "end": 1.0, "cluster": "0"},
        {"start": 1.0, "end": 2.0, "cluster": "0"},
    ]

    _attach_embeddings(
        np.zeros(32000, dtype=np.float32), 16000, turns, Path("/fake/emb.onnx"), num_threads=1
    )

    assert "embedding" not in turns[0]
    assert turns[1]["embedding"] == [0.1, 0.2, 0.3]


def test_diarize_logs_but_does_not_raise_on_an_onnxruntime_warning(monkeypatch, tmp_path, capsys):
    """A warning-level onnxruntime line is surfaced (at minimum logged) but is not treated as a
    job failure -- only an error/fatal-level line is."""

    class _WarningDiarizer(_FakeDiarizer):
        def process(self, samples):
            os.write(2, b"[W:onnxruntime:, some_file.cc:1 SomeFunc] a non-fatal warning\n")
            return super().process(samples)

    segments = [_FakeSegment(0.0, 1.0, 0)]
    fake = _install_fake_sherpa_onnx(monkeypatch, segments)
    fake.OfflineSpeakerDiarization = MagicMock(return_value=_WarningDiarizer(segments))
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

    artifact = diarize(tmp_path / "audio.m4a")

    assert artifact.turns  # a warning does not turn a successful diarize into an error
    assert "onnxruntime warning" in capsys.readouterr().out


def test_diarize_restores_the_real_stderr_fd_after_capturing(monkeypatch, tmp_path, capfd):
    """The fd-2 redirect used to catch onnxruntime's own logging must not leak: once `diarize()`
    returns, fd 2 must point at the real stderr again, not the (by-then-closed) temp file."""
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

    diarize(tmp_path / "audio.m4a")
    os.write(2, b"after-diarize\n")

    assert "after-diarize" in capfd.readouterr().err


def test_has_valid_timed_words_unchanged_by_the_engine_swap():
    assert has_valid_timed_words({"word_segments": [{"start": 0.0, "end": 1.0, "word": "hi"}]})
    assert not has_valid_timed_words({"word_segments": []})


def test_decode_pins_a_narrow_protocol_whitelist_and_a_timeout(monkeypatch, tmp_path):
    """Every other ffmpeg call site in this project pins a protocol whitelist; this one is the
    narrowest, because the diarize input is always a local temp file. Without it a downloaded
    artifact that is really a manifest could make ffmpeg fetch the URLs it names. The timeout
    matters because this runs inside a worker before the next `ctx.stop()` check, so an unbounded
    decode of malformed media would hold its admission slot until the job's own timeout.
    """
    import subprocess

    import numpy as np

    from citypods.diarize import DECODE_TIMEOUT_SECONDS, _load_waveform

    seen = {}

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(cmd, 0, np.zeros(4, dtype=np.float32).tobytes(), b"")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    _load_waveform(tmp_path / "audio.m4a", 16000)

    assert "-protocol_whitelist" in seen["cmd"]
    whitelist = seen["cmd"][seen["cmd"].index("-protocol_whitelist") + 1]
    assert set(whitelist.split(",")) == {"file", "crypto", "data"}
    assert "http" not in whitelist and "tcp" not in whitelist
    assert seen["timeout"] == DECODE_TIMEOUT_SECONDS


def test_a_stuck_decode_becomes_an_actionable_error(monkeypatch, tmp_path):
    import subprocess

    from citypods.diarize import _load_waveform

    def _timeout(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout") or 0)

    monkeypatch.setattr(subprocess, "run", _timeout)
    with pytest.raises(RuntimeError, match="did not finish decoding"):
        _load_waveform(tmp_path / "audio.m4a", 16000)


def test_ffmpeg_error_detail_redacts_credentials_and_is_bounded():
    """ffmpeg echoes its input URL on failure, and that text reaches logs and a stored
    `speakers_error`. A signed media URL must not survive that trip."""
    from citypods.diarize import _ffmpeg_detail

    detail = _ffmpeg_detail(
        b"https://cdn.example/a.m4a?X-Amz-Signature=deadbeef&token=hunter2: Invalid data"
    )
    assert "deadbeef" not in detail
    assert "hunter2" not in detail
    assert detail.count("<redacted>") == 2
    assert len(_ffmpeg_detail(b"x" * 5000)) == 500


def test_diarize_rss_spike_margin_matches_the_documented_broadcast_evidence():
    """Pins DIARIZE_RSS_SPIKE_MARGIN_BYTES so a change to it is deliberate. Sized from real,
    directly measured `peak_rss_mb` on five production incidents (2026-09-07, review/31 §A.4
    addendum): the worst (a 15.09h recording) ran ~4.5GiB hotter than even the conservative
    350MB+650MB/hr formula predicts -- the margin must cover at least that gap, rounded up for
    headroom against a still-longer recording ratcheting further (the overshoot scales with
    recording length/turn count, not a fixed one-time spike)."""
    from citypods.diarize import DIARIZE_RSS_SPIKE_MARGIN_BYTES

    worst_observed_overshoot_bytes = int(4.5 * 1024 * 1024 * 1024)
    assert DIARIZE_RSS_SPIKE_MARGIN_BYTES >= worst_observed_overshoot_bytes
    assert DIARIZE_RSS_SPIKE_MARGIN_BYTES == 5 * 1024 * 1024 * 1024


def test_diarize_memory_ceiling_subtracts_the_spike_margin_once():
    from citypods.diarize import DIARIZE_RSS_SPIKE_MARGIN_BYTES, diarize_memory_ceiling_bytes

    margin_mb = DIARIZE_RSS_SPIKE_MARGIN_BYTES / (1024 * 1024)
    # Production's actual configured value (config/site_config.yml `speakers.memory_budget_mb`).
    assert diarize_memory_ceiling_bytes(14000) == int((14000 - margin_mb) * 1024 * 1024)


def test_diarize_memory_ceiling_passes_through_the_disabled_sentinel_unchanged():
    """`memory_budget_mb: 0` (or blank) means "admission disabled" -- not a budget to apply a
    safety margin to. Must stay exactly 0, not go negative or become some other sentinel."""
    from citypods.diarize import diarize_memory_ceiling_bytes

    assert diarize_memory_ceiling_bytes(0) == 0
    assert diarize_memory_ceiling_bytes(-5) == 0


def test_diarize_memory_ceiling_clamps_a_budget_smaller_than_the_margin_to_zero():
    """A configured budget smaller than the spike margin must clamp to 0 (disabled), never go
    negative -- `MemoryReservation` isn't built for a negative budget."""
    from citypods.diarize import diarize_memory_ceiling_bytes

    assert diarize_memory_ceiling_bytes(1024) == 0


def test_run_diarize_job_logs_peak_rss_and_the_log_label_on_success(monkeypatch, tmp_path, capsys):
    from citypods.diarize import DiarizeArtifacts, run_diarize_job

    fake_artifact = DiarizeArtifacts(turns=[], clusters=[], engine="sherpa-onnx", model="x")
    monkeypatch.setattr("citypods.diarize.diarize", lambda *a, **k: fake_artifact)
    monkeypatch.setattr("citypods.diarize._peak_rss_mb", lambda: 512.3)

    result = run_diarize_job(str(tmp_path / "audio.m4a"), log_label="uid-123")

    assert result is fake_artifact
    out = capsys.readouterr().out
    assert "peak_rss_mb=512.3" in out
    assert "label='uid-123'" in out


def test_run_diarize_job_reports_none_peak_rss_without_crashing(monkeypatch, tmp_path, capsys):
    """`_peak_rss_mb` returning `None` (platform without `/proc` or `resource`) must not crash
    the log line or the round() call on it."""
    from citypods.diarize import DiarizeArtifacts, run_diarize_job

    fake_artifact = DiarizeArtifacts(turns=[], clusters=[], engine="sherpa-onnx", model="x")
    monkeypatch.setattr("citypods.diarize.diarize", lambda *a, **k: fake_artifact)
    monkeypatch.setattr("citypods.diarize._peak_rss_mb", lambda: None)

    run_diarize_job(str(tmp_path / "audio.m4a"))
    assert "peak_rss_mb=None" in capsys.readouterr().out


def test_run_diarize_job_wraps_memory_error_with_peak_rss_and_label(monkeypatch, tmp_path):
    """A genuine `MemoryError` -- CPython failing to satisfy a malloc and recovering enough to
    raise it -- is real evidence of memory exhaustion, not a guess. It must not be swallowed or
    reported as a generic diarize error indistinguishable from any other failure."""
    from citypods.diarize import run_diarize_job

    def _boom(*a, **k):
        raise MemoryError("could not allocate 2.5 GiB")

    monkeypatch.setattr("citypods.diarize.diarize", _boom)
    monkeypatch.setattr("citypods.diarize._peak_rss_mb", lambda: 13500.0)

    with pytest.raises(RuntimeError, match=r"uid-999.*MemoryError.*peak_rss_mb=13500\.0") as exc:
        run_diarize_job(str(tmp_path / "audio.m4a"), log_label="uid-999")
    assert isinstance(exc.value.__cause__, MemoryError)


def test_run_diarize_job_wraps_oserror_enomem_but_not_other_oserrors(monkeypatch, tmp_path):
    """A native allocation failure (numpy/onnxruntime) can surface as `OSError(errno=ENOMEM)`
    instead of a Python-level `MemoryError` -- same underlying cause, must get the same
    enriched-and-re-raised treatment. Any *other* `OSError` (a real filesystem/IO problem) must
    pass through unchanged rather than being misreported as a memory issue it has nothing to do
    with."""
    import errno

    from citypods.diarize import run_diarize_job

    def _enomem(*a, **k):
        raise OSError(errno.ENOMEM, "Cannot allocate memory")

    monkeypatch.setattr("citypods.diarize.diarize", _enomem)
    monkeypatch.setattr("citypods.diarize._peak_rss_mb", lambda: 8000.0)
    with pytest.raises(RuntimeError, match=r"OSError.*peak_rss_mb=8000\.0"):
        run_diarize_job(str(tmp_path / "audio.m4a"))

    def _enoent(*a, **k):
        raise OSError(errno.ENOENT, "No such file or directory")

    monkeypatch.setattr("citypods.diarize.diarize", _enoent)
    with pytest.raises(OSError, match="No such file or directory"):
        run_diarize_job(str(tmp_path / "audio.m4a"))
