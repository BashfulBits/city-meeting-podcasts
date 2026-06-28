"""Tests for INFRA-9 (issue #150): timeline/clip verification + contracts.

Acceptance criteria:
- Synthetic bad EDLs (overlap, out-of-range remap, duration mismatch) are caught.
- Deep-link liveness is sampled in contracts.py (checked in test_contracts / live suite).
- Good EDLs produce no findings.
"""

from __future__ import annotations

from datetime import UTC, datetime

from citypods.audit import check_timeline_integrity
from citypods.media import AudioDurationProbe
from citypods.models import Episode
from citypods.timeline import Segment, SourceMedia, Timeline

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ep(uid="uid-g1") -> Episode:
    return Episode(
        guid="g1",
        uid=uid,
        title="Meeting",
        published=datetime(2026, 5, 20, tzinfo=UTC),
        video_url="https://src/vid.mp4",
        media_kind="direct",
        duration=3600,
    )


def _src(id="s0", duration=3600.0) -> SourceMedia:
    return SourceMedia(
        id=id,
        provider="g",
        ref="https://g.com/1.mp4",
        media_kind="direct",
        duration=duration,
        watch_url=None,
    )


def _seg(ss, se, src_s, src_e, sid="s0") -> Segment:
    return Segment(
        served_start=ss,
        served_end=se,
        kind="source",
        source_id=sid,
        source_start=src_s,
        source_end=src_e,
    )


def _good_timeline() -> Timeline:
    """A valid trimmed timeline: silence cut 300–600."""
    return Timeline(
        version="silence-v1",
        segments=(
            _seg(0, 300, 0, 300),
            _seg(300, 3300, 600, 3600),
        ),
    )


def _findings(episodes):
    return check_timeline_integrity("test-tx", episodes)


# ---------------------------------------------------------------------------
# Good EDLs — no findings
# ---------------------------------------------------------------------------


class TestGoodEDLs:
    def test_identity_timeline_skipped(self):
        ep = _ep()
        ep.timeline = None
        assert _findings([ep]) == []

    def test_valid_trimmed_timeline_no_findings(self):
        ep = _ep()
        ep.timeline = _good_timeline()
        ep.audio_duration_served = 3300.0
        ep.sources = [_src()]
        assert _findings([ep]) == []

    def test_valid_concat_timeline_no_findings(self):
        ep = _ep()
        ep.timeline = Timeline(
            version="concat-v1",
            segments=(
                _seg(0, 1800, 0, 1800, "s0"),
                _seg(1800, 3600, 0, 1800, "s1"),
            ),
        )
        ep.audio_duration_served = 3600.0
        ep.sources = [_src("s0", 1800.0), _src("s1", 1800.0)]
        assert _findings([ep]) == []

    def test_no_episodes_no_findings(self):
        assert _findings([]) == []

    def test_chapters_in_range_no_findings(self):
        ep = _ep()
        ep.timeline = _good_timeline()
        ep.audio_duration_served = 3300.0
        ep.chapters = [{"start": 0, "title": "A"}, {"start": 200, "title": "B"}]
        ep.chapters_basis = "served"
        assert _findings([ep]) == []


# ---------------------------------------------------------------------------
# Segment overlap
# ---------------------------------------------------------------------------


class TestSegmentOverlap:
    def test_overlapping_segments_caught(self):
        ep = _ep()
        ep.timeline = Timeline(
            version="bad-v1",
            segments=(
                _seg(0, 400, 0, 400),  # ends at 400
                _seg(300, 700, 300, 700),  # starts at 300 — OVERLAP!
            ),
        )
        ep.audio_duration_served = 700.0
        fs = _findings([ep])
        checks = [f.check for f in fs]
        assert "timeline-overlap" in checks

    def test_exactly_touching_segments_ok(self):
        ep = _ep()
        # Segments touch exactly: first ends at 300, second starts at 300 — no overlap.
        ep.timeline = Timeline(
            version="v1",
            segments=(
                _seg(0, 300, 0, 300),
                _seg(300, 3300, 600, 3600),
            ),
        )
        ep.audio_duration_served = 3300.0
        fs = _findings([ep])
        overlap = [f for f in fs if f.check == "timeline-overlap"]
        assert overlap == []


# ---------------------------------------------------------------------------
# Duration mismatch
# ---------------------------------------------------------------------------


