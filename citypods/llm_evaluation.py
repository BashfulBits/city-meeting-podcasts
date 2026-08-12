"""Reusable confidence calibration and human-review support for LLM features.

The evaluator deliberately separates three concerns:

* a feature records validated candidates (for example, R5 topic-tag suggestions);
* this module records human decisions and derives a sparse calibration matrix;
* an admission policy projects candidates into the feature's visible output.

An exact matrix row is keyed by feature, provider/model route, input/prompt version, taxonomy
version, tag (or another feature-specific label), and scope.  Unpopulated rows use the configured
feature/route fallback.  The initial R5 fallback is 1.0, so shadow candidates can be collected
without becoming visible until a maintainer deliberately lowers an unquantified fallback or the
row earns a human-calibrated threshold.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from citypods.security import SecurityError, validate_source_url

STATE_VERSION = 1
DEFAULT_STATE_NAME = "llm_evaluation.json"
REVIEW_MARKER = "<!-- citypods:llm-review "
REVIEW_DECISIONS = ("correct", "incorrect", "ambiguous")
PRELABELER_DECISIONS = ("likely_correct", "needs_human_review", "likely_incorrect")
# Shared between render_review_body() and parse_review() -- see parse_review()'s docstring-level
# comment for why this exact line is the security-relevant scan boundary.
_CHECKBOX_HEADER = "Choose exactly one:"


@dataclass(frozen=True)
class EvaluationConfig:
    """Feature-independent policy knobs; feature-specific fallbacks live in ``fallbacks``."""

    fallback_confidence: float = 1.0
    required_precision: float = 0.90
    minimum_reviews: int = 12
    prelabeler_required_precision: float = 0.95
    prelabeler_minimum_reviews: int = 50
    # Require both actionable evaluator decisions to have a small independent support floor.
    # Without this, one likely-incorrect sample could qualify automated suppression merely because
    # the other 49 reviews were likely-correct/uncertain.
    prelabeler_minimum_decision_reviews: int = 5
    review_batch_size: int = 80
    # Hard cap for one active tag/source/assessment/scope stratum in a weekly packet.
    max_reviews_per_subject_stratum: int = 8
    state_path: str = DEFAULT_STATE_NAME
    fallbacks: dict[str, dict[str, float]] | None = None

    def fallback_for(self, feature: str, provider_model: str) -> float:
        routes = (self.fallbacks or {}).get(feature, {})
        if provider_model in routes:
            return float(routes[provider_model])
        return float(self.fallback_confidence)


def config_from_mapping(raw: dict[str, Any] | None) -> EvaluationConfig:
    raw = raw if isinstance(raw, dict) else {}
    fallbacks: dict[str, dict[str, float]] = {}
    for feature, routes in (raw.get("fallbacks") or {}).items():
        if not isinstance(feature, str) or not isinstance(routes, dict):
            continue
        fallbacks[feature] = {
            str(route): min(1.0, max(0.0, float(value))) for route, value in routes.items()
        }
    return EvaluationConfig(
        fallback_confidence=min(1.0, max(0.0, float(raw.get("fallback_confidence", 1.0)))),
        required_precision=min(1.0, max(0.0, float(raw.get("required_precision", 0.90)))),
        minimum_reviews=max(1, int(raw.get("minimum_reviews", 12))),
        prelabeler_required_precision=min(
            1.0, max(0.0, float(raw.get("prelabeler_required_precision", 0.95)))
        ),
        prelabeler_minimum_reviews=max(1, int(raw.get("prelabeler_minimum_reviews", 50))),
        prelabeler_minimum_decision_reviews=max(
            1, int(raw.get("prelabeler_minimum_decision_reviews", 5))
        ),
        review_batch_size=min(100, max(1, int(raw.get("review_batch_size", 80)))),
        max_reviews_per_subject_stratum=max(1, int(raw.get("max_reviews_per_subject_stratum", 8))),
        state_path=str(raw.get("state_path", DEFAULT_STATE_NAME) or DEFAULT_STATE_NAME),
        fallbacks=fallbacks,
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _json_hash(value: Any) -> str:
    return hashlib.sha1(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()[:16]


def load_state(path: str | Path) -> dict[str, Any]:
    """Load the durable calibration state, defaulting to empty only when the file is genuinely
    absent. A file that exists but can't be read/parsed, or isn't a JSON object, fails closed
    (raises) instead of silently returning an empty snapshot -- the caller that persists state
    back (``save_state``) would otherwise clobber real review history with an empty file merely
    because a read hit a transient error or corruption once."""
    path = Path(path)
    if not path.exists():
        return {"version": STATE_VERSION, "reviews": {}, "matrix": [], "trend": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"invalid LLM evaluation state file: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"invalid LLM evaluation state file: {path}")
    value.setdefault("version", STATE_VERSION)
    value.setdefault("reviews", {})
    value.setdefault("matrix", [])
    value.setdefault("trend", [])
    return value


def save_state(path: str | Path, state: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def candidate_matrix_key(candidate: dict[str, Any]) -> dict[str, Any]:
    """Return the exact calibration dimensions for a generic candidate."""
    assessment = str(candidate.get("assessment_kind") or "")
    prompt_version = candidate.get("prompt_version")
    if assessment == "prelabeler-overlay":
        # The evaluator prompt is a distinct calibration dimension. Older audit rows used the
        # candidate/tagger prompt field, so keep that as the compatibility fallback.
        prompt_version = candidate.get("prelabeler_prompt_version") or prompt_version
    result = {
        "feature": str(candidate.get("feature") or ""),
        "provider_model": str(candidate.get("provider_model") or ""),
        "prompt_version": str(prompt_version or ""),
        "taxonomy_version": candidate.get("taxonomy_version"),
        "label": str(candidate.get("id") or candidate.get("label") or ""),
        "scope": str(
            candidate.get("scope") or ("chapter" if candidate.get("chapter_id") else "episode")
        ),
    }
    # Existing R5 rows predate the unified ledger. Keep their exact five-dimensional key when the
    # source/assessment fields are absent so old review state remains qualified. New candidates
    # carry those fields explicitly, keeping rule, LLM-tagger, and evaluator evidence independent.
    if "source_kind" in candidate or assessment == "prelabeler-overlay":
        result["source_kind"] = str(candidate.get("source_kind") or "llm")
    if "assessment_kind" in candidate or assessment not in {"", "tagger-admission"}:
        result["assessment_kind"] = assessment or "tagger-admission"
        if candidate.get("evaluator_model"):
            result["evaluator_model"] = str(candidate["evaluator_model"])
    if candidate.get("chapter_pipeline_version") is not None:
        result["chapter_pipeline_version"] = str(candidate["chapter_pipeline_version"])
    return result


def matrix_key(candidate: dict[str, Any]) -> str:
    return _json_hash(candidate_matrix_key(candidate))


def _legacy_matrix_key(candidate: dict[str, Any]) -> dict[str, Any] | None:
    """Return the pre-overlay five-dimensional key for an old LLM tagger row."""
    if candidate.get("source_kind", "llm") != "llm":
        return None
    if str(candidate.get("assessment_kind") or "tagger-admission") != "tagger-admission":
        return None
    key = candidate_matrix_key(candidate)
    return {
        field: key[field]
        for field in (
            "feature",
            "provider_model",
            "prompt_version",
            "taxonomy_version",
            "label",
            "scope",
        )
    }


def _matrix_row(candidate: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
    rows = _matrix_entries(state)
    current = matrix_key(candidate)
    row = next((item for item in rows if item.get("matrix_id") == current), None)
    if row is not None:
        return row
    legacy = _legacy_matrix_key(candidate)
    if legacy is None:
        return None
    legacy_id = _json_hash(legacy)
    return next(
        (item for item in rows if item.get("matrix_id") == legacy_id or item.get("key") == legacy),
        None,
    )


def candidate_id(candidate: dict[str, Any]) -> str:
    """Stable review identity; source text and model recipe are part of the identity."""
    identity = {
        **candidate_matrix_key(candidate),
        "episode_uid": candidate.get("episode_uid"),
        "chapter_id": candidate.get("chapter_id"),
        "recipe_hash": candidate.get("recipe_hash"),
        "confidence": candidate.get("confidence"),
        "source_kind": candidate.get("source_kind", "llm"),
        "assessment_kind": candidate.get("assessment_kind", "tagger-admission"),
        "prelabeler_decision": candidate.get("prelabeler_decision"),
        "prelabeler_reason": candidate.get("prelabeler_reason"),
        "prelabeler_input_digest": candidate.get("prelabeler_input_digest"),
    }
    return f"llm-{_json_hash(identity)}"


def prelabeler_review_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    """Return a review subject for auditing an evaluator result, not the original candidate."""
    value = dict(candidate)
    value["assessment_kind"] = "prelabeler-overlay"
    value["evaluator_model"] = value.get("prelabeler_model") or value.get("evaluator_model")
    value["prompt_version"] = value.get("prelabeler_prompt_version") or value.get(
        "prompt_version", ""
    )
    value["tagger_confidence"] = value.get("confidence")
    value["confidence"] = float(value.get("prelabeler_confidence", 0.0) or 0.0)
    value["subject_candidate_id"] = str(candidate.get("candidate_id") or candidate_id(candidate))
    # Keep one audit identity per underlying subject/evaluator recipe. A retry that changes only
    # the evaluator's wording or confidence must not inflate the 50-example denominator.
    audit_identity = {
        **candidate_matrix_key(value),
        "subject_candidate_id": value["subject_candidate_id"],
        "provider_model": value.get("provider_model"),
        "evaluator_model": value.get("evaluator_model"),
        "prompt_version": value.get("prompt_version"),
        "recipe_hash": value.get("recipe_hash"),
    }
    value["candidate_id"] = f"prelabel-{_json_hash(audit_identity)}"
    return value


def _matrix_entries(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [entry for entry in state.get("matrix", []) if isinstance(entry, dict)]


def resolve_threshold(
    candidate: dict[str, Any], *, config: EvaluationConfig, state: dict[str, Any]
) -> tuple[float, str]:
    """Resolve an exact qualified row, then the feature/route fallback."""
    key = candidate_matrix_key(candidate)
    keys = [key]
    legacy = _legacy_matrix_key(candidate)
    if legacy is not None:
        keys.append(legacy)
    for entry in _matrix_entries(state):
        if entry.get("key") in keys and entry.get("qualified"):
            threshold = entry.get("threshold")
            if isinstance(threshold, int | float):
                return float(threshold), "calibrated"
    return config.fallback_for(key["feature"], key["provider_model"]), "fallback"


def resolve_prelabeler_threshold(
    candidate: dict[str, Any], *, config: EvaluationConfig, state: dict[str, Any]
) -> tuple[float | None, str]:
    """Resolve a qualified pre-labeler confidence threshold for this exact decision class."""
    probe = {
        **candidate,
        "assessment_kind": "prelabeler-overlay",
        "evaluator_model": candidate.get("prelabeler_model") or candidate.get("evaluator_model"),
    }
    key = candidate_matrix_key(probe)
    for entry in _matrix_entries(state):
        if entry.get("key") == key and entry.get("qualified"):
            threshold = entry.get("threshold")
            if isinstance(threshold, int | float):
                return float(threshold), "calibrated"
    return None, "unqualified"


def apply_admission(
    candidate: dict[str, Any], *, config: EvaluationConfig, state: dict[str, Any]
) -> dict[str, Any]:
    value = dict(candidate)
    threshold, basis = resolve_threshold(candidate, config=config, state=state)
    confidence = float(candidate.get("confidence", 0.0))
    value["candidate_id"] = str(candidate.get("candidate_id") or candidate_id(candidate))
    value["admission_threshold"] = threshold
    value["admission_basis"] = basis
    # A "calibrated" threshold is itself an observed confidence value from real human review, so
    # meeting it exactly must admit. An unreviewed "fallback" threshold carries no such evidence;
    # requiring the candidate to strictly exceed it keeps a fallback of 1.0 truly unreachable
    # (confidence is clamped to <= 1.0), rather than admitting on a model reporting exactly 1.0
    # with zero real calibration behind it.
    admitted = confidence > threshold if basis == "fallback" else confidence >= threshold
    value["admission"] = "admitted" if admitted else "shadow"
    value.setdefault("source_kind", "llm")
    if value.get("candidate_state") == "historical":
        # Historical rows remain in the ledger for audit, but are never an active public or review
        # subject even when a caller passes the whole ledger to this generic projection helper.
        value["display"] = False
        value["tagger_display"] = False
        value["prelabeler_display"] = False
        value["prelabeler_basis"] = "historical"
        value["prelabeler_qualified"] = False
        return value
    value.setdefault(
        "tagger_admission",
        "not_applicable" if value["source_kind"] == "rule" else value["admission"],
    )
    if value.get("source_kind") == "rule":
        value["tagger_admission"] = "not_applicable"
        value["admission"] = "admitted"
    # Display is a projection, never a sticky persisted decision. Recompute the tagger baseline
    # from current admission so a candidate persisted as shadow becomes visible when its row later
    # qualifies, and so policy changes re-project deterministically.
    tagger_display = value["admission"] == "admitted"
    value["tagger_display"] = tagger_display
    value["display"] = tagger_display
    decision = value.get("prelabeler_decision")
    if decision in {"likely_correct", "needs_human_review", "likely_incorrect"}:
        threshold, basis = resolve_prelabeler_threshold(value, config=config, state=state)
        value["prelabeler_basis"] = basis
        value["prelabeler_qualified"] = basis == "calibrated"
        value["prelabeler_threshold"] = threshold
        # The overlay is deliberately decision-only. The calibrated threshold is diagnostic
        # evidence, not a hidden second gate that changes suppression when confidence scales drift.
        value["prelabeler_confidence_gate"] = "decision-only"
        # Before qualification the overlay is audit-only. Once qualified, only a confirmed
        # likely-incorrect decision suppresses an otherwise-visible candidate.
        overlay_display = tagger_display
        if basis == "calibrated" and decision == "likely_incorrect":
            overlay_display = False
        value["prelabeler_display"] = overlay_display
        value["display"] = overlay_display
        subject_id = str(value.get("candidate_id") or candidate_id(value))
        override = next(
            (
                review
                for review in reversed(list((state.get("reviews") or {}).values()))
                if isinstance(review, dict)
                and review.get("assessment_kind") == "prelabeler-overlay"
                and review.get("subject_candidate_id") == subject_id
                and review.get("decision") in REVIEW_DECISIONS
            ),
            None,
        )
        if basis == "calibrated" and override and override.get("decision") != "correct":
            # A human rejection of a likely-correct evaluator result means suppress the candidate;
            # a rejection of likely-incorrect means restore the tagger baseline. Ambiguous keeps
            # the conservative public baseline rather than trusting the evaluator suppression.
            if decision == "likely_correct":
                value["display"] = False
            elif decision == "likely_incorrect":
                value["display"] = tagger_display
            else:
                value["display"] = tagger_display
            value["prelabeler_basis"] = "human_override"
        elif basis == "calibrated" and override and override.get("decision") == "correct":
            value["prelabeler_basis"] = "human_override"
    else:
        value.setdefault("prelabeler_basis", "unqualified")
        value.setdefault("prelabeler_qualified", False)
    return value


def visible_candidates(
    candidates: list[dict[str, Any]], *, config: EvaluationConfig, state: dict[str, Any]
) -> list[dict[str, Any]]:
    return [
        applied
        for candidate in candidates
        if candidate.get("candidate_state") != "historical"
        if (applied := apply_admission(candidate, config=config, state=state))["admission"]
        == "admitted"
        and applied.get("display", True)
    ]


def _reviewed_candidates(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in state.get("reviews", {}).values()
        if isinstance(item, dict) and item.get("decision") in REVIEW_DECISIONS
    ]


def refresh_matrix(state: dict[str, Any], *, config: EvaluationConfig) -> dict[str, Any]:
    """Recompute sparse exact rows from immutable-by-id human review decisions."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for review in _reviewed_candidates(state):
        grouped.setdefault(str(review.get("matrix_id") or ""), []).append(review)
    rows: list[dict[str, Any]] = []
    for key_id, reviews in sorted(grouped.items()):
        if not key_id:
            continue
        key = reviews[0].get("matrix_key") or {}
        thresholds = sorted(
            {
                float(item["confidence"])
                for item in reviews
                if isinstance(item.get("confidence"), (int, float))
            }
        )
        selected_threshold: float | None = None
        selected_precision: float | None = None
        selected_count = 0
        assessment_kind = str(key.get("assessment_kind") or "tagger-admission")
        if assessment_kind == "prelabeler-overlay":
            required_reviews = config.prelabeler_minimum_reviews
            required_precision = config.prelabeler_required_precision
            decision_metrics: dict[str, dict[str, Any]] = {}
            for predicted in PRELABELER_DECISIONS:
                predicted_reviews = [
                    item for item in reviews if item.get("prelabeler_decision") == predicted
                ]
                predicted_correct = sum(
                    item.get("decision") == "correct" for item in predicted_reviews
                )
                decision_metrics[predicted] = {
                    "reviewed": len(predicted_reviews),
                    "correct": predicted_correct,
                    "precision": (
                        predicted_correct / len(predicted_reviews) if predicted_reviews else None
                    ),
                }
        else:
            required_reviews = config.minimum_reviews
            required_precision = config.required_precision
        if assessment_kind == "prelabeler-overlay":
            # The pre-labeler's discrete decision is the policy signal; confidence is retained
            # for diagnostics but must not let a high-confidence wrong suppression qualify. A
            # single row therefore tracks precision separately for each actionable decision.
            eligible_metrics = [
                decision_metrics[predicted]
                for predicted in ("likely_correct", "likely_incorrect")
                if decision_metrics[predicted]["reviewed"]
            ]
            if (
                len(reviews) >= required_reviews
                and len(eligible_metrics) == 2
                and all(
                    metric["reviewed"] >= config.prelabeler_minimum_decision_reviews
                    and metric["precision"] is not None
                    and metric["precision"] >= required_precision
                    for metric in eligible_metrics
                )
            ):
                selected_threshold = min(thresholds) if thresholds else None
                selected_precision = min(float(metric["precision"]) for metric in eligible_metrics)
                selected_count = len(reviews)
        else:
            for threshold in thresholds:
                selected = [
                    item
                    for item in reviews
                    if isinstance(item.get("confidence"), (int, float))
                    and float(item["confidence"]) >= threshold
                ]
                correct = sum(item.get("decision") == "correct" for item in selected)
                precision = correct / len(selected) if selected else 0.0
                if len(selected) >= required_reviews and precision >= required_precision:
                    selected_threshold = threshold
                    selected_precision = precision
                    selected_count = len(selected)
                    break
        total = len(reviews)
        rows.append(
            {
                "key": key,
                "matrix_id": key_id,
                "reviewed": total,
                "correct": sum(item.get("decision") == "correct" for item in reviews),
                "incorrect": sum(item.get("decision") == "incorrect" for item in reviews),
                "ambiguous": sum(item.get("decision") == "ambiguous" for item in reviews),
                "qualified": selected_threshold is not None,
                "threshold": selected_threshold,
                "precision": selected_precision,
                "qualified_count": selected_count,
                "updated_at": _utc_now(),
            }
        )
        if assessment_kind == "prelabeler-overlay":
            rows[-1]["decision_metrics"] = decision_metrics
    state["matrix"] = rows
    state["matrix_updated_at"] = _utc_now()
    trend_point = {
        "at": state["matrix_updated_at"],
        "rows": len(rows),
        "qualified_rows": sum(bool(row.get("qualified")) for row in rows),
        "reviewed": sum(int(row.get("reviewed", 0) or 0) for row in rows),
    }
    trend = state.setdefault("trend", [])
    if not isinstance(trend, list):
        trend = []
        state["trend"] = trend
    last = trend[-1] if trend and isinstance(trend[-1], dict) else {}
    if {key: last.get(key) for key in trend_point if key != "at"} != {
        key: trend_point[key] for key in trend_point if key != "at"
    }:
        trend.append(trend_point)
        del trend[:-52]
    return state


