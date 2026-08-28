from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from citypods.compute.base import JobHandle, JobResult
from citypods.compute.llm import LLMDispatchTerminalError, LLMStructuredOutputError
from citypods.compute.llm_deferred import DeferredSnapshot, DeferredSnapshotEntry
from citypods.compute.llm_policy import DeferredLLMRequest, LLMRequestPolicy
from scripts import llm_deferred_sweep


def _handle(recipe_hash: str) -> JobHandle:
    return JobHandle(
        task="tag", recipe_hash=recipe_hash, backend="litellm", ref="deferred:" + recipe_hash
    )


def _snapshot(handles):
    return DeferredSnapshot(
        [
            DeferredSnapshotEntry(
                key=f"state/llm_deferred/{handle.recipe_hash}.json",
                last_modified=None,
                data={"status": "pending", "created_at": "2026-01-01T00:00:00+00:00"},
                decoded=handle,
            )
            for handle in handles
        ]
    )


def test_sweep_registers_known_contracts_before_reconciling():
    """The sweep runs as its own process, separate from whatever Stage originally submitted a
    pending "tag" job -- citypods.compute.structured's registry is a plain in-memory dict, so
    response_model("topic-tags") only resolves if something in *this* process registered it.
    Without _register_known_contracts(), every pending "tag" record would fail to reconcile
    forever (caught per-record, logged, never actually completed) until its 38-day TTL expired."""
    from citypods.compute.structured import response_model

    llm_deferred_sweep._register_known_contracts()

    assert response_model("topic-tags") is not None
    assert response_model("moment-extraction") is not None


def test_sweep_reports_zero_when_storage_is_unavailable(monkeypatch, capsys):
    monkeypatch.setattr(llm_deferred_sweep, "load_site_config", lambda *_: {"defaults": {}})
    monkeypatch.setattr(llm_deferred_sweep, "make_storage", lambda *_args, **_kwargs: None)

    assert llm_deferred_sweep.main([]) == 0
    assert "nothing to do" in capsys.readouterr().err


def test_sweep_reconciles_pending_records_and_prunes(monkeypatch, capsys):
    monkeypatch.setattr(llm_deferred_sweep, "load_site_config", lambda *_: {"defaults": {}})
    fake_storage = SimpleNamespace(cas_capable=True)
    monkeypatch.setattr(llm_deferred_sweep, "make_storage", lambda *_args, **_kwargs: fake_storage)

    handles = [_handle("recipe-1"), _handle("recipe-2"), _handle("recipe-3")]
    monkeypatch.setattr(
        llm_deferred_sweep, "load_deferred_snapshot", lambda _storage: _snapshot(handles)
    )
    prune_kwargs = {}

    def _capturing_prune(_storage, _snapshot, **kw):
        prune_kwargs.update(kw)
        return 2

    monkeypatch.setattr(
        llm_deferred_sweep,
        "prune_expired_deferred_snapshot",
        _capturing_prune,
    )

    results = {
        "recipe-1": JobResult(task="tag", recipe_hash="recipe-1", output={}, model="m"),
        "recipe-2": None,  # still pending
    }

    class FakeBackend:
        def __init__(self, *_args, **_kwargs):
            pass

        def reconcile(self, handle):
            if handle.recipe_hash == "recipe-3":
                raise RuntimeError("boom")
            return results[handle.recipe_hash]

    monkeypatch.setattr(llm_deferred_sweep, "LiteLLMBackend", FakeBackend)

    assert llm_deferred_sweep.main([]) == 0
    out = capsys.readouterr()
    assert "3 pending seen" in out.out
    assert "1 completed" in out.out
    assert "1 still pending observations" in out.out
    assert "1 failed" in out.out
    assert "2 pruned" in out.out
    assert "recipe-3" in out.err
    # Verify the active backend was propagated to prune_expired_deferred_snapshot.
    assert "backend" in prune_kwargs
    assert isinstance(prune_kwargs["backend"], FakeBackend)


