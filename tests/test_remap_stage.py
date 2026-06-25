"""Tests for INFRA-5 (issue #146): artifact remap + served/source basis convention.

Acceptance criteria:
- Source-time chapters remap correctly onto a trimmed EDL; cut chapters drop.
- Untimed transcript content flagged (is_timed_transcript returns False for plain text/PDF).
- chapters_basis field propagates through record round-trip.
- RemapStage is a no-op for identity timelines.
- Ordering: remap stage precedes audio.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from citypods.models import City, Episode
from citypods.stages import (
    RemapStage,
    StageContext,
    default_stages,
    enrich_stages,
    is_timed_transcript,
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


def _ep(chapters=None) -> Episode:
    return Episode(
        guid="g1",
        uid="uid-g1",
        title="Meeting",
        published=datetime(2026, 5, 20, tzinfo=UTC),
        video_url="https://src/vid.mp4",
        media_kind="direct",
        duration=3600,
        chapters=chapters or [],
    )


def _ctx(tmp_path: Path) -> StageContext:
    class FakeFfmpeg:
        def extract_audio(
            self,
            tl,
            srcs,
            dest,
            ch=None,
            *,
            loudness_profile=None,
            processing_profile=None,
            asset_resolver=None,
        ):
            dest.write_bytes(b"fake")

    return StageContext(
        storage=LocalStorage(root=tmp_path / "audio", url_prefix="https://cdn/"),
        ffmpeg=FakeFfmpeg(),
        max_kbps=96,
        dry_run=True,
    )


class FakeProvider:
    def resolve_media_url(self, ep, source):
        return ep.video_url


def _trimmed_timeline() -> Timeline:
    """Source 3600s; silence cut 300–600 → served [0,300] ++ [300,3300]."""
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


# ---------------------------------------------------------------------------
# RemapStage — identity timeline (no-op)
# ---------------------------------------------------------------------------


class TestRemapStageIdentity:
    def test_noop_when_timeline_is_none(self, tmp_path):
        ep = _ep(chapters=[{"start": 0, "title": "A"}, {"start": 120, "title": "B"}])
        ep.timeline = None  # identity
        stage = RemapStage()
        stats = stage.process(FakeProvider(), _city(), [ep], _ctx(tmp_path))
        # chapters stay in source-time (unchanged)
        assert ep.chapters[0]["start"] == 0
        assert ep.chapters[1]["start"] == 120
        assert ep.chapters_basis == "source:s0"
        assert stats.reused == 1

    def test_noop_when_identity_timeline(self, tmp_path):
        src = SourceMedia(
            id="s0", provider="g", ref="u", media_kind="direct", duration=3600.0, watch_url=None
        )
        ep = _ep(chapters=[{"start": 60, "title": "A"}])
        ep.timeline = identity_timeline(src, 3600.0)
        stage = RemapStage()
        stats = stage.process(FakeProvider(), _city(), [ep], _ctx(tmp_path))
        assert ep.chapters[0]["start"] == 60  # unchanged
        assert ep.chapters_basis == "source:s0"
        assert stats.reused == 1

    def test_noop_when_no_chapters(self, tmp_path):
        ep = _ep(chapters=[])
        ep.timeline = _trimmed_timeline()
        stage = RemapStage()
        stats = stage.process(FakeProvider(), _city(), [ep], _ctx(tmp_path))
        assert ep.chapters == []
        assert stats.reused == 1

    def test_noop_when_already_served(self, tmp_path):
        ep = _ep(chapters=[{"start": 200, "title": "A"}])
        ep.timeline = _trimmed_timeline()
        ep.chapters_basis = "served"  # already remapped
        stage = RemapStage()
        stats = stage.process(FakeProvider(), _city(), [ep], _ctx(tmp_path))
        assert ep.chapters[0]["start"] == 200  # not re-remapped
        assert stats.reused == 1


# ---------------------------------------------------------------------------
# RemapStage — trimmed timeline (chapters remap + cut-span drop)
# ---------------------------------------------------------------------------


class TestRemapStageTrimmed:
    def test_chapters_in_first_span_remap_unchanged(self, tmp_path):
        ep = _ep(
            chapters=[
                {"start": 0, "title": "Call to order"},
                {"start": 200, "title": "Agenda"},
            ]
        )
        ep.timeline = _trimmed_timeline()
        RemapStage().process(FakeProvider(), _city(), [ep], _ctx(tmp_path))
        assert ep.chapters[0]["start"] == 0.0
        assert ep.chapters[1]["start"] == 200.0

    def test_chapters_in_second_span_get_offset(self, tmp_path):
        ep = _ep(
            chapters=[
                {"start": 600, "title": "After recess"},
                {"start": 1200, "title": "New business"},
            ]
        )
        ep.timeline = _trimmed_timeline()
        RemapStage().process(FakeProvider(), _city(), [ep], _ctx(tmp_path))
        # source 600 → served 300; source 1200 → served 900
        assert ep.chapters[0]["start"] == 300.0
        assert ep.chapters[1]["start"] == 900.0

    def test_cut_span_chapters_are_dropped(self, tmp_path):
        ep = _ep(
            chapters=[
                {"start": 0, "title": "Before cut"},
                {"start": 350, "title": "IN CUT SPAN — should be dropped"},
                {"start": 600, "title": "After cut"},
            ]
        )
        ep.timeline = _trimmed_timeline()
        RemapStage().process(FakeProvider(), _city(), [ep], _ctx(tmp_path))
        titles = [c["title"] for c in ep.chapters]
        assert "IN CUT SPAN — should be dropped" not in titles
        assert len(ep.chapters) == 2

    def test_chapters_basis_set_to_served(self, tmp_path):
        ep = _ep(chapters=[{"start": 0, "title": "A"}])
        ep.timeline = _trimmed_timeline()  # version "silence-v1"
        RemapStage().process(FakeProvider(), _city(), [ep], _ctx(tmp_path))
        # basis records the EDL version it was remapped against (review item #10)
        assert ep.chapters_basis == "served:silence-v1"

    def test_stats_ran_incremented(self, tmp_path):
        ep = _ep(chapters=[{"start": 0, "title": "A"}])
        ep.timeline = _trimmed_timeline()
        stats = RemapStage().process(FakeProvider(), _city(), [ep], _ctx(tmp_path))
        assert stats.ran == 1

    def test_chapter_end_remapped(self, tmp_path):
        ep = _ep(chapters=[{"start": 0, "end": 200, "title": "A"}])
        ep.timeline = _trimmed_timeline()
        RemapStage().process(FakeProvider(), _city(), [ep], _ctx(tmp_path))
        assert ep.chapters[0]["end"] == 200.0  # source 200 → served 200

    def test_chapter_end_none_when_cut(self, tmp_path):
        # Chapter starts at 200, ends at 400 (which is in the cut 300-600)
        ep = _ep(chapters=[{"start": 200, "end": 400, "title": "Spans cut"}])
        ep.timeline = _trimmed_timeline()
        RemapStage().process(FakeProvider(), _city(), [ep], _ctx(tmp_path))
        assert len(ep.chapters) == 1
        assert ep.chapters[0]["start"] == 200.0
        assert ep.chapters[0]["end"] is None  # end was cut

    def test_extra_chapter_keys_preserved(self, tmp_path):
        ep = _ep(chapters=[{"start": 0, "title": "A", "speaker": "Mayor"}])
        ep.timeline = _trimmed_timeline()
        RemapStage().process(FakeProvider(), _city(), [ep], _ctx(tmp_path))
        assert ep.chapters[0]["speaker"] == "Mayor"

    def test_source_id_from_sources_registry(self, tmp_path):
        """When ep.sources is populated, use the first source's id."""
        ep = _ep(chapters=[{"start": 600, "title": "A"}])
        ep.timeline = _trimmed_timeline()
        ep.sources = [
            SourceMedia(
                id="s0", provider="g", ref="u", media_kind="direct", duration=3600.0, watch_url=None
            )
        ]
        RemapStage().process(FakeProvider(), _city(), [ep], _ctx(tmp_path))
        assert ep.chapters[0]["start"] == 300.0  # correctly remapped via s0


