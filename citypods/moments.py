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

from citypods.compute.structured import (
    parse_structured_json,
    register_response_model,
    response_model,
)

MOMENTS_CONTRACT = "moment-extraction"
# 2 (2026-09-24): pull-quote criteria in the prompt (MOMENTS_SYSTEM_PROMPT); a new version starts a
# new human-score calibration cell (review/36) and re-runs extraction through the recipe hash.
MOMENTS_PROMPT_VERSION = "2"
MOMENTS_PIPELINE_VERSION = "2"
# Council feeds run on full-meeting transcripts, which can exceed the Gemini free tier's real
# per-request ceiling well before its 1M-token context window (confirmed live -- see
# `hard_input_ceiling` in config/provider_limits.yml). `deepseek/deepseek-v4-flash` (OrcaRouter,
# 1M context) and `moonshotai/kimi-k3` (NVIDIA) have no such hard cap, added as overflow so an
# oversized council job has somewhere to go instead of a guaranteed 429. Order matters only for
# tie-breaking (the caller's own model is still preferred when multiple routes are eligible), not
# correctness -- the Gemini entries stay first.
#
# THIS TUPLE IS PART OF THE MOMENTS RECIPE HASH (`recipe_hash(route_models=...)`): changing it
# re-runs moments for every council episode. 2026-09-24 (approved backfill): gemini-3.8/3.7-flash
# added for capacity (independent 20/day pools, AA 40.9/39.1), and the retired
# `deepseek/deepseek-v4-pro` alias (which pointed at NVIDIA's v4.1) replaced by OrcaRouter's v4:
# NVIDIA's v4.1 returns empty content to any `response_format` (verified live 2026-09-24). GLM 5.3
# Flash (OrcaRouter, AA 41.8, 800/day, 1M context) added the same day for capacity.
COUNCIL_MOMENT_MODELS = (
    "gemini/gemini-3.8-flash",
    "gemini/gemini-3.7-flash",
    "gemini/gemini-3.6-flash",
    "gemini/gemini-3.5-flash",
    "zai/glm-5.3-flash",
    "deepseek/deepseek-v4-flash",
    "moonshotai/kimi-k3",
)
DEFAULT_MOMENT_MODELS = ("gemini/gemini-3.5-flash-lite", "gemini/gemini-3.1-flash-lite")
# Derived from the project's goals (VISION: legible, shareable civic process; quote-driven, never
# editorialized; "national highlights" of strong public comment, officials doing the work well, and
# transparently quoted opposition). Pull quotes become captioned 9:16 share clips, so each must
# stand alone. The judge scores against the same criteria (moment_judging.JUDGE_SYSTEM_PROMPT).
PULL_QUOTE_CRITERIA = (
    "Pull quotes are short exact transcript excerpts that help a resident who missed the meeting "
    "understand what was at stake and how people argued it. They are published as captioned, "
    "shareable clips, so each must work on its own. A strong pull quote: (1) stands alone -- a "
    "listener with no other context understands the point, without 'this item', 'as I said', or "
    "'the gentleman before me'; (2) says something substantive about a public decision: a "
    "position, reason, consequence, cost, number, or commitment. Any civic topic qualifies; give "
    "particular attention to land use, housing, zoning, parking, streets and transportation, "
    "budgets, taxes, debt, and infrastructure maintenance; (3) is clear and memorable -- "
    "plain-spoken, vivid, or heartfelt lines are welcome when the feeling conveys real stakes, not "
    "when it is only heat; (4) reflects the range of voices -- public commenters, elected "
    "officials, and staff; when people disagreed on an item, include each side's strongest "
    "statement, including opposition to housing or development, quoted without framing; (5) is "
    "one to three sentences of contiguous speech. Do not choose: procedure (motions, seconds, roll "
    "calls, 'all in favor', item numbers -- outcomes belong in decisions); greetings, thanks, "
    "pleasantries, jokes without substance, crosstalk, or garbled transcription; lines chosen to "
    "mock, embarrass, or provoke, or that are only insults; a member of the public's personal "
    "details (home address, phone, health, family); anything whose meaning depends on your "
    "explanation. Prefer a few strong quotes over many weak ones, and return none when the meeting "
    "has none. Set quality_score by how well a quote meets these criteria. In why, write one "
    "neutral sentence on what the quote shows, and never name a member of the public (say "
    "'a resident', 'a commenter', 'a business owner')."
)
MOMENTS_SYSTEM_PROMPT = (
    "You extract civic meeting moments. Quote only exact contiguous wording from the transcript. "
    "Do not invent votes, decisions, names, times, or outcomes. Summary points: one neutral "
    "description per supplied chapter, routine chapters included. Decisions: recorded outcomes, "
    "each with the exact quote that states it. " + PULL_QUOTE_CRITERIA
)

