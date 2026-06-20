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

from citypods.asr import TranscriptArtifacts, asr_spec_hash
from citypods.compute import DispatchCoordinator, DispatchTarget
from citypods.compute.base import InferenceJob, JobHandle
from citypods.compute.budget import Budget
from citypods.compute.local import LocalBackend
from citypods.models import City, Episode
from citypods.records import (
    episode_to_record,
    record_to_episode,
    referenced_audio_keys,
    save_records,
    source_key,
)
from citypods.stages import (
    ASR_PIPELINE_VERSION,
    AsrRuntimeLog,
    StageContext,
    TranscriptStage,
    _asr_fits_remaining_budget,
    _asr_timeout_seconds,
    default_stages,
    enrich_stages,
)
from citypods.storage.local import LocalStorage

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
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

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
            "chapters",
            "timeline",
            "remap",
            "audio",
            "transcript",
            "links",
        ]

    def test_full_order_enrich(self):
        assert self._names(enrich_stages()) == [
            "chapters",
            "timeline",
            "remap",
            "audio",
            "transcript",
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
        # #16: reuse is cheap idempotent bookkeeping → must run even after stop(), so a yielded
        # run still references the already-stored transcript (mirrors AudioStage).
        ep = self._store_once(tmp_path)
        ep.transcript_hosted_url = None  # a fresh run must re-attach the URL
        ctx = _ctx(tmp_path)
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

    def test_spec_is_content_addressed_not_url(self, tmp_path):
        # #17: identical content behind different (tokenized) URLs → same spec/key.
        ep_a = _ep(uid="uid-x", links={"transcript": "https://p/a.vtt?token=AAA"})
        ep_b = _ep(uid="uid-x", links={"transcript": "https://p/b.vtt?token=ZZZ"})
        ep_a.timeline = ep_b.timeline = None
        with patch("citypods.http.make_session", return_value=_fetch(VTT_CONTENT)):
            TranscriptStage().process(FakeProvider(), _city(), [ep_a], _ctx(tmp_path))
            TranscriptStage().process(FakeProvider(), _city(), [ep_b], _ctx(tmp_path))
        assert ep_a.transcript_spec_hash == ep_b.transcript_spec_hash
        assert ep_a.transcript_key == ep_b.transcript_key

    def test_referenced_keys_protect_transcripts_from_gc(self, tmp_path):
        # Fix A: the live set the orphan GC keeps must include transcript keys, or
        # gc_audio.py --apply would reap hosted transcripts.
        ep = self._store_once(tmp_path)
        state_dir = tmp_path / "state"
        save_records(state_dir, source_key(_city()), {ep.uid: episode_to_record(ep)})
        refs = referenced_audio_keys(state_dir)
        assert ep.transcript_key in refs

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
ASR_WORDS = b'{"schema":"1","basis":"served","segments":[]}'


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

    def align(self, audio_path, text, model_or_name, language, cpu_threads):
        if self.fail:
            raise RuntimeError("align failed")
        self.align_calls.append({"text": text, "model": model_or_name})
        return TranscriptArtifacts(vtt=self.vtt, words=self.words)

    def asr_spec_hash(self, audio_spec_hash, model, align_hash, version):
        # Use the real implementation
        return asr_spec_hash(audio_spec_hash, model, align_hash, version)


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
        assert "asr-" in ep.transcript_key
        assert stats.ran == 1
        assert stats.transcribed == 1
        assert stats.aligned == 0

    def test_path_b_initial_prompt_contains_title(self, tmp_path):
        """Path B: initial_prompt includes podcast_title and episode title."""
        ep = _ep_with_audio()
        ep, stats, fake_asr = _run_asr(tmp_path, ep)

        assert len(fake_asr.transcribe_calls) == 1
        prompt = fake_asr.transcribe_calls[0]["prompt"]
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

        with patch("citypods.stages._probe_duration_secs", return_value=1800.0):
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
            patch("citypods.stages._probe_duration_secs", return_value=None),
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
            patch("citypods.stages._probe_duration_secs", return_value=5 * 3600),
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

    def test_path_a_forced_alignment_skipped_when_disabled(self, tmp_path, capsys):
        """Stored untimed txt transcript is deferred while alignment is disabled."""
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
            patch("citypods.http.make_session", return_value=_TextSession()),
        ):
            stats = TranscriptStage().process(FakeProvider(), _city(), [ep], _ctx(tmp_path))

        out = capsys.readouterr().out
        assert ep.transcript_synced is False
        assert ep.transcript_format == "txt"
        assert fake_asr.align_calls == []
        assert fake_asr.transcribe_calls == []
        assert stats.skipped == 1
        assert stats.defer_reasons == {"alignment-disabled": 1}
        assert "reason=alignment-disabled" in out

    def test_transcribe_lane_ignores_source_text_when_alignment_disabled(self, tmp_path, capsys):
        """The scheduled transcribe lane forces fresh ASR before applying the align-only guard."""
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
        assert len(fake_asr.transcribe_calls) == 1
        assert ep.transcript_synced is True
        assert stats.transcribed == 1
        assert stats.skipped == 0
        assert "reason=alignment-disabled" not in out

    def test_provider_text_transcript_defers_alignment_without_fallback(self, tmp_path, capsys):
        """Newly fetched untimed provider text is stored, then deferred while alignment is off."""
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
            patch("citypods.http.make_session", return_value=_TextSession()),
        ):
            stats = TranscriptStage().process(FakeProvider(), _city(), [ep], _ctx(tmp_path))

        out = capsys.readouterr().out
        assert ep.transcript_key is not None
        assert ep.transcript_format == "txt"
        assert ep.transcript_synced is False
        assert stats.ran == 1  # provider text was stored
        assert stats.skipped == 1  # timed upgrade deferred
        assert fake_asr.align_calls == []
        assert fake_asr.transcribe_calls == []
        assert "reason=alignment-disabled" in out

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
        assert "asr-" in ep.transcript_key
        assert stats.aligned == 1
        assert stats.transcribed == 0

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
            def align(self, audio_path, text, model_or_name, language, cpu_threads):
                self.align_calls.append({"text": text, "model": model_or_name})
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
                            words=b"{}",
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
        recipe = asr_spec_hash(ep.audio_spec_hash, _city().asr_model, None, ASR_PIPELINE_VERSION)
        src_key = _src_key(_city())
        asr_key = f"transcripts/{src_key}/{ep.uid}-asr-{recipe}.vtt"

        # Put the key in storage
        storage_root = tmp_path / "audio"
        storage_root.mkdir(parents=True, exist_ok=True)
        (storage_root / asr_key).parent.mkdir(parents=True, exist_ok=True)
        (storage_root / asr_key).write_bytes(ASR_VTT)

        fake_asr = _FakeAsr()
        with patch("citypods.stages.asr_mod", fake_asr):
            stage = TranscriptStage()
            stats = stage.process(FakeProvider(), _city(), [ep], _ctx(tmp_path))

        assert fake_asr.transcribe_calls == []
        assert ep.transcript_synced is True
        assert ep.transcript_key == asr_key
        assert stats.reused == 1

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
        recipe = asr_spec_hash(ep.audio_spec_hash, _city().asr_model, None, ASR_PIPELINE_VERSION)
        src_key = source_key(_city())
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

    def test_stale_version_asr_transcript_is_redone(self, tmp_path):
        ep = _ep_with_audio()
        src_key = source_key(_city())
        old_recipe = asr_spec_hash(ep.audio_spec_hash, _city().asr_model, None, "1")
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
        new_recipe = asr_spec_hash(
            ep.audio_spec_hash, _city().asr_model, None, ASR_PIPELINE_VERSION
        )
        assert ep.transcript_key == f"transcripts/{src_key}/{ep.uid}-asr-{new_recipe}.vtt"
        assert ep.transcript_words_key.endswith(f"-asr-{new_recipe}.words.json")

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
