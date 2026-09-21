import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import scripts.reconcile_stuck_chapter_agenda as reconcile_module
from citypods.compute.base import JobHandle
from citypods.compute.llm_deferred import write_deferred
from citypods.compute.llm_policy import DeferredLLMRequest, LLMRequestPolicy
from citypods.storage import StorageReadUnavailable
from scripts.reconcile_stuck_chapter_agenda import _apply, _classify_entry, _whole_int
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


def test_apply_discards_many_candidates_concurrently():
    """Regression for the production timeout: ~4,000 candidates fully sequential took over 1.5
    hours and still didn't finish inside the workflow's 110-minute step timeout. Verifies the
    parallel discard path doesn't lose or double-process anything at a scale well past what a
    single DISCARD_WORKERS batch (16) covers in one round."""
    storage = MemStorage()
    count = 40
    handles = [
        _handle(f"remote-{index}", model="mistral/mistral-medium-latest", ref=f"ref-{index}")
        for index in range(count)
    ]
    for handle in handles:
        write_deferred(storage, handle.recipe_hash, handle, now=NOW)
    candidates = [
        _classify_entry(_entry(handle, age_hours=25), now=NOW, older_than_hours=24)
        for handle in handles
    ]

    class Backend:
        def cancel_batch(self, refs):
            return {"cancelled": refs, "in_flight": [], "not_found": []}

    result = _apply(storage, Backend(), candidates)

    assert result["discarded_count"] == count
    assert result["dispositions"] == {"superseded": count}
    assert result["deadline_reached"] is False
    for handle in handles:
        assert storage.get_bytes(f"state/llm_deferred/{handle.recipe_hash}.json") is None


def test_apply_logs_discard_progress_to_stderr(capsys):
    """The timed-out production run's own artifact came back completely empty, with zero output
    anywhere in the workflow log for its entire ~1h39m apply phase -- there was no way to tell
    afterward how much of it actually happened. The discard pass must now be observable while
    it's still running, not just from a final report a killed process might never reach."""
    storage = MemStorage()
    handles = [
        _handle(f"remote-{index}", model="mistral/mistral-medium-latest", ref=f"ref-{index}")
        for index in range(5)
    ]
    for handle in handles:
        write_deferred(storage, handle.recipe_hash, handle, now=NOW)
    candidates = [
        _classify_entry(_entry(handle, age_hours=25), now=NOW, older_than_hours=24)
        for handle in handles
    ]

    class Backend:
        def cancel_batch(self, refs):
            return {"cancelled": refs, "in_flight": [], "not_found": []}

    _apply(storage, Backend(), candidates)

    stderr = capsys.readouterr().err
    assert "discarding 5 candidates" in stderr
    assert "discard progress 5/5 (100.0%)" in stderr


def test_apply_stops_early_on_should_stop_and_reports_it():
    """A stopped run must not silently drop work: candidates not yet discarded when the stop
    fires must still exist in the registry afterward (safe for a later run to pick up), and the
    report must say plainly that it stopped early rather than implying a clean, complete pass."""
    storage = MemStorage()
    count = 10
    handles = [
        _handle(f"remote-{index}", model="mistral/mistral-medium-latest", ref=f"ref-{index}")
        for index in range(count)
    ]
    for handle in handles:
        write_deferred(storage, handle.recipe_hash, handle, now=NOW)
    candidates = [
        _classify_entry(_entry(handle, age_hours=25), now=NOW, older_than_hours=24)
        for handle in handles
    ]

    class Backend:
        def cancel_batch(self, refs):
            return {"cancelled": refs, "in_flight": [], "not_found": []}

    calls = {"count": 0}

    def should_stop() -> bool:
        calls["count"] += 1
        return calls["count"] >= 3

    # A single worker makes completion order deterministic (submission order): should_stop() is
    # checked once per completed discard, so the 3rd candidate is always discarded (it already
    # completed by the time its check trips the stop) before the loop breaks. Whether a 4th
    # candidate is *also* discarded is a genuine, expected race: the single worker thread may
    # already have started (or even finished) it before the main thread's check-and-break runs --
    # and per the fix this regression-tests, that in-flight discard's real, already-executed
    # mutation must still be picked up and counted, not silently dropped. So the only guarantee is
    # discarded_count in {3, 4}, not an exact value.
    result = _apply(storage, Backend(), candidates, discard_workers=1, should_stop=should_stop)

    assert result["deadline_reached"] is True
    assert result["discarded_count"] in (3, 4)
    assert result["discarded_count"] + result["retained_count"] < count
    remaining = [
        handle
        for handle in handles
        if storage.get_bytes(f"state/llm_deferred/{handle.recipe_hash}.json") is not None
    ]
    assert len(remaining) == count - result["discarded_count"]


