"""Tests for ChapterBoundaryLocatorStage."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from citypods.chapter_artifacts import (
    AgendaCandidate,
    AgendaCandidatesArtifact,
)
from citypods.chapter_jobs import LOCATOR_MODELS, LOCATOR_REUSABLE_MODELS, build_locator_job
from citypods.chapter_locator import LOCATOR_ROUTING_VERSION, build_locator_units
from citypods.compute.base import JobHandle, JobResult
from citypods.models import City, Episode
from citypods.stages import (
    CHAPTER_LOCATOR_PIPELINE_VERSION,
    ChapterBoundaryLocatorStage,
    StageContext,
    StageStats,
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

    # A completed result records the model/prompt_version it was produced under, so a later
    # LOCATOR_PROMPT_VERSION/LOCATOR_MODEL bump can tell this artifact apart from a stale one
    # (see test_locator_stage_reprocesses_a_completed_episode_under_stale_policy below).
    from citypods.chapter_jobs import LOCATOR_MODEL, LOCATOR_PROMPT_VERSION

    assert ep.generated_agenda_candidates["locator_model"] == LOCATOR_MODEL
    assert ep.generated_agenda_candidates["locator_prompt_version"] == LOCATOR_PROMPT_VERSION
    assert ep.generated_agenda_candidates["locator_routing_version"] == LOCATOR_ROUTING_VERSION


@pytest.mark.parametrize(
    ("stale_field", "stale_value"),
    [
        ("locator_model", "gemini/gemini-3.5-flash-lite"),
        ("locator_prompt_version", "locator-v1"),
        ("locator_routing_version", "size-split-retired"),
        ("locator_routing_version", None),
    ],
)
def test_locator_stage_reprocesses_a_completed_episode_under_stale_policy(
    tmp_path: Path, stale_field: str, stale_value: str | None
):
    """Stale model, prompt, or routing metadata must not be re-stamped as current on reuse."""
    from citypods.chapter_jobs import LOCATOR_MODEL, LOCATOR_PROMPT_VERSION

    stage = ChapterBoundaryLocatorStage()
    city = _make_city()
    storage = LocalStorage(root=tmp_path / "s", url_prefix="https://cdn")
    ep = _make_episode("ep-stale-locator-completed")
    _put_storage_bytes(storage, ep.transcript_key, SAMPLE_VTT)
    ep.generated_agenda_candidates.update(
        {
            "locator_status": "completed",
            "locator_model": LOCATOR_MODEL,
            "locator_prompt_version": LOCATOR_PROMPT_VERSION,
            "locator_routing_version": LOCATOR_ROUTING_VERSION,
            "boundary_artifact_key": "state/generated_chapters/boundary/stale-key",
        }
    )
    if stale_value is None:
        ep.generated_agenda_candidates.pop(stale_field)
    else:
        ep.generated_agenda_candidates[stale_field] = stale_value

    model_output = json.dumps(
        {
            "anchors": [
                {
                    "agenda_item_index": 0,
                    "unit_id": "u00001",
                    "transition_quote": "Meeting is called to order",
                    "confidence": 0.95,
                    "rationale": "Chair calls to order",
                }
            ]
        }
    )
    result = JobResult(
        task="agenda-chapter-locate",
        recipe_hash="recipe-locator-fresh-1",
        output={"choices": [{"message": {"content": model_output}}]},
    )
    backend = FakeBackend(result)
    ctx = _ctx(storage=storage, dry_run=False)
    ctx.chapter_llm_backend = backend

    stats = stage.process(None, city, [ep], ctx)
    assert stats.ran == 1
    assert stats.reused == 0

    assert ep.generated_agenda_candidates["locator_model"] == LOCATOR_MODEL
    assert ep.generated_agenda_candidates["locator_prompt_version"] == LOCATOR_PROMPT_VERSION
    assert ep.generated_agenda_candidates["locator_routing_version"] == LOCATOR_ROUTING_VERSION


@pytest.mark.parametrize("model", LOCATOR_REUSABLE_MODELS)
@pytest.mark.parametrize("status", ["completed", "accepted"])
def test_locator_stage_reuses_a_completed_episode_that_is_already_current(
    tmp_path: Path, model: str, status: str
):
    from citypods.chapter_jobs import LOCATOR_PROMPT_VERSION

    stage = ChapterBoundaryLocatorStage()
    city = _make_city()
    storage = LocalStorage(root=tmp_path / "s", url_prefix="https://cdn")
    ep = _make_episode("ep-current-locator-completed")
    ep.generated_agenda_candidates.update(
        {
            "locator_status": status,
            "locator_model": model,
            "locator_prompt_version": LOCATOR_PROMPT_VERSION,
            "locator_routing_version": LOCATOR_ROUTING_VERSION,
        }
    )
    backend = FakeBackend()  # must never be called
    ctx = _ctx(storage=storage, dry_run=False)
    ctx.chapter_llm_backend = backend

    stats = stage.process(None, city, [ep], ctx)
    assert stats.reused == 1
    assert stats.ran == 0
    assert backend.enqueue_calls == []


def test_locator_stage_clears_pending_state_when_finalize_fails(tmp_path: Path):
    """A JobResult that fails finalize_locator_job (e.g. an out-of-range/hallucinated unit
    reference) must not leave the episode wedged in "pending" pointing at the same dead
    locator_recipe -- retrying it next run would just refetch the identical broken content and
    fail identically forever. Only the locator-owned fields should be cleared: chapter-agenda's
    own fields on the same dict must survive so that stage's own reuse check is unaffected.

    It also must not leave the underlying "completed" registry record in place -- see
    AgendaChapterCandidatesStage's own test_stage_clears_pending_state_when_finalize_fails for
    why: without deleting it, a future submission under this exact recipe would just replay the
    identical bad content forever via enqueue_batch's cached_completed short-circuit."""
    from citypods.compute.llm_deferred import look_up_deferred, write_deferred

    stage = ChapterBoundaryLocatorStage()
    city = _make_city()
    storage = LocalStorage(root=tmp_path / "s", url_prefix="https://cdn")
    ep = _make_episode("ep-broken-locator")
    _put_storage_bytes(storage, ep.transcript_key, SAMPLE_VTT)
    agenda_recipe_before = ep.generated_agenda_candidates["recipe"]
    ep.generated_agenda_candidates.update(
        {
            "locator_status": "pending",
            "locator_recipe": "recipe-locator-broken-1",
            "locator_job_ref": "ref-locator-broken-1",
        }
    )

    # unit_id "u09999" does not exist among this episode's locator units -- a hallucinated
    # reference finalize_locator_job's validate_locator_response rejects outright.
    model_output = json.dumps(
        {
            "anchors": [
                {
                    "agenda_item_index": 0,
                    "unit_id": "u09999",
                    "transition_quote": "Meeting is called to order",
                    "confidence": 0.95,
                    "rationale": "Chair calls to order",
                }
            ]
        }
    )
    result = JobResult(
        task="agenda-chapter-locate",
        recipe_hash="recipe-locator-broken-1",
        output={"choices": [{"message": {"content": model_output}}]},
    )
    # Mirrors what a real Worker dispatch would have durably recorded once this job resolved.
    write_deferred(storage, "recipe-locator-broken-1", result)
    backend = FakeBackend(result)
    ctx = _ctx(storage=storage, dry_run=False)
    ctx.chapter_llm_backend = backend

    stats = stage.process(None, city, [ep], ctx)
    assert stats.ran == 0
    assert len(stats.errors) == 1
    assert "locator_status" not in ep.generated_agenda_candidates
    assert "locator_recipe" not in ep.generated_agenda_candidates
    assert "locator_job_ref" not in ep.generated_agenda_candidates
    # chapter-agenda's own fields (produced by a different stage) must survive untouched.
    assert ep.generated_agenda_candidates["status"] == "completed"
    assert ep.generated_agenda_candidates["recipe"] == agenda_recipe_before
    assert look_up_deferred(storage, "recipe-locator-broken-1") is None


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


