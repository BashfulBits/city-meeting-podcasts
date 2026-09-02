"""Tests for AgendaChapterCandidatesStage."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from citypods.compute.base import JobHandle, JobResult
from citypods.models import City, Episode
from citypods.stages import (
    CHAPTER_AGENDA_PIPELINE_VERSION,
    AgendaChapterCandidatesStage,
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
    return Episode(
        guid=f"guid-{uid}",
        uid=uid,
        title="City Council Regular Meeting",
        published=datetime(2026, 8, 1, tzinfo=UTC),
        video_url="https://example.com/video.mp4",
        media_kind="direct",
        body="City Council",
        links={"agenda_text_artifact_key": f"documents/austin-tx/{uid}/agenda-text-abc12345"},
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
    `citypods.compute.llm.dispatch_job_batch`), not the older per-job run_inference/reconcile one
    it replaced.

    enqueue_batch returns one outcome per submitted job: either the constructor's single
    ``outcome`` applied uniformly (every current test submits at most one job per stage.process
    call, except the batching test below), or ``outcomes`` for a distinct result per job in
    submission order. poll_batch resolves only handles present in ``poll_results`` (a dict keyed
    by handle ref); anything else comes back unresolved -- matching either a v1-backed handle the
    real poll_batch would have filtered out internally, or a v2 handle genuinely still pending.
    """

    def __init__(
        self,
        outcome: JobHandle | JobResult | None = None,
        *,
        outcomes: list | None = None,
        poll_results: dict | None = None,
    ):
        self.outcome = outcome
        self._outcomes = outcomes
        self.enqueue_calls: list[list] = []  # one entry per enqueue_batch call
        self.submitted_jobs: list = []  # flattened across every enqueue_batch call
        self.poll_calls: list[list] = []  # one entry per poll_batch call
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


def test_stage_skips_on_dry_run_or_missing_storage(tmp_path: Path):
    stage = AgendaChapterCandidatesStage()
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


def test_stage_defers_on_missing_agenda_artifact(tmp_path: Path):
    stage = AgendaChapterCandidatesStage()
    city = _make_city()
    storage = LocalStorage(root=tmp_path / "s", url_prefix="https://cdn")
    ctx = _ctx(storage=storage, dry_run=False)

    # Episodes with no key and with missing bytes -- two distinct failure modes, kept as separate
    # defer reasons (diagnostic split, see AgendaChapterCandidatesStage.process) so a live run's
    # aggregate breakdown doesn't conflate "never had a link" with "link present but unreadable".
    ep_no_key = _make_episode("ep-no-key")
    ep_no_key.links = {}
    ep_missing_bytes = _make_episode("ep-missing-bytes")
    stats = stage.process(None, city, [ep_no_key, ep_missing_bytes], ctx)
    assert stats.defer_reasons.get("missing-agenda-artifact") == 1
    assert stats.defer_reasons.get("agenda-artifact-key-present-but-unreadable") == 1
    assert stats.skipped == 2


def test_stage_uses_provider_chapters_and_never_enqueues_fallback_work(tmp_path: Path):
    stage = AgendaChapterCandidatesStage()
    city = _make_city()
    storage = LocalStorage(root=tmp_path / "s", url_prefix="https://cdn")
    ep = _make_episode("ep-provider-chapters")
    ep.source_chapters = [{"start": 0, "title": "Provider call to order"}]
    ep.generated_agenda_candidates = {
        "status": "pending",
        "recipe": "obsolete-recipe",
        "job_ref": "obsolete-v2-ref",
    }
    ep.generated_chapters = [{"start": 0, "title": "Old generated title"}]
    backend = FakeBackend()
    ctx = _ctx(storage=storage, dry_run=False)
    ctx.chapter_llm_backend = backend

    stats = stage.process(None, city, [ep], ctx)

    assert backend.enqueue_calls == []
    assert stats.reused == 1
    assert ep.generated_agenda_candidates == {
        "status": "not_applicable",
        "reason": "provider_chapters",
        "provider_chapter_count": 1,
        "locator_status": "not_applicable",
    }
    assert ep.generated_chapters == []


def test_stage_records_pending_on_job_handle(tmp_path: Path):
    stage = AgendaChapterCandidatesStage()
    city = _make_city()
    storage = LocalStorage(root=tmp_path / "s", url_prefix="https://cdn")
    ep = _make_episode("ep-pending")
    key = ep.links["agenda_text_artifact_key"]
    _put_storage_bytes(storage, key, b"1. Call to order\n2. Public hearing")

    handle = JobHandle(
        task="agenda-item-extract",
        ref="job-ref-12345",
        recipe_hash="recipe-hash-123",
        backend="litellm",
    )
    backend = FakeBackend(handle)
    ctx = _ctx(storage=storage, dry_run=False)
    ctx.chapter_llm_backend = backend

    stats = stage.process(None, city, [ep], ctx)
    assert stats.defer_reasons.get("llm-pending") == 1
    assert ep.generated_agenda_candidates is not None
    assert ep.generated_agenda_candidates["status"] == "pending"
    assert ep.generated_agenda_candidates["job_ref"] == "job-ref-12345"
    assert ep.generated_agenda_candidates["model"] == "mistral/mistral-medium-latest"


