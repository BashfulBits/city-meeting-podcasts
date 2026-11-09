"""Reconstruction must work from retained inputs, not ignored run directories."""

import hashlib
import json

import pytest

from evals.remedy import accepted_pairs
from evals.remedy.evidence_archive import (
    PAIRS,
    ROOT,
    archive_observations,
    load_observations,
    rebuild_packets,
)


def test_rebuilt_judge_packets_match_original_approved_bytes(tmp_path):
    output = tmp_path / "rebuilt"
    rebuild_packets(output)
    approval = json.loads((PAIRS / "adjudication-approval.json").read_text())
    for ref in approval["packets"].values():
        assert hashlib.sha256((output / ref["path"]).read_bytes()).hexdigest() == ref["sha256"]
    with pytest.raises(FileExistsError):
        rebuild_packets(output)


def test_profiles_factor_metadata_without_losing_actual_answers_or_provenance(tmp_path):
    rows = [
        dict(
            case_id=f"C{i}",
            route_id="route",
            provider="provider",
            requested_model="model",
            normalized={"decision": {"action": "manual_review", "rationale": "Observed answer"}},
            original_result_sha256=str(i) * 64,
        )
        for i in range(2)
    ]
    original = tmp_path / "original.json"
    original.write_text(json.dumps({"observed_results": rows, "case_outcomes": ["derived"]}))
    destination = tmp_path / "answers.json"
    archive_observations(original, destination)
    compact = json.loads(destination.read_text())
    assert len(compact["profiles"]) == 1 and "case_outcomes" not in compact["metadata"]
    assert load_observations(destination)["observed_results"] == rows
    assert (
        compact["metadata"]["original_artifact_sha256"]
        == hashlib.sha256(original.read_bytes()).hexdigest()
    )
    with pytest.raises(FileExistsError):
        archive_observations(original, destination)


def test_retained_evidence_still_scores_pairs_and_new_model_without_raw_archive():
    evidence = accepted_pairs.read(accepted_pairs.DEFAULT)
    before = accepted_pairs.score(evidence)
    assert before["cohort"] == 60 and len(before["pairs"]) == 10
    assert before["cascades"][0]["counts"] == {
        "correct_resolution": 4,
        "manual_disagreement_or_abstention": 3,
    }
    added = [
        dict(case_id=key.split("/", 1)[1], answer=row["answer"])
        for key, row in evidence["initial"].items()
        if key.startswith("deepseek/")
    ]
    expanded = accepted_pairs.score(evidence, [("new-model-profile", added)])
    assert expanded["coverage"]["new-model-profile"] == 60
    assert expanded["coverage"]["deepseek"] == before["coverage"]["deepseek"]
    with pytest.raises(ValueError, match="distinct model/profile"):
        accepted_pairs.score(evidence, [("deepseek", added)])
    answers = load_observations(ROOT / "evals/remedy/qualification/runtime-v2/answers.json")
    assert len(answers["observed_results"]) == 39  # Includes the preserved initial 429.
    assert sum(row["status"] == "completed" for row in answers["observed_results"]) == 38
