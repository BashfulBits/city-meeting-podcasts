"""Tests for ChapterBoundaryLocatorStage."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from citypods.chapter_artifacts import (
    AgendaCandidate,
    AgendaCandidatesArtifact,
)
from citypods.chapter_jobs import build_locator_job
from citypods.chapter_locator import build_locator_units
from citypods.compute.base import JobHandle, JobResult
from citypods.models import City, Episode
from citypods.stages import (
    CHAPTER_LOCATOR_PIPELINE_VERSION,
    ChapterBoundaryLocatorStage,
    StageContext,
    _legacy_stage_complete,
    stage_input_fingerprint,
    stage_output_pointer,
)
from citypods.storage.local import LocalStorage


def _make_city() -> City:
    return City(
        slug="austin-tx",
        provider="civicplus",
        source={"feed_url": "https://example.com/feed"},
        podcast_title="Austin Council",
        podcast_author="City of Austin",
        podcast_email="test@example.com",
        podcast_description="Austin meeting recordings",
    )


def _make_episode(uid: str = "ep-1") -> Episode:
    agenda = AgendaCandidatesArtifact(
        episode_uid=uid,
        source_hash="agenda-hash-1",
        model="mistral/mistral-medium-2508",
        prompt_version="agenda-flow",
        recipe="recipe-agenda-1",
        items=(
            AgendaCandidate(
                index=0,
                title="Call to order",
                kind="substantive_action",
                line_start=1,
                line_end=1,
                evidence_text="1. Call to order",
                display_ref="1",
                status="accepted",
            ),
            AgendaCandidate(
                index=1,
                title="Rejected item",
                kind="procedural",
                line_start=2,
                line_end=2,
                evidence_text="2. Rejected item",
                display_ref="2",
                status="rejected",
            ),
        ),
    )
    return Episode(
        guid=f"guid-{uid}",
        uid=uid,
        title="City Council Regular Meeting",
        published=datetime(2026, 8, 1, tzinfo=UTC),
        video_url="https://example.com/video.mp4",
        media_kind="direct",
        body="City Council",
        transcript_key=f"transcripts/{uid}.vtt",
        transcript_words_key=f"transcripts/{uid}.words.json",
        generated_agenda_candidates=agenda.to_dict(),
    )


def _put_storage_bytes(storage: LocalStorage, key: str, data: bytes) -> None:
    path = storage._path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _ctx(storage: LocalStorage | None = None, *, dry_run: bool = False, stop=None) -> StageContext:
    return StageContext(
        storage=storage,
        ffmpeg=None,
        max_kbps=96,
        dry_run=dry_run,
        stop=stop,
    )


class FakeBackend:
    """Mimics LiteLLMBackend's enqueue_batch/poll_batch contract (see
    `citypods.compute.llm.dispatch_job_batch`). enqueue_batch returns one outcome per submitted
    job (the
    constructor's ``outcome`` applied uniformly, or ``outcomes`` for a distinct result per job in
    submission order). poll_batch resolves only handles present in ``poll_results``; anything
    else comes back unresolved."""

    def __init__(
        self,
        outcome: JobHandle | JobResult | None = None,
        *,
        outcomes: list | None = None,
        poll_results: dict | None = None,
    ):
        self.outcome = outcome
        self._outcomes = outcomes
        self.enqueue_calls: list[list] = []
        self.submitted_jobs: list = []
        self.poll_calls: list[list] = []
        self._poll_results = poll_results or {}

    def enqueue_batch(self, jobs):
        jobs = list(jobs)
        self.enqueue_calls.append(jobs)
        self.submitted_jobs.extend(jobs)
        if self._outcomes is not None:
            return list(self._outcomes)
        return [self.outcome for _ in jobs]

    def poll_batch(self, handles):
        handles = list(handles)
        self.poll_calls.append(handles)
        return {h.ref: self._poll_results[h.ref] for h in handles if h.ref in self._poll_results}


SAMPLE_VTT = b"""WEBVTT

