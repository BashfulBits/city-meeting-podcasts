"""Golden tests for the Timeline/EDL core (INFRA-1, issue #142).

Covers: forward map, inverse map, remap (with cut-span drops, concat boundaries,
insert spans), identity timeline passthrough, and timeline_digest semantics.
"""

from __future__ import annotations

from citypods.timeline import (
    Segment,
    SourceMedia,
    Timeline,
    edl_duration,
    identity_timeline,
    remap,
    served_to_source,
    source_to_served,
    timeline_digest,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _src(id="s0", duration=3600.0) -> SourceMedia:
    return SourceMedia(
        id=id,
        provider="granicus",
        ref="https://example.granicus.com/player/clip/1",
        media_kind="direct",
        duration=duration,
        watch_url="https://example.granicus.com/player/clip/1",
    )


def _trimmed_timeline() -> Timeline:
    """Source 3600s; silence cut at 300–600s → served is [0,300] + [300,3300]."""
    return Timeline(
        version="silence-v1",
        segments=(
            Segment(
                served_start=0,
                served_end=300,
                kind="source",
                source_id="s0",
                source_start=0,
                source_end=300,
            ),
            Segment(
                served_start=300,
                served_end=3300,
                kind="source",
                source_id="s0",
                source_start=600,
                source_end=3600,
            ),
        ),
    )


def _concat_timeline() -> Timeline:
    """Two sources: s0=0–1800s, s1=0–1800s, concatenated end-to-end."""
    return Timeline(
        version="concat-v1",
        segments=(
            Segment(
                served_start=0,
                served_end=1800,
                kind="source",
                source_id="s0",
                source_start=0,
                source_end=1800,
            ),
            Segment(
                served_start=1800,
                served_end=3600,
                kind="source",
                source_id="s1",
                source_start=0,
                source_end=1800,
            ),
        ),
    )


def _intro_timeline() -> Timeline:
    """60s intro insert, then 3600s source."""
    return Timeline(
        version="intro-v1",
        segments=(
            Segment(
                served_start=0,
                served_end=60,
                kind="insert",
                insert="intro",
                asset_id="brand-v1",
                asset_version="1",
            ),
            Segment(
                served_start=60,
                served_end=3660,
                kind="source",
                source_id="s0",
                source_start=0,
                source_end=3600,
            ),
        ),
    )


# ---------------------------------------------------------------------------
# edl_duration — the single EDL/cue-clock primitive (review/20)
# ---------------------------------------------------------------------------


class TestEdlDuration:
    def test_trimmed_total_is_sum_of_served_spans(self):
        # 300 + 3000 = 3300 served seconds after the silence cut.
        assert edl_duration(_trimmed_timeline()) == 3300.0

    def test_concat_total_spans_both_sources(self):
        assert edl_duration(_concat_timeline()) == 3600.0

    def test_insert_span_counts_toward_served_total(self):
        # 60s intro + 3600s source.
        assert edl_duration(_intro_timeline()) == 3660.0

    def test_identity_full_span_equals_source(self):
        assert edl_duration(identity_timeline(_src(), 3600.0)) == 3600.0

    def test_none_and_empty_return_none(self):
        assert edl_duration(None) is None
        assert edl_duration(Timeline(version="x", segments=())) is None

    def test_is_single_derivation_for_all_callers(self):
        """media._served_duration, stages._edited_timeline_served_duration, and
        audit._timeline_duration must all derive the EDL clock from this one primitive so the
        three duration facts cannot drift apart through divergent local math (review/20)."""
        from datetime import UTC, datetime

        from citypods.audit import _timeline_duration
        from citypods.media import _served_duration
        from citypods.models import Episode
        from citypods.stages import _edited_timeline_served_duration

        tl = _trimmed_timeline()
        src = _src()
        ep = Episode(
            guid="g",
            title="t",
            published=datetime(2026, 5, 20, tzinfo=UTC),
            video_url="https://src/video.m3u8",
            timeline=tl,
            sources=[src],
        )

        expected = edl_duration(tl)
        assert expected == 3300.0
        assert _served_duration(ep) == expected
        assert _edited_timeline_served_duration(ep) == expected
        assert _timeline_duration(ep) == expected


# ---------------------------------------------------------------------------
# identity_timeline
# ---------------------------------------------------------------------------


class TestIdentityTimeline:
    def test_single_segment_full_span(self):
        src = _src()
        tl = identity_timeline(src, 3600.0)
        assert len(tl.segments) == 1
        s = tl.segments[0]
        assert s.kind == "source"
        assert s.source_id == "s0"
        assert s.served_start == s.source_start == 0.0
        assert s.served_end == s.source_end == 3600.0

    def test_digest_is_empty_string(self):
        tl = identity_timeline(_src(), 3600.0)
        assert timeline_digest(tl) == ""

    def test_forward_map_is_passthrough(self):
        tl = identity_timeline(_src(), 3600.0)
        assert served_to_source(tl, 0.0) == ("s0", 0.0)
        assert served_to_source(tl, 412.5) == ("s0", 412.5)
        assert served_to_source(tl, 3600.0) == ("s0", 3600.0)

    def test_inverse_map_is_passthrough(self):
        tl = identity_timeline(_src(), 3600.0)
        assert source_to_served(tl, "s0", 0.0) == 0.0
        assert source_to_served(tl, "s0", 412.5) == 412.5
        assert source_to_served(tl, "s0", 3600.0) == 3600.0

    def test_remap_items_unchanged(self):
        tl = identity_timeline(_src(), 3600.0)
        items = [
            {"start": 0, "title": "Call to order"},
            {"start": 120, "end": 300, "title": "Agenda"},
        ]
        out = remap(tl, items)
        assert out[0]["start"] == 0
        assert out[1]["start"] == 120
        assert out[1]["end"] == 300


# ---------------------------------------------------------------------------
# timeline_digest
# ---------------------------------------------------------------------------


class TestTimelineDigest:
    def test_identity_returns_empty_string(self):
        assert timeline_digest(identity_timeline(_src(), 1800.0)) == ""

    def test_non_identity_returns_12char_hex(self):
        tl = _trimmed_timeline()
        d = timeline_digest(tl)
        assert len(d) == 12
        assert all(c in "0123456789abcdef" for c in d)

    def test_different_timelines_have_different_digests(self):
        assert timeline_digest(_trimmed_timeline()) != timeline_digest(_concat_timeline())

    def test_same_timeline_same_digest(self):
        assert timeline_digest(_trimmed_timeline()) == timeline_digest(_trimmed_timeline())

    def test_structurally_identity_returns_empty_even_without_constructor(self):
        # Any single full-pass source segment is identity, regardless of version string.
        tl = Timeline(
            version="something-else",
            segments=(
                Segment(
                    served_start=0.0,
                    served_end=1000.0,
                    kind="source",
                    source_id="s0",
                    source_start=0.0,
                    source_end=1000.0,
                ),
            ),
        )
        assert timeline_digest(tl) == ""

    def test_offset_trim_is_not_identity(self):
        # Start trim: served starts at 0, source starts at 120 → not identity.
        tl = Timeline(
            version="v1",
            segments=(
                Segment(
                    served_start=0.0,
                    served_end=1000.0,
                    kind="source",
                    source_id="s0",
                    source_start=120.0,
                    source_end=1120.0,
                ),
            ),
        )
        assert timeline_digest(tl) != ""

    def test_two_segments_is_not_identity(self):
        assert timeline_digest(_trimmed_timeline()) != ""
        assert timeline_digest(_concat_timeline()) != ""


# ---------------------------------------------------------------------------
# served_to_source — trimmed timeline
# ---------------------------------------------------------------------------


class TestServedToSource:
    def test_first_span_maps_correctly(self):
        tl = _trimmed_timeline()
        assert served_to_source(tl, 0.0) == ("s0", 0.0)
        assert served_to_source(tl, 150.0) == ("s0", 150.0)
        # 299.9 is clearly inside span[0]; 300.0 is the boundary and belongs to span[1].
        assert served_to_source(tl, 299.9) == ("s0", 299.9)

    def test_second_span_maps_with_offset(self):
        tl = _trimmed_timeline()
        # Boundary 300.0 belongs to span[1] (half-open intervals): maps to source 600.
        assert served_to_source(tl, 300.0) == ("s0", 600.0)
        result = served_to_source(tl, 300.1)
        assert result is not None
        sid, t = result
        assert sid == "s0"
        assert abs(t - 600.1) < 1e-9

    def test_end_of_served_is_valid(self):
        tl = _trimmed_timeline()
        assert served_to_source(tl, 3300.0) == ("s0", 3600.0)

    def test_beyond_served_duration_returns_none(self):
        tl = _trimmed_timeline()
        assert served_to_source(tl, 3301.0) is None

    def test_insert_returns_none(self):
        tl = _intro_timeline()
        assert served_to_source(tl, 0.0) is None  # inside intro insert
        assert served_to_source(tl, 30.0) is None  # still in insert
        assert served_to_source(tl, 59.9) is None

    def test_source_after_insert_maps_correctly(self):
        tl = _intro_timeline()
        # served 60 → source s0 @ 0
        assert served_to_source(tl, 60.0) == ("s0", 0.0)
        assert served_to_source(tl, 160.0) == ("s0", 100.0)


# ---------------------------------------------------------------------------
# source_to_served — trimmed timeline
# ---------------------------------------------------------------------------


class TestSourceToServed:
    def test_kept_span_maps_correctly(self):
        tl = _trimmed_timeline()
        assert source_to_served(tl, "s0", 0.0) == 0.0
        assert source_to_served(tl, "s0", 150.0) == 150.0
        # 299.9 is clearly inside span[0]; 300.0 is the right boundary, excluded by
        # half-open convention (it falls in the "gap" between span[0] and span[1]).
        assert source_to_served(tl, "s0", 299.9) == 299.9

    def test_cut_span_returns_none(self):
        tl = _trimmed_timeline()
        # source 300–600 was cut
        assert source_to_served(tl, "s0", 300.1) is None
        assert source_to_served(tl, "s0", 450.0) is None
        assert source_to_served(tl, "s0", 599.9) is None

    def test_second_kept_span_maps_with_negative_offset(self):
        tl = _trimmed_timeline()
        # source 600 → served 300, source 601 → served 301
        assert source_to_served(tl, "s0", 600.0) == 300.0
        assert source_to_served(tl, "s0", 900.0) == 600.0
        assert source_to_served(tl, "s0", 3600.0) == 3300.0

    def test_wrong_source_id_returns_none(self):
        tl = _trimmed_timeline()
        assert source_to_served(tl, "s1", 100.0) is None


# ---------------------------------------------------------------------------
# served_to_source / source_to_served — concat timeline
# ---------------------------------------------------------------------------


class TestConcatTimeline:
    def test_s0_maps_correctly(self):
        tl = _concat_timeline()
        assert served_to_source(tl, 0.0) == ("s0", 0.0)
        assert served_to_source(tl, 900.0) == ("s0", 900.0)
        # 1800.0 is the boundary: belongs to span[1] (s1), not span[0] (s0).
        assert served_to_source(tl, 1799.9) == ("s0", 1799.9)

    def test_s1_maps_correctly(self):
        tl = _concat_timeline()
        # served 1800 → s0 (boundary belongs to first segment)
        # served 1800.1 → s1 @ 0.1
        sid, t = served_to_source(tl, 1800.1)
        assert sid == "s1"
        assert abs(t - 0.1) < 1e-9

    def test_s1_end(self):
        tl = _concat_timeline()
        assert served_to_source(tl, 3600.0) == ("s1", 1800.0)

    def test_inverse_s0(self):
        tl = _concat_timeline()
        assert source_to_served(tl, "s0", 0.0) == 0.0
        assert source_to_served(tl, "s0", 900.0) == 900.0

    def test_inverse_s1_has_offset(self):
        tl = _concat_timeline()
        assert source_to_served(tl, "s1", 0.0) == 1800.0
        assert source_to_served(tl, "s1", 900.0) == 2700.0
        assert source_to_served(tl, "s1", 1800.0) == 3600.0

    def test_s0_time_does_not_map_via_s1(self):
        tl = _concat_timeline()
        # s1 time 900 maps to served 2700, not 900
        assert source_to_served(tl, "s1", 900.0) != 900.0


# ---------------------------------------------------------------------------
# remap
# ---------------------------------------------------------------------------


class TestRemap:
    def test_identity_remap_unchanged(self):
        tl = identity_timeline(_src(), 3600.0)
        items = [{"start": 0, "title": "A"}, {"start": 120, "end": 300, "title": "B"}]
        out = remap(tl, items)
        assert out[0]["start"] == 0
        assert out[1]["start"] == 120
        assert out[1]["end"] == 300

    def test_cut_span_items_are_dropped(self):
        tl = _trimmed_timeline()
        # source 300–600 was cut; chapters with start in that range should be dropped
        items = [
            {"start": 0, "title": "A"},  # kept → served 0
            {"start": 200, "title": "B"},  # kept → served 200
            {"start": 350, "title": "C"},  # CUT (source 350 is in 300–600 gap)
            {"start": 600, "title": "D"},  # kept → served 300
        ]
        out = remap(tl, items)
        assert len(out) == 3
        titles = [i["title"] for i in out]
        assert "C" not in titles
        assert out[0]["start"] == 0.0
        assert out[1]["start"] == 200.0
        assert out[2]["start"] == 300.0

    def test_remap_end_for_kept_item(self):
        tl = _trimmed_timeline()
        items = [{"start": 0, "end": 200, "title": "A"}]
        out = remap(tl, items)
        assert out[0]["end"] == 200.0

    def test_remap_end_none_when_end_is_cut(self):
        tl = _trimmed_timeline()
        # start=200 is kept, end=400 is in cut span
        items = [{"start": 200, "end": 400, "title": "A"}]
        out = remap(tl, items)
        assert len(out) == 1
        assert out[0]["start"] == 200.0
        assert out[0]["end"] is None  # end was cut

    def test_explicit_source_id(self):
        tl = _concat_timeline()
        items = [{"start": 0, "title": "Intro"}, {"start": 900, "title": "Middle"}]
        # Remap from s1's perspective
        out = remap(tl, items, source_id="s1")
        assert out[0]["start"] == 1800.0  # s1@0 → served 1800
        assert out[1]["start"] == 2700.0  # s1@900 → served 2700

    def test_extra_keys_are_preserved(self):
        tl = identity_timeline(_src(), 3600.0)
        items = [{"start": 60, "end": 120, "title": "Test", "speaker": "Mayor"}]
        out = remap(tl, items)
        assert out[0]["speaker"] == "Mayor"

    def test_empty_items_returns_empty(self):
        tl = identity_timeline(_src(), 3600.0)
        assert remap(tl, []) == []

    def test_concat_boundary_crossing(self):
        """A chapter starting just before the s0/s1 boundary maps to the right served time."""
        tl = _concat_timeline()
        items = [{"start": 1799, "title": "Near boundary"}]
        out = remap(tl, items)
        assert len(out) == 1
        assert out[0]["start"] == 1799.0  # s0@1799 → served 1799


# ---------------------------------------------------------------------------
# Concat-seam boundary convention (review item #1) — pins the documented
# asymmetry between the two maps at a source→source handoff so it can't drift.
# ---------------------------------------------------------------------------


class TestConcatSeamConvention:
    def test_served_boundary_belongs_to_the_later_segment(self):
        # served 1800 is the s0→s1 seam; served_to_source assigns it to the segment that
        # STARTS there (s1@0), because non-last segments are half-open [start, end).
        tl = _concat_timeline()
        assert served_to_source(tl, 1800.0) == ("s1", 0.0)

    def test_source_boundary_is_closed_for_its_own_segment(self):
        # Going the other way, 1800 is the closed right endpoint of s0's (only) segment,
        # so it maps to served 1800 — the same instant served_to_source hands to s1.
        tl = _concat_timeline()
        assert source_to_served(tl, "s0", 1800.0) == 1800.0
        assert source_to_served(tl, "s1", 0.0) == 1800.0

    def test_maps_are_inverse_everywhere_except_the_seam(self):
        # The documented sub-frame asymmetry: round-tripping s0@1800 lands on s1, not s0 …
        tl = _concat_timeline()
        assert served_to_source(tl, source_to_served(tl, "s0", 1800.0)) == ("s1", 0.0)
        # … but one frame earlier the maps are exact inverses (true away from the seam).
        assert served_to_source(tl, source_to_served(tl, "s0", 1799.0)) == ("s0", 1799.0)


# ---------------------------------------------------------------------------
# remap(clamp_to=...) — review item #3: a cut end is clamped instead of None.
# ---------------------------------------------------------------------------


class TestRemapClampTo:
    def test_cut_end_is_clamped_when_requested(self):
        # _trimmed_timeline cuts source 300-600; an item ending at 450 (inside the cut)
        # would otherwise remap to end=None. clamp_to pins it to the nearest segment
        # boundary (served=300, the edge of the cut) rather than the overall served
        # duration -- clamping to the global duration would wrongly overlap any later
        # chapter/cue that follows the cut.
        tl = _trimmed_timeline()
        items = [{"start": 100, "end": 450, "title": "runs into the cut"}]
        out = remap(tl, items, clamp_to=3300.0)
        assert out[0]["start"] == 100.0
        assert out[0]["end"] == 300.0

    def test_without_clamp_cut_end_is_still_none(self):
        tl = _trimmed_timeline()
        out = remap(tl, [{"start": 100, "end": 450}])  # no clamp_to
        assert out[0]["end"] is None

    def test_clamp_leaves_an_uncut_end_untouched(self):
        tl = _trimmed_timeline()
        out = remap(tl, [{"start": 100, "end": 200}], clamp_to=3300.0)
        assert out[0]["end"] == 200.0  # 200 is in the kept span → unchanged