MOMENTS_MIN_SECONDS = 8.0
MOMENTS_MAX_SECONDS = 90.0
# Output-token budget for one moments extraction. Reasoning models spend output tokens thinking
# before they answer: at the former 4,096, GLM 5.3 Flash on a 21-27k-token council transcript used
# all 4,096 on reasoning and stopped with EMPTY content (finish_reason "length", verified in the AI
# Gateway logs 2026-09-25; kimi-k3 hit the same wall on 3 of 8 calls). Every r6-moments route
# allows at least 65,536 output tokens. Not part of the recipe hash (nothing re-extracts).
MOMENTS_OUTPUT_TOKEN_BUDGET = 16_384
MOMENTS_PADDING_SECONDS = 1.5
MOMENTS_FRAMING_PROFILE = "social-vertical-opencv-mouth-motion-v1"


def ensure_moment_contract():
    """Register the R6 structured response contract lazily and idempotently."""
    cached = getattr(ensure_moment_contract, "model", None)
    if cached is not None:
        return cached

    try:
        model = response_model(MOMENTS_CONTRACT)
        ensure_moment_contract.model = model
        return model
    except ValueError:
        pass

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

    model = register_response_model(MOMENTS_CONTRACT, Response)
    ensure_moment_contract.model = model
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


_WORD_TOKEN_RE = re.compile(r"[^\w]+")


def _token(value: str) -> str:
    return _WORD_TOKEN_RE.sub("", str(value or "").casefold())


def parse_words_sidecar(data: bytes | None) -> list[dict[str, Any]]:
    """Served-time words from an ASR/provider-align ``.words.json`` sidecar (empty when absent)."""
    if not data:
        return []
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []
    words: list[dict[str, Any]] = []
    # A malformed container is skipped, never raised: the stage parses this before its per-episode
    # error handling, so one bad sidecar must not stop later episodes.
    segments = payload.get("segments", []) if isinstance(payload, dict) else []
    for segment in segments if isinstance(segments, list) else []:
        segment_words = segment.get("words", []) if isinstance(segment, dict) else []
        for word in segment_words if isinstance(segment_words, list) else []:
            if not isinstance(word, dict):
                continue
            text = str(word.get("w") or "").strip()
            start, end = word.get("s"), word.get("e")
            if text and isinstance(start, int | float) and isinstance(end, int | float):
                words.append({"text": text, "start": float(start), "end": float(end)})
    return words


def word_region(
    quote: str, words: list[Mapping[str, Any]], *, near: tuple[float, float] | None = None
) -> tuple[float, float] | None:
    """Exact spoken span of ``quote`` from word timing: first word's start, last word's end.

    Words are compared as punctuation-free tokens. With ``near`` (the cue-level match), only an
    occurrence inside it counts -- even a single match elsewhere is rejected, so a sidecar that
    disagrees with the cues falls back to the cue span instead of relocating the quote. An
    unresolvable repeat also returns None rather than guessing.
    """
    target = [token for token in (_token(part) for part in str(quote).split()) if token]
    tokens = [_token(word.get("text")) for word in words]
    if not target or len(target) > len(tokens):
        return None
    hits = [
        index
        for index in range(len(tokens) - len(target) + 1)
        if tokens[index : index + len(target)] == target
    ]
    if near is not None:
        hits = [i for i in hits if near[0] - 1.0 <= float(words[i]["start"]) <= near[1] + 1.0]
    if len(hits) != 1:
        return None
    first, last = words[hits[0]], words[hits[0] + len(target) - 1]
    start, end = float(first["start"]), float(last["end"])
    return (start, end) if end > start else None


def quote_timing(
    quote: str,
    segments: list[Mapping[str, Any]],
    words: list[Mapping[str, Any]] | None = None,
) -> tuple[float, float, str] | None:
    """``(start, end, source)`` of a quote: word-exact when the words sidecar resolves it."""
    cue = transcript_region(quote, segments)
    if cue is None:
        return None
    exact = word_region(quote, list(words or []), near=cue) if words else None
    if exact is not None:
        return exact[0], exact[1], "words"
    return cue[0], cue[1], "cues"


