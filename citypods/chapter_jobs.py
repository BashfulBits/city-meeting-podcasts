"""Production job builders/finalizers for agenda extraction and chapter locating."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from citypods.chapter_artifacts import (
    AgendaCandidate,
    AgendaCandidatesArtifact,
    BoundaryResultArtifact,
    recipe_hash,
)
from citypods.chapter_locator import (
    PRODUCTION_LOCATOR_MODEL,
    LocatorAgendaItem,
    LocatorUnit,
    build_production_locator_request,
    validate_locator_response,
)
from citypods.chapter_titles import (
    AGENDA_ITEM_EXTRACTOR_CONTRACT,
    AGENDA_PRODUCTION_MODEL,
    AGENDA_PRODUCTION_MODELS,
    build_production_agenda_item_extraction_request,
    ensure_agenda_item_extractor_contract,
    validate_agenda_item_extractor_response,
)
from citypods.compute.base import InferenceJob, JobResult
from citypods.compute.llm import TASK_VERSIONS
from citypods.compute.llm_policy import LLMRequestPolicy

# chapter_locator.py is the single source of truth for the production locator model name.
LOCATOR_MODEL = PRODUCTION_LOCATOR_MODEL
# Prompt variant used for all production agenda extraction jobs.
AGENDA_PROMPT_VERSION = "agenda-flow"
LOCATOR_PROMPT_VERSION = "locator-v1"


def _locator_cues(display_ref: str | None, evidence_text: str) -> tuple[str, ...]:
    """Derive compact, immutable agenda phrases from the validated source span."""

    values: list[str] = []
    if display_ref:
        values.append(display_ref)
    for line in evidence_text.splitlines():
        normalized = re.sub(r"\s+", " ", line).strip()
        if normalized:
            values.append(normalized)
    return tuple(dict.fromkeys(values))


def _response_content(output: Any) -> str:
    """Extract the assistant JSON content from a provider-neutral completion response."""

    if isinstance(output, Mapping):
        choices = output.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
            message = choices[0].get("message")
            if isinstance(message, Mapping) and isinstance(message.get("content"), str):
                return message["content"]
    if isinstance(output, str):
        return output
    raise ValueError("LLM job output did not contain assistant content")


def build_agenda_job(
    *,
    episode_uid: str,
    agenda_text: str,
    agenda_source_hash: str,
    candidate_hints: Sequence[Mapping[str, object]] = (),
) -> InferenceJob:
    request = build_production_agenda_item_extraction_request(
        agenda_text, candidate_hints=candidate_hints
    )
    recipe_parts: dict[str, Any] = {
        "task": "agenda-item-extract",
        "task_version": TASK_VERSIONS["agenda-item-extract"],
        "episode_uid": episode_uid,
        "source_hash": agenda_source_hash,
        "model": AGENDA_PRODUCTION_MODEL,
        "prompt_version": AGENDA_PROMPT_VERSION,
    }
    if candidate_hints:
        recipe_parts["candidate_hints"] = [dict(h) for h in candidate_hints]
    recipe = recipe_hash(**recipe_parts)
    ensure_agenda_item_extractor_contract()
    return InferenceJob(
        task="agenda-item-extract",
        recipe_hash=recipe,
        inputs={
            "messages": list(request.messages),
            "structured_output": AGENDA_ITEM_EXTRACTOR_CONTRACT,
            "llm_policy": LLMRequestPolicy(
                allowed_models=AGENDA_PRODUCTION_MODELS,
                purpose="chapter-agenda",
                # This stage persists its pending recipe and finalizes on a later chapter-lane
                # pass, so Worker-owned queueing is safer than a runner-side deadline.
                queue_only=True,
            ),
        },
    )


def finalize_agenda_job(
    result: JobResult,
    *,
    episode_uid: str,
    agenda_text: str,
    agenda_source_hash: str,
    model: str | None = None,
) -> AgendaCandidatesArtifact:
    """Validate a completed agenda response and build its durable source-backed artifact.

    ``model`` defaults to ``result.model`` -- the model the scheduler actually dispatched to, now
    that ``AGENDA_PRODUCTION_MODELS`` (R13) offers more than one same-priority candidate.  Falls
    back to ``AGENDA_PRODUCTION_MODEL`` only for a caller/backend that never set ``result.model``.
    """
    resolved_model = model or result.model or AGENDA_PRODUCTION_MODEL

    content = _response_content(result.output)
    items = validate_agenda_item_extractor_response(content, agenda_text=agenda_text)
    lines = agenda_text.splitlines()
    candidates: list[AgendaCandidate] = []
    for index, item in enumerate(items):
        evidence_text = "\n".join(lines[item.line_start - 1 : item.line_end]).strip()
        cues = _locator_cues(item.display_ref, evidence_text)
        candidates.append(
            AgendaCandidate(
                index=index,
                title=item.title,
                kind="substantive_action",
                line_start=item.line_start,
                line_end=item.line_end,
                evidence_text=evidence_text,
                locator_cues=cues,
                display_ref=item.display_ref,
                evidence_quote=item.evidence_quote,
            )
        )
    return AgendaCandidatesArtifact(
        episode_uid=episode_uid,
        source_hash=agenda_source_hash,
        model=resolved_model,
        prompt_version=AGENDA_PROMPT_VERSION,
        recipe=result.recipe_hash,
        items=tuple(candidates),
        diagnostics={"source_line_count": len(lines)},
    )


def build_locator_job(
    *,
    episode_uid: str,
    agenda: AgendaCandidatesArtifact,
    transcript_hash: str,
    units: Sequence[LocatorUnit],
    unit_annotations: Mapping[str, Mapping[str, Any]] | None = None,
) -> InferenceJob:
    locator_items = [
        LocatorAgendaItem(
            index=item.index,
            title=item.title,
            evidence_text=item.evidence_text,
            locator_cues=item.locator_cues,
            display_ref=item.display_ref,
        )
        for item in agenda.items
        if item.status == "accepted"
    ]
    request = build_production_locator_request(
        locator_items, units, unit_annotations=unit_annotations
    )
    hint_mode = "none" if not unit_annotations else "research"
    recipe_parts: dict[str, Any] = {
        "task": "agenda-chapter-locate",
        "task_version": TASK_VERSIONS["agenda-chapter-locate"],
        "episode_uid": episode_uid,
        "agenda_recipe": agenda.recipe,
        "transcript_hash": transcript_hash,
        "model": LOCATOR_MODEL,
        "prompt_version": LOCATOR_PROMPT_VERSION,
        "hint_mode": hint_mode,
    }
    if unit_annotations:
        recipe_parts["unit_annotations"] = {k: dict(v) for k, v in unit_annotations.items()}
    recipe = recipe_hash(**recipe_parts)
    from citypods.chapter_locator import LOCATOR_CONTRACT, ensure_locator_contract

    ensure_locator_contract()
    return InferenceJob(
        task="agenda-chapter-locate",
        recipe_hash=recipe,
        inputs={
            "messages": list(request.messages),
            "structured_output": LOCATOR_CONTRACT,
            "llm_policy": LLMRequestPolicy(
                allowed_models=(LOCATOR_MODEL,),
                purpose="chapter-locator",
                queue_only=True,
            ),
        },
    )


def finalize_locator_job(
    result: JobResult,
    *,
    episode_uid: str,
    agenda: AgendaCandidatesArtifact,
    transcript_hash: str,
    units: Sequence[LocatorUnit],
) -> BoundaryResultArtifact:
    """Validate unit references and map selected units to durable boundary records."""

    content = _response_content(result.output)
    anchors = validate_locator_response(content, agenda_item_count=len(agenda.items), units=units)
    normalized = []
    for anchor in anchors:
        normalized.append(
            {
                "agenda_item_index": anchor.agenda_item_index,
                "unit_id": anchor.unit.id,
                "start": anchor.unit.start,
                "end": anchor.unit.end,
                "basis": "served",
                "transition_quote": anchor.transition_quote,
                "confidence": anchor.confidence,
                "rationale": anchor.rationale,
                "item_confidence": anchor.item_confidence,
                "boundary_confidence": anchor.boundary_confidence,
                "evidence_type": anchor.evidence_type,
                "alternative_agenda_item_index": anchor.alternative_agenda_item_index,
                "alternative_unit_id": anchor.alternative_unit_id,
                "uncertainty_reason": anchor.uncertainty_reason,
            }
        )
    return BoundaryResultArtifact(
        episode_uid=episode_uid,
        agenda_recipe=agenda.recipe,
        transcript_hash=transcript_hash,
        model=LOCATOR_MODEL,
        prompt_version=LOCATOR_PROMPT_VERSION,
        recipe=result.recipe_hash,
        anchors=tuple(normalized),
        diagnostics={"unit_count": len(units), "agenda_item_count": len(agenda.items)},
    )


__all__ = [
    "AGENDA_PROMPT_VERSION",
    "LOCATOR_MODEL",
    "LOCATOR_PROMPT_VERSION",
    "build_agenda_job",
    "build_locator_job",
    "finalize_agenda_job",
    "finalize_locator_job",
]