@pytest.mark.parametrize("model", LOCATOR_REUSABLE_MODELS)
def test_episode_needs_chapter_locator_evaluates_correctly(model: str):
    from citypods.chapter_jobs import LOCATOR_PROMPT_VERSION
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

    # Both selected models are reusable with the current prompt and routing versions.
    ep.generated_agenda_candidates = {
        "status": "completed",
        "locator_status": "completed",
        "locator_model": model,
        "locator_prompt_version": LOCATOR_PROMPT_VERSION,
        "locator_routing_version": LOCATOR_ROUTING_VERSION,
    }
    assert episode_needs_chapter_locator(ep) is False
    ep.generated_agenda_candidates = {
        "status": "completed",
        "locator_status": "accepted",
        "locator_model": model,
        "locator_prompt_version": LOCATOR_PROMPT_VERSION,
        "locator_routing_version": LOCATOR_ROUTING_VERSION,
    }
    assert episode_needs_chapter_locator(ep) is False

    # Missing or stale routing metadata must requeue even with the current model and prompt.
    del ep.generated_agenda_candidates["locator_routing_version"]
    assert episode_needs_chapter_locator(ep) is True
    ep.generated_agenda_candidates["locator_routing_version"] = "size-split-retired"
    assert episode_needs_chapter_locator(ep) is True
    ep.generated_agenda_candidates["locator_routing_version"] = LOCATOR_ROUTING_VERSION
    ep.generated_agenda_candidates["locator_model"] = "gemini/gemini-3.5-flash-lite"
    assert episode_needs_chapter_locator(ep) is True

    # Completed under a STALE locator_prompt_version -- this is the actual pre-filter `run.py`
    # applies before ChapterBoundaryLocatorStage.process() ever runs (--lane chapter-locator/
    # chapter); without this check a completed-but-stale episode would never reach that stage's
    # own is_current_locator_artifact check, silently defeating a LOCATOR_PROMPT_VERSION bump.
    ep.generated_agenda_candidates = {
        "status": "completed",
        "locator_status": "completed",
        "locator_model": model,
        "locator_prompt_version": "locator-v0-stale",
        "locator_routing_version": LOCATOR_ROUTING_VERSION,
    }
    assert episode_needs_chapter_locator(ep) is True

    # Completed with no recorded model/version at all (legacy data predating this check) is
    # treated the same as stale -- it cannot be confirmed current, so it is redone.
    ep.generated_agenda_candidates = {"status": "completed", "locator_status": "completed"}
    assert episode_needs_chapter_locator(ep) is True

    # Provider chapters present but not yet reconciled
    ep.source_chapters = [{"start": 0, "title": "Call to order"}]
    ep.generated_agenda_candidates = {"status": "completed", "locator_status": "pending"}
    assert episode_needs_chapter_locator(ep) is True

    # Provider chapters already reconciled to not_applicable
    ep.generated_agenda_candidates = {"status": "completed", "locator_status": "not_applicable"}
    assert episode_needs_chapter_locator(ep) is False


