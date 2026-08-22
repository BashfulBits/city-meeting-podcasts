"""R6 grounded meeting-moment extraction and admission helpers.

The model is allowed to propose text and a quality score, but it is never trusted for timing.
Every public quote is derived from an exact transcript match and carries a durable calibration
identity.  Video rendering is intentionally separate from this module so a media failure cannot
erase a text admission.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from citypods.compute.structured import register_response_model, response_model

MOMENTS_CONTRACT = "moment-extraction"
MOMENTS_PROMPT_VERSION = "1"
MOMENTS_PIPELINE_VERSION = "1"
MOMENTS_MIN_SECONDS = 8.0
MOMENTS_MAX_SECONDS = 90.0


def ensure_moment_contract():
    """Register the R6 structured response contract lazily and idempotently."""
    from typing import Literal

    from pydantic import BaseModel, ConfigDict, Field

    class SummaryPoint(BaseModel):
        model_config = ConfigDict(extra="forbid")
        chapter_id: str = Field(min_length=1, max_length=160)
        text: str = Field(min_length=1, max_length=400)
        confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    class PullQuote(BaseModel):
        model_config = ConfigDict(extra="forbid")
        quote: str = Field(min_length=3, max_length=500)
        chapter_id: str | None = Field(default=None, max_length=160)
        quality_score: float = Field(default=0.0, ge=0.0, le=1.0)
        confidence: float = Field(default=0.0, ge=0.0, le=1.0)
        why: str = Field(default="", max_length=400)

    class Decision(BaseModel):
        model_config = ConfigDict(extra="forbid")
        chapter_id: str = Field(min_length=1, max_length=160)
        decision_type: Literal["approved", "denied", "deferred", "tabled", "no_decision", "unclear"]
        quote: str = Field(min_length=3, max_length=500)
        confidence: float = Field(default=0.0, ge=0.0, le=1.0)
        explanation: str = Field(default="", max_length=400)

    class Response(BaseModel):
        model_config = ConfigDict(extra="forbid")
        episode_summary: str = Field(default="", max_length=1200)
        summary_points: list[SummaryPoint] = Field(default_factory=list)
        pull_quotes: list[PullQuote] = Field(default_factory=list, max_length=10)
        decisions: list[Decision] = Field(default_factory=list, max_length=20)

    model = getattr(ensure_moment_contract, "model", None)
    if model is None:
        try:
            model = response_model(MOMENTS_CONTRACT)
        except ValueError:
            model = register_response_model(MOMENTS_CONTRACT, Response)
        ensure_moment_contract.model = model
    else:
        try:
            response_model(MOMENTS_CONTRACT)
        except ValueError:
            register_response_model(MOMENTS_CONTRACT, model)
    return model


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def transcript_region(
    quote: str, segments: Iterable[Mapping[str, Any]]
) -> tuple[float, float] | None:
    """Find the exact timed transcript region containing ``quote``."""
    target = _norm(quote)
    if not target:
        return None
    pieces: list[str] = []
    owners: list[int] = []
    rows = list(segments)
    for index, row in enumerate(rows):
        text = _norm(str(row.get("text") or ""))
        if not text:
            continue
        if pieces:
            pieces.append(" ")
            owners.append(index)
        pieces.append(text)
        owners.extend([index] * len(text))
    joined = "".join(pieces)
    start_index = joined.find(target)
    if start_index < 0 or not owners:
        return None
    end_index = min(start_index + len(target) - 1, len(owners) - 1)
    start_row = rows[owners[start_index]]
    end_row = rows[owners[end_index]]
    start = start_row.get("start")
    end = end_row.get("end", end_row.get("start"))
    if not isinstance(start, int | float) or not isinstance(end, int | float):
        return None
    if float(end) <= float(start):
        return None
    return float(start), float(end)


def recipe_hash(
    *,
    transcript_key: str | None,
    transcript_words_key: str | None,
    chapters: list[dict[str, Any]],
    agenda_text_key: str | None,
    route_models: tuple[str, ...],
    meeting_family: str,
    evaluation_policy: str,
) -> str:
    payload = {
        "pipeline": MOMENTS_PIPELINE_VERSION,
        "prompt": MOMENTS_PROMPT_VERSION,
        "transcript": transcript_key,
        "words": transcript_words_key,
        "chapters": chapters,
        "agenda": agenda_text_key,
        "models": route_models,
        "meeting_family": meeting_family,
        "evaluation_policy": evaluation_policy,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()[:24]


def candidate_matrix_key(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Return the stable R6 quality-calibration dimensions."""
    return {
        "feature": str(candidate.get("feature") or "pull-quote"),
        "provider_model": str(candidate.get("provider_model") or ""),
        "prompt_version": str(candidate.get("prompt_version") or MOMENTS_PROMPT_VERSION),
        "meeting_family": str(candidate.get("meeting_family") or "default"),
        "duration_bucket": str(candidate.get("duration_bucket") or "unknown"),
        "framing_profile": str(candidate.get("framing_profile") or "default"),
        "judge_model": str(candidate.get("judge_model") or ""),
        "judge_prompt_version": str(candidate.get("judge_prompt_version") or ""),
        "judge_schema_version": str(candidate.get("judge_schema_version") or ""),
    }


