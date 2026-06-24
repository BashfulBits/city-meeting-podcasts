"""Tests for the Timeline-aware encoder (INFRA-3, issue #144).

Acceptance criteria:
- Fake-ffmpeg asserts the planned filtergraph for identity/trim/concat/insert/loudness.
- Identity render produces the same ffmpeg args as pre-INFRA-3 (copy/re-encode).

Strategy: test ``build_filter_complex`` (pure function) directly for filtergraph
correctness, and test ``CommandFfmpeg`` via subprocess mocking for arg wiring.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime

import pytest

from citypods.media import (
    PODCAST_SPEECH_PROFILE,
    CommandFfmpeg,
    LoudnessMeasurements,
    UnusableAudioError,
    _build_streaming_single_source_filter,
    _linear_loudnorm_filter,
    _parse_ebur128_summary,
    _parse_lufs,
    _peak_limited_linear_filter,
    _served_duration,
    build_filter_complex,
    materialize_audio,
)
from citypods.models import City, Episode
from citypods.storage.local import LocalStorage
from citypods.timeline import Segment, SourceMedia, Timeline, identity_timeline

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seg_src(served_start, served_end, source_start, source_end, sid="s0") -> Segment:
    return Segment(
        served_start=served_start,
        served_end=served_end,
        kind="source",
        source_id=sid,
        source_start=source_start,
        source_end=source_end,
    )


def _seg_insert(served_start, served_end, asset_id="brand", version="1") -> Segment:
    return Segment(
        served_start=served_start,
        served_end=served_end,
        kind="insert",
        insert="intro",
        asset_id=asset_id,
        asset_version=version,
    )


def _src(id="s0") -> SourceMedia:
    return SourceMedia(
        id=id,
        provider="granicus",
        ref="https://g.com/1.mp4",
        media_kind="direct",
        duration=3600.0,
        watch_url="https://g.com/1",
    )


def _city() -> City:
    return City(
        slug="t-tx",
        provider="civicplus",
        source={"feed_url": "x"},
        podcast_title="T",
        podcast_author="City of T",
        podcast_email="",
        podcast_description="d",
        extract_audio=True,
    )


def _ep(kind="hls", url="https://src/video.m3u8") -> Episode:
    return Episode(
        guid="g1",
        uid="uid-g1",
        title="Meeting",
        published=datetime(2026, 5, 20, tzinfo=UTC),
        video_url=url,
        media_kind=kind,
        duration=3600,
    )


# ---------------------------------------------------------------------------
# build_filter_complex — identity-equivalent (single full-span source)
# ---------------------------------------------------------------------------


class TestFiltergraphIdentityEquiv:
    """When the timeline is identity, CommandFfmpeg takes the identity path and never
    calls build_filter_complex.  These tests verify that build_filter_complex, when
    called with a single full-span segment, still produces correct (if redundant) output."""

    def test_single_segment_produces_atrim_and_label(self):
        segs = (_seg_src(0, 3600, 0, 3600),)
        fc, out = build_filter_complex(segs, {"s0": 0}, {})
        assert "[0:a]atrim=start=0.0:end=3600" in fc
        assert "asetpts=PTS-STARTPTS" in fc
        assert out == "[a0]"  # single segment, no concat

    def test_output_label_for_single_segment(self):
        segs = (_seg_src(0, 1000, 0, 1000),)
        _, out = build_filter_complex(segs, {"s0": 0}, {})
        assert out == "[a0]"


# ---------------------------------------------------------------------------
# build_filter_complex — trim (single source, multiple segments)
# ---------------------------------------------------------------------------


class TestFiltergraphTrim:
    def test_two_kept_spans_from_one_source(self):
        # Source 3600s; silence cut 300-600 → served [0,300] + [300,3300]
        segs = (
            _seg_src(0, 300, 0, 300),
            _seg_src(300, 3300, 600, 3600),
        )
        fc, out = build_filter_complex(segs, {"s0": 0}, {})
        assert "[0:a]atrim=start=0.0:end=300" in fc
        assert "[0:a]atrim=start=600" in fc  # second span, start=600
        assert "concat=n=2:v=0:a=1[outa]" in fc
        assert out == "[outa]"

    def test_three_kept_spans(self):
        segs = (
            _seg_src(0, 100, 0, 100),
            _seg_src(100, 200, 200, 300),
            _seg_src(200, 300, 400, 500),
        )
        fc, out = build_filter_complex(segs, {"s0": 0}, {})
        assert "concat=n=3:v=0:a=1[outa]" in fc
        assert out == "[outa]"

    def test_output_labels_are_distinct(self):
        segs = (
            _seg_src(0, 100, 0, 100),
            _seg_src(100, 200, 200, 300),
        )
        fc, _ = build_filter_complex(segs, {"s0": 0}, {})
        assert "[a0]" in fc and "[a1]" in fc

    def test_source_indices_map_correctly(self):
        # Input 0 is s0, input 1 is s1 — both trimmed
        segs = (
            _seg_src(0, 1800, 0, 1800, "s0"),
            _seg_src(1800, 3600, 0, 1800, "s1"),
        )
        fc, out = build_filter_complex(segs, {"s0": 0, "s1": 1}, {})
        assert "[0:a]atrim=start=0.0:end=1800" in fc
        assert "[1:a]atrim=start=0.0:end=1800" in fc
        assert "concat=n=2" in fc


class TestStreamingSingleSourceFilter:
    def test_monotonic_keep_spans_use_sample_accurate_one_pass_aselect(self):
        segs = (
            _seg_src(0, 300, 0, 300),
            _seg_src(300, 3300, 600, 3600),
        )

        result = _build_streaming_single_source_filter(segs, {"s0": 0})

        assert result is not None
        graph, output = result
        assert graph.startswith("[0:a]aresample=48000")
        assert "asettb=1/48000" in graph
        assert "asendcmd=" in graph
        assert "asetnsamples@timeline_cut_frames n 1" in graph
        assert "asetnsamples@timeline_cut_frames=n=1024" in graph
        assert "gte(pts\\,0)*lt(pts\\,14400000)" in graph
        assert "gte(pts\\,28800000)*lt(pts\\,172800000)" in graph
        assert "asetpts=N/SR/TB,asetnsamples=n=1024:p=0[program]" in graph
        assert "atrim" not in graph
        assert "concat" not in graph
        assert output == "[program]"

    def test_fractional_boundaries_are_rounded_to_nearest_sample(self):
        segs = (
            _seg_src(0, 0.5, 0.000354, 0.513729),
            _seg_src(0.5, 1.1, 1.117146, 1.731521),
        )

        result = _build_streaming_single_source_filter(segs, {"s0": 0})

        assert result is not None
        graph, _ = result
        assert "gte(pts\\,17)*lt(pts\\,24659)" in graph
        assert "gte(pts\\,53623)*lt(pts\\,83113)" in graph

    def test_real_ffmpeg_emits_exact_selected_sample_count(self, tmp_path):
        if shutil.which("ffmpeg") is None:
            pytest.skip("ffmpeg is required for the audio filter integration check")
        segs = (
            _seg_src(0, 0.5, 0.000354, 0.513729),
            _seg_src(0.5, 1.1, 1.117146, 1.731521),
            _seg_src(1.1, 1.5, 2.000271, 2.401188),
        )
        result = _build_streaming_single_source_filter(segs, {"s0": 0})
        assert result is not None
        graph, output = result
        raw = tmp_path / "selected.s16le"

        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=3",
                "-filter_complex",
                graph,
                "-map",
                output,
                "-f",
                "s16le",
                "-acodec",
                "pcm_s16le",
                str(raw),
            ],
            check=True,
        )

        expected_samples = sum(
            round(segment.source_end * 48_000) - round(segment.source_start * 48_000)
            for segment in segs
        )
        assert raw.stat().st_size == expected_samples * 2

    def test_reordered_spans_fall_back_to_generic_graph(self):
        segs = (
            _seg_src(0, 100, 600, 700),
            _seg_src(100, 200, 0, 100),
        )

        assert _build_streaming_single_source_filter(segs, {"s0": 0}) is None

    def test_multi_source_spans_fall_back_to_generic_graph(self):
        segs = (
            _seg_src(0, 100, 0, 100, "s0"),
            _seg_src(100, 200, 0, 100, "s1"),
        )

        assert _build_streaming_single_source_filter(segs, {"s0": 0, "s1": 1}) is None


# ---------------------------------------------------------------------------
# build_filter_complex — multi-source concat
# ---------------------------------------------------------------------------


class TestFiltergraphConcat:
    def test_two_source_full_concat(self):
        segs = (
            _seg_src(0, 1800, 0, 1800, "s0"),
            _seg_src(1800, 3600, 0, 1800, "s1"),
        )
        fc, out = build_filter_complex(segs, {"s0": 0, "s1": 1}, {})
        assert "[0:a]" in fc and "[1:a]" in fc
        assert "concat=n=2:v=0:a=1[outa]" in fc
        assert out == "[outa]"

    def test_concat_boundary_labels(self):
        segs = (
            _seg_src(0, 1800, 0, 1800, "s0"),
            _seg_src(1800, 3600, 0, 1800, "s1"),
        )
        fc, _ = build_filter_complex(segs, {"s0": 0, "s1": 1}, {})
        # Both segments must be represented
        assert fc.count("atrim") == 2


# ---------------------------------------------------------------------------
# build_filter_complex — insert (intro/outro)
# ---------------------------------------------------------------------------


class TestFiltergraphInsert:
    def test_insert_uses_acopy(self):
        segs = (
            _seg_insert(0, 60),  # intro insert
            _seg_src(60, 3660, 0, 3600),  # main source
        )
        asset_idx = {("brand", "1"): 1}
        fc, out = build_filter_complex(segs, {"s0": 0}, asset_idx)
        assert "[1:a]acopy[a0]" in fc  # insert uses asset input (idx=1)
        assert "[0:a]atrim" in fc  # source uses main input (idx=0)
        assert "concat=n=2" in fc
        assert out == "[outa]"

    def test_outro_insert_at_end(self):
        segs = (
            _seg_src(0, 3600, 0, 3600),
            _seg_insert(3600, 3660),
        )
        asset_idx = {("brand", "1"): 1}
        fc, out = build_filter_complex(segs, {"s0": 0}, asset_idx)
        assert "concat=n=2" in fc
        assert out == "[outa]"


# ---------------------------------------------------------------------------
# build_filter_complex — loudness
# ---------------------------------------------------------------------------


class TestFiltergraphLoudness:
    def test_loudnorm_appended_single_segment(self):
        segs = (_seg_src(0, 3600, 0, 3600),)
        fc, out = build_filter_complex(segs, {"s0": 0}, {}, loudness_profile="ebuR128:-16LUFS")
        assert "loudnorm=I=-16:TP=-1.5:LRA=11[outa]" in fc
        assert out == "[outa]"

    def test_loudnorm_appended_after_concat(self):
        segs = (
            _seg_src(0, 300, 0, 300),
            _seg_src(300, 3300, 600, 3600),
        )
        fc, out = build_filter_complex(segs, {"s0": 0}, {}, loudness_profile="ebuR128:-23LUFS")
        assert "loudnorm=I=-23" in fc
        assert "[preln]loudnorm" in fc  # intermediate label before loudnorm
        assert out == "[outa]"

    def test_parse_lufs_various_formats(self):
        assert _parse_lufs("ebuR128:-16LUFS") == "-16"
        assert _parse_lufs("ebuR128:-23LUFS") == "-23"
        assert _parse_lufs("unknown") == "-16"  # safe default

    def test_parse_ebur128_summary(self):
        measured = _parse_ebur128_summary(
            """
            Summary:
              Integrated loudness:
                I:         -20.2 LUFS
                Threshold: -30.2 LUFS
              Loudness range:
                LRA:         7.4 LU
                Threshold: -40.0 LUFS
              True peak:
                Peak:       -8.0 dBFS
            """
        )
        assert measured == LoudnessMeasurements(-20.2, -30.2, 7.4, -8.0)

    def test_linear_loudnorm_never_requests_lra_reduction(self):
        filt = _linear_loudnorm_filter(
            "ebuR128:-16LUFS",
            LoudnessMeasurements(-20.0, -30.0, 14.5, -8.0),
        )
        assert "LRA=14.5" in filt
        assert "linear=true" in filt

    def test_peak_limited_linear_filter_uses_constant_gain_and_streaming_limiter(self):
        measured = LoudnessMeasurements(
            integrated=-25.0,
            threshold=-35.0,
            loudness_range=8.0,
            true_peak=-1.0,
        )

        result = _peak_limited_linear_filter("ebuR128:-16LUFS", measured)

        assert result.startswith("volume=9dB:precision=double")
        assert "aresample=192000" in result
        assert "alimiter=limit=0.749894209:level=false:latency=true" in result
        assert result.endswith("aresample=48000")
        assert "loudnorm" not in result


# ---------------------------------------------------------------------------
# CommandFfmpeg identity path — arg wiring (subprocess mock)
# ---------------------------------------------------------------------------


class TestCommandFfmpegIdentityPath:
    """The identity path must produce the same ffmpeg args as pre-INFRA-3."""

    def _run_identity(
        self, monkeypatch, tmp_path, source_url, bitrate_str="128000", codec_name="aac"
    ):
        import citypods.media as media

        calls: list[tuple[list, dict]] = []

        def _fake_run(cmd, **kw):
            calls.append((cmd, kw))

            class _R:
                stdout = (
                    f'{{"streams":[{{"codec_name":"{codec_name}","bit_rate":"{bitrate_str}"}}]}}'
                )

            return _R()

        monkeypatch.setattr(media.subprocess, "run", _fake_run)
        media.CommandFfmpeg(max_kbps=96).extract_audio(
            timeline=None,
            sources_by_id={"s0": source_url},
            dest=tmp_path / "out.m4a",
        )
        return calls

    def test_no_filter_complex_in_identity_path(self, monkeypatch, tmp_path):
        calls = self._run_identity(monkeypatch, tmp_path, "https://src/vid.mp4")
        _, (enc_cmd, _) = calls[0], calls[1]
        assert "-filter_complex" not in enc_cmd

    def test_copy_when_bitrate_under_cap(self, monkeypatch, tmp_path):
        calls = self._run_identity(
            monkeypatch, tmp_path, "https://src/vid.mp4", bitrate_str="64000"
        )
        _, (enc_cmd, _) = calls[0], calls[1]
        assert "-c:a" in enc_cmd
        idx = enc_cmd.index("-c:a")
        assert enc_cmd[idx + 1] == "copy"

    def test_reencode_when_bitrate_over_cap(self, monkeypatch, tmp_path):
        calls = self._run_identity(
            monkeypatch, tmp_path, "https://src/vid.mp4", bitrate_str="192000"
        )
        _, (enc_cmd, _) = calls[0], calls[1]
        assert "-c:a" in enc_cmd
        idx = enc_cmd.index("-c:a")
        assert enc_cmd[idx + 1] == "aac"

    def test_reencode_when_codec_not_m4a_copy_safe(self, monkeypatch, tmp_path):
        calls = self._run_identity(
            monkeypatch,
            tmp_path,
            "https://src/vid.mp4",
            bitrate_str="64000",
            codec_name="mp2",
        )
        _, (enc_cmd, _) = calls[0], calls[1]
        assert "-c:a" in enc_cmd
        idx = enc_cmd.index("-c:a")
        assert enc_cmd[idx + 1] == "aac"

    def test_reencode_pins_ffmpeg_threads(self, monkeypatch, tmp_path):
        import citypods.media as media

        calls: list[tuple[list, dict]] = []

        def _fake_run(cmd, **kw):
            calls.append((cmd, kw))

            class _R:
                stdout = "192000"

            return _R()

        monkeypatch.setattr(media.subprocess, "run", _fake_run)
        media.CommandFfmpeg(max_kbps=96, threads=2).extract_audio(
            timeline=None,
            sources_by_id={"s0": "https://src/vid.mp4"},
            dest=tmp_path / "out.m4a",
        )
        _, (enc_cmd, _) = calls[0], calls[1]
        assert "-threads" in enc_cmd
        idx = enc_cmd.index("-threads")
        assert enc_cmd[idx + 1] == "2"

    def test_copy_does_not_add_thread_pin(self, monkeypatch, tmp_path):
        calls = self._run_identity(
            monkeypatch, tmp_path, "https://src/vid.mp4", bitrate_str="64000"
        )
        _, (enc_cmd, _) = calls[0], calls[1]
        assert "-threads" not in enc_cmd

    def test_source_url_in_identity_inputs(self, monkeypatch, tmp_path):
        calls = self._run_identity(monkeypatch, tmp_path, "https://src/vid.mp4")
        _, (enc_cmd, _) = calls[0], calls[1]
        assert "https://src/vid.mp4" in enc_cmd

    def test_rw_timeout_present(self, monkeypatch, tmp_path):
        calls = self._run_identity(monkeypatch, tmp_path, "https://src/vid.mp4")
        _, (enc_cmd, _) = calls[0], calls[1]
        assert "-rw_timeout" in enc_cmd

    def test_protocol_whitelist_present(self, monkeypatch, tmp_path):
        calls = self._run_identity(monkeypatch, tmp_path, "https://src/vid.mp4")
        _, (enc_cmd, _) = calls[0], calls[1]
        assert "-protocol_whitelist" in enc_cmd

    def test_identity_timeline_takes_identity_path(self, monkeypatch, tmp_path):
        """An explicit identity Timeline must NOT trigger the filter path."""
        import citypods.media as media

        calls: list = []

        def _fake_run(cmd, **kw):
            calls.append(cmd)

            class _R:
                stdout = '{"streams":[{"codec_name":"aac","bit_rate":"64000"}]}'

            return _R()

        monkeypatch.setattr(media.subprocess, "run", _fake_run)
        src = _src()
        tl = identity_timeline(src, 3600.0)
        media.CommandFfmpeg(max_kbps=96).extract_audio(
            timeline=tl,
            sources_by_id={"s0": "https://src/vid.mp4"},
            dest=tmp_path / "out.m4a",
        )
        enc_cmd = calls[1]  # second call is ffmpeg encode
        assert "-filter_complex" not in enc_cmd


# ---------------------------------------------------------------------------
# CommandFfmpeg filter path — arg wiring (subprocess mock)
# ---------------------------------------------------------------------------


class TestCommandFfmpegFilterPath:
    def _run_filter(
        self, monkeypatch, tmp_path, timeline, sources_by_id, loudness=None, asset_resolver=None
    ):
        import citypods.media as media

        calls: list = []

        def _fake_run(cmd, **kw):
            calls.append(cmd)

            class _R:
                stdout = ""

            return _R()

        monkeypatch.setattr(media.subprocess, "run", _fake_run)
        media.CommandFfmpeg(max_kbps=96).extract_audio(
            timeline=timeline,
            sources_by_id=sources_by_id,
            dest=tmp_path / "out.m4a",
            loudness_profile=loudness,
            asset_resolver=asset_resolver,
        )
        return calls[0]  # filter path has NO probe; first call is ffmpeg encode

    def _trim_timeline(self) -> Timeline:
        return Timeline(
            version="silence-v1",
            segments=(
                _seg_src(0, 300, 0, 300),
                _seg_src(300, 3300, 600, 3600),
            ),
        )

    def test_filter_complex_present_for_trim(self, monkeypatch, tmp_path):
        tl = self._trim_timeline()
        cmd = self._run_filter(monkeypatch, tmp_path, tl, {"s0": "https://src/vid.mp4"})
        assert "-filter_complex" in cmd

    def test_no_probe_in_filter_path(self, monkeypatch, tmp_path):
        """Filter path always re-encodes; no bitrate probe needed."""
        tl = self._trim_timeline()
        import citypods.media as media

        calls: list = []

        def _fake_run(cmd, **kw):
            calls.append(cmd)

            class _R:
                stdout = ""

            return _R()

        monkeypatch.setattr(media.subprocess, "run", _fake_run)
        media.CommandFfmpeg(max_kbps=96).extract_audio(
            timeline=tl,
            sources_by_id={"s0": "https://s/v.mp4"},
            dest=tmp_path / "out.m4a",
        )
        # Only ONE subprocess call (no ffprobe in filter path)
        assert len(calls) == 1

    def test_atrim_in_filter_complex(self, monkeypatch, tmp_path):
        tl = self._trim_timeline()
        cmd = self._run_filter(monkeypatch, tmp_path, tl, {"s0": "https://s/v.mp4"})
        fc_idx = cmd.index("-filter_complex")
        fc_str = cmd[fc_idx + 1]
        assert "atrim" in fc_str and "concat" in fc_str

    def test_map_output_label(self, monkeypatch, tmp_path):
        tl = self._trim_timeline()
        cmd = self._run_filter(monkeypatch, tmp_path, tl, {"s0": "https://s/v.mp4"})
        assert "-map" in cmd
        map_idx = cmd.index("-map")
        assert "[" in cmd[map_idx + 1]  # output is a labeled stream

    def test_loudnorm_in_filter_complex(self, monkeypatch, tmp_path):
        tl = self._trim_timeline()
        cmd = self._run_filter(
            monkeypatch, tmp_path, tl, {"s0": "https://s/v.mp4"}, loudness="ebuR128:-16LUFS"
        )
        fc_idx = cmd.index("-filter_complex")
        fc_str = cmd[fc_idx + 1]
        assert "loudnorm=I=-16" in fc_str

    def test_always_aac_encode_in_filter_path(self, monkeypatch, tmp_path):
        tl = self._trim_timeline()
        cmd = self._run_filter(monkeypatch, tmp_path, tl, {"s0": "https://s/v.mp4"})
        assert "-c:a" in cmd

    def test_filter_path_pins_ffmpeg_threads(self, monkeypatch, tmp_path):
        import citypods.media as media

        calls: list = []

        def _fake_run(cmd, **kw):
            calls.append(cmd)

            class _R:
                stdout = ""

            return _R()

        monkeypatch.setattr(media.subprocess, "run", _fake_run)
        media.CommandFfmpeg(max_kbps=96, threads=2).extract_audio(
            timeline=self._trim_timeline(),
            sources_by_id={"s0": "https://s/v.mp4"},
            dest=tmp_path / "out.m4a",
        )
        cmd = calls[0]
        assert "-threads" in cmd
        idx = cmd.index("-threads")
        assert cmd[idx + 1] == "2"
        idx = cmd.index("-c:a")
        assert cmd[idx + 1] == "aac"

    def test_filter_path_pins_filter_threads(self, monkeypatch, tmp_path):
        import citypods.media as media

        calls: list = []

        def _fake_run(cmd, **kw):
            calls.append(cmd)

            class _R:
                stdout = ""

            return _R()

        monkeypatch.setattr(media.subprocess, "run", _fake_run)
        media.CommandFfmpeg(max_kbps=96, threads=1).extract_audio(
            timeline=self._trim_timeline(),
            sources_by_id={"s0": "https://s/v.mp4"},
            dest=tmp_path / "out.m4a",
        )
        cmd = calls[0]
        assert "-filter_threads" in cmd
        idx = cmd.index("-filter_threads")
        assert cmd[idx + 1] == "1"
        assert "-filter_complex_threads" in cmd
        idx = cmd.index("-filter_complex_threads")
        assert cmd[idx + 1] == "1"
        assert cmd.index("-filter_threads") < cmd.index("-filter_complex")
        assert cmd.index("-filter_complex_threads") < cmd.index("-filter_complex")

    def test_podcast_speech_profile_uses_bounded_two_pass_flow(self, monkeypatch, tmp_path):
        import citypods.media as media

        calls: list[tuple[list[str], str]] = []
        summary = b"""
        Summary:
          Integrated loudness:
            I:         -20.0 LUFS
            Threshold: -30.0 LUFS
          Loudness range:
            LRA:         8.0 LU
            Threshold: -40.0 LUFS
          True peak:
            Peak:       -8.0 dBFS
        """

        def _fake_guard(cmd, *, phase, **kwargs):
            calls.append((cmd, phase))
            return b"", summary if phase == "speech-measure" else b""

        monkeypatch.setattr(media, "_run_ffmpeg_guarded", _fake_guard)
        media.CommandFfmpeg(max_kbps=96, threads=1).extract_audio(
            timeline=self._trim_timeline(),
            sources_by_id={"s0": "https://s/v.mp4"},
            dest=tmp_path / "out.m4a",
            loudness_profile="ebuR128:-16LUFS",
            processing_profile=PODCAST_SPEECH_PROFILE,
        )

        assert [phase for _, phase in calls] == ["speech-measure", "loudness-render"]
        measure_graph = calls[0][0][calls[0][0].index("-filter_complex") + 1]
        assert "highpass=f=80" in measure_graph
        assert "dynaudnorm=f=500:g=21" in measure_graph
        assert "acompressor=threshold=0.125:ratio=2.5" in measure_graph
        assert "ebur128=framelog=quiet:peak=true" in measure_graph
        assert "loudnorm" not in measure_graph
        assert "aselect=" in measure_graph
        assert "atrim" not in measure_graph
        assert "concat" not in measure_graph
        final_graph = calls[1][0][calls[1][0].index("-filter_complex") + 1]
        assert "measured_I=-20" in final_graph
        assert "linear=true" in final_graph
        assert "speech-leveled.flac" in " ".join(calls[1][0])

    def test_podcast_speech_profile_limits_peak_when_linear_loudnorm_is_impossible(
        self, monkeypatch, tmp_path
    ):
        import citypods.media as media

        calls: list[tuple[list[str], str]] = []
        summary = b"""
        Summary:
          Integrated loudness:
            I:         -25.0 LUFS
            Threshold: -35.0 LUFS
          Loudness range:
            LRA:         8.0 LU
            Threshold: -45.0 LUFS
          True peak:
            Peak:       -1.0 dBFS
        """

        def _fake_guard(cmd, *, phase, **kwargs):
            calls.append((cmd, phase))
            return b"", summary if phase == "speech-measure" else b""

        monkeypatch.setattr(media, "_run_ffmpeg_guarded", _fake_guard)
        media.CommandFfmpeg(max_kbps=96, threads=1).extract_audio(
            timeline=self._trim_timeline(),
            sources_by_id={"s0": "https://s/v.mp4"},
            dest=tmp_path / "out.m4a",
            loudness_profile="ebuR128:-16LUFS",
            processing_profile=PODCAST_SPEECH_PROFILE,
        )

        assert [phase for _, phase in calls] == ["speech-measure", "loudness-limit-render"]
        final_graph = calls[1][0][calls[1][0].index("-filter_complex") + 1]
        assert "volume=9dB:precision=double" in final_graph
        assert "aresample=192000" in final_graph
        assert "alimiter=" in final_graph
        assert "level=false:latency=true" in final_graph
        assert "loudnorm" not in final_graph

    def test_podcast_speech_finalization_runs_on_dedicated_worker(self, monkeypatch, tmp_path):
        import threading

        import citypods.media as media

        phases: list[tuple[str, str]] = []
        summary = b"""
        Summary:
          Integrated loudness:
            I:         -20.0 LUFS
            Threshold: -30.0 LUFS
          Loudness range:
            LRA:         8.0 LU
            Threshold: -40.0 LUFS
          True peak:
            Peak:       -8.0 dBFS
        """

        def _fake_guard(cmd, *, phase, **kwargs):
            phases.append((phase, threading.current_thread().name))
            return b"", summary if phase == "speech-measure" else b""

        monkeypatch.setattr(media, "_run_ffmpeg_guarded", _fake_guard)
        runner = media.CommandFfmpeg(max_kbps=96, threads=1, finalize_workers=1)
        try:
            runner.extract_audio(
                timeline=self._trim_timeline(),
                sources_by_id={"s0": "https://s/v.mp4"},
                dest=tmp_path / "out.m4a",
                loudness_profile="ebuR128:-16LUFS",
                processing_profile=PODCAST_SPEECH_PROFILE,
            )
        finally:
            runner.close()

        assert phases[0][0] == "speech-measure"
        assert not phases[0][1].startswith("audio-finalize")
        assert phases[1][0] == "loudness-render"
        assert phases[1][1].startswith("audio-finalize")

    def test_podcast_speech_uses_phase_specific_native_gate(self, monkeypatch, tmp_path):
        import citypods.media as media

        gate_events: list[tuple[str, str]] = []
        summary = b"""
        Summary:
          Integrated loudness:
            I:         -20.0 LUFS
            Threshold: -30.0 LUFS
          Loudness range:
            LRA:         8.0 LU
            Threshold: -40.0 LUFS
          True peak:
            Peak:       -8.0 dBFS
        """

        class _Gate:
            def acquire(self, *, kind, label, stop=None):
                gate_events.append(("acquire", kind))
                return True

            def release(self, *, kind):
                gate_events.append(("release", kind))

        monkeypatch.setattr(
            media,
            "_run_ffmpeg_guarded",
            lambda cmd, *, phase, **kwargs: (
                b"",
                summary if phase == "speech-measure" else b"",
            ),
        )
        runner = media.CommandFfmpeg(
            max_kbps=96,
            threads=1,
            phase_gate=_Gate(),
            finalize_workers=1,
        )
        try:
            runner.extract_audio(
                timeline=self._trim_timeline(),
                sources_by_id={"s0": "https://s/v.mp4"},
                dest=tmp_path / "out.m4a",
                loudness_profile="ebuR128:-16LUFS",
                processing_profile=PODCAST_SPEECH_PROFILE,
            )
        finally:
            runner.close()

        assert gate_events == [
            ("acquire", "audio"),
            ("release", "audio"),
            ("acquire", "audio-finalize"),
            ("release", "audio-finalize"),
        ]

    def test_native_gate_stop_raises_stop_requested_not_timeout(self, monkeypatch, tmp_path):
        """A gate slot denied because the run's wall-clock budget fired must surface as
        StopRequested — deferred work to retry next run — not a fabricated 2700s ffmpeg
        timeout that gets recorded as a real encode failure."""
        import citypods.media as media
        from citypods.http import StopRequested

        class _StoppedGate:
            def acquire(self, *, kind, label, stop=None):
                return False

            def release(self, *, kind):
                raise AssertionError("release should not be called when acquire failed")

        runner = media.CommandFfmpeg(max_kbps=96, threads=1, phase_gate=_StoppedGate())
        try:
            with pytest.raises(StopRequested):
                runner.extract_audio(
                    timeline=self._trim_timeline(),
                    sources_by_id={"s0": "https://s/v.mp4"},
                    dest=tmp_path / "out.m4a",
                )
        finally:
            runner.close()

    def test_podcast_speech_profile_rejects_subsecond_timeline(self, tmp_path):
        import pytest

        tl = Timeline(
            version="silence-v1",
            segments=(_seg_src(0, 0.005, 0, 0.005),),
        )
        with pytest.raises(UnusableAudioError, match="only 0.005s"):
            CommandFfmpeg().extract_audio(
                timeline=tl,
                sources_by_id={"s0": "https://s/v.mp4"},
                dest=tmp_path / "out.m4a",
                loudness_profile="ebuR128:-16LUFS",
                processing_profile=PODCAST_SPEECH_PROFILE,
            )


# ---------------------------------------------------------------------------
# materialize_audio integration — timeline plumbing
# ---------------------------------------------------------------------------


class TestMaterializeWithTimeline:
    """Verify that materialize_audio passes ep.timeline through to extract_audio."""

    class _CapturingFfmpeg:
        def __init__(self):
            self.timelines = []
            self.sources = []

        def extract_audio(
            self,
            timeline,
            sources_by_id,
            dest,
            chapters=None,
            *,
            loudness_profile=None,
            processing_profile=None,
            asset_resolver=None,
        ):
            self.timelines.append(timeline)
            self.sources.append(dict(sources_by_id))
            dest.write_bytes(b"fake")

    def test_identity_episode_passes_none_timeline(self, tmp_path):
        city = _city()
        ep = _ep()
        ep.uid = "uid-t1"
        ep.timeline = None

        ff = self._CapturingFfmpeg()
        materialize_audio(
            city,
            [ep],
            storage=LocalStorage(root=tmp_path / "a", url_prefix="https://cdn/"),
            ffmpeg=ff,
            max_kbps=96,
            resolve_media_url=lambda e: e.video_url,
        )
        assert ff.timelines[0] is None

    def test_nonidentity_episode_passes_timeline(self, tmp_path):
        city = _city()
        ep = _ep()
        ep.uid = "uid-t2"
        tl = Timeline(
            version="silence-v1",
            segments=(
                _seg_src(0, 300, 0, 300),
                _seg_src(300, 3300, 600, 3600),
            ),
        )
        ep.timeline = tl

        ff = self._CapturingFfmpeg()
        materialize_audio(
            city,
            [ep],
            storage=LocalStorage(root=tmp_path / "a", url_prefix="https://cdn/"),
            ffmpeg=ff,
            max_kbps=96,
            resolve_media_url=lambda e: e.video_url,
        )
        assert ff.timelines[0] is tl

    def test_sources_by_id_has_correct_url(self, tmp_path):
        city = _city()
        ep = _ep(url="https://src/vid.m3u8")
        ep.uid = "uid-t3"
        ep.timeline = None

        ff = self._CapturingFfmpeg()
        materialize_audio(
            city,
            [ep],
            storage=LocalStorage(root=tmp_path / "a", url_prefix="https://cdn/"),
            ffmpeg=ff,
            max_kbps=96,
            resolve_media_url=lambda e: e.video_url,
        )
        assert "https://src/vid.m3u8" in ff.sources[0].values()


# ---------------------------------------------------------------------------
# build_filter_complex — format normalization before concat (review item #8)
# ---------------------------------------------------------------------------


class TestFiltergraphFormatNorm:
    """Branches are normalized to a common mono rate before concat only when they can
    differ (multiple distinct sources, or an insert asset) — never for same-source trims."""

    def test_multi_source_concat_normalizes_each_branch(self):
        segs = (_seg_src(0, 1800, 0, 1800, "s0"), _seg_src(1800, 3600, 0, 1800, "s1"))
        fc, _ = build_filter_complex(segs, {"s0": 0, "s1": 1}, {})
        assert fc.count("aresample=48000") == 2
        assert "aformat=channel_layouts=mono" in fc
        assert "concat=n=2:v=0:a=1[outa]" in fc

    def test_insert_branch_is_normalized(self):
        segs = (_seg_insert(0, 60), _seg_src(60, 3660, 0, 3600))
        fc, _ = build_filter_complex(segs, {"s0": 0}, {("brand", "1"): 1})
        assert "aresample=48000" in fc
        assert "concat=n=2" in fc

    def test_same_source_trim_is_not_resampled(self):
        # #111's path: one source, multiple kept spans → already uniform, no extra resample.
        segs = (_seg_src(0, 300, 0, 300), _seg_src(300, 3300, 600, 3600))
        fc, _ = build_filter_complex(segs, {"s0": 0}, {})
        assert "aresample" not in fc
        assert "concat=n=2:v=0:a=1[outa]" in fc


# ---------------------------------------------------------------------------
# _served_duration — EDL-derived served duration (review item #7)
# ---------------------------------------------------------------------------


class TestServedDuration:
    def _ep(self, duration):
        return Episode(
            guid="g",
            title="t",
            published=datetime(2026, 5, 1, tzinfo=UTC),
            video_url="https://g/1.mp4",
            duration=duration,
        )

    def test_identity_uses_source_duration(self):
        assert _served_duration(self._ep(3600)) == 3600.0

    def test_trim_uses_segment_sum_not_source_duration(self):
        ep = self._ep(3600)
        # cut 300–600 → served total 3300, while ep.duration is still source 3600
        ep.timeline = Timeline(
            version="silence-v1",
            segments=(_seg_src(0, 300, 0, 300), _seg_src(300, 3300, 600, 3600)),
        )
        assert _served_duration(ep) == 3300.0
        assert ep.duration == 3600  # source duration left untouched

    def test_identity_timeline_object_falls_back_to_source(self):
        ep = self._ep(1234)
        ep.timeline = identity_timeline(_src(), 1234.0)  # digest "" → identity
        assert _served_duration(ep) == 1234.0
