from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from citypods.compute.base import JobHandle, JobResult
from citypods.compute.llm_policy import DeferredLLMRequest, LLMRequestPolicy
from scripts import llm_deferred_sweep


def _handle(recipe_hash: str) -> JobHandle:
    return JobHandle(
        task="tag", recipe_hash=recipe_hash, backend="litellm", ref="deferred:" + recipe_hash
    )


def test_sweep_registers_topic_tags_contract_before_reconciling():
    """The sweep runs as its own process, separate from whatever Stage originally submitted a
    pending "tag" job -- citypods.compute.structured's registry is a plain in-memory dict, so
    response_model("topic-tags") only resolves if something in *this* process registered it.
    Without _register_known_contracts(), every pending "tag" record would fail to reconcile
    forever (caught per-record, logged, never actually completed) until its 38-day TTL expired."""
    from citypods.compute.structured import response_model

    llm_deferred_sweep._register_known_contracts()

    assert response_model("topic-tags") is not None


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
    monkeypatch.setattr(llm_deferred_sweep, "iter_pending_deferred", lambda _storage: iter(handles))
    monkeypatch.setattr(llm_deferred_sweep, "list_pending_deferred", lambda _storage: handles)
    monkeypatch.setattr(llm_deferred_sweep, "prune_expired_deferred", lambda _storage: 2)

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


def test_sweep_skips_same_capacity_cohort_after_no_fit(monkeypatch, capsys):
    monkeypatch.setattr(llm_deferred_sweep, "load_site_config", lambda *_: {"defaults": {}})
    fake_storage = SimpleNamespace(cas_capable=True)
    monkeypatch.setattr(llm_deferred_sweep, "make_storage", lambda *_args, **_kwargs: fake_storage)
    monkeypatch.setattr(llm_deferred_sweep, "prune_expired_deferred", lambda _storage: 0)
    monkeypatch.setattr(llm_deferred_sweep, "list_pending_deferred", lambda _storage: [])

    policy = LLMRequestPolicy(
        allowed_models=("gemini/gemini-3.1-flash-lite",), purpose="topic-tags"
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
    monkeypatch.setattr(llm_deferred_sweep, "iter_pending_deferred", lambda _storage: iter(handles))

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
    monkeypatch.setattr(llm_deferred_sweep, "prune_expired_deferred", lambda _storage: 0)
    monkeypatch.setattr(llm_deferred_sweep, "list_pending_deferred", lambda _storage: [])

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
        _handle_for("recipe-tag", task="tag", structured_output="topic-tags", purpose="topic-tags"),
        _handle_for(
            "recipe-classify",
            task="classify",
            structured_output="civic-platforms",
            purpose="civic-platforms",
        ),
    ]
    monkeypatch.setattr(llm_deferred_sweep, "iter_pending_deferred", lambda _storage: iter(handles))

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
