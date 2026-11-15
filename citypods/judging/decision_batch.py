"""Offline v1 tag and moment decision batches; no transport or model runtime is invoked here."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from citypods.judging.backends import Item

GLIDE_MAX_OPTIONS = 255


def canonical_json(value: Any) -> str:
    """Serialize a contract value deterministically, rejecting non-finite numbers."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _item_id(row: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(row).encode("utf-8")).hexdigest()


def build_items(items: Sequence[Item]) -> list[dict[str, Any]]:
    """Build sorted, content-addressed rows without carrying subject payload or truth."""
    rows = []
    for item in items:
        if len(item.subjects) != 1 or item.tier != "T1" or item.evidence.tier != item.tier:
            raise ValueError("expected one subject with T1 evidence")
        subject = item.subjects[0]
        allowed = {
            ("tag", "supported"): "validate",
            ("moment", "supported"): "validate",
            ("moment", "publishable"): "gate",
            ("moment", "usefulness"): "grade",
            ("moment", "best"): "choose",
        }
        if allowed.get((subject.task, item.question.id)) != item.question.kind:
            raise ValueError("unsupported task or question")
        if item.order not in (("", "_r") if item.question.kind == "choose" else ("",)):
            raise ValueError("unsupported question order")
        question: dict[str, Any] = {
            "type": {"validate": "noul", "gate": "noul", "grade": "score", "choose": "choice"}[
                item.question.kind
            ],
            "instructions": item.question.instruction,
        }
        subject_ids = None
        if item.question.kind == "grade":
            if not item.question.levels:
                raise ValueError("score needs criteria")
            question["criteria"] = list(item.question.levels)
        elif item.question.kind == "choose":
            subject_ids = dict(item.options)
            if not subject_ids or len(subject_ids) != len(item.options):
                raise ValueError("choice needs unique options")
            question["criteria"] = {letter: f"Quote {letter}" for letter in subject_ids}
        row = {
            "schema_version": 1,
            "task": subject.task,
            "episode_uid": subject.episode_uid,
            "subject_id": subject.subject_id,
            "question_id": item.question_id,
            "tier": item.tier,
            "prompt_version": item.question.prompt_version,
            "evidence_digest": item.evidence.digest,
            "state": item.evidence.text,
            "question": question,
        }
        if subject_ids is not None:
            row["subject_ids"] = subject_ids
        rows.append({"item_id": _item_id(row), **row})
    rows.sort(key=lambda row: row["item_id"])
    if len({row["item_id"] for row in rows}) != len(rows):
        raise ValueError("duplicate item IDs")
    return rows


def serialize_jsonl(rows: Sequence[Mapping[str, Any]]) -> str:
    """Write canonical UTF-8-ready JSONL in item-ID order."""
    return "".join(canonical_json(row) + "\n" for row in sorted(rows, key=lambda r: r["item_id"]))


def native_request(row: Mapping[str, Any]) -> dict[str, Any]:
    """Map one item to a native GLiDE request, retaining its exact answer key."""
    if type(row.get("schema_version")) is not int or row["schema_version"] != 1:
        raise ValueError("unsupported item schema version")
    state = row.get("state")
    if not isinstance(state, str):
        raise ValueError("expected text evidence")
    question = row.get("question")
    if not isinstance(question, Mapping) or not isinstance(question.get("instructions"), str):
        raise ValueError("invalid question")
    kind = question.get("type")
    criteria = question.get("criteria")
    if kind == "noul":
        valid = set(question) == {"type", "instructions"} and "subject_ids" not in row
    elif kind == "score":
        valid = (
            set(question) == {"type", "instructions", "criteria"}
            and isinstance(criteria, list)
            and 0 < len(criteria) <= GLIDE_MAX_OPTIONS
            and all(isinstance(value, str) for value in criteria)
            and "subject_ids" not in row
        )
    elif kind == "choice":
        subject_ids = row.get("subject_ids")
        valid = (
            set(question) == {"type", "instructions", "criteria"}
            and isinstance(criteria, Mapping)
            and 0 < len(criteria) <= GLIDE_MAX_OPTIONS
            and all(isinstance(k, str) and isinstance(v, str) for k, v in criteria.items())
            and isinstance(subject_ids, Mapping)
            and set(subject_ids) == set(criteria)
            and all(isinstance(v, str) for v in subject_ids.values())
        )
    else:
        valid = False
    if not valid:
        raise ValueError("unsupported or invalid question")
    item_id = row.get("item_id")
    if item_id != _item_id({key: value for key, value in row.items() if key != "item_id"}):
        raise ValueError("item ID does not match content")
    return {"state": state, "questions": {item_id: dict(question)}}