00:00:10.000 --> 00:00:20.000
Meeting is called to order by the chair.

00:00:30.000 --> 00:00:45.000
Moving on to the rejected item discussion.
"""


def test_locator_stage_skips_on_dry_run_or_missing_storage(tmp_path: Path):
    stage = ChapterBoundaryLocatorStage()
    city = _make_city()
    ep = _make_episode()

    # Dry run
    storage = LocalStorage(root=tmp_path / "s", url_prefix="https://cdn")
    ctx_dry = _ctx(storage=storage, dry_run=True)
    stats = stage.process(None, city, [ep], ctx_dry)
    assert stats.ran == 0
    assert not stats.errors

    # Missing storage
    ctx_no_store = _ctx(storage=None, dry_run=False)
    stats = stage.process(None, city, [ep], ctx_no_store)
    assert stats.ran == 0
    assert not stats.errors


def test_locator_stage_defers_when_agenda_not_completed(tmp_path: Path):
    stage = ChapterBoundaryLocatorStage()
    city = _make_city()
    storage = LocalStorage(root=tmp_path / "s", url_prefix="https://cdn")
    ctx = _ctx(storage=storage, dry_run=False)

    # Missing generated_agenda_candidates
    ep_none = _make_episode("ep-none")
    ep_none.generated_agenda_candidates = None

    # Status pending
    ep_pending = _make_episode("ep-pending")
    ep_pending.generated_agenda_candidates["status"] = "pending"

    stats = stage.process(None, city, [ep_none, ep_pending], ctx)
    assert stats.defer_reasons.get("agenda-not-complete") == 2


def test_locator_stage_defers_when_missing_timed_transcript(tmp_path: Path):
    stage = ChapterBoundaryLocatorStage()
    city = _make_city()
    storage = LocalStorage(root=tmp_path / "s", url_prefix="https://cdn")
    ctx = _ctx(storage=storage, dry_run=False)

    # No transcript in storage
    ep = _make_episode("ep-no-trans")
    stats = stage.process(None, city, [ep], ctx)
    assert stats.defer_reasons.get("missing-timed-transcript") == 1


def test_locator_stage_hashes_vtt_when_falling_back_from_empty_words(tmp_path: Path):
    stage = ChapterBoundaryLocatorStage()
    city = _make_city()
    storage = LocalStorage(root=tmp_path / "s", url_prefix="https://cdn")
    ep = _make_episode("ep-fallback")

    # words is valid JSON but has empty words array; falls back to VTT
    _put_storage_bytes(storage, ep.transcript_words_key, b'{"words": []}')
    _put_storage_bytes(storage, ep.transcript_key, SAMPLE_VTT)

    handle = JobHandle(
        task="agenda-chapter-locate",
        ref="job-ref-locator",
        recipe_hash="recipe-loc-1",
        backend="litellm",
    )
    backend = FakeBackend(handle)
    ctx = _ctx(storage=storage, dry_run=False)
    ctx.chapter_llm_backend = backend

    stats = stage.process(None, city, [ep], ctx)
    assert stats.defer_reasons.get("llm-pending") == 1
    assert len(backend.submitted_jobs) == 1
    job = backend.submitted_jobs[0]

    # Verifies transcript_hash used SAMPLE_VTT instead of empty words JSON
    expected_units, _ = build_locator_units(words_data=None, vtt_data=SAMPLE_VTT)
    agenda_obj = AgendaCandidatesArtifact.from_dict(ep.generated_agenda_candidates)
    expected_job = build_locator_job(
        episode_uid=ep.uid,
        agenda=agenda_obj,
        transcript_hash=hashlib.sha256(SAMPLE_VTT).hexdigest(),
        units=expected_units,
    )
    assert job.recipe_hash == expected_job.recipe_hash


def test_locator_stage_records_pending_on_job_handle(tmp_path: Path):
    stage = ChapterBoundaryLocatorStage()
    city = _make_city()
    storage = LocalStorage(root=tmp_path / "s", url_prefix="https://cdn")
    ep = _make_episode("ep-pending")
    _put_storage_bytes(storage, ep.transcript_key, SAMPLE_VTT)

    handle = JobHandle(
        task="agenda-chapter-locate",
        ref="job-ref-pending-1",
        recipe_hash="loc-recipe-pending",
        backend="litellm",
    )
    backend = FakeBackend(handle)
    ctx = _ctx(storage=storage, dry_run=False)
    ctx.chapter_llm_backend = backend

    stats = stage.process(None, city, [ep], ctx)
    assert stats.defer_reasons.get("llm-pending") == 1
    assert ep.generated_agenda_candidates["locator_status"] == "pending"
    assert ep.generated_agenda_candidates["locator_job_ref"] == "job-ref-pending-1"


def test_locator_stage_finalizes_and_filters_non_accepted_items(tmp_path: Path):
    stage = ChapterBoundaryLocatorStage()
    city = _make_city()
    storage = LocalStorage(root=tmp_path / "s", url_prefix="https://cdn")
    ep = _make_episode("ep-filter")
    _put_storage_bytes(storage, ep.transcript_key, SAMPLE_VTT)

    model_output = json.dumps(
        {
            "anchors": [
                {
                    "agenda_item_index": 0,  # accepted item
                    "unit_id": "u00001",
                    "transition_quote": "Meeting is called to order",
                    "confidence": 0.95,
                    "rationale": "Chair calls to order",
                },
                {
                    "agenda_item_index": 1,  # rejected item in agenda candidates
                    "unit_id": "u00002",
                    "transition_quote": "Moving on to rejected",
                    "confidence": 0.85,
                    "rationale": "Discussion begins",
                },
            ]
        }
    )
    result = JobResult(
        task="agenda-chapter-locate",
        recipe_hash="recipe-locator-result-777",
        output={"choices": [{"message": {"content": model_output}}]},
    )
    backend = FakeBackend(result)
    ctx = _ctx(storage=storage, dry_run=False)
    ctx.chapter_llm_backend = backend

    stats = stage.process(None, city, [ep], ctx)
    assert stats.ran == 1
    assert not stats.errors

    # Verify generated_chapters filters out rejected item index 1
    assert len(ep.generated_chapters) == 1
    assert ep.generated_chapters[0]["agenda_item_index"] == 0
    assert ep.generated_chapters[0]["title"] == "Call to order"
    assert ep.generated_chapters[0]["start"] == 10.0

    # Verify metadata and storage
    assert ep.generated_chapters_spec_hash == "recipe-locator-result-777"
    assert ep.generated_agenda_candidates["locator_status"] == "completed"
    boundary_key = ep.generated_agenda_candidates["boundary_artifact_key"]
    assert boundary_key.startswith("state/generated_chapters/boundary/ep-filter-")
    assert storage.exists(boundary_key)


def test_locator_stage_batches_multiple_episodes_into_one_enqueue_call(tmp_path: Path):
    """The actual point of this refactor (see review/44's 2026-08-18 incident retrospective):
    N episodes in one run must produce exactly one enqueue_batch call carrying all N jobs, not N
    separate single-job calls."""
    stage = ChapterBoundaryLocatorStage()
    city = _make_city()
    storage = LocalStorage(root=tmp_path / "s", url_prefix="https://cdn")

    episodes = [_make_episode(f"ep-batch-{i}") for i in range(3)]
    for ep in episodes:
        _put_storage_bytes(storage, ep.transcript_key, SAMPLE_VTT)

    handles = [
        JobHandle(
            task="agenda-chapter-locate",
            ref=f"job-ref-{i}",
            recipe_hash=f"loc-recipe-{i}",
            backend="llm-dispatch-v2",
        )
        for i in range(3)
    ]
    backend = FakeBackend(outcomes=handles)
    ctx = _ctx(storage=storage, dry_run=False)
    ctx.chapter_llm_backend = backend

    stats = stage.process(None, city, episodes, ctx)

    assert len(backend.enqueue_calls) == 1  # one call, not three
    assert len(backend.enqueue_calls[0]) == 3  # carrying all three jobs
    assert stats.defer_reasons.get("llm-pending") == 3
    for i, ep in enumerate(episodes):
        assert ep.generated_agenda_candidates["locator_job_ref"] == f"job-ref-{i}"


def test_locator_stage_defers_on_stop_signal(tmp_path: Path):
    stage = ChapterBoundaryLocatorStage()
    city = _make_city()
    storage = LocalStorage(root=tmp_path / "s", url_prefix="https://cdn")
    ep = _make_episode("ep-stop")
    _put_storage_bytes(storage, ep.transcript_key, SAMPLE_VTT)

    ctx = _ctx(storage=storage, dry_run=False, stop=lambda: True)
    stats = stage.process(None, city, [ep], ctx)
    assert stats.defer_reasons.get("stop-signal") == 1
    assert stats.ran == 0


def test_locator_stage_fingerprint_and_completion_helpers():
    stage = ChapterBoundaryLocatorStage()
    assert stage.version == CHAPTER_LOCATOR_PIPELINE_VERSION
    city = _make_city()
    ep = _make_episode("ep-helpers")

    # Fingerprint includes transcript keys, agenda artifact/recipe, and dynamic recipe
    fp1 = stage_input_fingerprint("chapter_locator", ep, city)
    ep.transcript_key = "transcripts/different.vtt"
    fp2 = stage_input_fingerprint("chapter_locator", ep, city)
    assert fp1 != fp2

    # Completion predicates
    assert not _legacy_stage_complete("chapter_locator", ep)
    ep.generated_agenda_candidates["locator_status"] = "completed"
    assert _legacy_stage_complete("chapter_locator", ep)
    ep.generated_agenda_candidates["locator_status"] = "rejected"
    assert _legacy_stage_complete("chapter_locator", ep)
    ep.generated_agenda_candidates["locator_status"] = "not_found"
    assert _legacy_stage_complete("chapter_locator", ep)
    ep.generated_agenda_candidates["locator_status"] = "pending"
    assert not _legacy_stage_complete("chapter_locator", ep)
    ep.generated_chapters_spec_hash = "spec-hash-123"
    assert _legacy_stage_complete("chapter_locator", ep)

    # Output pointer
    ep.generated_agenda_candidates = {"boundary_artifact_key": "state/gen/bound.json"}
    assert stage_output_pointer("chapter_locator", ep) == "state/gen/bound.json"
    ep.generated_agenda_candidates = {}
    ep.generated_chapters_spec_hash = "spec-1"
    assert stage_output_pointer("chapter_locator", ep) == "spec-1"


def test_locator_stage_short_circuits_storage_download_when_deferred_job_pending(tmp_path: Path):
    from citypods.compute.llm_deferred import write_deferred

    stage = ChapterBoundaryLocatorStage()
    city = _make_city()
    storage = LocalStorage(root=tmp_path / "s", url_prefix="https://cdn")
    ep = _make_episode("ep-locator-short-circuit")
    # Deliberately do NOT write transcript words or vtt to storage!
    # If the short-circuit fails, missing units would defer as missing-timed-transcript.

    handle = JobHandle(
        task="agenda-chapter-locate",
        ref="durable-loc-v2-ref-999",
        recipe_hash="loc-pending-recipe-123",
        backend="llm-dispatch-v2",
    )
    write_deferred(storage, "loc-pending-recipe-123", handle)

    ep.generated_agenda_candidates = {
        "status": "completed",
        "locator_status": "pending",
        "locator_recipe": "loc-pending-recipe-123",
        "locator_job_ref": "batch-pending:loc-pending-recipe-123",
    }
    backend = FakeBackend()
    ctx = _ctx(storage=storage, dry_run=False)
    ctx.chapter_llm_backend = backend

    stats = stage.process(None, city, [ep], ctx)
    assert stats.defer_reasons.get("llm-pending") == 1
    assert backend.enqueue_calls == []
    assert ep.generated_agenda_candidates["locator_job_ref"] == "durable-loc-v2-ref-999"


def test_locator_stage_enforces_producer_dispatch_cap(tmp_path: Path):
    stage = ChapterBoundaryLocatorStage()
    city = _make_city()
    storage = LocalStorage(root=tmp_path / "s", url_prefix="https://cdn")
    ep1 = _make_episode("ep-loc-cap-1")
    ep2 = _make_episode("ep-loc-cap-2")
    _put_storage_bytes(storage, ep1.transcript_key, SAMPLE_VTT)
    _put_storage_bytes(storage, ep2.transcript_key, SAMPLE_VTT)

    handle = JobHandle(
        task="agenda-chapter-locate",
        ref="loc-cap-ref",
        recipe_hash="cap-loc-recipe",
        backend="llm-dispatch-v2",
    )
    backend = FakeBackend(handle)
    ctx = _ctx(storage=storage, dry_run=False)
    ctx.chapter_locator_max_dispatches = 1
    ctx.chapter_llm_backend = backend

    stats = stage.process(None, city, [ep1, ep2], ctx)
    assert len(backend.submitted_jobs) == 1
    assert stats.defer_reasons.get("producer-cap") == 1
    assert stats.defer_reasons.get("llm-pending") == 1


def test_locator_stage_failed_batch_job_does_not_consume_dispatch_quota(tmp_path: Path):
    stage = ChapterBoundaryLocatorStage()
    city = _make_city()
    storage = LocalStorage(root=tmp_path / "s", url_prefix="https://cdn")
    ep = _make_episode("ep-loc-fail-dispatch")
    _put_storage_bytes(storage, ep.transcript_key, SAMPLE_VTT)

    backend = FakeBackend(RuntimeError("worker ingress 503"))
    ctx = _ctx(storage=storage, dry_run=False)
    ctx.chapter_locator_max_dispatches = 1
    ctx.chapter_llm_backend = backend

    stats = stage.process(None, city, [ep], ctx)
    assert len(stats.errors) == 1
    assert ctx.chapter_locator_dispatches_count == 0
    assert ctx.chapter_locator_dispatches_reserved == 0
    assert not ctx.chapter_locator_dispatch_exhausted.is_set()


def test_episode_needs_chapter_locator_evaluates_correctly():
    from citypods.stages import episode_needs_chapter_locator

    ep = _make_episode("ep-loc-eval")
    # ep has completed agenda and transcript keys
    assert episode_needs_chapter_locator(ep) is True

    # Missing transcript keys
    ep.transcript_key = ""
    ep.transcript_words_key = ""
    assert episode_needs_chapter_locator(ep) is False
    ep.transcript_key = "transcripts/t.vtt"

    # Agenda not completed
    ep.generated_agenda_candidates = {"status": "pending"}
    assert episode_needs_chapter_locator(ep) is False

    # Locator already completed or accepted
    ep.generated_agenda_candidates = {"status": "completed", "locator_status": "completed"}
    assert episode_needs_chapter_locator(ep) is False
    ep.generated_agenda_candidates = {"status": "completed", "locator_status": "accepted"}
    assert episode_needs_chapter_locator(ep) is False

    # Provider chapters present but not yet reconciled
    ep.source_chapters = [{"start": 0, "title": "Call to order"}]
    ep.generated_agenda_candidates = {"status": "completed", "locator_status": "pending"}
    assert episode_needs_chapter_locator(ep) is True

    # Provider chapters already reconciled to not_applicable
    ep.generated_agenda_candidates = {"status": "completed", "locator_status": "not_applicable"}
    assert episode_needs_chapter_locator(ep) is False
