from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from citypods.compute.base import JobHandle
from citypods.compute.llm_deferred import write_deferred
from citypods.compute.llm_policy import DeferredLLMRequest, LLMRequestPolicy
from citypods.storage import StorageReadUnavailable
from scripts.reconcile_stuck_chapter_agenda import _apply, _classify_entry
from tests._cas_fake import MemStorage

NOW = datetime(2026, 9, 18, 12, tzinfo=UTC)


def _handle(
    recipe_hash: str,
    *,
    model: str | None,
    ref: str,
    backend: str = "llm-dispatch-v2",
    deferred: bool = False,
    purpose: str = "chapter-agenda",
) -> JobHandle:
    request = None
    if deferred:
        request = DeferredLLMRequest(
            messages=({"role": "user", "content": "test"},),
            policy=LLMRequestPolicy(purpose=purpose),
        )
    return JobHandle(
        task="agenda-item-extract",
        recipe_hash=recipe_hash,
        backend=backend,
        ref=ref,
        model=model,
        deferred_request=request,
    )


def _entry(handle: JobHandle, *, age_hours: float) -> SimpleNamespace:
    created = NOW - timedelta(hours=age_hours)
    return SimpleNamespace(
        decoded=handle,
        data={"created_at": created.isoformat()},
    )


def test_classification_finds_legacy_and_old_handles_but_not_fresh_current_work():
    legacy = _classify_entry(
        _entry(
            _handle(
                "legacy",
                model="mistral/mistral-medium-latest",
                ref="legacy-ref",
            ),
            age_hours=1,
        ),
        now=NOW,
        older_than_hours=24,
    )
    old_current = _classify_entry(
        _entry(
            _handle(
                "old-current",
                model="nvidia/nemotron-3-ultra-550b-a55b:free",
                ref="old-ref",
            ),
            age_hours=25,
        ),
        now=NOW,
        older_than_hours=24,
    )
    fresh = _classify_entry(
        _entry(
            _handle(
                "fresh",
                model="nvidia/nemotron-3-ultra-550b-a55b:free",
                ref="fresh-ref",
            ),
            age_hours=1,
        ),
        now=NOW,
        older_than_hours=24,
    )

    assert legacy["reasons"] == ["legacy-model"]
    assert old_current["reasons"] == ["age"]
    assert fresh is None


def test_apply_cancels_remote_jobs_and_discards_safe_synthetic_handles():
    storage = MemStorage()
    remote = _handle("remote", model="mistral/mistral-medium-latest", ref="remote-ref")
    synthetic = _handle(
        "synthetic",
        model=None,
        ref="deferred:synthetic",
        deferred=True,
    )
    write_deferred(storage, remote.recipe_hash, remote, now=NOW)
    write_deferred(storage, synthetic.recipe_hash, synthetic, now=NOW)
    candidates = [
        _classify_entry(_entry(remote, age_hours=1), now=NOW, older_than_hours=24),
        _classify_entry(_entry(synthetic, age_hours=25), now=NOW, older_than_hours=24),
    ]

    class Backend:
        def cancel_batch(self, refs):
            assert refs == ["remote-ref"]
            return {"cancelled": ["remote-ref"], "in_flight": [], "not_found": []}

    result = _apply(storage, Backend(), candidates)

    assert result["cancelled_count"] == 1
    assert result["discarded_count"] == 2
    assert storage.get_bytes("state/llm_deferred/remote.json") is None
    assert storage.get_bytes("state/llm_deferred/synthetic.json") is None