@pytest.mark.parametrize("change", ["unchanged", "transcript", "agenda", "policy", "legacy"])
def test_exhausted_locator_reopens_only_for_changed_identity(tmp_path, monkeypatch, change):
    storage = LocalStorage(root=tmp_path / "s", url_prefix="https://cdn")
    ep = _make_episode()
    _put_storage_bytes(storage, ep.transcript_key, SAMPLE_VTT)
    ep.generated_agenda_candidates.update(
        {"locator_retry_count": 1, "locator_retry_hint": "Repair conflicting anchors"}
    )
    invalid = JobResult(task="agenda-chapter-locate", recipe_hash="invalid", output={"anchors": []})
    backend = FakeBackend(invalid)
    ctx = _ctx(storage)
    ctx.chapter_llm_backend = backend
    stage = ChapterBoundaryLocatorStage()
    stage.process(None, _make_city(), [ep], ctx)
    assert ep.generated_agenda_candidates["locator_status"] == "retry_exhausted"
    assert ep.generated_agenda_candidates["locator_retry_policy"]
    assert ep.generated_agenda_candidates["locator_retry_input"]
    if change == "transcript":
        _put_storage_bytes(storage, ep.transcript_key, SAMPLE_VTT + b"\n")
    elif change == "agenda":
        ep.generated_agenda_candidates["items"][0]["title"] = "Opening remarks"
    elif change == "policy":
        monkeypatch.setattr("citypods.chapter_jobs.LOCATOR_ROUTING_VERSION", "next-policy")
    elif change == "legacy":
        ep.generated_agenda_candidates.pop("locator_retry_policy")
        ep.generated_agenda_candidates.pop("locator_retry_input")
    backend = FakeBackend(
        JobHandle(task="agenda-chapter-locate", ref="fresh", recipe_hash="fresh", backend="fake")
    )
    ctx = _ctx(storage)
    ctx.chapter_llm_backend = backend
    stats = stage.process(None, _make_city(), [ep], ctx)
    if change == "unchanged":
        assert not backend.submitted_jobs
        assert stats.defer_reasons["locator-repair-exhausted"] == 1
    else:
        assert len(backend.submitted_jobs) == 1
        assert ep.generated_agenda_candidates["locator_status"] == "pending"
        assert "locator_retry_count" not in ep.generated_agenda_candidates
        assert "locator_retry_hint" not in ep.generated_agenda_candidates
        assert "locator_retry_error" not in ep.generated_agenda_candidates


def test_old_completed_pending_recipe_consumes_current_dispatch_slot(tmp_path):
    from citypods.compute.llm_deferred import write_deferred

    storage = LocalStorage(root=tmp_path / "s", url_prefix="https://cdn")
    episodes = [_make_episode(f"old-{i}") for i in range(2)]
    for ep in episodes:
        _put_storage_bytes(storage, ep.transcript_key, SAMPLE_VTT)
        old_recipe = f"old-policy-{ep.uid}"
        ep.generated_agenda_candidates.update(
            {"locator_status": "pending", "locator_recipe": old_recipe}
        )
        write_deferred(
            storage,
            old_recipe,
            JobResult(task="agenda-chapter-locate", recipe_hash=old_recipe, output={}),
        )
    backend = FakeBackend(
        JobHandle(
            task="agenda-chapter-locate", ref="current", recipe_hash="current", backend="fake"
        )
    )
    ctx = _ctx(storage)
    ctx.chapter_llm_backend = backend
    ctx.chapter_locator_max_dispatches = 1
    stats = ChapterBoundaryLocatorStage().process(None, _make_city(), episodes, ctx)
    assert len(backend.submitted_jobs) == 1
    assert stats.defer_reasons["producer-cap"] == 1


@pytest.mark.parametrize("cached_kind", ["completed", "pending"])
@pytest.mark.parametrize("cached_first", [True, False])
def test_current_recipe_cache_does_not_spend_locator_cap(tmp_path, cached_kind, cached_first):
    from citypods.compute.llm_deferred import write_deferred

    storage = LocalStorage(root=tmp_path / "s", url_prefix="https://cdn")
    cached_ep = _make_episode("cached-without-locator-metadata")
    fresh_ep = _make_episode("fresh-locator")
    units, _ = build_locator_units(words_data=None, vtt_data=SAMPLE_VTT)
    job = build_locator_job(
        episode_uid=cached_ep.uid,
        agenda=AgendaCandidatesArtifact.from_dict(cached_ep.generated_agenda_candidates),
        transcript_hash=hashlib.sha256(SAMPLE_VTT).hexdigest(),
        units=units,
    )
    if cached_kind == "completed":
        cached = JobResult(
            task=job.task,
            recipe_hash=job.recipe_hash,
            output={"choices": [{"message": {"content": '{"anchors": []}'}}]},
        )
    else:
        cached = JobHandle(
            task=job.task, recipe_hash=job.recipe_hash, ref="already-queued", backend="fake"
        )
    write_deferred(storage, job.recipe_hash, cached)
    fresh = JobHandle(task=job.task, recipe_hash="fresh", ref="newly-queued", backend="fake")
    episodes = [cached_ep, fresh_ep] if cached_first else [fresh_ep, cached_ep]
    outcomes = [cached, fresh] if cached_first else [fresh, cached]
    for ep in episodes:
        _put_storage_bytes(storage, ep.transcript_key, SAMPLE_VTT)
    backend = FakeBackend(outcomes=outcomes)
    ctx = _ctx(storage)
    ctx.chapter_llm_backend = backend
    ctx.chapter_locator_max_dispatches = 1

    stats = ChapterBoundaryLocatorStage().process(None, _make_city(), episodes, ctx)

    assert len(backend.submitted_jobs) == 2
    assert not stats.errors
    assert not stats.defer_reasons.get("producer-cap")
    assert ctx.chapter_locator_dispatches_count == 1
    assert ctx.chapter_locator_dispatches_reserved == 0
    assert fresh_ep.generated_agenda_candidates["locator_status"] == "pending"
    assert cached_ep.generated_agenda_candidates["locator_status"] == (
        "completed" if cached_kind == "completed" else "pending"
    )


