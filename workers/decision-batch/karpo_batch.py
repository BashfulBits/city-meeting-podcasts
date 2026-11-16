"""Run frozen decision batches with a locally provisioned Karpo checkpoint.

Provision and verify the model checkpoint and pinned upstream runtime before invoking this worker.
Score outputs retain Karpo's expected level and probabilities; the integer score is the mode.
"""

from __future__ import annotations

import argparse
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from run_batch import run_batch


class KarpoAdapter:
    """Adapt Karpo's System One responses to the existing frozen worker contract."""

    def __init__(self, model: Any, request_error: type[Exception], length_error: type[Exception]):
        self.model = model
        self.request_error = request_error
        self.length_error = length_error

    def decide_batch(self, requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
        replies: list[dict[str, Any] | None] = [None] * len(requests)
        valid_requests, plans, positions = [], [], []
        for index, request in enumerate(requests):
            item_id, question = next(iter(request["questions"].items()))
            if question["type"] == "score" and len(question["criteria"]) < 2:
                replies[index] = {"item_id": item_id, "status": "invalid_input"}
                continue
            try:
                plan = self.model.plan(request)
            except self.length_error:
                replies[index] = {"item_id": item_id, "status": "too_large"}
                continue
            except self.request_error:
                replies[index] = {"item_id": item_id, "status": "invalid_input"}
                continue
            valid_requests.append(request)
            plans.append(plan)
            positions.append(index)
        if valid_requests:
            native = self.model.decide(valid_requests, plans=plans)
            if not isinstance(native, (list, tuple)) or len(native) != len(valid_requests):
                raise ValueError("wrong Karpo response cardinality")
            for index, request, response in zip(positions, valid_requests, native, strict=True):
                replies[index] = convert_response(request, response)
        if any(reply is None for reply in replies):
            raise ValueError("missing Karpo response")
        return replies


def convert_response(request: Mapping[str, Any], response: Any) -> dict[str, Any]:
    """Map a continuous Karpo score to mode plus its original expectation."""
    item_id, question = next(iter(request["questions"].items()))
    if not isinstance(response, Mapping) or not isinstance(response.get("answers"), Mapping):
        raise ValueError("invalid Karpo response")
    answers = response["answers"]
    if set(answers) != {item_id} or not isinstance(answers[item_id], Mapping):
        raise ValueError("unexpected Karpo answer ID")
    answer = dict(answers[item_id])
    if answer.get("type") != question["type"]:
        raise ValueError("unexpected Karpo answer type")
    if question["type"] == "score":
        levels = len(question["criteria"])
        probabilities = answer.get("probabilities")
        keys = [str(i) for i in range(levels)]
        if not isinstance(probabilities, Mapping) or set(probabilities) != set(keys):
            raise ValueError("invalid score probability keys")
        values = [probabilities[key] for key in keys]
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 <= value <= 1
            for value in values
        ) or not math.isclose(sum(values), 1.0, rel_tol=1e-5, abs_tol=1e-6):
            raise ValueError("invalid score distribution")
        expectation = answer.get("score")
        if (
            isinstance(expectation, bool)
            or not isinstance(expectation, (int, float))
            or not math.isfinite(expectation)
            or not math.isclose(
                expectation, sum(i * value for i, value in enumerate(values)), abs_tol=1e-5
            )
        ):
            raise ValueError("score expectation does not match distribution")
        answer["score"] = max(range(levels), key=values.__getitem__)
        answer["expected_level"] = expectation
    return {**response, "answers": {item_id: answer}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--token-budget", type=int, default=16384)
    parser.add_argument("--max-len", type=int, default=4096)
    args = parser.parse_args()
    if not args.model_dir.is_dir():
        parser.error("--model-dir must be an existing, verified local checkpoint")
    if args.token_budget <= 0 or args.max_len <= 0:
        parser.error("token limits must be positive")

    def load_model() -> tuple[Any, type[Exception]]:
        from karpo_decision.decider import Decider
        from karpo_decision.encoding import LengthError
        from karpo_decision.protocol import RequestError

        model = Decider.from_pretrained(
            str(args.model_dir.resolve()),
            device="cuda",
            token_budget=args.token_budget,
            max_length=args.max_len,
        )
        return KarpoAdapter(model, RequestError, LengthError), RequestError

    run_batch(args.input, args.output, load_model)


if __name__ == "__main__":
    main()
