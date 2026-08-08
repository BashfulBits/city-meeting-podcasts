from datetime import UTC, datetime, timedelta

from citypods.compute.base import JobHandle, JobResult
from citypods.compute.llm_budget import mutate_llm_budget
from citypods.compute.llm_deferred import (
    DEFAULT_TTL_DAYS,
    DEFERRED_INDEX_MIGRATION_KEY,
    DEFERRED_INDEX_PENDING_PREFIX,
    DEFERRED_PREFIX,
    _indexed_listing,
    _write_json,
    deferred_key,
    iter_pending_deferred,
    list_pending_deferred,
    load_deferred_snapshot,
    look_up_deferred,
    prune_expired_deferred,
    prune_expired_deferred_snapshot,
    repair_deferred_index,
    write_deferred,
)
from citypods.compute.llm_policy import ROUTES, DeferredLLMRequest, LLMRequestPolicy
from tests._cas_fake import MemStorage

NOW = datetime(2026, 7, 16, 12, tzinfo=UTC)


def test_deferred_key_is_scoped_under_the_prefix():
    assert deferred_key("recipe-1") == f"{DEFERRED_PREFIX}recipe-1.json"


def test_write_and_look_up_a_pending_handle_round_trips():
    storage = MemStorage()
    policy = LLMRequestPolicy(
        allowed_models=("deepseek/deepseek-v4-flash",),
        allow_paid=True,
        deadline_at=datetime(2026, 7, 20, tzinfo=UTC),
        purpose="test",
    )
    handle = JobHandle(
        task="tag",
        recipe_hash="recipe-1",
        backend="litellm",
        ref="deferred:recipe-1",
        structured_output="test-output",
        deferred_request=DeferredLLMRequest(
            messages=({"role": "user", "content": "hi"},), policy=policy
        ),
    )
    write_deferred(storage, "recipe-1", handle)

    found = look_up_deferred(storage, "recipe-1")
    assert isinstance(found, JobHandle)
    assert found.recipe_hash == "recipe-1"
    assert found.structured_output == "test-output"
    assert found.deferred_request is not None
    assert found.deferred_request.messages == ({"role": "user", "content": "hi"},)
    assert found.deferred_request.policy == policy


def test_pending_records_index_both_live_route_consumers_without_shared_bucket_writes():
    storage = MemStorage()
    models = ("gemini/gemini-3.1-flash-lite", "gemini/gemini-3.5-flash-lite")
    for task, recipe_hash, purpose in (
        ("tag", "tag-recipe", "topic-tags"),
        ("classify", "classify-recipe", "classify-civic-platforms"),
    ):
        handle = _pending_handle(recipe_hash)
        handle = JobHandle(
            **{
                **handle.__dict__,
                "task": task,
                "deferred_request": DeferredLLMRequest(
                    messages=handle.deferred_request.messages,
                    policy=LLMRequestPolicy(allowed_models=models, purpose=purpose),
                ),
            }
        )
        write_deferred(storage, recipe_hash, handle, now=NOW)

    assert set(storage.keys(DEFERRED_INDEX_PENDING_PREFIX)) == {
        f"{DEFERRED_INDEX_PENDING_PREFIX}{model}/{recipe}.json"
        for model in models
        for recipe in ("tag-recipe", "classify-recipe")
    }


def test_completion_removes_old_route_pointers_after_canonical_write():
    storage = MemStorage()
    write_deferred(
        storage,
        "recipe-1",
        _pending_handle("recipe-1"),
        now=NOW,
    )
    assert storage.keys(DEFERRED_INDEX_PENDING_PREFIX)
    write_deferred(
        storage,
        "recipe-1",
        JobResult(task="tag", recipe_hash="recipe-1", output={}, model="m"),
        now=NOW,
    )
    assert storage.keys(DEFERRED_INDEX_PENDING_PREFIX) == []


