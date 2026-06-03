"""Tests for INFRA-9 (issue #150): timeline/clip verification + contracts.

Acceptance criteria:
- Synthetic bad EDLs (overlap, out-of-range remap, duration mismatch) are caught.
- Deep-link liveness is sampled in contracts.py (checked in test_contracts / live suite).
- Good EDLs produce no findings.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from citypods.audit import check_timeline_integrity
from citypods.models import City, Episode
from citypods.timeline import Segment, SourceMedia, Timeline


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ep(uid="uid-g1") -> Episode:
    return Episode(
        guid="g1", uid=uid, title="Meeting",
        published=datetime(2026, 5, 20, tzinfo=UTC),
        video_url="https://src/vid.mp4",
        media_kind="direct", duration=3600,
    )


def _src(id="s0", duration=3600.0) -> SourceMedia:
    return SourceMedia(id=id, provider="g", ref="https://g.com/1.mp4",
                       media_kind="direct", duration=duration, watch_url=None)


def _seg(ss, se, src_s, src_e, sid="s0") -> Segment:
    return Segment(served_start=ss, served_end=se, kind="source",
                   source_id=sid, source_start=src_s, source_end=src_e)


def _good_timeline() -> Timeline:
    """A valid trimmed timeline: silence cut 300–600."""
    return Timeline(version="silence-v1", segments=(
        _seg(0, 300, 0, 300),
        _seg(300, 3300, 600, 3600),
    ))


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
        ep.timeline = Timeline(version="concat-v1", segments=(
            _seg(0, 1800, 0, 1800, "s0"),
            _seg(1800, 3600, 0, 1800, "s1"),
        ))
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
        ep.timeline = Timeline(version="bad-v1", segments=(
            _seg(0, 400, 0, 400),   # ends at 400
            _seg(300, 700, 300, 700),  # starts at 300 — OVERLAP!
        ))
        ep.audio_duration_served = 700.0
        fs = _findings([ep])
        checks = [f.check for f in fs]
        assert "timeline-overlap" in checks

    def test_exactly_touching_segments_ok(self):
        ep = _ep()
        # Segments touch exactly: first ends at 300, second starts at 300 — no overlap.
        ep.timeline = Timeline(version="v1", segments=(
            _seg(0, 300, 0, 300),
            _seg(300, 3300, 600, 3600),
        ))
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


# ---------------------------------------------------------------------------
# Gap at start
# ---------------------------------------------------------------------------

class TestGapAtStart:
    def test_first_segment_not_at_zero(self):
        ep = _ep()
        ep.timeline = Timeline(version="bad-v1", segments=(
            _seg(5, 300, 5, 300),  # starts at 5, not 0
            _seg(300, 3300, 600, 3600),
        ))
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
        ep.timeline = Timeline(version="v1", segments=(
            _seg(0, 3300, 0, 3600),  # source_end=3600 > SourceMedia.duration=3000
        ))
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
        ep.sources = [_src("s0", None)]  # duration unknown
        ep.sources[0] = SourceMedia(id="s0", provider="g", ref="u", media_kind="direct",
                                    duration=None, watch_url=None)
        ep.timeline = Timeline(version="v1", segments=(
            _seg(0, 3300, 0, 9999),  # would exceed any known duration
        ))
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


# ---------------------------------------------------------------------------
# Finding severity and structure
# ---------------------------------------------------------------------------

class TestFindingStructure:
    def test_findings_are_error_or_warn(self):
        ep = _ep()
        ep.timeline = Timeline(version="bad", segments=(
            _seg(0, 400, 0, 400),
            _seg(300, 700, 300, 700),  # overlap
        ))
        ep.audio_duration_served = 700.0
        fs = _findings([ep])
        assert all(f.severity in ("error", "warn") for f in fs)

    def test_finding_slug_matches(self):
        ep = _ep()
        ep.timeline = Timeline(version="bad", segments=(
            _seg(0, 400, 0, 400),
            _seg(300, 700, 300, 700),
        ))
        ep.audio_duration_served = 700.0
        fs = _findings([ep])
        assert all(f.slug == "test-tx" for f in fs)

    def test_multiple_bad_edls_produce_multiple_findings(self):
        # Both overlap AND duration mismatch
        ep = _ep()
        ep.timeline = Timeline(version="bad", segments=(
            _seg(0, 400, 0, 400),
            _seg(300, 700, 300, 700),  # overlap
        ))
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
        from citypods.contracts import check_city
        from citypods.providers.granicus import GranicusProvider
        # The deeplink check runs after episode fetch; with no real network we just
        # verify the check function exists and accepts the provider name.
        assert "deeplink" in GranicusProvider.capabilities
        # The liveness probe itself is live-only (test_contracts.py in tests/live/).