def record_review(
    state: dict[str, Any],
    candidate: dict[str, Any],
    *,
    decision: str,
    actor: str = "",
    issue_number: int | None = None,
    issue_url: str = "",
) -> dict[str, Any]:
    if decision not in REVIEW_DECISIONS:
        raise ValueError(f"unknown LLM review decision: {decision!r}")
    value = dict(candidate)
    value["candidate_id"] = str(candidate.get("candidate_id") or candidate_id(candidate))
    review = {
        "candidate_id": value["candidate_id"],
        "matrix_id": matrix_key(value),
        "matrix_key": candidate_matrix_key(value),
        "feature": value.get("feature"),
        "provider_model": value.get("provider_model"),
        "prompt_version": value.get("prompt_version"),
        "taxonomy_version": value.get("taxonomy_version"),
        "label": value.get("id") or value.get("label"),
        "scope": value.get("scope"),
        "confidence": float(value.get("confidence", 0.0)),
        "decision": decision,
        "episode_uid": value.get("episode_uid"),
        "chapter_id": value.get("chapter_id"),
        "source_kind": value.get("source_kind", "llm"),
        "assessment_kind": value.get("assessment_kind", "tagger-admission"),
        "prelabeler_decision": value.get("prelabeler_decision"),
        "evaluator_model": value.get("evaluator_model") or value.get("prelabeler_model"),
        "subject_candidate_id": value.get("subject_candidate_id"),
        "reviewed_at": _utc_now(),
        "reviewed_by": actor,
        "issue_number": issue_number,
        "issue_url": issue_url,
    }
    state.setdefault("reviews", {})[value["candidate_id"]] = review
    return review


