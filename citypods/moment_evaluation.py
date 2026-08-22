"""Human calibration and admission policy for R6 pull quotes.

This is deliberately separate from the R5 topic-tag evaluator: a quote has a three-way human
label, a time-based warm-up, and a learned score threshold.  The state is append-only so a review
can be audited and recalculated when a prompt, model, or framing recipe changes.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

LABELS = frozenset({"Good", "Borderline", "Reject"})
MIN_CALIBRATION_DAYS = 30
MIN_CELL_REVIEWS = 30
MIN_CELL_POSITIVES = 3
MIN_CELL_NEGATIVES = 3
REQUIRED_PRECISION = 0.90


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


def cell_key(candidate: Mapping[str, Any]) -> str:
    dimensions = (
        "meeting_family",
        "provider_model",
        "prompt_version",
        "duration_bucket",
        "framing_profile",
    )
    return "|".join(str(candidate.get(key) or "") for key in dimensions)


def judge_cell_key(candidate: Mapping[str, Any], assessment: Mapping[str, Any]) -> str:
    """Return a calibration cell for one independent judge route."""
    return "|".join(
        (
            cell_key(candidate),
            str(assessment.get("provider_model") or ""),
            str(assessment.get("prompt_version") or ""),
            str(assessment.get("schema_version") or ""),
        )
    )


def load_state(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text())
    except FileNotFoundError:
        return {
            "version": 1,
            "reviews": [],
            "policies": {},
            "judge_policies": {},
            "judge_observations": [],
            "overrides": {},
        }
    except (OSError, ValueError) as exc:
        raise ValueError(f"could not read R6 evaluation state {path}") from exc
    if not isinstance(state, dict):
        raise ValueError(f"R6 evaluation state {path} must be a JSON object")
    state.setdefault("version", 1)
    state.setdefault("reviews", [])
    state.setdefault("policies", {})
    state.setdefault("judge_policies", {})
    state.setdefault("judge_observations", [])
    state.setdefault("overrides", {})
    return state


def save_state(path: Path, state: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(dict(state), stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        Path(temporary_name).replace(path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


@contextmanager
def state_lock(path: Path):
    """Serialize a local R6 state transaction across review and pipeline processes."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def append_judge_observation(
    path: Path, candidate: Mapping[str, Any], assessment: Mapping[str, Any]
) -> None:
    """Append a late background score without rewriting an immutable human review row.

    A reviewer may label a candidate before all free judge queues drain.  Observations are therefore
    separate append-only facts which are joined to the human label while policies are recalculated.
    """
    candidate_id = str(candidate.get("candidate_id") or "")
    model = str(assessment.get("provider_model") or "")
    prompt = str(assessment.get("prompt_version") or "")
    schema = str(assessment.get("schema_version") or "")
    if not candidate_id or not model or not prompt or not schema:
        raise ValueError("R6 judge observation needs candidate and judge identity")
    with state_lock(path):
        state = load_state(path)
        rows = state.setdefault("judge_observations", [])
        if any(
            isinstance(row, Mapping)
            and row.get("candidate_id") == candidate_id
            and row.get("provider_model") == model
            and row.get("prompt_version") == prompt
            and row.get("schema_version") == schema
            for row in rows
        ):
            return
        rows.append(
            {
                "candidate_id": candidate_id,
                "candidate_identity": {
                    key: candidate.get(key)
                    for key in (
                        "meeting_family",
                        "provider_model",
                        "prompt_version",
                        "duration_bucket",
                        "framing_profile",
                    )
                },
                **dict(assessment),
                "observed_at": _iso(),
            }
        )
        save_state(path, state)


