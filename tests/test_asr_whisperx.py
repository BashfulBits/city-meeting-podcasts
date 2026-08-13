from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest

import citypods.asr as asr


def _fake_whisperx(result):
    module = ModuleType("whisperx")
    module.load_audio = lambda path: [0.0]
    module.align = lambda *args, **kwargs: result
    sys.modules["whisperx"] = module


def _loaded():
    return asr._LoadedAlignmentModel("test", "en", object(), {}, "cpu")


def _sections():
    return [{"start": 0.0, "end": 10.0, "text": "one two three four five six seven eight nine ten"}]


def test_raw_coverage_gate_runs_before_interpolation(tmp_path: Path):
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"audio")
    _fake_whisperx(
        {
            "segments": [
                {
                    "start": 0.0,
                    "end": 10.0,
                    "words": [
                        {"word": "one", "start": 1.0, "end": 2.0},
                        {"word": "two", "start": None, "end": None},
                        {"word": "three", "start": None, "end": None},
                        {"word": "four", "start": 8.0, "end": 9.0},
                    ],
                }
            ]
        }
    )
    with pytest.raises(asr.AlignmentQualityError, match="90%"):
        asr.align_known_text(audio, _sections(), _loaded(), "en", 1, "linear")


def test_successful_alignment_interpolates_after_gate(tmp_path: Path):
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"audio")
    _fake_whisperx(
        {
            "segments": [
                {
                    "start": 0.0,
                    "end": 10.0,
                    "words": [
                        {"word": "one", "start": 1.0, "end": 2.0},
                        {"word": "two", "start": None, "end": None},
                        {"word": "three", "start": 3.0, "end": 3.5},
                        {"word": "four", "start": 4.0, "end": 4.5},
                        {"word": "five", "start": 5.0, "end": 5.5},
                        {"word": "six", "start": 6.0, "end": 6.5},
                        {"word": "seven", "start": 7.0, "end": 7.5},
                        {"word": "eight", "start": 8.0, "end": 8.5},
                        {"word": "nine", "start": 8.5, "end": 9.0},
                        {"word": "ten", "start": 9.0, "end": 9.5},
                    ],
                }
            ]
        }
    )
    artifacts = asr.align_known_text(audio, _sections(), _loaded(), "en", 1, "linear")
    assert artifacts.coverage == 0.9
    assert b"one two three four five six seven eight nine ten" in artifacts.vtt
    assert b'"s":2.0' in artifacts.words
