"""The corrected evaluation selects bodies without suggested answers or implicit gold."""

import importlib.util
import json
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from citypods.remedy_evaluation import canonical_hash

DIRECTORY = Path(__file__).resolve().parents[1] / "evals/remedy/results/body-selection-2026-10-13"
NOW = datetime(2026, 10, 13, 15, tzinfo=UTC)


@pytest.fixture
def selection(monkeypatch):
    monkeypatch.syspath_prepend(str(DIRECTORY))
    spec = importlib.util.spec_from_file_location("body_selection", DIRECTORY / "body_selection.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "body_selection", module)
    spec.loader.exec_module(module)
    return module


def answer(case, feeds=None, action="assign_existing"):
    ref = case.evidence_refs[0].id
    return {
        "case_id": case.case_id,
        "bodies": [{"name": "Evidence-established body", "evidence_ref_ids": [ref]}],
        "feed_slugs": feeds or [case.existing_feeds[0]["slug"]],
        "publication_action": action,
        "evidence_ref_ids": [ref],
        "missing_evidence": [],
        "rationale": "Official evidence",
    }


def observation(selection, case, attempts=1, status="failed", **extra):
    return {
        "case_id": case.case_id,
        "candidate": "sol",
        "status": status,
        "provider_attempts": attempts,
        "observed_at": NOW.isoformat(),
        "finished_at": NOW.isoformat(),
        "failure_category": "server_error",
        "manifest_hash": "manifest",
        "mode": "blind_body_selection",
        "prompt_hash": "prompt",
        "schema_hash": "schema",
        "case_input_hash": "input",
        **extra,
    }


def test_sixty_answer_free_recordings(selection):
    _, cases = selection.load_manifest()
    assert len(cases) == len({(c.source_key, c.recordings[0].uid) for c in cases}) == 60
    assert len(selection.MODELS) == 8
    for case in cases:
        payload = json.loads(selection.messages(case)[1]["content"])
        assert "claim" not in payload
        assert "expected_feeds" not in payload
        assert "proposed_owner" not in payload
        assert "maintained_local_disposition" not in payload["policy"]
        assert all("source" not in feed for feed in payload["existing_feeds"])
        assert all(ref["id"] != "maintained-policy" for ref in payload["evidence_refs"])
        assert case.case_id.startswith("recording-")
        assert case.case_id not in payload["recordings"][0]["title"]


def test_frozen_hashes_bind_whole_input_and_gold_documents(selection):
    manifest, _ = selection.load_manifest()
    gold = json.loads((selection.PACKET / "gold.json").read_text())
    hashes = json.loads((selection.PACKET / "hashes-v2.json").read_text())
    assert hashes["manifest_hash"] == canonical_hash(manifest)
    assert hashes["gold_hash"] == canonical_hash(gold)


def test_joint_selection_is_a_set_not_a_single_owner(selection):
    _, cases = selection.load_manifest()
    case = cases[0]
    feeds = [f["slug"] for f in case.existing_feeds[:2]]
    parsed = selection.validate_answer(answer(case, feeds=feeds), case)
    truth = {
        "category": "existing_body_assignment",
        "expected_feeds": feeds[::-1],
        "expected_action": "assign_existing",
    }
    assert selection.score_answer(parsed, truth)["correct"]
    parsed.feed_slugs = feeds[:1]
    assert not selection.score_answer(parsed, truth)["correct"]


def test_unknown_owner_is_not_non_government_truth(selection):
    _, cases = selection.load_manifest()
    row = answer(cases[0])
    row.update(feed_slugs=[], publication_action="unresolved", missing_evidence=["Owner proof"])
    parsed = selection.validate_answer(row, cases[0])
    result = selection.score_answer(parsed, {"category": "insufficient_owner_proof"})
    assert result["body_assignment_correct"] is None
    assert result["guardrail_pass"]


@pytest.mark.parametrize("kind", ["feed", "evidence", "case", "duplicate"])
def test_untrusted_answer_references_fail(selection, kind):
    _, cases = selection.load_manifest()
    row = answer(cases[0])
    if kind == "feed":
        row["feed_slugs"] = ["invented"]
    elif kind == "evidence":
        row["evidence_ref_ids"] = ["invented"]
    elif kind == "case":
        row["case_id"] = "other"
    else:
        row["feed_slugs"] *= 2
    with pytest.raises(ValueError):
        selection.validate_answer(row, cases[0])


@pytest.mark.parametrize(
    "used,round_number,position",
    [(0, 1, 1), (3, 1, 4), (4, 2, 1), (7, 2, 4), (8, 3, 1), (11, 3, 4)],
)
def test_round_boundaries(selection, used, round_number, position):
    assert selection.attempt_position(used) == {
        "round": round_number,
        "attempt_in_round": position,
    }
    with pytest.raises(ValueError, match="exhausted"):
        selection.attempt_position(12)


def test_retry_cap_does_not_reset_with_date(selection):
    _, cases = selection.load_manifest()
    case = cases[0]
    rows = [observation(selection, case) for _ in range(12)]
    result = selection.plan(rows, "sol", [case], NOW + timedelta(days=2))
    assert result["state"] == "held_or_exhausted"
    assert result["exhausted"] == 1
    assert not result["eligible_case_ids"]


def test_completed_wrong_answer_is_never_retried(selection):
    _, cases = selection.load_manifest()
    case = cases[0]
    row = observation(selection, case, status="completed", answer={"wrong": True})
    assert selection.plan([row], "sol", [case], NOW)["state"] == "complete"


def test_cooldown_and_unknown_intent_hold(selection, tmp_path):
    _, cases = selection.load_manifest()
    case = cases[0]
    row = observation(selection, case, attempt_id="unknown")
    selection.write_exclusive(tmp_path / "intents/one.json", row)
    rows = selection.history(tmp_path, "manifest", {case.case_id})
    assert selection.plan(rows, "sol", [case], NOW + timedelta(days=1))["unknown_outcomes"] == 1
    selection.write_exclusive(tmp_path / "batches/one.json", row)
    rows = selection.history(tmp_path, "manifest", {case.case_id})
    assert len(rows) == 1
    assert selection.plan(rows, "sol", [case], NOW)["state"] == "cooldown"
    assert selection.plan(rows, "sol", [case], NOW + timedelta(minutes=30))["state"] == "ready"


def test_immutable_observations_and_context(selection, tmp_path):
    _, cases = selection.load_manifest()
    row = observation(selection, cases[0])
    selection.write_exclusive(tmp_path / "batches/one.json", row)
    with pytest.raises(FileExistsError):
        selection.write_exclusive(tmp_path / "batches/one.json", row)
    selection.write_exclusive(tmp_path / "batches/two.json", row | {"prompt_hash": "changed"})
    with pytest.raises(ValueError, match="context"):
        selection.history(tmp_path, "manifest", {cases[0].case_id})


@pytest.fixture
def runner(selection, monkeypatch):
    spec = importlib.util.spec_from_file_location("selection_runner", DIRECTORY / "run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dry_run_never_reads_gold_or_credentials(runner, monkeypatch, tmp_path, capsys):
    read = Path.read_text

    def safe_read(path, *args, **kwargs):
        if path.name == "gold.json":
            pytest.fail("runner must never read gold")
        return read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", safe_read)
    monkeypatch.setattr(
        runner.transport, "make_storage", lambda *_: pytest.fail("dry plan loaded credentials")
    )
    monkeypatch.setattr(sys, "argv", ["run.py", "--results-dir", str(tmp_path)])
    runner.main()
    report = json.loads(capsys.readouterr().out)
    assert report["answered"] == 0
    assert report["target"] == 480


@pytest.mark.parametrize("bypass", [False, True])
@pytest.mark.parametrize("candidate", ["flashlite", "glm_full", "sol", "gptoss", "qwen38"])
def test_provider_call_intent_and_actual_answer_contract(
    runner, monkeypatch, tmp_path, candidate, bypass
):
    _, cases = runner.load_manifest()
    case = cases[0]
    expected = answer(case)
    output = {
        "model": "model",
        "choices": [{"message": {"content": json.dumps(expected)}}],
    }
    route = SimpleNamespace(
        route_id="beatapi_gpt_6_1_sol_free",
        provider="beatapi",
        model="test/model",
        upstream_model="model",
        direct_model="openai/model",
        input_context_limit=32768,
        output_context_limit=16384,
        hard_input_ceiling=None,
        reasoning_controls_json="",
        request_params_json="",
    )
    if candidate == "flashlite":
        route.provider = "gemini"
    if candidate in runner.GROQ_CANDIDATES:
        route.provider = "groq"
        monkeypatch.setattr(
            runner, "token_headroom", lambda *_a, **_k: (True, {"kind": "available"})
        )
    seen = []

    class Backend:
        def __init__(self, *_args, **_kwargs):
            pass

        def _completion_fn(self):
            def completion(**_kwargs):
                intents = list((tmp_path / "intents").glob("*.json"))
                assert len(intents) == 1
                assert json.loads(intents[0].read_text())["provider_attempts"] == 1
                return output

            return completion

        def run_immediate(self, job):
            seen.append(job.inputs)
            return SimpleNamespace(
                output=self._completion(
                    extra_body={"reasoning_effort": "high", "reasoning_format": "parsed"}
                ),
                upstream_model="model",
            )

    pause_options = []

    @contextmanager
    def drained(*_args, **kwargs):
        assert not bypass
        pause_options.append(kwargs)
        yield SimpleNamespace(drained=True)

    monkeypatch.setattr(runner.transport, "route_for", lambda _: route)
    monkeypatch.setattr(runner, "route_for", lambda _: route)
    monkeypatch.setattr(runner.transport, "LiteLLMBackend", Backend)
    monkeypatch.setattr(
        runner.transport, "headroom", lambda *_a, **_k: (True, {"kind": "available"})
    )
    monkeypatch.setattr(runner.transport, "paused", drained)

    def reserve(*_args):
        assert not bypass

    client = SimpleNamespace(reserve=reserve)
    row = runner.attempt(
        tmp_path, candidate, case, 0, "manifest", object(), client, coordination_bypass=bypass
    )
    assert row["status"] == "completed"
    assert row["answer"]["feed_slugs"] == expected["feed_slugs"]
    assert row["provider_attempts"] == 1
    assert seen[0]["max_provider_attempts"] == 1
    assert seen[0]["num_retries"] == 0
    timeout = 120 if candidate == "flashlite" else 900
    deadline = 150 if candidate == "flashlite" else 950
    assert row["request_timeout"] == seen[0]["timeout"] == timeout
    start = datetime.fromisoformat(row["observed_at"])
    budget = (seen[0]["llm_policy"].deadline_at - start).total_seconds()
    assert deadline <= budget < deadline + 5
    assert row["coordination_bypass"] == bypass
    assert row["production_drained"] == (not bypass)
    intent = json.loads(next((tmp_path / "intents").glob("*.json")).read_text())
    assert intent["coordination_bypass"] == bypass
    if bypass:
        assert not pause_options
        assert "maintainer chat2026-10-13" in row["coordination_approval_ref"]
    else:
        assert pause_options[0]["seconds"] == timeout + 100
    if candidate in runner.GROQ_CANDIDATES:
        assert row["submitted_reasoning_controls"]["reasoning_effort"] == "high"
        assert seen[0]["reasoning_level"] == "high"
    assert "claim" not in json.loads(seen[0]["messages"][1]["content"])


def test_shared_lock_blocks_calls(runner, monkeypatch, tmp_path):
    import fcntl

    shared = tmp_path / "shared.lock"
    results = tmp_path / "results"
    monkeypatch.setattr(runner.transport, "load_site_config", lambda _: {})
    monkeypatch.setattr(
        runner.transport, "make_storage", lambda *_: SimpleNamespace(cas_capable=True)
    )
    monkeypatch.setattr(runner.transport, "DispatchPauseClient", lambda: object())
    monkeypatch.setattr(runner.transport, "headroom", lambda *_: (True, {"kind": "available"}))
    monkeypatch.setattr(runner, "attempt", lambda *_: pytest.fail("shared lock was bypassed"))
    monkeypatch.setattr(
        sys,
        "argv",
        ["run.py", "--execute", "--results-dir", str(results), "--shared-lock", str(shared)],
    )

    def interrupted(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(runner.time, "sleep", interrupted)
    with shared.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(KeyboardInterrupt):
            runner.main()
    status = json.loads((results / "controller-status.json").read_text())
    assert status["waiting_for_shared_lock"]
    assert status["provider_attempts"] == 0


@pytest.fixture
def priority():
    spec = importlib.util.spec_from_file_location("call_priority", DIRECTORY / "call_priority.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def older_status(**extra):
    return {
        "updated_at": NOW.isoformat(),
        "manifest_hash": "old-manifest",
        "run_id": "old-run",
        "provider_attempts_this_run": 5,
        "stop_reason": None,
        "answered": 676,
        "target": 678,
        "routes": {"glm_full": {"state": "runner_busy"}},
        **extra,
    }


def test_two_old_submissions_per_new_slot_and_resume(priority, tmp_path):
    status = older_status()
    policy = priority.bind_priority(tmp_path, status, NOW)
    assert not priority.allocation(policy, status, NOW)["allow_new"]
    status["provider_attempts_this_run"] = 6
    assert not priority.allocation(policy, status, NOW)["allow_new"]
    status["provider_attempts_this_run"] = 7
    assert priority.allocation(policy, status, NOW)["contested"]
    assert priority.allocation(policy, status, NOW)["allow_new"]
    policy["new_contested_slots"].append("submitted-or-unknown")
    policy = json.loads(json.dumps(policy))
    assert not priority.allocation(policy, status, NOW)["allow_new"]
    status["provider_attempts_this_run"] = 9
    assert priority.allocation(policy, status, NOW)["allow_new"]


@pytest.mark.parametrize("state", ["cooldown", "daily_quota", "provider_busy", "held_or_exhausted"])
def test_borrowing_does_not_consume_contested_slot(priority, tmp_path, state):
    status = older_status(routes={"glm_full": {"state": state}})
    policy = priority.bind_priority(tmp_path, status, NOW)
    report = priority.allocation(policy, status, NOW)
    assert report["allow_new"] and not report["contested"]
    assert policy["new_contested_slots"] == []
    status["routes"]["glm_full"]["state"] = "ready"
    assert not priority.allocation(policy, status, NOW)["allow_new"]


@pytest.mark.parametrize(
    "change",
    [
        {"run_id": "different"},
        {"manifest_hash": "different"},
        {"provider_attempts_this_run": 4},
        {"provider_attempts_this_run": True},
        {"updated_at": (NOW - timedelta(minutes=11)).isoformat()},
        {"updated_at": (NOW + timedelta(minutes=1)).isoformat()},
    ],
)
def test_priority_invalid_or_stale_status_holds_new_calls(priority, tmp_path, change):
    status = older_status()
    policy = priority.bind_priority(tmp_path, status, NOW)
    assert not priority.allocation(policy, {**status, **change}, NOW)["allow_new"]


def test_priority_completed_or_stopped_releases_new_calls(priority, tmp_path):
    status = older_status()
    policy = priority.bind_priority(tmp_path, status, NOW)
    assert priority.allocation(policy, {**status, "answered": 678}, NOW)["allow_new"]
    assert priority.allocation(policy, {**status, "stop_reason": "exhausted"}, NOW)["allow_new"]
    assert priority.allocation(policy, {}, NOW, active=False)["allow_new"]


def test_priority_unreadable_active_status_holds_calls(priority, tmp_path, monkeypatch):
    policy = priority.bind_priority(tmp_path, older_status(), NOW)
    monkeypatch.setattr(priority, "controller_active", lambda directory: True)
    assert not priority.inspect_priority(policy, NOW)["allow_new"]


def test_priority_controller_lock_detection(priority, tmp_path):
    import fcntl

    path = tmp_path / "controller.lock"
    assert not priority.controller_active(tmp_path)
    with path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert priority.controller_active(tmp_path)
    assert not priority.controller_active(tmp_path)


def prepare_priority_runner(runner, monkeypatch, tmp_path):
    older = tmp_path / "older"
    older.mkdir()
    (older / "controller-status.json").write_text(json.dumps(older_status()))
    results = tmp_path / "new"
    monkeypatch.setattr(runner.transport, "load_site_config", lambda _: {})
    monkeypatch.setattr(
        runner.transport, "make_storage", lambda *_: SimpleNamespace(cas_capable=True)
    )
    monkeypatch.setattr(runner.transport, "DispatchPauseClient", lambda: object())
    monkeypatch.setattr(runner.transport, "headroom", lambda *_: (True, {"kind": "available"}))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run.py",
            "--execute",
            "--results-dir",
            str(results),
            "--shared-lock",
            str(older / "run.lock"),
            "--priority-results-dir",
            str(older),
        ],
    )

    def interrupted(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(runner.time, "sleep", interrupted)
    return results


def test_priority_yield_blocks_calls_and_preserves_baseline_on_resume(
    runner, monkeypatch, tmp_path
):
    results = prepare_priority_runner(runner, monkeypatch, tmp_path)
    monkeypatch.setattr(
        runner, "inspect_priority", lambda *_: {"allow_new": False, "reason": "yielding_to_older"}
    )
    monkeypatch.setattr(runner, "attempt", lambda *_: pytest.fail("priority was bypassed"))
    for _ in range(2):
        with pytest.raises(KeyboardInterrupt):
            runner.main()
        saved = json.loads((results / "controller-status.json").read_text())
        assert saved["call_priority"]["older_baseline_attempts"] == 5
        assert saved["call_priority"]["new_contested_slots"] == []
        assert saved["provider_attempts"] == 0


@pytest.mark.parametrize("submitted", [0, 1])
def test_priority_reserves_before_call_but_releases_preflight_deferral(
    runner, monkeypatch, tmp_path, submitted
):
    results = prepare_priority_runner(runner, monkeypatch, tmp_path)
    monkeypatch.setattr(
        runner, "inspect_priority", lambda *_: {"allow_new": True, "contested": True}
    )

    def attempted(*_args):
        saved = json.loads((results / "controller-status.json").read_text())
        assert len(saved["call_priority"]["new_contested_slots"]) == 1
        monkeypatch.setattr(
            runner, "inspect_priority", lambda *_: {"allow_new": False, "reason": "yield"}
        )
        return {"status": "deferred", "provider_attempts": submitted}

    monkeypatch.setattr(runner, "attempt", attempted)
    with pytest.raises(KeyboardInterrupt):
        runner.main()
    saved = json.loads((results / "controller-status.json").read_text())
    assert len(saved["call_priority"]["new_contested_slots"]) == submitted


def test_approved_model_expansion_preserves_original_deadline(selection):
    state = {
        "models": {name: {} for name in selection.BASE_MODELS},
        "max_provider_attempts": 4320,
        "started_at": NOW.isoformat(),
        "deadline_at": (NOW + timedelta(days=7)).isoformat(),
    }
    selection.expand_model_set(state, 60, NOW + timedelta(hours=1))
    assert state["max_provider_attempts"] == 5760
    assert state["started_at"] == NOW.isoformat()
    assert state["deadline_at"] == (NOW + timedelta(days=7)).isoformat()
    assert len(state["model_set_history"]) == 1
    selection.expand_model_set(state, 60, NOW + timedelta(hours=2))
    assert len(state["model_set_history"]) == 1
    with pytest.raises(ValueError, match="unapproved"):
        selection.expand_model_set({"max_provider_attempts": 9000}, 60, NOW)


@pytest.mark.parametrize("candidate", ["gptoss", "qwen38"])
def test_groq_pilot_keeps_full_inputs_and_prioritizes_largest_packets(selection, candidate):
    _, cases = selection.load_manifest()
    saved = canonical_hash([c.model_dump() for c in cases])
    eligible = selection.plan([], candidate, cases, NOW)["eligible_case_ids"]
    by_id = {c.case_id: c for c in cases}
    assert [by_id[c].recordings[0].uid for c in eligible[:4]] == list(selection.GROQ_PILOT_UIDS)
    assert len(eligible) == 60
    assert canonical_hash([c.model_dump() for c in cases]) == saved


@pytest.fixture
def groq(selection, monkeypatch):
    spec = importlib.util.spec_from_file_location("groq_routes", DIRECTORY / "groq_routes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for rid, _effort in module.CANDIDATES.values():
        route = module.ROUTE_REGISTRY[rid]
        monkeypatch.setitem(module.ROUTE_REGISTRY, rid, route)
        monkeypatch.setitem(
            module.ROUTE_CANDIDATES, route.model, module.ROUTE_CANDIDATES[route.model]
        )
        monkeypatch.setitem(module.LOGICAL_ROUTES, route.model, module.LOGICAL_ROUTES[route.model])

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, _tz=None):
            return NOW

    monkeypatch.setattr(module, "datetime", FixedDatetime)
    return module


@pytest.mark.parametrize("candidate", ["gptoss", "qwen38"])
def test_groq_high_controls_and_unchanged_limits(groq, runner, candidate):
    from citypods.compute.base import InferenceJob
    from citypods.compute.llm import LiteLLMBackend, LLMBackendConfig
    from citypods.compute.structured import register_response_model

    original = groq.ROUTE_REGISTRY[groq.CANDIDATES[candidate][0]]
    route = groq.route_for(candidate)
    assert route.quota.rpm == original.quota.rpm
    assert route.quota.rpd == original.quota.rpd
    assert route.quota.tpm == original.quota.tpm
    assert route.hard_input_ceiling == original.hard_input_ceiling
    assert route.quota.tpd == 180000
    assert route.output_context_limit == 16384
    assert route.free
    register_response_model("groq-payload-test", runner.BodySelectionAnswer)
    job = InferenceJob(
        task="tag",
        recipe_hash="test",
        inputs={
            "messages": [{"role": "user", "content": "Frozen evidence"}],
            "structured_output": "groq-payload-test",
            "reasoning_level": "high",
            "max_tokens": 16384,
        },
    )
    backend = LiteLLMBackend(LLMBackendConfig(model=route.model))
    payload = backend._payload(
        job,
        model=runner.BodySelectionAnswer,
        resolved_model=route.direct_model,
        route=route,
        direct=True,
    )
    assert payload["extra_body"]["reasoning_effort"] == "high"
    assert payload["extra_body"]["reasoning_format"] == "parsed"
    assert payload["response_format"]["type"] == "json_object"
    assert payload["model"] == route.direct_model
    assert "BodySelectionAnswer" in str(payload["messages"])


def quota_fixture(groq, worker_used=0, local_used=0):
    route = groq.route_for("qwen38")
    stats = {
        "now": int(NOW.timestamp() * 1000),
        "routes": {"total": 1, "all": [{"route_id": route.route_id, "tpd_used": worker_used}]},
    }
    client = SimpleNamespace(_request=lambda *_: stats)
    budget = SimpleNamespace(
        routes={
            route.route_id: SimpleNamespace(
                tokens_day=local_used,
                tokens_day_updated_at=NOW.isoformat(),
            )
        }
    )
    transport = SimpleNamespace(load_llm_budget_cas=lambda _: (budget, None))
    return route, stats, client, transport


def test_groq_daily_tokens_include_both_stores_and_production_reserve(groq):
    route, _stats, client, transport = quota_fixture(groq, 100000, 50000)
    enough, capacity = groq.token_headroom(transport, client, object(), route, 23060)
    assert not enough
    assert capacity["conservative_tokens_used"] == 150000
    assert capacity["production_token_reserve"] == 36000
    assert capacity["kind"] == "daily_quota"
    route, _stats, client, transport = quota_fixture(groq, 60000, 50000)
    assert groq.token_headroom(transport, client, object(), route, 23060)[0]


@pytest.mark.parametrize("problem", ["missing", "partial", "stale", "negative", "nan", "boolean"])
def test_groq_invalid_daily_usage_holds_requests(groq, problem):
    route, stats, client, transport = quota_fixture(groq)
    if problem == "missing":
        stats["routes"]["all"][0]["route_id"] = "different"
    elif problem == "partial":
        stats["routes"]["total"] = 2
    elif problem == "stale":
        stats["now"] -= 61000
    else:
        stats["routes"]["all"][0]["tpd_used"] = {
            "negative": -1,
            "nan": float("nan"),
            "boolean": True,
        }[problem]
    with pytest.raises(ValueError):
        groq.token_headroom(transport, client, object(), route, 23060)


@pytest.mark.parametrize("enough", [False, True])
def test_bypass_qwen_uses_local_capacity_without_worker(runner, monkeypatch, enough):
    _, cases = runner.load_manifest()
    route = runner.route_for("qwen38")
    monkeypatch.setattr(
        runner.transport,
        "headroom",
        lambda *_a, **kw: (kw["coordination_bypass"], {"kind": "available"}),
    )

    def tokens(_transport, client, _storage, chosen, _tokens, coordination_bypass=False):
        assert client is None
        assert chosen.route_id == route.route_id
        assert coordination_bypass
        return enough, {
            "kind": "available" if enough else "daily_quota",
            "worker_tokens_used": None,
            "usage_visibility": "local_only",
        }

    monkeypatch.setattr(runner, "token_headroom", tokens)
    permitted, capacity = runner.headroom(None, object(), route, cases[0], True)
    assert permitted == enough
    assert capacity["worker_tokens_used"] is None
    assert capacity["usage_visibility"] == "local_only"


@pytest.mark.parametrize("used,expected", [(0, True), (150000, False)])
def test_groq_bypass_keeps_local_token_reserve_without_worker(groq, used, expected):
    route, _stats, client, transport = quota_fixture(groq, local_used=used)
    client._request = lambda *_a, **_k: pytest.fail("bypass contacted Worker")
    enough, capacity = groq.token_headroom(
        transport, client, object(), route, 23060, coordination_bypass=True
    )
    assert enough == expected
    assert capacity["worker_tokens_used"] is None
    assert capacity["usage_visibility"] == "local_only"
    assert capacity["local_tokens_used"] == used
    assert capacity["production_token_reserve"] == 36000


@pytest.mark.parametrize("obsolete", [False, True])
def test_qwen_resume_clears_only_obsolete_hold(runner, monkeypatch, tmp_path, obsolete):
    frozen, _cases = runner.load_manifest()
    shared = tmp_path / "shared.lock"
    next_time = (datetime.now(UTC) + timedelta(minutes=15)).isoformat()
    status = {
        "manifest_hash": canonical_hash(frozen),
        "prompt_hash": canonical_hash(runner.PROMPT),
        "schema_hash": canonical_hash(runner.BodySelectionAnswer.model_json_schema()),
        "shared_lock": str(shared.resolve()),
        "started_at": NOW.isoformat(),
        "deadline_at": (NOW + timedelta(days=7)).isoformat(),
        "max_provider_attempts": 5760,
        "coordination_bypass": True,
        "models": {
            "qwen38": {
                "state": "capacity_unknown" if obsolete else "daily_quota",
                "next_eligible_at": next_time,
                "capacity": {
                    "reason": "Qwen held: bypass" if obsolete else "local tokens exhausted"
                },
            }
        },
    }
    path = tmp_path / "controller-status.json"
    path.write_text(json.dumps(status))

    def stop_before_storage(_path):
        raise KeyboardInterrupt

    monkeypatch.setattr(runner.transport, "load_site_config", stop_before_storage)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run.py",
            "--execute",
            "--coordination-bypass",
            "--results-dir",
            str(tmp_path),
            "--shared-lock",
            str(shared),
        ],
    )
    with pytest.raises(KeyboardInterrupt):
        runner.main()
    saved = json.loads(path.read_text())
    assert saved["models"]["qwen38"]["next_eligible_at"] == (None if obsolete else next_time)
    assert saved["qwen_bypass_enabled"]
    for key in ("started_at", "deadline_at", "max_provider_attempts"):
        assert saved[key] == status[key]