def test_a_retry_narrowing_the_candidate_models_drops_only_the_stale_pointer():
    """Regression for the crash-safety fix: pointers common to both the old and new model set are
    left alone (never deleted then immediately recreated), and only the pointer for a model that's
    no longer a candidate is removed. New-then-canonical-then-stale-delete ordering means a crash
    at any point during this call never leaves the still-pending record with zero pointers."""
    storage = MemStorage()
    kept_model, dropped_model = ("gemini/gemini-3.1-flash-lite", "gemini/gemini-3.5-flash-lite")
    write_deferred(
        storage,
        "recipe-1",
        JobHandle(
            task="tag",
            recipe_hash="recipe-1",
            backend="litellm",
            ref="deferred:recipe-1",
            deferred_request=DeferredLLMRequest(
                messages=({"role": "user", "content": "hi"},),
                policy=LLMRequestPolicy(allowed_models=(kept_model, dropped_model)),
            ),
        ),
        now=NOW,
    )
    assert set(storage.keys(DEFERRED_INDEX_PENDING_PREFIX)) == {
        f"{DEFERRED_INDEX_PENDING_PREFIX}{kept_model}/recipe-1.json",
        f"{DEFERRED_INDEX_PENDING_PREFIX}{dropped_model}/recipe-1.json",
    }

    write_deferred(
        storage,
        "recipe-1",
        JobHandle(
            task="tag",
            recipe_hash="recipe-1",
            backend="litellm",
            ref="deferred:recipe-1",
            model=kept_model,
        ),
        now=NOW,
    )

    assert set(storage.keys(DEFERRED_INDEX_PENDING_PREFIX)) == {
        f"{DEFERRED_INDEX_PENDING_PREFIX}{kept_model}/recipe-1.json"
    }


def test_repair_marks_migration_and_indexed_snapshot_rechecks_canonical_records():
    storage = MemStorage()
    write_deferred(storage, "pending-1", _pending_handle("pending-1"), now=NOW)
    write_deferred(
        storage,
        "completed-1",
        JobResult(task="tag", recipe_hash="completed-1", output={}, model="m"),
        now=NOW,
    )

    assert repair_deferred_index(storage, now=NOW) == 2
    assert storage.exists(DEFERRED_INDEX_MIGRATION_KEY)
    snapshot = load_deferred_snapshot(storage, now=NOW)
    assert [handle.recipe_hash for handle in snapshot.pending()] == ["pending-1"]
    assert len(snapshot.entries) == 1


def test_indexed_listing_skips_a_model_partition_that_is_out_of_capacity():
    storage = MemStorage()
    blocked_model, open_model = ("gemini/gemini-3.1-flash-lite", "gemini/gemini-3.5-flash-lite")
    for recipe_hash, model in (("r-blocked", blocked_model), ("r-open", open_model)):
        write_deferred(
            storage,
            recipe_hash,
            JobHandle(
                task="tag",
                recipe_hash=recipe_hash,
                backend="litellm",
                ref=f"deferred:{recipe_hash}",
                model=model,
            ),
            now=NOW,
        )
    mutate_llm_budget(
        storage,
        lambda budget, now: budget.block(
            blocked_model, until=now + timedelta(days=1), route=ROUTES[blocked_model], now=now
        ),
        now=NOW,
    )

    listing = _indexed_listing(storage, now=NOW)

    assert {key for key, _ in listing} == {
        f"{DEFERRED_INDEX_PENDING_PREFIX}{open_model}/r-open.json"
    }


def test_repair_deletes_an_orphan_pointer_not_backed_by_any_canonical_record():
    storage = MemStorage()
    write_deferred(storage, "pending-1", _pending_handle("pending-1"), now=NOW)
    model = next(iter(ROUTES))
    orphan_key = f"{DEFERRED_INDEX_PENDING_PREFIX}{model}/never-written.json"
    _write_json(storage, orphan_key, b'{"recipe_hash": "never-written"}\n')

    assert repair_deferred_index(storage, now=NOW) == 1
    remaining = set(storage.keys(DEFERRED_INDEX_PENDING_PREFIX))
    assert orphan_key not in remaining
    assert any(key.endswith("/pending-1.json") for key in remaining)


def test_write_and_look_up_a_completed_result_round_trips():
    storage = MemStorage()
    result = JobResult(
        task="tag",
        recipe_hash="recipe-1",
        output={"choices": []},
        model="gemini/gemini-3-flash-preview",
    )
    write_deferred(storage, "recipe-1", result)

    found = look_up_deferred(storage, "recipe-1")
    assert isinstance(found, JobResult)
    assert found.output == {"choices": []}
    assert found.model == "gemini/gemini-3-flash-preview"


def test_look_up_missing_recipe_hash_returns_none():
    storage = MemStorage()
    assert look_up_deferred(storage, "never-written") is None


def test_a_completed_record_is_never_downgraded_back_to_pending():
    storage = MemStorage()
    write_deferred(
        storage,
        "recipe-1",
        JobResult(task="tag", recipe_hash="recipe-1", output={"done": True}, model="m"),
    )
    stale_handle = JobHandle(
        task="tag",
        recipe_hash="recipe-1",
        backend="litellm",
        ref="deferred:recipe-1",
        deferred_request=DeferredLLMRequest(
            messages=({"role": "user", "content": "hi"},), policy=LLMRequestPolicy()
        ),
    )
    write_deferred(storage, "recipe-1", stale_handle)

    found = look_up_deferred(storage, "recipe-1")
    assert isinstance(found, JobResult)
    assert found.output == {"done": True}