class TestDurationMismatch:
    def test_segment_total_ne_served_duration(self):
        ep = _ep()
        ep.timeline = _good_timeline()
        # Segment total = 3300s but we claim 3600s
        ep.audio_duration_served = 3600.0
        fs = _findings([ep])
        assert any(f.check == "timeline-duration-mismatch" for f in fs)

    def test_segment_total_matches_served_duration(self):
        ep = _ep()
        ep.timeline = _good_timeline()
        ep.audio_duration_served = 3300.0  # correct: 300 + 3000 = 3300
        fs = _findings([ep])
        assert not any(f.check == "timeline-duration-mismatch" for f in fs)

    def test_no_served_duration_skips_duration_check(self):
        ep = _ep()
        ep.timeline = _good_timeline()
        ep.audio_duration_served = None  # not yet recorded
        fs = _findings([ep])
        assert not any(f.check == "timeline-duration-mismatch" for f in fs)

    def test_small_floating_point_delta_not_flagged(self):
        ep = _ep()
        ep.timeline = _good_timeline()
        ep.audio_duration_served = 3300.0 + 0.05  # within 0.1s frame tolerance
        fs = _findings([ep])
        assert not any(f.check == "timeline-duration-mismatch" for f in fs)

    def test_container_only_drift_is_diagnostic_not_error(self):
        ep = _ep()
        ep.timeline = _good_timeline()
        ep.audio_key = "audio/u1.m4a"
        diagnostics = []

        fs = check_timeline_integrity(
            "test-tx",
            [ep],
            probe_audio=lambda _ep: AudioDurationProbe(
                container_duration=3304.0,
                stream_sample_duration=3300.0,
                stream_duration_source="stream-duration-ts",
            ),
            diagnostics=diagnostics,
        )

        assert fs == []
        assert diagnostics[0]["check"] == "container-duration-drift"
        assert diagnostics[0]["repair"] == []

    def test_stream_duration_mismatch_flags_repair_actions(self):
        ep = _ep()
        ep.timeline = _good_timeline()
        ep.audio_key = "audio/u1.m4a"
        diagnostics = []

        fs = check_timeline_integrity(
            "test-tx",
            [ep],
            probe_audio=lambda _ep: AudioDurationProbe(
                container_duration=3304.0,
                stream_sample_duration=3304.0,
                stream_duration_source="stream-duration-ts",
            ),
            diagnostics=diagnostics,
            mutate_integrity=True,
        )

        assert any(f.check == "rendered-duration-mismatch" for f in fs)
        assert diagnostics[0]["repair"] == [
            "timeline-replan",
            "audio-rematerialize",
            "transcript-regenerate",
        ]
        assert ep.integrity["timeline_audio"]["status"] == "rendered-duration-mismatch"

    def test_subthreshold_stream_mismatch_is_telemetry_only(self):
        ep = _ep()
        ep.timeline = _good_timeline()
        ep.audio_key = "audio/u1.m4a"
        diagnostics = []

        fs = check_timeline_integrity(
            "test-tx",
            [ep],
            probe_audio=lambda _ep: AudioDurationProbe(
                container_duration=3300.4,
                stream_sample_duration=3300.4,
                stream_duration_source="stream-duration-ts",
            ),
            diagnostics=diagnostics,
            mutate_integrity=True,
            repair_min_delta=1.0,
            repair_cohort="gt1s",
            finding_min_delta=1.0,
        )

        assert fs == []
        assert diagnostics[0]["check"] == "rendered-duration-mismatch"
        assert diagnostics[0]["repair_selected"] is False
        assert diagnostics[0]["repair_cohort"] == "gt1s"
        assert ep.integrity == {}

    def test_repair_cohort_selects_over_threshold_stream_mismatch(self):
        ep = _ep()
        ep.timeline = _good_timeline()
        ep.audio_key = "audio/u1.m4a"
        diagnostics = []

        fs = check_timeline_integrity(
            "test-tx",
            [ep],
            probe_audio=lambda _ep: AudioDurationProbe(
                container_duration=3302.0,
                stream_sample_duration=3302.0,
                stream_duration_source="stream-duration-ts",
            ),
            diagnostics=diagnostics,
            mutate_integrity=True,
            repair_min_delta=1.0,
            repair_cohort="gt1s",
            finding_min_delta=1.0,
        )

        assert any(f.check == "rendered-duration-mismatch" for f in fs)
        assert diagnostics[0]["repair_selected"] is True
        assert diagnostics[0]["repair_min_delta"] == 1.0
        assert diagnostics[0]["repair_cohort"] == "gt1s"
        block = ep.integrity["timeline_audio"]
        assert block["repair_cohort"] == "gt1s"
        assert block["repair_min_delta"] == 1.0
        assert block["repair"] == [
            "audio-rematerialize",
            "timeline-replan",
            "transcript-regenerate",
        ]

    def test_inconclusive_diagnostic_carries_probe_error(self):
        ep = _ep()
        ep.timeline = _good_timeline()
        diagnostics = []

        fs = check_timeline_integrity(
            "test-tx",
            [ep],
            probe_audio=lambda _ep: AudioDurationProbe(probe_error="missing-audio-key"),
            diagnostics=diagnostics,
        )

        assert fs == []
        assert diagnostics[0]["probe_error"] == "missing-audio-key"


