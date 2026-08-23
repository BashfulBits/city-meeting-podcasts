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
from citypods.chapter_locator import select_locator_models
from citypods.compute.llm_policy import estimate_tokens

AGENDA_ITEM_EXTRACTOR_CONTRACT = "agenda-chapter-item-extract"
TITLE_EQUIVALENCE_CONTRACT = "agenda-chapter-title-equivalence"
AGENDA_PRODUCTION_MODEL = "mistral/mistral-medium-2508"
# Same-priority alternate for AGENDA_PRODUCTION_MODEL (hotfix follow-up: Mistral's account-wide
# monthly budget is exhausted, see config/provider_limits.yml). The scheduler's own
# availability/pacing ranking picks between these -- this is not a preferred-then-fallback order.
# `AGENDA_PRODUCTION_MODEL` itself stays the label used for recipe-hash/provenance defaults; the
# actually-dispatched model is recorded separately via `JobResult.model`/`JobHandle.model` (R13).
AGENDA_PRODUCTION_MODELS = (AGENDA_PRODUCTION_MODEL, "meta-llama/llama-3.3-70b-instruct")

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

AGENDA_EXTRACTION_PROMPT_VARIANTS = frozenset(_PROMPT_VARIANT_INSTRUCTIONS)

_MAX_EVIDENCE_SPAN_LINES = 200

# Agenda layout extraction commonly separates a visible item/case reference from its action
# wording.  Keep this deliberately narrow: the post-processor may absorb one immediately
# preceding identifier-only line, but must not swallow a preceding prose heading or an arbitrary
# neighboring agenda item.
_IDENTIFIER_ONLY_LINE_RE = re.compile(
    r"^\s*(?:(?:id|item|case|no\.?|ref(?:erence)?)\s*[:#\-]?\s*"
    r"[a-z0-9][a-z0-9._/\-]*|"
    r"(?:[a-z]?\d+(?:[./\-][a-z0-9]+)*|[a-z])\s*[.)]?)\s*$",
    re.IGNORECASE,
)

_EXPLICIT_REFERENCE_RE = re.compile(
    r"\b(?:id|item|case|no\.?|ref(?:erence)?)\s*[:#\-]?\s*"
    r"[a-z0-9][a-z0-9._/\-]*",
    re.IGNORECASE,
)

_LEADING_REFERENCE_RE = re.compile(
    r"^\s*((?:[a-z]?\d+(?:[./\-][a-z0-9]+)*|[a-z])\s*[.)]?)\s+(?=\S)",
    re.IGNORECASE,
)

_RECOVERY_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_RECOVERY_MARKER_LINE_RE = re.compile(
    r"^(?:(?:[a-z]?\d+(?:[./\-][a-z0-9]+)*|[ivxlcdm]{1,8}|[a-z])[.)]?|"
    r"(?:id|item|case|no\.?|ref(?:erence)?)\s*[:#\-]?\s*[a-z0-9][a-z0-9._/\-]*)$",
    re.IGNORECASE,
)
_RECOVERY_FORMAL_REFERENCE_RE = re.compile(
    r"^(?:(?:id|item|case|no\.?|ref(?:erence)?)\s*[:#\-]?\s*[a-z0-9][a-z0-9._/\-]*|"
    r"[a-z]{1,8}\d+[a-z0-9]*(?:[./\-][a-z0-9]+)*[.)]?|"
    r"[a-z]?\d+(?:[./\-][a-z0-9]+)*[.)]?|"
    r"[ivxlcdm]+(?:\.[a-z0-9]+)+[.)]?|"
    r"[ivxlcdm]+[.)]?)$",
    re.IGNORECASE,
)


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
    """A direct, source-line-addressable agenda-item extraction request.

    ``model`` is the primary/first candidate; ``models`` (additive) carries the full same-priority
    candidate band from ``select_locator_models`` when the caller didn't pin an explicit model.
    """

    source_line_count: int
    messages: tuple[dict[str, str], ...]
    model: str
    models: tuple[str, ...]
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
class RecoveredAgendaItem:
    """A rejected raw item recovered by a source-only shadow repair."""

    raw_index: int
    display_ref: str | None
    original_display_ref: str | None
    title: str
    evidence_quote: str
    source_evidence: str
    line_start: int
    line_end: int
    recovery_method: str
    evidence_span_repaired: bool = True


@dataclass(frozen=True)
class AgendaItemRecoveryAssessment:
    """Strict accepted items plus separately labeled, source-only recovery candidates."""

    items: tuple[ExtractedAgendaItem, ...]
    recovered: tuple[RecoveredAgendaItem, ...]
    rejected: tuple[RejectedAgendaItem, ...]
    unrecovered: tuple[RejectedAgendaItem, ...]