def test_apply_incorporates_a_discard_still_in_flight_when_the_stop_fired():
    """Regression for a CodeRabbit review finding on the fix above: a discard already running
    (not merely queued) when should_stop() fires must still have its real, already-executed
    mutation counted in the report -- executor.shutdown must wait for it and its outcome must be
    folded into discarded_count/dispositions, not silently dropped just because the main loop
    broke before observing its completion. Uses a controllable block/release instead of relying
    on natural thread-scheduling timing, so this is deterministic rather than a timing race."""
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

    class Backend:
        def cancel_batch(self, refs):
            return {"cancelled": refs, "in_flight": [], "not_found": []}

    # 3 workers for 3 candidates means every discard_deferred call starts immediately, so
    # "remote-2" is guaranteed to already be running (blocked here) by the time should_stop() is
    # first checked -- exactly the "in flight, not yet consumed" scenario being regression-tested.
    held_released = threading.Event()
    real_discard_deferred = reconcile_module.discard_deferred

    def blocking_discard_deferred(storage, recipe_hash, *, expected_ref=None):
        if recipe_hash == "remote-2":
            assert held_released.wait(timeout=5), "test did not release the held discard in time"
        return real_discard_deferred(storage, recipe_hash, expected_ref=expected_ref)

    def should_stop() -> bool:
        # Let the deliberately-held discard proceed once the run has decided to stop, mirroring
        # a real deadline: in-flight work is allowed to finish, only new admission stops.
        held_released.set()
        return True

    reconcile_module.discard_deferred = blocking_discard_deferred
    try:
        result = _apply(storage, Backend(), candidates, discard_workers=3, should_stop=should_stop)
    finally:
        reconcile_module.discard_deferred = real_discard_deferred

    assert result["deadline_reached"] is True
    assert result["discarded_count"] == 3
    for handle in handles:
        assert storage.get_bytes(f"state/llm_deferred/{handle.recipe_hash}.json") is None


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


def test_whole_int_accepts_github_actions_decimal_formatted_number_inputs():
    """A GitHub Actions `workflow_dispatch` input declared `type: number` renders as a decimal-
    formatted string (e.g. "25000.0") even for a plain integer value or default -- confirmed live
    when --max-row-writes used bare `type=int`: `int("25000.0")` raised ValueError and argparse
    failed the whole workflow before it classified a single record."""
    assert _whole_int("25000") == 25000
    assert _whole_int("25000.0") == 25000
    assert _whole_int("0.0") == 0


def test_whole_int_rejects_a_genuine_fraction():
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        _whole_int("25000.5")


def test_main_parses_decimal_formatted_number_inputs_end_to_end(monkeypatch):
    """Integration-level guard: exercises the actual argparse wiring in main(), not just
    _whole_int in isolation, so a future change to how --max-row-writes is declared can't
    silently reintroduce the same failure."""
    import scripts.reconcile_stuck_chapter_agenda as reconcile_module

    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(reconcile_module, "run", fake_run)

    exit_code = reconcile_module.main(
        [
            "--dry-run",
            "--older-than-hours",
            "0.0",
            "--max-row-writes",
            "25000.0",
        ]
    )

    assert exit_code == 0
    assert captured["older_than_hours"] == 0.0
    assert captured["max_row_writes"] == 25000
    assert isinstance(captured["max_row_writes"], int)


def test_run_rejects_a_non_positive_run_time_budget():
    import pytest

    from scripts.reconcile_stuck_chapter_agenda import run

    with pytest.raises(ValueError, match="--run-time-budget-minutes"):
        run(
            apply=False,
            site_config_path="unused",
            output_dir="unused",
            older_than_hours=24,
            run_time_budget_minutes=0,
        )


def test_main_wires_the_run_time_budget_through_to_run(monkeypatch):
    import scripts.reconcile_stuck_chapter_agenda as reconcile_module

    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(reconcile_module, "run", fake_run)

    exit_code = reconcile_module.main(
        ["--dry-run", "--older-than-hours", "24", "--run-time-budget-minutes", "45.0"]
    )

    assert exit_code == 0
    assert captured["run_time_budget_minutes"] == 45.0
    # main() must install signal handlers and hand run() a real _StopState, not None, so a
    # SIGTERM from the workflow's own timeout can still reach an in-progress apply/discard pass.
    assert isinstance(captured["stop_state"], reconcile_module._StopState)


def test_main_defaults_the_run_time_budget_comfortably_under_the_workflow_step_timeout(
    monkeypatch,
):
    """The workflow wraps this script's own step in a 110-minute timeout; the internal default
    must leave real margin for the final report print/artifact upload, not race it."""
    import scripts.reconcile_stuck_chapter_agenda as reconcile_module

    captured = {}
    monkeypatch.setattr(reconcile_module, "run", lambda **kwargs: captured.update(kwargs) or 0)

    reconcile_module.main(["--dry-run", "--older-than-hours", "24"])

    assert 0 < captured["run_time_budget_minutes"] < 110
