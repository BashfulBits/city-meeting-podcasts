from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest

import citypods.asr as asr


def _fake_whisperx(monkeypatch, result, *, load_align_model=None):
    module = ModuleType("whisperx")
    module.load_audio = lambda path: [0.0]
    module.align = lambda *args, **kwargs: result
    if load_align_model is not None:
        module.load_align_model = load_align_model
    monkeypatch.setitem(sys.modules, "whisperx", module)


def _loaded():
    return asr._LoadedAlignmentModel("test", "en", object(), {}, "cpu")


def _sections():
    return [{"start": 0.0, "end": 10.0, "text": "one two three four five six seven eight nine ten"}]


def test_raw_coverage_gate_runs_before_interpolation(tmp_path: Path, monkeypatch):
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"audio")
    _fake_whisperx(
        monkeypatch,
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
        },
    )
    with pytest.raises(asr.AlignmentQualityError, match="90%"):
        asr.align_known_text(audio, _sections(), _loaded(), "en", 1, "linear")


def test_successful_alignment_interpolates_after_gate(tmp_path: Path, monkeypatch):
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"audio")
    _fake_whisperx(
        monkeypatch,
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
        },
    )
    artifacts = asr.align_known_text(audio, _sections(), _loaded(), "en", 1, "linear")
    assert artifacts.coverage == 0.9
    assert b"one two three four five six seven eight nine ten" in artifacts.vtt
    assert b'"s":2.0' in artifacts.words


def test_nearest_interpolation_and_invalid_method(tmp_path: Path, monkeypatch):
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"audio")
    _fake_whisperx(
        monkeypatch,
        {
            "segments": [
                {
                    "start": 0.0,
                    "end": 10.0,
                    "words": [
                        {"word": "one", "start": 1.0, "end": 2.0},
                        {"word": "two", "start": None, "end": None},
                        {"word": "three", "start": 4.0, "end": 5.0},
                        *[
                            {"word": str(index), "start": float(index), "end": float(index) + 0.5}
                            for index in range(6, 13)
                        ],
                    ],
                }
            ]
        },
    )
    artifacts = asr.align_known_text(audio, _sections(), _loaded(), "en", 1, "nearest")
    assert b'"s":1.0' in artifacts.words
    with pytest.raises(ValueError, match="interpolate_method"):
        asr._interpolate_words([], "invalid")


def test_word_segments_fallback_owns_each_word_once():
    sections = [
        {"start": 0.0, "end": 5.0, "text": "one two"},
        {"start": 5.0, "end": 10.0, "text": "three four"},
    ]
    segments = asr._whisperx_segments(
        {
            "word_segments": [
                {"word": "one", "start": 1.0, "end": 1.5},
                {"word": "two", "start": None, "end": None},
                {"word": "three", "start": 6.0, "end": 6.5},
                {"word": "four", "start": None, "end": None},
            ]
        },
        sections,
    )
    assert [[word.word for word in segment.words] for segment in segments] == [
        ["one", "two"],
        ["three", "four"],
    ]


def test_alignment_model_load_is_cached_by_model_language_and_threads(monkeypatch):
    calls = []
    asr._model_cache.clear()
    _fake_whisperx(
        monkeypatch,
        {},
        load_align_model=lambda **kwargs: calls.append(kwargs) or (object(), {"x": 1}),
    )
    first = asr.load_alignment_model("model-a", "en", 2)
    assert asr.load_alignment_model("model-a", "en", 2) is first
    asr.load_alignment_model("model-a", "en", 3)
    assert calls == [
        {"language_code": "en", "device": "cpu", "model_name": "model-a"},
        {"language_code": "en", "device": "cpu", "model_name": "model-a"},
    ]


def test_open_final_section_is_closed_from_loaded_audio(tmp_path: Path, monkeypatch):
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"audio")
    result = {
        "segments": [
            {
                "words": [
                    {"word": str(index), "start": float(index), "end": float(index) + 0.5}
                    for index in range(10)
                ]
            }
        ]
    }
    _fake_whisperx(monkeypatch, result)
    artifacts = asr.align_known_text(
        audio,
        [{"start": 0.0, "end": None, "text": "open ended section"}],
        _loaded(),
        "en",
        1,
    )
    assert artifacts.coverage == 1.0


def test_finite_alignment_section_end_is_clamped_to_loaded_audio():
    bounded = asr._bounded_alignment_sections(
        [{"start": 0.25, "end": 9.0, "text": "stale duration"}],
        [0.0] * 16_000,
    )
    assert bounded == [{"start": 0.25, "end": 1.0, "text": "stale duration"}]