def test_iter_pending_deferred_yields_oldest_last_modified_first():
    """Ordering follows ``last_modified`` (free from the listing, no body read needed), not the
    backend's raw listing order (typically lexicographic by key) -- otherwise a capacity-limited
    run spends its budget on an arbitrary subset instead of whichever records have gone longest
    without a successful attempt."""
    recipe_hashes_oldest_to_newest = ["z-record", "m-record", "a-record"]
    timestamps = {
        "z-record": datetime(2026, 1, 1, tzinfo=UTC),
        "m-record": datetime(2026, 3, 1, tzinfo=UTC),
        "a-record": datetime(2026, 6, 1, tzinfo=UTC),
    }

    class _ReorderedStorage(MemStorage):
        def list_objects(self, prefix=""):
            for recipe_hash, ts in timestamps.items():
                key = deferred_key(recipe_hash)
                if key.startswith(prefix):
                    yield key, ts

    storage = _ReorderedStorage()
    for recipe_hash in timestamps:
        write_deferred(
            storage,
            recipe_hash,
            JobHandle(
                task="tag",
                recipe_hash=recipe_hash,
                backend="litellm",
                ref=f"deferred:{recipe_hash}",
                deferred_request=DeferredLLMRequest(
                    messages=({"role": "user", "content": "hi"},), policy=LLMRequestPolicy()
                ),
            ),
        )

    ordered = [handle.recipe_hash for handle in iter_pending_deferred(storage)]
    assert ordered == recipe_hashes_oldest_to_newest


def test_snapshot_reads_each_registry_record_once_and_prune_reuses_it():
    class _CountingStorage(MemStorage):
        list_calls = 0

        def list_objects(self, prefix=""):
            self.list_calls += 1
            yield from super().list_objects(prefix)

    storage = _CountingStorage()
    for recipe_hash in ("pending-1", "completed-1", "expired-1"):
        write_deferred(storage, recipe_hash, _pending_handle(recipe_hash), now=NOW)
    write_deferred(
        storage,
        "completed-1",
        JobResult(task="tag", recipe_hash="completed-1", output={}, model="m"),
        now=NOW,
    )
    storage.class_a = storage.class_b = storage.list_calls = 0

    snapshot = load_deferred_snapshot(storage)
    assert storage.list_calls == 1
    assert storage.class_b == 3

    prune_expired_deferred_snapshot(
        storage, snapshot, now=NOW + timedelta(days=DEFAULT_TTL_DAYS + 1)
    )
    # Pruning uses the decoded snapshot; it only rereads expiry candidates for a conflict-safe
    # compare, never listing the registry or downloading non-expired records again.
    assert storage.list_calls == 1
    assert storage.class_b == 6


def test_snapshot_prune_does_not_delete_a_record_changed_after_snapshot():
    storage = MemStorage()
    write_deferred(storage, "recipe-1", _pending_handle("recipe-1"), now=NOW)
    snapshot = load_deferred_snapshot(storage)

    # A later completion wins over the stale pending snapshot.
    write_deferred(
        storage,
        "recipe-1",
        JobResult(task="tag", recipe_hash="recipe-1", output={"done": True}, model="m"),
        now=NOW + timedelta(days=1),
    )

    deleted = prune_expired_deferred_snapshot(
        storage, snapshot, now=NOW + timedelta(days=DEFAULT_TTL_DAYS + 1)
    )
    assert deleted == 0
    assert isinstance(look_up_deferred(storage, "recipe-1"), JobResult)


def test_list_pending_deferred_returns_only_pending_records():
    storage = MemStorage()
    write_deferred(
        storage,
        "completed-1",
        JobResult(task="tag", recipe_hash="completed-1", output={}, model="m"),
    )
    pending_handle = JobHandle(
        task="tag",
        recipe_hash="pending-1",
        backend="litellm",
        ref="deferred:pending-1",
        deferred_request=DeferredLLMRequest(
            messages=({"role": "user", "content": "hi"},),
            policy=LLMRequestPolicy(deadline_at=datetime.now(UTC) + timedelta(days=3)),
        ),
    )
    write_deferred(storage, "pending-1", pending_handle)
    dispatch_handle = JobHandle(
        task="tag",
        recipe_hash="pending-2",
        backend="litellm",
        ref="/v1/requests/chatcmpl-1",
        model="mistral/mistral-large-2512",
        owner="pending-2",
        input_per_token=0.0,
        output_per_token=0.0,
    )
    write_deferred(storage, "pending-2", dispatch_handle)

    pending = {handle.recipe_hash: handle for handle in list_pending_deferred(storage)}
    assert set(pending) == {"pending-1", "pending-2"}
    assert pending["pending-1"].deferred_request is not None
    # A genuine in-flight dispatch handle has no deferred_request -- reconcile() must route it
    # through the real URL-polling path, not re-run selection.
    assert pending["pending-2"].deferred_request is None
    assert pending["pending-2"].model == "mistral/mistral-large-2512"
    assert pending["pending-2"].owner == "pending-2"