def review_priority(
    candidate: dict[str, Any], *, state: dict[str, Any], config: EvaluationConfig
) -> tuple[Any, ...]:
    row = _matrix_row(candidate, state)
    reviewed = int((row or {}).get("reviewed", 0) or 0)
    qualified = bool((row or {}).get("qualified"))
    if candidate.get("assessment_kind") == "prelabeler-overlay":
        threshold, _ = resolve_prelabeler_threshold(candidate, config=config, state=state)
        threshold = float(threshold if threshold is not None else 1.0)
    else:
        threshold, _ = resolve_threshold(candidate, config=config, state=state)
    return (
        0 if not qualified else 1,
        0 if reviewed == 0 else 1,
        reviewed,
        abs(float(candidate.get("confidence", 0.0)) - threshold),
        str(candidate.get("candidate_id") or candidate_id(candidate)),
    )


def qualification_distance(
    candidate: dict[str, Any], *, state: dict[str, Any], config: EvaluationConfig
) -> dict[str, Any]:
    """Return report-friendly progress toward this candidate's applicable matrix gate."""
    key_id = matrix_key(candidate)
    row = _matrix_row(candidate, state)
    assessment = str(candidate.get("assessment_kind") or "tagger-admission")
    required = (
        config.prelabeler_minimum_reviews
        if assessment == "prelabeler-overlay"
        else config.minimum_reviews
    )
    precision_required = (
        config.prelabeler_required_precision
        if assessment == "prelabeler-overlay"
        else config.required_precision
    )
    if assessment == "prelabeler-overlay":
        threshold, threshold_basis = resolve_prelabeler_threshold(
            candidate, config=config, state=state
        )
    else:
        threshold, threshold_basis = resolve_threshold(candidate, config=config, state=state)
    reviewed = int((row or {}).get("reviewed", 0) or 0)
    precision = (row or {}).get("precision")
    result = {
        "reviewed": reviewed,
        "required_reviews": required,
        "reviews_remaining": max(0, required - reviewed),
        "precision": precision,
        "required_precision": precision_required,
        "precision_gap": max(0.0, precision_required - float(precision or 0.0)),
        "qualified": bool((row or {}).get("qualified")),
        "threshold": threshold,
        "threshold_basis": threshold_basis,
        "matrix_id": key_id,
    }
    if assessment == "prelabeler-overlay":
        metrics = (row or {}).get("decision_metrics") or {}
        result["likely_correct_precision"] = (metrics.get("likely_correct") or {}).get("precision")
        result["likely_incorrect_precision"] = (metrics.get("likely_incorrect") or {}).get(
            "precision"
        )
        result["decision_counts"] = {
            decision: int((metrics.get(decision) or {}).get("reviewed", 0) or 0)
            for decision in PRELABELER_DECISIONS
        }
    return result


