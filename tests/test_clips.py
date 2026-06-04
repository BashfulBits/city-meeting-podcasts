"""Tests for INFRA-7 (issue #148): clip service (citypods/clips.py).

Acceptance criteria:
- A served range spanning a concat boundary maps to the right multiple source cuts.
- Fake-ffmpeg (CapturingFfmpeg) asserts the cut+concat plan.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from citypods.clips import (
    ClipArtifact,
    _clip_timeline,
    _identity_clip_timeline,
    clip_object_key,
    extract_clip,
)
from citypods.models import Episode
from citypods.storage.local import LocalStorage
from citypods.timeline import Segment, SourceMedia, Timeline

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ep(uid="uid-g1", duration=3600.0) -> Episode:
    return Episode(
        guid="g1",
        uid=uid,
        title="Meeting",
        published=datetime(2026, 5, 20, tzinfo=UTC),
        video_url="https://src/vid.mp4",
        media_kind="direct",
        duration=int(duration),
    )


def _src(id="s0") -> SourceMedia:
    return SourceMedia(
        id=id,
        provider="g",
        ref="https://src/vid.mp4",
        media_kind="direct",
        duration=3600.0,
        watch_url=None,
    )


def _seg_src(ss, se, src_s, src_e, sid="s0") -> Segment:
    return Segment(
        served_start=ss,
        served_end=se,
        kind="source",
        source_id=sid,
        source_start=src_s,
        source_end=src_e,
    )


def _trimmed_tl() -> Timeline:
    """Source 3600s; silence cut 300–600 → served [0,300] ++ [300,3300]."""
    return Timeline(
        version="silence-v1",
        segments=(
            _seg_src(0, 300, 0, 300),
            _seg_src(300, 3300, 600, 3600),
        ),
    )


def _concat_tl() -> Timeline:
    """s0: 0–1800s, s1: 0–1800s, concatenated → served 0–3600."""
    return Timeline(
        version="concat-v1",
        segments=(
            _seg_src(0, 1800, 0, 1800, "s0"),
            _seg_src(1800, 3600, 0, 1800, "s1"),
        ),
    )


class CapturingFfmpeg:
    """Records extract_audio calls without running ffmpeg; writes stub bytes."""

    def __init__(self):
        self.calls: list[dict] = []

    def extract_audio(
        self,
        timeline,
        sources_by_id,
        dest,
        chapters=None,
        *,
        loudness_profile=None,
        asset_resolver=None,
    ):
        self.calls.append(
            {
                "timeline": timeline,
                "sources_by_id": dict(sources_by_id),
            }
        )
        dest.write_bytes(b"fake-clip")


def _store(tmp_path):
    return LocalStorage(root=tmp_path / "clips", url_prefix="https://cdn/clips")


# ---------------------------------------------------------------------------
# clip_object_key — content addressing
# ---------------------------------------------------------------------------


class TestClipObjectKey:
    def test_key_includes_uid_prefix(self):
        key = clip_object_key("uid-abc", 0.0, 60.0, "silence-v1", "audio")
        assert key.startswith("clips/uid-abc/")

    def test_key_has_m4a_extension_for_audio(self):
        key = clip_object_key("uid-abc", 0.0, 60.0, "v1", "audio")
        assert key.endswith(".m4a")

    def test_key_has_mp4_extension_for_video(self):
        key = clip_object_key("uid-abc", 0.0, 60.0, "v1", "video")
        assert key.endswith(".mp4")

    def test_same_params_same_key(self):
        a = clip_object_key("uid-abc", 10.0, 70.0, "v1", "audio")
        b = clip_object_key("uid-abc", 10.0, 70.0, "v1", "audio")
        assert a == b

    def test_different_range_different_key(self):
        a = clip_object_key("uid-abc", 10.0, 70.0, "v1", "audio")
        b = clip_object_key("uid-abc", 10.0, 71.0, "v1", "audio")
        assert a != b

    def test_different_uid_different_key(self):
        a = clip_object_key("uid-abc", 0.0, 60.0, "v1", "audio")
        b = clip_object_key("uid-xyz", 0.0, 60.0, "v1", "audio")
        assert a != b

    def test_different_timeline_version_different_key(self):
        a = clip_object_key("uid-abc", 0.0, 60.0, "v1", "audio")
        b = clip_object_key("uid-abc", 0.0, 60.0, "v2", "audio")
        assert a != b

    def test_subms_float_jitter_yields_same_key(self):
        # Review item #14: range bounds normalize to integer ms, so trivially-different
        # floats map to the same key and reuse the cached object.
        assert clip_object_key("u", 600.0, 660.0, "v1", "audio") == clip_object_key(
            "u", 600.0000001, 660.0, "v1", "audio"
        )
        assert clip_object_key("u", 600, 660, "v1", "audio") == clip_object_key(
            "u", 600.0, 660.0, "v1", "audio"
        )


# ---------------------------------------------------------------------------
# _clip_timeline — sub-timeline construction
# ---------------------------------------------------------------------------


class TestClipTimeline:
    def test_identity_clip_is_direct_source_range(self):
        clip_tl, cuts = _identity_clip_timeline("s0", 100.0, 200.0)
        assert len(clip_tl.segments) == 1
        s = clip_tl.segments[0]
        assert s.kind == "source"
        assert s.source_start == 100.0
        assert s.source_end == 200.0
        assert s.served_start == 0.0
        assert s.served_end == 100.0
        assert cuts == (("s0", 100.0, 200.0),)

    def test_clip_within_single_span_of_trimmed(self):
        # Clip [0, 200] — entirely in first span [0, 300]
        clip_tl, cuts = _clip_timeline(_trimmed_tl(), 0.0, 200.0)
        assert len(clip_tl.segments) == 1
        s = clip_tl.segments[0]
        assert s.source_start == 0.0
        assert s.source_end == 200.0
        assert cuts == (("s0", 0.0, 200.0),)

    def test_clip_in_second_span_of_trimmed(self):
        # Clip [400, 600] → served 400 maps to source 700; served 600 → source 900
        clip_tl, cuts = _clip_timeline(_trimmed_tl(), 400.0, 600.0)
        assert len(clip_tl.segments) == 1
        s = clip_tl.segments[0]
        assert s.source_start == 700.0  # source offset = 600 - 300 = 300; served 400 + 300 = 700
        assert s.source_end == 900.0
        assert cuts == (("s0", 700.0, 900.0),)

    def test_clip_spanning_silence_gap_skips_cut_region(self):
        # Clip [200, 500]: spans the silence gap (source 300–600).
        # served [200,300] → source [200,300]
        # served [300,500] → source [600,800]
        clip_tl, cuts = _clip_timeline(_trimmed_tl(), 200.0, 500.0)
        assert len(clip_tl.segments) == 2
        assert cuts == (("s0", 200.0, 300.0), ("s0", 600.0, 800.0))

    def test_clip_spanning_concat_boundary(self):
        # Concat: s0=0-1800, s1=0-1800. Clip [1600, 2000] spans the boundary at 1800.
        # served [1600,1800] → s0 [1600,1800]
        # served [1800,2000] → s1 [0,200]
        clip_tl, cuts = _clip_timeline(_concat_tl(), 1600.0, 2000.0)
        assert len(clip_tl.segments) == 2
        assert cuts[0] == ("s0", 1600.0, 1800.0)
        assert cuts[1] == ("s1", 0.0, 200.0)

    def test_clip_in_s0_only(self):
        clip_tl, cuts = _clip_timeline(_concat_tl(), 500.0, 1000.0)
        assert len(clip_tl.segments) == 1
        assert cuts == (("s0", 500.0, 1000.0),)

    def test_clip_in_s1_only(self):
        clip_tl, cuts = _clip_timeline(_concat_tl(), 2000.0, 2500.0)
        assert len(clip_tl.segments) == 1
        # s1 offset = source_start(0) - served_start(1800) = -1800
        # served 2000 → source 2000 - 1800 = 200; served 2500 → source 700
        assert cuts == (("s1", 200.0, 700.0),)

    def test_clip_timeline_resets_served_time_to_zero(self):
        # Even if the clip comes from the middle of the episode, the sub-timeline starts at 0
        clip_tl, _ = _clip_timeline(_concat_tl(), 1600.0, 2000.0)
        assert clip_tl.segments[0].served_start == 0.0
        total_served = sum(s.served_end - s.served_start for s in clip_tl.segments)
        assert abs(total_served - 400.0) < 1e-9  # 2000 - 1600


# ---------------------------------------------------------------------------
# extract_clip — filtergraph plan assertions (CapturingFfmpeg)
# ---------------------------------------------------------------------------


class TestExtractClipFiltergraph:
    def test_identity_clip_single_source_atrim(self, tmp_path):
        ep = _ep()
        ep.timeline = None
        ff = CapturingFfmpeg()
        extract_clip(
            ep,
            100.0,
            200.0,
            storage=_store(tmp_path),
            ffmpeg=ff,
            resolve_media_url=lambda e: e.video_url,
        )
        assert len(ff.calls) == 1
        call = ff.calls[0]
        # Single source segment
        assert len(call["timeline"].segments) == 1
        seg = call["timeline"].segments[0]
        assert seg.source_start == 100.0
        assert seg.source_end == 200.0

    def test_concat_boundary_clip_has_two_segments(self, tmp_path):
        ep = _ep()
        ep.sources = [_src("s0"), _src("s1")]
        ep.timeline = _concat_tl()

        ff = CapturingFfmpeg()
        extract_clip(
            ep,
            1600.0,
            2000.0,
            storage=_store(tmp_path),
            ffmpeg=ff,
            resolve_media_url=lambda e: e.video_url,
        )
        assert len(ff.calls) == 1
        tl = ff.calls[0]["timeline"]
        assert len(tl.segments) == 2
        assert tl.segments[0].source_id == "s0"
        assert tl.segments[1].source_id == "s1"

    def test_trimmed_episode_clip_maps_correctly(self, tmp_path):
        ep = _ep()
        ep.timeline = _trimmed_tl()

        ff = CapturingFfmpeg()
        extract_clip(
            ep,
            400.0,
            600.0,
            storage=_store(tmp_path),
            ffmpeg=ff,
            resolve_media_url=lambda e: e.video_url,
        )
        call = ff.calls[0]
        seg = call["timeline"].segments[0]
        # served 400 in second span: offset = 600-300=300, source = 400+300 = 700
        assert seg.source_start == 700.0
        assert seg.source_end == 900.0


# ---------------------------------------------------------------------------
# extract_clip — artifact fields
# ---------------------------------------------------------------------------


class TestExtractClipArtifact:
    def test_returns_clip_artifact(self, tmp_path):
        ep = _ep()
        ep.timeline = None
        artifact = extract_clip(
            ep,
            0.0,
            60.0,
            storage=_store(tmp_path),
            ffmpeg=CapturingFfmpeg(),
            resolve_media_url=lambda e: e.video_url,
        )
        assert isinstance(artifact, ClipArtifact)

    def test_video_kind_not_implemented(self, tmp_path):
        # Review item #13: the audio encoder strips video (-vn), so video clips are refused
        # until a real video render path exists, rather than shipping a silent container.
        ep = _ep()
        with pytest.raises(NotImplementedError):
            extract_clip(
                ep,
                0.0,
                60.0,
                kind="video",
                storage=_store(tmp_path),
                ffmpeg=CapturingFfmpeg(),
                resolve_media_url=lambda e: e.video_url,
            )

    def test_artifact_served_range(self, tmp_path):
        ep = _ep()
        artifact = extract_clip(
            ep,
            100.0,
            200.0,
            storage=_store(tmp_path),
            ffmpeg=CapturingFfmpeg(),
            resolve_media_url=lambda e: e.video_url,
        )
        assert artifact.served_start == 100.0
        assert artifact.served_end == 200.0

    def test_artifact_uid(self, tmp_path):
        ep = _ep(uid="uid-test")
        artifact = extract_clip(
            ep,
            0.0,
            60.0,
            storage=_store(tmp_path),
            ffmpeg=CapturingFfmpeg(),
            resolve_media_url=lambda e: e.video_url,
        )
        assert artifact.uid == "uid-test"

    def test_artifact_source_cuts_for_identity(self, tmp_path):
        ep = _ep()
        ep.timeline = None
        artifact = extract_clip(
            ep,
            100.0,
            200.0,
            storage=_store(tmp_path),
            ffmpeg=CapturingFfmpeg(),
            resolve_media_url=lambda e: e.video_url,
        )
        assert len(artifact.source_cuts) == 1
        sid, src_start, src_end = artifact.source_cuts[0]
        assert src_start == 100.0
        assert src_end == 200.0

    def test_artifact_source_cuts_for_concat_boundary(self, tmp_path):
        ep = _ep()
        ep.sources = [_src("s0"), _src("s1")]
        ep.timeline = _concat_tl()
        artifact = extract_clip(
            ep,
            1600.0,
            2000.0,
            storage=_store(tmp_path),
            ffmpeg=CapturingFfmpeg(),
            resolve_media_url=lambda e: e.video_url,
        )
        assert len(artifact.source_cuts) == 2
        assert artifact.source_cuts[0][0] == "s0"
        assert artifact.source_cuts[1][0] == "s1"

    def test_artifact_key_content_addressed(self, tmp_path):
        ep = _ep(uid="uid-abc")
        ep.timeline = None
        artifact = extract_clip(
            ep,
            0.0,
            60.0,
            storage=_store(tmp_path),
            ffmpeg=CapturingFfmpeg(),
            resolve_media_url=lambda e: e.video_url,
        )
        expected_key = clip_object_key("uid-abc", 0.0, 60.0, "identity", "audio")
        assert artifact.key == expected_key

    def test_artifact_url_publicly_accessible(self, tmp_path):
        ep = _ep()
        artifact = extract_clip(
            ep,
            0.0,
            60.0,
            storage=_store(tmp_path),
            ffmpeg=CapturingFfmpeg(),
            resolve_media_url=lambda e: e.video_url,
        )
        assert artifact.url.startswith("https://")

    def test_reuse_skips_encode_when_already_in_storage(self, tmp_path):
        ep = _ep(uid="uid-reuse")
        ep.timeline = None

        ff1 = CapturingFfmpeg()
        artifact1 = extract_clip(
            ep,
            0.0,
            60.0,
            storage=_store(tmp_path),
            ffmpeg=ff1,
            resolve_media_url=lambda e: e.video_url,
        )
        # Second call: object already in storage → should reuse without encoding
        ff2 = CapturingFfmpeg()
        artifact2 = extract_clip(
            ep,
            0.0,
            60.0,
            storage=_store(tmp_path),
            ffmpeg=ff2,
            resolve_media_url=lambda e: e.video_url,
        )
        assert artifact2.key == artifact1.key
        assert artifact2.url == artifact1.url
        assert len(ff2.calls) == 0  # no re-encode

    def test_raises_on_empty_range(self, tmp_path):
        ep = _ep()
        with pytest.raises(ValueError):
            extract_clip(
                ep,
                100.0,
                100.0,
                storage=_store(tmp_path),
                ffmpeg=CapturingFfmpeg(),
                resolve_media_url=lambda e: e.video_url,
            )

    def test_raises_on_inverted_range(self, tmp_path):
        ep = _ep()
        with pytest.raises(ValueError):
            extract_clip(
                ep,
                200.0,
                100.0,
                storage=_store(tmp_path),
                ffmpeg=CapturingFfmpeg(),
                resolve_media_url=lambda e: e.video_url,
            )