def _pending_handle(recipe_hash: str, *, deadline_at: datetime | None = None) -> JobHandle:
    return JobHandle(
        task="tag",
        recipe_hash=recipe_hash,
        backend="litellm",
        ref=f"deferred:{recipe_hash}",
        deferred_request=DeferredLLMRequest(
            messages=({"role": "user", "content": "hi"},),
            policy=LLMRequestPolicy(deadline_at=deadline_at),
        ),
    )


def test_write_deferred_preserves_created_at_across_redefers():
    """Observed indirectly through TTL behavior: if a re-defer incorrectly reset `created_at` to
    the second write's time, the record would survive an extra 5 days past the default TTL."""
    storage = MemStorage()
    write_deferred(storage, "recipe-1", _pending_handle("recipe-1"), now=NOW)
    write_deferred(storage, "recipe-1", _pending_handle("recipe-1"), now=NOW + timedelta(days=5))

    # 39 days after the *original* write, but only 34 days after the re-defer.
    deleted = prune_expired_deferred(storage, now=NOW + timedelta(days=DEFAULT_TTL_DAYS + 1))
    assert deleted == 1
    assert look_up_deferred(storage, "recipe-1") is None


def test_prune_expired_deferred_leaves_fresh_records_alone():
    storage = MemStorage()
    write_deferred(storage, "recipe-1", _pending_handle("recipe-1"), now=NOW)
    deleted = prune_expired_deferred(storage, now=NOW + timedelta(days=1))
    assert deleted == 0
    assert look_up_deferred(storage, "recipe-1") is not None


def test_prune_expired_deferred_deletes_past_the_default_ttl():
    storage = MemStorage()
    write_deferred(storage, "recipe-1", _pending_handle("recipe-1"), now=NOW)
    deleted = prune_expired_deferred(storage, now=NOW + timedelta(days=DEFAULT_TTL_DAYS + 1))
    assert deleted == 1
    assert look_up_deferred(storage, "recipe-1") is None


def test_prune_expired_deferred_never_deletes_before_a_longer_caller_deadline():
    """A caller waiting out something like a monthly cost cap may set a deadline longer than the
    default TTL -- that must win, or the request silently vanishes before it can ever complete."""
    storage = MemStorage()
    long_deadline = NOW + timedelta(days=60)
    write_deferred(
        storage, "recipe-1", _pending_handle("recipe-1", deadline_at=long_deadline), now=NOW
    )

    # Past the default 38-day TTL, but not yet past the caller's own 60-day deadline.
    deleted = prune_expired_deferred(storage, now=NOW + timedelta(days=DEFAULT_TTL_DAYS + 1))
    assert deleted == 0
    assert look_up_deferred(storage, "recipe-1") is not None

    deleted = prune_expired_deferred(storage, now=long_deadline + timedelta(days=1))
    assert deleted == 1
    assert look_up_deferred(storage, "recipe-1") is None


def test_prune_expired_deferred_also_cleans_up_completed_records():
    storage = MemStorage()
    write_deferred(
        storage,
        "recipe-1",
        JobResult(task="tag", recipe_hash="recipe-1", output={}, model="m"),
        now=NOW,
    )
    deleted = prune_expired_deferred(storage, now=NOW + timedelta(days=DEFAULT_TTL_DAYS + 1))
    assert deleted == 1
    assert look_up_deferred(storage, "recipe-1") is None


