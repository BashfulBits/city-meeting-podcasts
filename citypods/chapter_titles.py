"""Prepare source-anchored agenda-item extraction experiments for generated chapters.

The extractor is deliberately shadow-only: it neither calls an LLM nor mutates an Episode.  A
model may recognize agenda action items directly from the full source text, but every returned
reference and evidence quote must be verbatim within model-cited source lines. Generated titles
remain separately marked as generated provenance. This avoids making a brittle deterministic
heading parser the gate for a capable model while retaining auditability.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from citypods.agenda_text import AgendaTitleCandidate, agenda_title_similarity
from citypods.chapter_locator import select_locator_model
from citypods.compute.llm_policy import estimate_tokens

AGENDA_ITEM_EXTRACTOR_CONTRACT = "agenda-chapter-item-extract"
TITLE_EQUIVALENCE_CONTRACT = "agenda-chapter-title-equivalence"

AGENDA_EXTRACTION_PROMPT_VARIANTS = frozenset(
    {
        "standard",
        "coverage-audit",
        "transcript-anchor",
        "hierarchy-first",
        "ambiguity-inclusion",
        "agenda-flow",
    }
)

_PROMPT_VARIANT_INSTRUCTIONS = {
    "standard": "",
    "coverage-audit": (
        " Work in two silent passes: first enumerate candidates across the whole agenda, then "
        "audit every source line and nested list for a distinct item you missed. Return only the "
        "final JSON, but favor complete source coverage over a short list."
    ),
    "transcript-anchor": (
        " This is a recall-oriented chapter-candidate pass. Include every distinct agenda segment "
        "that a chair, clerk, staff member, or applicant could plausibly announce, introduce, "
        "present, hear, vote on, or close in the meeting. A later transcript stage will reject "
        "items that were never reached, so do not omit a supported segment merely because it may "
        "be withdrawn or receive little discussion."
    ),
    "hierarchy-first": (
        " Traverse the agenda hierarchy completely: section, subsection, then each numbered, "
        "lettered, case, or ID-bearing action beneath it. A parent action is not a reason to skip "
        "its separately scheduled children. However, a clearly labeled Consent Agenda is one "
        "composite action: exclude its constituent child entries unless the agenda explicitly says "
        "one will be separately considered. Treat attachment lists, backup links, exhibits, and "
        "supporting-material entries as evidence for their numbered parent, not separate actions, "
        "unless independently scheduled for action."
    ),
    "ambiguity-inclusion": (
        " Optimize recall. When a source-grounded entry might reasonably be a meeting chapter "
        "rather than boilerplate, include it with its exact evidence; only exclude entries that "
        "are clearly document furniture, logistics, participation instructions, or attachments. "
        "Do not merge distinct supported agenda business merely to make the list shorter."
    ),
    "agenda-flow": (
        " Model the meeting's likely spoken flow from the agenda. Include distinct call-to-order, "
        "recognition, report, presentation, hearing, case, appointment, ordinance, resolution, "
        "discussion, vote, and adjournment segments when source-grounded. Do not stop after one "
        "section: continue through the complete agenda and use a separate item for each distinct "
        "business segment, subject to the Consent Agenda composite rule."
    ),
}


@dataclass(frozen=True)
class ExtractedAgendaItem:
    """A generated title tied to exact action evidence and optional display-reference provenance."""

    display_ref: str | None
    title: str
    evidence_quote: str
    line_start: int
    line_end: int
    evidence_span_repaired: bool


@dataclass(frozen=True)
class AgendaItemExtractionRequest:
    """A direct, source-line-addressable agenda-item extraction request."""

    source_line_count: int
    messages: tuple[dict[str, str], ...]
    model: str
    input_tokens: int


@dataclass(frozen=True)
class RejectedAgendaItem:
    """One structured item rejected by source-evidence validation in a shadow evaluation."""

    index: int
    display_ref: str | None
    reason: str


@dataclass(frozen=True)
class AgendaItemEvidenceAssessment:
    """Valid source-backed items and isolated validation failures from one model response."""

    items: tuple[ExtractedAgendaItem, ...]
    rejected: tuple[RejectedAgendaItem, ...]


@dataclass(frozen=True)
class TitleEquivalenceRequest:
    """A held-out semantic comparison request; never used to generate agenda items."""

    messages: tuple[dict[str, str], ...]
    model: str
    input_tokens: int


@dataclass(frozen=True)
class TitleEquivalenceMatch:
    """A one-to-one semantic match between generated and canonical title indices."""

    generated_index: int
    canonical_index: int


@dataclass(frozen=True)
class TitleEquivalenceResult:
    """Action denominator and semantic equivalences for benchmark scoring only."""

    canonical_action_indices: tuple[int, ...]
    matches: tuple[TitleEquivalenceMatch, ...]


def _normalized_source_text(value: str) -> str:
    """Normalize only presentation whitespace for verbatim-source containment checks."""
    return re.sub(r"\s+", " ", value).strip()


def _evidence_comparison_text(value: str) -> str:
    """Remove layout-extraction spacing artifacts without accepting paraphrased source text."""
    value = _normalized_source_text(value)
    # `pypdf` layout extraction may put a repeated page number / print timestamp / document header
    # between two visual lines of one agenda item. It is presentation furniture, not item evidence.
    value = re.sub(
        r"\bPage\s+\d+\s+Printed\s+on\s+\d{1,2}/\d{1,2}/\d{2,4}\s+.*?"
        r"\bMeeting\s+Agenda\s+[A-Z][a-z]+\s+\d{1,2},\s+\d{4}\b",
        " ",
        value,
    )
    value = re.sub(r"\s*([,.;:!?])\s*", r"\1 ", value)
    value = re.sub(r"(?<=\d)\.\s+(?=[A-Za-z]\b)", ".", value)
    value = re.sub(r"(?<=\w)\s*([’'])\s*(?=\w)", r"\1", value)
    value = re.sub(r"(?<=\w)\s*-\s*(?=\w)", "-", value)
    value = re.sub(r"\(\s*", "(", value)
    value = re.sub(r"\s*\)", ")", value)
    return _normalized_source_text(value)


def _evidence_span(
    lines: Sequence[str], *, line_start: int, line_end: int, evidence_quote: str
) -> tuple[int, int, bool]:
    """Return the declared span or a uniquely exact nearby correction for LLM line-number drift."""
    quote = _evidence_comparison_text(evidence_quote).casefold()
    declared = _evidence_comparison_text(" ".join(lines[line_start - 1 : line_end])).casefold()
    if quote in declared:
        return line_start, line_end, False
    # Models sometimes omit a nearby numbered line while quoting it exactly. Search only a small
    # local envelope and accept just one minimum-width exact match; paraphrases still fail closed.
    lower = max(1, line_start - 4)
    upper = min(len(lines), line_end + 4)
    matches: list[tuple[int, int]] = []
    for candidate_start in range(lower, upper + 1):
        for candidate_end in range(candidate_start, upper + 1):
            candidate = _evidence_comparison_text(
                " ".join(lines[candidate_start - 1 : candidate_end])
            ).casefold()
            if quote in candidate:
                matches.append((candidate_start, candidate_end))
    if not matches:
        raise ValueError("agenda item evidence quote is absent from cited lines")
    minimum_width = min(end - start for start, end in matches)
    tightest = [match for match in matches if match[1] - match[0] == minimum_width]
    if len(tightest) != 1:
        raise ValueError("agenda item evidence quote has ambiguous nearby source span")
    return *tightest[0], True


def outline_adds_title_evidence(*, agenda_text: str, outline_text: str | None) -> bool:
    """Whether a format-aware source text is distinct enough to merit a paired experiment."""
    return bool(
        outline_text
        and _normalized_source_text(outline_text)
        and _normalized_source_text(outline_text) != _normalized_source_text(agenda_text)
    )


def build_agenda_item_extraction_request(
    *,
    agenda_text: str,
    model: str | None = None,
    candidate_hints: Sequence[Mapping[str, object]] = (),
    prompt_variant: str = "standard",
) -> AgendaItemExtractionRequest:
    """Build a full-agenda request whose response is constrained to line-addressable source text."""
    if not agenda_text.strip():
        raise ValueError("agenda item extraction requires non-empty agenda text")
    if prompt_variant not in AGENDA_EXTRACTION_PROMPT_VARIANTS:
        raise ValueError(f"unknown agenda extraction prompt variant: {prompt_variant}")
    source_lines = tuple(agenda_text.splitlines())
    material: dict[str, object] = {
        "source_lines": [
            {"line": line_number, "text": text}
            for line_number, text in enumerate(source_lines, start=1)
            if text.strip()
        ]
    }
    if candidate_hints:
        validated_hints = []
        for hint in candidate_hints:
            line_start = hint.get("line_start")
            line_end = hint.get("line_end")
            priority = hint.get("priority")
            if (
                not isinstance(line_start, int)
                or not isinstance(line_end, int)
                or line_start < 1
                or line_end < line_start
                or line_end > len(source_lines)
                or priority not in {"high", "low"}
            ):
                raise ValueError("candidate hints must contain bounded high/low source spans")
            validated_hints.append(
                {"line_start": line_start, "line_end": line_end, "priority": priority}
            )
        material["candidate_hints"] = validated_hints
    messages = (
        {
            "role": "system",
            "content": (
                "Extract every actual municipal-meeting agenda action item from the supplied "
                "source lines. Exclude section headings, meeting instructions, schedules, "
                "boilerplate, attachments, and document labels. For every item, return a concise "
                "faithful generated title and an exact "
                "evidence_quote from the cited lines. Do not add facts or merge items. "
                "Return only a JSON object with exactly one top-level key, `items`, whose value is "
                "an array of item objects matching the requested fields; do not include Markdown, "
                "commentary, or any other top-level field. "
                "line_start and line_end must delimit the source lines supporting evidence_quote. "
                "The title is generated provenance, not canonical provider text. You may return "
                "display_ref for a visible official ID or item number, but it is optional: do not "
                "include a parent/section heading in the evidence span merely to construct it. "
                "Do not use a section heading itself as an agenda action item. A section heading "
                "is not itself an agenda action item, but include each distinct action, case, "
                "application, ordinance, resolution, or hearing listed beneath it. In particular, "
                "under headings such as Public Hearings, include the individual hearing cases or "
                "actions; do not return only the parent heading or discard its contents. Exclude "
                "generic public-comment participation instructions, speaker-registration "
                "instructions, and time-limit rules unless the agenda separately identifies a "
                "specific action item. Treat a clearly labeled Consent Agenda or Consent Calendar "
                "as one composite agenda action when following entries are its constituent items "
                "and the agenda does not state that they will be separately considered. Return the "
                "composite consent-agenda item rather than enumerating every constituent entry. "
                "If an entry is expressly removed, separately considered, or called out for an "
                "individual vote, return it separately."
                " Candidate hints, when supplied, are soft recall cues from independent "
                "deterministic and weak-label methods. Inspect the cited source lines yourself: "
                "return a hint only when it is genuinely chapterable, and do not omit a "
                "source-grounded item merely because it is absent from the hints. Hint priority "
                "does not change the output rules."
                + _PROMPT_VARIANT_INSTRUCTIONS[prompt_variant]
            ),
        },
        {
            "role": "user",
            "content": json.dumps(material, ensure_ascii=False, separators=(",", ":")),
        },
    )
    input_tokens = estimate_tokens(list(messages))
    return AgendaItemExtractionRequest(
        source_line_count=len(source_lines),
        messages=messages,
        model=model or select_locator_model(input_tokens),
        input_tokens=input_tokens,
    )


def build_title_equivalence_request(
    *, canonical_titles: Sequence[str], generated_titles: Sequence[str]
) -> TitleEquivalenceRequest:
    """Build a held-out semantic judge request after blind agenda-item extraction completes."""
    if not canonical_titles or not generated_titles:
        raise ValueError("title equivalence requires non-empty canonical and generated title lists")
    material = {
        "canonical_titles": list(canonical_titles),
        "generated_titles": list(generated_titles),
    }
    messages = (
        {
            "role": "system",
            "content": (
                "Evaluate semantic equivalence between generated agenda-item titles and canonical "
                "provider chapter titles. The generated titles are intentionally concise; do not "
                "require copied wording. First identify canonical titles that are specific agenda "
                "actions/items, excluding generic section headings and procedural headings. Then "
                "return one-to-one index matches only where both titles represent the same agenda "
                "action. All indices are zero-based: the first title in either supplied list is "
                "index 0. Do not match merely related topics, and do not infer missing actions."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(material, ensure_ascii=False, separators=(",", ":")),
        },
    )
    input_tokens = estimate_tokens(list(messages))
    return TitleEquivalenceRequest(
        messages=messages,
        model=select_locator_model(input_tokens),
        input_tokens=input_tokens,
    )


def ensure_agenda_item_extractor_contract():
    """Register the structured direct-extraction response contract."""
    from pydantic import BaseModel, ConfigDict, Field

    from citypods.compute.structured import register_response_model, response_model

    class Item(BaseModel):
        model_config = ConfigDict(extra="forbid")
        display_ref: str | None = Field(default=None, max_length=500)
        title: str = Field(min_length=1, max_length=2_000)
        evidence_quote: str = Field(min_length=1, max_length=20_000)
        line_start: int = Field(ge=1)
        line_end: int = Field(ge=1)

    class Response(BaseModel):
        model_config = ConfigDict(extra="forbid")
        items: list[Item] = Field(default_factory=list, max_length=300)

    model = getattr(ensure_agenda_item_extractor_contract, "model", None)
    if model is None:
        model = Response
        register_response_model(AGENDA_ITEM_EXTRACTOR_CONTRACT, model)
        ensure_agenda_item_extractor_contract.model = model
    else:
        try:
            response_model(AGENDA_ITEM_EXTRACTOR_CONTRACT)
        except ValueError:
            register_response_model(AGENDA_ITEM_EXTRACTOR_CONTRACT, model)
    return model


def ensure_title_equivalence_contract():
    """Register the benchmark-only semantic title-equivalence response contract."""
    from pydantic import BaseModel, ConfigDict, Field

    from citypods.compute.structured import register_response_model, response_model

    class Match(BaseModel):
        model_config = ConfigDict(extra="forbid")
        generated_index: int = Field(ge=0)
        canonical_index: int = Field(ge=0)

    class Response(BaseModel):
        model_config = ConfigDict(extra="forbid")
        canonical_action_indices: list[int] = Field(default_factory=list, max_length=300)
        matches: list[Match] = Field(default_factory=list, max_length=300)

    model = getattr(ensure_title_equivalence_contract, "model", None)
    if model is None:
        model = Response
        register_response_model(TITLE_EQUIVALENCE_CONTRACT, model)
        ensure_title_equivalence_contract.model = model
    else:
        try:
            response_model(TITLE_EQUIVALENCE_CONTRACT)
        except ValueError:
            register_response_model(TITLE_EQUIVALENCE_CONTRACT, model)
    return model


def assess_agenda_item_extractor_response(
    content: str | bytes, *, agenda_text: str
) -> AgendaItemEvidenceAssessment:
    """Validate each structured item independently for shadow-only evidence coverage reporting."""
    model = ensure_agenda_item_extractor_contract()
    response = model.model_validate_json(content)
    lines = tuple(agenda_text.splitlines())
    seen_evidence: set[tuple[int, int, str]] = set()
    items: list[ExtractedAgendaItem] = []
    rejected: list[RejectedAgendaItem] = []
    for index, item in enumerate(response.items):
        display_ref = _normalized_source_text(item.display_ref or "") or None
        try:
            if item.line_end < item.line_start or item.line_end > len(lines):
                raise ValueError("agenda item source line range is outside the supplied agenda")
            title = _normalized_source_text(item.title)
            evidence_quote = _normalized_source_text(item.evidence_quote)
            if not title or not evidence_quote:
                raise ValueError("agenda item source evidence must not be whitespace")
            evidence_start, evidence_end, evidence_span_repaired = _evidence_span(
                lines,
                line_start=item.line_start,
                line_end=item.line_end,
                evidence_quote=evidence_quote,
            )
            evidence_key = (
                evidence_start,
                evidence_end,
                _evidence_comparison_text(evidence_quote).casefold(),
            )
            if evidence_key in seen_evidence:
                raise ValueError("duplicate agenda item source evidence")
        except ValueError as exc:
            rejected.append(
                RejectedAgendaItem(index=index, display_ref=display_ref, reason=str(exc))
            )
            continue
        seen_evidence.add(evidence_key)
        items.append(
            ExtractedAgendaItem(
                display_ref=display_ref,
                title=title,
                evidence_quote=evidence_quote,
                line_start=evidence_start,
                line_end=evidence_end,
                evidence_span_repaired=evidence_span_repaired,
            )
        )
    return AgendaItemEvidenceAssessment(items=tuple(items), rejected=tuple(rejected))


def validate_agenda_item_extractor_response(
    content: str | bytes, *, agenda_text: str
) -> list[ExtractedAgendaItem]:
    """Require every model item to have unique, line-bounded, verbatim source evidence."""
    assessment = assess_agenda_item_extractor_response(content, agenda_text=agenda_text)
    if assessment.rejected:
        raise ValueError(assessment.rejected[0].reason)
    return list(assessment.items)


def validate_title_equivalence_response(
    content: str | bytes, *, canonical_count: int, generated_count: int
) -> TitleEquivalenceResult:
    """Reject malformed or non-one-to-one semantic benchmark matches."""
    if canonical_count <= 0 or generated_count <= 0:
        raise ValueError("title equivalence counts must be positive")
    model = ensure_title_equivalence_contract()
    response = model.model_validate_json(content)
    raw_action_indices = tuple(response.canonical_action_indices)
    raw_matches = tuple(response.matches)
    raw_indices = [*raw_action_indices]
    raw_indices.extend(match.canonical_index for match in raw_matches)
    raw_indices.extend(match.generated_index for match in raw_matches)
    # Mistral has repeatedly used one-based list positions despite the prompt. Only normalize when
    # the convention is unambiguous: a terminal index equals a supplied list length and no zero
    # appears. Any other out-of-range output remains invalid.
    one_based = 0 not in raw_indices and (
        canonical_count in raw_action_indices
        or any(match.canonical_index == canonical_count for match in raw_matches)
    )
    offset = 1 if one_based else 0
    action_indices = tuple(index - offset for index in raw_action_indices)
    if len(set(action_indices)) != len(action_indices):
        raise ValueError("duplicate canonical action index")
    if any(index >= canonical_count for index in action_indices):
        raise ValueError("canonical action index is outside supplied titles")
    canonical_indices: set[int] = set()
    generated_indices: set[int] = set()
    matches: list[TitleEquivalenceMatch] = []
    for raw_match in raw_matches:
        canonical_index = raw_match.canonical_index - offset
        generated_index = raw_match.generated_index - offset
        if canonical_index < 0 or generated_index < 0:
            raise ValueError("title equivalence match index is outside supplied titles")
        if canonical_index >= canonical_count or generated_index >= generated_count:
            raise ValueError("title equivalence match index is outside supplied titles")
        if canonical_index not in action_indices:
            raise ValueError("title equivalence match targets a non-action canonical title")
        if canonical_index in canonical_indices or generated_index in generated_indices:
            raise ValueError("title equivalence matches must be one-to-one")
        canonical_indices.add(canonical_index)
        generated_indices.add(generated_index)
        matches.append(
            TitleEquivalenceMatch(
                generated_index=generated_index,
                canonical_index=canonical_index,
            )
        )
    return TitleEquivalenceResult(canonical_action_indices=action_indices, matches=tuple(matches))


def match_title_candidates(
    canonical_titles: Sequence[str],
    candidates: Sequence[AgendaTitleCandidate],
    *,
    threshold: float = 0.8,
) -> list[float]:
    """Return one-to-one high-confidence lexical matches for benchmark evaluation only."""
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold must be between zero and one")
    pairs = sorted(
        (
            (agenda_title_similarity(canonical, candidate.title), canonical_index, candidate_index)
            for canonical_index, canonical in enumerate(canonical_titles)
            for candidate_index, candidate in enumerate(candidates)
        ),
        reverse=True,
    )
    used_canonical: set[int] = set()
    used_candidates: set[int] = set()
    matches: list[float] = []
    for similarity, canonical_index, candidate_index in pairs:
        if similarity < threshold:
            break
        if canonical_index in used_canonical or candidate_index in used_candidates:
            continue
        used_canonical.add(canonical_index)
        used_candidates.add(candidate_index)
        matches.append(similarity)
    return matches


__all__ = [
    "AGENDA_ITEM_EXTRACTOR_CONTRACT",
    "AGENDA_EXTRACTION_PROMPT_VARIANTS",
    "TITLE_EQUIVALENCE_CONTRACT",
    "AgendaItemEvidenceAssessment",
    "AgendaItemExtractionRequest",
    "ExtractedAgendaItem",
    "RejectedAgendaItem",
    "TitleEquivalenceMatch",
    "TitleEquivalenceRequest",
    "TitleEquivalenceResult",
    "assess_agenda_item_extractor_response",
    "build_agenda_item_extraction_request",
    "build_title_equivalence_request",
    "ensure_agenda_item_extractor_contract",
    "ensure_title_equivalence_contract",
    "match_title_candidates",
    "outline_adds_title_evidence",
    "validate_agenda_item_extractor_response",
    "validate_title_equivalence_response",
]