def _probability(value: Any, name: str) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    if not 0 <= value <= 1:
        raise ValueError(f"{name} must be finite and between zero and one")
    return value


def normalize_results(
    rows: Sequence[Mapping[str, Any]], responses: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Validate native successes as a complete one-to-one set, returning request order."""
    for row in rows:
        native_request(row)
    expected = [row["item_id"] for row in rows]
    by_id = {row["item_id"]: row for row in rows}
    expected_ids = set(expected)
    if len(expected_ids) != len(expected) or len(responses) != len(expected):
        raise ValueError("wrong request IDs or response cardinality")
    results: dict[str, dict[str, Any]] = {}
    for response in responses:
        if not isinstance(response, Mapping):
            raise ValueError("invalid response")
        if "status" in response:
            if set(response) != {"item_id", "status"} or response["status"] not in {
                "invalid_input",
                "too_large",
                "retryable_error",
            }:
                raise ValueError("invalid error result")
            item_id = response["item_id"]
            if item_id not in expected_ids or item_id in results:
                raise ValueError("unknown or duplicate answer ID")
            results[item_id] = {"schema_version": 1, **response}
            continue
        answers = response.get("answers")
        if not isinstance(answers, Mapping) or len(answers) != 1:
            raise ValueError("expected exactly one answer per response")
        item_id, answer = next(iter(answers.items()))
        if item_id not in expected_ids or item_id in results:
            raise ValueError("unknown or duplicate answer ID")
        question = by_id[item_id]["question"]
        kind = question["type"]
        if not isinstance(answer, Mapping) or answer.get("type") != kind:
            raise ValueError("answer type does not match question")
        normalized_answer: dict[str, Any] = {"type": kind}
        if kind == "noul":
            normalized_answer["noul"] = _probability(answer.get("noul"), "noul")
        elif kind == "score":
            score = answer.get("score")
            if type(score) is not int or not 0 <= score < len(question["criteria"]):
                raise ValueError("score outside criteria")
            normalized_answer["score"] = score
        else:
            choice = answer.get("choice")
            if not isinstance(choice, str) or choice not in question["criteria"]:
                raise ValueError("unknown choice")
            normalized_answer["choice"] = choice
        if "confidence" in answer:
            normalized_answer["confidence"] = _probability(answer["confidence"], "confidence")
        if "probabilities" in answer:
            probabilities = answer["probabilities"]
            keys = (
                set(question["criteria"])
                if kind == "choice"
                else {str(i) for i in range(len(question["criteria"]))}
                if kind == "score"
                else set()
            )
            if not isinstance(probabilities, Mapping) or not keys or set(probabilities) != keys:
                raise ValueError("invalid probability keys")
            normalized_answer["probabilities"] = {
                key: _probability(value, "probability") for key, value in probabilities.items()
            }
        if "expected_level" in answer:
            level = answer["expected_level"]
            if (
                kind != "score"
                or isinstance(level, bool)
                or not isinstance(level, (int, float))
                or not 0 <= level <= len(question["criteria"]) - 1
            ):
                raise ValueError("invalid expected level")
            normalized_answer["expected_level"] = level
        result: dict[str, Any] = {
            "schema_version": 1,
            "item_id": item_id,
            "status": "ok",
            "answer": normalized_answer,
        }
        usage = response.get("usage")
        if usage is not None:
            if not isinstance(usage, Mapping):
                raise ValueError("invalid usage")
            if "input_tokens" in usage:
                tokens = usage["input_tokens"]
                if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
                    raise ValueError("input_tokens must be a nonnegative integer")
                result["input_tokens"] = tokens
        results[item_id] = result
    return [results[item_id] for item_id in expected]
