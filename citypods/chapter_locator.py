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

LOCATOR_CONTRACT = "agenda-chapter-locate"
# Mistral's current API/dispatch alias for Mistral Large 3. The dated API ID
# ``mistral-large-2512`` is useful for external audit, but the paced Worker advertises this
# maintained alias and accepts the provider-qualified form below.
MISTRAL_LOCATOR_MODEL = "mistral/mistral-large-latest"
GEMINI_LOCATOR_MODEL = "gemini/gemini-3-flash-preview"
# Mistral Large 3 accepts 256k total tokens. Reserve enough output room for a long chapter list
# plus one structured-output repair. Gemini's documented 1M input window has the same reserve.
MISTRAL_CONTEXT_TOKENS = 256_000
GEMINI_CONTEXT_TOKENS = 1_048_576
LOCATOR_OUTPUT_TOKEN_RESERVE = 16_384


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
    """A complete shadow-job payload and its deterministic model route."""

    messages: tuple[dict[str, str], ...]
    model: str
    input_tokens: int


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
    from pydantic import BaseModel, ConfigDict, Field

    from citypods.compute.structured import register_response_model, response_model

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

    model = getattr(ensure_locator_contract, "model", None)
    if model is None:
        model = Response
        register_response_model(LOCATOR_CONTRACT, model)
        ensure_locator_contract.model = model
    else:
        try:
            response_model(LOCATOR_CONTRACT)
        except ValueError:
            register_response_model(LOCATOR_CONTRACT, model)
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
    response = model.model_validate_json(content)
    units_by_id = {unit.id: unit for unit in units}
    anchors: list[LocatorAnchor] = []
    seen_agenda: set[int] = set()
    seen_units: set[str] = set()
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
            raise ValueError(f"duplicate locator unit: {item.unit_id}")
        seen_agenda.add(item.agenda_item_index)
        seen_units.add(item.unit_id)
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
    if any(
        left.unit.start >= right.unit.start
        for left, right in zip(anchors, anchors[1:], strict=False)
    ):
        raise ValueError("locator anchors must resolve to strictly increasing timestamps")
    return anchors


def _format_timestamp(seconds: float) -> str:
    total_ms = round(seconds * 1000)
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{milliseconds:03d}"


def select_locator_model(input_tokens: int) -> str:
    """Choose Mistral by default, escalating only for a measured context overflow.

    The estimate must include the instruction and response reserve, since provider context limits
    include every input and output token. Nothing is truncated: an over-limit request is an
    explicit non-admission until a future long-context/chunking evaluation decides otherwise.
    """
    if input_tokens < 0:
        raise ValueError("input_tokens must be non-negative")
    reserved_tokens = input_tokens + LOCATOR_OUTPUT_TOKEN_RESERVE
    if reserved_tokens <= MISTRAL_CONTEXT_TOKENS:
        return MISTRAL_LOCATOR_MODEL
    if reserved_tokens <= GEMINI_CONTEXT_TOKENS:
        return GEMINI_LOCATOR_MODEL
    raise ValueError(
        "locator payload exceeds the Gemini context budget "
        f"({reserved_tokens} > {GEMINI_CONTEXT_TOKENS} tokens)"
    )


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

    messages = (
        {
            "role": "system",
            "content": system_content,
        },
        {
            "role": "user",
            "content": json.dumps(material, ensure_ascii=False, separators=(",", ":")),
        },
    )
    from citypods.compute.llm_policy import estimate_tokens

    input_tokens = estimate_tokens(list(messages))
    return LocatorRequest(
        messages=messages,
        model=select_locator_model(input_tokens),
        input_tokens=input_tokens,
    )
