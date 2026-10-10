"""Judgments live in the episode's own ``judging`` block, never in candidate dicts (review/53).

``Episode.judging`` is an artifact block owned only by the ``judge`` lane (records.py
``_LANE_OWNED_BLOCKS``), so the judge lane never rewrites the tag/moment candidates other lanes own
and rebuild, and those lanes never drop a judgment. Per judged subject it holds ``judgments`` (the
append-only record, never modified or deleted) and ``pending`` (the recipe that will answer each
outstanding question, so a packet may span episodes). Failed, malformed or deferred calls write no
judgment row. ``unbuildable`` records a (question, judge, tier) whose evidence could not be built
from the current transcript, so an unchanged episode is not revisited for it every pass.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

JUDGMENT_SCHEMA = 1

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


def _entry(subject: Any, *, create: bool) -> dict[str, Any] | None:
    store = subject.store
    subjects = store.get("subjects")
    if not isinstance(subjects, dict):
        if not create:
            return None
        subjects = store["subjects"] = {}
    entry = subjects.get(subject.subject_id)
    if not isinstance(entry, dict):
        if not create:
            return None
        entry = subjects[subject.subject_id] = {"task": subject.task}
    return entry


def _rows(subject: Any, name: str) -> list[dict[str, Any]]:
    entry = _entry(subject, create=False)
    return [row for row in (entry or {}).get(name) or [] if isinstance(row, dict)]


def judgments(subject: Any) -> list[dict[str, Any]]:
    return _rows(subject, "judgments")


def pending(subject: Any) -> list[dict[str, Any]]:
    return _rows(subject, "pending")


def has_key(subject: Any, key: tuple[Any, ...]) -> bool:
    return any(key_of(row) == key for row in judgments(subject)) or any(
        key_of(row) == key for row in pending(subject)
    )


def append_judgment(subject: Any, row: Mapping[str, Any]) -> bool:
    """Append ``row`` unless an identical key is already recorded; True when appended."""
    if any(key_of(existing) == key_of(row) for existing in judgments(subject)):
        return False
    record = {"schema": JUDGMENT_SCHEMA, **row}
    record.setdefault("judged_at", datetime.now(UTC).isoformat(timespec="seconds"))
    entry = _entry(subject, create=True)
    entry["judgments"] = [*judgments(subject), record]
    return True


def add_pending(subject: Any, marker: Mapping[str, Any]) -> None:
    if any(key_of(row) == key_of(marker) for row in pending(subject)):
        return
    entry = _entry(subject, create=True)
    entry["pending"] = [*pending(subject), dict(marker)]


def drop_pending(subject: Any, marker: Mapping[str, Any]) -> None:
    entry = _entry(subject, create=False)
    if entry is None:
        return
    remaining = [row for row in pending(subject) if key_of(row) != key_of(marker)]
    if remaining:
        entry["pending"] = remaining
    else:
        entry.pop("pending", None)


def unbuildable_key(question_id: str, judge_model: str, prompt_version: str, tier: str) -> str:
    return f"{question_id}|{judge_model}|{prompt_version}|{tier}"


def unbuildable(subject: Any) -> dict[str, str]:
    """(question|judge|prompt|tier) -> the transcript identity it could not be built from."""
    entry = _entry(subject, create=False)
    value = (entry or {}).get("unbuildable")
    return value if isinstance(value, dict) else {}


def mark_unbuildable(subject: Any, key: str, transcript_identity: str) -> None:
    if unbuildable(subject).get(key) == transcript_identity:
        return
    entry = _entry(subject, create=True)
    entry["unbuildable"] = {**unbuildable(subject), key: transcript_identity}


def anchor_value(subject: Any, question_id: str, tier: str, anchor_model: str) -> float | None:
    """The anchor's recorded probability for one question at one tier (escalation input)."""
    for row in judgments(subject):
        if (
            row.get("question_id") == question_id
            and row.get("context_tier") == tier
            and row.get("judge_model") == anchor_model
            and isinstance(row.get("value"), (int, float))
            and not isinstance(row.get("value"), bool)
        ):
            return float(row["value"])
    return None
