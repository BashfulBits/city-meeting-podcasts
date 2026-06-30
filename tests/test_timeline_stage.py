"""Tests for INFRA-4 (issue #145): TimelineStage + TimelinePlanner protocol.

Acceptance criteria:
- Stage produces identity (None) timeline when no planner fires.
- A fake planner composes segments correctly onto the episode.
- Ordering test: timeline stage precedes audio in default_stages and enrich_stages.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from citypods.integrity import REPAIR_TIMELINE_REPLAN, set_timeline_audio_integrity
from citypods.media import MaterializeStats, RateLimitedMediaFetchError
from citypods.models import City, Episode
from citypods.stages import (
    AudioStage,
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
        sources=None,
        loudness_profile=None,
        processing_profile=None,
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
    version = "1"

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
    version = "1"

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
    version = "1"

    def plan(self, provider, city, ep, ctx, current):
        return None  # always passes through


class _DeferredPlanner:
    """Planner that explicitly defers after deciding the source cannot be planned safely."""

    name = "deferred-fake"
    version = "1"

    def plan(self, provider, city, ep, ctx, current):
        ep.timeline_defer_reason = "deferred_decode_unavailable"
        return None


class _CountingPlanner:
    name = "counting-fake"
    version = "1"

    def __init__(self):
        self.calls = 0

    def plan(self, provider, city, ep, ctx, current):
        self.calls += 1
        return identity_timeline(
            SourceMedia(
                id="s0",
                provider="civicplus",
                ref=ep.video_url,
                media_kind=ep.media_kind,
                duration=float(ep.duration or 0),
                watch_url=None,
                duration_basis="decoded",
            ),
            float(ep.duration or 0),
        )


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

    def test_deferred_planner_counts_reason_and_leaves_timeline_untouched(self, tmp_path):
        ep = _ep()
        stage = TimelineStage(planners=[_DeferredPlanner()])
        stats = stage.process(FakeProvider(), _city(), [ep], _ctx(tmp_path))
        assert ep.timeline is None
        assert stats.ran == 0
        assert stats.skipped == 1
        assert stats.defer_reasons == {"deferred_decode_unavailable": 1}

    def test_audio_stage_skips_episode_deferred_by_timeline_in_same_run(self, tmp_path):
        ep = _ep()
        ep.timeline_defer_reason = "deferred_decode_unavailable"
        ctx = _ctx(tmp_path)
        ctx.dry_run = False

        with patch("citypods.stages.materialize_audio", return_value=MaterializeStats()) as mock:
            stats = AudioStage().process(FakeProvider(), _city(), [ep], ctx)

        assert stats.ran == 0
        assert mock.call_args.args[1] == []

    def test_timeline_specific_materialize_backoff_defers_planning(self, tmp_path):
        ep = _ep()
        ep.materialize_attempts = 1
        ep.materialize_last_attempt = datetime.now(UTC).isoformat()
        ep.materialize_error = "timeline-decode"
        planner = _CountingPlanner()

        stats = TimelineStage(planners=[planner]).process(
            FakeProvider(), _city(), [ep], _ctx(tmp_path)
        )

        assert planner.calls == 0
        assert ep.timeline is None
        assert stats.skipped == 1
        assert stats.defer_reasons == {"timeline-decode-backoff": 1}

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
# TimelineStage — planner raises a provider-throttle error (e.g. SilencePlanner's
# source-cache prefetch hits a 403). Must be counted in stats + record the per-episode
# backoff, not silently swallowed by the global queue's blanket per-item catch.
# ---------------------------------------------------------------------------


class _RateLimitedPlanner:
    name = "rate-limited-fake"
    version = "1"

    def plan(self, provider, city, ep, ctx, current):
        raise RateLimitedMediaFetchError("ffmpeg speech-measure hit provider throttle (HTTP 403)")


class TestTimelineStagePlannerThrottle:
    def test_rate_limited_planner_counted_not_raised(self, tmp_path):
        ep = _ep()
        stage = TimelineStage(planners=[_RateLimitedPlanner()])
        stats = stage.process(FakeProvider(), _city(), [ep], _ctx(tmp_path))
        assert stats.rate_limited == 1
        assert len(stats.errors) == 1
        assert ep.materialize_attempts == 1
        assert ep.materialize_error == "rate_limited"
        assert ep.materialize_last_attempt is not None
        assert ep.timeline is None  # episode left untouched, not crashed

    def test_throttle_in_second_planner_does_not_lose_first_planner_result(self, tmp_path):
        # The first planner's result is discarded on a later throttle (no partial timeline is
        # stamped) — same all-or-nothing semantics as any other mid-loop exception.
        ep = _ep()
        stage = TimelineStage(planners=[_SilencePlanner(), _RateLimitedPlanner()])
        stage.process(FakeProvider(), _city(), [ep], _ctx(tmp_path))
        assert ep.timeline is None


# ---------------------------------------------------------------------------
# TimelineStage — single planner
# ---------------------------------------------------------------------------


class TestTimelineStageSinglePlanner:
    def test_planner_result_set_on_episode(self, tmp_path):
        ep = _ep()
        stage = TimelineStage(planners=[_SilencePlanner()])
        stats = stage.process(FakeProvider(), _city(), [ep], _ctx(tmp_path))
        assert ep.timeline is not None
        # version is stamped with the planner-set signature (review item #9), not the
        # planner's internal label, so a later run can detect staleness.
        assert ep.timeline.version == "silence-fake:1"
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
        assert tl.version == "intro-fake:1+silence-fake:1"  # planner-set signature (sorted)
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
        assert ep.timeline.version == "noop:1+silence-fake:1"

    def test_silence_then_noop_preserves_silence(self, tmp_path):
        ep = _ep()
        stage = TimelineStage(planners=[_SilencePlanner(), _NoOpPlanner()])
        stage.process(FakeProvider(), _city(), [ep], _ctx(tmp_path))
        assert ep.timeline is not None
        assert ep.timeline.version == "noop:1+silence-fake:1"
        assert len(ep.timeline.segments) == 2  # silence output survived the no-op


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
        names = self._names(default_stages())
        assert names == ["chapters", "timeline", "remap", "audio", "transcript", "diarize", "links"]

    def test_enrich_stages_order(self):
        names = self._names(enrich_stages())
        assert names == ["chapters", "timeline", "remap", "audio", "transcript", "diarize"]

    def test_silence_planner_registered_in_default_stages(self):
        from citypods.silence import SilencePlanner

        tl_stage = next(s for s in default_stages() if s.name == "timeline")
        assert any(isinstance(p, SilencePlanner) for p in tl_stage.planners)

    def test_swagit_concat_planner_registered_before_silence_in_default_stages(self):
        from citypods.concat import SwagitConcatPlanner
        from citypods.silence import SilencePlanner

        tl_stage = next(s for s in default_stages() if s.name == "timeline")
        planner_types = [type(p) for p in tl_stage.planners]
        assert planner_types.index(SwagitConcatPlanner) < planner_types.index(SilencePlanner)

    def test_silence_planner_registered_in_enrich_stages(self):
        from citypods.silence import SilencePlanner

        tl_stage = next(s for s in enrich_stages() if s.name == "timeline")
        assert any(isinstance(p, SilencePlanner) for p in tl_stage.planners)

    def test_swagit_concat_planner_registered_before_silence_in_enrich_stages(self):
        from citypods.concat import SwagitConcatPlanner
        from citypods.silence import SilencePlanner

        tl_stage = next(s for s in enrich_stages() if s.name == "timeline")
        planner_types = [type(p) for p in tl_stage.planners]
        assert planner_types.index(SwagitConcatPlanner) < planner_types.index(SilencePlanner)


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


# ---------------------------------------------------------------------------
# Plan-once / stop-gate (review item #9)
# ---------------------------------------------------------------------------


class _CountingSilence(_SilencePlanner):
    """Silence planner that counts how many times it actually runs."""

    version = "1"

    def __init__(self):
        self.calls = 0

    def plan(self, *a, **kw):
        self.calls += 1
        return super().plan(*a, **kw)


class TestTimelineStageSkipAndStop:
    def test_skips_replanning_when_signature_matches(self, tmp_path):
        ep = _ep()
        planner = _CountingSilence()
        stage = TimelineStage(planners=[planner])
        stage.process(FakeProvider(), _city(), [ep], _ctx(tmp_path))
        assert planner.calls == 1
        assert ep.timeline.version == "silence-fake:1"
        # Second run: the stored EDL already carries this signature → no recompute.
        stats = stage.process(FakeProvider(), _city(), [ep], _ctx(tmp_path))
        assert planner.calls == 1  # plan() not invoked again
        assert stats.reused == 1 and stats.ran == 0

    def test_repair_flag_forces_replanning_when_signature_matches(self, tmp_path):
        ep = _ep()
        planner = _CountingSilence()
        stage = TimelineStage(planners=[planner])
        stage.process(FakeProvider(), _city(), [ep], _ctx(tmp_path))
        set_timeline_audio_integrity(
            ep,
            {
                "status": "timeline-source-duration-mismatch",
                "repair": [REPAIR_TIMELINE_REPLAN],
            },
        )

        stats = stage.process(FakeProvider(), _city(), [ep], _ctx(tmp_path))

        assert planner.calls == 2
        assert stats.ran == 1

    def test_replans_when_planner_version_changes(self, tmp_path):
        ep = _ep()
        TimelineStage(planners=[_SilencePlanner()]).process(
            FakeProvider(), _city(), [ep], _ctx(tmp_path)
        )
        assert ep.timeline.version == "silence-fake:1"

        class _SilenceV2(_SilencePlanner):
            version = "2"

        stats = TimelineStage(planners=[_SilenceV2()]).process(
            FakeProvider(), _city(), [ep], _ctx(tmp_path)
        )
        assert ep.timeline.version == "silence-fake:2"  # signature changed → re-planned
        assert stats.ran == 1

    def test_stop_defers_planning(self, tmp_path):
        ep = _ep()
        ctx = StageContext(
            storage=LocalStorage(root=tmp_path / "a", url_prefix="https://cdn/"),
            ffmpeg=_FakeFfmpeg(),
            max_kbps=96,
            dry_run=True,
            stop=lambda: True,  # window spent → defer planning, don't fail
        )
        stats = TimelineStage(planners=[_SilencePlanner()]).process(
            FakeProvider(), _city(), [ep], ctx
        )
        assert ep.timeline is None  # not planned this run
        assert stats.skipped == 1 and stats.ran == 0
