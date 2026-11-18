"""Offline Karpo adapter contract checks; no model or GPU required."""

import gzip
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from citypods.judging.decision_batch import canonical_json

WORKERS = Path(__file__).resolve().parents[1] / "workers/decision-batch"
sys.path.insert(0, str(WORKERS))
spec = importlib.util.spec_from_file_location("karpo_batch", WORKERS / "karpo_batch.py")
karpo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(karpo)
sys.path.remove(str(WORKERS))


class RequestError(Exception):
    pass


class LengthError(Exception):
    pass


def row(question, **extra):
    data = {
        "schema_version": 1,
        "state": "evidence",
        "question": question,
        **extra,
    }
    return {**data, "item_id": hashlib.sha256(canonical_json(data).encode()).hexdigest()}


def test_full_worker_replay_preserves_score_mean_and_mode(tmp_path):
    rows = [
        row({"type": "noul", "instructions": "yes?"}),
        row({"type": "score", "instructions": "grade", "criteria": ["a", "b", "c"]}),
        row(
            {"type": "choice", "instructions": "pick", "criteria": {"A": "a", "B": "b"}},
            subject_ids={"A": "a", "B": "b"},
        ),
        row({"type": "score", "instructions": "single", "criteria": ["a"]}),
        row({"type": "noul", "instructions": "too long"}),
    ]
    source, output = tmp_path / "input.gz", tmp_path / "output.gz"
    with gzip.open(source, "wt") as stream:
        for item in rows:
            stream.write(canonical_json(item) + "\n")

    class Model:
        def plan(self, request):
            if next(iter(request["questions"].values()))["instructions"] == "too long":
                raise LengthError()
            return ["plan"]

        def decide(self, requests, plans):
            assert plans == [["plan"]] * 3
            answers = [
                {"type": "noul", "noul": 0.8},
                {"type": "score", "score": 1.1, "probabilities": {"0": 0.4, "1": 0.1, "2": 0.5}},
                {"type": "choice", "choice": "B", "probabilities": {"A": 0.2, "B": 0.8}},
            ]
            return [
                {"answers": {next(iter(req["questions"])): answer}, "usage": {"input_tokens": 9}}
                for req, answer in zip(requests, answers, strict=True)
            ]

    adapter = karpo.KarpoAdapter(Model(), RequestError, LengthError)
    karpo.run_batch(source, output, lambda: (adapter, RequestError))
    with gzip.open(output, "rt") as stream:
        result = [json.loads(line) for line in stream]
    assert [item["item_id"] for item in result] == [item["item_id"] for item in rows]
    assert result[1]["answer"]["score"] == 2
    assert result[1]["answer"]["expected_level"] == 1.1
    assert result[1]["input_tokens"] == 9
    assert [item["status"] for item in result] == ["ok", "ok", "ok", "invalid_input", "too_large"]


@pytest.mark.parametrize(
    "probabilities,expectation",
    [({"0": 0.2, "1": 0.8}, 0.2), ({"0": float("nan"), "1": 0.5}, 0.5), ({"0": 0.4}, 0.4)],
)
def test_invalid_score_rejected(probabilities, expectation):
    request = {"questions": {"id": {"type": "score", "criteria": ["a", "b"]}}}
    response = {
        "answers": {"id": {"type": "score", "score": expectation, "probabilities": probabilities}}
    }
    with pytest.raises(ValueError):
        karpo.convert_response(request, response)


def test_tie_chooses_first_level():
    request = {"questions": {"id": {"type": "score", "criteria": ["a", "b"]}}}
    response = {
        "answers": {"id": {"type": "score", "score": 0.5, "probabilities": {"0": 0.5, "1": 0.5}}}
    }
    assert karpo.convert_response(request, response)["answers"]["id"]["score"] == 0
