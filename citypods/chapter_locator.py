"""Prepare time-anchored transcript units for generated agenda chapter evaluation.

This module deliberately contains no model client and does not mutate :class:`Episode` records.
It makes the existing ASR word sidecar (or, when unavailable, the public VTT) into a compact,
stable request contract: an LLM may select a unit ID, but never invent a timestamp.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from citypods.compute.llm_lanes import lane_for
from citypods.compute.structured import parse_structured_json

LOCATOR_CONTRACT = "agenda-chapter-locate"
# Mistral's current API/dispatch alias for Mistral Large 3. The dated API ID
# ``mistral-large-2512`` is useful for external audit, but the paced Worker advertises this
# maintained alias and accepts the provider-qualified form below.
MISTRAL_LOCATOR_MODEL = "mistral/mistral-large-2512"
# Free-tier peers of MISTRAL_LOCATOR_MODEL (hotfix follow-up: Mistral's account-wide monthly
# budget is exhausted, see config/provider_limits.yml). These are catalog `model` keys from
# citypods/compute/llm_routes.json -- the scheduler pools every physical route sharing one of
# these keys and matches `allowed_models` against them, so an unqualified/provider-prefixed name
# would silently match nothing. `deepseek/deepseek-v4-flash` pools NVIDIA and OrcaRouter free
# routes; OpenCode's former free DeepSeek alias is no longer cataloged.
DEEPSEEK_FREE_LOCATOR_MODEL = "deepseek/deepseek-v4-flash"
NEMOTRON_LOCATOR_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
GEMINI_LOCATOR_MODEL = "gemini/gemini-3-flash-preview"
# Production uses the baseline prompt. DeepSeek V4 Flash is the preferred route for packets in its
# empirically verified fit band; MiniMax M3.1 Flash handles larger packets up to
# LOCATOR_OVERFLOW_MAX_INPUT_TOKENS (review/58 WP8). Keep these in the lane registry and select one
# per request so the Worker reserves only that model's pool.
#
# THIS STRING IS PART OF THE RECIPE HASH (see `stages.py`'s `chapter_locator` recipe), so changing
# the configured model re-queues every locator artifact in the catalog -- a deliberate backfill
# whose story the PR and CHANGELOG must state per AGENTS.md, not a config tweak.
# `tests/test_llm_lanes.py` pins the primary (DeepSeek) route.
#
# The `_LOCATOR_MODEL_BANDS` ladder below is a SEPARATE concern: it picks a fallback candidate set
# by request size for the shadow/experimental locator path, and is not the production lane.
PRODUCTION_LOCATOR_MODEL = lane_for("chapter-locator").primary_model
PRODUCTION_LOCATOR_MODELS = lane_for("chapter-locator").models
KIMI_LOCATOR_MODEL = "moonshotai/kimi-k3"
# NVIDIA retired Kimi K3 on 2026-10-15 (HTTP 410). DeepSeek V4.1 Flash then served the overflow
# band alone until review/58 WP8 (2026-11-11) removed it from the lane: about 5% of its NVIDIA
# calls succeeded. Results it already produced stay reusable (LOCATOR_RETIRED_REUSABLE_MODELS).
DEEPSEEK_V41_LOCATOR_MODEL = "deepseek/deepseek-v4.1-flash"
# review/58 WP8 overflow model (owner decision 2, 2026-11-11): 88.0% on the 12 long meetings with
# 12 of 12 structurally valid, baseline prompt. Its one route is NVIDIA's, at 1 RPM, one call at
# a time, sharing NVIDIA's provider slots.
MINIMAX_LOCATOR_MODEL = "minimaxai/minimax-m3.1-flash"
# Both size limits below are in this repository's ``estimate_tokens`` units (chars/4 over the
# request messages, as ``build_locator_request(...).input_tokens`` reports), not provider tokens.
#
# Saved V4 Flash telemetry accepted packets through 76,546 internal estimated tokens and
# repeatedly rejected packets from 81,301. Keep a small margin below the observed accepted edge.
DEEPSEEK_V4_FLASH_SAFE_INPUT_TOKENS = 76_000
# Largest packet sent to MINIMAX_LOCATOR_MODEL. A larger packet is never submitted: the producer
# holds it as deferred (``locator-over-input-cap``), and it becomes eligible on the first run after
# this cap is raised. Set from the WP8a input-size probe (2026-11-11): MiniMax admitted 941,663
# provider tokens (610,831 estimated) and rejected a 709,545-estimate packet with HTTP 400; the
# provider/estimate ratio ran 1.40 to 1.59, so 500,000 estimated is about 795,000 provider tokens
# at the worst ratio. Accuracy above 191,294 estimated tokens is unmeasured (no gold labels), and
# two of five probe answers above it were structurally invalid; lower this one value (for example
# to 300,000, below which both probe answers were valid) if production quality disappoints.
LOCATOR_OVERFLOW_MAX_INPUT_TOKENS = 500_000
# Deliberately NOT bumped for WP8. This string is part of every locator recipe hash and of the
# reuse check on completed results, so bumping it would re-queue every accepted V4 Flash result in
# the catalog. A request's model is already in its recipe hash, so moving the overflow band to a
# new model gives those packets new recipes without touching the V4 Flash band's.
LOCATOR_ROUTING_VERSION = "size-split-v1"
# Models no longer in the lane whose COMPLETED results, produced under the current prompt and
# routing version, are still reused rather than re-located. Pending work on them is not kept: the
# producer cancels a queued job naming one and re-plans it on the lane as configured now.
LOCATOR_RETIRED_REUSABLE_MODELS: tuple[str, ...] = (DEEPSEEK_V41_LOCATOR_MODEL,)
# Mistral Large 3 accepts 256k total tokens. Reserve enough output room for a long chapter list
# plus one structured-output repair. Gemini's documented 1M input window has the same reserve.
MISTRAL_CONTEXT_TOKENS = 256_000
DEEPSEEK_FREE_CONTEXT_TOKENS = 200_000
NEMOTRON_CONTEXT_TOKENS = 1_000_000
GEMINI_CONTEXT_TOKENS = 1_048_576
LOCATOR_OUTPUT_TOKEN_RESERVE = 20_000
# Research-only shadow selection bands. Production uses the explicit empirically-derived split
# below; this ladder remains for old shadow/evaluation callers.
_LOCATOR_MODEL_BANDS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        DEEPSEEK_FREE_CONTEXT_TOKENS,
        (MISTRAL_LOCATOR_MODEL, DEEPSEEK_FREE_LOCATOR_MODEL, NEMOTRON_LOCATOR_MODEL),
    ),
    (MISTRAL_CONTEXT_TOKENS, (MISTRAL_LOCATOR_MODEL, NEMOTRON_LOCATOR_MODEL)),
    (NEMOTRON_CONTEXT_TOKENS, (NEMOTRON_LOCATOR_MODEL,)),
    (GEMINI_CONTEXT_TOKENS, (GEMINI_LOCATOR_MODEL,)),
)


@dataclass(frozen=True)
class LocatorUnit:
    """One ephemeral, time-anchored transcript unit supplied to a chapter locator.

    ``id`` is deterministic within a request and maps directly to ``start``.  It is not an
    Episode identifier and is intentionally never persisted as a transcript annotation.
    """

    id: str
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class LocatorAnchor:
    """A validated model choice mapped to a supplied locator unit.

    The list returned by :func:`validate_locator_response` is sorted by observed time. The
    original agenda index is retained, so an out-of-order meeting is represented rather than
    incorrectly forced back into agenda order.
    """

    agenda_item_index: int
    unit: LocatorUnit
    transition_quote: str
    confidence: float
    rationale: str
    item_confidence: float | None = None
    boundary_confidence: float | None = None
    evidence_type: str | None = None
    alternative_agenda_item_index: int | None = None
    alternative_unit_id: str | None = None
    uncertainty_reason: str | None = None


@dataclass(frozen=True)
class LocatorAgendaItem:
    """One generated display label plus immutable agenda evidence for transcript retrieval."""

    index: int
    title: str
    evidence_text: str
    locator_cues: tuple[str, ...] = ()
    display_ref: str | None = None


@dataclass(frozen=True)
class LocatorRequest:
    """A complete shadow-job payload and its candidate model route(s).

    ``model`` is the primary/first candidate -- unchanged contract for callers (mostly the
    research scripts under scripts/research/agenda_chapters/) that build a direct single-model
    backend from it, several of which construct ``LocatorRequest`` directly without ``models``.
    ``models`` (additive) carries the full same-priority candidate band for a caller that wants to
    build an ``LLMRequestPolicy(allowed_models=...)`` instead; it defaults to ``(model,)`` when
    omitted, so every existing single-model construction site stays valid.
    """

    messages: tuple[dict[str, str], ...]
    model: str
    input_tokens: int
    models: tuple[str, ...] = ()
    # The request's ``max_tokens``: the output reserve, clamped to the largest output limit among
    # the selected model's routes so the Worker has a route that can take the job.
    max_output_tokens: int = LOCATOR_OUTPUT_TOKEN_RESERVE

    def __post_init__(self) -> None:
        if not self.models:
            object.__setattr__(self, "models", (self.model,))


class LocatorInputOverCapError(ValueError):
    """A production locator packet larger than ``LOCATOR_OVERFLOW_MAX_INPUT_TOKENS``.

    Not a failure: the producer holds the episode as deferred, submits nothing, and counts it, so
    the packet waits for a raised cap or the split-packet design (GH#2651).
    """

    def __init__(self, input_tokens: int, cap: int) -> None:
        super().__init__(
            f"locator packet of {input_tokens} estimated input tokens is above the "
            f"{cap}-token overflow cap; held as deferred"
        )
        self.input_tokens = input_tokens
        self.cap = cap


_VTT_TIMING = re.compile(
    r"(?P<start>\d{2}:\d{2}:\d{2}\.\d{3})\s+-->\s+"
    r"(?P<end>\d{2}:\d{2}:\d{2}\.\d{3})(?:\s+.*)?$"
)


def _timestamp_seconds(value: str) -> float:
    hours, minutes, rest = value.split(":")
    seconds, milliseconds = rest.split(".")
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(milliseconds) / 1000


def _unit_rows(segments: object) -> list[tuple[float, float, str]]:
    """Validate the small shared segment shape emitted by ASR and accepted from VTT."""
    if not isinstance(segments, list):
        return []
    rows: list[tuple[float, float, str]] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        text = str(segment.get("text") or "").strip()
        try:
            start = float(segment["start"])
            end = float(segment["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if text and start >= 0 and end >= start:
            rows.append((start, end, text))
    return rows


def _number_units(rows: list[tuple[float, float, str]]) -> list[LocatorUnit]:
    """Give chronological rows stable, request-local identifiers."""
    return [
        LocatorUnit(id=f"u{index:05d}", start=start, end=end, text=text)
        for index, (start, end, text) in enumerate(rows, start=1)
    ]


def locator_units_from_words(data: bytes | None) -> list[LocatorUnit]:
    """Return ASR segment units from a word-sidecar payload, or an empty list if malformed.

    The word sidecar's segment text preserves readable utterance-sized context; individual words
    would make a needlessly huge model request.  Its per-word offsets remain available for a later
    refinement without changing this request contract.
    """
    if not data:
        return []
    try:
        payload: Any = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    return _number_units(_unit_rows(payload.get("segments")))


def locator_units_from_vtt(data: bytes | None) -> list[LocatorUnit]:
    """Parse readable VTT cues into locator units without depending on a new parser package."""
    if not data:
        return []
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
    rows: list[tuple[float, float, str]] = []
    for block in text.split("\n\n"):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        match = _VTT_TIMING.fullmatch(lines[timing_index])
        if match is None:
            continue
        cue_text = " ".join(lines[timing_index + 1 :]).strip()
        if not cue_text:
            continue
        rows.append(
            (
                _timestamp_seconds(match.group("start")),
                _timestamp_seconds(match.group("end")),
                cue_text,
            )
        )
    return _number_units(rows)


def build_locator_units(
    *, words_data: bytes | None, vtt_data: bytes | None
) -> tuple[list[LocatorUnit], str | None]:
    """Prefer ASR word-sidecar segments, falling back to VTT cues.

    Returns the units and their source (``"words"`` or ``"vtt"``).  An empty result is an
    intentional non-admission signal for the future stage, not an exception or guessed timing.
    """
    words = locator_units_from_words(words_data)
    if words:
        return words, "words"
    vtt = locator_units_from_vtt(vtt_data)
    return (vtt, "vtt") if vtt else ([], None)


def ensure_locator_contract():
    """Register the structured locator response lazily and idempotently.

    A persisted dispatch handle names this contract, so a later reconciliation process needs this
    public helper before it can validate the returned response. This mirrors the existing tag
    contract without adding a task verb to the pre-1.0 compute interface.
    """
    from citypods.compute.structured import register_response_model, response_model

    cached = getattr(ensure_locator_contract, "model", None)
    if cached is not None:
        return cached

    try:
        model = response_model(LOCATOR_CONTRACT)
        ensure_locator_contract.model = model
        return model
    except ValueError:
        pass

    from pydantic import BaseModel, ConfigDict, Field

    class Anchor(BaseModel):
        model_config = ConfigDict(extra="forbid")
        agenda_item_index: int = Field(ge=0)
        unit_id: str = Field(pattern=r"^u\d{5}$")
        transition_quote: str = Field(min_length=3, max_length=500)
        confidence: float = Field(ge=0.0, le=1.0)
        rationale: str = Field(min_length=1, max_length=500)
        # Research-only calibration fields. Existing callers may omit them; calibrated shadow
        # prompts request all of them and preserve the values in the normalized artifact.
        item_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
        boundary_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
        evidence_type: str | None = Field(default=None, min_length=1, max_length=40)
        alternative_agenda_item_index: int | None = Field(default=None, ge=0)
        alternative_unit_id: str | None = Field(default=None, pattern=r"^u\d{5}$")
        uncertainty_reason: str | None = Field(default=None, max_length=300)

    class Response(BaseModel):
        model_config = ConfigDict(extra="forbid")
        anchors: list[Anchor] = Field(default_factory=list, max_length=200)

    model = register_response_model(LOCATOR_CONTRACT, Response)
    ensure_locator_contract.model = model
    return model


def validate_locator_response(
    content: str | bytes,
    *,
    agenda_item_count: int,
    units: Sequence[LocatorUnit],
) -> list[LocatorAnchor]:
    """Accept only model anchors that refer to supplied agenda items and units.

    Pydantic enforces the wire shape. The remaining checks are request-specific and cannot be
    expressed in the schema: a unit must be present in this request, each agenda item can produce
    at most one chapter, and emitted chapter timestamps must be strictly increasing. We sort by
    time but deliberately do *not* require agenda indices to be monotonic.
    """
    if agenda_item_count < 1:
        raise ValueError("agenda_item_count must be positive")
    model = ensure_locator_contract()
    response = model.model_validate(
        parse_structured_json(content, context="chapter locator response")
    )
    units_by_id = {unit.id: unit for unit in units}
    anchors: list[LocatorAnchor] = []
    seen_agenda: set[int] = set()
    seen_units: dict[str, int] = {}
    for item in response.anchors:
        if item.agenda_item_index >= agenda_item_count:
            raise ValueError(f"agenda item index out of range: {item.agenda_item_index}")
        if (
            item.alternative_agenda_item_index is not None
            and item.alternative_agenda_item_index >= agenda_item_count
        ):
            raise ValueError(
                f"alternative agenda item index out of range: {item.alternative_agenda_item_index}"
            )
        if item.agenda_item_index in seen_agenda:
            raise ValueError(f"duplicate agenda item index: {item.agenda_item_index}")
        unit = units_by_id.get(item.unit_id)
        if unit is None:
            raise ValueError(f"unknown locator unit: {item.unit_id}")
        if item.alternative_unit_id is not None and item.alternative_unit_id not in units_by_id:
            raise ValueError(f"unknown alternative locator unit: {item.alternative_unit_id}")
        if item.unit_id in seen_units:
            # Name both agenda items: the same title listed twice (e.g. an agenda merged from
            # overlapping feeds) is a different cause than the model mis-anchoring distinct items.
            raise ValueError(
                f"duplicate locator unit: {item.unit_id} "
                f"(agenda items {seen_units[item.unit_id]} and {item.agenda_item_index})"
            )
        seen_agenda.add(item.agenda_item_index)
        seen_units[item.unit_id] = item.agenda_item_index
        anchors.append(
            LocatorAnchor(
                agenda_item_index=item.agenda_item_index,
                unit=unit,
                transition_quote=item.transition_quote,
                confidence=item.confidence,
                rationale=item.rationale,
                item_confidence=item.item_confidence,
                boundary_confidence=item.boundary_confidence,
                evidence_type=item.evidence_type,
                alternative_agenda_item_index=item.alternative_agenda_item_index,
                alternative_unit_id=item.alternative_unit_id,
                uncertainty_reason=item.uncertainty_reason,
            )
        )
    anchors.sort(key=lambda anchor: (anchor.unit.start, anchor.unit.id))
    for left, right in zip(anchors, anchors[1:], strict=False):
        if left.unit.start == right.unit.start:
            raise DuplicateLocatorStartError(
                (left.agenda_item_index, right.agenda_item_index), left.unit.start
            )
        if left.unit.start > right.unit.start:
            raise ValueError("locator anchors must resolve to strictly increasing timestamps")
    return anchors


class DuplicateLocatorStartError(ValueError):
    """Two distinct agenda items resolved to the same transcript start time."""

    def __init__(self, agenda_item_indices: tuple[int, int], start: float) -> None:
        self.agenda_item_indices = agenda_item_indices
        self.start = start
        super().__init__(
            "locator anchors share a start time for agenda items "
            f"{agenda_item_indices[0]} and {agenda_item_indices[1]} ({start:.3f}s)"
        )


def _format_timestamp(seconds: float) -> str:
    total_ms = round(seconds * 1000)
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{milliseconds:03d}"


def select_locator_models(input_tokens: int) -> tuple[str, ...]:
    """Return every same-priority locator model that can hold this request.

    The estimate must include the instruction and response reserve, since provider context limits
    include every input and output token. Nothing is truncated: an over-limit request is an
    explicit non-admission until a future long-context/chunking evaluation decides otherwise.
    Candidates are drawn from ``_LOCATOR_MODEL_BANDS``, the narrowest band the request fits in --
    Mistral is escalated past, not preferred within a band; a caller free to pick among the
    returned set (e.g. via ``LLMRequestPolicy.allowed_models``) should let real availability decide.
    """
    if input_tokens < 0:
        raise ValueError("input_tokens must be non-negative")
    reserved_tokens = input_tokens + LOCATOR_OUTPUT_TOKEN_RESERVE
    for ceiling, candidates in _LOCATOR_MODEL_BANDS:
        if reserved_tokens <= ceiling:
            return candidates
    raise ValueError(
        "locator payload exceeds the Gemini context budget "
        f"({reserved_tokens} > {GEMINI_CONTEXT_TOKENS} tokens)"
    )


def select_locator_model(input_tokens: int) -> str:
    """Backward-compatible single-model selector: the primary candidate for this request size."""
    return select_locator_models(input_tokens)[0]


def select_production_locator_models(input_tokens: int) -> tuple[str, ...]:
    """The production lane model for a packet of ``input_tokens`` (``estimate_tokens`` units).

    Up to DEEPSEEK_V4_FLASH_SAFE_INPUT_TOKENS: DeepSeek V4 Flash, the lane's primary. Above it, up
    to LOCATOR_OVERFLOW_MAX_INPUT_TOKENS: MiniMax M3.1 Flash. Above the cap nothing is selected:
    :class:`LocatorInputOverCapError` tells the producer to hold the packet as deferred.
    """
    if input_tokens < 0:
        raise ValueError("input_tokens must be non-negative")
    if input_tokens <= DEEPSEEK_V4_FLASH_SAFE_INPUT_TOKENS:
        return (PRODUCTION_LOCATOR_MODEL,)
    if input_tokens <= LOCATOR_OVERFLOW_MAX_INPUT_TOKENS:
        return (MINIMAX_LOCATOR_MODEL,)
    raise LocatorInputOverCapError(input_tokens, LOCATOR_OVERFLOW_MAX_INPUT_TOKENS)


def locator_max_output_tokens(model: str) -> int:
    """``max_tokens`` for a locator request on ``model``.

    The output reserve, clamped to the largest ``output_context_limit`` among the model's
    compiled routes: the Worker indexes a job only on routes whose output limit holds its
    ``max_tokens``, so a reserve above every route's limit would leave the job unroutable.
    DeepSeek V4 Flash keeps the full reserve (its OrcaRouter route allows 128,000); MiniMax M3.1
    Flash is clamped to its route's 12,800.
    """
    from citypods.compute.llm_policy import ROUTE_CANDIDATES, canonical_model

    limits = [
        route.output_context_limit
        for route in ROUTE_CANDIDATES.get(canonical_model(model), ())
        if route.output_context_limit
    ]
    if not limits:
        return LOCATOR_OUTPUT_TOKEN_RESERVE
    return min(LOCATOR_OUTPUT_TOKEN_RESERVE, max(limits))


def build_locator_request(
    agenda_items: Sequence[LocatorAgendaItem],
    units: Sequence[LocatorUnit],
    *,
    unit_annotations: Mapping[str, Mapping[str, Any]] | None = None,
) -> LocatorRequest:
    """Build the full-context, main-agenda-only shadow request and select its route.

    This keeps deterministic candidate scoring out of the admission path: all supplied units are
    visible to the model. The caller passes titles already extracted from the main agenda; backup
    materials are intentionally excluded. ``unit_annotations`` is an optional research aid for
    comparing pooled candidate packets with packets that retain per-unit retrieval provenance; it
    is omitted from ordinary requests.
    """
    messages = _locator_messages(agenda_items, units, unit_annotations=unit_annotations)
    from citypods.compute.llm_policy import estimate_tokens

    input_tokens = estimate_tokens(list(messages))
    models = select_locator_models(input_tokens)
    return LocatorRequest(
        messages=messages,
        model=models[0],
        models=models,
        input_tokens=input_tokens,
    )


def _locator_messages(
    agenda_items: Sequence[LocatorAgendaItem],
    units: Sequence[LocatorUnit],
    *,
    unit_annotations: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[dict[str, str], ...]:
    """Validate the inputs and build the baseline locator messages (shared by both builders)."""
    if not agenda_items:
        raise ValueError("locator requires at least one agenda item")
    if not units:
        raise ValueError("locator requires at least one timed transcript unit")
    indices = [item.index for item in agenda_items]
    if any(index < 0 for index in indices) or len(indices) != len(set(indices)):
        raise ValueError("agenda item indices must be unique non-negative integers")
    if any(not item.title.strip() for item in agenda_items):
        raise ValueError("agenda item titles must be non-empty")
    if any(not item.evidence_text.strip() for item in agenda_items):
        raise ValueError("agenda item evidence text must be non-empty")
    if any(any(not cue.strip() for cue in item.locator_cues) for item in agenda_items):
        raise ValueError("agenda item locator cues must be non-empty when supplied")
    if len({unit.id for unit in units}) != len(units):
        raise ValueError("locator unit IDs must be unique")

    transcript_units = []
    for unit in units:
        payload: dict[str, Any] = {
            "id": unit.id,
            "start": _format_timestamp(unit.start),
            "end": _format_timestamp(unit.end),
            "text": unit.text,
        }
        if unit_annotations is not None:
            annotation = unit_annotations.get(unit.id)
            if annotation:
                payload["retrieval_provenance"] = annotation
        transcript_units.append(payload)

    material = {
        "agenda_items": [
            {
                "index": item.index,
                "title": item.title.strip(),
                "display_ref": item.display_ref,
                "evidence_text": item.evidence_text.strip(),
                "locator_cues": [cue.strip() for cue in item.locator_cues],
            }
            for item in agenda_items
        ],
        "transcript_units": transcript_units,
    }
    system_content = (
        "Locate agenda-item discussion starts in this public meeting transcript. "
        "Return an anchor only for an item actually discussed. Select unit_id exactly "
        "from transcript_units; never invent a timestamp or ID. A transition quote must "
        "be copied from the selected or immediately adjacent unit. Agenda order is only "
        "a soft prior: items can be skipped, reordered, or revisited. Do not use the "
        "generated agenda title as evidence unless it is spoken in the supplied "
        "transcript. "
        "Use each item's source-grounded evidence_text and locator_cues to recognize "
        "announcements, including an item number or ID that is spoken without its summary."
    )
    if unit_annotations:
        system_content += (
            " Some transcript units include retrieval_provenance. Its candidate_for entries "
            "are [agenda_item_index, ranks, signals]; rank keys L/T/H mean lexical, TF-IDF, "
            "and learned retrieval, while signals L/T/H mean direct top candidates and l/t "
            "mean neighboring candidates. Treat these as retrieval hints, not proof: use them "
            "to preserve item-to-unit associations while checking transcript text and timing."
        )

    return (
        {
            "role": "system",
            "content": system_content,
        },
        {
            "role": "user",
            "content": json.dumps(material, ensure_ascii=False, separators=(",", ":")),
        },
    )


def build_production_locator_request(
    agenda_items: Sequence[LocatorAgendaItem],
    units: Sequence[LocatorUnit],
    *,
    unit_annotations: Mapping[str, Mapping[str, Any]] | None = None,
    retry_hint: str | None = None,
) -> LocatorRequest:
    """Build a baseline-prompt request on the size-selected production route.

    The complete unit list is always retained.  Annotations are optional research provenance and
    never replace or filter transcript units. The size is the same ``estimate_tokens`` figure
    :func:`build_locator_request` reports, plus any retry hint. A packet above
    LOCATOR_OVERFLOW_MAX_INPUT_TOKENS raises :class:`LocatorInputOverCapError`; it is not
    truncated.
    """
    from citypods.compute.llm_policy import estimate_tokens

    messages = _locator_messages(agenda_items, units, unit_annotations=unit_annotations)
    if retry_hint:
        messages = (*messages, {"role": "user", "content": retry_hint})
    input_tokens = estimate_tokens(list(messages))
    models = select_production_locator_models(input_tokens)
    return LocatorRequest(
        messages=messages,
        model=models[0],
        models=models,
        input_tokens=input_tokens,
        max_output_tokens=locator_max_output_tokens(models[0]),
    )


__all__ = [
    "DEEPSEEK_FREE_CONTEXT_TOKENS",
    "DEEPSEEK_FREE_LOCATOR_MODEL",
    "DEEPSEEK_V4_FLASH_SAFE_INPUT_TOKENS",
    "DuplicateLocatorStartError",
    "GEMINI_CONTEXT_TOKENS",
    "GEMINI_LOCATOR_MODEL",
    "LOCATOR_CONTRACT",
    "LOCATOR_OUTPUT_TOKEN_RESERVE",
    "LOCATOR_OVERFLOW_MAX_INPUT_TOKENS",
    "LOCATOR_RETIRED_REUSABLE_MODELS",
    "LOCATOR_ROUTING_VERSION",
    "DEEPSEEK_V41_LOCATOR_MODEL",
    "KIMI_LOCATOR_MODEL",
    "LocatorAgendaItem",
    "LocatorAnchor",
    "LocatorInputOverCapError",
    "LocatorRequest",
    "LocatorUnit",
    "MISTRAL_CONTEXT_TOKENS",
    "MINIMAX_LOCATOR_MODEL",
    "MISTRAL_LOCATOR_MODEL",
    "NEMOTRON_CONTEXT_TOKENS",
    "NEMOTRON_LOCATOR_MODEL",
    "PRODUCTION_LOCATOR_MODEL",
    "PRODUCTION_LOCATOR_MODELS",
    "build_locator_request",
    "build_locator_units",
    "build_production_locator_request",
    "ensure_locator_contract",
    "locator_max_output_tokens",
    "locator_units_from_vtt",
    "locator_units_from_words",
    "select_locator_model",
    "select_locator_models",
    "select_production_locator_models",
    "validate_locator_response",
]