@dataclass(frozen=True)
class TitleEquivalenceRequest:
    """A held-out semantic comparison request; never used to generate agenda items.

    ``model`` is the primary/first candidate; ``models`` (additive) carries the full same-priority
    candidate band from ``select_locator_models``.
    """

    messages: tuple[dict[str, str], ...]
    model: str
    models: tuple[str, ...]
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


def _is_identifier_only_line(line: str) -> bool:
    """Return whether *line* is only a visible agenda number, letter, or ID."""
    return bool(_IDENTIFIER_ONLY_LINE_RE.fullmatch(line))


def _expand_identifier_prefix(
    lines: Sequence[str], *, line_start: int, line_end: int
) -> tuple[int, int, bool]:
    """Absorb one immediately preceding identifier-only line into an evidence span."""
    if line_start <= 1 or not _is_identifier_only_line(lines[line_start - 2]):
        return line_start, line_end, False
    return line_start - 1, line_end, True


def _reference_in_span(
    display_ref: str, lines: Sequence[str], *, line_start: int, line_end: int
) -> bool:
    """Compare a model reference with immutable source text after layout normalization."""
    reference = _evidence_comparison_text(display_ref).casefold()
    source = _evidence_comparison_text(" ".join(lines[line_start - 1 : line_end])).casefold()
    return bool(reference) and reference in source


def _derive_display_ref(lines: Sequence[str], *, line_start: int, line_end: int) -> str | None:
    """Derive the first explicit or leading visible reference from an expanded source span."""
    source_lines = lines[line_start - 1 : line_end]
    for line in source_lines:
        if _is_identifier_only_line(line):
            return _normalized_source_text(line)
    for line in source_lines:
        explicit = _EXPLICIT_REFERENCE_RE.search(line)
        if explicit:
            return _normalized_source_text(explicit.group(0))
    for line in source_lines:
        leading = _LEADING_REFERENCE_RE.match(line)
        if leading:
            return _normalized_source_text(leading.group(1))
    return None


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
                "does not change the output rules." + _PROMPT_VARIANT_INSTRUCTIONS[prompt_variant]
            ),
        },
        {
            "role": "user",
            "content": json.dumps(material, ensure_ascii=False, separators=(",", ":")),
        },
    )
    input_tokens = estimate_tokens(list(messages))
    models = (model,) if model else select_locator_models(input_tokens)
    return AgendaItemExtractionRequest(
        source_line_count=len(source_lines),
        messages=messages,
        model=models[0],
        models=models,
        input_tokens=input_tokens,
    )


