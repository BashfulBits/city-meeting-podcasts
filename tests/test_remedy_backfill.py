"""Offline recovery tests: provider mocks never spend quota or alter production."""

import importlib.util
import json
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

DIRECTORY = Path(__file__).resolve().parents[1] / "evals/remedy/results/varied-2026-10-11"
NOW = datetime(2026, 10, 13, 2, tzinfo=UTC)


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(DIRECTORY))
    loaded = []
    for name in ("recovery", "run", "backfill"):
        spec = importlib.util.spec_from_file_location(name, DIRECTORY / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        loaded.append(module)
    return loaded


def observation(candidate="sol", case="one", status="failed", when=None, **changes):
    return {
        "candidate": candidate,
        "case_id": case,
        "status": status,
        "manifest_hash": "manifest",
        "provider_attempts": 1,
        "observed_at": (when or NOW - timedelta(hours=1)).isoformat(),
        "end_to_end_seconds": 0,
        "error_class": "InternalServerError",
        **changes,
    }


def test_failed_case_recovers_before_global_success(modules):
    recovery, _, controller = modules
    rows = [observation(), observation(case="two", status="completed")]
    plan = recovery.candidate_plan(rows, "sol", ["one", "two", "three"], NOW, NOW)
    assert plan["eligible_case_ids"] == ["three", "one"]
    assert (plan["answered"], plan["failed_pending"], plan["never_called"]) == (1, 1, 1)
    state = controller.run_state(None, "manifest", NOW)
    status = controller.snapshot(rows, state, ["one", "two", "three"], NOW)
    assert status["stop_reason"] is None
    assert status["routes"]["sol"]["state"] == "ready"
    assert status["target"] == 24


def test_success_on_secondary_prevents_primary_duplicate(modules):
    recovery, _, _ = modules
    rows = [observation("gemini37"), observation("gemini37_secondary", status="completed")]
    for name in ("gemini37", "gemini37_secondary"):
        plan = recovery.candidate_plan(rows, name, ["one"], NOW, NOW)
        assert plan["state"] == "complete"
        assert not plan["eligible_case_ids"]
        assert plan["answered"] == 1


def test_cooldown_begins_after_slow_legacy_request(modules):
    recovery, _, _ = modules
    row = observation(when=NOW - timedelta(minutes=35), end_to_end_seconds=600)
    plan = recovery.candidate_plan([row], "sol", ["one"], NOW, NOW)
    assert plan["state"] == "cooldown"
    assert recovery.timestamp(plan["next_eligible_at"]) == NOW + timedelta(minutes=5)
    later = recovery.candidate_plan([row], "sol", ["one"], NOW + timedelta(minutes=5), NOW)
    assert later["state"] == "ready"


def test_retry_after_longer_than_default_cooldown(modules):
    recovery, _, _ = modules
    row = observation(
        when=NOW,
        provider_error={"category": "rate_limit", "retry_after_seconds": 3600},
    )
    assert recovery.cooldown_until(row) == NOW + timedelta(hours=1)


def test_retry_daily_limit_reopens_at_utc_reset(modules):
    recovery, _, _ = modules
    rows = [observation(when=NOW - timedelta(hours=1, minutes=i)) for i in range(3)]
    plan = recovery.candidate_plan(rows, "sol", ["one"], NOW, NOW)
    assert plan["state"] == "daily_attempt_limit"
    tomorrow = datetime(2026, 10, 14, tzinfo=UTC)
    assert recovery.timestamp(plan["next_eligible_at"]) == tomorrow
    assert recovery.candidate_plan(rows, "sol", ["one"], tomorrow, NOW)["state"] == "ready"


def test_retry_run_limit_survives_daily_reset(modules):
    recovery, _, _ = modules
    started = NOW - timedelta(hours=2)
    rows = [observation(when=NOW - timedelta(hours=1, minutes=i)) for i in range(3)]
    tomorrow = datetime(2026, 10, 14, tzinfo=UTC)
    plan = recovery.candidate_plan(rows, "sol", ["one"], tomorrow, started)
    assert plan["state"] == "retry_exhausted"
    assert plan["retry_exhausted"] == 1


def test_access_errors_hold_for_review(modules):
    recovery, _, _ = modules
    row = observation(provider_error={"category": "access_error"})
    assert recovery.candidate_plan([row], "sol", ["one"], NOW, NOW)["state"] == "access_error"


def test_aware_reset_handles_dst(modules):
    recovery, _, _ = modules
    # The November 1 midnight-to-midnight interval in Los Angeles lasts 25 hours.
    start = datetime(2026, 11, 1, 7, tzinfo=UTC)
    assert recovery.next_reset(start, "America/Los_Angeles") == start + timedelta(hours=25)
    with pytest.raises(ValueError, match="timezone"):
        recovery.timestamp("2026-10-13T00:00:00")


def test_persisted_wait_and_horizon_resume_without_extension(modules, tmp_path):
    recovery, _, controller = modules
    original = controller.run_state(None, "manifest", NOW)
    path = tmp_path / "status.json"
    recovery.atomic_status(path, original)
    loaded = json.loads(path.read_text())
    resumed = controller.run_state(loaded, "manifest", NOW + timedelta(hours=2))
    assert resumed["deadline_at"] == original["deadline_at"]
    assert resumed["run_id"] == original["run_id"]
    ready = {"state": "ready", "next_eligible_at": None}
    wait = {"state": "daily_quota", "next_eligible_at": (NOW + timedelta(hours=5)).isoformat()}
    assert controller.retain_wait(ready, wait, NOW)["state"] == "daily_quota"
    assert controller.retain_wait(ready, wait, NOW + timedelta(hours=5))["state"] == "ready"
    assert not path.with_suffix(".json.tmp").exists()


@pytest.mark.parametrize("kind", ["horizon", "calls"])
def test_incomplete_stopping_is_explicit(modules, kind):
    _, _, controller = modules
    state = controller.run_state(None, "manifest", NOW, max_calls=1)
    rows = [observation(when=NOW)] if kind == "calls" else []
    now = NOW + timedelta(days=1) if kind == "horizon" else NOW
    status = controller.snapshot(rows, state, ["one"], now)
    assert status["stop_reason"] == ("call_budget" if kind == "calls" else "run_horizon")
    assert status["answered"] == 0


def test_astra_is_retained_but_excluded(modules):
    _, _, controller = modules
    row = observation("astra", status="completed", when=NOW)
    state = controller.run_state(None, "manifest", NOW)
    status = controller.snapshot([row], state, ["one"], NOW)
    assert status["answered"] == 0
    assert status["provider_attempts_this_run"] == 0
    assert status["target"] == 8


def test_original_status_and_secrets_are_preserved_safely(modules, monkeypatch):
    recovery, _, _ = modules
    monkeypatch.setenv("TEST_API_KEY", "secret-value-1234")
    error = RuntimeError("gateway rejected Bearer abc123 and secret-value-1234")
    error.status_code = 429
    error.headers = {"Retry-After": "3600", "Authorization": "secret-value-1234"}
    details = recovery.provider_error(error, NOW)
    assert details["category"] == "rate_limit"
    assert details["http_status"] == 429
    assert details["retry_after_seconds"] == 3600
    assert "secret-value" not in json.dumps(details)
    assert "abc123" not in json.dumps(details)
    assert recovery.provider_error(RuntimeError("no status"), NOW)["http_status"] is None


def test_history_rejects_conflicting_successes(modules, tmp_path):
    recovery, _, _ = modules
    batch = tmp_path / "batch"
    batch.mkdir()
    for i in range(2):
        row = observation(status="completed", case_input_hash=str(i))
        (batch / f"{i}.json").write_text(json.dumps(row))
    with pytest.raises(ValueError, match="conflicting completed inputs"):
        recovery.read_history(tmp_path, "manifest", {"one"})


def test_interrupted_intent_holds_both_keys_until_observation_exists(modules, tmp_path):
    recovery, _, _ = modules
    batches = tmp_path / "batches"
    intents = tmp_path / "intents"
    intents.mkdir()
    row = observation("gemini37", attempt_id="intent-one")
    (intents / "one.json").write_text(json.dumps(row))
    history = recovery.read_history(batches, "manifest", {"one"})
    for name in ("gemini37", "gemini37_secondary"):
        plan = recovery.candidate_plan(history, name, ["one"], NOW, NOW)
        assert plan["state"] == "unknown_outcome"
        assert plan["unknown_outcomes"] == 1
        assert not plan["eligible_case_ids"]
    batch = batches / "finished"
    batch.mkdir(parents=True)
    (batch / "one.json").write_text(json.dumps(row | {"status": "completed"}))
    history = recovery.read_history(batches, "manifest", {"one"})
    assert len(history) == 1
    assert (
        recovery.candidate_plan(history, "gemini37_secondary", ["one"], NOW, NOW)["state"]
        == "complete"
    )


def test_quota_wait_uses_later_worker_reset(modules, monkeypatch):
    _, runner, _ = modules

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    route = SimpleNamespace(
        route_id="gemini_3_7_flash_primary",
        model="gemini/test",
        provider="gemini",
        quota=SimpleNamespace(rpd=20, reset_timezone="America/Los_Angeles"),
    )
    reset = datetime(2026, 10, 13, 7, 1, tzinfo=UTC)
    cell = {"rpd_used": 20, "rpd_resets_at": int(reset.timestamp() * 1000)}
    client = SimpleNamespace(status=lambda _: {"routes": {route.route_id: cell}})
    monkeypatch.setattr(runner, "datetime", FrozenDatetime)
    monkeypatch.setattr(runner, "load_llm_budget_cas", lambda _: (SimpleNamespace(routes={}), None))
    enough, capacity = runner.headroom(client, object(), route)
    assert not enough
    assert capacity["kind"] == "daily_quota"
    assert capacity["next_eligible_at"] == reset.isoformat()
    cell["rpd_used"] = -1
    with pytest.raises(RuntimeError, match="invalid"):
        runner.headroom(client, object(), route)


def test_runner_keeps_original_429_before_attempt_budget_error(modules, monkeypatch, tmp_path):
    _, runner, _ = modules
    (tmp_path / "pilot-reviewed.json").write_text("{}")
    route = SimpleNamespace(
        route_id="beatapi_gpt_6_1_sol_free",
        provider="beatapi",
        model="test/model",
        upstream_model="model",
        direct_model="openai/model",
        hard_input_ceiling=None,
        input_context_limit=32768,
        output_context_limit=16384,
        reasoning_controls_json="",
        request_params_json="",
        quota=SimpleNamespace(rpm=1, rpd=1000),
    )

    class Backend:
        def __init__(self, *_args, **_kwargs):
            pass

        def _completion_fn(self):
            def rejected(**_kwargs):
                error = RuntimeError("upstream gateway throttle")
                error.status_code = 429
                error.headers = {"Retry-After": "3600"}
                raise error

            return rejected

        def run_immediate(self, _job):
            try:
                self._completion()
            except RuntimeError:
                raise RuntimeError("immediate provider-attempt limit reached") from None

    @contextmanager
    def window(*_args, **_kwargs):
        yield SimpleNamespace(drained=True)

    monkeypatch.setattr(runner, "OUT", tmp_path)
    monkeypatch.setattr(runner, "route_for", lambda _: route)
    monkeypatch.setattr(runner, "headroom", lambda *_: (True, {"kind": "available"}))
    monkeypatch.setattr(runner, "make_storage", lambda *_: SimpleNamespace(cas_capable=True))
    monkeypatch.setattr(
        runner, "DispatchPauseClient", lambda: SimpleNamespace(reserve=lambda *_: None)
    )
    monkeypatch.setattr(runner, "LiteLLMBackend", Backend)
    monkeypatch.setattr(runner, "paused", window)
    monkeypatch.setattr(sys, "argv", ["run.py", "--stage", "full", "--candidate", "sol"])
    runner.main()
    rows = [json.loads(p.read_text()) for p in (tmp_path / "batches").glob("*/*.json")]
    assert len(rows) == 1
    assert rows[0]["provider_attempts"] == 1
    assert rows[0]["provider_error"]["http_status"] == 429
    assert rows[0]["reason"] == "upstream gateway throttle"
    assert rows[0]["failure_category"] == "rate_limit"


def test_controller_refuses_existing_lock(modules, monkeypatch, tmp_path):
    import fcntl

    _, _, controller = modules
    (tmp_path / "pilot-reviewed.json").write_text("{}")
    with (tmp_path / "controller.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        monkeypatch.setattr(
            sys, "argv", ["backfill.py", "--execute", "--results-dir", str(tmp_path)]
        )
        with pytest.raises(SystemExit, match="already running"):
            controller.main()
