"""Tests for INFRA-8 (issue #149): transcript artifact storage.

Acceptance criteria:
- Reuse-first provider transcript stored + referenced.
- ASR slot stubbed (no-op when no provider transcript).
- Feed emits <podcast:transcript> tag only when synced/present.
- Episode.transcript_url removed; new transcript artifact fields used.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from citypods.models import City, Episode
from citypods.records import episode_to_record, record_to_episode
from citypods.stages import (
    StageContext,
    TranscriptStage,
    default_stages,
    enrich_stages,
)
from citypods.storage.local import LocalStorage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _city() -> City:
    return City(
        slug="t-tx", provider="civicplus",
        source={"feed_url": "x"}, podcast_title="T",
        podcast_author="City of T", podcast_email="", podcast_description="d",
    )


def _ep(uid="uid-g1", links=None) -> Episode:
    return Episode(
        guid="g1", uid=uid, title="Meeting",
        published=datetime(2026, 5, 20, tzinfo=UTC),
        video_url="https://src/vid.mp4",
        media_kind="direct", duration=3600,
        links=links or {},
    )


class FakeFfmpeg:
    def extract_audio(self, tl, srcs, dest, ch=None, *, loudness_profile=None, asset_resolver=None):
        dest.write_bytes(b"fake")


def _ctx(tmp_path: Path) -> StageContext:
    return StageContext(
        storage=LocalStorage(root=tmp_path / "audio", url_prefix="https://cdn/"),
        ffmpeg=FakeFfmpeg(),
        max_kbps=96,
        dry_run=False,
    )


class FakeProvider:
    def resolve_media_url(self, ep, source):
        return ep.video_url


VTT_CONTENT = b"WEBVTT\n\n00:00:01.000 --> 00:00:05.000\nHello world\n"
SRT_CONTENT = b"1\n00:00:01,000 --> 00:00:05,000\nHello world\n"
PLAIN_CONTENT = b"These are the minutes of the meeting. No timestamps."


# ---------------------------------------------------------------------------
# Episode model — transcript_url removed
# ---------------------------------------------------------------------------

class TestTranscriptUrlRemoved:
    def test_episode_has_no_transcript_url_field(self):
        ep = _ep()
        assert not hasattr(ep, "transcript_url"), \
            "transcript_url should be removed in INFRA-8"

    def test_episode_has_transcript_hosted_url(self):
        ep = _ep()
        assert hasattr(ep, "transcript_hosted_url")
        assert ep.transcript_hosted_url is None

    def test_episode_has_transcript_synced_false_by_default(self):
        ep = _ep()
        assert ep.transcript_synced is False

    def test_episode_has_transcript_key(self):
        ep = _ep()
        assert hasattr(ep, "transcript_key")
        assert ep.transcript_key is None

    def test_episode_has_transcript_basis(self):
        ep = _ep()
        assert ep.transcript_basis == "source:s0"


# ---------------------------------------------------------------------------
# Transcript record round-trip
# ---------------------------------------------------------------------------

class TestTranscriptRecordRoundTrip:
    def test_transcript_block_survives_round_trip(self):
        ep = _ep()
        ep.transcript_key = "transcripts/src/uid-g1-abc.vtt"
        ep.transcript_hosted_url = "https://cdn/transcripts/src/uid-g1-abc.vtt"
        ep.transcript_spec_hash = "abc123def456"
        ep.transcript_format = "vtt"
        ep.transcript_basis = "served"
        ep.transcript_synced = True

        rec = episode_to_record(ep)
        assert rec["transcript"] is not None
        assert rec["transcript"]["synced"] is True
        assert rec["transcript"]["format"] == "vtt"
        assert rec["transcript"]["basis"] == "served"

        ep2 = record_to_episode(rec)
        assert ep2.transcript_key == ep.transcript_key
        assert ep2.transcript_hosted_url == ep.transcript_hosted_url
        assert ep2.transcript_synced is True
        assert ep2.transcript_format == "vtt"
        assert ep2.transcript_basis == "served"

    def test_no_transcript_stores_null(self):
        ep = _ep()
        rec = episode_to_record(ep)
        assert rec.get("transcript") is None

    def test_v1_record_without_transcript_block_loads_cleanly(self):
        v1_rec = {
            "uid": "uid-g1",
            "provider_guid": "g1",
            "title": "Meeting",
            "published": "2026-05-20T00:00:00+00:00",
            "video_url": "https://src/v.mp4",
            "audio": {},
        }
        ep = record_to_episode(v1_rec)
        assert ep.transcript_key is None
        assert ep.transcript_synced is False

    def test_old_transcript_url_in_v1_record_is_silently_dropped(self):
        """V1 records had a top-level transcript_url key — it must be silently ignored."""
        v1_rec = {
            "uid": "uid-g1",
            "provider_guid": "g1",
            "title": "Meeting",
            "published": "2026-05-20T00:00:00+00:00",
            "video_url": "https://src/v.mp4",
            "audio": {},
            "transcript_url": "https://old-provider-url/transcript.pdf",  # old field
        }
        ep = record_to_episode(v1_rec)
        assert ep.transcript_key is None
        assert ep.transcript_synced is False


# ---------------------------------------------------------------------------
# TranscriptStage — no provider transcript (ASR slot stubbed)
# ---------------------------------------------------------------------------

class TestTranscriptStageASRStubbed:
    def test_noop_when_no_transcript_link(self, tmp_path):
        ep = _ep(links={})  # no "transcript" key in links
        stage = TranscriptStage()
        stats = stage.process(FakeProvider(), _city(), [ep], _ctx(tmp_path))
        assert ep.transcript_key is None
        assert stats.ran == 0
        assert stats.reused == 0

    def test_dry_run_skips(self, tmp_path):
        ep = _ep(links={"transcript": "https://provider/transcript.vtt"})
        ctx = StageContext(
            storage=LocalStorage(root=tmp_path / "a", url_prefix="https://cdn/"),
            ffmpeg=FakeFfmpeg(), max_kbps=96, dry_run=True,
        )
        stage = TranscriptStage()
        stats = stage.process(FakeProvider(), _city(), [ep], ctx)
        assert ep.transcript_key is None
        assert stats.ran == 0


# ---------------------------------------------------------------------------
# TranscriptStage — VTT provider transcript (mocked HTTP)
# ---------------------------------------------------------------------------

class TestTranscriptStageVTT:
    def _run_with_content(self, tmp_path, content: bytes, url="https://provider/t.vtt"):
        from unittest.mock import patch

        ep = _ep(links={"transcript": url})
        ep.timeline = None  # identity

        _body = content  # capture for closure

        class _FakeSession:
            def __enter__(self): return self
            def __exit__(self, *a): pass

            def get(self, url, **kw):
                class _R:
                    status_code = 200
                _r = _R()
                _r.content = _body
                return _r

        with patch("citypods.http.make_session", return_value=_FakeSession()):
            stage = TranscriptStage()
            stats = stage.process(FakeProvider(), _city(), [ep], _ctx(tmp_path))

        return ep, stats

    def test_vtt_stored_and_synced(self, tmp_path):
        ep, stats = self._run_with_content(tmp_path, VTT_CONTENT)
        assert stats.ran == 1
        assert ep.transcript_key is not None
        assert ep.transcript_synced is True
        assert ep.transcript_format == "vtt"

    def test_vtt_basis_served_for_identity_timeline(self, tmp_path):
        ep, _ = self._run_with_content(tmp_path, VTT_CONTENT)
        assert ep.transcript_basis == "served"

    def test_vtt_object_uploaded_to_storage(self, tmp_path):
        ep, _ = self._run_with_content(tmp_path, VTT_CONTENT)
        assert ep.transcript_hosted_url is not None
        assert ep.transcript_hosted_url.startswith("https://")
        assert ep.transcript_key is not None
        # Object should exist on disk
        audio_dir = tmp_path / "audio"
        assert (audio_dir / ep.transcript_key).exists()

    def test_srt_detected_as_synced(self, tmp_path):
        ep, stats = self._run_with_content(tmp_path, SRT_CONTENT)
        assert ep.transcript_synced is True
        assert ep.transcript_format == "srt"

    def test_plain_text_not_synced(self, tmp_path):
        ep, stats = self._run_with_content(tmp_path, PLAIN_CONTENT)
        assert ep.transcript_synced is False
        assert ep.transcript_format == "txt"

    def test_reuse_skips_refetch(self, tmp_path):
        ep, _ = self._run_with_content(tmp_path, VTT_CONTENT)
        first_key = ep.transcript_key

        class _FakeSession:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def get(self, *a, **kw):
                raise AssertionError("should not fetch again on reuse")

        with patch("citypods.http.make_session", return_value=_FakeSession()):
            stage = TranscriptStage()
            stats = stage.process(FakeProvider(), _city(), [ep], _ctx(tmp_path))
        assert ep.transcript_key == first_key
        assert stats.reused == 1


# ---------------------------------------------------------------------------
# feeds.py — <podcast:transcript> tag emission
# ---------------------------------------------------------------------------

class TestTranscriptTagEmission:
    def _build_feed(self, ep: Episode, base_url="https://example.com") -> str:
        from citypods.feeds import build_rss
        return build_rss(_city(), [ep], kind="audio", base_url=base_url)

    def test_tag_emitted_when_synced(self):
        ep = _ep()
        ep.transcript_hosted_url = "https://cdn/transcript.vtt"
        ep.transcript_synced = True
        ep.transcript_format = "vtt"
        ep.hosted_audio_url = "https://cdn/ep.m4a"  # need enclosure to appear
        xml = self._build_feed(ep)
        assert "<podcast:transcript" in xml
        assert 'type="text/vtt"' in xml
        assert "cdn/transcript.vtt" in xml

    def test_tag_not_emitted_when_not_synced(self):
        ep = _ep()
        ep.transcript_hosted_url = "https://cdn/transcript.vtt"
        ep.transcript_synced = False
        ep.hosted_audio_url = "https://cdn/ep.m4a"
        xml = self._build_feed(ep)
        assert "<podcast:transcript" not in xml

    def test_tag_not_emitted_when_no_transcript(self):
        ep = _ep()
        ep.hosted_audio_url = "https://cdn/ep.m4a"
        xml = self._build_feed(ep)
        assert "<podcast:transcript" not in xml

    def test_srt_tag_has_correct_mime(self):
        ep = _ep()
        ep.transcript_hosted_url = "https://cdn/transcript.srt"
        ep.transcript_synced = True
        ep.transcript_format = "srt"
        ep.hosted_audio_url = "https://cdn/ep.m4a"
        xml = self._build_feed(ep)
        assert 'type="application/x-subrip"' in xml


# ---------------------------------------------------------------------------
# Stage ordering
# ---------------------------------------------------------------------------

class TestTranscriptStageOrdering:
    def _names(self, stages):
        return [s.name for s in stages]

    def test_transcript_after_audio_in_default(self):
        names = self._names(default_stages())
        assert names.index("transcript") > names.index("audio")

    def test_transcript_after_audio_in_enrich(self):
        names = self._names(enrich_stages())
        assert names.index("transcript") > names.index("audio")

    def test_full_order_default(self):
        assert self._names(default_stages()) == [
            "chapters", "timeline", "remap", "audio", "transcript", "links"
        ]

    def test_full_order_enrich(self):
        assert self._names(enrich_stages()) == [
            "chapters", "timeline", "remap", "audio", "transcript"
        ]
