from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from citypods.asr import TranscriptArtifacts
from citypods.compute.base import InferenceJob
from citypods.compute.local_process import InferenceProcessTerminated, ProcessLocalBackend


class _FakeAsr:
    class AlignmentQualityError(RuntimeError):
        pass

    def load_model(self, model, compute_type, cpu_threads):
        return f"loaded:{model}:{compute_type}:{cpu_threads}"

    def transcribe(
        self, audio_path, model, language, compute_type, beam_size, initial_prompt, cpu_threads
    ):
        if initial_prompt == "sleep":
            time.sleep(30)
        return TranscriptArtifacts(vtt=f"WEBVTT:{model}".encode(), words=b"{}")

    def align(self, audio_path, text, model, language, cpu_threads):
        return TranscriptArtifacts(vtt=f"WEBVTT:{text}:{model}".encode(), words=b"{}")


def _job(*, prompt="prompt"):
    return InferenceJob(
        task="transcribe",
        inputs={
            "audio_path": Path("/tmp/audio.m4a"),
            "model": "large-v3-turbo",
            "language": "en",
            "compute_type": "int8",
            "beam_size": 5,
            "initial_prompt": prompt,
            "cpu_threads": 4,
        },
        recipe_hash="recipe",
    )


def test_spawn_worker_starts_and_returns_serialized_error_without_asr_dependencies():
    backend = ProcessLocalBackend(start_method="spawn")
    try:
        with pytest.raises(RuntimeError, match="does not implement task 'diarize'"):
            backend.run_inference(
                InferenceJob(
                    task="diarize",
                    inputs={"model": "unused"},
                    recipe_hash="recipe",
                )
            )
    finally:
        backend.close()


@pytest.mark.skipif("fork" not in __import__("multiprocessing").get_all_start_methods(), reason="")
def test_process_backend_returns_artifacts_and_reuses_worker():
    backend = ProcessLocalBackend(start_method="fork", asr=_FakeAsr())
    try:
        first_pid = None
        result = backend.run_inference(_job())
        first_pid = backend._process.pid
        second = backend.run_inference(_job(prompt="again"))
        assert result.output.vtt.startswith(b"WEBVTT:loaded:large-v3-turbo")
        assert second.output.words == b"{}"
        assert backend._process.pid == first_pid
    finally:
        backend.close()


@pytest.mark.skipif("fork" not in __import__("multiprocessing").get_all_start_methods(), reason="")
def test_process_backend_can_kill_and_restart_stuck_inference():
    backend = ProcessLocalBackend(start_method="fork", asr=_FakeAsr())
    result: dict[str, object] = {}

    def _run():
        try:
            backend.run_inference(_job(prompt="sleep"))
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc

    thread = threading.Thread(target=_run)
    thread.start()
    deadline = time.monotonic() + 5
    while backend._process is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert backend._process is not None, "process worker did not start within timeout"
    old_pid = backend._process.pid
    assert backend.terminate_active() is True
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert isinstance(result["error"], InferenceProcessTerminated)

    try:
        restarted = backend.run_inference(_job(prompt="restart"))
        assert restarted.output.words == b"{}"
        assert backend._process.pid != old_pid
    finally:
        backend.close()
