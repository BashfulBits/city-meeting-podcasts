"""Tests for the H13 GPU/ASR execution-backend interface and its ``local`` adapter.

The headline acceptance: the ``local`` adapter yields **byte-identical** VTT + words.json to
the pre-refactor direct ``citypods.asr`` path. We assert that by running the same mocked
faster-whisper / stable-ts inputs through both ``asr.transcribe``/``asr.align`` directly and
through ``LocalBackend.run_inference`` and comparing the bytes.
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest

import citypods.asr as asr
from citypods.asr import TranscriptArtifacts
from citypods.compute import (
    Backend,
    InferenceJob,
    JobHandle,
    JobResult,
    LocalBackend,
    make_compute,
)
from citypods.compute.base import Task

# ── faster-whisper / stable-ts fakes (mirrors tests/test_asr.py) ──────────────


def _inject_fw(model_instance):
    mod = ModuleType("faster_whisper")
    mod.WhisperModel = MagicMock(return_value=model_instance)
    sys.modules["faster_whisper"] = mod
    return mod


def _inject_sw(model_instance):
    mod = ModuleType("stable_whisper")
    mod.load_faster_whisper = MagicMock(return_value=model_instance)
    sys.modules["stable_whisper"] = mod
    return mod


def _seg(start, end, text):
    s = MagicMock()
    s.start = start
    s.end = end
    s.text = text
    s.words = []  # no word-level data → segment-level fallback
    return s


def _fw_model(segments):
    model = MagicMock()
    model.transcribe.return_value = (iter(segments), MagicMock())
    return model


# ── interface / contract ──────────────────────────────────────────────────────


class TestInterface:
    def test_local_backend_satisfies_protocol(self):
        assert isinstance(LocalBackend(), Backend)

    def test_local_backend_name(self):
        assert LocalBackend().name == "local"

    def test_full_verb_set_is_typed(self):
        # base.py types the full §5.5 verb set — GPU/ASR verbs + reserved LLM verbs — so R3/R4's
        # adapter slots in with no interface change.
        assert set(Task.__args__) == {
            "transcribe",
            "align",
            "diarize",
            "summarize",
            "tag",
            "soundbite-select",
        }

    def test_make_compute_defaults_to_local(self):
        assert isinstance(make_compute({}), LocalBackend)
        assert isinstance(make_compute({"defaults": {"compute_backend": "local"}}), LocalBackend)

    def test_make_compute_env_override(self, monkeypatch):
        monkeypatch.setenv("COMPUTE_BACKEND", "local")
        assert isinstance(make_compute({"defaults": {"compute_backend": "nope"}}), LocalBackend)

    def test_make_compute_rejects_unknown(self):
        with pytest.raises(ValueError, match="unknown compute_backend"):
            make_compute({"defaults": {"compute_backend": "warp-drive"}})

    def test_unknown_task_rejected(self):
        with pytest.raises(ValueError, match="does not implement task"):
            LocalBackend(asr=MagicMock()).run_inference(InferenceJob(task="diarize"))

    def test_job_result_carries_task_and_recipe(self):
        fake = MagicMock()
        fake.transcribe.return_value = TranscriptArtifacts(vtt=b"WEBVTT\n", words=b"{}")
        result = LocalBackend(asr=fake).run_inference(
            InferenceJob(
                task="transcribe",
                inputs={
                    "audio_path": "a.m4a",
                    "model": "m",
                    "language": "en",
                    "compute_type": "int8",
                    "beam_size": 5,
                    "initial_prompt": None,
                    "cpu_threads": 4,
                },
                recipe_hash="cafef00d1234",
            )
        )
        assert isinstance(result, JobResult)
        assert result.task == "transcribe"
        assert result.recipe_hash == "cafef00d1234"

    def test_job_handle_shape(self):
        # JobHandle is the dispatch-backend return (H14); just lock its shape here.
        h = JobHandle(task="transcribe", recipe_hash="abc", backend="modal", ref="job-1")
        assert (h.task, h.recipe_hash, h.backend, h.ref) == (
            "transcribe",
            "abc",
            "modal",
            "job-1",
        )


# ── byte-identical: transcribe ─────────────────────────────────────────────────


class TestTranscribeByteIdentical:
    def test_local_matches_direct_asr(self, tmp_path):
        audio = tmp_path / "a.m4a"
        audio.write_bytes(b"fake")
        segs = [_seg(0.0, 1.5, "Council meeting called to order"), _seg(1.5, 3.0, "Roll call")]

        # Direct path (pre-refactor).
        _inject_fw(_fw_model(segs))
        direct = asr.transcribe(audio, "base.en", "en", "int8", 5, "prompt", 4)

        # Backend path — same inputs, same mocked model output.
        _inject_fw(_fw_model(segs))
        result = LocalBackend(asr=asr).run_inference(
            InferenceJob(
                task="transcribe",
                inputs={
                    "audio_path": audio,
                    "model": "base.en",
                    "language": "en",
                    "compute_type": "int8",
                    "beam_size": 5,
                    "initial_prompt": "prompt",
                    "cpu_threads": 4,
                },
                recipe_hash="r",
            )
        )
        out = result.output
        assert isinstance(out, TranscriptArtifacts)
        assert out.vtt == direct.vtt
        assert out.words == direct.words

    def test_passes_inputs_through_unchanged(self, tmp_path):
        # The adapter is a pure call-forward: every transcribe arg is forwarded positionally.
        fake = MagicMock()
        fake.transcribe.return_value = TranscriptArtifacts(vtt=b"v", words=b"w")
        LocalBackend(asr=fake).run_inference(
            InferenceJob(
                task="transcribe",
                inputs={
                    "audio_path": "a.m4a",
                    "model": "MODEL",
                    "language": "en",
                    "compute_type": "int8",
                    "beam_size": 7,
                    "initial_prompt": "PROMPT",
                    "cpu_threads": 3,
                },
            )
        )
        fake.transcribe.assert_called_once_with("a.m4a", "MODEL", "en", "int8", 7, "PROMPT", 3)


# ── byte-identical: align ──────────────────────────────────────────────────────


class TestAlignByteIdentical:
    def test_local_matches_direct_asr(self, tmp_path):
        audio = tmp_path / "a.m4a"
        audio.write_bytes(b"fake")
        vtt_str = "WEBVTT\n\n00:00:00.000 --> 00:00:05.000\nHello world\n"

        def _fresh_sw():
            fake_result = MagicMock()
            fake_result.to_vtt.return_value = vtt_str
            fake_result.segments = []
            fake_model = MagicMock()
            fake_model.align.return_value = fake_result
            _inject_sw(fake_model)

        _fresh_sw()
        direct = asr.align(audio, "Hello world", "base.en", "en", 4)

        _fresh_sw()
        result = LocalBackend(asr=asr).run_inference(
            InferenceJob(
                task="align",
                inputs={
                    "audio_path": audio,
                    "text": "Hello world",
                    "model": "base.en",
                    "language": "en",
                    "cpu_threads": 4,
                },
                recipe_hash="r",
            )
        )
        out = result.output
        assert out.vtt == direct.vtt
        assert out.words == direct.words

    def test_passes_inputs_through_unchanged(self):
        fake = MagicMock()
        fake.align.return_value = TranscriptArtifacts(vtt=b"v", words=b"w")
        LocalBackend(asr=fake).run_inference(
            InferenceJob(
                task="align",
                inputs={
                    "audio_path": "a.m4a",
                    "text": "TEXT",
                    "model": "MODEL",
                    "language": "en",
                    "cpu_threads": 2,
                },
            )
        )
        fake.align.assert_called_once_with("a.m4a", "TEXT", "MODEL", "en", 2)

    def test_alignment_quality_error_propagates(self):
        # The stage's align→transcribe fallback depends on the backend surfacing the asr exception
        # unchanged (it inspects ``asr.AlignmentQualityError``).
        fake = MagicMock()
        fake.align.side_effect = asr.AlignmentQualityError("too low")
        with pytest.raises(asr.AlignmentQualityError):
            LocalBackend(asr=fake).run_inference(
                InferenceJob(
                    task="align",
                    inputs={
                        "audio_path": "a.m4a",
                        "text": "t",
                        "model": "m",
                        "language": "en",
                        "cpu_threads": 1,
                    },
                )
            )