def test_client_only_deferred_locator_still_requires_dispatch_slot(tmp_path, monkeypatch):
    storage = LocalStorage(root=tmp_path / "s", url_prefix="https://cdn")
    ep = _make_episode("client-deferred")
    _put_storage_bytes(storage, ep.transcript_key, SAMPLE_VTT)
    deferred = JobHandle(
        task="agenda-chapter-locate",
        recipe_hash="client-deferred",
        ref="not-submitted",
        backend="fake",
        deferred_request=object(),
    )
    monkeypatch.setattr("citypods.compute.llm_deferred.look_up_deferred", lambda *_: deferred)
    ctx = _ctx(storage)
    ctx.chapter_llm_backend = FakeBackend()
    ctx.chapter_locator_max_dispatches = 0

    stats = ChapterBoundaryLocatorStage().process(None, _make_city(), [ep], ctx)

    assert not ctx.chapter_llm_backend.submitted_jobs
    assert stats.defer_reasons["producer-cap"] == 1
    assert ctx.chapter_locator_dispatches_count == 0
    assert ctx.chapter_locator_dispatches_reserved == 0


@pytest.mark.parametrize(
    ("prefix", "cap", "event"),
    [
        ("tag", "tag_max_dispatches", "tag_llm_dispatch_exhausted"),
        ("chapter_agenda", "chapter_agenda_max_dispatches", "chapter_agenda_dispatch_exhausted"),
        ("chapter_locator", "chapter_locator_max_dispatches", "chapter_locator_dispatch_exhausted"),
    ],
)
def test_unsettled_reservations_do_not_permanently_close_lane(prefix, cap, event):
    ctx = _ctx()
    setattr(ctx, cap, 1)
    reserve = getattr(ctx, f"reserve_{prefix}_dispatch")
    settle = getattr(ctx, f"settle_{prefix}_dispatch")
    assert reserve()
    assert not reserve()
    assert not getattr(ctx, event).is_set()
    settle(False)
    assert reserve()
    settle(True)
    assert getattr(ctx, event).is_set()
    assert not reserve()


def test_locator_stage_defers_without_error_when_no_accepted_agenda_items(tmp_path: Path):
    stage = ChapterBoundaryLocatorStage()
    city = _make_city()
    storage = LocalStorage(root=tmp_path / "s", url_prefix="https://cdn")
    ctx = _ctx(storage=storage, dry_run=False)

    ep = _make_episode("ep-no-accepted")
    raw = dict(ep.generated_agenda_candidates)
    raw["items"] = [{**item, "status": "rejected"} for item in raw["items"]]
    ep.generated_agenda_candidates = raw
    # Transcript present: the deferral must not depend on the transcript check.
    _put_storage_bytes(storage, ep.transcript_key, SAMPLE_VTT)

    stats = stage.process(None, city, [ep], ctx)
    assert stats.defer_reasons.get("no-accepted-agenda-items") == 1
    assert not stats.errors


# --- review/58 WP8: MiniMax overflow, over-cap deferral, retired V4.1 work ----------------------


def test_v41_left_the_lane_but_its_completed_results_stay_reusable():
    from citypods.chapter_locator import DEEPSEEK_V41_LOCATOR_MODEL, MINIMAX_LOCATOR_MODEL

    assert DEEPSEEK_V41_LOCATOR_MODEL not in LOCATOR_MODELS
    assert MINIMAX_LOCATOR_MODEL in LOCATOR_MODELS
    assert DEEPSEEK_V41_LOCATOR_MODEL in LOCATOR_REUSABLE_MODELS


def test_v4_flash_band_job_is_unchanged_by_wp8():
    """A <=76k packet keeps its model, output reserve and routing version, so neither its accepted
    results nor its pending submissions go stale (the recipe hash parts are unchanged)."""
    ep = _make_episode("ep-recipe")
    agenda = AgendaCandidatesArtifact.from_dict(ep.generated_agenda_candidates)
    units, _ = build_locator_units(words_data=None, vtt_data=SAMPLE_VTT)
    job = build_locator_job(episode_uid=ep.uid, agenda=agenda, transcript_hash="t", units=units)
    assert LOCATOR_ROUTING_VERSION == "size-split-v1"
    assert job.inputs["llm_policy"].allowed_models == ("deepseek/deepseek-v4-flash",)
    assert job.inputs["max_tokens"] == 20_000


def _shrink_bands(monkeypatch, *, v4_max: int, cap: int) -> None:
    import citypods.chapter_locator as chapter_locator

    monkeypatch.setattr(chapter_locator, "DEEPSEEK_V4_FLASH_SAFE_INPUT_TOKENS", v4_max)
    monkeypatch.setattr(chapter_locator, "LOCATOR_OVERFLOW_MAX_INPUT_TOKENS", cap)


def _pending_handle(ref: str = "r") -> JobHandle:
    return JobHandle(task="agenda-chapter-locate", ref=ref, recipe_hash="r", backend="fake")


def test_locator_stage_sends_overflow_band_to_minimax(tmp_path, monkeypatch):
    from citypods.chapter_locator import MINIMAX_LOCATOR_MODEL

    _shrink_bands(monkeypatch, v4_max=10, cap=1_000_000)
    storage = LocalStorage(root=tmp_path / "s", url_prefix="https://cdn")
    ep = _make_episode("ep-overflow")
    _put_storage_bytes(storage, ep.transcript_key, SAMPLE_VTT)
    backend = FakeBackend(_pending_handle())
    ctx = _ctx(storage)
    ctx.chapter_llm_backend = backend

    ChapterBoundaryLocatorStage().process(None, _make_city(), [ep], ctx)

    (job,) = backend.submitted_jobs
    assert job.inputs["llm_policy"].allowed_models == (MINIMAX_LOCATOR_MODEL,)
    assert job.inputs["max_tokens"] == 12_800
    assert job.inputs["max_tokens_mode"] == "route_max"