def candidate_id(candidate: Mapping[str, Any]) -> str:
    identity = {
        **candidate_matrix_key(candidate),
        "episode_uid": candidate.get("episode_uid"),
        "chapter_id": candidate.get("chapter_id"),
        "quote": candidate.get("quote"),
        "recipe_hash": candidate.get("recipe_hash"),
    }
    digest = hashlib.sha1(
        json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()[:20]
    return f"r6-{digest}"


def duration_bucket(seconds: float | int | None) -> str:
    value = float(seconds or 0)
    if value < 20:
        return "8-19"
    if value < 45:
        return "20-44"
    return "45-90"


def normalize_quote_candidate(
    candidate: Mapping[str, Any],
    *,
    episode_uid: str,
    provider_model: str,
    recipe: str,
    meeting_family: str,
    transcript_segments: list[dict[str, Any]],
) -> dict[str, Any] | None:
    quote = str(candidate.get("quote") or "").strip()
    region = transcript_region(quote, transcript_segments)
    if region is None:
        return None
    start, end = region
    if end - start < MOMENTS_MIN_SECONDS or end - start > MOMENTS_MAX_SECONDS:
        return None
    value = {
        "candidate_id": "",
        "feature": "pull-quote",
        "source_kind": "llm",
        "assessment_kind": "quality-admission",
        "episode_uid": episode_uid,
        "chapter_id": candidate.get("chapter_id"),
        "quote": quote,
        "why": str(candidate.get("why") or "")[:400],
        "quality_score": float(candidate.get("quality_score") or 0.0),
        "confidence": float(candidate.get("confidence") or 0.0),
        "start": start,
        "end": end,
        "provider_model": provider_model,
        "prompt_version": MOMENTS_PROMPT_VERSION,
        "meeting_family": meeting_family,
        "duration_bucket": duration_bucket(end - start),
        "framing_profile": "social-vertical-v1",
        "recipe_hash": recipe,
        "manual_status": None,
        "admission": "shadow",
        "display": False,
    }
    value["candidate_id"] = candidate_id(value)
    return value


def quote_safety_gate(candidate: Mapping[str, Any], segments: list[dict[str, Any]]) -> bool:
    """Recheck grounded timing after a maintainer supplies a range override."""
    quote = str(candidate.get("quote") or "")
    grounded = transcript_region(quote, segments)
    start = candidate.get("start")
    end = candidate.get("end")
    if grounded is None or not isinstance(start, int | float) or not isinstance(end, int | float):
        return False
    duration = float(end) - float(start)
    return (
        MOMENTS_MIN_SECONDS <= duration <= MOMENTS_MAX_SECONDS
        and float(start) <= grounded[0]
        and float(end) >= grounded[1]
    )


def parse_transcript_segments(content: bytes, fmt: str = "vtt") -> list[dict[str, Any]]:
    """Parse the small timed-transcript subset needed for grounding and captions."""
    pattern = re.compile(
        r"(?P<start>\d{2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*"
        r"(?P<end>\d{2}:\d{2}:\d{2}[.,]\d{3})\s*\n(?P<text>.*?)(?=\n\s*\n|\Z)",
        re.S,
    )

    def seconds(value: str) -> float:
        hours, minutes, remainder = value.replace(",", ".").split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(remainder)

    if fmt not in {"vtt", "srt"}:
        return []
    rows: list[dict[str, Any]] = []
    for match in pattern.finditer(content.decode("utf-8-sig", errors="replace")):
        text = re.sub(r"<[^>]+>", "", match.group("text"))
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            rows.append(
                {
                    "start": seconds(match.group("start")),
                    "end": seconds(match.group("end")),
                    "text": text,
                }
            )
    return rows


def response_payload(output: Mapping[str, Any]) -> dict[str, Any]:
    """Extract and validate a provider-neutral structured response."""
    choices = output.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("moment response did not contain choices")
    message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, str):
        raise ValueError("moment response did not contain JSON content")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("moment response was not valid JSON") from exc
    model = ensure_moment_contract()
    return model.model_validate(parsed).model_dump()


__all__ = [
    "MOMENTS_CONTRACT",
    "MOMENTS_MAX_SECONDS",
    "MOMENTS_MIN_SECONDS",
    "MOMENTS_PIPELINE_VERSION",
    "MOMENTS_PROMPT_VERSION",
    "candidate_id",
    "candidate_matrix_key",
    "duration_bucket",
    "ensure_moment_contract",
    "normalize_quote_candidate",
    "parse_transcript_segments",
    "quote_safety_gate",
    "response_payload",
    "recipe_hash",
    "transcript_region",
]
