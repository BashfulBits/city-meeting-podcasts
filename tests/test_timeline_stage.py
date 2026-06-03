"""Tests for INFRA-4 (issue #145): TimelineStage + TimelinePlanner protocol.

Acceptance criteria:
- Stage produces identity (None) timeline when no planner fires.
- A fake planner composes segments correctly onto the episode.
- Ordering test: timeline stage precedes audio in default_stages and enrich_stages.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from citypods.models import City, Episode
from citypods.stages import (
    StageContext,
    TimelineStage,
    default_stages,
    enrich_stages,
)
from citypods.storage.local import LocalStorage
from citypods.timeline import Segment, SourceMedia, Timeline, identity_timeline

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _ep(guid="g1") -> Episode:
    return Episode(
        guid=guid,
        uid=f"uid-{guid}",
        title="Meeting",
        published=datetime(2026, 5, 20, tzinfo=UTC),
        video_url="https://src/vid.mp4",
        media_kind="direct",
        duration=3600,
    )


def _ctx(tmp_path: Path) -> StageContext:
    return StageContext(
        storage=LocalStorage(root=tmp_path / "audio", url_prefix="https://cdn/"),
        ffmpeg=_FakeFfmpeg(),
        max_kbps=96,
        dry_run=True,  # no real uploads
    )


class _FakeFfmpeg:
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
        dest.write_bytes(b"fake")


class FakeProvider:
    def resolve_media_url(self, ep, source):
        return ep.video_url


# ---------------------------------------------------------------------------
# TimelinePlanner helpers
# ---------------------------------------------------------------------------


class _SilencePlanner:
    """Fake planner: cuts source 300–600 (returns trimmed two-segment Timeline)."""

    name = "silence-fake"

    def plan(self, provider, city, ep, ctx, current):
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


class _IntroPlannerFake:
    """Fake planner: prepends a 30s intro insert to whatever timeline it receives."""

    name = "intro-fake"

    def plan(self, provider, city, ep, ctx, current):
        # Shift existing segments by 30s and prepend an intro insert
        if current is None:
            base_segs = (
                Segment(
                    served_start=30,
                    served_end=3630,
                    kind="source",
                    source_id="s0",
                    source_start=0,
                    source_end=3600,
                ),
            )
        else:
            base_segs = tuple(
                Segment(
                    served_start=s.served_start + 30,
                    served_end=s.served_end + 30,
                    kind=s.kind,
                    source_id=s.source_id,
                    source_start=s.source_start,
                    source_end=s.source_end,
                    insert=s.insert,
                    asset_id=s.asset_id,
                    asset_version=s.asset_version,
                )
                for s in current.segments
            )
        intro = Segment(
            served_start=0,
            served_end=30,
            kind="insert",
            insert="intro",
            asset_id="brand",
            asset_version="1",
        )
        return Timeline(version="intro-v1", segments=(intro,) + base_segs)


class _NoOpPlanner:
    """Planner that never fires."""

    name = "noop"

    def plan(self, provider, city, ep, ctx, current):
        return None  # always passes through


# ---------------------------------------------------------------------------
# TimelineStage — no planners (identity path)
# ---------------------------------------------------------------------------


class TestTimelineStageNoPlanner:
    def test_timeline_stays_none_when_no_planners(self, tmp_path):
        ep = _ep()
        stage = TimelineStage(planners=[])
        stage.process(FakeProvider(), _city(), [ep], _ctx(tmp_path))
        assert ep.timeline is None

    def test_stats_reused_when_no_planners(self, tmp_path):
        ep = _ep()
        stage = TimelineStage(planners=[])
        stats = stage.process(FakeProvider(), _city(), [ep], _ctx(tmp_path))
        assert stats.reused == 1
        assert stats.ran == 0

    def test_already_set_timeline_stays_when_no_planners(self, tmp_path):
        src = SourceMedia(
            id="s0", provider="g", ref="u", media_kind="direct", duration=3600.0, watch_url=None
        )
        ep = _ep()
        ep.timeline = identity_timeline(src, 3600.0)
        stage = TimelineStage(planners=[])
        stage.process(FakeProvider(), _city(), [ep], _ctx(tmp_path))
        assert ep.timeline is not None  # unchanged

    def test_noop_planner_leaves_timeline_none(self, tmp_path):
        ep = _ep()
        stage = TimelineStage(planners=[_NoOpPlanner()])
        stage.process(FakeProvider(), _city(), [ep], _ctx(tmp_path))
        assert ep.timeline is None
        stats = stage.process(FakeProvider(), _city(), [ep], _ctx(tmp_path))
        assert stats.reused == 1


# ---------------------------------------------------------------------------
# TimelineStage — single planner
# ---------------------------------------------------------------------------


class TestTimelineStageSinglePlanner:
    def test_planner_result_set_on_episode(self, tmp_path):
        ep = _ep()
        stage = TimelineStage(planners=[_SilencePlanner()])
        stats = stage.process(FakeProvider(), _city(), [ep], _ctx(tmp_path))
        assert ep.timeline is not None
        assert ep.timeline.version == "silence-v1"
        assert len(ep.timeline.segments) == 2
        assert stats.ran == 1

    def test_silence_planner_trims_correctly(self, tmp_path):
        ep = _ep()
        stage = TimelineStage(planners=[_SilencePlanner()])
        stage.process(FakeProvider(), _city(), [ep], _ctx(tmp_path))
        segs = ep.timeline.segments
        assert segs[0].source_start == 0 and segs[0].source_end == 300
        assert segs[1].source_start == 600 and segs[1].source_end == 3600


# ---------------------------------------------------------------------------
# TimelineStage — planner composition (chained planners)
# ---------------------------------------------------------------------------


class TestTimelineStagePlannerComposition:
    def test_intro_planner_receives_previous_timeline(self, tmp_path):
        ep = _ep()
        # Silence runs first, then intro adds inserts on top
        stage = TimelineStage(planners=[_SilencePlanner(), _IntroPlannerFake()])
        stage.process(FakeProvider(), _city(), [ep], _ctx(tmp_path))

        tl = ep.timeline
        assert tl is not None
        assert tl.version == "intro-v1"
        # Should have an intro insert + the two silence-trimmed segments
        assert len(tl.segments) == 3
        assert tl.segments[0].kind == "insert"
        assert tl.segments[0].insert == "intro"
        # The silence segments should be shifted by 30s
        assert tl.segments[1].served_start == 30
        assert tl.segments[2].served_start == 330

    def test_stats_ran_is_1_even_with_two_planners(self, tmp_path):
        ep = _ep()
        stage = TimelineStage(planners=[_SilencePlanner(), _IntroPlannerFake()])
        stats = stage.process(FakeProvider(), _city(), [ep], _ctx(tmp_path))
        assert stats.ran == 1

    def test_noop_then_silence_still_fires(self, tmp_path):
        ep = _ep()
        stage = TimelineStage(planners=[_NoOpPlanner(), _SilencePlanner()])
        stage.process(FakeProvider(), _city(), [ep], _ctx(tmp_path))
        assert ep.timeline is not None
        assert ep.timeline.version == "silence-v1"

    def test_silence_then_noop_preserves_silence(self, tmp_path):
        ep = _ep()
        stage = TimelineStage(planners=[_SilencePlanner(), _NoOpPlanner()])
        stage.process(FakeProvider(), _city(), [ep], _ctx(tmp_path))
        assert ep.timeline is not None
        assert ep.timeline.version == "silence-v1"


# ---------------------------------------------------------------------------
# Ordering: timeline before audio in all stage lists
# ---------------------------------------------------------------------------


class TestStageOrdering:
    def _names(self, stages) -> list[str]:
        return [s.name for s in stages]

    def test_timeline_before_audio_in_default_stages(self):
        names = self._names(default_stages())
        assert names.index("timeline") < names.index("audio")

    def test_timeline_before_audio_in_enrich_stages(self):
        names = self._names(enrich_stages())
        assert names.index("timeline") < names.index("audio")

    def test_chapters_before_timeline_in_default_stages(self):
        names = self._names(default_stages())
        assert names.index("chapters") < names.index("timeline")

    def test_chapters_before_timeline_in_enrich_stages(self):
        names = self._names(enrich_stages())
        assert names.index("chapters") < names.index("timeline")

    def test_timeline_stage_present_in_default(self):
        assert "timeline" in self._names(default_stages())

    def test_timeline_stage_present_in_enrich(self):
        assert "timeline" in self._names(enrich_stages())

    def test_default_stages_order(self):
        # Full expected order for default_stages
        names = self._names(default_stages())
        assert names == ["chapters", "timeline", "audio", "links"]

    def test_enrich_stages_order(self):
        names = self._names(enrich_stages())
        assert names == ["chapters", "timeline", "audio"]


# ---------------------------------------------------------------------------
# TimelinePlanner is a structural Protocol (duck-typed)
# ---------------------------------------------------------------------------


class TestTimelinePlannerProtocol:
    def test_fake_planner_satisfies_protocol(self):
        """Runtime check that _SilencePlanner matches TimelinePlanner structurally."""
        p = _SilencePlanner()
        assert hasattr(p, "name")
        assert hasattr(p, "plan")
        assert callable(p.plan)

    def test_timeline_stage_accepts_any_compliant_object(self, tmp_path):
        """TimelineStage accepts any object with a .plan() method."""

        class _MinimalPlanner:
            name = "minimal"

            def plan(self, *a, **kw):
                return None  # no-op

        ep = _ep()
        stage = TimelineStage(planners=[_MinimalPlanner()])
        stage.process(FakeProvider(), _city(), [ep], _ctx(tmp_path))
        # Just verifying it doesn't raise
