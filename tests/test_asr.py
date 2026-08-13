"""Unit tests for citypods/asr.py — all faster_whisper/stable_whisper calls are mocked."""

from __future__ import annotations

import json
import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest

from citypods.asr import (
    TranscriptArtifacts,
    _fmt_ts,
    _to_vtt,
    _to_words_json,
    asr_spec_hash,
    srt_to_text,
    vtt_to_text,
)


def _inject_fw(model_instance):
    """Inject a fake faster_whisper module so the lazy import inside transcribe() works."""
    mod = ModuleType("faster_whisper")
    mod.WhisperModel = MagicMock(return_value=model_instance)
    sys.modules.setdefault("faster_whisper", mod)
    return mod


def _inject_sw(model_instance):
    """Inject a fake stable_whisper module so the lazy import inside align() works."""
    mod = ModuleType("stable_whisper")
    mod.load_faster_whisper = MagicMock(return_value=model_instance)
    sys.modules.setdefault("stable_whisper", mod)
    return mod


# ── _fmt_ts ──────────────────────────────────────────────────────────────────


class TestFmtTs:
    def test_zero(self):
        assert _fmt_ts(0.0) == "00:00:00.000"

    def test_sub_minute(self):
        assert _fmt_ts(5.5) == "00:00:05.500"

    def test_over_minute(self):
        assert _fmt_ts(90.0) == "00:01:30.000"

    def test_over_hour(self):
        assert _fmt_ts(3661.0) == "01:01:01.000"

    def test_rounds_before_split_instead_of_overflowing_seconds_field(self):
        # CR2-CP-19: naive floor-division + "%06.3f" formatting rounded 119.9996 to a literal
        # "60.000" seconds field (invalid WebVTT); rounding whole milliseconds first carries
        # into the minutes field instead.
        assert _fmt_ts(119.9996) == "00:02:00.000"


# ── _to_vtt ──────────────────────────────────────────────────────────────────


class TestToVtt:
    def _seg(self, start, end, text):
        s = MagicMock()
        s.start = start
        s.end = end
        s.text = text
        s.words = []  # no word-level data → segment-level fallback
        return s

    def _word(self, word, start, end):
        w = MagicMock()
        w.word = word
        w.start = start
        w.end = end
        return w

    def test_starts_with_webvtt(self):
        vtt = _to_vtt([])
        assert vtt.startswith(b"WEBVTT")

    def test_single_cue(self):
        seg = self._seg(1.0, 5.0, "Hello world")
        vtt = _to_vtt([seg]).decode()
        assert "00:00:01.000 --> 00:00:05.000" in vtt
        assert "Hello world" in vtt

    def test_empty_text_segments_skipped(self):
        segs = [self._seg(0, 1, ""), self._seg(1, 2, "text"), self._seg(2, 3, "   ")]
        vtt = _to_vtt(segs).decode()
        assert vtt.count("-->") == 1
        assert "text" in vtt

    def test_multiple_cues(self):
        segs = [self._seg(0, 1, "A"), self._seg(1, 2, "B")]
        vtt = _to_vtt(segs).decode()
        assert vtt.count("-->") == 2
        assert "A" in vtt and "B" in vtt

    def test_segment_level_even_when_words_present(self):
        # H12: _to_vtt is segment-level only; per-word timing lives in the JSON sidecar.
        seg = self._seg(1.0, 5.0, "Hello world")
        seg.words = [self._word(" Hello", 1.0, 1.5), self._word(" world", 1.5, 5.0)]
        vtt = _to_vtt([seg]).decode()
        assert vtt.count("-->") == 1
        assert "00:00:01.000 --> 00:00:05.000" in vtt
        assert "Hello world" in vtt


# ── _to_words_json ────────────────────────────────────────────────────────────


