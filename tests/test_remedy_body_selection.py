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
    assert len(selection.MODELS) == 6
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
    assert report["target"] == 360


def test_provider_call_intent_and_actual_answer_contract(runner, monkeypatch, tmp_path):
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
            return SimpleNamespace(output=self._completion(), upstream_model="model")

    @contextmanager
    def drained(*_args, **_kwargs):
        yield SimpleNamespace(drained=True)

    monkeypatch.setattr(runner.transport, "route_for", lambda _: route)
    monkeypatch.setattr(runner.transport, "LiteLLMBackend", Backend)
    monkeypatch.setattr(runner.transport, "paused", drained)
    client = SimpleNamespace(reserve=lambda *_: None)
    row = runner.attempt(tmp_path, "sol", case, 0, "manifest", object(), client)
    assert row["status"] == "completed"
    assert row["answer"]["feed_slugs"] == expected["feed_slugs"]
    assert row["provider_attempts"] == 1
    assert seen[0]["max_provider_attempts"] == 1
    assert seen[0]["num_retries"] == 0
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