def clip_window(
    quote_start: float, quote_end: float, *, bounds: tuple[float, float]
) -> tuple[float, float] | None:
    """Padded share-clip window around the spoken quote, widened to the minimum clip length.

    Returns None only when the quote itself (plus padding) exceeds the maximum clip length.
    """
    low, high = bounds
    start = max(low, quote_start - MOMENTS_PADDING_SECONDS)
    end = min(high, quote_end + MOMENTS_PADDING_SECONDS)
    shortfall = MOMENTS_MIN_SECONDS - (end - start)
    if shortfall > 0:
        start = max(low, start - shortfall / 2)
        end = min(high, start + MOMENTS_MIN_SECONDS)
        start = max(low, end - MOMENTS_MIN_SECONDS)
    if end - start < MOMENTS_MIN_SECONDS or end - start > MOMENTS_MAX_SECONDS:
        return None
    return start, end


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
        "padding_seconds": MOMENTS_PADDING_SECONDS,
        "minimum_seconds": MOMENTS_MIN_SECONDS,
        "maximum_seconds": MOMENTS_MAX_SECONDS,
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
    transcript_words: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    quote = str(candidate.get("quote") or "").strip()
    timing = quote_timing(quote, transcript_segments, transcript_words)
    if timing is None:
        return None
    quote_start, quote_end, timing_source = timing
    transcript_start = min(
        float(row["start"]) if row.get("start") is not None else quote_start
        for row in transcript_segments
    )
    transcript_end = max(
        float(row["end"]) if row.get("end") is not None else quote_end
        for row in transcript_segments
    )
    window = clip_window(quote_start, quote_end, bounds=(transcript_start, transcript_end))
    if window is None:
        return None
    start, end = window
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
        # Served time. start/end is the share-clip window; quote_start/quote_end is the exact
        # spoken quote (word-accurate when timing_source is "words"), kept so clips can be
        # re-framed later without searching the transcript again.
        "start": start,
        "end": end,
        "quote_start": quote_start,
        "quote_end": quote_end,
        "timing_source": timing_source,
        "provider_model": provider_model,
        "prompt_version": MOMENTS_PROMPT_VERSION,
        "meeting_family": meeting_family,
        "duration_bucket": duration_bucket(end - start),
        "framing_profile": MOMENTS_FRAMING_PROFILE,
        "recipe_hash": recipe,
        "manual_status": None,
        "admission": "shadow",
        "display": False,
    }
    value["candidate_id"] = candidate_id(value)
    return value


def normalize_decision_candidate(
    candidate: Mapping[str, Any],
    *,
    provider_model: str,
    transcript_segments: list[dict[str, Any]],
    transcript_words: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Keep an AI interpretation only when its supporting quote is transcript-grounded."""
    quote = str(candidate.get("quote") or "").strip()
    timing = quote_timing(quote, transcript_segments, transcript_words)
    if timing is None:
        return None
    return {
        "chapter_id": str(candidate.get("chapter_id") or ""),
        "decision_type": str(candidate.get("decision_type") or "unclear"),
        "quote": quote,
        "start": timing[0],
        "end": timing[1],
        "timing_source": timing[2],
        "confidence": float(candidate.get("confidence") or 0.0),
        "explanation": str(candidate.get("explanation") or "")[:400],
        "source_kind": "llm",
        "provider_model": provider_model,
        "prompt_version": MOMENTS_PROMPT_VERSION,
        "label": "AI interpretation; not official minutes or vote evidence.",
    }


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
        r"(?P<end>\d{2}:\d{2}:\d{2}[.,]\d{3})[^\r\n]*\r?\n"
        r"(?P<text>.*?)(?=\r?\n[ \t]*\r?\n|\Z)",
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
    parsed = parse_structured_json(content, context="moment response")
    model = ensure_moment_contract()
    return model.model_validate(parsed).model_dump()


__all__ = [
    "MOMENTS_CONTRACT",
    "MOMENTS_MAX_SECONDS",
    "MOMENTS_MIN_SECONDS",
    "MOMENTS_PADDING_SECONDS",
    "MOMENTS_FRAMING_PROFILE",
    "MOMENTS_PIPELINE_VERSION",
    "MOMENTS_PROMPT_VERSION",
    "candidate_id",
    "candidate_matrix_key",
    "duration_bucket",
    "ensure_moment_contract",
    "normalize_quote_candidate",
    "normalize_decision_candidate",
    "parse_transcript_segments",
    "quote_safety_gate",
    "response_payload",
    "recipe_hash",
    "transcript_region",
]