class TestToWordsJson:
    def _seg(self, start, end, text, words=None):
        s = MagicMock()
        s.start = start
        s.end = end
        s.text = text
        s.words = words or []
        return s

    def _word(self, word, start, end):
        w = MagicMock()
        w.word = word
        w.start = start
        w.end = end
        return w

    def test_schema_and_basis(self):
        data = json.loads(_to_words_json([], basis="served"))
        assert data["schema"] == "2"
        assert data["basis"] == "served"
        assert data["segments"] == []

    def test_segment_and_word_timings(self):
        seg = self._seg(
            1.0,
            2.0,
            "Hello world",
            [self._word(" Hello", 1.0, 1.5), self._word(" world", 1.5, 2.0)],
        )
        data = json.loads(_to_words_json([seg]))
        s0 = data["segments"][0]
        assert s0["start"] == 1.0 and s0["end"] == 2.0
        assert s0["text"] == "Hello world"
        assert s0["words"] == [
            {"w": "Hello", "s": 1.0, "e": 1.5},
            {"w": "world", "s": 1.5, "e": 2.0},
        ]

    def test_skips_words_without_timestamps(self):
        seg = self._seg(
            0.5,
            1.0,
            "Good morning",
            [self._word(" Good", 0.5, 1.0), self._word(" morning", None, None)],
        )
        data = json.loads(_to_words_json([seg]))
        assert [w["w"] for w in data["segments"][0]["words"]] == ["Good"]

    def test_includes_probability_when_present(self):
        word = self._word(" Good", 0.5, 1.0)
        word.probability = 0.87123
        seg = self._seg(0.5, 1.0, "Good", [word])
        data = json.loads(_to_words_json([seg]))
        assert data["segments"][0]["words"][0]["p"] == 0.8712

    def test_omits_probability_when_absent_or_non_numeric(self):
        # A MagicMock auto-vivifies `.probability` as another MagicMock, not a float — the
        # `isinstance` guard must treat that as "no probability data", not crash on float().
        seg = self._seg(0.5, 1.0, "Good", [self._word(" Good", 0.5, 1.0)])
        data = json.loads(_to_words_json([seg]))
        assert "p" not in data["segments"][0]["words"][0]


# ── asr_spec_hash ─────────────────────────────────────────────────────────────


class TestAsrSpecHash:
    def test_deterministic(self):
        h1 = asr_spec_hash("abc", "small.en", None, "1")
        h2 = asr_spec_hash("abc", "small.en", None, "1")
        assert h1 == h2

    def test_length(self):
        h = asr_spec_hash("abc", "small.en", None, "1")
        assert len(h) == 12

    def test_changes_on_audio_hash(self):
        h1 = asr_spec_hash("aaa", "small.en", None, "1")
        h2 = asr_spec_hash("bbb", "small.en", None, "1")
        assert h1 != h2

    def test_changes_on_model(self):
        h1 = asr_spec_hash("abc", "small.en", None, "1")
        h2 = asr_spec_hash("abc", "large-v3-turbo", None, "1")
        assert h1 != h2

    def test_changes_on_version_bump(self):
        h1 = asr_spec_hash("abc", "small.en", None, "1")
        h2 = asr_spec_hash("abc", "small.en", None, "2")
        assert h1 != h2

    def test_none_vs_text_hash(self):
        h1 = asr_spec_hash("abc", "small.en", None, "1")
        h2 = asr_spec_hash("abc", "small.en", "deadbeef0000", "1")
        assert h1 != h2

    def test_changes_on_fresh_transcription_prompt_or_hints(self):
        base = asr_spec_hash(
            "abc",
            "small.en",
            None,
            "1",
            language="en",
            compute_type="int8",
            beam_size=5,
            initial_prompt="City. Council.",
        )
        assert base != asr_spec_hash(
            "abc",
            "small.en",
            None,
            "1",
            language="es",
            compute_type="int8",
            beam_size=5,
            initial_prompt="City. Council.",
        )
        assert base != asr_spec_hash(
            "abc",
            "small.en",
            None,
            "1",
            language="en",
            compute_type="int8",
            beam_size=1,
            initial_prompt="City. Council.",
        )
        assert base != asr_spec_hash(
            "abc",
            "small.en",
            None,
            "1",
            language="en",
            compute_type="int8",
            beam_size=5,
            initial_prompt="County. Council.",
        )
        assert base != asr_spec_hash(
            "abc",
            "small.en",
            None,
            "1",
            language="en",
            compute_type="float16",
            beam_size=5,
            initial_prompt="City. Council.",
        )

    def test_hexadecimal(self):
        h = asr_spec_hash("x", "m", None, "v")
        assert all(c in "0123456789abcdef" for c in h)


# ── transcribe() — mocked ────────────────────────────────────────────────────