def select_review_candidates(
    candidates: list[dict[str, Any]], *, state: dict[str, Any], config: EvaluationConfig
) -> list[dict[str, Any]]:
    reviewed = set((state.get("reviews") or {}).keys())

    def review_ids(value: dict[str, Any]) -> set[str]:
        ids = {str(value.get("candidate_id") or candidate_id(value))}
        if value.get("assessment_kind") == "prelabeler-overlay":
            # Compatibility with audit rows written before prelabeler identities were stabilized;
            # those IDs included the evaluator's reason/confidence and would otherwise be reviewed
            # a second time after this version loads the same subject.
            ids.add(candidate_id(value))
        legacy = _legacy_matrix_key(value)
        if legacy is not None:
            identity = {
                **legacy,
                "episode_uid": value.get("episode_uid"),
                "chapter_id": value.get("chapter_id"),
                "recipe_hash": value.get("recipe_hash"),
                "confidence": value.get("confidence"),
            }
            ids.add(f"llm-{_json_hash(identity)}")
        return ids

    pending = [candidate for candidate in candidates if not review_ids(candidate) & reviewed]
    if not pending:
        return []

    rows = {str(row.get("matrix_id")): row for row in _matrix_entries(state)}
    label_counts: dict[tuple[str, ...], int] = {}
    for candidate in pending:
        subject_key = (
            str(candidate.get("source_kind") or "llm"),
            str(candidate.get("assessment_kind") or "tagger-admission"),
            str(candidate.get("id") or candidate.get("label") or ""),
            str(candidate.get("scope") or "episode"),
        )
        label_counts[subject_key] = label_counts.get(subject_key, 0) + 1

    def bucket(candidate: dict[str, Any]) -> int:
        assessment = str(candidate.get("assessment_kind") or "tagger-admission")
        if assessment == "prelabeler-overlay":
            return 1
        row = _matrix_row(candidate, state) or rows.get(matrix_key(candidate)) or {}
        if candidate.get("source_kind") == "rule" or row.get("qualified"):
            return 3
        subject_key = (
            str(candidate.get("source_kind") or "llm"),
            str(candidate.get("assessment_kind") or "tagger-admission"),
            str(candidate.get("id") or candidate.get("label") or ""),
            str(candidate.get("scope") or "episode"),
        )
        if label_counts.get(subject_key, 0) <= 2:
            return 2
        return 0

    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for candidate in pending:
        key = (
            bucket(candidate),
            str(candidate.get("source_kind") or "llm"),
            str(candidate.get("assessment_kind") or "tagger-admission"),
            str(candidate.get("id") or candidate.get("label") or ""),
            str(candidate.get("scope") or "episode"),
            str(candidate.get("provider_model") or ""),
            str(candidate.get("evaluator_model") or candidate.get("prelabeler_model") or ""),
            str(
                candidate.get("prompt_version") or candidate.get("prelabeler_prompt_version") or ""
            ),
            str(candidate.get("taxonomy_version") or ""),
        )
        groups.setdefault(key, []).append(candidate)
    for values in groups.values():
        values.sort(key=lambda item: review_priority(item, state=state, config=config))

    targets = {
        0: round(config.review_batch_size * 0.50),
        1: round(config.review_batch_size * 0.25),
        2: round(config.review_batch_size * 0.15),
    }
    targets[3] = max(0, config.review_batch_size - sum(targets.values()))
    selected: list[dict[str, Any]] = []
    leftovers: list[dict[str, Any]] = []

    def subject_stratum(value: dict[str, Any]) -> tuple[str, ...]:
        return (
            str(value.get("source_kind") or "llm"),
            str(value.get("assessment_kind") or "tagger-admission"),
            str(value.get("id") or value.get("label") or ""),
            str(value.get("scope") or "episode"),
        )

    selected_subject_strata: dict[tuple[str, ...], int] = {}

    def take(value: dict[str, Any]) -> bool:
        key = subject_stratum(value)
        if selected_subject_strata.get(key, 0) >= config.max_reviews_per_subject_stratum:
            return False
        selected.append(value)
        selected_subject_strata[key] = selected_subject_strata.get(key, 0) + 1
        return True

    for wanted_bucket in range(4):
        bucket_groups = {
            key: list(values) for key, values in groups.items() if key[0] == wanted_bucket
        }
        picked = 0
        while bucket_groups and picked < targets[wanted_bucket]:
            for key in sorted(bucket_groups):
                values = bucket_groups[key]
                if values and picked < targets[wanted_bucket]:
                    value = values.pop(0)
                    if take(value):
                        picked += 1
                    else:
                        # Do not let the leftovers path bypass the hard per-stratum cap.
                        leftovers.append(value)
            bucket_groups = {key: values for key, values in bucket_groups.items() if values}
        leftovers.extend(item for values in bucket_groups.values() for item in values)
    # If a sparse catalog cannot fill a bucket's quota, use remaining strata. The same hard
    # per-tag/source/assessment/scope cap applies during this fill pass.
    leftovers.sort(key=lambda item: review_priority(item, state=state, config=config))
    for value in leftovers:
        if len(selected) >= config.review_batch_size:
            break
        take(value)
    return selected[: config.review_batch_size]