def test_locator_stage_holds_over_cap_packets_as_deferred(tmp_path, monkeypatch):
    _shrink_bands(monkeypatch, v4_max=10, cap=20)
    storage = LocalStorage(root=tmp_path / "s", url_prefix="https://cdn")
    ep = _make_episode("ep-over-cap")
    _put_storage_bytes(storage, ep.transcript_key, SAMPLE_VTT)
    before = json.loads(json.dumps(ep.generated_agenda_candidates))
    backend = FakeBackend(_pending_handle())
    ctx = _ctx(storage)
    ctx.chapter_llm_backend = backend
    ctx.chapter_locator_max_dispatches = 5

    stats = ChapterBoundaryLocatorStage().process(None, _make_city(), [ep], ctx)

    # Deferred, not failed: counted, nothing submitted, no error, no slot spent, no state change.
    assert stats.defer_reasons == {"locator-over-input-cap": 1}
    assert stats.defer_samples and stats.defer_samples[0].startswith("ep-over-cap: ")
    assert not stats.errors
    assert backend.enqueue_calls == []
    assert ctx.chapter_locator_dispatches_count == 0
    assert ctx.chapter_locator_dispatches_reserved == 0
    assert ep.generated_agenda_candidates == before

    # Raising the cap makes the same episode eligible on the next run, with no other change.
    _shrink_bands(monkeypatch, v4_max=10, cap=1_000_000)
    stats = ChapterBoundaryLocatorStage().process(None, _make_city(), [ep], ctx)
    assert len(backend.submitted_jobs) == 1
    assert stats.defer_reasons == {"llm-pending": 1}


class CancellingBackend(FakeBackend):
    def __init__(self, outcome=None, *, cancel_result=None):
        super().__init__(outcome)
        self.cancel_calls: list[list[str]] = []
        self.cancel_result = cancel_result or {}

    def cancel_batch(self, refs):
        self.cancel_calls.append(list(refs))
        return self.cancel_result


def _pending_on(storage, ep, *, model, ref="old-v41-ref", deferred_request=None) -> str:
    from citypods.compute.llm_deferred import write_deferred

    old_recipe = f"old-recipe-{ep.uid}"
    write_deferred(
        storage,
        old_recipe,
        JobHandle(
            task="agenda-chapter-locate",
            recipe_hash=old_recipe,
            backend="llm-dispatch-v2",
            ref=ref,
            model=model,
            deferred_request=deferred_request,
        ),
    )
    ep.generated_agenda_candidates.update(
        {"locator_status": "pending", "locator_recipe": old_recipe, "locator_job_ref": ref}
    )
    return old_recipe


def test_queued_job_on_a_retired_model_is_cancelled_and_replanned(tmp_path):
    from citypods.compute.llm_deferred import look_up_deferred

    storage = LocalStorage(root=tmp_path / "s", url_prefix="https://cdn")
    ep = _make_episode("ep-v41-queued")
    _put_storage_bytes(storage, ep.transcript_key, SAMPLE_VTT)
    old_recipe = _pending_on(storage, ep, model="deepseek/deepseek-v4.1-flash")
    backend = CancellingBackend(
        _pending_handle("new-ref"),
        cancel_result={"cancelled": ["old-v41-ref"], "in_flight": [], "not_found": []},
    )
    ctx = _ctx(storage)
    ctx.chapter_llm_backend = backend

    stats = ChapterBoundaryLocatorStage().process(None, _make_city(), [ep], ctx)

    assert backend.cancel_calls == [["old-v41-ref"]]
    assert look_up_deferred(storage, old_recipe) is None
    (job,) = backend.submitted_jobs
    assert job.recipe_hash != old_recipe
    assert set(job.inputs["llm_policy"].allowed_models) <= set(LOCATOR_MODELS)
    assert ep.generated_agenda_candidates["locator_status"] == "pending"
    assert ep.generated_agenda_candidates["locator_recipe"] == job.recipe_hash
    assert not stats.errors


@pytest.mark.parametrize("outcome", ["in_flight", "not_found"])
def test_retired_model_job_not_cancellable_keeps_waiting(tmp_path, outcome):
    from citypods.compute.llm_deferred import look_up_deferred

    storage = LocalStorage(root=tmp_path / "s", url_prefix="https://cdn")
    ep = _make_episode("ep-v41-in-flight")
    old_recipe = _pending_on(storage, ep, model="deepseek/deepseek-v4.1-flash")
    backend = CancellingBackend(cancel_result={"cancelled": [], outcome: ["old-v41-ref"]})
    ctx = _ctx(storage)
    ctx.chapter_llm_backend = backend

    stats = ChapterBoundaryLocatorStage().process(None, _make_city(), [ep], ctx)

    assert backend.cancel_calls == [["old-v41-ref"]]
    assert backend.enqueue_calls == []
    assert isinstance(look_up_deferred(storage, old_recipe), JobHandle)
    assert ep.generated_agenda_candidates["locator_status"] == "pending"
    assert stats.defer_reasons == {"llm-pending": 1}


@pytest.mark.parametrize("model", [*LOCATOR_MODELS, None])
def test_pending_job_on_a_lane_model_is_never_cancelled(tmp_path, model):
    storage = LocalStorage(root=tmp_path / "s", url_prefix="https://cdn")
    ep = _make_episode("ep-lane-pending")
    _pending_on(storage, ep, model=model)
    backend = CancellingBackend(cancel_result={"cancelled": ["old-v41-ref"]})
    ctx = _ctx(storage)
    ctx.chapter_llm_backend = backend

    stats = ChapterBoundaryLocatorStage().process(None, _make_city(), [ep], ctx)

    assert backend.cancel_calls == []
    assert backend.enqueue_calls == []
    assert stats.defer_reasons == {"llm-pending": 1}