def build_production_agenda_item_extraction_request(
    agenda_text: str,
    *,
    candidate_hints: Sequence[Mapping[str, object]] = (),
) -> AgendaItemExtractionRequest:
    """Build the pinned production agenda-flow request.

    Research callers retain the historical model selector and prompt variants; production uses
    the approved Mistral Medium route and the recall-oriented agenda-flow instructions.
    """

    return build_agenda_item_extraction_request(
        agenda_text=agenda_text,
        model=AGENDA_PRODUCTION_MODEL,
        candidate_hints=candidate_hints,
        prompt_variant="agenda-flow",
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
    models = select_locator_models(input_tokens)
    return TitleEquivalenceRequest(
        messages=messages,
        model=models[0],
        models=models,
        input_tokens=input_tokens,
    )


def ensure_agenda_item_extractor_contract():
    """Register the structured direct-extraction response contract."""
    from citypods.compute.structured import register_response_model, response_model

    cached = getattr(ensure_agenda_item_extractor_contract, "model", None)
    if cached is not None:
        return cached

    try:
        model = response_model(AGENDA_ITEM_EXTRACTOR_CONTRACT)
        ensure_agenda_item_extractor_contract.model = model
        return model
    except ValueError:
        pass

    from pydantic import BaseModel, ConfigDict, Field

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

    model = register_response_model(AGENDA_ITEM_EXTRACTOR_CONTRACT, Response)
    ensure_agenda_item_extractor_contract.model = model
    return model


def ensure_title_equivalence_contract():
    """Register the benchmark-only semantic title-equivalence response contract."""
    from citypods.compute.structured import register_response_model, response_model

    cached = getattr(ensure_title_equivalence_contract, "model", None)
    if cached is not None:
        return cached

    try:
        model = response_model(TITLE_EQUIVALENCE_CONTRACT)
        ensure_title_equivalence_contract.model = model
        return model
    except ValueError:
        pass

    from pydantic import BaseModel, ConfigDict, Field

    class Match(BaseModel):
        model_config = ConfigDict(extra="forbid")
        generated_index: int = Field(ge=0)
        canonical_index: int = Field(ge=0)

    class Response(BaseModel):
        model_config = ConfigDict(extra="forbid")
        canonical_action_indices: list[int] = Field(default_factory=list, max_length=300)
        matches: list[Match] = Field(default_factory=list, max_length=300)

    model = register_response_model(TITLE_EQUIVALENCE_CONTRACT, Response)
    ensure_title_equivalence_contract.model = model
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
            if item.line_end - item.line_start + 1 > _MAX_EVIDENCE_SPAN_LINES:
                raise ValueError("agenda item source line range is implausibly wide")
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
            evidence_start, evidence_end, prefix_expanded = _expand_identifier_prefix(
                lines, line_start=evidence_start, line_end=evidence_end
            )
            evidence_span_repaired = evidence_span_repaired or prefix_expanded
            if display_ref is not None:
                if not _reference_in_span(
                    display_ref, lines, line_start=evidence_start, line_end=evidence_end
                ):
                    raise ValueError("display reference is absent from expanded evidence span")
            else:
                display_ref = _derive_display_ref(
                    lines, line_start=evidence_start, line_end=evidence_end
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


def _recovery_tokens(value: str) -> list[str]:
    return _RECOVERY_TOKEN_RE.findall(value.casefold())


def _recovery_line_offsets(lines: Sequence[str]) -> tuple[list[str], list[int]]:
    normalized_lines = [_evidence_comparison_text(line).casefold() for line in lines]
    offsets: list[int] = []
    cursor = 0
    for index, line in enumerate(normalized_lines):
        offsets.append(cursor)
        cursor += len(line)
        if index + 1 < len(normalized_lines):
            cursor += 1
    return normalized_lines, offsets


def _recovery_line_for_offset(offsets: Sequence[int], offset: int) -> int:
    result = 0
    for index, start in enumerate(offsets):
        if start > offset:
            break
        result = index
    return result


def _recovery_exact_spans(lines: Sequence[str], quote: str) -> list[tuple[int, int]]:
    """Find all exact normalized source line spans containing *quote*."""
    normalized_lines, offsets = _recovery_line_offsets(lines)
    source = " ".join(normalized_lines)
    needle = _evidence_comparison_text(quote).casefold()
    if not needle:
        return []
    spans: list[tuple[int, int]] = []
    position = source.find(needle)
    while position >= 0:
        end_position = position + len(needle) - 1
        start_line = _recovery_line_for_offset(offsets, position)
        end_line = _recovery_line_for_offset(offsets, end_position)
        spans.append((start_line + 1, end_line + 1))
        position = source.find(needle, position + 1)
    return spans


def _recovery_tightest_spans(spans: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    if not spans:
        return []
    width = min(end - start for start, end in spans)
    return sorted({span for span in spans if span[1] - span[0] == width})


def _recovery_subsequence_spans(
    lines: Sequence[str],
    quote: str,
    *,
    line_start: int,
    line_end: int,
    radius: int = 8,
) -> list[tuple[int, int]]:
    """Find bounded source windows with quote tokens in order, for layout-only recovery."""
    quote_tokens = _recovery_tokens(_evidence_comparison_text(quote))
    if len(quote_tokens) < 6:
        return []
    lower = max(1, line_start - radius)
    upper = min(len(lines), line_end + radius)
    token_lines = [_recovery_tokens(_evidence_comparison_text(line)) for line in lines]
    candidates: list[tuple[int, int, int]] = []
    for start in range(lower, upper + 1):
        source_tokens: list[str] = []
        for end in range(start, upper + 1):
            source_tokens.extend(token_lines[end - 1])
            cursor = 0
            first = None
            last = None
            for token in quote_tokens:
                try:
                    found = source_tokens.index(token, cursor)
                except ValueError:
                    break
                if first is None:
                    first = found
                last = found
                cursor = found + 1
            else:
                assert first is not None and last is not None
                extra_tokens = (last - first + 1) - len(quote_tokens)
                if extra_tokens <= max(24, len(quote_tokens) // 2):
                    candidates.append((start, end, extra_tokens))
    if not candidates:
        return []
    minimum_width = min(end - start for start, end, _ in candidates)
    minimum_extra = min(extra for start, end, extra in candidates if end - start == minimum_width)
    return sorted(
        {
            (start, end)
            for start, end, extra in candidates
            if end - start == minimum_width and extra == minimum_extra
        }
    )


def _recovery_reference_kind(reference: object) -> str:
    if not isinstance(reference, str) or not reference.strip():
        return "absent"
    return (
        "formal"
        if _RECOVERY_FORMAL_REFERENCE_RE.fullmatch(reference.strip())
        else "descriptive_label"
    )


def _recovery_reference_parts(reference: str) -> list[str]:
    normalized = _evidence_comparison_text(reference).casefold()
    normalized = re.sub(r"^(?:id|item|case|no|reference)\s+", "", normalized)
    if "." in normalized:
        return [part for part in re.split(r"\s*\.\s*", normalized) if part]
    return _recovery_tokens(normalized)


def _recovery_marker_line(line: str, part: str) -> bool:
    tokens = _recovery_tokens(line)
    part_tokens = _recovery_tokens(part)
    return len(tokens) <= 3 and bool(part_tokens) and part_tokens[-1] in tokens


def _recovery_identifier_prefix_start(lines: Sequence[str], line_start: int) -> int:
    """Return the first immediately preceding standalone identifier/marker line."""
    start = line_start
    while start > 1 and _RECOVERY_MARKER_LINE_RE.fullmatch(lines[start - 2].strip()):
        start -= 1
    return start


def _recovery_trailing_reference_line(line: str) -> bool:
    """Return whether *line* is a standalone formal ID suitable for forward expansion."""
    normalized = _normalized_source_text(line)
    if not normalized or not _RECOVERY_FORMAL_REFERENCE_RE.fullmatch(normalized):
        return False
    # Do not absorb a bare ``A.``/``3.`` continuation marker or the next numbered item. A
    # trailing recovery line must look like a formal ID (e.g. DCA26-0002B., SUP14-6) or use an
    # explicit ID/Item/Case/Reference label.
    return bool(re.search(r"\d", normalized)) and bool(
        re.search(r"[a-z]", normalized, re.IGNORECASE)
        or re.match(r"(?i)^(?:id|item|case|no\.?|ref(?:erence)?)\b", normalized)
    )


def _recovery_expand_trailing_reference(
    lines: Sequence[str], *, line_end: int, forward_lines: int = 16
) -> tuple[int, bool]:
    """Expand through one nearby formal ID line when the source paragraph continues to it."""
    upper = min(len(lines), line_end + forward_lines)
    for candidate in range(line_end + 1, upper + 1):
        # A blank line or page/layout break ends the paragraph. This prevents absorbing the next
        # agenda item's reference when the extractor put adjacent items in separate blocks.
        if any(
            not _normalized_source_text(lines[index - 1])
            for index in range(line_end + 1, candidate)
        ):
            break
        if _recovery_trailing_reference_line(lines[candidate - 1]):
            return candidate, True
    return line_end, False


def _recovery_resolve_reference(
    reference: object,
    lines: Sequence[str],
    *,
    line_start: int,
    line_end: int,
    prefix_lines: int = 12,
) -> tuple[str, bool, list[int]]:
    """Resolve a formal reference without treating descriptive labels as identifiers."""
    kind = _recovery_reference_kind(reference)
    if kind == "absent":
        return kind, True, []
    assert isinstance(reference, str)
    normalized_reference = _evidence_comparison_text(reference).casefold()
    span_source = _evidence_comparison_text(" ".join(lines[line_start - 1 : line_end])).casefold()
    if normalized_reference in span_source:
        return kind, True, []
    if kind == "descriptive_label":
        return kind, True, []
    parts = _recovery_reference_parts(reference)
    envelope_start = max(1, line_start - prefix_lines)
    cursor = 0
    matched_lines: list[int] = []
    for part in parts:
        part_norm = _evidence_comparison_text(part).casefold()
        found = None
        for index in range(cursor, line_end - envelope_start + 1):
            line = _evidence_comparison_text(lines[envelope_start - 1 + index]).casefold()
            if _recovery_marker_line(line, part_norm) and re.search(
                rf"(?<![a-z0-9]){re.escape(part_norm)}(?![a-z0-9])", line
            ):
                found = index
                break
        if found is None:
            return kind, False, []
        matched_lines.append(envelope_start + found)
        cursor = found + 1
    return kind, True, matched_lines


def recover_agenda_item_extractor_response(
    content: str | bytes, *, agenda_text: str
) -> AgendaItemRecoveryAssessment:
    """Return strict items plus source-only recovery candidates without changing strict behavior.

    Exact recovery searches the complete source. Token-subsequence recovery is deliberately
    shadow-only: its returned ``source_evidence`` is the complete source window, never the model's
    potentially discontinuous quote. Ambiguous spans and unresolved formal references remain
    rejected for a later retry/OCR decision.
    """
    model = ensure_agenda_item_extractor_contract()
    response = model.model_validate_json(content)
    strict = assess_agenda_item_extractor_response(content, agenda_text=agenda_text)
    lines = tuple(agenda_text.splitlines())
    rejected_by_index = {item.index: item for item in strict.rejected}
    accepted_keys = {
        (
            item.line_start,
            item.line_end,
            _evidence_comparison_text(item.evidence_quote).casefold(),
        )
        for item in strict.items
    }
    recovered_keys: set[tuple[int, int, str]] = set()
    recovered: list[RecoveredAgendaItem] = []
    recovered_indices: set[int] = set()
    for index, raw_item in enumerate(response.items):
        rejection = rejected_by_index.get(index)
        if rejection is None:
            continue
        declared_start = max(1, int(raw_item.line_start))
        declared_end = min(len(lines), int(raw_item.line_end))
        if declared_start > declared_end or not lines:
            continue
        exact = _recovery_tightest_spans(_recovery_exact_spans(lines, raw_item.evidence_quote))
        spans = exact
        method = ""
        if len(exact) == 1:
            method = "exact-global"
        elif not exact:
            if declared_end - declared_start + 1 > _MAX_EVIDENCE_SPAN_LINES:
                continue
            subsequence = _recovery_subsequence_spans(
                lines,
                raw_item.evidence_quote,
                line_start=declared_start,
                line_end=declared_end,
            )
            if len(subsequence) == 1:
                spans = subsequence
                method = "token-subsequence-window"
        if len(spans) != 1:
            continue
        line_start, line_end = spans[0]
        line_end, trailing_expanded = _recovery_expand_trailing_reference(lines, line_end=line_end)
        if trailing_expanded:
            method += "+trailing-reference"
        kind, resolved, matched_prefix_lines = _recovery_resolve_reference(
            raw_item.display_ref,
            lines,
            line_start=line_start,
            line_end=line_end,
        )
        if not resolved:
            continue
        if matched_prefix_lines:
            line_start = min(line_start, *matched_prefix_lines)
            method += "+hierarchical-prefix"
        else:
            prefix_start = _recovery_identifier_prefix_start(lines, line_start)
            if prefix_start < line_start:
                line_start = prefix_start
                method += "+identifier-prefix"
        display_ref = (
            _normalized_source_text(raw_item.display_ref or "") if kind == "formal" else None
        )
        source_evidence = "\n".join(lines[line_start - 1 : line_end])
        key = (
            line_start,
            line_end,
            _evidence_comparison_text(raw_item.evidence_quote).casefold(),
        )
        if key in accepted_keys or key in recovered_keys:
            continue
        recovered.append(
            RecoveredAgendaItem(
                raw_index=index,
                display_ref=display_ref,
                original_display_ref=(_normalized_source_text(raw_item.display_ref or "") or None),
                title=_normalized_source_text(raw_item.title),
                evidence_quote=_normalized_source_text(raw_item.evidence_quote),
                source_evidence=source_evidence,
                line_start=line_start,
                line_end=line_end,
                recovery_method=method,
            )
        )
        recovered_indices.add(index)
        recovered_keys.add(key)
    unrecovered = tuple(item for item in strict.rejected if item.index not in recovered_indices)
    return AgendaItemRecoveryAssessment(
        items=strict.items,
        recovered=tuple(recovered),
        rejected=strict.rejected,
        unrecovered=unrecovered,
    )


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
    "AGENDA_PRODUCTION_MODEL",
    "AGENDA_PRODUCTION_MODELS",
    "AGENDA_ITEM_EXTRACTOR_CONTRACT",
    "AGENDA_EXTRACTION_PROMPT_VARIANTS",
    "TITLE_EQUIVALENCE_CONTRACT",
    "AgendaItemEvidenceAssessment",
    "AgendaItemExtractionRequest",
    "AgendaItemRecoveryAssessment",
    "ExtractedAgendaItem",
    "RecoveredAgendaItem",
    "RejectedAgendaItem",
    "TitleEquivalenceMatch",
    "TitleEquivalenceRequest",
    "TitleEquivalenceResult",
    "assess_agenda_item_extractor_response",
    "build_agenda_item_extraction_request",
    "build_production_agenda_item_extraction_request",
    "build_title_equivalence_request",
    "ensure_agenda_item_extractor_contract",
    "ensure_title_equivalence_contract",
    "match_title_candidates",
    "outline_adds_title_evidence",
    "recover_agenda_item_extractor_response",
    "validate_agenda_item_extractor_response",
    "validate_title_equivalence_response",
]
