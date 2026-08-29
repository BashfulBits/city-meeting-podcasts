"""Tests for INFRA-8 (issue #149): transcript artifact storage.

Acceptance criteria:
- Reuse-first provider transcript stored + referenced.
- ASR slot stubbed (no-op when no provider transcript).
- Feed emits <podcast:transcript> tag only when synced/present.
- Episode.transcript_url removed; new transcript artifact fields used.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from citypods.asr import TranscriptArtifacts, asr_initial_prompt, asr_spec_hash
from citypods.compute import DispatchCoordinator, DispatchTarget
from citypods.compute.base import InferenceJob, JobHandle
from citypods.compute.budget import Budget
from citypods.compute.local import LocalBackend
from citypods.models import City, Episode
from citypods.providers.swagit import TranscriptProbe
from citypods.records import (
    episode_to_record,
    record_to_episode,
    referenced_audio_keys,
    save_records,
    source_key,
    transcript_media_hash,
)
from citypods.stages import (
    ASR_PIPELINE_VERSION,
    AsrArtifactCache,
    AsrRuntimeLog,
    ProviderTranscriptDiarizeStage,
    StageContext,
    TranscriptStage,
    _asr_fits_remaining_budget,
    _asr_timeout_seconds,
    _provider_alignment_artifact_is_reusable,
    _provider_alignment_inputs,
    _provider_source_text,
    _provider_transcript_probe_due,
    _provider_vtt_words_json,
    _record_provider_transcript_probe,
    default_stages,
    enrich_stages,
)
from citypods.storage.local import LocalStorage
from citypods.timeline import Segment, SourceMedia, Timeline
from citypods.transcript_quality import TranscriptQualityRoute

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _city(**overrides) -> City:
    values = {
        "slug": "t-tx",
        "provider": "civicplus",
        "source": {"feed_url": "x"},
        "podcast_title": "T",
        "podcast_author": "City of T",
        "podcast_email": "",
        "podcast_description": "d",
    }
    values.update(overrides)
    return City(
        **values,
    )


def _ep(uid="uid-g1", links=None) -> Episode:
    return Episode(
        guid="g1",
        uid=uid,
        title="Meeting",
        published=datetime(2026, 5, 20, tzinfo=UTC),
        video_url="https://src/vid.mp4",
        media_kind="direct",
        duration=3600,
        links=links or {},
    )


class FakeFfmpeg:
    def extract_audio(
        self,
        tl,
        srcs,
        dest,
        ch=None,
        *,
        sources=None,
        loudness_profile=None,
        processing_profile=None,
        asset_resolver=None,
    ):
        dest.write_bytes(b"fake")


class FakeAdmission:
    def __init__(self, admitted=True):
        self.admitted = admitted
        self.calls: list[tuple[str, str]] = []

    def wait(self, *, kind, label, stop=None):
        self.calls.append((kind, label))
        return self.admitted


def _ctx(tmp_path: Path) -> StageContext:
    return StageContext(
        storage=LocalStorage(root=tmp_path / "audio", url_prefix="https://cdn/"),
        ffmpeg=FakeFfmpeg(),
        max_kbps=96,
        dry_run=False,
        transcript_quality_state_dir=tmp_path / "state",
    )


class FakeProvider:
    def resolve_media_url(self, ep, source):
        return ep.video_url


VTT_CONTENT = b"WEBVTT\n\n00:00:01.000 --> 00:00:05.000\nHello world\n"
WORD_VTT_CONTENT = (
    b"WEBVTT\n\n00:00:01.000 --> 00:00:05.000\n<00:00:01.000>Hello <00:00:02.000>world\n"
)
WORD_VTT_WITH_LEADING_TEXT = b"WEBVTT\n\n00:00:01.000 --> 00:00:05.000\nHello <00:00:02.000>world\n"
SRT_CONTENT = b"1\n00:00:01,000 --> 00:00:05,000\nHello world\n"
PLAIN_CONTENT = b"These are the minutes of the meeting. No timestamps."


@pytest.fixture(autouse=True)
def _skip_provider_url_dns_validation(monkeypatch):
    monkeypatch.setattr("citypods.stages.validate_source_url", lambda *_args, **_kwargs: None)


# ---------------------------------------------------------------------------
# Episode model — transcript_url removed
# ---------------------------------------------------------------------------


class TestTranscriptUrlRemoved:
    def test_episode_has_no_transcript_url_field(self):
        ep = _ep()
        assert not hasattr(ep, "transcript_url"), "transcript_url should be removed in INFRA-8"

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
        ep.transcript_words_key = "transcripts/src/uid-g1-asr-abc.words.json"
        ep.transcript_words_url = "https://cdn/transcripts/src/uid-g1-asr-abc.words.json"
        ep.transcript_pipeline_version = "3"

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
        assert ep2.transcript_words_key == ep.transcript_words_key
        assert ep2.transcript_words_url == ep.transcript_words_url
        assert ep2.transcript_pipeline_version == "3"

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
            ffmpeg=FakeFfmpeg(),
            max_kbps=96,
            dry_run=True,
        )
        stage = TranscriptStage()
        stats = stage.process(FakeProvider(), _city(), [ep], ctx)
        assert ep.transcript_key is None
        assert stats.ran == 0


# ---------------------------------------------------------------------------
# TranscriptStage — VTT provider transcript (mocked HTTP)
# ---------------------------------------------------------------------------


class TestTranscriptStageVTT:
    def test_legacy_txt_provider_alignment_is_not_reused(self):
        assert not _provider_alignment_artifact_is_reusable({"format": "txt"})
        assert not _provider_alignment_artifact_is_reusable(
            {"format": "txt", "align_pipeline_version": "2"}
        )

    @pytest.mark.parametrize("source_format", ["vtt", "srt"])
    def test_legacy_cue_timed_provider_alignment_is_reprocessed(self, source_format):
        assert not _provider_alignment_artifact_is_reusable(
            {"format": source_format, "align_pipeline_version": "2"}
        )

    def test_word_timed_vtt_keeps_leading_unmarked_words_and_align_text_strips_markers(self):
        words = json.loads(_provider_vtt_words_json(WORD_VTT_WITH_LEADING_TEXT).decode())
        assert [word["w"] for word in words["segments"][0]["words"]] == ["Hello", "world"]
        assert _provider_source_text(WORD_VTT_WITH_LEADING_TEXT, "vtt") == "Hello world"

    def test_provider_cue_times_are_remapped_to_served_time_for_alignment(self):
        ep = _ep()
        ep.sources = [
            SourceMedia(
                id="s0",
                provider="test",
                ref="https://src/vid.mp4",
                media_kind="direct",
                duration=30.0,
                watch_url=None,
            )
        ]
        ep.timeline = Timeline(
            version="cut-v1",
            segments=(
                Segment(
                    served_start=0.0,
                    served_end=10.0,
                    kind="source",
                    source_id="s0",
                    source_start=10.0,
                    source_end=20.0,
                ),
            ),
        )

        text, timed_segments = _provider_alignment_inputs(
            ep,
            b"WEBVTT\n\n00:00:12.000 --> 00:00:15.000\nHello world here\n",
            "vtt",
        )

        assert text == "Hello world here"
        assert timed_segments == [{"start": 2.0, "end": 5.0, "text": "Hello world here"}]

    def test_swagit_text_anchors_become_coarse_alignment_windows(self):
        ep = _ep()
        ep.duration = 600

        text, timed_segments = _provider_alignment_inputs(
            ep,
            b"* provider disclaimer\n"
            b"[CALL TO ORDER]\n"
            b"[00:00:04]\n"
            b"THE MEETING IS CALLED TO ORDER.\n"
            b"[00:05:01]\n"
            b"THANK YOU, MAYOR. THE NEXT ITEM IS BUDGET.\n",
            "txt",
        )

        assert text == (
            "THE MEETING IS CALLED TO ORDER.\nTHANK YOU, MAYOR. THE NEXT ITEM IS BUDGET."
        )
        assert timed_segments == [
            {"start": 4.0, "end": 301.0, "text": "THE MEETING IS CALLED TO ORDER."},
            {"start": 301.0, "end": 600.0, "text": "THANK YOU, MAYOR. THE NEXT ITEM IS BUDGET."},
        ]

    def test_sparse_swagit_text_anchors_fall_back_to_full_alignment(self):
        ep = _ep()
        ep.duration = 3600

        text, timed_segments = _provider_alignment_inputs(
            ep,
            b"[00:00:04]\nTHE MEETING IS CALLED TO ORDER.\n[00:30:01]\nTHANK YOU, MAYOR.\n",
            "txt",
        )

        assert timed_segments is None
        assert "THE MEETING IS CALLED TO ORDER." in text

    def test_swagit_coarse_windows_are_remapped_from_source_to_served_time(self):
        ep = _ep()
        ep.duration = 30
        ep.sources = [
            SourceMedia(
                id="s0",
                provider="test",
                ref="https://src/vid.mp4",
                media_kind="direct",
                duration=30.0,
                watch_url=None,
            )
        ]
        ep.timeline = Timeline(
            version="cut-v1",
            segments=(
                Segment(
                    served_start=0.0,
                    served_end=10.0,
                    kind="source",
                    source_id="s0",
                    source_start=10.0,
                    source_end=20.0,
                ),
            ),
        )

        _text, timed_segments = _provider_alignment_inputs(
            ep,
            b"[00:00:12]\nKEPT WORDS HERE.\n[00:00:15]\nMORE KEPT WORDS HERE.\n",
            "txt",
        )

        assert timed_segments == [
            {"start": 2.0, "end": 5.0, "text": "KEPT WORDS HERE."},
            {"start": 5.0, "end": 10.0, "text": "MORE KEPT WORDS HERE."},
        ]

    def _run_with_content(self, tmp_path, content: bytes, url="https://provider/t.vtt"):
        from unittest.mock import patch

        ep = _ep(links={"transcript": url})
        ep.timeline = None  # identity

        _body = content  # capture for closure

        class _FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

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

    def test_vtt_stored_as_provider_candidate(self, tmp_path):
        ep, stats = self._run_with_content(tmp_path, VTT_CONTENT)
        assert stats.ran == 1
        candidate = ep.provider_transcript["candidate"]
        assert candidate["key"].endswith(".vtt")
        assert candidate["key"] is not None
        assert candidate["synced"] is True
        assert candidate["format"] == "vtt"
        assert ep.transcript_key is None

    def test_provider_endpoint_is_probed_outside_materialization_cohort(self, tmp_path):
        first = _ep(uid="uid-first", links={"transcript": "https://provider/first.vtt"})
        deep = _ep(uid="uid-deep", links={"transcript": "https://provider/deep.vtt"})

        with (
            patch("citypods.http.make_session", return_value=_fetch(VTT_CONTENT)),
            patch("citypods.stages._materialize_set", return_value=[first]),
        ):
            TranscriptStage().process(FakeProvider(), _city(), [first, deep], _ctx(tmp_path))

        assert first.provider_transcript is not None
        assert deep.provider_transcript is not None
        assert deep.provider_transcript["candidate"]["format"] == "vtt"

    def test_provider_candidate_basis_remains_source_time(self, tmp_path):
        ep, _ = self._run_with_content(tmp_path, VTT_CONTENT)
        assert ep.provider_transcript["candidate"]["basis"] == "source:s0"

    def test_vtt_object_uploaded_to_storage(self, tmp_path):
        ep, _ = self._run_with_content(tmp_path, VTT_CONTENT)
        candidate = ep.provider_transcript["candidate"]
        assert candidate["hosted_url"] is not None
        assert candidate["hosted_url"].startswith("https://")
        assert candidate["key"] is not None
        # Object should exist on disk
        audio_dir = tmp_path / "audio"
        assert (audio_dir / candidate["key"]).exists()

    def test_srt_detected_as_synced(self, tmp_path):
        ep, stats = self._run_with_content(tmp_path, SRT_CONTENT)
        assert ep.provider_transcript["candidate"]["synced"] is True
        assert ep.provider_transcript["candidate"]["format"] == "srt"

    def test_plain_text_not_synced(self, tmp_path):
        ep, stats = self._run_with_content(tmp_path, PLAIN_CONTENT)
        assert ep.provider_transcript["candidate"]["synced"] is False
        assert ep.provider_transcript["candidate"]["format"] == "txt"

    def test_swagit_fallback_probe_stores_transcript_and_is_not_repeated(self, tmp_path):
        class SwagitProbeProvider(FakeProvider):
            def __init__(self):
                self.calls = 0

            def transcript_url(self, episode):
                return "https://swagit.example/videos/100/transcript"

            def probe_transcript(self, episode, source):
                self.calls += 1
                return TranscriptProbe(
                    url=self.transcript_url(episode), status_code=200, content=VTT_CONTENT
                )

        provider = SwagitProbeProvider()
        city = _city(
            provider="swagit",
            source={"list_url": "https://swagit.example/archive"},
            asr_enabled=False,
        )
        ep = _ep(links={"canonical_video": "https://swagit.example/videos/100"})

        TranscriptStage().process(provider, city, [ep], _ctx(tmp_path))
        assert provider.calls == 1
        assert ep.links["transcript"].endswith("/videos/100/transcript")
        assert ep.provider_transcript["candidate"]["format"] == "vtt"
        assert ep.provider_transcript["probe"]["status"] == "available"

        TranscriptStage().process(provider, city, [ep], _ctx(tmp_path))
        assert provider.calls == 1

    def test_swagit_missing_transcript_probe_uses_absent_backoff(self, tmp_path):
        class SwagitProbeProvider(FakeProvider):
            def __init__(self):
                self.calls = 0

            def transcript_url(self, episode):
                return "https://swagit.example/videos/100/transcript"

            def probe_transcript(self, episode, source):
                self.calls += 1
                return TranscriptProbe(
                    url=self.transcript_url(episode), status_code=404, content=b""
                )

        provider = SwagitProbeProvider()
        city = _city(
            provider="swagit",
            source={"list_url": "https://swagit.example/archive"},
            asr_enabled=False,
        )
        ep = _ep(links={"canonical_video": "https://swagit.example/videos/100"})

        TranscriptStage().process(provider, city, [ep], _ctx(tmp_path))
        assert provider.calls == 1
        assert "transcript" not in ep.links
        probe = ep.provider_transcript["probe"]
        assert probe["status"] == "absent"
        assert probe["attempts"] == 1
        next_retry = datetime.fromisoformat(probe["next_retry_at"])
        assert next_retry - datetime.fromisoformat(probe["checked_at"]) == timedelta(days=7)

        TranscriptStage().process(provider, city, [ep], _ctx(tmp_path))
        assert provider.calls == 1

    def test_provider_probe_due_uses_persisted_retry_time(self):
        ep = _ep()
        url = "https://swagit.example/videos/100/transcript"
        checked = datetime(2026, 6, 1, tzinfo=UTC)
        _record_provider_transcript_probe(
            ep, url=url, status="absent", now=checked, status_code=404
        )
        assert not _provider_transcript_probe_due(
            ep, url=url, now=checked + timedelta(days=6, hours=23)
        )
        assert _provider_transcript_probe_due(ep, url=url, now=checked + timedelta(days=7))

    def test_swagit_probe_cap_is_shared_across_episode_calls(self, tmp_path):
        class SwagitProbeProvider(FakeProvider):
            def __init__(self):
                self.calls = 0

            def transcript_url(self, episode):
                return f"https://swagit.example/videos/{episode.guid}/transcript"

            def probe_transcript(self, episode, source):
                self.calls += 1
                return TranscriptProbe(
                    url=self.transcript_url(episode), status_code=200, content=VTT_CONTENT
                )

        provider = SwagitProbeProvider()
        city = _city(
            provider="swagit",
            source={"list_url": "https://swagit.example/archive"},
            asr_enabled=False,
        )
        first = _ep(uid="uid-100", links={"canonical_video": "https://swagit.example/videos/100"})
        first.guid = "100"
        second = _ep(uid="uid-101", links={"canonical_video": "https://swagit.example/videos/101"})
        second.guid = "101"
        ctx = _ctx(tmp_path)
        ctx.provider_transcript_probes_per_source = 1

        TranscriptStage().process(provider, city, [first], ctx)
        TranscriptStage().process(provider, city, [second], ctx)
        assert provider.calls == 1
        assert "transcript" not in second.links

    def test_swagit_recheck_uses_probe_path_and_records_denial(self, tmp_path):
        class SwagitProbeProvider(FakeProvider):
            def __init__(self):
                self.calls = 0

            def transcript_url(self, episode):
                return "https://swagit.example/videos/100/transcript"

            def probe_transcript(self, episode, source):
                self.calls += 1
                if self.calls == 1:
                    return TranscriptProbe(
                        url=self.transcript_url(episode), status_code=200, content=VTT_CONTENT
                    )
                return TranscriptProbe(
                    url=self.transcript_url(episode), status_code=403, content=b""
                )

        provider = SwagitProbeProvider()
        city = _city(
            provider="swagit",
            source={"list_url": "https://swagit.example/archive"},
            asr_enabled=False,
        )
        ep = _ep(links={"canonical_video": "https://swagit.example/videos/100"})

        TranscriptStage().process(provider, city, [ep], _ctx(tmp_path))
        ep.provider_transcript["probe"]["next_retry_at"] = (
            datetime.now(UTC) - timedelta(seconds=1)
        ).isoformat()

        with patch(
            "citypods.http.make_session",
            side_effect=AssertionError("scheduled Swagit recheck must use probe_transcript"),
        ):
            TranscriptStage().process(provider, city, [ep], _ctx(tmp_path))

        assert provider.calls == 2
        probe = ep.provider_transcript["probe"]
        assert probe["status"] == "error"
        assert probe["status_code"] == 403
        assert datetime.fromisoformat(probe["next_retry_at"]) > datetime.now(UTC)

    def test_unchanged_refetch_refreshes_checked_at_without_new_candidate(self, tmp_path):
        ep, _ = self._run_with_content(tmp_path, VTT_CONTENT)
        first = dict(ep.provider_transcript["candidate"])

        with patch("citypods.http.make_session", return_value=_fetch(VTT_CONTENT)):
            stage = TranscriptStage()
            stats = stage.process(FakeProvider(), _city(), [ep], _ctx(tmp_path))
        second = ep.provider_transcript["candidate"]
        assert second["key"] == first["key"]
        assert second["spec_hash"] == first["spec_hash"]
        assert second["status"] == "unchanged"
        assert second["checked_at"] >= first["checked_at"]
        assert stats.reused == 1

    def test_unchanged_refetch_reuploads_when_provider_object_is_missing(self, tmp_path):
        ep, _ = self._run_with_content(tmp_path, VTT_CONTENT)
        key = ep.provider_transcript["candidate"]["key"]
        stored = tmp_path / "audio" / key
        stored.unlink()

        with patch("citypods.http.make_session", return_value=_fetch(VTT_CONTENT)):
            stats = TranscriptStage().process(FakeProvider(), _city(), [ep], _ctx(tmp_path))

        assert stats.ran == 1
        assert ep.provider_transcript["candidate"]["key"] == key
        assert stored.exists()

    def test_unchanged_refetch_matches_by_content_not_signed_url(self, tmp_path):
        ep, _ = self._run_with_content(tmp_path, VTT_CONTENT, url="https://provider/t.vtt?token=a")
        fresh = _ep(uid=ep.uid, links={"transcript": "https://provider/t.vtt?token=b"})
        fresh.provider_transcript = ep.provider_transcript

        with patch("citypods.http.make_session", return_value=_fetch(VTT_CONTENT)):
            stats = TranscriptStage().process(FakeProvider(), _city(), [fresh], _ctx(tmp_path))

        assert stats.reused == 1
        assert fresh.provider_transcript["candidate"]["url"].endswith("token=b")

    def test_unchanged_refetch_only_updates_matching_slot_when_url_is_shared(self, tmp_path):
        ep, _ = self._run_with_content(tmp_path, b"candidate bytes")
        candidate = dict(ep.provider_transcript["candidate"])
        known_good = dict(candidate)
        known_good["spec_hash"] = "known-good-spec"
        known_good["key"] = "transcripts/src/u-provider-known.txt"
        ep.provider_transcript = {
            "known_good": known_good,
            "candidate": candidate,
            "history": [],
        }

        with patch("citypods.http.make_session", return_value=_fetch(b"candidate bytes")):
            stats = TranscriptStage().process(FakeProvider(), _city(), [ep], _ctx(tmp_path))

        assert stats.reused == 1
        assert ep.provider_transcript["candidate"]["status"] == "unchanged"
        assert ep.provider_transcript["known_good"]["status"] == "candidate"

    def test_known_good_refetch_clears_stale_candidate_to_history(self, tmp_path):
        ep, _ = self._run_with_content(tmp_path, b"known good bytes")
        known_good = dict(ep.provider_transcript["candidate"])
        candidate = dict(known_good)
        candidate["spec_hash"] = "candidate-spec"
        candidate["key"] = "transcripts/src/u-provider-candidate.txt"
        ep.provider_transcript = {
            "known_good": known_good,
            "candidate": candidate,
            "history": [],
        }

        with patch("citypods.http.make_session", return_value=_fetch(b"known good bytes")):
            stats = TranscriptStage().process(FakeProvider(), _city(), [ep], _ctx(tmp_path))

        assert stats.reused == 1
        assert "candidate" not in ep.provider_transcript
        assert ep.provider_transcript["known_good"]["status"] == "unchanged"
        assert ep.provider_transcript["history"][0]["spec_hash"] == "candidate-spec"

    def test_changed_bytes_become_candidate_and_prior_candidate_moves_to_history(self, tmp_path):
        ep, _ = self._run_with_content(tmp_path, b"old transcript")
        first = dict(ep.provider_transcript["candidate"])

        with patch("citypods.http.make_session", return_value=_fetch(b"new transcript")):
            stats = TranscriptStage().process(FakeProvider(), _city(), [ep], _ctx(tmp_path))

        candidate = ep.provider_transcript["candidate"]
        assert stats.ran == 1
        assert candidate["spec_hash"] != first["spec_hash"]
        assert ep.provider_transcript["history"][0]["spec_hash"] == first["spec_hash"]
        assert ep.transcript_key is None

    def test_changed_candidate_does_not_copy_known_good_to_history(self, tmp_path):
        ep, _ = self._run_with_content(tmp_path, b"old candidate")
        first = dict(ep.provider_transcript["candidate"])
        ep.provider_transcript["known_good"] = {
            **first,
            "spec_hash": "known-good-spec",
            "key": "transcripts/src/u-provider-known.txt",
        }

        with patch("citypods.http.make_session", return_value=_fetch(b"new candidate")):
            TranscriptStage().process(FakeProvider(), _city(), [ep], _ctx(tmp_path))

        history_hashes = [item["spec_hash"] for item in ep.provider_transcript["history"]]
        assert history_hashes == [first["spec_hash"]]

    def test_active_asr_transcript_does_not_block_provider_source_backfill(self, tmp_path):
        ep = _ep(links={"transcript": "https://provider/t.vtt"})
        src_key = source_key(_city())
        ep.transcript_key = f"transcripts/{src_key}/{ep.uid}-asr-current.vtt"
        ep.transcript_hosted_url = None
        ep.transcript_synced = True
        ep.transcript_format = "vtt"
        ep.transcript_pipeline_version = ASR_PIPELINE_VERSION
        storage_root = tmp_path / "audio"
        (storage_root / ep.transcript_key).parent.mkdir(parents=True, exist_ok=True)
        (storage_root / ep.transcript_key).write_bytes(ASR_VTT)

        with patch("citypods.http.make_session", return_value=_fetch(VTT_CONTENT)):
            stats = TranscriptStage().process(FakeProvider(), _city(), [ep], _ctx(tmp_path))

        assert stats.ran == 1
        assert stats.reused == 1
        assert ep.transcript_key.endswith("-asr-current.vtt")
        assert ep.provider_transcript["candidate"]["format"] == "vtt"

    def test_provider_fetch_validates_url_with_dns_resolution(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "citypods.stages.validate_source_url",
            lambda url, **kwargs: calls.append((url, kwargs)),
        )

        self._run_with_content(tmp_path, VTT_CONTENT, url="https://provider/t.vtt")

        assert calls == [("https://provider/t.vtt", {"resolve": True})]

    def test_hosted_word_timed_provider_candidate_is_served_natively(self, tmp_path):
        ep = _ep(links={"transcript": "https://provider/t.vtt"})
        ep.audio_key = "audio/src/uid-g1-a.m4a"
        ep.hosted_audio_url = "https://cdn/audio/src/uid-g1-a.m4a"

        with patch("citypods.http.make_session", return_value=_fetch(WORD_VTT_CONTENT)):
            stats = TranscriptStage().process(
                FakeProvider(), _city(asr_enabled=False), [ep], _ctx(tmp_path)
            )

        assert stats.ran == 2  # provider-source fetch + provider-native sidecar publish
        assert stats.aligned == 0
        assert ep.transcript_key.endswith(
            "-provider-" + ep.provider_transcript["known_good"]["spec_hash"] + ".vtt"
        )
        assert ep.transcript_synced is True
        assert ep.transcript_basis == "served"
        assert ep.transcript_pipeline_version == "provider-native:1"
        assert ep.transcript_text_source == "provider"
        assert ep.transcript_timing_source == "provider"
        assert ep.transcript_selection == "provider-native"
        assert "candidate" not in ep.provider_transcript
        known_good = ep.provider_transcript["known_good"]
        assert known_good["word_timed"] is True
        assert known_good["words_key"]

    def test_provider_align_remaps_source_time_through_timeline(self, tmp_path):
        ep = _ep(links={"transcript": "https://provider/t.vtt"})
        ep.audio_key = "audio/src/uid-g1-a.m4a"
        ep.hosted_audio_url = "https://cdn/audio/src/uid-g1-a.m4a"
        ep.sources = [
            SourceMedia(
                id="s0",
                provider="test",
                ref="https://src/vid.mp4",
                media_kind="direct",
                duration=30.0,
                watch_url=None,
            )
        ]
        ep.timeline = Timeline(
            version="cut-v1",
            segments=(
                Segment(
                    served_start=0.0,
                    served_end=10.0,
                    kind="source",
                    source_id="s0",
                    source_start=10.0,
                    source_end=20.0,
                ),
            ),
        )
        content = b"WEBVTT\n\n00:00:12.000 --> 00:00:15.000\nKept cue\n"

        with patch("citypods.http.make_session", return_value=_fetch(content)):
            TranscriptStage().process(
                FakeProvider(), _city(asr_enabled=False), [ep], _ctx(tmp_path)
            )

        assert ep.transcript_key is None
        assert ep.provider_transcript["candidate"]["format"] == "vtt"

    def test_word_timed_provider_vtt_on_edited_timeline_uses_provider_alignment(self, tmp_path):
        ep = _ep_with_audio(links={"transcript": "https://provider/t.vtt"})
        ep.sources = [
            SourceMedia(
                id="s0",
                provider="test",
                ref="https://src/vid.mp4",
                media_kind="direct",
                duration=30.0,
                watch_url=None,
            )
        ]
        ep.timeline = Timeline(
            version="cut-v1",
            segments=(
                Segment(
                    served_start=0.0,
                    served_end=10.0,
                    kind="source",
                    source_id="s0",
                    source_start=10.0,
                    source_end=20.0,
                ),
            ),
        )
        fake_asr = _FakeAsr()
        edited_word_vtt = (
            b"WEBVTT\n\n00:00:12.000 --> 00:00:15.000\n<00:00:12.000>Hello <00:00:13.000>world\n"
        )
        with (
            patch("citypods.stages.asr_mod", fake_asr),
            patch("citypods.stages._download_audio_file", side_effect=_fake_audio_download),
            patch("citypods.http.make_session", return_value=_fetch(edited_word_vtt)),
        ):
            stats = TranscriptStage().process(FakeProvider(), _city(), [ep], _ctx(tmp_path))

        assert stats.aligned == 1
        assert stats.transcribed == 0
        assert ep.transcript_selection == "provider-aligned"
        assert len(fake_asr.align_calls) == 1
        assert fake_asr.align_calls[0]["text"] == "Hello world"
        # The WhisperX path receives served-time sections; this legacy fake only observes the
        # compatibility text projection, not the section payload passed to LocalBackend.
        assert "timed_segments" not in fake_asr.align_calls[0]

    def test_worse_provider_candidate_moves_to_history_and_keeps_known_good(self, tmp_path):
        ep = _ep(links={"transcript": "https://provider/t.vtt"})
        ep.audio_key = "audio/src/uid-g1-a.m4a"
        ep.hosted_audio_url = "https://cdn/audio/src/uid-g1-a.m4a"
        ep.sources = [
            SourceMedia(
                id="s0",
                provider="test",
                ref="https://src/vid.mp4",
                media_kind="direct",
                duration=30.0,
                watch_url=None,
            )
        ]
        ep.timeline = Timeline(
            version="cut-v1",
            segments=(
                Segment(
                    served_start=0.0,
                    served_end=5.0,
                    kind="source",
                    source_id="s0",
                    source_start=5.0,
                    source_end=10.0,
                ),
            ),
        )
        known_content = b"WEBVTT\n\n00:00:06.000 --> 00:00:08.000\nKnown\n"
        ctx = _ctx(tmp_path)
        with patch("citypods.http.make_session", return_value=_fetch(known_content)):
            TranscriptStage().process(FakeProvider(), _city(asr_enabled=False), [ep], ctx)
        known_spec = ep.provider_transcript["candidate"]["spec_hash"]

        worse_content = (
            b"WEBVTT\n\n00:00:06.000 --> 00:00:07.000\nKept\n\n00:00:20.000 --> 00:00:22.000\nCut\n"
        )
        with patch("citypods.http.make_session", return_value=_fetch(worse_content)):
            TranscriptStage().process(FakeProvider(), _city(asr_enabled=False), [ep], ctx)

        assert ep.provider_transcript["candidate"]["spec_hash"] != known_spec
        assert ep.transcript_key is None
        assert ep.provider_transcript["history"][0]["spec_hash"] == known_spec


def _provider_aligned_ep(tmp_path: Path, content: bytes) -> Episode:
    ep = _ep(links={"transcript": "https://provider/t.vtt"})
    ep.audio_key = "audio/src/uid-g1-a.m4a"
    ep.hosted_audio_url = "https://cdn/audio/src/uid-g1-a.m4a"
    ep.transcript_key = "transcripts/t-tx/uid-g1-provider-align-align123.vtt"
    ep.transcript_hosted_url = f"https://cdn/{ep.transcript_key}"
    ep.transcript_spec_hash = "align123"
    ep.transcript_format = "vtt"
    ep.transcript_basis = "served"
    ep.transcript_synced = True
    ep.transcript_pipeline_version = "provider-align:1"
    ep.provider_transcript = {
        "known_good": {
            "key": "transcripts/t-tx/uid-g1-provider-source.vtt",
            "url": "https://cdn/transcripts/t-tx/uid-g1-provider-source.vtt",
            "spec_hash": "source123",
            "format": "vtt",
            "basis": "source:s0",
            "synced": True,
            "confidence": 1.0,
            "align_spec_hash": "align123",
        }
    }
    target = tmp_path / "audio" / ep.transcript_key
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return ep


class TestProviderTranscriptDiarizeStage:
    def test_provider_aligned_transcript_writes_speaker_turns(self, tmp_path):
        content = (
            b"WEBVTT\n\n"
            b"00:00:01.000 --> 00:00:04.000\nMAYOR: We are in session.\n\n"
            b"00:00:04.000 --> 00:00:06.000\nCOUNCIL MEMBER: Thank you.\n"
        )
        ep = _provider_aligned_ep(tmp_path, content)

        stats = ProviderTranscriptDiarizeStage().process(
            FakeProvider(), _city(asr_enabled=False), [ep], _ctx(tmp_path)
        )

        assert stats.ran == 1
        assert ep.transcript_key == "transcripts/t-tx/uid-g1-provider-align-align123.vtt"
        assert ep.speakers_key.endswith(".speakers.json")
        assert ep.speakers_synced is True
        assert ep.speakers_confidence == 1.0
        assert ep.provider_transcript["known_good"]["diarize_status"] == "known_good"
        assert ep.provider_transcript["known_good"]["diarize_spec_hash"] == ep.speakers_spec_hash

        stored = json.loads((tmp_path / "audio" / ep.speakers_key).read_text())
        assert stored["basis"] == "served"
        assert stored["source"] == "provider-transcript"
        assert stored["turns"] == [
            {"end": 4.0, "speaker": "MAYOR", "start": 1.0, "text": "We are in session."},
            {"end": 6.0, "speaker": "COUNCIL MEMBER", "start": 4.0, "text": "Thank you."},
        ]

    def test_diarize_failure_keeps_provider_aligned_transcript(self, tmp_path):
        content = b"WEBVTT\n\n00:00:01.000 --> 00:00:04.000\nNo speaker labels here.\n"
        ep = _provider_aligned_ep(tmp_path, content)
        original_key = ep.transcript_key

        stats = ProviderTranscriptDiarizeStage().process(
            FakeProvider(), _city(asr_enabled=False), [ep], _ctx(tmp_path)
        )

        assert stats.defer_reasons == {"no-speaker-labels": 1}
        assert ep.transcript_key == original_key
        assert ep.transcript_synced is True
        assert ep.speakers_key is None
        assert ep.speakers_synced is False
        assert ep.speakers_error == "no-speaker-labels"
        assert ep.provider_transcript["known_good"]["diarize_status"] == "no-speaker-labels"


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
            "chapters",
            "timeline",
            "remap",
            "audio",
            "transcript",
            "links",
            "agenda_text",
            "minutes_text",
            "diarize",
            "tags",
        ]

    def test_full_order_enrich(self):
        assert self._names(enrich_stages()) == [
            "chapters",
            "timeline",
            "remap",
            "audio",
            "transcript",
            "links",
            "agenda_text",
            "minutes_text",
            "diarize",
            "tags",
        ]


# ---------------------------------------------------------------------------
# stop convention, content-addressing, GC protection (review items #16, #17, A)
# ---------------------------------------------------------------------------


def _fetch(content: bytes):
    """A make_session() replacement that returns `content` from a GET."""

    class _R:
        status_code = 200

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, *a, **kw):
            r = _R()
            r.content = content
            return r

    return _Session()


class _NoFetch:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def get(self, *a, **kw):
        raise AssertionError("must not fetch")


class TestTranscriptStopAndStorage:
    def _store_once(self, tmp_path):
        ep = _ep(links={"transcript": "https://provider/t.vtt"})
        ep.timeline = None
        with patch("citypods.http.make_session", return_value=_fetch(VTT_CONTENT)):
            TranscriptStage().process(FakeProvider(), _city(), [ep], _ctx(tmp_path))
        return ep

    def test_reuse_runs_even_when_stopped(self, tmp_path):
        # #16: active transcript reuse is cheap idempotent bookkeeping → must run even after
        # stop(), so a yielded run still references the already-stored transcript (mirrors
        # AudioStage). Provider-source fetching itself is expensive and remains stop-gated below.
        ep = self._store_once(tmp_path)
        ep.transcript_key = f"transcripts/{source_key(_city())}/uid-g1-asr-abc.vtt"
        ep.transcript_hosted_url = None  # a fresh run must re-attach the URL
        ep.transcript_synced = True
        ep.transcript_pipeline_version = ASR_PIPELINE_VERSION
        ctx = _ctx(tmp_path)
        dest = tmp_path / "audio" / ep.transcript_key
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(VTT_CONTENT)
        ctx.stop = lambda: True
        with patch("citypods.http.make_session", return_value=_NoFetch()):
            stats = TranscriptStage().process(FakeProvider(), _city(), [ep], ctx)
        assert stats.reused == 1 and stats.skipped == 0
        assert ep.transcript_hosted_url is not None  # re-attached despite the stop

    def test_fetch_deferred_when_stopped(self, tmp_path):
        # The expensive fetch+store IS gated: an un-stored transcript defers under stop().
        ep = _ep(links={"transcript": "https://provider/t.vtt"})
        ep.timeline = None
        ctx = _ctx(tmp_path)
        ctx.stop = lambda: True
        with patch("citypods.http.make_session", return_value=_NoFetch()):
            stats = TranscriptStage().process(FakeProvider(), _city(), [ep], ctx)
        assert stats.skipped == 1
        assert ep.transcript_key is None
        assert ep.provider_transcript == {}

    def test_spec_is_content_addressed_not_url(self, tmp_path):
        # #17: identical content behind different (tokenized) URLs → same spec/key.
        ep_a = _ep(uid="uid-x", links={"transcript": "https://p/a.vtt?token=AAA"})
        ep_b = _ep(uid="uid-x", links={"transcript": "https://p/b.vtt?token=ZZZ"})
        ep_a.timeline = ep_b.timeline = None
        with patch("citypods.http.make_session", return_value=_fetch(VTT_CONTENT)):
            TranscriptStage().process(FakeProvider(), _city(), [ep_a], _ctx(tmp_path))
            TranscriptStage().process(FakeProvider(), _city(), [ep_b], _ctx(tmp_path))
        assert (
            ep_a.provider_transcript["candidate"]["spec_hash"]
            == ep_b.provider_transcript["candidate"]["spec_hash"]
        )
        assert (
            ep_a.provider_transcript["candidate"]["key"]
            == ep_b.provider_transcript["candidate"]["key"]
        )

    def test_referenced_keys_protect_transcripts_from_gc(self, tmp_path):
        # Fix A: the live set the orphan GC keeps must include transcript keys, or
        # gc_audio.py --apply would reap hosted transcripts.
        ep = self._store_once(tmp_path)
        state_dir = tmp_path / "state"
        save_records(state_dir, source_key(_city()), {ep.uid: episode_to_record(ep)})
        refs = referenced_audio_keys(state_dir)
        assert ep.provider_transcript["candidate"]["key"] in refs

    def test_referenced_keys_protect_word_sidecar_from_gc(self, tmp_path):
        # H12: the word-JSON sidecar must also be in the orphan-GC live set.
        ep = _ep()
        ep.transcript_key = "transcripts/src/uid-g1-asr-abc.vtt"
        ep.transcript_words_key = "transcripts/src/uid-g1-asr-abc.words.json"
        ep.transcript_synced = True
        state_dir = tmp_path / "state"
        save_records(state_dir, source_key(_city()), {ep.uid: episode_to_record(ep)})
        refs = referenced_audio_keys(state_dir)
        assert ep.transcript_key in refs
        assert ep.transcript_words_key in refs

    def test_referenced_keys_protect_provider_transcript_registry_from_gc(self, tmp_path):
        ep = _ep()
        ep.provider_transcript = {
            "known_good": {"key": "transcripts/src/uid-g1-provider-old.pdf"},
            "candidate": {"key": "transcripts/src/uid-g1-provider-new.pdf"},
            "history": [{"key": "transcripts/src/uid-g1-provider-history.pdf"}],
        }
        state_dir = tmp_path / "state"
        save_records(state_dir, source_key(_city()), {ep.uid: episode_to_record(ep)})
        refs = referenced_audio_keys(state_dir)
        assert refs >= {
            "transcripts/src/uid-g1-provider-old.pdf",
            "transcripts/src/uid-g1-provider-new.pdf",
            "transcripts/src/uid-g1-provider-history.pdf",
        }


# ---------------------------------------------------------------------------
# ASR slot (issue #110)
# ---------------------------------------------------------------------------


def _ep_with_audio(uid="uid-asr", links=None) -> Episode:
    """Episode with hosted audio (minimal fields needed for the ASR slot)."""
    ep = _ep(uid=uid, links=links or {})
    ep.audio_key = "audio/src/uid-asr-abc.m4a"
    ep.audio_spec_hash = "deadbeef0000"
    ep.hosted_audio_url = "https://cdn/audio/uid-asr.m4a"
    return ep


ASR_VTT = b"WEBVTT\n\n00:00:00.000 --> 00:00:05.000\nCouncil meeting called to order.\n"
ASR_WORDS = b'{"schema":"1","basis":"served","segments":[{"words":[{"w":"Council","s":0,"e":1}]}]}'


class _FakeAsr:
    """Replaces citypods.asr in TranscriptStage for tests."""

    def __init__(
        self,
        vtt: bytes = ASR_VTT,
        words: bytes = ASR_WORDS,
        *,
        fail: bool = False,
        fail_load: bool = False,
    ):
        self.vtt = vtt
        self.words = words
        self.fail = fail
        self.fail_load = fail_load
        self.transcribe_calls: list[dict] = []
        self.align_calls: list[dict] = []

    # Sentinel returned by load_model() so tests can verify it is passed through.
    _FAKE_MODEL = object()

    def load_model(self, model, compute_type, cpu_threads):
        if self.fail_load:
            raise RuntimeError("model load failed")
        return self._FAKE_MODEL

    def transcribe(
        self, audio_path, model_or_name, language, compute_type, beam_size, prompt, cpu_threads
    ):
        if self.fail:
            raise RuntimeError("transcribe failed")
        self.transcribe_calls.append({"model": model_or_name, "prompt": prompt})
        return TranscriptArtifacts(vtt=self.vtt, words=self.words)

    def align(
        self,
        audio_path,
        text,
        model_or_name,
        language,
        cpu_threads,
        compute_type="int8",
        **kwargs,
    ):
        if self.fail:
            raise RuntimeError("align failed")
        self.align_calls.append(
            {
                "text": text,
                "model": model_or_name,
                "compute_type": compute_type,
                **kwargs,
            }
        )
        return TranscriptArtifacts(vtt=self.vtt, words=self.words)

    def asr_spec_hash(self, audio_spec_hash, model, align_hash, version, **kwargs):
        # Use the real implementation
        return asr_spec_hash(audio_spec_hash, model, align_hash, version, **kwargs)


class _FakeDispatchBackend:
    name = "modal"

    def __init__(self):
        self.submitted: list[InferenceJob] = []

    def estimate_gpu_seconds(self, job):
        return 60.0

    def run_inference(self, job):
        self.submitted.append(job)
        return JobHandle(
            task=job.task,
            recipe_hash=job.recipe_hash,
            backend=self.name,
            ref=f"job-{len(self.submitted)}",
        )


def _dispatcher(tmp_path, fake_asr, external, *, monthly_gpu_seconds=10_000):
    return DispatchCoordinator(
        local=LocalBackend(fake_asr),
        targets=[
            DispatchTarget(
                backend=external,
                monthly_gpu_seconds=monthly_gpu_seconds,
                max_inflight=8,
            )
        ],
        budget=Budget(),
        state_dir=tmp_path,
    )


def _asr_ctx(tmp_path: Path, fake_asr: _FakeAsr) -> StageContext:
    """StageContext with a patched asr module and no stop signal."""
    ctx = _ctx(tmp_path)

    class _FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url, **kw):
            r = type("R", (), {"status_code": 200, "content": b"fake audio bytes"})()
            r.iter_content = lambda chunk_size=8192: iter([b"fake audio"])
            r.raise_for_status = lambda: None
            return r

    # Patch both the asr module and HTTP session
    ctx._fake_asr = fake_asr
    ctx._fake_session = _FakeSession()
    return ctx


def _fake_audio_download(_url, dest):
    Path(dest).write_bytes(b"fake audio")


def test_download_audio_file_retries_chunked_encoding_error(tmp_path):
    import requests

    import citypods.stages as stages_mod

    attempts = {"n": 0}

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise requests.exceptions.ChunkedEncodingError("Connection broken: IncompleteRead")
            yield b"fake audio bytes"

    class _FakeSession:
        headers: dict = {}

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def get(self, *args, **kwargs):
            return _FakeResponse()

    sleeps: list[float] = []
    dest = tmp_path / "audio.m4a"
    with patch("requests.Session", _FakeSession):
        stages_mod._download_audio_file("https://example.com/audio.m4a", dest, sleep=sleeps.append)

    assert attempts["n"] == 3
    assert dest.read_bytes() == b"fake audio bytes"
    assert sleeps == [2.0, 4.0]


def test_download_audio_file_raises_after_exhausting_retries(tmp_path):
    import requests

    import citypods.stages as stages_mod

    attempts = {"n": 0}

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            attempts["n"] += 1
            raise requests.exceptions.ChunkedEncodingError("Connection broken: IncompleteRead")

    class _FakeSession:
        headers: dict = {}

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def get(self, *args, **kwargs):
            return _FakeResponse()

    dest = tmp_path / "audio.m4a"
    with (
        patch("requests.Session", _FakeSession),
        pytest.raises(requests.exceptions.ChunkedEncodingError),
    ):
        stages_mod._download_audio_file(
            "https://example.com/audio.m4a", dest, max_attempts=3, sleep=lambda _s: None
        )

    assert attempts["n"] == 3


def test_download_audio_file_retries_connection_error(tmp_path):
    import requests

    import citypods.stages as stages_mod

    attempts = {"n": 0}

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise requests.exceptions.ConnectionError("Connection reset by peer")
            yield b"fake audio bytes"

    class _FakeSession:
        headers: dict = {}

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def get(self, *args, **kwargs):
            return _FakeResponse()

    dest = tmp_path / "audio.m4a"
    with patch("requests.Session", _FakeSession):
        stages_mod._download_audio_file(
            "https://example.com/audio.m4a", dest, sleep=lambda _s: None
        )

    assert attempts["n"] == 2
    assert dest.read_bytes() == b"fake audio bytes"


def test_download_audio_file_retries_response_timeout(tmp_path):
    import requests

    import citypods.stages as stages_mod

    attempts = {"n": 0}

    class _FakeSession:
        headers: dict = {}

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def get(self, *args, **kwargs):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise requests.exceptions.ReadTimeout("response timed out")

            class _Response:
                def raise_for_status(self):
                    pass

                def iter_content(self, chunk_size):
                    yield b"fake audio bytes"

            return _Response()

    dest = tmp_path / "audio.m4a"
    with patch("requests.Session", _FakeSession):
        stages_mod._download_audio_file(
            "https://example.com/audio.m4a", dest, sleep=lambda _s: None
        )

    assert attempts["n"] == 2
    assert dest.read_bytes() == b"fake audio bytes"


def test_download_audio_file_retries_http_429_and_honors_capped_retry_after(tmp_path):
    import requests

    import citypods.stages as stages_mod

    attempts = {"n": 0}

    class _FakeResponse:
        status_code = 429
        headers = {"Retry-After": "300"}

        def raise_for_status(self):
            raise requests.exceptions.HTTPError(response=self)

        def close(self):
            pass

        def iter_content(self, chunk_size):
            yield b"fake audio bytes"

    class _FakeSession:
        headers: dict = {}

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def get(self, *args, **kwargs):
            attempts["n"] += 1
            if attempts["n"] < 3:
                return _FakeResponse()
            response = _FakeResponse()
            response.status_code = 200
            response.headers = {}
            response.raise_for_status = lambda: None
            return response

    sleeps: list[float] = []
    dest = tmp_path / "audio.m4a"
    with patch("requests.Session", _FakeSession):
        stages_mod._download_audio_file("https://example.com/audio.m4a", dest, sleep=sleeps.append)

    assert attempts["n"] == 3
    assert dest.read_bytes() == b"fake audio bytes"
    assert sleeps == [120, 120]


def test_download_audio_file_raises_http_429_after_retries(tmp_path):
    import requests

    import citypods.stages as stages_mod

    class _FakeResponse:
        status_code = 429
        headers = {"Retry-After": "1"}

        def raise_for_status(self):
            raise requests.exceptions.HTTPError(response=self)

        def close(self):
            pass

    class _FakeSession:
        headers: dict = {}

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def get(self, *args, **kwargs):
            return _FakeResponse()

    dest = tmp_path / "audio.m4a"
    with (
        patch("requests.Session", _FakeSession),
        pytest.raises(requests.exceptions.HTTPError),
    ):
        stages_mod._download_audio_file(
            "https://example.com/audio.m4a", dest, max_attempts=3, sleep=lambda _s: None
        )


def test_download_audio_file_validates_manual_redirects(tmp_path):
    import requests

    import citypods.stages as stages_mod

    class _FakeResponse:
        def __init__(self, status_code, headers=None):
            self.status_code = status_code
            self.headers = headers or {}
            self.closed = False

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.exceptions.HTTPError(response=self)

        def iter_content(self, chunk_size):
            yield b"fake audio bytes"

        def close(self):
            self.closed = True

    class _FakeSession:
        headers: dict = {}

        def __init__(self):
            self.calls = []
            self.responses = iter(
                [
                    _FakeResponse(302, {"Location": "/audio.m4a"}),
                    _FakeResponse(200),
                ]
            )

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return next(self.responses)

    sessions = []

    def _session():
        session = _FakeSession()
        sessions.append(session)
        return session

    dest = tmp_path / "audio.m4a"
    with (
        patch("requests.Session", side_effect=_session),
        patch("citypods.stages.validate_source_url") as validate,
    ):
        stages_mod._download_audio_file("https://example.com/download", dest)

    assert dest.read_bytes() == b"fake audio bytes"
    assert [url for url, _kwargs in sessions[0].calls] == [
        "https://example.com/download",
        "https://example.com/audio.m4a",
    ]
    assert all(kwargs["allow_redirects"] is False for _url, kwargs in sessions[0].calls)
    assert [args[0] for args, _kwargs in validate.call_args_list] == [
        "https://example.com/download",
        "https://example.com/audio.m4a",
    ]


def test_download_audio_file_rejects_unsafe_redirect(tmp_path):
    import citypods.stages as stages_mod
    from citypods.security import SecurityError

    class _FakeResponse:
        status_code = 302
        headers = {"Location": "http://127.0.0.1/audio.m4a"}

        def close(self):
            pass

    class _FakeSession:
        headers: dict = {}

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def get(self, *args, **kwargs):
            return _FakeResponse()

    def _validate(url, *, resolve):
        if url.startswith("http://127.0.0.1"):
            raise SecurityError("blocked redirect")

    dest = tmp_path / "audio.m4a"
    with (
        patch("requests.Session", _FakeSession),
        patch("citypods.stages.validate_source_url", side_effect=_validate),
        pytest.raises(SecurityError),
    ):
        stages_mod._download_audio_file("https://example.com/download", dest)


def test_download_audio_file_enforces_redirect_limit(tmp_path):
    import requests

    import citypods.stages as stages_mod
    from citypods.security import MAX_REDIRECTS

    class _FakeResponse:
        status_code = 302
        headers = {"Location": "/next"}

        def close(self):
            pass

    class _FakeSession:
        headers: dict = {}

        def __init__(self):
            self.calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def get(self, *args, **kwargs):
            self.calls += 1
            return _FakeResponse()

    sessions = []

    def _session():
        session = _FakeSession()
        sessions.append(session)
        return session

    dest = tmp_path / "audio.m4a"
    with (
        patch("requests.Session", side_effect=_session),
        patch("citypods.stages.validate_source_url"),
        pytest.raises(requests.exceptions.TooManyRedirects),
    ):
        stages_mod._download_audio_file("https://example.com/download", dest)

    assert sessions[0].calls == MAX_REDIRECTS + 1


def test_download_audio_file_exhausts_default_attempt_limit_with_backoff_schedule(tmp_path):
    import requests

    import citypods.stages as stages_mod

    attempts = {"n": 0}

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            attempts["n"] += 1
            raise requests.exceptions.ConnectionError("Connection reset by peer")

    class _FakeSession:
        headers: dict = {}

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def get(self, *args, **kwargs):
            return _FakeResponse()

    sleeps: list[float] = []
    dest = tmp_path / "audio.m4a"
    with (
        patch("requests.Session", _FakeSession),
        pytest.raises(requests.exceptions.ConnectionError),
    ):
        # No max_attempts override: this pins the production default (4 attempts) and its
        # backoff schedule so a regression that quietly shrinks either passes unnoticed.
        stages_mod._download_audio_file("https://example.com/audio.m4a", dest, sleep=sleeps.append)

    assert attempts["n"] == 4
    assert sleeps == [2.0, 4.0, 8.0]


def test_download_audio_file_aborts_when_stream_exceeds_size_cap(tmp_path):
    import citypods.stages as stages_mod

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            # One chunk already over the cap: the loop must abort before writing more.
            yield b"x" * (stages_mod._MAX_HOSTED_AUDIO_BYTES + 1)

    class _FakeSession:
        headers: dict = {}

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def get(self, *args, **kwargs):
            return _FakeResponse()

    dest = tmp_path / "audio.m4a"
    with (
        patch("requests.Session", _FakeSession),
        pytest.raises(stages_mod.HostedAudioTooLargeError),
    ):
        # Oversized streams are not transient — assert this doesn't burn through retries.
        stages_mod._download_audio_file(
            "https://example.com/audio.m4a",
            dest,
            sleep=lambda _s: pytest.fail("size cap breach must not be retried"),
        )


def _fresh_recipe(ep: Episode, city: City, version: str = ASR_PIPELINE_VERSION) -> str:
    return asr_spec_hash(
        transcript_media_hash(ep),
        city.asr_model,
        None,
        version,
        language=city.asr_language or None,
        compute_type=city.asr_compute_type,
        beam_size=city.asr_beam_size,
        initial_prompt=asr_initial_prompt(city.podcast_author, ep.body, ep.title),
    )


def _run_asr(tmp_path, ep, fake_asr=None):
    """Run TranscriptStage with the fake ASR module and a fake HTTP session."""
    if fake_asr is None:
        fake_asr = _FakeAsr()

    class _FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url, **kw):
            class _R:
                status_code = 200
                content = b"fake audio bytes"

                def iter_content(self, chunk_size=8192):
                    return iter([b"fake"])

                def raise_for_status(self):
                    pass

            return _R()

    with (
        patch("citypods.stages.asr_mod", fake_asr),
        patch("citypods.stages._download_audio_file", side_effect=_fake_audio_download),
    ):
        stage = TranscriptStage()
        ctx = _ctx(tmp_path)
        stats = stage.process(FakeProvider(), _city(), [ep], ctx)

    return ep, stats, fake_asr


class TestTranscriptStageASR:
    def test_path_b_fresh_transcription_no_source_text(self, tmp_path):
        """No provider transcript → Path B (fresh transcription) → synced=True."""
        ep = _ep_with_audio()
        ep, stats, fake_asr = _run_asr(tmp_path, ep)

        assert ep.transcript_synced is True
        assert ep.transcript_basis == "served"
        assert ep.transcript_format == "vtt"
        assert ep.transcript_key is not None
        assert ep.transcript_key.endswith(".vtt")
        assert "-asr-" in ep.transcript_key
        assert ep.transcript_selection == "asr"
        assert stats.ran == 1
        assert stats.transcribed == 1
        assert stats.aligned == 0

    def test_asr_skips_episode_with_materialize_error(self, tmp_path, capsys):
        """An episode flagged with an audio materialization error must not be transcribed: it's a
        wasted serial ASR slot on possibly-broken audio. It is left for the audio lane to re-encode
        (run #25)."""
        ep = _ep_with_audio()
        ep.materialize_error = "error"

        ep, stats, fake_asr = _run_asr(tmp_path, ep)

        assert fake_asr.transcribe_calls == []  # never reached inference
        assert ep.transcript_synced is False
        assert stats.ran == 0
        out = capsys.readouterr().out
        assert "reason=audio-error" in out

    def test_asr_completion_records_l1_quality_sample(self, tmp_path):
        """H15 Layer 1: every successful align()/transcribe() call appends a near-zero-cost
        coverage + word-logprob sample to the capped raw log, independent of whether this
        source/body has any H15 review data yet."""
        from citypods.records import source_key as _src_key
        from citypods.transcript_quality import load_raw_log

        ep = _ep_with_audio()
        ep.body = "City Council"
        fake_asr = _FakeAsr(words=ASR_WORDS)
        ep, stats, fake_asr = _run_asr(tmp_path, ep, fake_asr)

        assert stats.transcribed == 1
        log = load_raw_log(tmp_path / "state")
        l1_events = [e for e in log["events"] if e.get("kind") == "l1_sample"]
        assert len(l1_events) == 1
        event = l1_events[0]
        assert event["source_key"] == _src_key(_city())
        assert event["body_key"] == "city-council"
        assert event["method"] == "transcribe"
        assert event["model_id"] == _city().asr_model

    def test_path_b_initial_prompt_contains_title(self, tmp_path):
        """Path B: initial_prompt includes stable author/body/title context."""
        ep = _ep_with_audio()
        ep, stats, fake_asr = _run_asr(tmp_path, ep)

        assert len(fake_asr.transcribe_calls) == 1
        prompt = fake_asr.transcribe_calls[0]["prompt"]
        assert _city().podcast_author in prompt
        assert "Meeting" in prompt  # ep.title

    def test_asr_log_reports_unknown_duration_without_rounding_to_zero(self, tmp_path, capsys):
        ep = _ep_with_audio()
        ep.duration = None
        ep.audio_duration_served = None

        _run_asr(tmp_path, ep)

        out = capsys.readouterr().out
        assert "duration_h=unknown" in out
        assert "duration_source=unknown" in out
        assert "provider=civicplus" in out
        assert "guid=g1" in out
        assert "duration_h=0.0" not in out

    def test_asr_probe_corrects_existing_served_duration(self, tmp_path, capsys):
        ep = _ep_with_audio()
        ep.duration = 7200
        ep.audio_duration_served = 7200

        with patch("citypods.stages._probe_served_duration_secs", return_value=1800.0):
            _run_asr(tmp_path, ep)

        out = capsys.readouterr().out
        assert ep.audio_duration_served == pytest.approx(1800.0)
        assert "duration_h=0.5" in out
        assert "duration_source=hosted" in out

    def test_asr_probe_corrects_edited_timeline_served_duration(self, tmp_path, capsys):
        """review/20: ASR probes the real hosted file for edited timelines too, so the served clock
        reflects the actual object (1800s) rather than being pinned to the EDL sum (10s)."""
        ep = _ep_with_audio()
        ep.duration = 7200
        ep.timeline = Timeline(
            version="cut-v1",
            segments=(
                Segment(
                    served_start=0.0,
                    served_end=10.0,
                    kind="source",
                    source_id="s0",
                    source_start=30.0,
                    source_end=40.0,
                ),
            ),
        )
        ep.audio_duration_served = 7200.0

        with patch("citypods.stages._probe_served_duration_secs", return_value=1800.0):
            _run_asr(tmp_path, ep)

        out = capsys.readouterr().out
        assert ep.audio_duration_served == pytest.approx(1800.0)
        assert "duration_h=0.5" in out
        assert "duration_source=hosted" in out

    def test_asr_timeout_is_capped_to_remaining_budget(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.asr_timeout_base_seconds = 120
        ctx.asr_timeout_per_hour_seconds = 0
        ctx.asr_deadline = time.monotonic() + 30
        ctx.asr_timeout_budget_reserve_seconds = 5

        timeout = _asr_timeout_seconds(ctx, 1.0)

        assert timeout == pytest.approx(25, abs=0.25)

    def test_asr_timeout_applies_safety_margin_over_configured_budget(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.asr_timeout_base_seconds = 120
        ctx.asr_timeout_per_hour_seconds = 0
        ctx.asr_timeout_safety_margin = 1.2

        timeout = _asr_timeout_seconds(ctx, 1.0)

        assert timeout == pytest.approx(144)

    def test_asr_timeout_safety_margin_below_one_is_ignored(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.asr_timeout_base_seconds = 120
        ctx.asr_timeout_per_hour_seconds = 0
        ctx.asr_timeout_safety_margin = 0.5

        timeout = _asr_timeout_seconds(ctx, 1.0)

        assert timeout == pytest.approx(120)

    def test_asr_runtime_log_uses_default_until_real_samples_and_rolls_previous_100(self, tmp_path):
        path = tmp_path / "asr_runtime_log.json"
        log = AsrRuntimeLog(path, default_ratio=0.75)

        assert not path.exists()
        assert log.average_ratio() == pytest.approx(0.75)

        for i in range(101):
            log.append(transcribe_seconds=100 + i, recording_seconds=100)

        data = json.loads(path.read_text())
        assert len(data["samples"]) == 100
        assert data["samples"][0]["transcribe_seconds"] == 101
        assert data["samples"][0]["id"]
        assert AsrRuntimeLog(path, default_ratio=0.75).average_ratio() == pytest.approx(1.505)

    def test_asr_runtime_log_preserves_parallel_local_writers(self, tmp_path):
        path = tmp_path / "asr_runtime_log.json"
        log_a = AsrRuntimeLog(path, default_ratio=0.75)
        log_b = AsrRuntimeLog(path, default_ratio=0.75)

        log_a.append(transcribe_seconds=10, recording_seconds=100)
        log_b.append(transcribe_seconds=20, recording_seconds=100)

        samples = json.loads(path.read_text())["samples"]
        assert any(s["transcribe_seconds"] == 10 for s in samples)
        assert any(s["transcribe_seconds"] == 20 for s in samples)
        assert AsrRuntimeLog(path, default_ratio=0.75).average_ratio() == pytest.approx(0.15)

    def test_asr_defers_recording_that_cannot_fit_remaining_budget(self, tmp_path, capsys):
        ep = _ep_with_audio()
        ep.duration = 4 * 3600
        fake_asr = _FakeAsr()
        ctx = _ctx(tmp_path)
        ctx.asr_local_max_duration_hours = 4
        ctx.asr_timeout_base_seconds = 15 * 60
        ctx.asr_timeout_per_hour_seconds = 30 * 60
        ctx.asr_start_deadline = time.monotonic() + 60 * 60
        runtime_log = AsrRuntimeLog(None, default_ratio=0.75)

        fits, estimate, remaining = _asr_fits_remaining_budget(ctx, 4.0, runtime_log)
        assert fits is False
        assert estimate == pytest.approx(180 * 60)
        assert remaining == pytest.approx(60 * 60, abs=1)

        with (
            patch("citypods.stages.asr_mod", fake_asr),
            patch("citypods.stages._download_audio_file", side_effect=_fake_audio_download),
        ):
            stats = TranscriptStage().process(FakeProvider(), _city(), [ep], ctx)

        out = capsys.readouterr().out
        assert fake_asr.transcribe_calls == []
        assert stats.skipped == 1
        assert ep.transcript_key is None
        assert "reason=insufficient-budget" in out

    def test_local_asr_defers_known_duration_above_memory_limit_before_download(
        self, tmp_path, capsys
    ):
        ep = _ep_with_audio()
        ep.duration = 5 * 3600
        fake_asr = _FakeAsr()
        ctx = _ctx(tmp_path)
        ctx.asr_local_max_duration_hours = 4
        ctx.asr_semaphore = threading.Semaphore(1)

        with (
            patch("citypods.stages.asr_mod", fake_asr),
            patch(
                "citypods.stages._download_audio_file",
                side_effect=AssertionError("oversized local ASR must not download audio"),
            ),
        ):
            stats = TranscriptStage().process(FakeProvider(), _city(), [ep], ctx)

        out = capsys.readouterr().out
        assert stats.skipped == 1
        assert stats.errors == []
        assert fake_asr.transcribe_calls == []
        assert ctx.asr_semaphore.acquire(blocking=False) is True
        assert "reason=external-required" in out
        assert "duration_h=5.00" in out
        assert "duration_source=source" in out
        assert "local_max_duration_h=4.00" in out

    def test_auto_dispatch_accepts_recording_above_local_duration_limit(self, tmp_path, capsys):
        ep = _ep_with_audio()
        ep.duration = 7 * 3600
        fake_asr = _FakeAsr()
        external = _FakeDispatchBackend()
        ctx = _ctx(tmp_path)
        ctx.asr_local_max_duration_hours = 4
        ctx.compute_backend = _dispatcher(tmp_path, fake_asr, external)

        with (
            patch("citypods.stages.asr_mod", fake_asr),
            patch(
                "citypods.stages._download_audio_file",
                side_effect=AssertionError("dispatched ASR must not download locally"),
            ),
        ):
            stats = TranscriptStage().process(FakeProvider(), _city(), [ep], ctx)

        out = capsys.readouterr().out
        assert stats.dispatched == 1
        assert stats.skipped == 0
        assert len(external.submitted) == 1
        assert fake_asr.transcribe_calls == []
        assert "transcript asr dispatched" in out
        assert "reason=external-required" not in out

    def test_auto_dispatch_decline_defers_oversized_recording_instead_of_local_overflow(
        self, tmp_path, capsys
    ):
        ep = _ep_with_audio()
        ep.duration = 7 * 3600
        fake_asr = _FakeAsr()
        external = _FakeDispatchBackend()
        ctx = _ctx(tmp_path)
        ctx.asr_local_max_duration_hours = 4
        ctx.compute_backend = _dispatcher(tmp_path, fake_asr, external, monthly_gpu_seconds=30)

        with (
            patch("citypods.stages.asr_mod", fake_asr),
            patch(
                "citypods.stages._download_audio_file",
                side_effect=AssertionError("ineligible local overflow must not download audio"),
            ),
        ):
            stats = TranscriptStage().process(FakeProvider(), _city(), [ep], ctx)

        out = capsys.readouterr().out
        assert stats.dispatched == 0
        assert stats.skipped == 1
        assert stats.errors == []
        assert external.submitted == []
        assert fake_asr.transcribe_calls == []
        assert "reason=external-required" in out

    @pytest.mark.parametrize("limit", [0, -1])
    def test_non_positive_local_duration_limit_preserves_existing_behavior(self, tmp_path, limit):
        ep = _ep_with_audio()
        ep.duration = 7 * 3600
        fake_asr = _FakeAsr()
        ctx = _ctx(tmp_path)
        ctx.asr_local_max_duration_hours = limit

        with (
            patch("citypods.stages.asr_mod", fake_asr),
            patch("citypods.stages._download_audio_file", side_effect=_fake_audio_download),
            patch("citypods.stages._probe_served_duration_secs", return_value=None),
        ):
            stats = TranscriptStage().process(FakeProvider(), _city(), [ep], ctx)

        assert stats.transcribed == 1
        assert len(fake_asr.transcribe_calls) == 1

    def test_hosted_probe_defers_unknown_oversized_duration_before_local_inference(
        self, tmp_path, capsys
    ):
        ep = _ep_with_audio()
        ep.duration = None
        ep.audio_duration_served = None
        fake_asr = _FakeAsr()
        ctx = _ctx(tmp_path)
        ctx.asr_local_max_duration_hours = 4
        ctx.asr_semaphore = threading.Semaphore(1)

        with (
            patch("citypods.stages.asr_mod", fake_asr),
            patch("citypods.stages._download_audio_file", side_effect=_fake_audio_download),
            patch("citypods.stages._probe_served_duration_secs", return_value=5 * 3600),
        ):
            stats = TranscriptStage().process(FakeProvider(), _city(), [ep], ctx)

        out = capsys.readouterr().out
        assert stats.skipped == 1
        assert stats.errors == []
        assert fake_asr.transcribe_calls == []
        assert ep.audio_duration_served == pytest.approx(5 * 3600)
        assert ctx.asr_semaphore.acquire(blocking=False) is True
        assert "reason=external-required" in out
        assert "duration_source=hosted" in out

    def test_asr_skips_when_no_remaining_budget_after_download(self, tmp_path, capsys):
        ep = _ep_with_audio()
        fake_asr = _FakeAsr()
        ctx = _ctx(tmp_path)
        ctx.asr_timeout_base_seconds = 120
        ctx.asr_timeout_per_hour_seconds = 0
        ctx.asr_start_deadline = time.monotonic() - 1
        ctx.asr_timeout_budget_reserve_seconds = 0

        with (
            patch("citypods.stages.asr_mod", fake_asr),
            patch("citypods.stages._download_audio_file", side_effect=_fake_audio_download),
        ):
            stats = TranscriptStage().process(FakeProvider(), _city(), [ep], ctx)

        out = capsys.readouterr().out
        assert fake_asr.transcribe_calls == []
        assert stats.skipped == 1
        assert "reason=insufficient-budget" in out

    def test_path_a_provider_alignment_runs_when_generic_alignment_is_disabled(
        self, tmp_path, capsys
    ):
        """Provider-sourced text uses the provider alignment lane regardless of the generic flag."""
        from citypods.records import source_key as _src_key

        sk = _src_key(_city())
        ep = _ep_with_audio()
        ep.transcript_key = f"transcripts/{sk}/uid-asr-oldspec.txt"
        ep.transcript_format = "txt"
        ep.transcript_synced = False
        ep.transcript_hosted_url = f"https://cdn/{ep.transcript_key}"

        storage_root = tmp_path / "audio"
        (storage_root / ep.transcript_key).parent.mkdir(parents=True, exist_ok=True)
        (storage_root / ep.transcript_key).write_bytes(b"These are the meeting minutes.")

        class _TextSession:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def get(self, url, **kw):
                class _R:
                    status_code = 200
                    content = b"These are the meeting minutes."

                return _R()

        fake_asr = _FakeAsr()
        with (
            patch("citypods.stages.asr_mod", fake_asr),
            patch("citypods.stages._download_audio_file", side_effect=_fake_audio_download),
            patch("citypods.http.make_session", return_value=_TextSession()),
        ):
            stats = TranscriptStage().process(FakeProvider(), _city(), [ep], _ctx(tmp_path))

        out = capsys.readouterr().out
        assert ep.transcript_synced is True
        assert ep.transcript_format == "vtt"
        assert ep.transcript_selection == "provider-aligned"
        assert len(fake_asr.align_calls) == 1
        assert fake_asr.transcribe_calls == []
        assert stats.aligned == 1
        assert "alignment-disabled" not in out

    def test_trusted_route_unblocks_align_lane_despite_site_wide_disable(self, tmp_path):
        """H15 routing payoff: a provider-align route_mode overrides the site-wide
        asr_alignment_enabled=False for just that source/body, per review/12's "align lane
        implemented but unscheduled" unblock."""
        from citypods.records import source_key as _src_key
        from citypods.transcript_quality import TranscriptQualityRoute

        city = _city()
        sk = _src_key(city)
        ep = _ep_with_audio()
        ep.body = "City Council"
        ep.transcript_key = f"transcripts/{sk}/uid-asr-oldspec.txt"
        ep.transcript_format = "txt"
        ep.transcript_synced = False
        ep.transcript_hosted_url = f"https://cdn/{ep.transcript_key}"

        storage_root = tmp_path / "audio"
        (storage_root / ep.transcript_key).parent.mkdir(parents=True, exist_ok=True)
        (storage_root / ep.transcript_key).write_bytes(b"These are the meeting minutes.")

        class _TextSession:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def get(self, url, **kw):
                class _R:
                    status_code = 200
                    content = b"These are the meeting minutes."

                return _R()

        fake_asr = _FakeAsr()
        ctx = _ctx(tmp_path)
        ctx.transcript_quality_routes = {
            (sk, "city-council"): TranscriptQualityRoute(
                source_key=sk,
                body_key="city-council",
                body_name="City Council",
                route_mode="provider-align",
            )
        }
        with (
            patch("citypods.stages.asr_mod", fake_asr),
            patch("citypods.stages._download_audio_file", side_effect=_fake_audio_download),
            patch("citypods.http.make_session", return_value=_TextSession()),
        ):
            stats = TranscriptStage().process(FakeProvider(), city, [ep], ctx)

        assert city.asr_alignment_enabled is False  # site-wide default stays off
        assert len(fake_asr.align_calls) == 1
        assert stats.defer_reasons.get("alignment-disabled", 0) == 0

    def test_transcribe_lane_defers_provider_text_to_alignment_lane(self, tmp_path, capsys):
        """The scheduled transcribe lane does not replace provider text with full ASR."""
        from citypods.records import source_key as _src_key

        sk = _src_key(_city())
        ep = _ep_with_audio()
        ep.transcript_key = f"transcripts/{sk}/uid-source.txt"
        ep.transcript_format = "txt"
        ep.transcript_synced = False
        ep.transcript_hosted_url = f"https://cdn/{ep.transcript_key}"

        storage_root = tmp_path / "audio"
        (storage_root / ep.transcript_key).parent.mkdir(parents=True, exist_ok=True)
        (storage_root / ep.transcript_key).write_bytes(b"These are the meeting minutes.")

        class _TextSession:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def get(self, url, **kw):
                class _R:
                    status_code = 200
                    content = b"These are the meeting minutes."

                return _R()

        fake_asr = _FakeAsr()
        ctx = _ctx(tmp_path)
        ctx.lane = "transcribe"
        with (
            patch("citypods.stages.asr_mod", fake_asr),
            patch("citypods.stages._download_audio_file", side_effect=_fake_audio_download),
            patch("citypods.http.make_session", return_value=_TextSession()),
        ):
            stats = TranscriptStage().process(FakeProvider(), _city(), [ep], ctx)

        out = capsys.readouterr().out
        assert fake_asr.align_calls == []
        assert fake_asr.transcribe_calls == []
        assert ep.transcript_synced is False
        assert stats.defer_reasons == {"provider-align-lane": 1}
        assert out == ""

    def test_provider_text_transcript_always_uses_provider_alignment(self, tmp_path, capsys):
        """Newly fetched provider text is aligned even when generic alignment is off."""
        ep = _ep_with_audio(links={"transcript": "https://provider/t.txt"})

        class _TextSession:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def get(self, url, **kw):
                class _R:
                    status_code = 200
                    content = b"These are the meeting minutes."

                    def iter_content(self, **kw):
                        return iter([b"fake"])

                    def raise_for_status(self):
                        pass

                return _R()

        fake_asr = _FakeAsr()
        with (
            patch("citypods.stages.asr_mod", fake_asr),
            patch("citypods.stages._download_audio_file", side_effect=_fake_audio_download),
            patch("citypods.http.make_session", return_value=_TextSession()),
        ):
            stats = TranscriptStage().process(FakeProvider(), _city(), [ep], _ctx(tmp_path))

        out = capsys.readouterr().out
        known_good = ep.provider_transcript["known_good"]
        assert known_good["key"].endswith(".txt")
        assert "-provider-align-" in ep.transcript_key
        assert ep.transcript_format == "vtt"
        assert ep.transcript_synced is True
        assert ep.transcript_text_source == "provider"
        assert ep.transcript_timing_source == "computed"
        assert ep.transcript_selection == "provider-aligned"
        assert stats.ran == 2  # provider text fetch + computed alignment
        assert stats.aligned == 1
        assert fake_asr.align_calls == [
            {
                "text": "These are the meeting minutes.",
                "model": "WAV2VEC2_ASR_BASE_960H",
                "compute_type": "int8",
            }
        ]
        assert fake_asr.transcribe_calls == []
        assert "alignment-disabled" not in out

    def test_path_a_forced_alignment_with_source_text_when_enabled(self, tmp_path):
        """Stored untimed txt transcript → Path A (alignment) → synced=True."""
        from citypods.records import source_key as _src_key

        city = _city(asr_alignment_enabled=True)
        sk = _src_key(city)
        ep = _ep_with_audio()
        # Simulate a prior run storing an untimed provider transcript
        ep.transcript_key = f"transcripts/{sk}/uid-asr-oldspec.txt"
        ep.transcript_format = "txt"
        ep.transcript_synced = False

        class _TextSession:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def get(self, url, **kw):
                class _R:
                    status_code = 200
                    content = b"These are the meeting minutes."

                    def iter_content(self, **kw):
                        return iter([b"fake"])

                    def raise_for_status(self):
                        pass

                return _R()

        fake_asr = _FakeAsr()
        with (
            patch("citypods.stages.asr_mod", fake_asr),
            patch("citypods.stages._download_audio_file", side_effect=_fake_audio_download),
            patch("citypods.http.make_session", return_value=_TextSession()),
        ):
            # Put the untimed transcript in storage so _present() returns True
            storage_root = tmp_path / "audio"
            (storage_root / ep.transcript_key).parent.mkdir(parents=True, exist_ok=True)
            (storage_root / ep.transcript_key).write_bytes(b"These are the meeting minutes.")
            ep.transcript_hosted_url = f"https://cdn/{ep.transcript_key}"

            stage = TranscriptStage()
            stats = stage.process(FakeProvider(), city, [ep], _ctx(tmp_path))

        assert ep.transcript_synced is True
        assert ep.transcript_basis == "served"
        assert ep.transcript_format == "vtt"
        assert "-provider-align-" in ep.transcript_key
        assert ep.transcript_selection == "provider-aligned"
        assert stats.aligned == 1
        assert stats.transcribed == 0
        assert len(fake_asr.align_calls) == 1
        assert fake_asr.align_calls[0] == {
            "text": "These are the meeting minutes.",
            "model": "WAV2VEC2_ASR_BASE_960H",
            "compute_type": "int8",
        }

    def test_alignment_error_falls_back_to_transcription(self, tmp_path, capsys):
        """Any Path A alignment failure still produces a fresh ASR transcript."""
        from citypods.records import source_key as _src_key

        sk = _src_key(_city())
        ep = _ep_with_audio()
        ep.transcript_key = f"transcripts/{sk}/uid-asr-oldspec.txt"
        ep.transcript_format = "txt"
        ep.transcript_synced = False
        ep.transcript_hosted_url = f"https://cdn/{ep.transcript_key}"

        storage_root = tmp_path / "audio"
        (storage_root / ep.transcript_key).parent.mkdir(parents=True, exist_ok=True)
        (storage_root / ep.transcript_key).write_bytes(b"These are the meeting minutes.")

        class _TextSession:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def get(self, url, **kw):
                class _R:
                    status_code = 200
                    content = b"These are the meeting minutes."

                    def iter_content(self, **kw):
                        return iter([b"fake"])

                    def raise_for_status(self):
                        pass

                return _R()

        class _AlignFailingAsr(_FakeAsr):
            def align(
                self,
                audio_path,
                text,
                model_or_name,
                language,
                cpu_threads,
                compute_type="int8",
                **kwargs,
            ):
                self.align_calls.append(
                    {
                        "text": text,
                        "model": model_or_name,
                        "compute_type": compute_type,
                        **kwargs,
                    }
                )
                raise AttributeError("'WhisperModel' object has no attribute 'align'")

        fake_asr = _AlignFailingAsr()
        with (
            patch("citypods.stages.asr_mod", fake_asr),
            patch("citypods.stages._download_audio_file", side_effect=_fake_audio_download),
            patch("citypods.http.make_session", return_value=_TextSession()),
        ):
            stats = TranscriptStage().process(
                FakeProvider(), _city(asr_alignment_enabled=True), [ep], _ctx(tmp_path)
            )

        out = capsys.readouterr().out
        assert len(fake_asr.align_calls) == 1
        assert fake_asr.align_calls[0] == {
            "text": "These are the meeting minutes.",
            "model": "WAV2VEC2_ASR_BASE_960H",
            "compute_type": "int8",
        }
        assert len(fake_asr.transcribe_calls) == 1
        assert ep.transcript_synced is True
        assert ep.transcript_basis == "served"
        assert ep.transcript_format == "vtt"
        assert "asr-" in ep.transcript_key
        assert stats.aligned == 0
        assert stats.transcribed == 1
        assert stats.ran == 1
        assert not stats.errors
        assert "alignment-error" in out
        assert "method=transcribed" in out
        assert ep.provider_transcript["known_good"]["align_ineligible_reason"] == "alignment-error"

    def test_align_lane_error_defers_to_full_asr_without_loading_transcription_model(
        self, tmp_path
    ):
        from citypods.records import source_key as _src_key

        sk = _src_key(_city())
        ep = _ep_with_audio()
        ep.transcript_key = f"transcripts/{sk}/uid-source.txt"
        ep.transcript_format = "txt"
        ep.transcript_synced = False
        ep.transcript_hosted_url = f"https://cdn/{ep.transcript_key}"
        root = tmp_path / "audio"
        (root / ep.transcript_key).parent.mkdir(parents=True, exist_ok=True)
        (root / ep.transcript_key).write_bytes(b"The meeting is called to order.")

        class _TextSession:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def get(self, url, **kwargs):
                return type(
                    "R", (), {"status_code": 200, "content": b"The meeting is called to order."}
                )()

        class _FailingAsr(_FakeAsr):
            def align(self, *args, **kwargs):
                raise RuntimeError("ctc unavailable")

        ctx = _ctx(tmp_path)
        ctx.lane = "align"
        with (
            patch("citypods.stages.asr_mod", _FailingAsr()),
            patch("citypods.stages._download_audio_file", side_effect=_fake_audio_download),
            patch("citypods.http.make_session", return_value=_TextSession()),
        ):
            stats = TranscriptStage().process(
                FakeProvider(), _city(asr_alignment_enabled=True), [ep], ctx
            )

        assert stats.defer_reasons == {"alignment-error": 1}
        assert ep.transcript_synced is False
        assert ep.provider_transcript["known_good"]["align_ineligible_reason"] == "alignment-error"

    def test_known_text_alignment_stays_local_when_dispatch_is_enabled(self, tmp_path):
        from citypods.records import source_key as _src_key

        sk = _src_key(_city())
        ep = _ep_with_audio()
        ep.transcript_key = f"transcripts/{sk}/uid-source.txt"
        ep.transcript_format = "txt"
        ep.transcript_synced = False
        ep.transcript_hosted_url = f"https://cdn/{ep.transcript_key}"
        root = tmp_path / "audio"
        (root / ep.transcript_key).parent.mkdir(parents=True, exist_ok=True)
        (root / ep.transcript_key).write_bytes(b"The meeting is called to order.")

        class _TextSession:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def get(self, url, **kwargs):
                return type(
                    "R", (), {"status_code": 200, "content": b"The meeting is called to order."}
                )()

        fake_asr = _FakeAsr()
        external = _FakeDispatchBackend()
        ctx = _ctx(tmp_path)
        ctx.compute_backend = _dispatcher(tmp_path, fake_asr, external)
        with (
            patch("citypods.stages.asr_mod", fake_asr),
            patch("citypods.stages._download_audio_file", side_effect=_fake_audio_download),
            patch("citypods.http.make_session", return_value=_TextSession()),
        ):
            stats = TranscriptStage().process(
                FakeProvider(), _city(asr_alignment_enabled=True), [ep], ctx
            )

        assert stats.dispatched == 0
        assert external.submitted == []
        assert len(fake_asr.align_calls) == 1

    def test_skip_when_already_synced(self, tmp_path):
        """synced=True (e.g. from CivicClerk timed VTT) → ASR never called."""
        from citypods.records import source_key as _src_key

        sk = _src_key(_city())

        ep = _ep_with_audio()
        ep.transcript_synced = True
        ep.transcript_key = f"transcripts/{sk}/uid-asr-existing.vtt"

        storage_root = tmp_path / "audio"
        storage_root.mkdir(parents=True, exist_ok=True)
        (storage_root / ep.transcript_key).parent.mkdir(parents=True, exist_ok=True)
        (storage_root / ep.transcript_key).write_bytes(b"WEBVTT\n\ncue\n")
        ep.transcript_hosted_url = f"https://cdn/{ep.transcript_key}"

        fake_asr = _FakeAsr()
        with patch("citypods.stages.asr_mod", fake_asr):
            stage = TranscriptStage()
            stats = stage.process(FakeProvider(), _city(), [ep], _ctx(tmp_path))

        assert fake_asr.transcribe_calls == []
        assert fake_asr.align_calls == []
        assert stats.reused == 1

    def test_skip_when_no_audio_key(self, tmp_path):
        """Episode without hosted audio → ASR skipped (ChaptersStage may not have run yet)."""
        ep = _ep(links={})
        ep.audio_key = None  # explicitly no audio
        assert ep.audio_spec_hash is None

        fake_asr = _FakeAsr()
        with patch("citypods.stages.asr_mod", fake_asr):
            stage = TranscriptStage()
            stats = stage.process(FakeProvider(), _city(), [ep], _ctx(tmp_path))

        assert fake_asr.transcribe_calls == []
        assert ep.transcript_key is None
        assert stats.ran == 0

    def test_stop_gate_defers_asr(self, tmp_path):
        """stop() returns True before ASR → skipped (not an error)."""
        ep = _ep_with_audio()

        fake_asr = _FakeAsr()
        with patch("citypods.stages.asr_mod", fake_asr):
            ctx = _ctx(tmp_path)
            ctx.stop = lambda: True
            stage = TranscriptStage()
            stats = stage.process(FakeProvider(), _city(), [ep], ctx)

        assert fake_asr.transcribe_calls == []
        assert stats.skipped == 1
        assert ep.transcript_key is None

    def test_asr_timeout_aborts_remaining_asr_for_run(self, tmp_path):
        """A stuck native ASR call is bounded and prevents more ASR from starting this run."""
        eps = [_ep_with_audio("uid-a"), _ep_with_audio("uid-b")]
        started = threading.Event()
        release = threading.Event()

        class _SlowAsr(_FakeAsr):
            def transcribe(
                self,
                audio_path,
                model_or_name,
                language,
                compute_type,
                beam_size,
                prompt,
                cpu_threads,
            ):
                self.transcribe_calls.append({"model": model_or_name, "prompt": prompt})
                started.set()
                release.wait(timeout=10)
                return TranscriptArtifacts(vtt=self.vtt, words=self.words)

        fake_asr = _SlowAsr()
        ctx = _ctx(tmp_path)
        ctx.asr_timeout_base_seconds = 0.01
        ctx.asr_timeout_per_hour_seconds = 0
        ctx.asr_abort_event = threading.Event()
        ctx.asr_abandoned_event = threading.Event()

        with (
            patch("citypods.stages.asr_mod", fake_asr),
            patch("citypods.stages._download_audio_file", side_effect=_fake_audio_download),
        ):
            stage = TranscriptStage()
            try:
                stats = stage.process(FakeProvider(), _city(), eps, ctx)
            finally:
                release.set()

        assert started.is_set()
        assert len(fake_asr.transcribe_calls) == 1
        assert ctx.asr_abort_event.is_set()
        assert ctx.asr_abandoned_event.is_set()
        assert stats.skipped == 2
        assert any("ASR timeout" in e for e in stats.errors)
        assert all(ep.transcript_key is None for ep in eps)

    def test_abandoned_asr_keeps_worker_slot_until_thread_exits(self, tmp_path):
        ep = _ep_with_audio("uid-a")
        started = threading.Event()
        release = threading.Event()

        class _SlowAsr(_FakeAsr):
            def transcribe(
                self,
                audio_path,
                model_or_name,
                language,
                compute_type,
                beam_size,
                prompt,
                cpu_threads,
            ):
                self.transcribe_calls.append({"model": model_or_name, "prompt": prompt})
                started.set()
                release.wait(timeout=10)
                return TranscriptArtifacts(vtt=self.vtt, words=self.words)

        fake_asr = _SlowAsr()
        ctx = _ctx(tmp_path)
        ctx.asr_timeout_base_seconds = 0.01
        ctx.asr_timeout_per_hour_seconds = 0
        ctx.asr_abort_event = threading.Event()
        ctx.asr_abandoned_event = threading.Event()
        ctx.asr_semaphore = threading.Semaphore(1)

        with (
            patch("citypods.stages.asr_mod", fake_asr),
            patch("citypods.stages._download_audio_file", side_effect=_fake_audio_download),
        ):
            stats = TranscriptStage().process(FakeProvider(), _city(), [ep], ctx)

        assert started.is_set()
        assert stats.skipped == 1
        assert ctx.asr_abandoned_event.is_set()
        assert ctx.asr_semaphore.acquire(blocking=False) is False
        release.set()
        assert ctx.asr_semaphore.acquire(timeout=2) is True
        ctx.asr_semaphore.release()

    def test_killable_backend_times_out_one_episode_then_continues(self, tmp_path, capsys):
        eps = [_ep_with_audio("uid-a"), _ep_with_audio("uid-b")]

        class _KillableBackend:
            name = "local"
            isolates_inference = True

            def __init__(self):
                self.calls = 0
                self.release = threading.Event()
                self.terminated = 0

            def run_inference(self, job):
                self.calls += 1
                if self.calls == 1:
                    self.release.wait(timeout=10)
                    raise RuntimeError("worker terminated")
                return type(
                    "_Result",
                    (),
                    {
                        "output": TranscriptArtifacts(
                            vtt=b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nok\n",
                            words=ASR_WORDS,
                        )
                    },
                )()

            def terminate_active(self):
                self.terminated += 1
                self.release.set()
                return True

        backend = _KillableBackend()
        ctx = _ctx(tmp_path)
        ctx.compute_backend = backend
        ctx.asr_timeout_base_seconds = 0.01
        ctx.asr_timeout_per_hour_seconds = 0
        ctx.asr_abort_event = threading.Event()
        ctx.asr_abandoned_event = threading.Event()

        with patch("citypods.stages._download_audio_file", side_effect=_fake_audio_download):
            stats = TranscriptStage().process(FakeProvider(), _city(), eps, ctx)

        out = capsys.readouterr().out
        assert backend.terminated == 1
        assert backend.calls == 2
        assert stats.skipped == 1
        assert stats.transcribed == 1
        assert eps[0].transcript_timeout_attempts == 1
        assert eps[0].transcript_timeout_last_attempt is not None
        assert eps[1].transcript_timeout_attempts == 0
        assert eps[1].transcript_synced is True
        assert not ctx.asr_abort_event.is_set()
        assert not ctx.asr_abandoned_event.is_set()
        assert "worker=terminated" in out

    def test_episode_timeout_backoff_skips_only_that_episode(self, tmp_path, capsys):
        backed_off = _ep_with_audio("uid-a")
        backed_off.transcript_timeout_attempts = 2
        backed_off.transcript_timeout_last_attempt = datetime.now(UTC).isoformat()
        eligible = _ep_with_audio("uid-b")
        fake_asr = _FakeAsr()

        with (
            patch("citypods.stages.asr_mod", fake_asr),
            patch("citypods.stages._download_audio_file", side_effect=_fake_audio_download),
        ):
            stats = TranscriptStage().process(
                FakeProvider(), _city(), [backed_off, eligible], _ctx(tmp_path)
            )

        out = capsys.readouterr().out
        assert stats.skipped == 1
        assert stats.transcribed == 1
        assert eligible.transcript_synced is True
        assert len(fake_asr.transcribe_calls) == 1
        assert "reason=timeout-backoff attempts=2" in out

    def test_expired_timeout_backoff_allows_retry(self, tmp_path):
        ep = _ep_with_audio()
        ep.transcript_timeout_attempts = 1
        ep.transcript_timeout_last_attempt = (datetime.now(UTC) - timedelta(days=2)).isoformat()

        ep, stats, fake_asr = _run_asr(tmp_path, ep)

        assert len(fake_asr.transcribe_calls) == 1
        assert stats.transcribed == 1
        assert ep.transcript_timeout_attempts == 0
        assert ep.transcript_timeout_last_attempt is None

    def test_waiting_asr_slot_polls_abort_event(self, tmp_path):
        ep = _ep_with_audio("uid-a")
        fake_asr = _FakeAsr()
        ctx = _ctx(tmp_path)
        ctx.asr_abort_event = threading.Event()
        ctx.asr_semaphore = threading.Semaphore(0)
        result = {}

        def _run():
            result["stats"] = TranscriptStage().process(FakeProvider(), _city(), [ep], ctx)

        with patch("citypods.stages.asr_mod", fake_asr):
            worker = threading.Thread(target=_run)
            worker.start()
            time.sleep(0.2)
            ctx.asr_abort_event.set()
            worker.join(timeout=4)

        assert not worker.is_alive()
        assert fake_asr.transcribe_calls == []
        assert result["stats"].skipped == 1
        assert ep.transcript_key is None

    def test_asr_waits_for_resource_admission(self, tmp_path):
        ep = _ep_with_audio()
        admission = FakeAdmission()
        fake_asr = _FakeAsr()
        ctx = _ctx(tmp_path)
        ctx.resource_admission = admission

        with (
            patch("citypods.stages.asr_mod", fake_asr),
            patch("citypods.stages._download_audio_file", side_effect=_fake_audio_download),
        ):
            stats = TranscriptStage().process(FakeProvider(), _city(), [ep], ctx)

        assert admission.calls == [("asr", ep.uid)]
        assert stats.transcribed == 1
        assert fake_asr.transcribe_calls

    def test_asr_defers_when_resource_admission_stops(self, tmp_path):
        ep = _ep_with_audio()
        admission = FakeAdmission(admitted=False)
        fake_asr = _FakeAsr()
        ctx = _ctx(tmp_path)
        ctx.resource_admission = admission

        with patch("citypods.stages.asr_mod", fake_asr):
            stats = TranscriptStage().process(FakeProvider(), _city(), [ep], ctx)

        assert admission.calls == [("asr", ep.uid)]
        assert stats.skipped == 1
        assert fake_asr.transcribe_calls == []

    def test_asr_reuse_when_key_already_present(self, tmp_path):
        """ASR key already in storage → reuse without re-running inference."""
        from citypods.records import source_key as _src_key

        ep = _ep_with_audio()

        # Pre-compute the asr_key we expect
        city = _city()
        recipe = _fresh_recipe(ep, city)
        src_key = _src_key(city)
        asr_key = f"transcripts/{src_key}/{ep.uid}-asr-{recipe}.vtt"

        # Put the key in storage
        storage_root = tmp_path / "audio"
        storage_root.mkdir(parents=True, exist_ok=True)
        (storage_root / asr_key).parent.mkdir(parents=True, exist_ok=True)
        (storage_root / asr_key).write_bytes(ASR_VTT)
        words_key = f"transcripts/{src_key}/{ep.uid}-asr-{recipe}.words.json"
        (storage_root / words_key).write_bytes(ASR_WORDS)

        fake_asr = _FakeAsr()
        with patch("citypods.stages.asr_mod", fake_asr):
            stage = TranscriptStage()
            stats = stage.process(FakeProvider(), _city(), [ep], _ctx(tmp_path))

        assert fake_asr.transcribe_calls == []
        assert ep.transcript_synced is True
        assert ep.transcript_key == asr_key
        assert stats.reused == 1

    def test_empty_existing_word_sidecar_routes_back_to_asr(self, tmp_path):
        ep = _ep_with_audio()
        city = _city()
        src_key = source_key(city)
        old_key = f"transcripts/{src_key}/{ep.uid}-asr-old.vtt"
        old_words_key = f"transcripts/{src_key}/{ep.uid}-asr-old.words.json"
        ep.transcript_key = old_key
        ep.transcript_words_key = old_words_key
        ep.transcript_synced = True
        ep.transcript_pipeline_version = ASR_PIPELINE_VERSION
        ep.transcript_spec_hash = "old"
        root = tmp_path / "audio"
        (root / old_key).parent.mkdir(parents=True, exist_ok=True)
        (root / old_key).write_bytes(ASR_VTT)
        (root / old_words_key).write_bytes(b'{"segments":[]}')

        ep, stats, fake_asr = _run_asr(tmp_path, ep)

        assert fake_asr.transcribe_calls
        assert stats.transcribed == 1
        assert ep.transcript_words_key
        assert (root / ep.transcript_words_key).read_bytes() == ASR_WORDS

    def test_concurrent_source_aliases_share_one_asr_inference(self, tmp_path):
        """Same stable meeting + recipe in two source views runs native ASR only once."""

        inference_started = threading.Event()
        finish_inference = threading.Event()

        second_acquire = threading.Event()

        class _InstrumentedSemaphore:
            def __init__(self):
                self._sem = threading.Semaphore(1)
                self._lock = threading.Lock()
                self._attempts = 0

            def acquire(self, timeout=None):
                with self._lock:
                    self._attempts += 1
                    if self._attempts == 2:
                        second_acquire.set()
                return self._sem.acquire(timeout=timeout)

            def release(self):
                self._sem.release()

        class _SlowFakeAsr(_FakeAsr):
            def transcribe(self, *args, **kwargs):
                inference_started.set()
                assert finish_inference.wait(timeout=3)
                return super().transcribe(*args, **kwargs)

        fake_asr = _SlowFakeAsr()
        ctx = _ctx(tmp_path)
        ctx.asr_semaphore = _InstrumentedSemaphore()
        city_a = _city(slug="source-a", source={"feed_url": "a"})
        city_b = _city(slug="source-b", source={"feed_url": "b"})
        ep_a = _ep_with_audio("shared")
        ep_b = _ep_with_audio("shared")
        results = []

        def _run(city, ep):
            results.append(TranscriptStage().process(FakeProvider(), city, [ep], ctx))

        with (
            patch("citypods.stages.asr_mod", fake_asr),
            patch("citypods.stages._download_audio_file", side_effect=_fake_audio_download),
        ):
            first = threading.Thread(target=_run, args=(city_a, ep_a))
            second = threading.Thread(target=_run, args=(city_b, ep_b))
            workers = [first, second]
            first.start()
            assert inference_started.wait(timeout=3)
            second.start()
            assert second_acquire.wait(timeout=3)
            finish_inference.set()
            for worker in workers:
                worker.join(timeout=5)

        assert all(not worker.is_alive() for worker in workers)
        assert len(fake_asr.transcribe_calls) == 1
        assert ep_a.transcript_synced and ep_b.transcript_synced
        assert ep_a.transcript_key != ep_b.transcript_key  # durable layout remains source-scoped
        assert sum(stats.transcribed for stats in results) == 1
        assert sum(stats.reused for stats in results) == 1

    def test_inflight_reservation_dedupes_with_multiple_asr_permits(self, tmp_path):
        """Per-key reservation, not the one-slot default, prevents concurrent duplicate ASR."""
        inference_started = threading.Event()
        finish_inference = threading.Event()
        second_claim = threading.Event()

        class _ObservedCache(AsrArtifactCache):
            def __init__(self):
                super().__init__()
                self._calls = 0
                self._calls_lock = threading.Lock()

            def claim(self, key):
                with self._calls_lock:
                    self._calls += 1
                    if self._calls == 2:
                        second_claim.set()
                return super().claim(key)

        class _SlowFakeAsr(_FakeAsr):
            def transcribe(self, *args, **kwargs):
                inference_started.set()
                assert finish_inference.wait(timeout=3)
                return super().transcribe(*args, **kwargs)

        fake_asr = _SlowFakeAsr()
        ctx = _ctx(tmp_path)
        ctx.asr_semaphore = threading.Semaphore(2)
        ctx.asr_artifact_cache = _ObservedCache()
        city_a = _city(slug="source-a", source={"feed_url": "a"})
        city_b = _city(slug="source-b", source={"feed_url": "b"})
        ep_a = _ep_with_audio("shared")
        ep_b = _ep_with_audio("shared")

        with (
            patch("citypods.stages.asr_mod", fake_asr),
            patch("citypods.stages._download_audio_file", side_effect=_fake_audio_download),
        ):
            first = threading.Thread(
                target=TranscriptStage().process,
                args=(FakeProvider(), city_a, [ep_a], ctx),
            )
            second = threading.Thread(
                target=TranscriptStage().process,
                args=(FakeProvider(), city_b, [ep_b], ctx),
            )
            first.start()
            assert inference_started.wait(timeout=3)
            second.start()
            assert second_claim.wait(timeout=3)
            finish_inference.set()
            first.join(timeout=5)
            second.join(timeout=5)

        assert not first.is_alive() and not second.is_alive()
        assert len(fake_asr.transcribe_calls) == 1
        assert ep_a.transcript_synced and ep_b.transcript_synced

    def test_asr_error_recorded_not_raised(self, tmp_path):
        """ASR failure → error recorded in stats, episode left without transcript."""
        ep = _ep_with_audio()

        fake_asr = _FakeAsr(fail=True)
        with (
            patch("citypods.stages.asr_mod", fake_asr),
            patch("citypods.stages._download_audio_file", side_effect=_fake_audio_download),
        ):
            stage = TranscriptStage()
            stats = stage.process(FakeProvider(), _city(), [ep], _ctx(tmp_path))

        assert stats.errors  # at least one error recorded
        assert ep.transcript_synced is False  # left in un-synced state

    def test_model_load_failure_skips_asr_with_single_error(self, tmp_path):
        """If the model can't be loaded (e.g. HF Hub 429), one error is recorded and
        all episodes skip ASR — no per-episode errors, no crash."""
        eps = [_ep_with_audio(f"uid-{i}") for i in range(3)]

        fake_asr = _FakeAsr(fail_load=True)
        with patch("citypods.stages.asr_mod", fake_asr):
            stage = TranscriptStage()
            stats = stage.process(FakeProvider(), _city(), eps, _ctx(tmp_path))

        assert fake_asr.transcribe_calls == []
        assert all(ep.transcript_key is None for ep in eps)
        assert len(stats.errors) == 1  # single batch-level error, not per-episode
        assert "model load failed" in stats.errors[0]

    def test_asr_disabled_skips_slot(self, tmp_path):
        """asr_enabled=False → slot is skipped entirely."""
        ep = _ep_with_audio()

        city = _city()
        city.asr_enabled = False

        fake_asr = _FakeAsr()
        with (
            patch("citypods.stages.asr_mod", fake_asr),
            patch("citypods.stages._download_audio_file", side_effect=_fake_audio_download),
        ):
            stage = TranscriptStage()
            stage.process(FakeProvider(), city, [ep], _ctx(tmp_path))

        assert fake_asr.transcribe_calls == []
        assert ep.transcript_key is None


class TestTranscriptVersionAwareReuse:
    """H12: version-aware reuse — stale ASR transcripts re-do, provider transcripts never do."""

    def test_fresh_transcribe_stores_word_sidecar_and_version(self, tmp_path):
        ep = _ep_with_audio()
        ep, stats, fake_asr = _run_asr(tmp_path, ep)
        assert ep.transcript_words_key and ep.transcript_words_key.endswith(".words.json")
        assert ep.transcript_pipeline_version == ASR_PIPELINE_VERSION
        audio_dir = tmp_path / "audio"
        assert (audio_dir / ep.transcript_key).exists()
        assert (audio_dir / ep.transcript_words_key).exists()

    def test_current_version_asr_transcript_is_reused(self, tmp_path):
        ep = _ep_with_audio()
        city = _city()
        recipe = _fresh_recipe(ep, city)
        src_key = source_key(city)
        asr_key = f"transcripts/{src_key}/{ep.uid}-asr-{recipe}.vtt"
        ep.transcript_key = asr_key
        ep.transcript_synced = True
        ep.transcript_pipeline_version = ASR_PIPELINE_VERSION
        root = tmp_path / "audio"
        (root / asr_key).parent.mkdir(parents=True, exist_ok=True)
        (root / asr_key).write_bytes(ASR_VTT)
        fake_asr = _FakeAsr()
        with patch("citypods.stages.asr_mod", fake_asr):
            stats = TranscriptStage().process(FakeProvider(), _city(), [ep], _ctx(tmp_path))
        assert stats.reused == 1
        assert fake_asr.transcribe_calls == []

    def test_stale_provider_align_keeps_active_vtt_when_source_is_unavailable(self, tmp_path):
        ep = _ep_with_audio()
        city = _city(asr_alignment_enabled=False)
        src_key = source_key(city)
        old_key = f"transcripts/{src_key}/{ep.uid}-provider-align-old.vtt"
        old_url = f"https://cdn/{old_key}"
        ep.transcript_key = old_key
        ep.transcript_hosted_url = old_url
        ep.transcript_format = "vtt"
        ep.transcript_basis = "served"
        ep.transcript_synced = True
        ep.transcript_pipeline_version = "provider-align:1"
        ep.provider_transcript = {
            "known_good": {
                "format": "txt",
                "hosted_url": "https://cdn/transcripts/provider-source.txt",
            }
        }
        root = tmp_path / "audio"
        (root / old_key).parent.mkdir(parents=True, exist_ok=True)
        (root / old_key).write_bytes(ASR_VTT)

        class _TextSession:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def get(self, url, **kwargs):
                return type(
                    "R", (), {"status_code": 200, "content": b"The meeting is called to order."}
                )()

        with (
            patch("citypods.stages.asr_mod", _FakeAsr()),
            patch("citypods.http.make_session", return_value=_TextSession()),
        ):
            stats = TranscriptStage().process(FakeProvider(), city, [ep], _ctx(tmp_path))

        assert stats.defer_reasons == {}
        assert ep.transcript_key == old_key
        assert ep.transcript_hosted_url == old_url
        assert ep.transcript_synced is True

    def test_current_version_old_shape_asr_key_is_copied_to_timeline_recipe(self, tmp_path):
        ep = _ep_with_audio()
        city = _city()
        src_key = source_key(city)
        old_recipe = asr_spec_hash(
            "old-audio-spec",
            city.asr_model,
            None,
            ASR_PIPELINE_VERSION,
            language=city.asr_language or None,
            compute_type=city.asr_compute_type,
            beam_size=city.asr_beam_size,
            initial_prompt=asr_initial_prompt(city.podcast_author, ep.body, ep.title),
        )
        old_key = f"transcripts/{src_key}/{ep.uid}-asr-{old_recipe}.vtt"
        old_words = f"transcripts/{src_key}/{ep.uid}-asr-{old_recipe}.words.json"
        ep.transcript_key = old_key
        ep.transcript_words_key = old_words
        ep.transcript_spec_hash = old_recipe
        ep.transcript_synced = True
        ep.transcript_pipeline_version = ASR_PIPELINE_VERSION
        root = tmp_path / "audio"
        (root / old_key).parent.mkdir(parents=True, exist_ok=True)
        (root / old_key).write_bytes(ASR_VTT)
        (root / old_words).write_bytes(ASR_WORDS)

        fake_asr = _FakeAsr()
        with patch("citypods.stages.asr_mod", fake_asr):
            stats = TranscriptStage().process(FakeProvider(), city, [ep], _ctx(tmp_path))

        new_recipe = _fresh_recipe(ep, city)
        new_key = f"transcripts/{src_key}/{ep.uid}-asr-{new_recipe}.vtt"
        new_words = f"transcripts/{src_key}/{ep.uid}-asr-{new_recipe}.words.json"
        assert fake_asr.transcribe_calls == []
        assert ep.transcript_key == new_key
        assert ep.transcript_words_key == new_words
        assert ep.transcript_spec_hash == new_recipe
        assert (root / new_key).read_bytes() == ASR_VTT
        assert (root / new_words).read_bytes() == ASR_WORDS
        assert stats.reused == 1
        assert stats.asr_migration_copied == 1
        assert stats.asr_migration_missing == 0
        assert stats.asr_migration_regenerated == 0

    def test_missing_old_shape_asr_artifact_is_reported_and_regenerated(self, tmp_path):
        ep = _ep_with_audio()
        city = _city()
        src_key = source_key(city)
        old_recipe = asr_spec_hash(
            "old-audio-spec",
            city.asr_model,
            None,
            ASR_PIPELINE_VERSION,
            language=city.asr_language or None,
            compute_type=city.asr_compute_type,
            beam_size=city.asr_beam_size,
            initial_prompt=asr_initial_prompt(city.podcast_author, ep.body, ep.title),
        )
        ep.transcript_key = f"transcripts/{src_key}/{ep.uid}-asr-{old_recipe}.vtt"
        ep.transcript_words_key = f"transcripts/{src_key}/{ep.uid}-asr-{old_recipe}.words.json"
        ep.transcript_spec_hash = old_recipe
        ep.transcript_synced = True
        ep.transcript_pipeline_version = ASR_PIPELINE_VERSION

        ep, stats, fake_asr = _run_asr(tmp_path, ep)

        assert len(fake_asr.transcribe_calls) == 1
        assert ep.transcript_spec_hash == _fresh_recipe(ep, city)
        assert stats.asr_migration_missing == 1
        assert stats.asr_migration_regenerated == 1
        assert stats.asr_migration_copied == 0

    def test_partial_migrated_asr_vtt_without_words_regenerates(self, tmp_path):
        ep = _ep_with_audio()
        city = _city()
        src_key = source_key(city)
        old_recipe = asr_spec_hash(
            "old-audio-spec",
            city.asr_model,
            None,
            ASR_PIPELINE_VERSION,
            language=city.asr_language or None,
            compute_type=city.asr_compute_type,
            beam_size=city.asr_beam_size,
            initial_prompt=asr_initial_prompt(city.podcast_author, ep.body, ep.title),
        )
        new_recipe = _fresh_recipe(ep, city)
        old_key = f"transcripts/{src_key}/{ep.uid}-asr-{old_recipe}.vtt"
        old_words = f"transcripts/{src_key}/{ep.uid}-asr-{old_recipe}.words.json"
        new_key = f"transcripts/{src_key}/{ep.uid}-asr-{new_recipe}.vtt"
        new_words = f"transcripts/{src_key}/{ep.uid}-asr-{new_recipe}.words.json"
        ep.transcript_key = old_key
        ep.transcript_words_key = old_words
        ep.transcript_spec_hash = old_recipe
        ep.transcript_synced = True
        ep.transcript_pipeline_version = ASR_PIPELINE_VERSION
        root = tmp_path / "audio"
        (root / old_key).parent.mkdir(parents=True, exist_ok=True)
        (root / old_key).write_bytes(ASR_VTT)
        (root / new_key).parent.mkdir(parents=True, exist_ok=True)
        (root / new_key).write_bytes(ASR_VTT)

        ep, stats, fake_asr = _run_asr(tmp_path, ep)

        assert len(fake_asr.transcribe_calls) == 1
        assert ep.transcript_key == new_key
        assert ep.transcript_words_key == new_words
        assert (root / new_words).exists()
        assert stats.asr_migration_missing == 1
        assert stats.asr_migration_regenerated == 1

    def test_stale_version_asr_transcript_is_redone(self, tmp_path):
        ep = _ep_with_audio()
        src_key = source_key(_city())
        city = _city()
        old_recipe = _fresh_recipe(ep, city, "1")
        old_key = f"transcripts/{src_key}/{ep.uid}-asr-{old_recipe}.vtt"
        ep.transcript_key = old_key
        ep.transcript_synced = True
        ep.transcript_pipeline_version = "1"
        root = tmp_path / "audio"
        (root / old_key).parent.mkdir(parents=True, exist_ok=True)
        (root / old_key).write_bytes(ASR_VTT)
        ep, stats, fake_asr = _run_asr(tmp_path, ep)
        assert len(fake_asr.transcribe_calls) == 1  # re-transcribed, not reused
        assert ep.transcript_pipeline_version == ASR_PIPELINE_VERSION
        new_recipe = _fresh_recipe(ep, city)
        assert ep.transcript_key == f"transcripts/{src_key}/{ep.uid}-asr-{new_recipe}.vtt"
        assert ep.transcript_words_key.endswith(f"-asr-{new_recipe}.words.json")

    def test_accepted_route_recipe_reuses_old_asr_without_retranscribe(self, tmp_path):
        # accepted_active_recipes is a catalog-wide lever, so it keys on the catalog-wide
        # transcript_pipeline_version — never transcript_spec_hash, which folds in a per-episode
        # media hash and could therefore never match more than one episode.
        ep = _ep_with_audio()
        ep.body = "City Council"
        city = _city()
        src_key = source_key(city)
        old_recipe = _fresh_recipe(ep, city, "1")
        old_key = f"transcripts/{src_key}/{ep.uid}-asr-{old_recipe}.vtt"
        old_words = f"transcripts/{src_key}/{ep.uid}-asr-{old_recipe}.words.json"
        ep.transcript_key = old_key
        ep.transcript_words_key = old_words
        ep.transcript_spec_hash = old_recipe
        ep.transcript_synced = True
        ep.transcript_pipeline_version = "1"
        root = tmp_path / "audio"
        (root / old_key).parent.mkdir(parents=True, exist_ok=True)
        (root / old_key).write_bytes(ASR_VTT)
        (root / old_words).write_bytes(ASR_WORDS)
        fake_asr = _FakeAsr()
        ctx = _ctx(tmp_path)
        ctx.transcript_quality_routes = {
            (src_key, "city-council"): TranscriptQualityRoute(
                source_key=src_key,
                body_key="city-council",
                body_name="City Council",
                route_mode="fresh-asr",
                accepted_active_recipes=("1",),
            )
        }
        with patch("citypods.stages.asr_mod", fake_asr):
            stats = TranscriptStage().process(FakeProvider(), city, [ep], ctx)
        assert fake_asr.transcribe_calls == []
        assert stats.reused == 1
        assert ep.transcript_key == old_key
        assert ep.transcript_words_key == old_words

    def test_provider_transcript_not_invalidated_by_asr_version(self, tmp_path):
        ep = _ep_with_audio()
        src_key = source_key(_city())
        prov_key = f"transcripts/{src_key}/{ep.uid}-deadbeef0000.vtt"  # no -asr- infix
        ep.transcript_key = prov_key
        ep.transcript_synced = True
        ep.transcript_pipeline_version = None  # provider-supplied
        root = tmp_path / "audio"
        (root / prov_key).parent.mkdir(parents=True, exist_ok=True)
        (root / prov_key).write_bytes(VTT_CONTENT)
        fake_asr = _FakeAsr()
        with patch("citypods.stages.asr_mod", fake_asr):
            stats = TranscriptStage().process(FakeProvider(), _city(), [ep], _ctx(tmp_path))
        assert stats.reused == 1
        assert fake_asr.transcribe_calls == []
        assert ep.transcript_key == prov_key