def record_review(
    state: dict[str, Any],
    candidate: Mapping[str, Any],
    label: str,
    *,
    reviewer: str,
    review_id: str,
    reviewed_at: datetime | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if label not in LABELS:
        raise ValueError(f"R6 review label must be one of {sorted(LABELS)}")
    if not str(review_id).strip():
        raise ValueError("R6 review_id must be non-empty")
    if not str(candidate.get("candidate_id") or "").strip():
        raise ValueError("R6 review candidate_id must be non-empty")
    for existing in state.setdefault("reviews", []):
        if isinstance(existing, dict) and existing.get("review_id") == review_id:
            expected = {
                "candidate_id": str(candidate.get("candidate_id") or ""),
                "label": label,
                "reviewer": reviewer,
                "cell": cell_key(candidate),
                "quality_score": float(candidate.get("quality_score") or 0.0),
                "overrides": dict(overrides or {}),
            }
            if any(existing.get(key) != value for key, value in expected.items()):
                raise ValueError(f"conflicting replay for R6 review_id {review_id!r}")
            return existing
    row = {
        "review_id": review_id,
        "candidate_id": str(candidate.get("candidate_id") or ""),
        "label": label,
        "reviewer": reviewer,
        "reviewed_at": _iso(reviewed_at),
        "cell": cell_key(candidate),
        "candidate_identity": {
            key: candidate.get(key)
            for key in (
                "meeting_family",
                "provider_model",
                "prompt_version",
                "duration_bucket",
                "framing_profile",
                "judge_model",
                "judge_prompt_version",
                "judge_schema_version",
            )
        },
        "quality_score": float(candidate.get("quality_score") or 0.0),
        "overrides": dict(overrides or {}),
        "judge_assessments": [
            dict(assessment)
            for assessment in candidate.get("judge_assessments") or []
            if isinstance(assessment, Mapping)
        ],
    }
    state.setdefault("reviews", []).append(row)
    state.setdefault("overrides", {})[row["candidate_id"]] = {
        "manual_status": label,
        **dict(overrides or {}),
    }
    refresh_policies(state)
    return row


def refresh_policies(
    state: dict[str, Any], *, now: datetime | None = None, required_precision: float = 0.90
) -> None:
    """Recompute each calibration cell; no cell is auto-enabled during warm-up."""
    current = now or _now()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for review in state.get("reviews", []):
        if isinstance(review, dict) and review.get("cell"):
            grouped.setdefault(str(review["cell"]), []).append(review)
    policies: dict[str, dict[str, Any]] = {}
    judge_rows: dict[str, list[dict[str, Any]]] = {}
    observations_by_candidate: dict[str, list[Mapping[str, Any]]] = {}
    for observation in state.get("judge_observations", []):
        if isinstance(observation, Mapping) and observation.get("candidate_id"):
            observations_by_candidate.setdefault(str(observation["candidate_id"]), []).append(
                observation
            )
    for key, rows in grouped.items():
        scores = sorted({float(row.get("quality_score") or 0.0) for row in rows})
        first = min(
            (_parse_time(row.get("reviewed_at"), default=current) for row in rows), default=current
        )
        qualified: dict[str, Any] | None = None
        for threshold in scores:
            admitted = [row for row in rows if float(row.get("quality_score") or 0.0) >= threshold]
            good = sum(row.get("label") == "Good" for row in admitted)
            positive = sum(row.get("label") == "Good" for row in rows)
            negative = sum(row.get("label") in {"Borderline", "Reject"} for row in rows)
            precision = good / len(admitted) if admitted else 0.0
            if (
                len(rows) >= MIN_CELL_REVIEWS
                and positive >= MIN_CELL_POSITIVES
                and negative >= MIN_CELL_NEGATIVES
                and (current - first) >= timedelta(days=MIN_CALIBRATION_DAYS)
                and precision >= required_precision
            ):
                qualified = {
                    "mode": "auto",
                    "threshold": threshold,
                    "precision": precision,
                    "review_count": len(rows),
                    "qualified_at": _iso(current),
                }
                break
        policies[key] = qualified or {
            "mode": "manual",
            "threshold": None,
            "review_count": len(rows),
            "qualified_at": None,
        }
        for row in rows:
            identity = row.get("candidate_identity") if isinstance(row, dict) else {}
            candidate = dict(identity) if isinstance(identity, Mapping) else {}
            candidate["quality_score"] = row.get("quality_score")
            assessments = [
                assessment
                for assessment in row.get("judge_assessments") or []
                if isinstance(assessment, Mapping)
            ]
            seen_assessments = {
                (
                    assessment.get("provider_model"),
                    assessment.get("prompt_version"),
                    assessment.get("schema_version"),
                )
                for assessment in assessments
            }
            assessments.extend(
                assessment
                for assessment in observations_by_candidate.get(
                    str(row.get("candidate_id") or ""), []
                )
                if (
                    assessment.get("provider_model"),
                    assessment.get("prompt_version"),
                    assessment.get("schema_version"),
                )
                not in seen_assessments
            )
            for assessment in assessments:
                if not isinstance(assessment, Mapping):
                    continue
                score = assessment.get("admission_score")
                if not isinstance(score, int | float):
                    continue
                judge_rows.setdefault(judge_cell_key(candidate, assessment), []).append(
                    {
                        "label": row.get("label"),
                        "reviewed_at": row.get("reviewed_at"),
                        "score": float(score),
                    }
                )
    state["policies"] = policies
    state["judge_policies"] = _qualified_policies(judge_rows, current, required_precision)


def _qualified_policies(
    grouped: Mapping[str, list[dict[str, Any]]], current: datetime, required_precision: float
) -> dict[str, dict[str, Any]]:
    policies: dict[str, dict[str, Any]] = {}
    for key, rows in grouped.items():
        scores = sorted({float(row["score"]) for row in rows})
        first = min(
            (_parse_time(row.get("reviewed_at"), default=current) for row in rows), default=current
        )
        qualified: dict[str, Any] | None = None
        for threshold in scores:
            admitted = [row for row in rows if float(row["score"]) >= threshold]
            good = sum(row.get("label") == "Good" for row in admitted)
            positive = sum(row.get("label") == "Good" for row in rows)
            negative = sum(row.get("label") in {"Borderline", "Reject"} for row in rows)
            precision = good / len(admitted) if admitted else 0.0
            if (
                len(rows) >= MIN_CELL_REVIEWS
                and positive >= MIN_CELL_POSITIVES
                and negative >= MIN_CELL_NEGATIVES
                and (current - first) >= timedelta(days=MIN_CALIBRATION_DAYS)
                and precision >= required_precision
            ):
                qualified = {
                    "mode": "auto",
                    "threshold": threshold,
                    "precision": precision,
                    "review_count": len(rows),
                    "qualified_at": _iso(current),
                }
                break
        policies[key] = qualified or {
            "mode": "manual",
            "threshold": None,
            "review_count": len(rows),
            "qualified_at": None,
        }
    return policies


def apply_admission(
    candidate: Mapping[str, Any],
    state: Mapping[str, Any],
    *,
    technical_gate: bool,
    global_mode: str = "manual",
) -> dict[str, Any]:
    """Apply manual overrides first, then the qualified score policy.

    ``technical_gate`` is supplied by the media layer.  This function never makes a video-safe
    decision from text alone and never changes the candidate's quote or timing.
    """
    result = dict(candidate)
    overrides = state.get("overrides") if isinstance(state, Mapping) else {}
    override = (overrides or {}).get(str(candidate.get("candidate_id") or ""), {})
    if isinstance(override, str):
        override = {"manual_status": override}
    manual = str(candidate.get("manual_status") or override.get("manual_status") or "").title()
    for key in ("start", "end", "title", "caption", "crop_anchor", "output_profile"):
        if key in override:
            result[key] = override[key]
    if manual == "Reject":
        result.update({"admission": "rejected", "display": False, "admission_reason": "manual"})
        return result
    if manual == "Borderline":
        result.update({"admission": "shadow", "display": False, "admission_reason": "manual"})
        return result
    if manual == "Good":
        admitted = technical_gate
        result.update(
            {
                "admission": "admitted" if admitted else "admitted_text_only",
                # A manual Good remains a public, text-backed quote when its video gate fails.
                # The page can show the transcript evidence while the renderer safely declines it.
                "display": True,
                "admission_reason": "manual-good",
            }
        )
        return result
    if global_mode != "auto" or not technical_gate:
        result.update({"admission": "shadow", "display": False, "admission_reason": "manual-only"})
        return result
    policy = (state.get("policies") or {}).get(cell_key(candidate)) or {}
    threshold = policy.get("threshold") if policy.get("mode") == "auto" else None
    admitted = threshold is not None and float(candidate.get("quality_score") or 0.0) >= float(
        threshold
    )
    reason = "calibrated" if admitted else "below-threshold"
    if not admitted:
        for assessment in candidate.get("judge_assessments") or []:
            if not isinstance(assessment, Mapping):
                continue
            judge_policy = (state.get("judge_policies") or {}).get(
                judge_cell_key(candidate, assessment)
            ) or {}
            judge_threshold = (
                judge_policy.get("threshold") if judge_policy.get("mode") == "auto" else None
            )
            score = assessment.get("admission_score")
            if (
                isinstance(score, int | float)
                and judge_threshold is not None
                and score >= judge_threshold
            ):
                admitted = True
                reason = f"judge-calibrated:{assessment.get('provider_model') or 'unknown'}"
                break
    result.update(
        {
            "admission": "admitted" if admitted else "shadow",
            "display": admitted,
            "admission_reason": reason,
        }
    )
    return result


def _parse_time(value: Any, *, default: datetime) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return default
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


__all__ = [
    "LABELS",
    "MIN_CALIBRATION_DAYS",
    "MIN_CELL_REVIEWS",
    "apply_admission",
    "append_judge_observation",
    "cell_key",
    "judge_cell_key",
    "load_state",
    "record_review",
    "refresh_policies",
    "save_state",
    "state_lock",
]
