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


@dataclass(frozen=True)
class EvaluationConfig:
    """Feature-independent policy knobs; feature-specific fallbacks live in ``fallbacks``."""

    fallback_confidence: float = 1.0
    required_precision: float = 0.90
    minimum_reviews: int = 12
    review_batch_size: int = 20
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
        review_batch_size=max(1, int(raw.get("review_batch_size", 20))),
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
    path = Path(path)
    if not path.exists():
        return {"version": STATE_VERSION, "reviews": {}, "matrix": [], "trend": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": STATE_VERSION, "reviews": {}, "matrix": [], "trend": []}
    if not isinstance(value, dict):
        return {"version": STATE_VERSION, "reviews": {}, "matrix": [], "trend": []}
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
    return {
        "feature": str(candidate.get("feature") or ""),
        "provider_model": str(candidate.get("provider_model") or ""),
        "prompt_version": str(candidate.get("prompt_version") or ""),
        "taxonomy_version": candidate.get("taxonomy_version"),
        "label": str(candidate.get("id") or candidate.get("label") or ""),
        "scope": str(
            candidate.get("scope") or ("chapter" if candidate.get("chapter_id") else "episode")
        ),
    }


def matrix_key(candidate: dict[str, Any]) -> str:
    return _json_hash(candidate_matrix_key(candidate))


def candidate_id(candidate: dict[str, Any]) -> str:
    """Stable review identity; source text and model recipe are part of the identity."""
    identity = {
        **candidate_matrix_key(candidate),
        "episode_uid": candidate.get("episode_uid"),
        "chapter_id": candidate.get("chapter_id"),
        "recipe_hash": candidate.get("recipe_hash"),
        "confidence": candidate.get("confidence"),
    }
    return f"llm-{_json_hash(identity)}"


def _matrix_entries(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [entry for entry in state.get("matrix", []) if isinstance(entry, dict)]


def resolve_threshold(
    candidate: dict[str, Any], *, config: EvaluationConfig, state: dict[str, Any]
) -> tuple[float, str]:
    """Resolve an exact qualified row, then the feature/route fallback."""
    key = candidate_matrix_key(candidate)
    for entry in _matrix_entries(state):
        if entry.get("key") == key and entry.get("qualified"):
            threshold = entry.get("threshold")
            if isinstance(threshold, int | float):
                return float(threshold), "calibrated"
    return config.fallback_for(key["feature"], key["provider_model"]), "fallback"


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
    return value


def visible_candidates(
    candidates: list[dict[str, Any]], *, config: EvaluationConfig, state: dict[str, Any]
) -> list[dict[str, Any]]:
    return [
        applied
        for candidate in candidates
        if (applied := apply_admission(candidate, config=config, state=state))["admission"]
        == "admitted"
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
        for threshold in thresholds:
            selected = [
                item
                for item in reviews
                if isinstance(item.get("confidence"), (int, float))
                and float(item["confidence"]) >= threshold
            ]
            correct = sum(item.get("decision") == "correct" for item in selected)
            precision = correct / len(selected) if selected else 0.0
            if len(selected) >= config.minimum_reviews and precision >= config.required_precision:
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
    key_id = matrix_key(candidate)
    row = next((item for item in _matrix_entries(state) if item.get("matrix_id") == key_id), None)
    reviewed = int((row or {}).get("reviewed", 0) or 0)
    qualified = bool((row or {}).get("qualified"))
    threshold, _ = resolve_threshold(candidate, config=config, state=state)
    return (
        0 if not qualified else 1,
        0 if reviewed == 0 else 1,
        reviewed,
        abs(float(candidate.get("confidence", 0.0)) - threshold),
        str(candidate.get("candidate_id") or candidate_id(candidate)),
    )


def select_review_candidates(
    candidates: list[dict[str, Any]], *, state: dict[str, Any], config: EvaluationConfig
) -> list[dict[str, Any]]:
    reviewed = set((state.get("reviews") or {}).keys())
    pending = [
        candidate
        for candidate in candidates
        if str(candidate.get("candidate_id") or candidate_id(candidate)) not in reviewed
    ]
    return sorted(pending, key=lambda item: review_priority(item, state=state, config=config))[
        : config.review_batch_size
    ]


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
            )
        },
    }
    lines = [
        f"# LLM review: `{applied.get('id') or applied.get('label')}`",
        "",
        f"- Feature: `{applied.get('feature')}`",
        f"- Model route: `{applied.get('provider_model')}`",
        f"- Confidence: `{float(applied.get('confidence', 0.0)):.3f}`",
        f"- Current policy: `{applied['admission_basis']}` at "
        f"`{applied['admission_threshold']:.3f}`",
        f"- Episode: `{applied.get('episode_title') or applied.get('episode_uid')}`",
        f"- Scope: `{applied.get('scope')}`",
        "",
        "## LLM explanation",
        "",
        # Untrusted model text, blockquoted for the same reason evidence quotes are: it must
        # never read as an unquoted "- [x] Correct" line and be mistaken for a real decision.
        *(_quote_block(applied.get("explanation") or "") or ["_(none supplied)_"]),
        "",
        "## Evidence",
        "",
    ]
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
            "Choose exactly one:",
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
    checked = [
        m.group("label").lower()
        for m in _DECISION_RE.finditer(body)
        if m.group("checked").lower() == "x"
    ]
    if len(checked) != 1:
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
    generated_at: str | None = None,
) -> str:
    generated_at = generated_at or _utc_now()
    admitted = sum(
        apply_admission(c, config=config, state=state)["admission"] == "admitted"
        for c in candidates
    )
    rows_by_id = {str(row.get("matrix_id")): row for row in _matrix_entries(state)}
    for candidate in candidates:
        key_id = matrix_key(candidate)
        rows_by_id.setdefault(
            key_id,
            {
                "matrix_id": key_id,
                "key": candidate_matrix_key(candidate),
                "reviewed": 0,
                "qualified": False,
                "threshold": None,
                "precision": None,
            },
        )
    rows = [rows_by_id[key] for key in sorted(rows_by_id)]
    lines = [
        "# LLM calibration weekly digest",
        "",
        f"Generated: {generated_at}",
        f"Candidates observed: {len(candidates)} · admitted: {admitted} · "
        f"shadow: {len(candidates) - admitted}",
        f"Review batch: {len(selected)} · required precision: "
        f"{config.required_precision:.1%} · minimum reviews: {config.minimum_reviews}",
        "",
        "Unqualified and sparsely reviewed matrix rows are selected first. "
        "A fallback confidence of "
        f"`{config.fallback_confidence:.3f}` keeps unquantified candidates shadow-only unless the "
        "maintainer deliberately lowers the fallback.",
        "",
        "## Calibration matrix",
        "",
        "| Feature | Model route | Label | Scope | Reviews | Precision | Threshold | Status |",
        "|---|---|---|---|---:|---:|---:|---|",
    ]
    for row in rows:
        key = row.get("key") or {}
        precision = row.get("precision")
        threshold = row.get("threshold")
        feature = key.get("feature")
        route = key.get("provider_model")
        label = key.get("label")
        scope = key.get("scope")
        if isinstance(precision, int | float) and isinstance(threshold, int | float):
            status = "qualified" if row.get("qualified") else "sparse/shadow"
            lines.append(
                f"| `{feature}` | `{route}` | `{label}` | `{scope}` | "
                f"{row.get('reviewed', 0)} | {precision:.1%} | {threshold:.3f} | {status} |"
            )
        else:
            lines.append(
                f"| `{feature}` | `{route}` | `{label}` | `{scope}` | "
                f"{row.get('reviewed', 0)} | — | — | sparse/shadow |"
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
    "record_review",
    "refresh_matrix",
    "render_digest",
    "render_review_body",
    "resolve_threshold",
    "save_state",
    "select_review_candidates",
    "visible_candidates",
]