def test_prune_releases_the_ledger_reservation_of_an_abandoned_dispatch_handle():
    """A genuine Mistral dispatch handle (no `deferred_request`) that's still `pending` past its
    38-day TTL means the Worker never produced a terminal response -- its ledger reservation would
    otherwise sit in `inflight` forever (a window rollover never clears it, only settle/release
    does), so pruning the registry record must also release the reservation it represents."""
    from citypods.compute.llm_budget import load_llm_budget_cas, mutate_llm_budget
    from citypods.compute.llm_policy import ROUTES

    storage = MemStorage()
    route = ROUTES["mistral/mistral-large-2512"]
    mutate_llm_budget(
        storage,
        lambda budget, attempt_now: budget.reserve(
            "owner-1", route.model, route=route, requests=1, tokens=10, cost=0.0, now=attempt_now
        ),
        now=NOW,
    )
    budget, _ = load_llm_budget_cas(storage)
    assert "owner-1" in budget.routes[route.model].inflight

    dispatch_handle = JobHandle(
        task="tag",
        recipe_hash="recipe-1",
        backend="litellm",
        ref="/v1/requests/chatcmpl-1",
        model=route.model,
        owner="owner-1",
        input_per_token=0.0,
        output_per_token=0.0,
    )
    write_deferred(storage, "recipe-1", dispatch_handle, now=NOW)

    deleted = prune_expired_deferred(storage, now=NOW + timedelta(days=DEFAULT_TTL_DAYS + 1))
    assert deleted == 1
    assert look_up_deferred(storage, "recipe-1") is None

    budget, _ = load_llm_budget_cas(storage)
    assert "owner-1" not in budget.routes[route.model].inflight


def test_look_up_deferred_tolerates_a_malformed_record_instead_of_raising():
    """A corrupt record (missing a required field, e.g. from a partial write or a future schema
    change) must not raise -- it must be treated as absent, the same as a missing key, so one bad
    record can't take down a caller iterating the whole registry (the sweep)."""
    storage = MemStorage()
    _write_raw(storage, "recipe-1", {"status": "pending"})  # missing task/recipe_hash
    _write_raw(storage, "recipe-2", {"status": "completed"})  # missing task/recipe_hash
    _write_raw(storage, "recipe-3", {"status": "pending", "task": "tag", "recipe_hash": "recipe-3"})

    assert look_up_deferred(storage, "recipe-1") is None
    assert look_up_deferred(storage, "recipe-2") is None
    assert isinstance(look_up_deferred(storage, "recipe-3"), JobHandle)


def test_list_pending_deferred_skips_malformed_records_rather_than_aborting():
    storage = MemStorage()
    _write_raw(storage, "bad-1", {"status": "pending", "policy": "not-a-mapping"})
    write_deferred(
        storage,
        "good-1",
        JobHandle(
            task="tag",
            recipe_hash="good-1",
            backend="litellm",
            ref="deferred:good-1",
            deferred_request=DeferredLLMRequest(
                messages=({"role": "user", "content": "hi"},), policy=LLMRequestPolicy()
            ),
        ),
        now=NOW,
    )

    pending = list_pending_deferred(storage)
    assert {handle.recipe_hash for handle in pending} == {"good-1"}


def test_list_pending_deferred_rejects_a_corrupt_attempted_requests_value():
    """A non-negative int (or absent, for a record predating this field) is the only shape
    `attempted_requests` may take -- a string, bool, or negative value must isolate the whole
    record as corrupt rather than reach ledger settlement math (CodeRabbit, PR #1007)."""
    storage = MemStorage()
    for recipe_hash, bad_value in (
        ("bad-string", "two"),
        ("bad-bool", True),
        ("bad-negative", -1),
    ):
        _write_raw(
            storage,
            recipe_hash,
            {
                "status": "pending",
                "task": "tag",
                "recipe_hash": recipe_hash,
                "backend": "litellm",
                "ref": f"deferred:{recipe_hash}",
                "attempted_requests": bad_value,
            },
        )
    write_deferred(
        storage,
        "good-1",
        JobHandle(
            task="tag",
            recipe_hash="good-1",
            backend="litellm",
            ref="deferred:good-1",
            attempted_requests=0,
            deferred_request=DeferredLLMRequest(
                messages=({"role": "user", "content": "hi"},), policy=LLMRequestPolicy()
            ),
        ),
        now=NOW,
    )

    pending = {handle.recipe_hash: handle for handle in list_pending_deferred(storage)}
    # 0 is a legitimate (falsy but valid) attempted_requests value -- e.g. a request rejected as
    # already-over-quota on its very first attempt -- and must round-trip, not be treated as absent.
    assert set(pending) == {"good-1"}
    assert pending["good-1"].attempted_requests == 0


def _write_raw(storage: MemStorage, recipe_hash: str, record: dict) -> None:
    import json
    import tempfile
    from pathlib import Path

    from citypods.compute.llm_deferred import deferred_key

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "record.json"
        path.write_text(json.dumps(record))
        storage.put_file(deferred_key(recipe_hash), path, "application/json")