def test_sweep_recovers_terminal_and_malformed_dispatch_records(monkeypatch, capsys):
    monkeypatch.setattr(llm_deferred_sweep, "load_site_config", lambda *_: {"defaults": {}})
    fake_storage = SimpleNamespace(cas_capable=True)
    monkeypatch.setattr(llm_deferred_sweep, "make_storage", lambda *_args, **_kwargs: fake_storage)
    handles = [_handle("recipe-502"), _handle("recipe-malformed")]
    monkeypatch.setattr(
        llm_deferred_sweep, "load_deferred_snapshot", lambda _storage: _snapshot(handles)
    )
    monkeypatch.setattr(
        llm_deferred_sweep,
        "prune_expired_deferred_snapshot",
        lambda _storage, _snapshot, **_kw: 0,
    )
    recovered = []
    monkeypatch.setattr(
        llm_deferred_sweep,
        "discard_terminal_failure",
        lambda _storage, _snapshot, handle, error, **_kw: (
            recovered.append((handle.recipe_hash, type(error).__name__)) or 1
        ),
    )
    monkeypatch.setattr(llm_deferred_sweep, "schema_correction_attempted", lambda *_args: False)
    corrections = []
    events = []

    def _record_correction(_storage, handle, _error):
        events.append("marker")
        corrections.append(handle.recipe_hash)

    monkeypatch.setattr(
        llm_deferred_sweep,
        "record_schema_correction",
        _record_correction,
    )
    rewritten = []
    monkeypatch.setattr(
        llm_deferred_sweep,
        "write_deferred",
        lambda _storage, recipe_hash, handle: (
            events.append("write") or rewritten.append((recipe_hash, handle.ref))
        ),
    )

    class FakeBackend:
        def __init__(self, *_args, **_kwargs):
            pass

        def reconcile(self, handle):
            if handle.recipe_hash == "recipe-502":
                raise LLMDispatchTerminalError("LLM dispatch poll returned HTTP 502")
            raise LLMStructuredOutputError(
                "structured dispatched response failed Pydantic validation"
            )

        def retry_malformed_dispatched(self, handle):
            return JobHandle(
                task=handle.task,
                recipe_hash=handle.recipe_hash,
                backend=handle.backend,
                ref="corrected:" + handle.recipe_hash,
                structured_output=handle.structured_output,
                model=handle.model,
            )

        def delete_dispatched_ref(self, _ref):
            events.append("delete")

        def ack_dispatched_ref(self, _handle):
            events.append("ack")

    monkeypatch.setattr(llm_deferred_sweep, "LiteLLMBackend", FakeBackend)

    assert llm_deferred_sweep.main([]) == 0
    out = capsys.readouterr()
    assert recovered == [("recipe-502", "LLMDispatchTerminalError")]
    assert corrections == ["recipe-malformed"]
    assert rewritten == [("recipe-malformed", "corrected:recipe-malformed")]
    assert events == ["write", "marker", "ack", "delete"]
    assert "2 failed (1 terminally recovered)" in out.out
    assert "submitted one schema correction" in out.err


def test_sweep_exhausts_a_second_malformed_reply_without_submitting_another_correction(
    monkeypatch, capsys
):
    monkeypatch.setattr(llm_deferred_sweep, "load_site_config", lambda *_: {"defaults": {}})
    storage = SimpleNamespace(cas_capable=True)
    monkeypatch.setattr(llm_deferred_sweep, "make_storage", lambda *_args, **_kwargs: storage)
    monkeypatch.setattr(
        llm_deferred_sweep,
        "load_deferred_snapshot",
        lambda _storage: _snapshot([_handle("recipe-1")]),
    )
    monkeypatch.setattr(
        llm_deferred_sweep, "prune_expired_deferred_snapshot", lambda *_args, **_kwargs: 0
    )
    monkeypatch.setattr(llm_deferred_sweep, "schema_correction_attempted", lambda *_args: True)
    exhausted = []
    monkeypatch.setattr(
        llm_deferred_sweep,
        "discard_terminal_failure",
        lambda _storage, _snapshot, handle, _error, **kwargs: (
            exhausted.append((handle.recipe_hash, kwargs["exhausted"])) or 2
        ),
    )

    class FakeBackend:
        def __init__(self, *_args, **_kwargs):
            pass

        def reconcile(self, _handle):
            raise LLMStructuredOutputError(
                "structured dispatched response failed Pydantic validation"
            )

        def retry_malformed_dispatched(self, _handle):
            raise AssertionError("a second malformed response must not be corrected again")

    monkeypatch.setattr(llm_deferred_sweep, "LiteLLMBackend", FakeBackend)

    assert llm_deferred_sweep.main([]) == 0
    out = capsys.readouterr()
    assert exhausted == [("recipe-1", True)]
    assert "1 failed (1 terminally recovered)" in out.out
    assert "malformed correction exhausted" in out.err