def test_stage_does_not_resolve_stale_v1_handle_via_poll(tmp_path: Path):
    """Regression test for the 2026-08-18 polling-storm incident: a pending JobHandle left over
    from before LLM_DISPATCH_V2_URL was wired in (backend != "llm-dispatch-v2") must not be
    treated as resolved by the batched reconcile pass. The real poll_batch (citypods/compute/
    llm.py) filters to v2-backed handles internally before ever sending a v1 GET request -- this
    FakeBackend mirrors that by only resolving refs present in poll_results, so a stale handle
    passed in (as any pending handle now is, batched) simply comes back unresolved, still
    "llm-pending", with no separate per-handle skip logic needed in the stage itself."""
    stage = AgendaChapterCandidatesStage()
    city = _make_city()
    storage = LocalStorage(root=tmp_path / "s", url_prefix="https://cdn")
    ep = _make_episode("ep-stale-v1")
    key = ep.links["agenda_text_artifact_key"]
    _put_storage_bytes(storage, key, b"1. Call to order\n2. Public hearing")

    stale_handle = JobHandle(
        task="agenda-item-extract",
        ref="chatcmpl-stale-v1",
        recipe_hash="recipe-hash-stale-v1",
        backend="litellm",  # a pre-v2 handle, not "llm-dispatch-v2"
    )
    backend = FakeBackend(stale_handle)  # no poll_results entry for this ref -> stays pending
    ctx = _ctx(storage=storage, dry_run=False)
    ctx.chapter_llm_backend = backend

    stats = stage.process(None, city, [ep], ctx)
    assert stats.defer_reasons.get("llm-pending") == 1
    assert ep.generated_agenda_candidates["job_ref"] == "chatcmpl-stale-v1"


def test_stage_reconciles_fresh_v2_handle(tmp_path: Path):
    """A handle backed by v2 is still reconciled inline (one batched poll_batch call) -- and if
    it resolves immediately, the stage finalizes it in the same pass rather than waiting a full
    extra run."""
    stage = AgendaChapterCandidatesStage()
    city = _make_city()
    storage = LocalStorage(root=tmp_path / "s", url_prefix="https://cdn")
    ep = _make_episode("ep-fresh-v2")
    key = ep.links["agenda_text_artifact_key"]
    agenda_text = "1. Call to order"
    _put_storage_bytes(storage, key, agenda_text.encode("utf-8"))

    v2_handle = JobHandle(
        task="agenda-item-extract",
        ref="job-v2-abc",
        recipe_hash="recipe-hash-v2-abc",
        backend="llm-dispatch-v2",
    )
    model_output = json.dumps(
        {
            "items": [
                {
                    "display_ref": "1",
                    "title": "Call to order",
                    "evidence_quote": "1. Call to order",
                    "line_start": 1,
                    "line_end": 1,
                }
            ]
        }
    )
    resolved = JobResult(
        task="agenda-item-extract",
        recipe_hash="recipe-hash-v2-abc",
        output={"choices": [{"message": {"content": model_output}}]},
    )
    backend = FakeBackend(v2_handle, poll_results={v2_handle.ref: resolved})
    ctx = _ctx(storage=storage, dry_run=False)
    ctx.chapter_llm_backend = backend

    stats = stage.process(None, city, [ep], ctx)
    assert len(backend.poll_calls) == 1
    assert backend.poll_calls[0] == [v2_handle]
    assert stats.ran == 1
    assert ep.generated_agenda_candidates["status"] == "completed"