def test_unsubmitted_retired_capsule_is_left_to_the_deferred_sweep(tmp_path):
    from citypods.compute.llm_policy import DeferredLLMRequest, LLMRequestPolicy

    storage = LocalStorage(root=tmp_path / "s", url_prefix="https://cdn")
    ep = _make_episode("ep-v41-capsule")
    _pending_on(
        storage,
        ep,
        model="deepseek/deepseek-v4.1-flash",
        ref="deferred-capsule-x",
        deferred_request=DeferredLLMRequest(
            messages=({"role": "user", "content": "x"},),
            policy=LLMRequestPolicy(allowed_models=("deepseek/deepseek-v4.1-flash",)),
        ),
    )
    backend = CancellingBackend(cancel_result={"cancelled": ["deferred-capsule-x"]})
    ctx = _ctx(storage)
    ctx.chapter_llm_backend = backend

    stats = ChapterBoundaryLocatorStage().process(None, _make_city(), [ep], ctx)

    assert backend.cancel_calls == []
    assert stats.defer_reasons == {"llm-pending": 1}


def test_failed_cancellation_keeps_the_episode_waiting(tmp_path):
    storage = LocalStorage(root=tmp_path / "s", url_prefix="https://cdn")
    ep = _make_episode("ep-v41-cancel-error")
    _pending_on(storage, ep, model="deepseek/deepseek-v4.1-flash")

    class Broken(CancellingBackend):
        def cancel_batch(self, refs):
            raise RuntimeError("worker 503")

    backend = Broken()
    ctx = _ctx(storage)
    ctx.chapter_llm_backend = backend

    stats = ChapterBoundaryLocatorStage().process(None, _make_city(), [ep], ctx)

    assert backend.enqueue_calls == []
    assert ep.generated_agenda_candidates["locator_status"] == "pending"
    assert stats.defer_reasons == {"llm-pending": 1}
    assert stats.errors == ["retired-model locator cancellation: worker 503"]


# --- review/58 §12.3: stale-recipe results are metadata, not work -------------------------------

_GEMINI_STALE = {
    "locator_model": "gemini/gemini-3.5-flash-lite",
    "locator_prompt_version": "locator-v2",
    "locator_routing_version": LOCATOR_ROUTING_VERSION,
}


def _anchor_output(*indices: int) -> dict:
    anchors = [
        {
            "agenda_item_index": index,
            "unit_id": f"u0000{index + 1}",
            "transition_quote": "Meeting is called to order",
            "confidence": 0.9,
            "rationale": "start",
        }
        for index in indices
    ]
    return {"choices": [{"message": {"content": json.dumps({"anchors": anchors})}}]}


def _completed_then_overwritten(tmp_path: Path, *, anchors=(0,), uid="ep-repair"):
    """Produce a genuinely current locator result through the stage itself, then overwrite its
    record metadata with the stale values the chapter-agenda lane used to write back."""
    stage = ChapterBoundaryLocatorStage()
    city = _make_city()
    storage = LocalStorage(root=tmp_path / "s", url_prefix="https://cdn")
    ep = _make_episode(uid)
    _put_storage_bytes(storage, ep.transcript_key, SAMPLE_VTT)
    result = JobResult(
        task="agenda-chapter-locate", recipe_hash=f"recipe-{uid}", output=_anchor_output(*anchors)
    )
    ctx = _ctx(storage=storage)
    ctx.chapter_llm_backend = FakeBackend(result)
    stats = stage.process(None, city, [ep], ctx)
    assert stats.ran == 1 and not stats.errors
    # In production a result arrives through the pending path, which records its recipe; this
    # direct-result double skips that step.
    ep.generated_agenda_candidates["locator_recipe"] = ep.generated_chapters_spec_hash
    ep.generated_agenda_candidates.update(_GEMINI_STALE)
    return stage, city, storage, ep


def _rerun(stage, city, storage, ep, **ctx_kwargs):
    ctx = _ctx(storage=storage, **ctx_kwargs)
    backend = FakeBackend(_pending_handle("replanned"))
    ctx.chapter_llm_backend = backend
    stats = stage.process(None, city, [ep], ctx)
    return stats, backend, ctx


def _kimi_policy() -> str:
    from citypods.chapter_locator import DEEPSEEK_FREE_LOCATOR_MODEL, KIMI_LOCATOR_MODEL
    from citypods.stages import _locator_policy_fingerprint

    return _locator_policy_fingerprint([DEEPSEEK_FREE_LOCATOR_MODEL, KIMI_LOCATOR_MODEL])


def test_proven_current_result_is_restamped_without_an_llm_call(tmp_path):
    from citypods.chapter_jobs import LOCATOR_MODEL, LOCATOR_PROMPT_VERSION
    from citypods.stages import episode_needs_chapter_locator

    stage, city, storage, ep = _completed_then_overwritten(tmp_path)
    chapters_before = list(ep.generated_chapters)
    assert episode_needs_chapter_locator(ep) is True

    stats, backend, ctx = _rerun(stage, city, storage, ep)

    assert backend.enqueue_calls == []
    assert stats.reused == 1 and stats.ran == 0 and stats.skipped == 0
    assert stats.quality_counts == {"locator-metadata-repaired": 1}
    raw = ep.generated_agenda_candidates
    assert raw["locator_model"] == LOCATOR_MODEL
    assert raw["locator_prompt_version"] == LOCATOR_PROMPT_VERSION
    assert raw["locator_routing_version"] == LOCATOR_ROUTING_VERSION
    assert ep.generated_chapters == chapters_before  # the result itself is untouched
    assert episode_needs_chapter_locator(ep) is False
    purpose = ctx.llm_work.snapshot()["purposes"]["chapter-locator"]
    assert purpose["states"] == {"reused": 1}
    assert purpose["outcomes"] == {"metadata-repaired": 1}