def test_sweep_skips_same_capacity_cohort_after_no_fit(monkeypatch, capsys):
    monkeypatch.setattr(llm_deferred_sweep, "load_site_config", lambda *_: {"defaults": {}})
    fake_storage = SimpleNamespace(cas_capable=True)
    monkeypatch.setattr(llm_deferred_sweep, "make_storage", lambda *_args, **_kwargs: fake_storage)
    monkeypatch.setattr(
        llm_deferred_sweep,
        "prune_expired_deferred_snapshot",
        lambda _storage, _snapshot, **_kw: 0,
    )

    policy = LLMRequestPolicy(
        allowed_models=("gemini/gemini-3.1-flash-lite",), purpose="generic-llm-work"
    )
    handles = [
        JobHandle(
            task="tag",
            recipe_hash=f"recipe-{idx}",
            backend="litellm",
            ref=f"deferred:recipe-{idx}",
            structured_output="topic-tags",
            deferred_request=DeferredLLMRequest(
                messages=({"role": "user", "content": "meeting text"},), policy=policy
            ),
        )
        for idx in range(3)
    ]
    monkeypatch.setattr(
        llm_deferred_sweep, "load_deferred_snapshot", lambda _storage: _snapshot(handles)
    )

    reconciled = []

    class FakeBackend:
        def __init__(self, *_args, **_kwargs):
            pass

        def reconcile(self, handle):
            reconciled.append(handle.recipe_hash)
            return None

    monkeypatch.setattr(llm_deferred_sweep, "LiteLLMBackend", FakeBackend)

    assert llm_deferred_sweep.main([]) == 0
    assert reconciled == ["recipe-0"]
    out = capsys.readouterr()
    assert "1 still pending observations" in out.out
    assert "2 further record(s) skipped" in out.err


def test_sweep_skips_a_different_purpose_sharing_the_same_exhausted_route_pool(monkeypatch):
    """The capacity-exhaustion cache is keyed on the resolved route pool, not on
    (task, structured_output, purpose) -- a second feature drawing on the exact same
    ``allowed_models`` must benefit from the first feature's already-proven exhaustion instead of
    independently re-discovering it record by record."""
    monkeypatch.setattr(llm_deferred_sweep, "load_site_config", lambda *_: {"defaults": {}})
    fake_storage = SimpleNamespace(cas_capable=True)
    monkeypatch.setattr(llm_deferred_sweep, "make_storage", lambda *_args, **_kwargs: fake_storage)
    monkeypatch.setattr(
        llm_deferred_sweep,
        "prune_expired_deferred_snapshot",
        lambda _storage, _snapshot, **_kw: 0,
    )

    shared_models = ("gemini/gemini-3.1-flash-lite", "gemini/gemini-3.5-flash-lite")

    def _handle_for(recipe_hash, *, task, structured_output, purpose):
        policy = LLMRequestPolicy(allowed_models=shared_models, purpose=purpose)
        return JobHandle(
            task=task,
            recipe_hash=recipe_hash,
            backend="litellm",
            ref=f"deferred:{recipe_hash}",
            structured_output=structured_output,
            deferred_request=DeferredLLMRequest(
                messages=({"role": "user", "content": "meeting text"},), policy=policy
            ),
        )

    handles = [
        _handle_for(
            "recipe-tag", task="tag", structured_output="topic-tags", purpose="generic-llm-work"
        ),
        _handle_for(
            "recipe-classify",
            task="classify",
            structured_output="civic-platforms",
            purpose="civic-platforms",
        ),
    ]
    monkeypatch.setattr(
        llm_deferred_sweep, "load_deferred_snapshot", lambda _storage: _snapshot(handles)
    )

    reconciled = []

    class FakeBackend:
        def __init__(self, *_args, **_kwargs):
            pass

        def reconcile(self, handle):
            reconciled.append(handle.recipe_hash)
            return None

    monkeypatch.setattr(llm_deferred_sweep, "LiteLLMBackend", FakeBackend)

    assert llm_deferred_sweep.main([]) == 0
    # Only the first record (whichever feature it came from) pays for discovering the pool is
    # exhausted -- the second is skipped purely on the shared route pool, despite its different
    # task/structured_output/purpose.
    assert reconciled == ["recipe-tag"]