def _safe_link(url: str) -> str | None:
    """Only render a document link the project's shared SSRF gate would allow fetching.

    ``resolve=False`` is the same offline/fast mode the gate uses at config-load time: this is
    a render-time decision about a Markdown link label, not a network fetch, so DNS resolution
    isn't needed here (and would make link rendering depend on network access at all).
    """
    try:
        validate_source_url(url, resolve=False)
    except SecurityError:
        return None
    return url


def _quote_block(text: str) -> list[str]:
    return [f"> {line}" if line else ">" for line in str(text).splitlines()]


def render_review_body(
    candidate: dict[str, Any], *, config: EvaluationConfig, state: dict[str, Any]
) -> str:
    applied = apply_admission(candidate, config=config, state=state)
    meta = {
        "schema_version": 1,
        "candidate": {
            key: applied.get(key)
            for key in (
                "candidate_id",
                "feature",
                "provider_model",
                "prompt_version",
                "taxonomy_version",
                "id",
                "scope",
                "confidence",
                "episode_uid",
                "chapter_id",
                "recipe_hash",
                "source_kind",
                "assessment_kind",
                "rule_pattern",
                "rule_patterns",
                "rule_version",
                "tagger_admission",
                "admission",
                "display",
                "prelabeler_model",
                "prelabeler_prompt_version",
                "prelabeler_decision",
                "prelabeler_confidence",
                "prelabeler_reason",
                "prelabeler_evidence_supported",
                "subject_candidate_id",
            )
        },
    }
    assessment = str(applied.get("assessment_kind") or "tagger-admission")
    source_kind = str(applied.get("source_kind") or "llm")
    review_title = (
        "pre-labeler audit"
        if assessment == "prelabeler-overlay"
        else f"{source_kind} tag candidate"
    )
    lines = [
        f"# R5 {review_title}: `{applied.get('id') or applied.get('label')}`",
        "",
        f"- Feature: `{applied.get('feature')}`",
        f"- Candidate source: `{source_kind}`",
        f"- Assessment: `{assessment}`",
        f"- Model route: `{applied.get('provider_model')}`",
        f"- Confidence: `{float(applied.get('confidence', 0.0)):.3f}`",
        f"- Current policy: `{applied['admission_basis']}` at "
        f"`{applied['admission_threshold']:.3f}`",
        f"- Episode: `{applied.get('episode_title') or applied.get('episode_uid')}`",
        f"- Scope: `{applied.get('scope')}`",
        f"- Current display: `{bool(applied.get('display', True))}`",
        "",
        "## Candidate explanation",
        "",
        # Untrusted model text, blockquoted for the same reason evidence quotes are: it must
        # never read as an unquoted "- [x] Correct" line and be mistaken for a real decision.
        *(_quote_block(applied.get("explanation") or "") or ["_(none supplied)_"]),
        "",
        "## Evidence",
        "",
    ]
    if applied.get("rule_pattern"):
        lines.extend(
            [
                f"Rule phrase: `{applied['rule_pattern']}` "
                f"(version `{applied.get('rule_version')}`)",
                "",
            ]
        )
    if assessment == "prelabeler-overlay":
        lines.extend(
            [
                "## Pre-labeler result",
                "",
                f"Decision: `{applied.get('prelabeler_decision')}`",
                f"Confidence: `{float(applied.get('prelabeler_confidence', 0.0)):.3f}`",
                *(_quote_block(applied.get("prelabeler_reason") or "") or ["_(none supplied)_"]),
                f"Evidence supported: `{bool(applied.get('prelabeler_evidence_supported'))}`",
                "",
                "Review whether the pre-labeler's decision is correct for this candidate.",
                "",
            ]
        )
    for evidence in applied.get("evidence") or []:
        where = evidence.get("where") or "source"
        lines.append(f"### {where}")
        quote = str(evidence.get("quote") or evidence.get("span") or "").strip()
        lines.extend(_quote_block(quote) or ["_(missing quote)_"])
        if where == "transcript" and evidence.get("start") is not None:
            end = evidence.get("end") if evidence.get("end") is not None else evidence.get("start")
            lines.append(f"Timestamp: `{evidence['start']:.3f}s`–`{float(end):.3f}s`")
        url = _safe_link(str(evidence.get("document_url") or ""))
        if url:
            label = evidence.get("document_locator") or "source document"
            lines.append(f"Source: [{label}]({url})")
        elif evidence.get("document_locator"):
            lines.append(f"Location: `{evidence['document_locator']}`")
        lines.append("")
    lines.extend(
        [
            _CHECKBOX_HEADER,
            "- [ ] Correct",
            "- [ ] Incorrect",
            "- [ ] Ambiguous",
            "",
            f"{REVIEW_MARKER}{json.dumps(meta, sort_keys=True)} -->",
            "",
        ]
    )
    return "\n".join(lines)


