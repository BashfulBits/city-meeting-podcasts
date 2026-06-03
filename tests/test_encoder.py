"""Tests for the Timeline-aware encoder (INFRA-3, issue #144).

Acceptance criteria:
- Fake-ffmpeg asserts the planned filtergraph for identity/trim/concat/insert/loudness.
- Identity render produces the same ffmpeg args as pre-INFRA-3 (copy/re-encode).

Strategy: test ``build_filter_complex`` (pure function) directly for filtergraph
correctness, and test ``CommandFfmpeg`` via subprocess mocking for arg wiring.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from citypods.media import (
    CommandFfmpeg,
    _parse_lufs,
    build_filter_complex,
    encode_args,
    materialize_audio,
)
from citypods.models import City, Episode
from citypods.records import audio_spec_hash
from citypods.storage.local import LocalStorage
from citypods.timeline import Segment, SourceMedia, Timeline, identity_timeline


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seg_src(served_start, served_end, source_start, source_end, sid="s0") -> Segment:
    return Segment(
        served_start=served_start, served_end=served_end,
        kind="source", source_id=sid,
        source_start=source_start, source_end=source_end,
    )


def _seg_insert(served_start, served_end, asset_id="brand", version="1") -> Segment:
    return Segment(
        served_start=served_start, served_end=served_end,
        kind="insert", insert="intro",
        asset_id=asset_id, asset_version=version,
    )


def _src(id="s0") -> SourceMedia:
    return SourceMedia(id=id, provider="granicus",
                       ref="https://g.com/1.mp4", media_kind="direct",
                       duration=3600.0, watch_url="https://g.com/1")


def _city() -> City:
    return City(slug="t-tx", provider="civicplus",
                source={"feed_url": "x"}, podcast_title="T",
                podcast_author="City of T", podcast_email="", podcast_description="d",
                extract_audio=True)


def _ep(kind="hls", url="https://src/video.m3u8") -> Episode:
    return Episode(guid="g1", uid="uid-g1", title="Meeting",
                   published=datetime(2026, 5, 20, tzinfo=UTC),
                   video_url=url, media_kind=kind, duration=3600)


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
            _seg_insert(0, 60),                  # intro insert
            _seg_src(60, 3660, 0, 3600),         # main source
        )
        asset_idx = {("brand", "1"): 1}
        fc, out = build_filter_complex(segs, {"s0": 0}, asset_idx)
        assert "[1:a]acopy[a0]" in fc  # insert uses asset input (idx=1)
        assert "[0:a]atrim" in fc       # source uses main input (idx=0)
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


# ---------------------------------------------------------------------------
# CommandFfmpeg identity path — arg wiring (subprocess mock)
# ---------------------------------------------------------------------------

class TestCommandFfmpegIdentityPath:
    """The identity path must produce the same ffmpeg args as pre-INFRA-3."""

    def _run_identity(self, monkeypatch, tmp_path, source_url, bitrate_str="128000"):
        import citypods.media as media
        calls: list[tuple[list, dict]] = []

        def _fake_run(cmd, **kw):
            calls.append((cmd, kw))
            class _R:
                stdout = bitrate_str
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
        calls = self._run_identity(monkeypatch, tmp_path, "https://src/vid.mp4", bitrate_str="64000")
        _, (enc_cmd, _) = calls[0], calls[1]
        assert "-c:a" in enc_cmd
        idx = enc_cmd.index("-c:a")
        assert enc_cmd[idx + 1] == "copy"

    def test_reencode_when_bitrate_over_cap(self, monkeypatch, tmp_path):
        calls = self._run_identity(monkeypatch, tmp_path, "https://src/vid.mp4", bitrate_str="192000")
        _, (enc_cmd, _) = calls[0], calls[1]
        assert "-c:a" in enc_cmd
        idx = enc_cmd.index("-c:a")
        assert enc_cmd[idx + 1] == "aac"

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
                stdout = "64000"
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
    def _run_filter(self, monkeypatch, tmp_path, timeline, sources_by_id,
                    loudness=None, asset_resolver=None):
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
            timeline=tl, sources_by_id={"s0": "https://s/v.mp4"},
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
        cmd = self._run_filter(monkeypatch, tmp_path, tl, {"s0": "https://s/v.mp4"},
                               loudness="ebuR128:-16LUFS")
        fc_idx = cmd.index("-filter_complex")
        fc_str = cmd[fc_idx + 1]
        assert "loudnorm=I=-16" in fc_str

    def test_always_aac_encode_in_filter_path(self, monkeypatch, tmp_path):
        tl = self._trim_timeline()
        cmd = self._run_filter(monkeypatch, tmp_path, tl, {"s0": "https://s/v.mp4"})
        assert "-c:a" in cmd
        idx = cmd.index("-c:a")
        assert cmd[idx + 1] == "aac"


# ---------------------------------------------------------------------------
# materialize_audio integration — timeline plumbing
# ---------------------------------------------------------------------------

class TestMaterializeWithTimeline:
    """Verify that materialize_audio passes ep.timeline through to extract_audio."""

    class _CapturingFfmpeg:
        def __init__(self):
            self.timelines = []
            self.sources = []

        def extract_audio(self, timeline, sources_by_id, dest, chapters=None, *,
                          loudness_profile=None, asset_resolver=None):
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
            city, [ep],
            storage=LocalStorage(root=tmp_path / "a", url_prefix="https://cdn/"),
            ffmpeg=ff, max_kbps=96,
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
            city, [ep],
            storage=LocalStorage(root=tmp_path / "a", url_prefix="https://cdn/"),
            ffmpeg=ff, max_kbps=96,
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
            city, [ep],
            storage=LocalStorage(root=tmp_path / "a", url_prefix="https://cdn/"),
            ffmpeg=ff, max_kbps=96,
            resolve_media_url=lambda e: e.video_url,
        )
        assert "https://src/vid.m3u8" in ff.sources[0].values()
