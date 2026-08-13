from __future__ import annotations

import tomllib
from pathlib import Path


def _extras() -> dict[str, list[str]]:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    return tomllib.loads(pyproject.read_text())["project"]["optional-dependencies"]


def test_transcribe_extra_excludes_alignment_stack():
    extras = _extras()
    transcribe = " ".join(extras["asr-transcribe"]).lower()
    assert "faster-whisper" in transcribe
    assert "stable-ts" not in transcribe
    assert "jiwer" not in transcribe


def test_align_and_bench_extras_are_explicit():
    extras = _extras()
    align = " ".join(extras["asr-align"]).lower()
    bench = " ".join(extras["asr-bench"]).lower()
    assert "whisperx" in align
    assert "whisperx" in bench
    assert "faster-whisper" in bench
    assert "jiwer" in bench


def test_legacy_asr_extra_remains_the_full_aggregate():
    aggregate = " ".join(_extras()["asr"]).lower()
    assert "faster-whisper" in aggregate
    assert "whisperx" in aggregate
    assert "jiwer" in aggregate
