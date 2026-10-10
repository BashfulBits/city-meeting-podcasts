"""Judgments are appended to the judged candidate itself, never modified or deleted (review/53).

Following R6's ``judge_assessments`` precedent, a candidate carries two lists: ``judgments`` (the
append-only record) and ``judge_pending`` (the recipe that will answer each outstanding question,
so a packet can span episodes: each subject finds its own answer by recipe hash). Failed,
malformed or deferred calls write no judgment row.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

JUDGMENT_SCHEMA = 1
JUDGMENTS = "judgments"
PENDING = "judge_pending"

KEY_FIELDS = (
    "subject_id",
    "question_id",
    "judge_model",
    "prompt_version",
    "context_tier",
    "evidence_digest",
)


def key_of(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(row.get(field) for field in KEY_FIELDS)


def judgments(candidate: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [row for row in candidate.get(JUDGMENTS) or [] if isinstance(row, dict)]


def pending(candidate: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [row for row in candidate.get(PENDING) or [] if isinstance(row, dict)]


def has_key(candidate: Mapping[str, Any], key: tuple[Any, ...]) -> bool:
    return any(key_of(row) == key for row in judgments(candidate)) or any(
        key_of(row) == key for row in pending(candidate)
    )


def append_judgment(candidate: dict[str, Any], row: Mapping[str, Any]) -> bool:
    """Append ``row`` unless an identical key is already recorded; True when appended."""
    if any(key_of(existing) == key_of(row) for existing in judgments(candidate)):
        return False
    record = {"schema": JUDGMENT_SCHEMA, **row}
    record.setdefault("judged_at", datetime.now(UTC).isoformat(timespec="seconds"))
    candidate[JUDGMENTS] = [*judgments(candidate), record]
    return True


def add_pending(candidate: dict[str, Any], marker: Mapping[str, Any]) -> None:
    if any(key_of(row) == key_of(marker) for row in pending(candidate)):
        return
    candidate[PENDING] = [*pending(candidate), dict(marker)]


def drop_pending(candidate: dict[str, Any], marker: Mapping[str, Any]) -> None:
    remaining = [row for row in pending(candidate) if key_of(row) != key_of(marker)]
    if remaining:
        candidate[PENDING] = remaining
    else:
        candidate.pop(PENDING, None)


def anchor_value(
    candidate: Mapping[str, Any], subject_id: str, question_id: str, tier: str, anchor_model: str
) -> float | None:
    """The anchor's recorded probability for one question at one tier (escalation input)."""
    for row in judgments(candidate):
        if (
            row.get("subject_id") == subject_id
            and row.get("question_id") == question_id
            and row.get("context_tier") == tier
            and row.get("judge_model") == anchor_model
            and isinstance(row.get("value"), (int, float))
            and not isinstance(row.get("value"), bool)
        ):
            return float(row["value"])
    return None
