"""Unit tests for citypods/asr.py — all faster_whisper/stable_whisper calls are mocked."""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock

from citypods.asr import (
    _fmt_ts,
    _to_vtt,
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


# ── _to_vtt ──────────────────────────────────────────────────────────────────


class TestToVtt:
    def _seg(self, start, end, text):
        s = MagicMock()
        s.start = start
        s.end = end
        s.text = text
        return s

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

        vtt = transcribe(audio, "base.en", "en", "int8", 5, None, 4)

        assert vtt.startswith(b"WEBVTT")
        assert b"Hello world" in vtt

    def test_missing_dep_raises_import_error(self, tmp_path):
        from citypods.asr import transcribe

        audio = tmp_path / "a.m4a"
        audio.write_bytes(b"fake")

        saved = sys.modules.pop("faster_whisper", None)
        try:
            try:
                transcribe(audio, "base.en", "en", "int8", 5, None, 4)
                raise AssertionError("Expected ImportError")
            except ImportError as exc:
                assert "citypods[asr]" in str(exc)
        finally:
            if saved is not None:
                sys.modules["faster_whisper"] = saved


# ── align() — mocked ─────────────────────────────────────────────────────────


class TestAlignMocked:
    def _setup_sw(self, vtt_str: str):
        fake_result = MagicMock()
        fake_result.to_vtt.return_value = vtt_str
        fake_model = MagicMock()
        fake_model.align.return_value = fake_result
        _inject_sw(fake_model)
        sys.modules["stable_whisper"].load_faster_whisper = MagicMock(return_value=fake_model)

    def test_returns_vtt_bytes(self, tmp_path):
        from citypods.asr import align

        audio = tmp_path / "a.m4a"
        audio.write_bytes(b"fake")

        self._setup_sw("WEBVTT\n\n00:00:00.000 --> 00:00:05.000\nHello world\n")
        vtt = align(audio, "Hello world", "base.en", "en", 4)

        assert vtt.startswith(b"WEBVTT")
        assert b"Hello world" in vtt

    def test_prepends_webvtt_if_missing(self, tmp_path):
        from citypods.asr import align

        audio = tmp_path / "a.m4a"
        audio.write_bytes(b"fake")

        self._setup_sw("00:00:00.000 --> 00:00:05.000\nHello\n")
        vtt = align(audio, "Hello", "base.en", "en", 4)

        assert vtt.startswith(b"WEBVTT")

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
        stable_model = MagicMock()
        stable_model.align.return_value = fake_result
        _inject_sw(stable_model)
        sys.modules["stable_whisper"].load_faster_whisper = MagicMock(return_value=stable_model)

        loaded = asr.load_model("base.en", "int8", 4)
        vtt = asr.align(audio, "Hello", loaded, "en", 4)

        assert vtt.startswith(b"WEBVTT")
        assert fast_model.align.call_count == 0
        sys.modules["stable_whisper"].load_faster_whisper.assert_called_once_with(
            "base.en",
            device="cpu",
            cpu_threads=4,
            compute_type="int8",
        )
        stable_model.align.assert_called_once_with(str(audio), "Hello", language="en")


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