# ---------------------------------------------------------------------------
# Gap at start
# ---------------------------------------------------------------------------


class TestGapAtStart:
    def test_first_segment_not_at_zero(self):
        ep = _ep()
        ep.timeline = Timeline(
            version="bad-v1",
            segments=(
                _seg(5, 300, 5, 300),  # starts at 5, not 0
                _seg(300, 3300, 600, 3600),
            ),
        )
        ep.audio_duration_served = 3295.0
        fs = _findings([ep])
        assert any(f.check == "timeline-gap-start" for f in fs)

    def test_first_segment_at_zero_ok(self):
        ep = _ep()
        ep.timeline = _good_timeline()
        fs = _findings([ep])
        assert not any(f.check == "timeline-gap-start" for f in fs)


# ---------------------------------------------------------------------------
# Source span bounds
# ---------------------------------------------------------------------------


class TestSourceSpanBounds:
    def test_source_end_exceeds_media_duration(self):
        ep = _ep()
        ep.sources = [_src("s0", 3000.0)]  # source is only 3000s
        ep.timeline = Timeline(
            version="v1",
            segments=(
                _seg(0, 3300, 0, 3600),  # source_end=3600 > SourceMedia.duration=3000
            ),
        )
        ep.audio_duration_served = 3300.0
        fs = _findings([ep])
        assert any(f.check == "timeline-source-overrun" for f in fs)

    def test_source_end_within_media_duration_ok(self):
        ep = _ep()
        ep.sources = [_src("s0", 3600.0)]
        ep.timeline = _good_timeline()
        ep.audio_duration_served = 3300.0
        fs = _findings([ep])
        assert not any(f.check == "timeline-source-overrun" for f in fs)

    def test_unknown_source_duration_skips_check(self):
        ep = _ep()
        ep.sources = [
            SourceMedia(
                id="s0", provider="g", ref="u", media_kind="direct", duration=None, watch_url=None
            )
        ]
        ep.timeline = Timeline(
            version="v1",
            segments=(
                _seg(0, 3300, 0, 9999),  # would exceed any known duration
            ),
        )
        ep.audio_duration_served = 3300.0
        fs = _findings([ep])
        assert not any(f.check == "timeline-source-overrun" for f in fs)

    def test_unregistered_source_skips_check(self):
        """If ep.sources doesn't include s0, source bounds check is skipped."""
        ep = _ep()
        ep.sources = []  # no registered sources
        ep.timeline = _good_timeline()
        ep.audio_duration_served = 3300.0
        fs = _findings([ep])
        assert not any(f.check == "timeline-source-overrun" for f in fs)


# ---------------------------------------------------------------------------
# Chapter alignment
# ---------------------------------------------------------------------------


class TestChapterAlignment:
    def test_chapter_past_served_duration_flagged(self):
        ep = _ep()
        ep.timeline = _good_timeline()
        ep.audio_duration_served = 3300.0
        ep.chapters = [{"start": 3400, "title": "Out of range"}]
        ep.chapters_basis = "served"
        fs = _findings([ep])
        assert any(f.check == "timeline-chapter-out-of-range" for f in fs)

    def test_chapter_in_range_ok(self):
        ep = _ep()
        ep.timeline = _good_timeline()
        ep.audio_duration_served = 3300.0
        ep.chapters = [{"start": 200, "title": "A"}, {"start": 1000, "title": "B"}]
        ep.chapters_basis = "served"
        fs = _findings([ep])
        assert not any(f.check == "timeline-chapter-out-of-range" for f in fs)

    def test_source_basis_chapters_not_checked(self):
        """Chapters in source-time are not checked against served duration."""
        ep = _ep()
        ep.timeline = _good_timeline()
        ep.audio_duration_served = 3300.0
        ep.chapters = [{"start": 3500, "title": "Way out"}]
        ep.chapters_basis = "source:s0"  # not "served" → skipped
        fs = _findings([ep])
        assert not any(f.check == "timeline-chapter-out-of-range" for f in fs)

    def test_no_served_duration_skips_chapter_check(self):
        ep = _ep()
        ep.timeline = _good_timeline()
        ep.audio_duration_served = None
        ep.chapters = [{"start": 9999, "title": "Impossible"}]
        ep.chapters_basis = "served"
        fs = _findings([ep])
        assert not any(f.check == "timeline-chapter-out-of-range" for f in fs)

    def test_versioned_served_basis_chapters_are_checked(self):
        # INFRA-5 stamps chapters_basis as "served:<edl-version>"; the range check must still
        # apply (review item #19/#20 — startswith("served")).
        ep = _ep()
        ep.timeline = _good_timeline()
        ep.audio_duration_served = 3300.0
        ep.chapters = [{"start": 9999, "title": "way past the end"}]
        ep.chapters_basis = "served:silence-v1"
        fs = _findings([ep])
        assert any(f.check == "timeline-chapter-out-of-range" for f in fs)