def test_repair_is_idempotent(tmp_path):
    stage, city, storage, ep = _completed_then_overwritten(tmp_path)
    _rerun(stage, city, storage, ep)
    stamped = dict(ep.generated_agenda_candidates)
    stats, backend, _ = _rerun(stage, city, storage, ep)
    assert backend.enqueue_calls == []
    assert stats.reused == 1
    assert stats.quality_counts == {}  # the plain reuse check takes it now
    assert ep.generated_agenda_candidates == stamped


def test_retired_v41_result_is_restamped_with_its_own_model(tmp_path):
    from citypods.chapter_locator import DEEPSEEK_V41_LOCATOR_MODEL

    stage, city, storage, ep = _completed_then_overwritten(tmp_path, uid="ep-v41")
    key = ep.generated_agenda_candidates["boundary_artifact_key"]
    boundary = json.loads(storage._path(key).read_bytes())
    boundary["model"] = DEEPSEEK_V41_LOCATOR_MODEL
    _put_storage_bytes(storage, key, json.dumps(boundary).encode())
    for chapter in ep.generated_chapters:
        chapter["model"] = DEEPSEEK_V41_LOCATOR_MODEL

    stats, backend, _ = _rerun(stage, city, storage, ep)
    assert backend.enqueue_calls == []
    assert stats.quality_counts == {"locator-metadata-repaired": 1}
    assert ep.generated_agenda_candidates["locator_model"] == DEEPSEEK_V41_LOCATOR_MODEL


def test_genuinely_stale_result_is_not_restamped_and_is_planned(tmp_path):
    """Old prompt version in the stored chapters (and the boundary file): no repair, re-plan."""
    stage, city, storage, ep = _completed_then_overwritten(tmp_path, uid="ep-old")
    key = ep.generated_agenda_candidates["boundary_artifact_key"]
    boundary = json.loads(storage._path(key).read_bytes())
    boundary["prompt_version"] = "locator-v2"
    _put_storage_bytes(storage, key, json.dumps(boundary).encode())
    for chapter in ep.generated_chapters:
        chapter["prompt_version"] = "locator-v2"

    stats, backend, ctx = _rerun(stage, city, storage, ep)

    assert stats.quality_counts == {"locator-metadata-not-provable": 1}
    assert len(backend.submitted_jobs) == 1  # planned normally
    assert stats.defer_reasons == {"llm-pending": 1}
    raw = ep.generated_agenda_candidates
    assert raw["locator_status"] == "pending"
    assert raw["locator_model"] == _GEMINI_STALE["locator_model"]
    outcomes = ctx.llm_work.snapshot()["purposes"]["chapter-locator"]["outcomes"]
    assert outcomes == {"metadata-not-provable": 1}


def test_stale_prompt_in_stored_chapters_alone_blocks_the_repair(tmp_path):
    stage, city, storage, ep = _completed_then_overwritten(tmp_path, uid="ep-old-chapters")
    ep.generated_chapters[0]["prompt_version"] = "locator-v2"
    stats, backend, _ = _rerun(stage, city, storage, ep)
    assert stats.quality_counts == {"locator-metadata-not-provable": 1}
    assert len(backend.submitted_jobs) == 1


@pytest.mark.parametrize("mismatch", ["transcript", "agenda", "spec_hash", "missing_file", "key"])
def test_result_without_matching_evidence_is_not_restamped(tmp_path, mismatch):
    stage, city, storage, ep = _completed_then_overwritten(tmp_path, uid=f"ep-{mismatch}")
    raw = ep.generated_agenda_candidates
    if mismatch == "transcript":
        _put_storage_bytes(storage, ep.transcript_key, SAMPLE_VTT + b"\n")
    elif mismatch == "agenda":
        raw["recipe"] = "recipe-agenda-2"
    elif mismatch == "spec_hash":
        ep.generated_chapters_spec_hash = "other"
    elif mismatch == "missing_file":
        storage._path(raw["boundary_artifact_key"]).unlink()
    else:
        raw["boundary_artifact_key"] = "state/generated_chapters/boundary/elsewhere.json"
    stats, backend, _ = _rerun(stage, city, storage, ep)
    assert stats.quality_counts == {"locator-metadata-not-provable": 1}
    assert ep.generated_agenda_candidates["locator_model"] == _GEMINI_STALE["locator_model"]
    assert len(backend.submitted_jobs) == 1


def test_no_chapter_result_needs_the_retry_policy_fingerprint(tmp_path):
    # Anchor only on the rejected item: a completed result with zero chapters.
    stage, city, storage, ep = _completed_then_overwritten(tmp_path, anchors=(1,), uid="ep-none")
    assert ep.generated_chapters == []
    stats, backend, _ = _rerun(stage, city, storage, ep)
    assert stats.quality_counts == {"locator-metadata-not-provable": 1}
    assert len(backend.submitted_jobs) == 1


def test_no_chapter_result_with_a_current_policy_fingerprint_is_restamped(tmp_path):
    from citypods.chapter_jobs import LOCATOR_PROMPT_VERSION

    stage, city, storage, ep = _completed_then_overwritten(tmp_path, anchors=(1,), uid="ep-nc")
    ep.generated_agenda_candidates["locator_retry_policy"] = _kimi_policy()
    stats, backend, _ = _rerun(stage, city, storage, ep)
    assert backend.enqueue_calls == []
    assert stats.quality_counts == {"locator-metadata-repaired": 1}
    assert ep.generated_agenda_candidates["locator_prompt_version"] == LOCATOR_PROMPT_VERSION