class TestTranscribeMocked:
    def _seg(self, start, end, text):
        s = MagicMock()
        s.start = start
        s.end = end
        s.text = text
        s.words = []  # no word-level data → segment-level fallback
        return s

    def _mock_model(self, segments):
        model = MagicMock()
        model.transcribe.return_value = (iter(segments), MagicMock())
        return model

    def test_returns_vtt_bytes(self, tmp_path):
        from citypods.asr import transcribe

        audio = tmp_path / "a.m4a"
        audio.write_bytes(b"fake")

        model = self._mock_model([self._seg(0.0, 1.5, "Hello world")])
        _inject_fw(model)
        sys.modules["faster_whisper"].WhisperModel = MagicMock(return_value=model)

        result = transcribe(audio, "base.en", "en", "int8", 5, None, 4)

        assert isinstance(result, TranscriptArtifacts)
        assert result.vtt.startswith(b"WEBVTT")
        assert b"Hello world" in result.vtt
        words = json.loads(result.words)
        assert words["schema"] == "2" and words["basis"] == "served"

    def test_records_l1_coverage_and_word_logprob(self, tmp_path):
        from citypods.asr import transcribe

        audio = tmp_path / "a.m4a"
        audio.write_bytes(b"fake")

        seg = self._seg(0.0, 1.5, "Hello world")
        w1, w2 = MagicMock(), MagicMock()
        w1.word, w1.start, w1.end, w1.probability = "Hello", 0.0, 0.5, 0.9
        w2.word, w2.start, w2.end, w2.probability = "world", 0.5, 1.5, 0.7
        seg.words = [w1, w2]
        model = self._mock_model([seg])
        _inject_fw(model)
        sys.modules["faster_whisper"].WhisperModel = MagicMock(return_value=model)

        result = transcribe(audio, "base.en", "en", "int8", 5, None, 4)

        assert result.coverage == 1.0
        assert result.word_logprob_mean == pytest.approx(0.8)
        assert result.word_logprob_p10 == pytest.approx(0.7)

    def test_missing_dep_raises_import_error(self, tmp_path):
        from citypods.asr import transcribe

        audio = tmp_path / "a.m4a"
        audio.write_bytes(b"fake")

        saved = sys.modules.get("faster_whisper")
        sys.modules["faster_whisper"] = None
        try:
            try:
                transcribe(audio, "base.en", "en", "int8", 5, None, 4)
                raise AssertionError("Expected ImportError")
            except ImportError as exc:
                assert "citypods[asr-transcribe]" in str(exc)
        finally:
            if saved is not None:
                sys.modules["faster_whisper"] = saved
            else:
                sys.modules.pop("faster_whisper", None)


# ── align() — mocked ─────────────────────────────────────────────────────────


