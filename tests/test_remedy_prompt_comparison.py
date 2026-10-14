"""General recipes and consensus metrics are bounded, isolated and reproducible."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "evals/remedy/results/prompt-comparison-2026-10-14"


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(DIRECTORY))
    monkeypatch.syspath_prepend(str(ROOT / "evals/remedy/results/body-selection-2026-10-13"))
    result = []
    for name in ["variants", "run", "score"]:
        spec = importlib.util.spec_from_file_location(
            f"comparison_{name}", DIRECTORY / f"{name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result.append(module)
    return result


def test_all540_jobs_are_unique_and_input_context_stays_frozen(modules):
    variants, run, _score = modules
    _, cases = run.load_manifest()
    jobs = run.jobs(cases)
    assert len(jobs) == len({(m, v, c.case_id) for m, v, c in jobs}) == 540
    assert set(m for m, _v, _c in jobs) == {"sol", "glm_flash", "deepseek"}
    for case in cases:
        baseline = variants.request_messages(case, "original")
        for variant in variants.VARIANTS:
            payload = variants.request_messages(case, variant)
            assert payload[1] == baseline[1]
            assert "dallas-tx" not in payload[0]["content"]
            assert "expected_feeds" not in payload[0]["content"]
            assert payload[0]["content"] == variants.VARIANTS[variant]


@pytest.mark.parametrize("state", ["complete", "exhausted", "unknown_outcome", "ready"])
def test_retry_and_unknown_outcome_bounds(modules, state):
    _variants, run, _score = modules
    row = {"candidate": "sol", "variant": "original", "case_id": "one", "provider_attempts": 1}
    if state == "complete":
        rows = [row | {"status": "completed"}]
    elif state == "unknown_outcome":
        rows = [row | {"status": "pending", "unknown_outcome": True}]
    elif state == "exhausted":
        rows = [row | {"status": "failed"} for _ in range(4)]
    else:
        rows = [row | {"status": "failed"} for _ in range(3)]
    assert run.pair_state(rows, "sol", "original", "one")[0] == state


def test_binding_changes_with_coordination_and_prompt(modules, monkeypatch):
    variants, run, _score = modules
    frozen, _cases = run.load_manifest()
    original = run.binding_for(frozen, False)
    assert original != run.binding_for(frozen, True)
    changed = dict(variants.VARIANTS) | {"self_check": "different"}
    monkeypatch.setattr(run, "VARIANTS", changed)
    assert original != run.binding_for(frozen, False)


def test_missing_intent_holds_and_foreign_results_fail(modules, tmp_path):
    _variants, run, _score = modules
    (tmp_path / "batches").mkdir()
    (tmp_path / "intents").mkdir()
    row = {
        "candidate": "sol",
        "mode": "prompt_comparison/original",
        "manifest_hash": "binding",
        "provider_attempts": 1,
        "attempt_id": "unreturned",
        "case_id": "one",
    }
    (tmp_path / "intents/one.json").write_text(json.dumps(row))
    rows = run.observations(tmp_path, "binding")
    assert rows[0]["unknown_outcome"]
    assert run.pair_state(rows, "sol", "original", "one")[0] == "unknown_outcome"
    with pytest.raises(ValueError, match="foreign"):
        run.observations(tmp_path, "another")


def test_shared_wrong_is_split_into_extra_and_missing_feeds(modules):
    _variants, _run, score = modules
    truth = {
        "category": "existing_body_assignment",
        "expected_feeds": ["a", "b"],
        "expected_action": "assign_existing",
    }
    wrong = SimpleNamespace(feed_slugs=["a", "c"], publication_action="assign_existing")
    assert score.compare(wrong, wrong, truth) == "wrong_agreement"
    errors = score.feed_errors(wrong, truth)
    assert errors["fp"] == ["c"] and errors["fn"] == ["b"]
    correct = SimpleNamespace(feed_slugs=["b", "a"], publication_action="assign_existing")
    assert score.compare(correct, correct, truth) == "correct_agreement"
    assert score.compare(wrong, correct, truth) == "disagreement"


def test_unknown_owner_does_not_fabricate_feed_truth(modules):
    _variants, _run, score = modules
    answer = SimpleNamespace(feed_slugs=[], publication_action="unconfigured_body")
    truth = {"category": "insufficient_owner_proof"}
    assert score.feed_errors(answer, truth) == {"fp": None, "fn": None, "unknown_owner": True}
    assert score.compare(answer, answer, truth) == "wrong_agreement"


@pytest.mark.parametrize("with_intent", [False, True])
def test_checkpoint_only_commits_observation_paths(modules, monkeypatch, tmp_path, with_intent):
    _variants, run, _score = modules
    calls = []
    directory = tmp_path / "observations"
    for name in ["batches", "intents"]:
        (directory / name).mkdir(parents=True)
        if name == "batches" or with_intent:
            (directory / name / "one.json").write_text("{}")
    monkeypatch.setattr(run, "ROOT", tmp_path)

    def subprocess(cmd, **_kwargs):
        calls.append(cmd)
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(run.subprocess, "run", subprocess)
    run.checkpoint(directory, "feat/p1-body-selection-evaluation")
    assert len(calls) == 3
    assert calls[-1][-1] == "HEAD:feat/p1-body-selection-evaluation"
    expected = ["observations/batches/one.json"]
    if with_intent:
        expected.append("observations/intents/one.json")
    assert calls[0][3:] == expected


def test_checkpoint_refuses_unrelated_staged_code(modules, monkeypatch):
    _variants, run, _score = modules

    def subprocess(cmd, **_kwargs):
        if "push" in cmd:
            pytest.fail("Unrelated code pushed")
        return SimpleNamespace(stdout="citypods/cli.py\n" if "diff" in cmd else "")

    monkeypatch.setattr(run.subprocess, "run", subprocess)
    with pytest.raises(RuntimeError, match="Unrelated"):
        run.checkpoint(run.OUT, "branch")