def test_unknown_routing_needs_the_policy_fingerprint(tmp_path):
    stage, city, storage, ep = _completed_then_overwritten(tmp_path, uid="ep-routing")
    ep.generated_agenda_candidates["locator_routing_version"] = None
    stats, _, _ = _rerun(stage, city, storage, ep)
    assert stats.quality_counts == {"locator-metadata-not-provable": 1}

    stage, city, storage, ep = _completed_then_overwritten(tmp_path / "b", uid="ep-routing-b")
    ep.generated_agenda_candidates["locator_routing_version"] = None
    ep.generated_agenda_candidates["locator_retry_policy"] = _kimi_policy()
    stats, _, _ = _rerun(stage, city, storage, ep)
    assert stats.quality_counts == {"locator-metadata-repaired": 1}


def test_repair_switch_off_plans_normally(tmp_path, monkeypatch):
    from citypods.stages import LOCATOR_METADATA_REPAIR_ENV

    monkeypatch.setenv(LOCATOR_METADATA_REPAIR_ENV, "0")
    stage, city, storage, ep = _completed_then_overwritten(tmp_path, uid="ep-off")
    stats, backend, _ = _rerun(stage, city, storage, ep)
    assert stats.quality_counts == {}
    assert len(backend.submitted_jobs) == 1


def test_repair_respects_the_stop_budget(tmp_path):
    stage, city, storage, ep = _completed_then_overwritten(tmp_path, uid="ep-stop")
    stats, backend, _ = _rerun(stage, city, storage, ep, stop=lambda: True)
    assert stats.defer_reasons == {"stop-signal": 1}
    assert stats.quality_counts == {}
    assert backend.enqueue_calls == []
    assert ep.generated_agenda_candidates["locator_model"] == _GEMINI_STALE["locator_model"]


def test_stale_metadata_makes_the_stage_dirty_despite_a_clean_marker(tmp_path):
    from citypods.stages import _mark_stage_complete, stage_is_dirty

    stage, city, storage, ep = _completed_then_overwritten(tmp_path, uid="ep-marker")
    _mark_stage_complete(stage, [ep], city, StageStats(stage.name))
    assert stage_is_dirty(stage, ep, city) is True  # stale metadata: ready work, so dirty
    _rerun(stage, city, storage, ep)
    _mark_stage_complete(stage, [ep], city, StageStats(stage.name))
    assert stage_is_dirty(stage, ep, city) is False


def _zero_agenda(ep: Episode) -> None:
    raw = dict(ep.generated_agenda_candidates)
    raw["items"] = [{**item, "status": "rejected"} for item in raw["items"]]
    ep.generated_agenda_candidates = raw


def test_zero_agenda_episode_is_not_ready_and_its_result_is_untouched(tmp_path):
    from citypods.stages import episode_needs_chapter_locator

    stage = ChapterBoundaryLocatorStage()
    city = _make_city()
    storage = LocalStorage(root=tmp_path / "s", url_prefix="https://cdn")
    ep = _make_episode("ep-zero")
    _zero_agenda(ep)
    ep.generated_agenda_candidates.update({"locator_status": "completed", **_GEMINI_STALE})
    before = dict(ep.generated_agenda_candidates)
    _put_storage_bytes(storage, ep.transcript_key, SAMPLE_VTT)
    assert episode_needs_chapter_locator(ep) is False

    ctx = _ctx(storage=storage)
    ctx.chapter_llm_backend = backend = FakeBackend()
    stage.census(city, [ep], ctx)
    purpose = ctx.llm_work.snapshot()["purposes"]["chapter-locator"]
    assert purpose["states"] == {"blocked": 1}
    assert purpose["backlog"] == 0
    assert purpose["outcomes"] == {"zero-agenda": 1}

    stats = stage.process(None, city, [ep], ctx)
    assert stats.quality_counts == {"locator-zero-agenda": 1}
    assert backend.enqueue_calls == []
    assert ep.generated_agenda_candidates == before


def test_census_ready_count_matches_the_lane_prefilter(tmp_path):
    from citypods.chapter_jobs import LOCATOR_MODEL, LOCATOR_PROMPT_VERSION
    from citypods.stages import episode_needs_chapter_locator

    stage = ChapterBoundaryLocatorStage()
    city = _make_city()
    storage = LocalStorage(root=tmp_path / "s", url_prefix="https://cdn")
    fresh = _make_episode("ep-fresh")
    stale = _make_episode("ep-stale")
    stale.generated_agenda_candidates.update({"locator_status": "completed", **_GEMINI_STALE})
    current = _make_episode("ep-current")
    current.generated_agenda_candidates.update(
        {
            "locator_status": "completed",
            "locator_model": LOCATOR_MODEL,
            "locator_prompt_version": LOCATOR_PROMPT_VERSION,
            "locator_routing_version": LOCATOR_ROUTING_VERSION,
        }
    )
    zero = _make_episode("ep-zero-2")
    _zero_agenda(zero)
    episodes = [fresh, stale, current, zero]
    ctx = _ctx(storage=storage)
    stage.census(city, episodes, ctx)
    states = ctx.llm_work.snapshot()["purposes"]["chapter-locator"]["states"]
    assert states == {"ready": 2, "reused": 1, "blocked": 1}
    assert sum(episode_needs_chapter_locator(ep) for ep in episodes) == states["ready"]


def test_stage_activity_event_carries_quality_counts(monkeypatch):
    from citypods.compute import llm_submission_telemetry as telemetry

    events = []
    monkeypatch.setattr(telemetry, "record", lambda event, **fields: events.append(fields))
    telemetry.record_stage_activity(
        lane="chapter-locator",
        stage="chapter_locator",
        ran=0,
        reused=3,
        backlog=0,
        errors=0,
        seconds=1.0,
        defer_reasons={},
        quality_counts={"locator-metadata-repaired": 2, "locator-zero-agenda": 1},
    )
    assert events[0]["quality_counts"] == {
        "locator-metadata-repaired": 2,
        "locator-zero-agenda": 1,
    }