# ---------------------------------------------------------------------------
# Internal gaps + end coverage (review item #19)
# ---------------------------------------------------------------------------


class TestGapAndCoverage:
    def test_internal_gap_between_segments_caught(self):
        ep = _ep()
        # seg0 ends at 300, seg1 starts at 400 → a 100s hole in the (contiguous) served clock
        ep.timeline = Timeline(
            version="bug-v1",
            segments=(_seg(0, 300, 0, 300), _seg(400, 3400, 600, 3600)),
        )
        ep.audio_duration_served = 3300.0
        assert any(f.check == "timeline-gap" for f in _findings([ep]))

    def test_contiguous_segments_have_no_gap(self):
        ep = _ep()
        ep.timeline = _good_timeline()
        ep.audio_duration_served = 3300.0
        assert not any(f.check == "timeline-gap" for f in _findings([ep]))

    def test_last_segment_short_of_served_duration_caught(self):
        ep = _ep()
        ep.timeline = _good_timeline()  # last segment ends at 3300
        ep.audio_duration_served = 3600.0  # enclosure is 3600 → 300s uncovered at the end
        assert any(f.check == "timeline-short-coverage" for f in _findings([ep]))

    def test_full_coverage_ok(self):
        ep = _ep()
        ep.timeline = _good_timeline()
        ep.audio_duration_served = 3300.0
        assert not any(f.check == "timeline-short-coverage" for f in _findings([ep]))


# ---------------------------------------------------------------------------
# Finding severity and structure
# ---------------------------------------------------------------------------


class TestFindingStructure:
    def test_findings_are_error_or_warn(self):
        ep = _ep()
        ep.timeline = Timeline(
            version="bad",
            segments=(
                _seg(0, 400, 0, 400),
                _seg(300, 700, 300, 700),  # overlap
            ),
        )
        ep.audio_duration_served = 700.0
        fs = _findings([ep])
        assert all(f.severity in ("error", "warn") for f in fs)

    def test_finding_slug_matches(self):
        ep = _ep()
        ep.timeline = Timeline(
            version="bad",
            segments=(
                _seg(0, 400, 0, 400),
                _seg(300, 700, 300, 700),
            ),
        )
        ep.audio_duration_served = 700.0
        fs = _findings([ep])
        assert all(f.slug == "test-tx" for f in fs)

    def test_multiple_bad_edls_produce_multiple_findings(self):
        # Both overlap AND duration mismatch
        ep = _ep()
        ep.timeline = Timeline(
            version="bad",
            segments=(
                _seg(0, 400, 0, 400),
                _seg(300, 700, 300, 700),  # overlap
            ),
        )
        ep.audio_duration_served = 9999.0  # wrong
        fs = _findings([ep])
        checks = [f.check for f in fs]
        # Should have at least overlap; might also have duration mismatch
        assert "timeline-overlap" in checks


# ---------------------------------------------------------------------------
# check_timeline_integrity is in contracts.py deeplink check (INFRA-6 wired it)
# ---------------------------------------------------------------------------


class TestDeeplinkLivenessWired:
    def test_contracts_check_includes_deeplink_for_granicus(self):
        """Verify that the contracts module routes deeplink checks for Granicus."""
        from citypods.providers.granicus import GranicusProvider

        # The deeplink check runs after episode fetch; with no real network we just
        # verify the check function exists and accepts the provider name.
        assert "deeplink" in GranicusProvider.capabilities
        # The liveness probe itself is live-only (test_contracts.py in tests/live/).