def test_apply_retains_unsupported_remote_handles_without_deleting_them():
    storage = MemStorage()
    handle = _handle(
        "unsupported",
        model="mistral/mistral-medium-latest",
        ref="legacy-ref",
        backend="litellm",
    )
    write_deferred(storage, handle.recipe_hash, handle, now=NOW)
    candidate = _classify_entry(_entry(handle, age_hours=25), now=NOW, older_than_hours=24)

    class Backend:
        def cancel_batch(self, refs):
            assert refs == []
            return {"cancelled": [], "in_flight": [], "not_found": []}

    result = _apply(storage, Backend(), [candidate])

    assert result["discarded_count"] == 0
    assert result["retained_count"] == 1
    assert result["dispositions"] == {"unsupported_remote_retained": 1}
    assert storage.get_bytes("state/llm_deferred/unsupported.json") is not None


def test_apply_retains_a_v2_job_that_is_still_in_flight():
    storage = MemStorage()
    handle = _handle("in-flight", model="mistral/mistral-medium-latest", ref="busy-ref")
    write_deferred(storage, handle.recipe_hash, handle, now=NOW)
    candidate = _classify_entry(_entry(handle, age_hours=25), now=NOW, older_than_hours=24)

    class Backend:
        def cancel_batch(self, refs):
            return {"cancelled": [], "in_flight": refs, "not_found": []}

    result = _apply(storage, Backend(), [candidate])

    assert result["in_flight_count"] == 1
    assert result["retained_count"] == 1
    assert storage.get_bytes("state/llm_deferred/in-flight.json") is not None


def test_apply_stops_before_the_row_write_budget_and_retains_the_remainder():
    storage = MemStorage()
    handles = [
        _handle(f"remote-{index}", model="mistral/mistral-medium-latest", ref=f"ref-{index}")
        for index in range(3)
    ]
    for handle in handles:
        write_deferred(storage, handle.recipe_hash, handle, now=NOW)
    candidates = [
        _classify_entry(_entry(handle, age_hours=25), now=NOW, older_than_hours=24)
        for handle in handles
    ]
    calls = []

    class Backend:
        def cancel_batch(self, refs):
            calls.append(refs)
            return {"cancelled": refs, "in_flight": [], "not_found": []}

    result = _apply(storage, Backend(), candidates, max_row_writes=16)

    assert calls == [["ref-0", "ref-1"]]
    assert result["estimated_row_writes"] == 16
    assert result["write_budget_skipped_count"] == 1
    assert result["dispositions"] == {"superseded": 2, "write_budget_retained": 1}
    assert storage.get_bytes("state/llm_deferred/remote-2.json") is not None


def test_apply_retains_an_unavailable_record_and_continues():
    class _UnavailableStorage(MemStorage):
        unavailable_key = None

        def get_file(self, key, local_path):
            if key == self.unavailable_key:
                raise StorageReadUnavailable(key, TimeoutError("connection reset"))
            return super().get_file(key, local_path)

    storage = _UnavailableStorage()
    unavailable = _handle(
        "unavailable",
        model=None,
        ref="deferred:unavailable",
        deferred=True,
    )
    good = _handle(
        "good-after-unavailable",
        model=None,
        ref="deferred:good-after-unavailable",
        deferred=True,
    )
    write_deferred(storage, unavailable.recipe_hash, unavailable, now=NOW)
    write_deferred(storage, good.recipe_hash, good, now=NOW)
    storage.unavailable_key = "state/llm_deferred/unavailable.json"
    candidates = [
        _classify_entry(_entry(unavailable, age_hours=25), now=NOW, older_than_hours=24),
        _classify_entry(_entry(good, age_hours=25), now=NOW, older_than_hours=24),
    ]

    class Backend:
        def cancel_batch(self, refs):
            assert refs == []
            return {"cancelled": [], "in_flight": [], "not_found": []}

    result = _apply(storage, Backend(), candidates)

    assert result["discarded_count"] == 1
    assert result["retained_count"] == 1
    assert result["dispositions"] == {
        "record_unavailable_retained": 1,
        "superseded": 1,
    }
    assert len(result["errors"]) == 1
    assert "unavailable" in result["errors"][0]
    assert storage.get_bytes("state/llm_deferred/unavailable.json") is not None
    assert storage.get_bytes("state/llm_deferred/good-after-unavailable.json") is None