class TestAlignMocked:
    def _seg(self, words, start=0.0, end=1.0, text="hello world"):
        s = MagicMock()
        s.start = start
        s.end = end
        s.text = text
        s.words = words
        return s

    def _word(self, word="hello", start=0.0, end=0.5):
        w = MagicMock()
        w.word = word
        w.start = start
        w.end = end
        return w

    def _fully_timed_segments(self, n=2):
        # All words get a real `start` timestamp -> 100% coverage, well above the gate.
        return [self._seg([self._word(), self._word()]) for _ in range(n)]

    def _setup_sw(self, vtt_str: str, segments=None):
        import citypods.asr as asr

        # _load_alignment_model caches by (model, compute_type, cpu_threads); every test here
        # uses the same "base.en"/None/4 key, so a stale cached model from an earlier test
        # would silently shadow this test's fake_result otherwise.
        asr._model_cache.clear()
        fake_result = MagicMock()
        fake_result.to_vtt.return_value = vtt_str
        fake_result.segments = segments if segments is not None else self._fully_timed_segments()
        fake_model = MagicMock()
        fake_model.align.return_value = fake_result
        _inject_sw(fake_model)
        sys.modules["stable_whisper"].load_faster_whisper = MagicMock(return_value=fake_model)

    def test_returns_vtt_bytes(self, tmp_path):
        from citypods.asr import align

        audio = tmp_path / "a.m4a"
        audio.write_bytes(b"fake")

        self._setup_sw("WEBVTT\n\n00:00:00.000 --> 00:00:05.000\nHello world\n")
        result = align(audio, "Hello world", "base.en", "en", 4)

        assert isinstance(result, TranscriptArtifacts)
        assert result.vtt.startswith(b"WEBVTT")
        assert b"Hello world" in result.vtt
        assert result.coverage == 1.0

    def test_records_l1_word_logprob(self, tmp_path):
        from citypods.asr import align

        audio = tmp_path / "a.m4a"
        audio.write_bytes(b"fake")

        w1, w2 = self._word("hello", 0.0, 0.5), self._word("world", 0.5, 1.0)
        w1.probability = 0.6
        w2.probability = 0.4
        seg = self._seg([w1, w2])
        self._setup_sw("WEBVTT\n\n00:00:00.000 --> 00:00:05.000\nHello world\n", segments=[seg])

        result = align(audio, "hello world", "base.en", "en", 4)

        assert result.coverage == 1.0
        assert result.word_logprob_mean == pytest.approx(0.5)
        assert result.word_logprob_p10 == pytest.approx(0.4)

    def test_prepends_webvtt_if_missing(self, tmp_path):
        from citypods.asr import align

        audio = tmp_path / "a.m4a"
        audio.write_bytes(b"fake")

        self._setup_sw("00:00:00.000 --> 00:00:05.000\nHello\n")
        result = align(audio, "Hello", "base.en", "en", 4)

        assert result.vtt.startswith(b"WEBVTT")

    def test_zero_words_raises_quality_error_instead_of_passing_silently(self, tmp_path):
        # CR2-CP-18: a degenerate alignment result with no words at all (result.segments is
        # empty, or every segment lacks a `words` attribute) must not skip the coverage gate —
        # it is 0% coverage, not a vacuous pass.
        from citypods.asr import AlignmentQualityError, align

        audio = tmp_path / "a.m4a"
        audio.write_bytes(b"fake")

        self._setup_sw("WEBVTT\n\n00:00:00.000 --> 00:00:05.000\nHello world\n", segments=[])
        with pytest.raises(AlignmentQualityError, match="0%"):
            align(audio, "Hello world", "base.en", "en", 4)

    def test_loaded_faster_model_uses_stable_ts_model_for_alignment(self, tmp_path):
        import citypods.asr as asr

        audio = tmp_path / "a.m4a"
        audio.write_bytes(b"fake")

        asr._model_cache.clear()
        fast_model = MagicMock()
        fast_model.transcribe.return_value = (iter([]), MagicMock())
        _inject_fw(fast_model)
        sys.modules["faster_whisper"].WhisperModel = MagicMock(return_value=fast_model)

        fake_result = MagicMock()
        fake_result.to_vtt.return_value = "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello\n"
        fake_result.segments = self._fully_timed_segments()
        stable_model = MagicMock()
        stable_model.align.return_value = fake_result
        _inject_sw(stable_model)
        sys.modules["stable_whisper"].load_faster_whisper = MagicMock(return_value=stable_model)

        loaded = asr.load_model("base.en", "int8", 4)
        result = asr.align(audio, "Hello", loaded, "en", 4)

        assert result.vtt.startswith(b"WEBVTT")
        assert fast_model.align.call_count == 0
        sys.modules["stable_whisper"].load_faster_whisper.assert_called_once_with(
            "base.en",
            device="cpu",
            cpu_threads=4,
            compute_type="int8",
        )
        stable_model.align.assert_called_once_with(
            str(audio), "Hello", language="en", vad=True, fast_mode=True
        )

    def test_align_string_model_defaults_to_int8_and_fast_mode(self, tmp_path):
        import citypods.asr as asr

        audio = tmp_path / "a.m4a"
        audio.write_bytes(b"fake")

        asr._model_cache.clear()
        fake_result = MagicMock()
        fake_result.to_vtt.return_value = "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello\n"
        fake_result.segments = self._fully_timed_segments()
        stable_model = MagicMock()
        stable_model.align.return_value = fake_result
        _inject_sw(stable_model)
        sys.modules["stable_whisper"].load_faster_whisper = MagicMock(return_value=stable_model)

        result = asr.align(audio, "Hello", "base.en", "en", 4)

        assert result.vtt.startswith(b"WEBVTT")
        sys.modules["stable_whisper"].load_faster_whisper.assert_called_once_with(
            "base.en",
            device="cpu",
            cpu_threads=4,
            compute_type="int8",
        )
        stable_model.align.assert_called_once_with(
            str(audio), "Hello", language="en", vad=True, fast_mode=True
        )

    def test_align_custom_compute_type_and_flags(self, tmp_path):
        import citypods.asr as asr

        audio = tmp_path / "a.m4a"
        audio.write_bytes(b"fake")

        asr._model_cache.clear()
        fake_result = MagicMock()
        fake_result.to_vtt.return_value = "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello\n"
        fake_result.segments = self._fully_timed_segments()
        stable_model = MagicMock()
        stable_model.align.return_value = fake_result
        _inject_sw(stable_model)
        sys.modules["stable_whisper"].load_faster_whisper = MagicMock(return_value=stable_model)

        result = asr.align(
            audio,
            "Hello",
            "base.en",
            "en",
            4,
            compute_type="float32",
            vad=False,
            fast_mode=False,
        )

        assert result.vtt.startswith(b"WEBVTT")
        sys.modules["stable_whisper"].load_faster_whisper.assert_called_once_with(
            "base.en",
            device="cpu",
            cpu_threads=4,
            compute_type="float32",
        )
        stable_model.align.assert_called_once_with(
            str(audio), "Hello", language="en", vad=False, fast_mode=False
        )

    def test_align_words_uses_existing_timed_windows(self, tmp_path):
        import citypods.asr as asr

        audio = tmp_path / "a.m4a"
        audio.write_bytes(b"fake")

        asr._model_cache.clear()
        fake_result = MagicMock()
        fake_result.to_vtt.return_value = "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello\n"
        fake_result.segments = self._fully_timed_segments()
        stable_model = MagicMock()
        stable_model.align_words.return_value = fake_result
        _inject_sw(stable_model)
        sys.modules["stable_whisper"].load_faster_whisper = MagicMock(return_value=stable_model)

        timed_segments = [
            {"start": 12.0, "end": 15.0, "text": "Hello world"},
        ]
        result = asr.align(
            audio,
            "Hello world",
            "base.en",
            "en",
            4,
            timed_segments=timed_segments,
        )

        assert result.vtt.startswith(b"WEBVTT")
        stable_model.align_words.assert_called_once_with(str(audio), timed_segments, language="en")
        stable_model.align.assert_not_called()

    def test_align_serializes_current_stable_ts_result_api(self, tmp_path):
        import citypods.asr as asr

        audio = tmp_path / "a.m4a"
        audio.write_bytes(b"fake")

        asr._model_cache.clear()
        fake_result = MagicMock(spec=["to_srt_vtt", "segments"])
        fake_result.to_srt_vtt.return_value = "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello\n"
        fake_result.segments = self._fully_timed_segments()
        stable_model = MagicMock()
        stable_model.align.return_value = fake_result
        _inject_sw(stable_model)
        sys.modules["stable_whisper"].load_faster_whisper = MagicMock(return_value=stable_model)

        result = asr.align(audio, "Hello", "base.en", "en", 4)

        assert result.vtt.startswith(b"WEBVTT")
        fake_result.to_srt_vtt.assert_called_once_with(
            segment_level=True, word_level=False, vtt=True
        )


# ── vtt_to_text / srt_to_text ────────────────────────────────────────────────


class TestTextExtraction:
    VTT = (
        "WEBVTT\n\n"
        "00:00:01.000 --> 00:00:05.000\nHello world\n\n"
        "00:00:05.000 --> 00:00:09.000\nFoo bar\n"
    )
    SRT = (
        "1\n00:00:01,000 --> 00:00:05,000\nHello world\n\n"
        "2\n00:00:05,000 --> 00:00:09,000\nFoo bar\n"
    )

    def test_vtt_strips_header_and_cues(self):
        text = vtt_to_text(self.VTT)
        assert "Hello world" in text
        assert "Foo bar" in text
        assert "WEBVTT" not in text
        assert "-->" not in text

    def test_srt_strips_numbers_and_timestamps(self):
        text = srt_to_text(self.SRT)
        assert "Hello world" in text
        assert "Foo bar" in text
        assert "-->" not in text