def test_stage_batches_multiple_episodes_into_one_enqueue_call(tmp_path: Path):
    """The actual point of this refactor (see review/44's 2026-08-18 incident retrospective):
    N episodes in one run must produce exactly one enqueue_batch call carrying all N jobs, not N
    separate single-job calls -- that per-job dispatch pattern was exactly the Worker-request
    volume the incident flagged. Uses ``outcomes`` to give each episode a distinct JobHandle so
    the per-episode result mapping (order preserved through the batch call) is verified too, not
    just the call count."""
    stage = AgendaChapterCandidatesStage()
    city = _make_city()
    storage = LocalStorage(root=tmp_path / "s", url_prefix="https://cdn")

    episodes = [_make_episode(f"ep-batch-{i}") for i in range(3)]
    for i, ep in enumerate(episodes):
        _put_storage_bytes(storage, ep.links["agenda_text_artifact_key"], f"1. Item {i}".encode())

    handles = [
        JobHandle(
            task="agenda-item-extract",
            ref=f"job-ref-{i}",
            recipe_hash=f"recipe-{i}",
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
        assert ep.generated_agenda_candidates["job_ref"] == f"job-ref-{i}"


def test_stage_finalizes_and_writes_artifact_on_job_result(tmp_path: Path):
    stage = AgendaChapterCandidatesStage()
    city = _make_city()
    storage = LocalStorage(root=tmp_path / "s", url_prefix="https://cdn")
    ep = _make_episode("ep-result")
    key = ep.links["agenda_text_artifact_key"]
    agenda_text = "1. Call to order\n2. Public comment"
    _put_storage_bytes(storage, key, agenda_text.encode("utf-8"))

    model_output = json.dumps(
        {
            "items": [
                {
                    "display_ref": "1",
                    "title": "Call to order",
                    "evidence_quote": "1. Call to order",
                    "line_start": 1,
                    "line_end": 1,
                },
                {
                    "display_ref": "2",
                    "title": "Public comment",
                    "evidence_quote": "2. Public comment",
                    "line_start": 2,
                    "line_end": 2,
                },
            ]
        }
    )
    result = JobResult(
        task="agenda-item-extract",
        recipe_hash="recipe-agenda-result-999",
        output={"choices": [{"message": {"content": model_output}}]},
    )
    backend = FakeBackend(result)
    ctx = _ctx(storage=storage, dry_run=False)
    ctx.chapter_llm_backend = backend

    stats = stage.process(None, city, [ep], ctx)
    assert stats.ran == 1
    assert not stats.errors
    assert ep.generated_agenda_candidates is not None
    assert ep.generated_agenda_candidates["status"] == "completed"
    assert ep.generated_agenda_candidates["recipe"] == "recipe-agenda-result-999"
    assert len(ep.generated_agenda_candidates["items"]) == 2
    artifact_key = ep.generated_agenda_candidates["artifact_key"]
    assert artifact_key.startswith("state/generated_chapters/agenda/ep-result-")
    assert storage.exists(artifact_key)


def test_stage_defers_on_stop_signal(tmp_path: Path):
    stage = AgendaChapterCandidatesStage()
    city = _make_city()
    storage = LocalStorage(root=tmp_path / "s", url_prefix="https://cdn")
    ep = _make_episode("ep-stop")
    key = ep.links["agenda_text_artifact_key"]
    _put_storage_bytes(storage, key, b"1. Call to order")

    ctx = _ctx(storage=storage, dry_run=False, stop=lambda: True)
    stats = stage.process(None, city, [ep], ctx)
    assert stats.defer_reasons.get("stop-signal") == 1
    assert stats.ran == 0


def test_stage_fingerprint_and_completion_helpers():
    stage = AgendaChapterCandidatesStage()
    assert stage.version == CHAPTER_AGENDA_PIPELINE_VERSION
    city = _make_city()
    ep = _make_episode("ep-helpers")

    # Fingerprint includes agenda_artifact and dynamic recipe
    fp1 = stage_input_fingerprint("chapter_agenda", ep, city)
    ep.links["agenda_text_artifact_key"] = "documents/austin-tx/ep-helpers/agenda-text-diff"
    fp2 = stage_input_fingerprint("chapter_agenda", ep, city)
    assert fp1 != fp2

    # Completion predicates
    assert not _legacy_stage_complete("chapter_agenda", ep)
    ep.generated_agenda_candidates = {"status": "completed"}
    assert _legacy_stage_complete("chapter_agenda", ep)
    ep.generated_agenda_candidates = {"status": "accepted"}
    assert _legacy_stage_complete("chapter_agenda", ep)
    ep.generated_agenda_candidates = {"status": "not_found"}
    assert _legacy_stage_complete("chapter_agenda", ep)
    ep.generated_agenda_candidates = {"status": "rejected"}
    assert _legacy_stage_complete("chapter_agenda", ep)
    ep.generated_agenda_candidates = {"status": "pending"}
    assert not _legacy_stage_complete("chapter_agenda", ep)

    # Output pointer
    ep.generated_agenda_candidates = {"artifact_key": "state/gen/key.json", "recipe": "r1"}
    assert stage_output_pointer("chapter_agenda", ep) == "state/gen/key.json"
    ep.generated_agenda_candidates = {"recipe": "r1"}
    assert stage_output_pointer("chapter_agenda", ep) == "r1"