_DECISION_RE = re.compile(
    r"^- \[(?P<checked>[xX ])\] (?P<label>Correct|Incorrect|Ambiguous)\s*$", re.MULTILINE
)


def parse_review(body: str) -> tuple[dict[str, Any], str]:
    # render_review_body() always appends the genuine marker last, after the checkboxes. Untrusted
    # candidate text rendered earlier (explanation, document_locator) is never grounded against
    # source material, so it could otherwise be crafted to contain a marker-shaped decoy earlier
    # in the body; take the LAST match so a decoy earlier in the body can never be mistaken for
    # the real one.
    markers = list(
        re.finditer(re.escape(REVIEW_MARKER) + r"(?P<meta>\{.*?\})\s*-->", body, re.DOTALL)
    )
    if not markers:
        raise ValueError("LLM review metadata missing")
    metadata = json.loads(markers[-1].group("meta"))
    # _DECISION_RE must not scan the whole body: render_review_body() renders untrusted candidate
    # text (explanation, evidence quotes, document_locator) *before* the real checkbox block, with
    # no guarantee those fields are newline-free. A crafted field containing an embedded newline
    # could otherwise inject a fake, syntactically valid "- [x] Correct" line that this regex would
    # accept as a genuine human decision. render_review_body() emits exactly one literal
    # ``_CHECKBOX_HEADER`` line, immediately followed by the three real checkboxes and then the
    # genuine trailing marker -- nothing renders after it, so scanning only from its LAST
    # occurrence onward (the same "take the last one" reasoning as the marker above) means an
    # earlier decoy header/checkbox block injected via untrusted text can never be mistaken for the
    # real one.
    header_at = body.rfind(_CHECKBOX_HEADER)
    if header_at == -1:
        raise ValueError("choose exactly one LLM review decision")
    checked = [
        m.group("label").lower()
        for m in _DECISION_RE.finditer(body[header_at:])
        if m.group("checked").lower() == "x"
    ]
    if not checked:
        raise ValueError("no LLM review decision checked")
    if len(checked) > 1:
        raise ValueError("choose exactly one LLM review decision")
    return metadata, checked[0]


def ingest_review_body(
    state: dict[str, Any],
    body: str,
    *,
    config: EvaluationConfig,
    actor: str = "",
    issue_number: int | None = None,
    issue_url: str = "",
) -> dict[str, Any]:
    metadata, decision = parse_review(body)
    candidate = metadata.get("candidate")
    if not isinstance(candidate, dict):
        raise ValueError("LLM review candidate metadata missing")
    decision = {"correct": "correct", "incorrect": "incorrect", "ambiguous": "ambiguous"}[decision]
    review = record_review(
        state,
        candidate,
        decision=decision,
        actor=actor,
        issue_number=issue_number,
        issue_url=issue_url,
    )
    refresh_matrix(state, config=config)
    return review


def policy_fingerprint(config: EvaluationConfig, state: dict[str, Any]) -> str:
    """Fingerprint only the parts of the matrix that actually change an admission decision.

    ``refresh_matrix`` stamps every row with a fresh ``updated_at`` on every call regardless of
    whether ``qualified``/``threshold`` changed, so hashing the raw rows would change this
    fingerprint (and therefore invalidate every episode's cached projection) after every human
    review is ingested, even when no admission decision moved at all.
    """
    stable_rows = [
        {
            "key": row.get("key"),
            "qualified": row.get("qualified"),
            "threshold": row.get("threshold"),
        }
        for row in state.get("matrix", [])
        if isinstance(row, dict)
    ]
    return _json_hash(
        {
            "config": {
                "fallback_confidence": config.fallback_confidence,
                "required_precision": config.required_precision,
                "minimum_reviews": config.minimum_reviews,
                "prelabeler_required_precision": config.prelabeler_required_precision,
                "prelabeler_minimum_reviews": config.prelabeler_minimum_reviews,
                "prelabeler_minimum_decision_reviews": config.prelabeler_minimum_decision_reviews,
                "max_reviews_per_subject_stratum": config.max_reviews_per_subject_stratum,
                "fallbacks": config.fallbacks or {},
            },
            "matrix": stable_rows,
        }
    )


