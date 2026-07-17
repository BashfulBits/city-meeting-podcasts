"""Versioned, explainable topic tagging for the R5 catalog taxonomy.

Rules are deliberately pure and additive.  They operate on chapter titles (the agenda signal)
and, when available, a locally stored transcript or extracted agenda sidecar.  The optional LLM
path uses the existing ``tag`` inference verb but can only add validated taxonomy IDs with evidence
that appears in the supplied material.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from citypods.chapters import episode_served_chapters

TAGGER_VERSION = "1"
LLM_CONTRACT = "topic-tags"
TAG_PROMPT_VERSION = "2"
TAG_FEATURE = "topic-tags"


@dataclass(frozen=True)
class TaxonomyTag:
    id: str
    label: str
    description: str
    group: str
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    source_refs: tuple[str, ...]


@dataclass(frozen=True)
class Taxonomy:
    version: int
    reviewed_at: str
    source_refs: dict[str, str]
    tags: tuple[TaxonomyTag, ...]

    @property
    def by_id(self) -> dict[str, TaxonomyTag]:
        return {tag.id: tag for tag in self.tags}


@dataclass(frozen=True)
class TagEvidence:
    where: Literal["agenda", "transcript"]
    span: str
    t: int | None = None
    chapter_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"where": self.where, "span": self.span}
        if self.t is not None:
            result["t"] = self.t
        if self.chapter_id is not None:
            result["chapter_id"] = self.chapter_id
        return result


def _as_strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("taxonomy rules must be lists of strings")
    return tuple(item.strip() for item in value if item.strip())


def taxonomy_from_dict(data: dict[str, Any]) -> Taxonomy:
    if not isinstance(data, dict):
        raise ValueError("taxonomy must be a mapping")
    raw_tags = data.get("tags")
    if not isinstance(raw_tags, list) or not raw_tags:
        raise ValueError("taxonomy must contain a non-empty tags list")
    source_refs = data.get("source_refs") or {}
    if not isinstance(source_refs, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in source_refs.items()
    ):
        raise ValueError("taxonomy source_refs must be a string mapping")
    tags: list[TaxonomyTag] = []
    seen: set[str] = set()
    for raw in raw_tags:
        if not isinstance(raw, dict):
            raise ValueError("each taxonomy tag must be a mapping")
        tag_id = str(raw.get("id") or "").strip()
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", tag_id):
            raise ValueError(f"invalid taxonomy tag id: {tag_id!r}")
        if tag_id in seen:
            raise ValueError(f"duplicate taxonomy tag id: {tag_id!r}")
        seen.add(tag_id)
        rules = raw.get("rules") or {}
        if not isinstance(rules, dict):
            raise ValueError(f"rules for {tag_id!r} must be a mapping")
        refs = _as_strings(raw.get("source_refs"))
        missing_refs = set(refs) - set(source_refs)
        if missing_refs:
            raise ValueError(f"unknown source_refs for {tag_id!r}: {sorted(missing_refs)}")
        include = _as_strings(rules.get("include"))
        if not include:
            raise ValueError(f"taxonomy tag {tag_id!r} has no include rules")
        tags.append(
            TaxonomyTag(
                id=tag_id,
                label=str(raw.get("label") or tag_id),
                description=str(raw.get("description") or ""),
                group=str(raw.get("group") or "other"),
                include=include,
                exclude=_as_strings(rules.get("exclude")),
                source_refs=refs,
            )
        )
    return Taxonomy(
        version=int(data.get("version") or 0),
        reviewed_at=str(data.get("reviewed_at") or ""),
        source_refs=dict(source_refs),
        tags=tuple(tags),
    )


def load_taxonomy(path: str | Path = "config/taxonomy.yml") -> Taxonomy:
    with Path(path).open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return taxonomy_from_dict(data)


def _literal_pattern(pattern: str) -> re.Pattern[str]:
    # Let a YAML phrase match normal whitespace variants while retaining word boundaries, so a
    # short term such as ``ADU`` does not match inside an unrelated identifier.
    expression = re.escape(pattern.strip()).replace(r"\ ", r"\s+")
    return re.compile(rf"(?<!\w){expression}(?!\w)", re.IGNORECASE)


def _evidence_for(
    text: str, *, where: Literal["agenda", "transcript"], patterns: tuple[str, ...]
) -> list[TagEvidence]:
    evidence: list[TagEvidence] = []
    seen: set[tuple[str, str]] = set()
    for pattern in patterns:
        for match in _literal_pattern(pattern).finditer(text):
            key = (where, match.group(0).casefold())
            if key in seen:
                continue
            seen.add(key)
            evidence.append(TagEvidence(where=where, span=match.group(0)))
    return evidence


def tag_episode(
    agenda_item_titles: str,
    transcript_text: str,
    taxonomy: Taxonomy,
) -> list[dict[str, Any]]:
    """Return deterministic rule tags in stable taxonomy order."""
    inputs = (
        ("agenda", agenda_item_titles or ""),
        ("transcript", transcript_text or ""),
    )
    # Exclude terms must suppress a match regardless of which source they appear in — an exclude
    # found only in the agenda must still block an include match found only in the transcript.
    combined_text = "\n".join(text for _, text in inputs if text)
    result: list[dict[str, Any]] = []
    for tag in taxonomy.tags:
        if any(_literal_pattern(pattern).search(combined_text) for pattern in tag.exclude):
            continue
        evidence: list[TagEvidence] = []
        for where, text in inputs:
            if not text:
                continue
            evidence.extend(_evidence_for(text, where=where, patterns=tag.include))
        if evidence:
            result.append(
                {
                    "id": tag.id,
                    "source": "rule",
                    "confidence": 1.0,
                    "evidence": [item.as_dict() for item in evidence[:8]],
                }
            )
    return result


def tag_recipe_hash(
    taxonomy: Taxonomy,
    *,
    agenda_item_titles: str,
    agenda_text: str,
    transcript_text: str,
    llm_enabled: bool,
    chapter_inputs: list[dict[str, Any]] | None = None,
    llm_route: str = "",
    prompt_version: str = TAG_PROMPT_VERSION,
    admission_policy: str = "",
) -> str:
    payload = {
        "taxonomy": taxonomy.version,
        "tagger": TAGGER_VERSION,
        "llm": llm_enabled,
        "llm_route": llm_route if llm_enabled else "",
        "prompt_version": prompt_version if llm_enabled else "",
        "admission_policy": admission_policy if llm_enabled else "",
        "agenda_item_titles": agenda_item_titles,
        "agenda_text": agenda_text,
        "transcript": transcript_text,
        "chapters": chapter_inputs or [],
    }
    return hashlib.sha1(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()[:16]


def _read_storage_bytes(storage: Any, key: str | None) -> bytes | None:
    if not key or storage is None or not hasattr(storage, "exists") or not storage.exists(key):
        return None
    if not hasattr(storage, "get_file"):
        return None
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "artifact"
        if not storage.get_file(key, path):
            return None
        return path.read_bytes()


def _text_from_transcript(data: bytes | None, fmt: str | None) -> str:
    if not data:
        return ""
    text = data.decode("utf-8", errors="replace")
    if fmt == "json" or text.lstrip().startswith(("{", "[")):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return text
        segments = payload.get("segments") if isinstance(payload, dict) else payload
        if isinstance(segments, list):
            return "\n".join(
                str(segment.get("text") or "")
                for segment in segments
                if isinstance(segment, dict) and segment.get("text")
            )
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.upper() == "WEBVTT" or stripped.isdigit():
            continue
        if "-->" in stripped:
            continue
        lines.append(re.sub(r"<[^>]+>", "", stripped))
    return "\n".join(lines)


def _timed_transcript_segments(data: bytes | None, fmt: str | None) -> list[dict[str, Any]]:
    """Parse only the bounded timing/text shape needed to assign evidence to chapters."""
    if not data:
        return []
    text = data.decode("utf-8", errors="replace")
    if fmt == "json" or text.lstrip().startswith(("{", "[")):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return []
        segments = payload.get("segments") if isinstance(payload, dict) else payload
        if isinstance(segments, list):
            result = []
            for segment in segments:
                if not isinstance(segment, dict) or not segment.get("text"):
                    continue
                try:
                    start = max(0.0, float(segment.get("start", 0)))
                except (TypeError, ValueError):
                    start = 0.0
                try:
                    end = float(segment.get("end")) if segment.get("end") is not None else None
                except (TypeError, ValueError):
                    end = None
                result.append({"start": start, "end": end, "text": str(segment["text"])})
            return result
    result: list[dict[str, Any]] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if "-->" not in line:
            continue
        start_raw, end_raw = (part.strip().replace(",", ".") for part in line.split("-->", 1))
        try:
            parts = [float(part) for part in start_raw.split(":")]
            start = (
                parts[-1]
                + (parts[-2] * 60 if len(parts) >= 2 else 0)
                + (parts[-3] * 3600 if len(parts) >= 3 else 0)
            )
            end_parts = [float(part) for part in end_raw.split(":")]
            end = (
                end_parts[-1]
                + (end_parts[-2] * 60 if len(end_parts) >= 2 else 0)
                + (end_parts[-3] * 3600 if len(end_parts) >= 3 else 0)
            )
        except ValueError:
            continue
        cue: list[str] = []
        for following in lines[index + 1 :]:
            if not following.strip() or "-->" in following:
                break
            cue.append(re.sub(r"<[^>]+>", "", following.strip()))
        if cue:
            result.append({"start": start, "end": end, "text": " ".join(cue)})
    return result


def chapter_id(ep: Any, chapter: dict[str, Any], index: int) -> str:
    """Stable identity based on source chapter data, never remapped served time.

    ``index`` is the chapter's position in the *served* list, which no longer matches its
    position in ``ep.source_chapters`` once an earlier chapter has been dropped by
    :func:`citypods.timeline.remap` (a chapter whose start falls in a cut span). When the
    served chapter carries a ``source_index`` (stamped by
    :func:`citypods.chapters.episode_served_chapters`), that true source position is used
    instead of ``index``.
    """
    lookup_index = chapter.get("source_index")
    if not isinstance(lookup_index, int):
        lookup_index = index
    source = chapter
    source_chapters = getattr(ep, "source_chapters", None) or []
    if 0 <= lookup_index < len(source_chapters) and isinstance(source_chapters[lookup_index], dict):
        source = source_chapters[lookup_index]
    payload = {
        "index": lookup_index,
        "start": source.get("start"),
        "title": str(source.get("title") or "").strip(),
    }
    digest = hashlib.sha1(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"ch-{digest[:12]}"


def chapter_tag_inputs(ep: Any, storage: Any = None) -> list[dict[str, Any]]:
    """Return chapter windows with stable IDs and served-time transcript evidence."""
    chapters = [
        chapter
        for chapter in episode_served_chapters(ep, with_source_index=True)
        if isinstance(chapter, dict)
    ]
    transcript_data = _read_storage_bytes(storage, ep.transcript_key)
    segments = sorted(
        _timed_transcript_segments(transcript_data, ep.transcript_format),
        key=lambda x: x["start"],
    )
    agenda_items = agenda_item_context(ep, storage)
    result: list[dict[str, Any]] = []
    for index, chapter in enumerate(chapters):
        try:
            start = max(0.0, float(chapter.get("start", 0)))
        except (TypeError, ValueError):
            start = 0.0
        try:
            next_start = (
                max(start, float(chapters[index + 1].get("start", start)))
                if index + 1 < len(chapters)
                else None
            )
        except (TypeError, ValueError):
            next_start = None
        local = [
            segment
            for segment in segments
            if segment["start"] >= start and (next_start is None or segment["start"] < next_start)
        ]
        result.append(
            {
                "chapter_id": chapter_id(ep, chapter, index),
                "title": str(chapter.get("title") or ""),
                "start": start,
                "end": next_start,
                "agenda_text": agenda_items.get(index, ""),
                "transcript_text": "\n".join(segment["text"] for segment in local),
                "transcript_segments": local,
            }
        )
    return result


def agenda_item_context(ep: Any, storage: Any = None) -> dict[int, str]:
    """Read only explicit chapter-index associations from R3's backup manifest.

    The current R3 writer often has only a flat agenda/packet text and link labels. Those are
    intentionally not guessed into chapter assignments. This consumer is ready for a future R3
    structured item mapping (``chapter_index`` + ``text``/``item_text``) and otherwise returns no
    per-chapter agenda context.
    """
    links = ep.links or {}
    data = _read_storage_bytes(storage, links.get("agenda_backup_artifact_key"))
    if not data:
        return {}
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    items = payload.get("items") or payload.get("links") if isinstance(payload, dict) else []
    if not isinstance(items, list):
        return {}
    result: dict[int, str] = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("chapter_index"), int):
            continue
        text = item.get("item_text") or item.get("text")
        if isinstance(text, str) and text.strip():
            result[item["chapter_index"]] = text[:20_000]
    return result


def episode_tag_inputs(ep: Any, storage: Any = None) -> tuple[str, str, str]:
    titles = "\n".join(
        str(chapter.get("title") or "")
        for chapter in episode_served_chapters(ep)
        if isinstance(chapter, dict) and chapter.get("title")
    )
    links = ep.links or {}
    agenda_data = _read_storage_bytes(storage, links.get("agenda_text_artifact_key"))
    agenda_text = agenda_data.decode("utf-8", errors="replace") if agenda_data else ""
    transcript_data = _read_storage_bytes(storage, ep.transcript_key)
    transcript = _text_from_transcript(transcript_data, ep.transcript_format)
    return titles, agenda_text, transcript


def agenda_document_context(ep: Any) -> list[dict[str, str]]:
    """Return official document links that may be cited by an LLM evidence item."""
    links = ep.links or {}
    candidates = [
        ("Agenda", ep.agenda_text_url),
        ("Agenda backup", ep.agenda_backup_url),
        ("Agenda", links.get("agenda")),
        ("Agenda", links.get("agenda_url")),
        ("Agenda backup", links.get("agenda_backup")),
    ]
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for title, url in candidates:
        if not isinstance(url, str) or not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        result.append({"title": title, "url": url})
    return result


def _contains_text(text: str, quote: str) -> bool:
    def normalize(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip().casefold()

    return bool(quote.strip()) and normalize(quote) in normalize(text)


def _transcript_region(
    quote: str, segments: list[dict[str, Any]]
) -> tuple[float | None, float | None]:
    """Find a quoted transcript region without trusting model-supplied offsets.

    Locates the quote as a contiguous span in the normalized, joined segment text and traces
    its exact start/end offsets back to the segments that contain them. This replaces an
    earlier heuristic that matched any segment containing just the quote's first or last word,
    which let a common word (e.g. "the") pull in unrelated segments from anywhere in the
    transcript and produce a bogus, episode-spanning timestamp range.
    """

    def normalize(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip().casefold()

    normalized_quote = normalize(quote)
    if not segments or not normalized_quote:
        return None, None

    pieces: list[str] = []
    owners: list[int] = []
    for index, item in enumerate(segments):
        text = normalize(str(item.get("text") or ""))
        if not text:
            continue
        if pieces:
            pieces.append(" ")
            owners.append(index)
        pieces.append(text)
        owners.extend([index] * len(text))
    joined = "".join(pieces)
    match_start = joined.find(normalized_quote)
    if match_start == -1:
        return None, None
    match_end = min(match_start + len(normalized_quote) - 1, len(owners) - 1)

    start_segment = segments[owners[match_start]]
    end_segment = segments[owners[match_end]]
    start = start_segment.get("start")
    end = end_segment.get("end")
    if end is None:
        end = end_segment.get("start")
    return start, end


def ensure_llm_contract():
    """Register the "topic-tags" structured-output contract, lazily (the optional Pydantic import
    stays out of the module's top-level cost) and idempotently (safe to call repeatedly).

    Public — not just an internal helper for :func:`llm_tag_suggestions` — because anything that
    calls :func:`citypods.compute.llm.LiteLLMBackend.reconcile` on a deferred "tag" job handle
    (e.g. ``scripts/llm_deferred_sweep.py``) needs this contract registered in *its own* process
    first, or ``response_model("topic-tags")`` raises. Discovery's contract registers as an
    import-time side effect instead (`citypods/discovery/classify.py`); this one does not, so it
    needs its own explicit call site wherever reconciliation can happen outside `tags.py` itself.
    """
    from typing import Literal as _Literal

    from pydantic import BaseModel, ConfigDict, Field

    from citypods.compute.structured import register_response_model

    class Evidence(BaseModel):
        model_config = ConfigDict(extra="forbid")
        where: _Literal["agenda", "transcript"]
        quote: str = Field(min_length=3, max_length=1200)
        start: float | None = Field(default=None, ge=0.0)
        end: float | None = Field(default=None, ge=0.0)
        document_url: str | None = None
        document_locator: str | None = Field(default=None, max_length=200)
        chapter_id: str | None = None

    class Suggestion(BaseModel):
        model_config = ConfigDict(extra="forbid")
        id: str = Field(min_length=1, max_length=100)
        chapter_id: str | None = None
        confidence: float = Field(ge=0.0, le=1.0)
        explanation: str = Field(min_length=1, max_length=500)
        evidence: list[Evidence] = Field(default_factory=list, max_length=8)

    class Response(BaseModel):
        model_config = ConfigDict(extra="forbid")
        tags: list[Suggestion] = Field(default_factory=list, max_length=20)

    # The module-level cache is intentionally attached to the function so the optional Pydantic
    # import remains lazy and the shared registry still rejects accidental duplicate contracts.
    model = getattr(ensure_llm_contract, "model", None)
    if model is None:
        model = Response
        register_response_model(LLM_CONTRACT, model)
        ensure_llm_contract.model = model
    return model


def llm_tag_suggestions(
    backend: Any,
    *,
    taxonomy: Taxonomy,
    agenda_item_titles: str,
    agenda_text: str,
    transcript_text: str,
    recipe_hash: str,
    chapter_inputs: list[dict[str, Any]] | None = None,
    agenda_documents: list[dict[str, str]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], bool, str | None]:
    """Run and validate additive suggestions; return episode tags, chapter tags, dispatched, and
    the resolved model that produced them (``None`` when dispatched/unresolved)."""
    model = ensure_llm_contract()
    from citypods.compute.base import InferenceJob, JobHandle, JobResult
    from citypods.compute.llm_policy import LLMRequestPolicy

    allowed = taxonomy.by_id
    material = {
        "agenda_item_titles": agenda_item_titles[:40_000],
        "agenda_text": agenda_text[:40_000],
        "transcript_text": transcript_text[:100_000],
        "agenda_documents": agenda_documents or [],
        "taxonomy": [
            {"id": tag.id, "label": tag.label, "description": tag.description}
            for tag in taxonomy.tags
        ],
        "chapters": [
            {
                "chapter_id": item["chapter_id"],
                "title": item["title"],
                "agenda_text": item.get("agenda_text", "")[:20_000],
                "transcript_text": item.get("transcript_text", "")[:30_000],
                "transcript_segments": [
                    {
                        "start": segment.get("start"),
                        "end": segment.get("end"),
                        "text": str(segment.get("text") or "")[:1200],
                    }
                    for segment in item.get("transcript_segments", [])[:200]
                    if isinstance(segment, dict)
                ],
            }
            for item in (chapter_inputs or [])
        ],
    }
    messages = [
        {
            "role": "system",
            "content": (
                "Return only topic tags from the supplied taxonomy. Use an existing id exactly. "
                "Suggest a tag only when the evidence supports it; never invent ids. Every "
                "evidence item must include a short exact quote copied from the supplied agenda "
                "or transcript. Transcript evidence should include start/end seconds when the "
                "chapter segments provide them. Agenda evidence may cite only a supplied document "
                "URL and should include a section/page locator when one is known."
            ),
        },
        {"role": "user", "content": json.dumps(material, ensure_ascii=False)},
    ]
    inputs: dict[str, Any] = {"messages": messages, "structured_output": LLM_CONTRACT}
    backend_storage = getattr(backend, "storage", None)
    backend_model = getattr(getattr(backend, "config", None), "model", None)
    if backend_model and getattr(backend_storage, "cas_capable", False):
        # Pin the scheduler to exactly the one model R5 is calibrated against
        # (config/site_config.yml's tagging.llm_model): the calibration matrix and fallback
        # config are keyed to one exact provider/model route, and letting the scheduler roam
        # across models would fragment calibration across separately-unreviewed routes instead of
        # deepening review of the one route R5 was designed around. Still gains real value from
        # the R13 scheduler even pinned: CAS-safe quota accounting across concurrent shards and a
        # clean deferral (a JobHandle, retried on this stage's own next scheduled run) instead of
        # a raw provider error. Omitted entirely when the backend's storage isn't CAS-capable
        # (e.g. local dev/dry runs with no R2 configured), so tagging keeps working there exactly
        # as it did before R13 rather than failing for lack of scheduler storage.
        inputs["llm_policy"] = LLMRequestPolicy(
            allowed_models=(backend_model,),
            allow_paid=False,
            purpose=TAG_FEATURE,
        )
    outcome = backend.run_inference(
        InferenceJob(
            task="tag",
            inputs=inputs,
            recipe_hash=recipe_hash,
        )
    )
    if isinstance(outcome, JobHandle):
        return [], {}, True, None
    if not isinstance(outcome, JobResult) or not isinstance(outcome.output, dict):
        raise ValueError("LLM tag backend returned an invalid result")
    choices = outcome.output.get("choices")
    content = (
        choices[0].get("message", {}).get("content")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict)
        else None
    )
    if not isinstance(content, str):
        raise ValueError("LLM tag backend returned no structured content")
    response = model.model_validate_json(content)
    chapter_by_id = {item["chapter_id"]: item for item in (chapter_inputs or [])}
    source_text = {"agenda": agenda_item_titles + "\n" + agenda_text, "transcript": transcript_text}
    transcript_segments = [
        segment
        for chapter in (chapter_inputs or [])
        for segment in chapter.get("transcript_segments", [])
        if isinstance(segment, dict)
    ]
    allowed_document_urls = {item.get("url") for item in (agenda_documents or [])}
    suggestions: list[dict[str, Any]] = []
    chapter_suggestions: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[str | None, str]] = set()
    for suggestion in response.tags:
        if suggestion.id not in allowed:
            continue
        if suggestion.chapter_id is not None and suggestion.chapter_id not in chapter_by_id:
            continue
        scope_key = (suggestion.chapter_id, suggestion.id)
        if scope_key in seen:
            continue
        seen.add(scope_key)
        evidence = []
        for item in suggestion.evidence:
            if item.chapter_id not in {None, suggestion.chapter_id}:
                continue
            if suggestion.chapter_id is not None and item.where == "transcript":
                text = chapter_by_id[suggestion.chapter_id].get("transcript_text", "")
            elif suggestion.chapter_id is not None and item.where == "agenda":
                chapter = chapter_by_id[suggestion.chapter_id]
                text = chapter.get("title", "") + "\n" + chapter.get("agenda_text", "")
            else:
                text = source_text[item.where]
            if _contains_text(text, item.quote):
                evidence_item = {"where": item.where, "quote": item.quote}
                if suggestion.chapter_id is not None:
                    evidence_item["chapter_id"] = suggestion.chapter_id
                if item.where == "transcript":
                    local_segments = (
                        chapter_by_id[suggestion.chapter_id].get("transcript_segments", [])
                        if suggestion.chapter_id is not None
                        else transcript_segments
                    )
                    start, end = _transcript_region(item.quote, local_segments)
                    if start is not None:
                        evidence_item["start"] = start
                    if end is not None:
                        evidence_item["end"] = end
                elif item.document_url in allowed_document_urls:
                    evidence_item["document_url"] = item.document_url
                    if item.document_locator:
                        evidence_item["document_locator"] = item.document_locator
                evidence.append(evidence_item)
        if not evidence:
            continue
        value = {
            "id": suggestion.id,
            "source": "llm",
            "confidence": float(suggestion.confidence),
            "explanation": suggestion.explanation,
            "evidence": evidence,
        }
        if suggestion.chapter_id is None:
            suggestions.append(value)
        else:
            chapter_suggestions.setdefault(suggestion.chapter_id, []).append(value)
    return suggestions, chapter_suggestions, False, outcome.model


def decorate_llm_candidates(
    candidates: list[dict[str, Any]],
    *,
    episode_uid: str | None,
    episode_title: str,
    provider_model: str,
    taxonomy: Taxonomy,
    recipe_hash: str,
    prompt_version: str = TAG_PROMPT_VERSION,
) -> list[dict[str, Any]]:
    """Attach generic evaluator dimensions to validated feature-specific suggestions."""
    from citypods.llm_evaluation import candidate_id

    result: list[dict[str, Any]] = []
    for candidate in candidates:
        value = {
            **candidate,
            "feature": TAG_FEATURE,
            "provider_model": provider_model,
            "prompt_version": prompt_version,
            "taxonomy_version": taxonomy.version,
            "recipe_hash": recipe_hash,
            "episode_uid": episode_uid,
            "episode_title": episode_title,
            "scope": "chapter" if candidate.get("chapter_id") else "episode",
        }
        value["candidate_id"] = candidate_id(value)
        result.append(value)
    return result


def merge_tag_sources(
    rule_tags: list[dict[str, Any]], llm_tags: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    result = [dict(tag) for tag in rule_tags]
    by_id = {tag["id"]: tag for tag in result}
    for tag in llm_tags:
        existing = by_id.get(tag["id"])
        if existing is None:
            result.append(dict(tag))
            by_id[tag["id"]] = result[-1]
            continue
        evidence = existing.setdefault("evidence", [])
        for item in tag.get("evidence", []):
            if item not in evidence:
                evidence.append(item)
        if tag.get("explanation") and not existing.get("explanation"):
            existing["explanation"] = tag["explanation"]
    return result


def rollup_tags(
    episode_tags: list[dict[str, Any]],
    chapter_annotations: list[dict[str, Any]],
    taxonomy: Taxonomy,
) -> list[dict[str, Any]]:
    """Derive the episode facet list from annotations in taxonomy order.

    This is a projection, not an incremental update. Re-running it from the stored annotations
    gives the same result and makes a missing-chapter fallback explicit.
    """
    by_id: dict[str, dict[str, Any]] = {}

    def add(tag: dict[str, Any], chapter: str | None = None) -> None:
        value = dict(tag)
        evidence = [dict(item) for item in value.get("evidence", [])]
        if chapter:
            evidence = [
                {**item, "chapter_id": chapter} if "chapter_id" not in item else item
                for item in evidence
            ]
        value["evidence"] = evidence
        existing = by_id.get(value.get("id"))
        if existing is None:
            by_id[value["id"]] = value
            return
        existing_evidence = existing.setdefault("evidence", [])
        for item in evidence:
            if item not in existing_evidence:
                existing_evidence.append(item)
        if value.get("source") == "rule":
            existing["source"] = "rule"
            existing["confidence"] = 1.0
        elif existing.get("source") != "rule":
            new_confidence = value.get("confidence")
            existing_confidence = existing.get("confidence")
            if isinstance(new_confidence, int | float) and (
                not isinstance(existing_confidence, int | float)
                or new_confidence > existing_confidence
            ):
                existing["source"] = value.get("source", existing.get("source"))
                existing["confidence"] = new_confidence
        if value.get("explanation") and not existing.get("explanation"):
            existing["explanation"] = value["explanation"]

    for tag in episode_tags:
        add(tag)
    for annotation in chapter_annotations:
        chapter = annotation.get("chapter_id")
        for tag in annotation.get("tags") or []:
            add(tag, chapter)
    order = {tag.id: index for index, tag in enumerate(taxonomy.tags)}
    return sorted(by_id.values(), key=lambda tag: order.get(tag.get("id"), len(order)))


__all__ = [
    "LLM_CONTRACT",
    "TAG_FEATURE",
    "TAG_PROMPT_VERSION",
    "TAGGER_VERSION",
    "Taxonomy",
    "TaxonomyTag",
    "episode_tag_inputs",
    "agenda_item_context",
    "chapter_id",
    "chapter_tag_inputs",
    "decorate_llm_candidates",
    "agenda_document_context",
    "ensure_llm_contract",
    "load_taxonomy",
    "merge_tag_sources",
    "rollup_tags",
    "tag_episode",
    "tag_recipe_hash",
    "taxonomy_from_dict",
]