def test_sweep_does_not_skip_durable_topic_tag_submissions(monkeypatch):
    monkeypatch.setattr(llm_deferred_sweep, "load_site_config", lambda *_: {"defaults": {}})
    fake_storage = SimpleNamespace(cas_capable=True)
    monkeypatch.setattr(llm_deferred_sweep, "make_storage", lambda *_args, **_kwargs: fake_storage)
    monkeypatch.setattr(
        llm_deferred_sweep,
        "prune_expired_deferred_snapshot",
        lambda _storage, _snapshot, **_kw: 0,
    )
    policy = LLMRequestPolicy(
        allowed_models=("gemini/gemini-3.1-flash-lite",), purpose="topic-tags"
    )
    handles = [
        JobHandle(
            task="tag",
            recipe_hash=f"recipe-{idx}",
            backend="litellm",
            ref=f"deferred:recipe-{idx}",
            deferred_request=DeferredLLMRequest(
                messages=({"role": "user", "content": "meeting text"},), policy=policy
            ),
        )
        for idx in range(3)
    ]
    monkeypatch.setattr(
        llm_deferred_sweep, "load_deferred_snapshot", lambda _storage: _snapshot(handles)
    )

    reconciled = []

    class FakeBackend:
        def __init__(self, *_args, **_kwargs):
            pass

        def reconcile(self, handle):
            assert handle.deferred_request.policy.queue_only is True
            reconciled.append(handle.recipe_hash)
            return None

    monkeypatch.setattr(llm_deferred_sweep, "LiteLLMBackend", FakeBackend)

    assert llm_deferred_sweep.main([]) == 0
    assert reconciled == ["recipe-0", "recipe-1", "recipe-2"]


def test_capacity_signature_normalizes_none_allowed_models_to_every_route():
    handle = JobHandle(
        task="tag",
        recipe_hash="recipe-1",
        backend="litellm",
        ref="deferred:recipe-1",
        deferred_request=DeferredLLMRequest(
            messages=({"role": "user", "content": "meeting text"},),
            policy=LLMRequestPolicy(allowed_models=None),
        ),
    )
    from citypods.compute.llm_policy import ROUTES

    signature = llm_deferred_sweep._capacity_signature(handle)
    assert signature == (frozenset(ROUTES), False)


def test_sweep_overrides_stale_deferred_deadline_for_retries():
    original = _handle("recipe-deadline")
    old_deadline = datetime(2026, 1, 1, tzinfo=UTC)
    sweep_deadline = datetime(2026, 7, 24, 12, tzinfo=UTC)
    original = JobHandle(
        **{
            **original.__dict__,
            "deferred_request": DeferredLLMRequest(
                messages=({"role": "user", "content": "meeting text"},),
                policy=LLMRequestPolicy(deadline_at=old_deadline),
            ),
        }
    )

    updated = llm_deferred_sweep._with_sweep_deadline(original, sweep_deadline)

    assert updated.deferred_request.policy.deadline_at == sweep_deadline
    assert original.deferred_request.policy.deadline_at == old_deadline


