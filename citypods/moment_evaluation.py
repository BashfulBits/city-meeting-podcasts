"""Human calibration and admission policy for R6 pull quotes.

This is deliberately separate from the R5 topic-tag evaluator: a quote has a three-way human
label, a time-based warm-up, and a learned score threshold.  The state is append-only so a review
can be audited and recalculated when a prompt, model, or framing recipe changes.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
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
        "judge_model",
        "judge_prompt_version",
        "judge_schema_version",
    )
    return "|".join(str(candidate.get(key) or "") for key in dimensions)


def load_state(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text())
    except (FileNotFoundError, OSError, ValueError):
        return {"version": 1, "reviews": [], "policies": {}, "overrides": {}}
    if not isinstance(state, dict):
        return {"version": 1, "reviews": [], "policies": {}, "overrides": {}}
    state.setdefault("version", 1)
    state.setdefault("reviews", [])
    state.setdefault("policies", {})
    state.setdefault("overrides", {})
    return state


def save_state(path: Path, state: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(dict(state), indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


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
    for key, rows in grouped.items():
        scores = sorted({float(row.get("quality_score") or 0.0) for row in rows})
        first = min((_parse_time(row.get("reviewed_at")) for row in rows), default=current)
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
    state["policies"] = policies


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
                "display": admitted,
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
    result.update(
        {
            "admission": "admitted" if admitted else "shadow",
            "display": admitted,
            "admission_reason": "calibrated" if admitted else "below-threshold",
        }
    )
    return result


def _parse_time(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


__all__ = [
    "LABELS",
    "MIN_CALIBRATION_DAYS",
    "MIN_CELL_REVIEWS",
    "apply_admission",
    "cell_key",
    "load_state",
    "record_review",
    "refresh_policies",
    "save_state",
]
