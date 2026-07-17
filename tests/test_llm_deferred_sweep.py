from __future__ import annotations

from types import SimpleNamespace

from citypods.compute.base import JobHandle, JobResult
from scripts import llm_deferred_sweep


def _handle(recipe_hash: str) -> JobHandle:
    return JobHandle(
        task="tag", recipe_hash=recipe_hash, backend="litellm", ref="deferred:" + recipe_hash
    )


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
    assert "3 pending" in out.out
    assert "1 completed" in out.out
    assert "1 still pending" in out.out
    assert "1 failed" in out.out
    assert "2 pruned" in out.out
    assert "recipe-3" in out.err