def test_sweep_upgrades_legacy_topic_tag_deferral_to_durable_queue():
    original = JobHandle(
        task="tag",
        recipe_hash="legacy-tag",
        backend="litellm",
        ref="deferred:legacy-tag",
        deferred_request=DeferredLLMRequest(
            messages=({"role": "user", "content": "meeting text"},),
            policy=LLMRequestPolicy(
                purpose="topic-tags",
                deadline_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
        ),
    )

    upgraded = llm_deferred_sweep._with_sweep_deadline(
        original, datetime(2026, 7, 24, 12, tzinfo=UTC)
    )

    assert upgraded.deferred_request.policy.queue_only is True
    assert upgraded.deferred_request.policy.deadline_at is None
    assert original.deferred_request.policy.queue_only is False


def test_sweep_upgrades_other_resumable_production_llm_work_but_not_city_onboarding():
    deadline = datetime(2026, 7, 24, 12, tzinfo=UTC)

    def handle(purpose: str) -> JobHandle:
        return JobHandle(
            task="agenda-item-extract",
            recipe_hash=purpose,
            backend="litellm",
            ref="deferred:" + purpose,
            deferred_request=DeferredLLMRequest(
                messages=({"role": "user", "content": "meeting text"},),
                policy=LLMRequestPolicy(purpose=purpose, deadline_at=deadline),
            ),
        )

    chapter = llm_deferred_sweep._with_sweep_deadline(handle("chapter-agenda"), deadline)
    city = llm_deferred_sweep._with_sweep_deadline(handle("city-onboarding"), deadline)

    assert chapter.deferred_request.policy.queue_only is True
    assert chapter.deferred_request.policy.deadline_at is None
    assert city.deferred_request.policy.queue_only is False
    assert city.deferred_request.policy.deadline_at == deadline


def test_sweep_batch_polls_v2_handles(monkeypatch):
    monkeypatch.setattr(llm_deferred_sweep, "load_site_config", lambda *_: {"defaults": {}})
    fake_storage = SimpleNamespace(
        cas_capable=True,
        get_file=lambda *args, **kwargs: False,
        put_file=lambda *args, **kwargs: None,
        delete=lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(llm_deferred_sweep, "make_storage", lambda *_args, **_kwargs: fake_storage)

    v2_handle = JobHandle(
        task="tag",
        recipe_hash="v2-recipe",
        backend="llm-dispatch-v2",
        ref="j1",
    )
    monkeypatch.setattr(
        llm_deferred_sweep,
        "load_deferred_snapshot",
        lambda *_: _snapshot([v2_handle]),
    )
    monkeypatch.setattr(llm_deferred_sweep, "prune_expired_failure_markers", lambda *_: None)

    poll_batch_called_with = []

    class MockBackend:
        def poll_batch(self, handles):
            poll_batch_called_with.extend(handles)
            return {h.ref: None for h in handles}

        def reconcile(self, handle):
            return None

    monkeypatch.setattr(llm_deferred_sweep, "LiteLLMBackend", lambda *_, **__: MockBackend())

    assert llm_deferred_sweep.main([]) == 0
    assert len(poll_batch_called_with) == 1
    assert poll_batch_called_with[0].ref == "j1"


def test_sweep_skips_reconcile_for_all_handles_observed_by_batch_poll(monkeypatch):
    # Regression test for the singleton-poll storm: a successful batch poll is authoritative for
    # both completed and still-pending v2 handles.  Neither may immediately go through
    # reconcile(), or a sweep with N pending jobs becomes one batch request plus N singletons.
    monkeypatch.setattr(llm_deferred_sweep, "load_site_config", lambda *_: {"defaults": {}})
    fake_storage = SimpleNamespace(
        cas_capable=True,
        get_file=lambda *args, **kwargs: False,
        put_file=lambda *args, **kwargs: None,
        delete=lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(llm_deferred_sweep, "make_storage", lambda *_args, **_kwargs: fake_storage)

    resolved_handle = JobHandle(
        task="tag", recipe_hash="resolved-recipe", backend="llm-dispatch-v2", ref="resolved-id"
    )
    pending_handle = JobHandle(
        task="tag", recipe_hash="pending-recipe", backend="llm-dispatch-v2", ref="pending-id"
    )
    monkeypatch.setattr(
        llm_deferred_sweep,
        "load_deferred_snapshot",
        lambda *_: _snapshot([resolved_handle, pending_handle]),
    )
    monkeypatch.setattr(llm_deferred_sweep, "prune_expired_failure_markers", lambda *_: None)

    reconciled_refs = []
    resolved_result = JobResult(task="tag", recipe_hash="resolved-recipe", output={"ok": True})

    class MockBackend:
        def poll_batch(self, handles):
            return {h.ref: (resolved_result if h.ref == "resolved-id" else None) for h in handles}

        def reconcile(self, handle):
            reconciled_refs.append(handle.ref)
            return None

    monkeypatch.setattr(llm_deferred_sweep, "LiteLLMBackend", lambda *_, **__: MockBackend())

    assert llm_deferred_sweep.main([]) == 0
    # The batch-resolved handle must NOT be reconciled again individually...
    assert "resolved-id" not in reconciled_refs
    # ...nor may the batch-observed-but-still-pending handle.
    assert "pending-id" not in reconciled_refs