def render_digest(
    candidates: list[dict[str, Any]],
    *,
    selected: list[dict[str, Any]],
    config: EvaluationConfig,
    state: dict[str, Any],
    taxonomy: Any | None = None,
    rule_audits: list[dict[str, Any]] | None = None,
    generated_at: str | None = None,
) -> str:
    generated_at = generated_at or _utc_now()
    rows_by_id = {str(row.get("matrix_id")): row for row in _matrix_entries(state)}
    for candidate in candidates:
        rows_by_id.setdefault(
            matrix_key(candidate),
            {
                "matrix_id": matrix_key(candidate),
                "key": candidate_matrix_key(candidate),
                "reviewed": 0,
                "qualified": False,
                "threshold": None,
                "precision": None,
            },
        )
    tagger_candidates = [
        candidate
        for candidate in candidates
        if str(candidate.get("assessment_kind") or "tagger-admission") == "tagger-admission"
    ]
    prelabel_candidates = [
        candidate
        for candidate in candidates
        if candidate.get("assessment_kind") == "prelabeler-overlay"
    ]
    tagger_applied = [apply_admission(c, config=config, state=state) for c in tagger_candidates]
    admitted = sum(
        item.get("admission") == "admitted" and item.get("display", True) for item in tagger_applied
    )
    tagger_by_key = {matrix_key(candidate): candidate for candidate in tagger_candidates}
    prelabel_by_key = {matrix_key(candidate): candidate for candidate in prelabel_candidates}
    lines = [
        "# R5 unified tag calibration weekly digest",
        "",
        f"Generated: {generated_at}",
        f"Candidates observed: {len(tagger_candidates)} · admitted: {admitted} · "
        f"not displayed: {len(tagger_candidates) - admitted} · "
        f"overlay audits: {len(prelabel_candidates)}",
        f"Review batch: {len(selected)} · tagger gate: {config.minimum_reviews}/"
        f"{config.required_precision:.1%} · pre-labeler gate: {config.prelabeler_minimum_reviews}/"
        f"{config.prelabeler_required_precision:.1%}",
        "",
        "The initial tagger gate controls public admission. A fallback confidence of "
        f"`{config.fallback_confidence:.3f}` keeps unquantified candidates shadow-only unless the "
        "maintainer deliberately lowers the fallback. The qualified pre-labeler is an overlay: "
        "likely-incorrect decisions suppress display without deleting evidence.",
        "",
        "## Tagger admission progress",
        "",
        "| Tag | Source | Route | Scope | Reviewed | Threshold | Precision | Reviews left | "
        "Precision gap | "
        "Candidates | Public state |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for key_id, candidate in sorted(tagger_by_key.items()):
        row = _matrix_row(candidate, state) or rows_by_id.get(key_id) or {}
        key = row.get("key") or {}
        distance = qualification_distance(candidate, state=state, config=config)
        precision = distance.get("precision")
        precision_text = f"{precision:.1%}" if isinstance(precision, (int, float)) else "—"
        threshold = distance.get("threshold")
        threshold_text = f"{threshold:.3f}" if isinstance(threshold, (int, float)) else "—"
        same_key = [item for item in tagger_candidates if matrix_key(item) == key_id]
        applied_same_key = [apply_admission(item, config=config, state=state) for item in same_key]
        if any(item.get("candidate_state") == "historical" for item in applied_same_key):
            state_label = "historical evidence"
        elif any(
            item.get("prelabeler_basis") == "calibrated" and not item.get("display", True)
            for item in applied_same_key
        ):
            state_label = "suppressed by qualified overlay"
        elif any(
            item.get("prelabeler_basis") == "calibrated"
            and item.get("prelabeler_decision") == "needs_human_review"
            for item in applied_same_key
        ):
            state_label = "under continued sampling"
        else:
            state_label = (
                "public now" if row.get("qualified") else "public after tagger qualification"
            )
        if candidate.get("source_kind") == "rule" and state_label not in {
            "historical evidence",
            "suppressed by qualified overlay",
            "under continued sampling",
        }:
            state_label = "deterministic public now"
        lines.append(
            f"| `{key.get('label')}` | `{candidate.get('source_kind', 'llm')}` | "
            f"`{candidate.get('provider_model')}` | "
            f"`{key.get('scope')}` | "
            f"{distance['reviewed']}/{distance['required_reviews']} | "
            f"{threshold_text} | {precision_text} |"
        )
        lines[-1] += (
            f" | {distance['reviews_remaining']} | {distance['precision_gap']:.1%} | "
            f"{sum(1 for item in tagger_candidates if matrix_key(item) == key_id)} | "
            f"{state_label} |"
        )
    lines.extend(
        [
            "",
            "## Pre-labeler overlay progress",
            "",
            "| Tag | Source | Evaluator route | Scope | Reviewed | Threshold | "
            "Likely-correct precision | "
            "Likely-incorrect precision | Reviews left | Counts (correct / human / incorrect) | "
            "Overlay subjects | Monitoring | Status |",
            "|---|---|---|---|---:|---:|---:|---:|---|---:|---:|---|",
        ]
    )
    prelabel_subjects_by_key: dict[str, list[dict[str, Any]]] = {}
    for subject in tagger_candidates:
        if subject.get("prelabeler_decision") not in PRELABELER_DECISIONS:
            continue
        audit_key = matrix_key(prelabeler_review_candidate(subject))
        prelabel_subjects_by_key.setdefault(audit_key, []).append(subject)
    for key_id, candidate in sorted(prelabel_by_key.items()):
        row = rows_by_id.get(key_id) or {}
        distance = qualification_distance(candidate, state=state, config=config)
        counts = distance.get("decision_counts") or {}
        status = (
            "pre-labeler automation qualified" if distance["qualified"] else "pre-labeler shadow"
        )
        subjects = prelabel_subjects_by_key.get(key_id, [])
        applied_subjects = [apply_admission(item, config=config, state=state) for item in subjects]
        overlay_subjects = len(subjects)
        monitoring = sum(
            bool(item.get("prelabeler_qualified"))
            and item.get("prelabeler_decision") == "needs_human_review"
            for item in applied_subjects
        )
        threshold = distance.get("threshold")
        threshold_text = f"{threshold:.3f}" if isinstance(threshold, (int, float)) else "—"
        likely_correct = distance.get("likely_correct_precision")
        likely_incorrect = distance.get("likely_incorrect_precision")
        correct_text = f"{likely_correct:.1%}" if isinstance(likely_correct, (int, float)) else "—"
        incorrect_text = (
            f"{likely_incorrect:.1%}" if isinstance(likely_incorrect, (int, float)) else "—"
        )
        lines.append(
            f"| `{(row.get('key') or {}).get('label')}` | "
            f"`{candidate.get('source_kind', 'llm')}` | "
            f"`{candidate.get('evaluator_model') or candidate.get('prelabeler_model')}` | "
            f"`{(row.get('key') or {}).get('scope')}` | "
            f"{distance['reviewed']}/{distance['required_reviews']} | "
            f"{threshold_text} | {correct_text} | {incorrect_text} | "
            f"{distance['reviews_remaining']} | "
            f"{counts.get('likely_correct', 0)} / {counts.get('needs_human_review', 0)} / "
            f"{counts.get('likely_incorrect', 0)} | {overlay_subjects} | {monitoring} | {status} |"
        )
    deterministic = [
        candidate for candidate in tagger_candidates if candidate.get("source_kind") == "rule"
    ]
    rule_audits = [item for item in (rule_audits or []) if isinstance(item, dict)]
    observed_include_counts: dict[tuple[str, str], int] = {}
    observed_exclude_counts: dict[tuple[str, str], int] = {}
    for observation in rule_audits:
        key = (str(observation.get("tag_id") or ""), str(observation.get("pattern") or ""))
        if not key[0] or not key[1]:
            continue
        target = (
            observed_exclude_counts
            if observation.get("kind") == "exclude"
            else observed_include_counts
        )
        target[key] = target.get(key, 0) + int(observation.get("match_count") or 1)
    # Older records have no rule-observation sidecar. Preserve their positive-match report using
    # candidate provenance, while newer records get exact include/exclude hit counts.
    audited_tag_ids = {key[0] for key in observed_include_counts}
    audited_tag_ids.update(key[0] for key in observed_exclude_counts)
    for candidate in deterministic:
        tag_id = str(candidate.get("id") or "")
        phrases = candidate.get("rule_patterns") or [candidate.get("rule_pattern")]
        for phrase in phrases:
            if phrase and tag_id not in audited_tag_ids:
                key = (tag_id, str(phrase))
                observed_include_counts[key] = observed_include_counts.get(key, 0) + 1
    audited_rules = sum(
        1 for candidate in prelabel_candidates if candidate.get("source_kind") == "rule"
    )
    suppressed_rules = sum(
        1
        for item in tagger_applied
        if item.get("source_kind") == "rule" and not item.get("display", True)
    )
    overlay_subjects = [
        subject
        for subject in tagger_candidates
        if subject.get("prelabeler_decision") in PRELABELER_DECISIONS
    ]
    admitted_overlay_subjects = sum(
        apply_admission(subject, config=config, state=state).get("admission") == "admitted"
        for subject in overlay_subjects
    )
    deterministic_overlay_subjects = sum(
        subject.get("source_kind") == "rule" for subject in overlay_subjects
    )
    post_admission_monitoring = sum(
        bool(applied_subject.get("prelabeler_qualified"))
        and applied_subject.get("prelabeler_decision") == "needs_human_review"
        for applied_subject in (
            apply_admission(subject, config=config, state=state) for subject in overlay_subjects
        )
    )
    human_confirmed_suppressions = 0
    deterministic_decisions = {decision: 0 for decision in PRELABELER_DECISIONS}
    for subject in deterministic:
        decision = subject.get("prelabeler_decision")
        if decision in deterministic_decisions:
            deterministic_decisions[decision] += 1
        if subject.get("prelabeler_decision") != "likely_incorrect":
            continue
        subject_id = str(subject.get("candidate_id") or candidate_id(subject))
        if any(
            review.get("assessment_kind") == "prelabeler-overlay"
            and review.get("subject_candidate_id") == subject_id
            and review.get("decision") == "correct"
            for review in (state.get("reviews") or {}).values()
            if isinstance(review, dict)
        ):
            human_confirmed_suppressions += 1
    rule_keys = {
        (
            candidate.get("episode_uid"),
            candidate.get("chapter_id"),
            candidate.get("id") or candidate.get("label"),
        )
        for candidate in deterministic
    }
    llm_without_rule = sum(
        candidate.get("source_kind") == "llm"
        and (
            candidate.get("episode_uid"),
            candidate.get("chapter_id"),
            candidate.get("id") or candidate.get("label"),
        )
        not in rule_keys
        for candidate in tagger_candidates
    )
    lines.extend(
        [
            "",
            "## Deterministic audit",
            "",
            f"Rule matches observed: {len(deterministic)} · audited: "
            f"{audited_rules} · suppressed by qualified overlay: {suppressed_rules} · "
            f"human-confirmed suppressions: {human_confirmed_suppressions}",
            f"Already-admitted subjects under an overlay: {admitted_overlay_subjects} · "
            f"deterministic subjects under an overlay: {deterministic_overlay_subjects} · "
            f"post-admission monitoring: {post_admission_monitoring} · "
            f"LLM-supported tags without a rule match: {llm_without_rule}",
            "Deterministic pre-labeler decisions: "
            f"likely-correct {deterministic_decisions['likely_correct']} · "
            f"needs-human-review {deterministic_decisions['needs_human_review']} · "
            f"likely-incorrect {deterministic_decisions['likely_incorrect']}",
            "",
            "Observed phrases: "
            + ", ".join(
                f"`{candidate.get('rule_pattern')}`"
                for candidate in deterministic
                if candidate.get("rule_pattern")
            )
            if deterministic
            else "Observed phrases: none",
        ]
    )
    disagreement_by_phrase: dict[str, int] = {}
    for candidate in prelabel_candidates:
        phrase = str(candidate.get("rule_pattern") or "")
        if phrase and candidate.get("prelabeler_decision") != "likely_correct":
            disagreement_by_phrase[phrase] = disagreement_by_phrase.get(phrase, 0) + 1
    lines.extend(
        [
            "",
            "High-disagreement phrases: "
            + (
                ", ".join(
                    f"`{phrase}` ({count})"
                    for phrase, count in sorted(
                        disagreement_by_phrase.items(), key=lambda item: (-item[1], item[0])
                    )
                )
                or "none observed"
            ),
        ]
    )
    if taxonomy is not None:
        lines.extend(
            [
                "",
                "## Deterministic taxonomy inventory",
                "",
                "All include/exclude phrases are authored in `config/taxonomy.yml`; observed "
                "counts below come from the persisted rule candidate ledger.",
                "",
                "| Tag | Include phrases | Exclude phrases | Observed includes | "
                "Observed excludes |",
                "|---|---|---|---:|---:|",
            ]
        )
        for tag in getattr(taxonomy, "tags", ()):
            phrases = ", ".join(f"`{phrase}`" for phrase in tag.include)
            excludes = ", ".join(f"`{phrase}`" for phrase in tag.exclude) or "—"
            include_count = sum(
                observed_include_counts.get((tag.id, phrase), 0) for phrase in tag.include
            )
            exclude_count = sum(
                observed_exclude_counts.get((tag.id, phrase), 0) for phrase in tag.exclude
            )
            lines.append(
                f"| `{tag.id}` | {phrases} | {excludes} | {include_count} | {exclude_count} |"
            )
    lines.extend(["", "## Review children", ""])
    if not selected:
        lines.append("No unreviewed candidates are currently eligible for a review child.")
    else:
        for candidate in selected:
            cid = candidate.get("candidate_id") or candidate_id(candidate)
            lines.append(
                f"- [ ] `{cid}` — `{candidate.get('id') or candidate.get('label')}` "
                f"at `{float(candidate.get('confidence', 0.0)):.3f}`"
            )
    return "\n".join(lines) + "\n"


__all__ = [
    "DEFAULT_STATE_NAME",
    "EvaluationConfig",
    "REVIEW_DECISIONS",
    "apply_admission",
    "candidate_id",
    "candidate_matrix_key",
    "config_from_mapping",
    "ingest_review_body",
    "load_state",
    "matrix_key",
    "policy_fingerprint",
    "PRELABELER_DECISIONS",
    "prelabeler_review_candidate",
    "qualification_distance",
    "record_review",
    "refresh_matrix",
    "render_digest",
    "render_review_body",
    "resolve_threshold",
    "resolve_prelabeler_threshold",
    "save_state",
    "select_review_candidates",
    "visible_candidates",
]