# ---------------------------------------------------------------------------
# is_timed_transcript — heuristic detection
# ---------------------------------------------------------------------------


class TestIsTimedTranscript:
    def test_webvtt_is_timed(self):
        content = b"WEBVTT\n\n00:00:01.000 --> 00:00:05.000\nHello world\n"
        assert is_timed_transcript(content) is True

    def test_webvtt_with_bom_is_timed(self):
        content = b"\xef\xbb\xbfWEBVTT\n\n00:00:01.000 --> 00:00:05.000\nHi\n"
        assert is_timed_transcript(content) is True

    def test_srt_is_timed(self):
        content = b"1\n00:00:01,000 --> 00:00:05,000\nHello world\n"
        assert is_timed_transcript(content) is True

    def test_plain_text_is_untimed(self):
        content = b"These are the minutes of the city council meeting held on May 19, 2026."
        assert is_timed_transcript(content) is False

    def test_pdf_header_is_untimed(self):
        content = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj"
        assert is_timed_transcript(content) is False

    def test_empty_bytes_is_untimed(self):
        assert is_timed_transcript(b"") is False

    def test_json_without_timestamps_is_untimed(self):
        content = b'{"items": [{"text": "hello", "speaker": "Mayor"}]}'
        assert is_timed_transcript(content) is False


# ---------------------------------------------------------------------------
# chapters_basis round-trip through records
# ---------------------------------------------------------------------------


class TestChaptersBasisRoundTrip:
    def test_served_basis_survives_record_round_trip(self):
        from citypods.records import episode_to_record, record_to_episode

        ep = _ep(chapters=[{"start": 300, "title": "Remapped"}])
        ep.chapters_basis = "served"
        rec = episode_to_record(ep)
        ep2 = record_to_episode(rec)
        assert ep2.chapters_basis == "served"

    def test_default_basis_is_source_s0(self):
        from citypods.records import episode_to_record, record_to_episode

        ep = _ep()
        ep2 = record_to_episode(episode_to_record(ep))
        assert ep2.chapters_basis == "source:s0"


# ---------------------------------------------------------------------------
# Ordering assertions (remap precedes audio)
# ---------------------------------------------------------------------------


class TestRemapOrdering:
    def _names(self, stages):
        return [s.name for s in stages]

    def test_remap_before_audio_in_default_stages(self):
        names = self._names(default_stages())
        assert names.index("remap") < names.index("audio")

    def test_remap_before_audio_in_enrich_stages(self):
        names = self._names(enrich_stages())
        assert names.index("remap") < names.index("audio")

    def test_timeline_before_remap(self):
        names = self._names(default_stages())
        assert names.index("timeline") < names.index("remap")

    def test_full_order_default(self):
        names = self._names(default_stages())
        assert names == ["chapters", "timeline", "remap", "audio", "transcript", "diarize", "links"]

    def test_full_order_enrich(self):
        names = self._names(enrich_stages())
        assert names == ["chapters", "timeline", "remap", "audio", "transcript", "diarize"]
