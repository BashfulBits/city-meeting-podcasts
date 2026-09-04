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

from citypods.agenda_text import agenda_chapter_eligible, resolve_chapter_spans
from citypods.chapters import episode_served_chapters

TAGGER_VERSION = "2"
CHAPTER_PIPELINE_VERSION = "1"
LLM_CONTRACT = "topic-tags"
PRELABELER_CONTRACT = "topic-tags-prelabeler"
TAG_PROMPT_VERSION = "3"
# Bump when the serialized structured-output request schema changes in a way that must create
# fresh durable dispatch requests. This is intentionally separate from the prompt version: the
# current migration changes provider-compatible schema keywords, not task instructions.
TAG_LLM_SCHEMA_VERSION = "2"
PRELABELER_PROMPT_VERSION = "1"
TAG_FEATURE = "topic-tags"
PRELABELER_DECISIONS = ("likely_correct", "needs_human_review", "likely_incorrect")
# Keep a full megabyte beneath the Worker's 8 MiB JSON-body ceiling for the structured-output
# schema and future envelope fields. This is a transport guard, separate from model context.
TAGGER_MAX_REQUEST_BYTES = 7 * 1024 * 1024


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


def tag_episode(
    agenda_item_titles: str,
    transcript_text: str,
    taxonomy: Taxonomy,
    *,
    include_rule_metadata: bool = False,
) -> list[dict[str, Any]]:
    """Return deterministic rule tags in stable taxonomy order.

    The default shape remains backwards compatible. The stage opts into authored phrase metadata
    so audits can distinguish the configured rule from the exact source text that matched it.
    """
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
        seen_evidence: set[tuple[str, str]] = set()
        matched_patterns: list[str] = []
        matched_texts: list[str] = []
        audit_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for where, text in inputs:
            if not text:
                continue
            for pattern in tag.include:
                for match in _literal_pattern(pattern).finditer(text):
                    matched = match.group(0)
                    evidence_key = (where, matched.casefold())
                    if evidence_key not in seen_evidence:
                        seen_evidence.add(evidence_key)
                        evidence.append(TagEvidence(where=where, span=matched))
                    if include_rule_metadata:
                        if pattern not in matched_patterns:
                            matched_patterns.append(pattern)
                        if matched not in matched_texts:
                            matched_texts.append(matched)
                        audit = audit_by_key.setdefault(
                            (pattern, where),
                            {
                                "tag_id": tag.id,
                                "kind": "include",
                                "pattern": pattern,
                                "where": where,
                                "match_count": 0,
                                "match_texts": [],
                            },
                        )
                        audit["match_count"] += 1
                        if matched not in audit["match_texts"] and len(audit["match_texts"]) < 3:
                            audit["match_texts"].append(matched)
        if evidence:
            value = {
                "id": tag.id,
                "source": "rule",
                "confidence": 1.0,
                "evidence": [item.as_dict() for item in evidence[:8]],
            }
            if include_rule_metadata:
                value["rule_patterns"] = matched_patterns
                value["rule_match_texts"] = matched_texts
                value["rule_audit"] = list(audit_by_key.values())
            result.append(value)
    return result


def rule_phrase_audit(
    agenda_text: str,
    transcript_text: str,
    taxonomy: Taxonomy,
    *,
    include: bool = True,
    exclude: bool = True,
) -> list[dict[str, Any]]:
    """Return observed authored include/exclude phrase hits for deterministic-rule reporting.

    This is deliberately an observation helper, not another candidate source.  Exclude hits are
    retained even though they prevent a rule candidate from being emitted, so the review report can
    distinguish "the phrase was never seen" from "the phrase was seen but globally suppressed".
    """
    inputs = (("agenda", agenda_text or ""), ("transcript", transcript_text or ""))
    # Persisted telemetry must remain bounded even when a boilerplate phrase occurs hundreds of
    # times.  The report needs the phrase, source, and frequency, not every copy of source text.
    observations: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for tag in taxonomy.tags:
        sources = []
        if include:
            sources.append(("include", tag.include))
        if exclude:
            sources.append(("exclude", tag.exclude))
        for kind, patterns in sources:
            for pattern in patterns:
                compiled = _literal_pattern(pattern)
                for where, text in inputs:
                    for match in compiled.finditer(text):
                        key = (tag.id, kind, pattern, where)
                        observation = observations.setdefault(
                            key,
                            {
                                "tag_id": tag.id,
                                "kind": kind,
                                "pattern": pattern,
                                "where": where,
                                "match_count": 0,
                                "match_texts": [],
                            },
                        )
                        observation["match_count"] += 1
                        matched = match.group(0)
                        if (
                            matched not in observation["match_texts"]
                            and len(observation["match_texts"]) < 3
                        ):
                            observation["match_texts"].append(matched)
    return list(observations.values())


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
    llm_schema_version: str = TAG_LLM_SCHEMA_VERSION,
    admission_policy: str = "",
    chapter_pipeline_version: str = CHAPTER_PIPELINE_VERSION,
) -> str:
    payload = {
        "taxonomy": taxonomy.version,
        "tagger": TAGGER_VERSION,
        "chapter_pipeline_version": chapter_pipeline_version,
        "llm": llm_enabled,
        "llm_route": llm_route if llm_enabled else "",
        "prompt_version": prompt_version if llm_enabled else "",
        "llm_schema_version": llm_schema_version if llm_enabled else "",
        "admission_policy": admission_policy if llm_enabled else "",
        "agenda_item_titles": agenda_item_titles,
        "agenda_text": agenda_text,
        "transcript": transcript_text,
        "chapters": chapter_inputs or [],
    }
    return hashlib.sha1(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()[:16]


def tag_input_fingerprint(
    ep: Any,
    taxonomy: Taxonomy,
    *,
    llm_enabled: bool,
    llm_route: str = "",
    prompt_version: str = TAG_PROMPT_VERSION,
    llm_schema_version: str = TAG_LLM_SCHEMA_VERSION,
    admission_policy: str = "",
    chapter_pipeline_version: str = CHAPTER_PIPELINE_VERSION,
) -> str:
    """Cheap, storage-I/O-free stand-in for what :func:`tag_recipe_hash` would eventually hash.

    ``agenda_text``/``transcript_text`` (episode- and chapter-scoped) are pure functions of the
    *content-addressed* artifact keys (``agenda_text_artifact_key``, ``agenda_backup_artifact_key``,
    ``ep.transcript_key``) plus the served chapter boundaries -- all already sitting on ``ep`` in
    memory. Content addressing guarantees a given key always resolves to the same bytes, so hashing
    these keys is exactly as sensitive to a real input change as hashing the decoded text they point
    at, without paying a storage round trip (fetch + ffprobe/parse) for every candidate episode on
    every run just to find out most of them haven't changed.

    This is a fast pre-check gate only (``TagsStage``): a match means "the real recipe hash would
    come out the same as last time, skip re-deriving it," never a substitute for the real
    ``tag_recipe_hash`` value that ``tags_spec_hash``/``tags_llm_recipe_hash`` are actually keyed
    on.
    """
    links = ep.links or {}
    chapters = [
        chapter
        for chapter in episode_served_chapters(ep, with_source_index=True)
        if isinstance(chapter, dict)
    ]
    chapter_fingerprint = [
        {
            "chapter_id": chapter_id(ep, chapter, index),
            "title": str(chapter.get("title") or ""),
            "start": chapter.get("start"),
            "source_index": chapter.get("source_index"),
        }
        for index, chapter in enumerate(chapters)
    ]
    payload = {
        "taxonomy": taxonomy.version,
        "tagger": TAGGER_VERSION,
        "chapter_pipeline_version": chapter_pipeline_version,
        "llm": llm_enabled,
        "llm_route": llm_route if llm_enabled else "",
        "prompt_version": prompt_version if llm_enabled else "",
        "llm_schema_version": llm_schema_version if llm_enabled else "",
        "admission_policy": admission_policy if llm_enabled else "",
        "agenda_text_artifact_key": links.get("agenda_text_artifact_key"),
        "agenda_backup_artifact_key": links.get("agenda_backup_artifact_key"),
        "agenda_quality": {
            "status": (getattr(ep, "agenda_text_quality", None) or {}).get("status"),
            "eligibility": (getattr(ep, "agenda_text_quality", None) or {}).get("eligibility"),
            "pipeline_version": (getattr(ep, "agenda_text_quality", None) or {}).get(
                "pipeline_version"
            ),
        },
        "transcript_key": ep.transcript_key,
        "transcript_format": ep.transcript_format,
        "chapters": chapter_fingerprint,
    }
    return hashlib.sha1(
        json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


def episode_needs_tagging(
    ep: Any,
    taxonomy: Taxonomy,
    *,
    llm_enabled: bool,
    llm_route: str = "",
    prompt_version: str = TAG_PROMPT_VERSION,
    admission_policy: str = "",
    prelabeler_enabled: bool = False,
    prelabeler_model: str = "",
    prelabeler_prompt_version: str = "1",
    prelabeler_llm_schema_version: str = "1",
) -> bool:
    """Return whether this episode requires rule-tag derivation, LLM suggestion dispatch,
    or chapter tagging.

    An episode is fully current (returns False) when its cached tags_input_fingerprint matches
    the current inputs, its tags_spec_hash is populated, chapter tags exist if chapters are present,
    and (if LLM is enabled) its tags_llm_recipe_hash is populated. When the pre-labeler is
    enabled, active rule/chapter candidates must also carry the current pre-labeler metadata.
    """
    has_chapters = bool(episode_served_chapters(ep))
    cheap_fingerprint = tag_input_fingerprint(
        ep,
        taxonomy,
        llm_enabled=llm_enabled,
        llm_route=llm_route,
        prompt_version=prompt_version,
        admission_policy=admission_policy,
    )
    inputs_unchanged = (
        ep.tags_input_fingerprint is not None
        and ep.tags_input_fingerprint == cheap_fingerprint
        and ep.tags_spec_hash is not None
        and (not has_chapters or ep.chapter_tags)
    )
    llm_pending = llm_enabled and ep.tags_llm_recipe_hash is None
    prelabeler_pending = prelabeler_enabled and any(
        candidate.get("candidate_state") != "historical"
        and (candidate.get("source_kind", "llm") == "rule" or candidate.get("chapter_id"))
        and (
            candidate.get("prelabeler_model") != prelabeler_model
            or candidate.get("prelabeler_prompt_version") != prelabeler_prompt_version
            or candidate.get("prelabeler_llm_schema_version") != prelabeler_llm_schema_version
            or candidate.get("prelabeler_decision")
            not in {"likely_correct", "needs_human_review", "likely_incorrect"}
        )
        for candidate in (ep.llm_tag_candidates or [])
        if isinstance(candidate, dict)
    )
    return not (inputs_unchanged and not llm_pending and not prelabeler_pending)


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
    position in ``ep.source_chapters`` once an earlier chapter has been dropped or snapped by
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
        # agenda_item_context() is keyed by source chapter_index (from R3's manifest), not served
        # position -- the same source_index vs. served-position desync chapter_id() already guards
        # against below. Without this, a remap() that dropped or snapped an earlier chapter would
        # attach a surviving chapter's agenda evidence/tags to whatever chapter now lands at its
        # old served index instead of its own.
        agenda_index = chapter.get("source_index")
        if not isinstance(agenda_index, int):
            agenda_index = index
        result.append(
            {
                "chapter_id": chapter_id(ep, chapter, index),
                "title": str(chapter.get("title") or ""),
                "start": start,
                "end": next_start,
                "agenda_text": agenda_items.get(agenda_index, ""),
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
    if not agenda_chapter_eligible(getattr(ep, "agenda_text_quality", None)):
        return {}
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


def _strip_preamble(agenda_text: str, chapter_titles: list[str]) -> str:
    """Drop meeting-notice boilerplate/hearing-procedure text that precedes the first agenda
    item's title -- confirmed, on a real production false positive (GH issue #1068, a
    neighborhood-engagement tag fired on standard hearing sign-up instructions), to sit before the
    first resolved chapter title and nowhere else. Falls back to the untouched text when no
    chapter title resolves at all, rather than guessing where an item boundary is."""
    if not chapter_titles:
        return agenda_text
    # resolve_chapter_spans()'s offsets are only valid against its own whitespace-normalized,
    # casefolded copy -- slicing the ORIGINAL agenda_text with those offsets would silently
    # replace it with lowercase, whitespace-collapsed text. That text is what rule-tag evidence
    # spans are captured from and what the LLM quotes back as "exact quote copied from the
    # supplied agenda", so it must stay verbatim. Re-locate the same matched title in the
    # original text (tolerating whitespace differences, not case) and slice from there instead.
    _, spans = resolve_chapter_spans(agenda_text, chapter_titles)
    first_title = next(
        (title for title, span in zip(chapter_titles, spans, strict=False) if span is not None),
        None,
    )
    if first_title is None:
        return agenda_text
    pattern = r"\s+".join(re.escape(part) for part in first_title.split())
    match = re.search(pattern, agenda_text, re.IGNORECASE)
    return agenda_text[match.start() :] if match else agenda_text


def _episode_backup_text(ep: Any, storage: Any = None) -> str:
    """Concatenate all backup/attachment document text from the agenda_backup manifest, for
    episode-level tagging context. Unlike agenda_item_context() (which only surfaces text with a
    resolved chapter_index, for per-chapter attribution), this includes every extracted item's
    text regardless of attribution -- an unattributed backup document (e.g. a generic staff
    report the content-match heuristic couldn't tie to one item) still describes something real
    about the episode and should not be silently dropped from episode-level tagging."""
    links = ep.links or {}
    data = _read_storage_bytes(storage, links.get("agenda_backup_artifact_key"))
    if not data:
        return ""
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    items = payload.get("items") or payload.get("links") if isinstance(payload, dict) else []
    if not isinstance(items, list):
        return ""
    parts: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = item.get("item_text") or item.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip()[:20_000])
    return "\n\n".join(parts)


def episode_tag_inputs(ep: Any, storage: Any = None) -> tuple[str, str, str]:
    chapter_titles = [
        str(chapter.get("title") or "")
        for chapter in episode_served_chapters(ep)
        if isinstance(chapter, dict) and chapter.get("title")
    ]
    titles = "\n".join(chapter_titles)
    links = ep.links or {}
    agenda_data = _read_storage_bytes(storage, links.get("agenda_text_artifact_key"))
    agenda_text = agenda_data.decode("utf-8", errors="replace") if agenda_data else ""
    quality = getattr(ep, "agenda_text_quality", None)
    if agenda_text and not agenda_chapter_eligible(quality):
        agenda_text = ""
    if agenda_text:
        agenda_text = _strip_preamble(agenda_text, chapter_titles)
    backup_text = _episode_backup_text(ep, storage) if agenda_chapter_eligible(quality) else ""
    if backup_text:
        agenda_text = f"{agenda_text}\n\n--- backup/attachment documents ---\n\n{backup_text}"
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


def _excerpt_around(text: str, quote: str, *, limit: int) -> str:
    """Return a bounded excerpt centered on a quoted piece of source when possible."""
    text = str(text or "")
    if len(text) <= limit:
        return text
    needle = str(quote or "").strip()
    position = text.casefold().find(needle.casefold()) if needle else -1
    if position < 0:
        return text[:limit]
    half = max(1, (limit - len(needle)) // 2)
    start = max(0, position - half)
    end = min(len(text), start + limit)
    start = max(0, end - limit)
    return text[start:end]


def _prelabel_source_excerpt(
    candidate: dict[str, Any],
    chapter: dict[str, Any] | None,
    *,
    fallback_agenda: str,
    fallback_transcript: str,
) -> str:
    """Build reduced, evidence-centered context for the independent evaluator."""
    evidence = candidate.get("evidence") or []
    chapter = chapter or {}
    title = str(chapter.get("title") or "")
    agenda = str(chapter.get("agenda_text") or fallback_agenda or "")
    transcript = str(chapter.get("transcript_text") or fallback_transcript or "")
    segments = [item for item in chapter.get("transcript_segments") or [] if isinstance(item, dict)]
    parts = [f"Chapter title: {title}" if title else ""]
    agenda_quotes = [
        str(item.get("quote") or item.get("span") or "")
        for item in evidence
        if item.get("where") == "agenda"
    ]
    transcript_quotes = [
        str(item.get("quote") or item.get("span") or "")
        for item in evidence
        if item.get("where") == "transcript"
    ]
    if agenda:
        quote = next((item for item in agenda_quotes if item), "")
        parts.append("Agenda evidence:\n" + _excerpt_around(agenda, quote, limit=4000))
    if transcript:
        chosen_segments: list[dict[str, Any]] = []
        for item in evidence:
            if item.get("where") != "transcript":
                continue
            start = item.get("start")
            end = item.get("end") if item.get("end") is not None else start
            if isinstance(start, (int, float)) and segments:
                matching = [
                    index
                    for index, segment in enumerate(segments)
                    if isinstance(segment.get("start"), (int, float))
                    and isinstance(segment.get("end", segment.get("start")), (int, float))
                    and float(segment.get("start")) <= float(end or start)
                    and float(segment.get("end", segment.get("start"))) >= float(start)
                ]
                for index in matching:
                    chosen_segments.extend(segments[max(0, index - 1) : index + 2])
        if chosen_segments:
            seen: set[int] = set()
            selected_text: list[str] = []
            for item in chosen_segments:
                item_id = id(item)
                if item_id in seen:
                    continue
                seen.add(item_id)
                selected_text.append(str(item.get("text") or ""))
            text = "\n".join(selected_text)
        else:
            quote = next((item for item in transcript_quotes if item), "")
            text = _excerpt_around(transcript, quote, limit=7000)
        parts.append("Transcript evidence:\n" + text)
    if len(parts) == 1 and (fallback_agenda or fallback_transcript):
        parts.append(
            "Source context:\n"
            + _excerpt_around(
                f"{fallback_agenda}\n{fallback_transcript}",
                next((str(item.get("quote") or item.get("span") or "") for item in evidence), ""),
                limit=11000,
            )
        )
    return "\n\n".join(item for item in parts if item)[:12_000]


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
    cached = getattr(ensure_llm_contract, "model", None)
    if cached is not None:
        return cached

    from citypods.compute.structured import register_response_model, response_model

    try:
        model = response_model(LLM_CONTRACT)
        ensure_llm_contract.model = model
        return model
    except ValueError:
        pass

    from typing import Literal as _Literal

    from pydantic import BaseModel, ConfigDict, Field

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

    model = register_response_model(LLM_CONTRACT, Response)
    ensure_llm_contract.model = model
    return model


def ensure_prelabeler_contract():
    """Register the independent, discrete candidate-review response contract."""
    cached = getattr(ensure_prelabeler_contract, "model", None)
    if cached is not None:
        return cached

    from citypods.compute.structured import register_response_model, response_model

    try:
        model = response_model(PRELABELER_CONTRACT)
        ensure_prelabeler_contract.model = model
        return model
    except ValueError:
        pass

    from typing import Literal as _Literal

    from pydantic import BaseModel, ConfigDict, Field

    class Assessment(BaseModel):
        model_config = ConfigDict(extra="forbid")
        candidate_id: str = Field(min_length=1, max_length=100)
        decision: _Literal[PRELABELER_DECISIONS]
        confidence: float = Field(ge=0.0, le=1.0)
        reason: str = Field(min_length=1, max_length=500)
        evidence_supported: bool

    class Response(BaseModel):
        model_config = ConfigDict(extra="forbid")
        assessments: list[Assessment] = Field(default_factory=list, max_length=100)

    model = register_response_model(PRELABELER_CONTRACT, Response)
    ensure_prelabeler_contract.model = model
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
    allow_paid: bool = False,
    purpose: str = TAG_FEATURE,
    deadline_at: Any | None = None,
    call_metadata_out: dict[str, Any] | None = None,
    _batched: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], bool, str | None]:
    """Run and validate additive suggestions; return episode tags, chapter tags, dispatched, and
    the resolved model that produced them (``None`` when dispatched/unresolved). When ``dispatched``
    is true because the material could never fit any allowed route's real token budget (not an
    ordinary "no capacity this minute" defer), the fourth element is instead the literal string
    ``"payload-too-large"`` -- callers (``TagsStage``) must not treat that as evidence of genuine
    quota exhaustion for the whole run, since it says nothing about remaining capacity."""
    model = ensure_llm_contract()
    from citypods.compute.base import InferenceJob, JobHandle, JobResult
    from citypods.compute.llm_policy import (
        ROUTE_CANDIDATES,
        ROUTES,
        LLMRequestPolicy,
        estimate_tokens,
    )

    allowed = taxonomy.by_id
    chapter_mode = bool(chapter_inputs)
    compact_chapters = [
        {
            "chapter_id": item["chapter_id"],
            "title": item["title"],
            "agenda_text": str(item.get("agenda_text") or ""),
            "transcript_text": str(item.get("transcript_text") or ""),
            "transcript_segments": [
                {
                    "start": segment.get("start"),
                    "end": segment.get("end"),
                    "text": str(segment.get("text") or ""),
                }
                for segment in item.get("transcript_segments", [])
                if isinstance(segment, dict)
            ],
        }
        for item in (chapter_inputs or [])
    ]

    def messages_for(chapters: list[dict[str, Any]]) -> list[dict[str, str]]:
        # Chapter-only tagging must not smuggle the episode-wide backup packet into every batch.
        # `agenda_text` below is only explicit chapter-associated evidence from agenda_item_context.
        material = {
            "agenda_item_titles": "" if chapter_mode else agenda_item_titles[:40_000],
            "agenda_text": "" if chapter_mode else agenda_text,
            "transcript_text": "" if chapter_mode else transcript_text[:100_000],
            "agenda_documents": agenda_documents or [],
            "taxonomy": [
                {"id": tag.id, "label": tag.label, "description": tag.description}
                for tag in taxonomy.tags
            ],
            "chapters": chapters,
        }
        return [
            {
                "role": "system",
                "content": (
                    "Return only topic tags from the supplied taxonomy. Use an existing id "
                    "exactly. Suggest a tag only when the evidence supports it; never invent "
                    "ids. A tag's "
                    "description may explicitly say what it should NOT be used for (often to "
                    "distinguish it from a similarly-worded tag) -- follow that guidance precisely "
                    "even when the material otherwise seems to match. Every evidence item must "
                    "include a short exact quote copied from the supplied agenda or transcript. "
                    "Transcript evidence should include start/end seconds when the chapter "
                    "segments provide them. Agenda evidence may cite only a supplied document URL "
                    "and should "
                    "include a section/page locator when one is known."
                ),
            },
            {"role": "user", "content": json.dumps(material, ensure_ascii=False)},
        ]

    messages = messages_for(compact_chapters)
    request_bytes = len(
        json.dumps(
            {
                "model": getattr(getattr(backend, "config", None), "model", ""),
                "messages": messages,
                "max_tokens": 1024,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    )
    input_tokens_estimate = estimate_tokens(messages)
    input_digest = hashlib.sha1(
        json.dumps(messages, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()[:16]
    # Chapter requests retain their complete mapped evidence.  Only the legacy episode request
    # format applies its explicit bounded-context policy above.
    tagger_truncated = not chapter_mode and (
        len(agenda_item_titles) > 40_000 or len(transcript_text) > 100_000
    )
    call_metadata = {
        "job_recipe_hashes": [recipe_hash],
        "input_tokens_estimate": input_tokens_estimate,
        "output_token_budget": 1024,
        "route_input_context_limit": None,
        "route_output_context_limit": None,
        "request_bytes_estimate": request_bytes,
        "truncation_occurred": tagger_truncated,
        "truncation_policy": "chapter-tag-v2-batched",
        "input_digest": input_digest,
    }

    def publish_call_metadata() -> None:
        if call_metadata_out is not None:
            call_metadata_out.clear()
            call_metadata_out.update(call_metadata)

    # Caller-owned telemetry avoids cross-episode races in the worker pool.
    publish_call_metadata()
    inputs: dict[str, Any] = {
        "messages": messages,
        "structured_output": LLM_CONTRACT,
        "max_tokens": 1024,
    }
    backend_storage = getattr(backend, "storage", None)
    backend_config = getattr(backend, "config", None)
    backend_model = getattr(backend_config, "model", None)
    configured_models = (
        (backend_model, *tuple(getattr(backend_config, "additional_models", ()) or ()))
        if backend_model
        else ()
    )
    candidate_routes = [
        route
        for candidate in configured_models
        for route in ROUTE_CANDIDATES.get(candidate, (ROUTES.get(candidate),))
        if route is not None
    ]
    if candidate_routes:
        call_metadata["route_input_context_limit"] = max(
            route.input_context_limit for route in candidate_routes
        )
        call_metadata["route_output_context_limit"] = max(
            route.output_context_limit for route in candidate_routes
        )
        publish_call_metadata()
        # A large meeting is many independent chapter subjects, not one opaque request. Batch
        # deterministically against both model context and serialized JSON bytes. Child recipes
        # are durable, so a later pass only polls batches the Worker already accepted.
        if chapter_mode and not _batched and compact_chapters:

            def fits_any_allowed_route(candidate_messages: list[dict[str, str]], size: int) -> bool:
                if size > TAGGER_MAX_REQUEST_BYTES:
                    return False
                input_tokens = estimate_tokens(candidate_messages)
                total_tokens = input_tokens + 1024
                return any(
                    input_tokens <= route.input_context_limit
                    and 1024 <= route.output_context_limit
                    and (route.quota.tpm is None or total_tokens <= int(route.quota.tpm))
                    for route in candidate_routes
                )

            batches: list[list[dict[str, Any]]] = []
            current: list[dict[str, Any]] = []
            for chapter in compact_chapters:
                proposed = current + [chapter]
                proposed_messages = messages_for(proposed)
                proposed_bytes = len(
                    json.dumps(
                        {"model": backend_model, "messages": proposed_messages, "max_tokens": 1024},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode()
                )
                if current and not fits_any_allowed_route(proposed_messages, proposed_bytes):
                    batches.append(current)
                    current = [chapter]
                else:
                    current = proposed
                single_messages = messages_for(current)
                single_bytes = len(
                    json.dumps(
                        {"model": backend_model, "messages": single_messages, "max_tokens": 1024},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode()
                )
                if not fits_any_allowed_route(single_messages, single_bytes):
                    return [], {}, True, "payload-too-large"
            if current:
                batches.append(current)
            if len(batches) > 1:
                merged: dict[str, list[dict[str, Any]]] = {}
                pending = False
                payload_too_large = False
                batch_metadata: list[dict[str, Any]] = []
                resolved_models: list[str] = []
                for index, batch in enumerate(batches):
                    metadata: dict[str, Any] = {}
                    batch_digest = hashlib.sha1(
                        json.dumps(batch, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest()[:16]
                    _, chapter_tags, batch_pending, resolved_model = llm_tag_suggestions(
                        backend,
                        taxonomy=taxonomy,
                        agenda_item_titles="",
                        agenda_text="",
                        transcript_text="",
                        recipe_hash=f"{recipe_hash}-tag-batch-{index}-{batch_digest}",
                        chapter_inputs=batch,
                        agenda_documents=agenda_documents,
                        allow_paid=allow_paid,
                        purpose=purpose,
                        call_metadata_out=metadata,
                        _batched=True,
                    )
                    batch_metadata.append(metadata)
                    pending = pending or batch_pending
                    payload_too_large = payload_too_large or resolved_model == "payload-too-large"
                    if resolved_model and resolved_model != "payload-too-large":
                        resolved_models.append(resolved_model)
                    for chapter_id_value, tags in chapter_tags.items():
                        merged.setdefault(chapter_id_value, []).extend(tags)
                call_metadata.update(
                    {
                        "job_recipe_hashes": [
                            recipe
                            for item in batch_metadata
                            for recipe in item.get("job_recipe_hashes", [])
                        ],
                        "input_tokens_estimate": sum(
                            int(item.get("input_tokens_estimate") or 0) for item in batch_metadata
                        ),
                        "tagger_batches": len(batches),
                        "tagger_batches_metadata": batch_metadata,
                        "truncation_policy": "chapter-tag-v2-batched",
                    }
                )
                publish_call_metadata()
                if pending:
                    return [], {}, True, "payload-too-large" if payload_too_large else None
                return [], merged, False, resolved_models[-1] if resolved_models else backend_model
        if request_bytes > TAGGER_MAX_REQUEST_BYTES:
            return [], {}, True, "payload-too-large"
        if candidate_routes and all(
            input_tokens_estimate > route.input_context_limit or 1024 > route.output_context_limit
            for route in candidate_routes
        ):
            return [], {}, True, "payload-too-large"
    if backend_model and getattr(backend_storage, "cas_capable", False):
        # Allow the scheduler exactly the calibrated tag route(s): the primary ``model`` plus any
        # ``additional_models`` (config's ``tagging.llm_models``). Production tags one call per
        # episode; the extra routes exist only so a run can spill onto a second model's INDEPENDENT
        # free-tier quota pool once the primary's per-minute/daily window fills -- pure throughput,
        # not a model comparison (that stays the tournament's job, review/34 §7). ``model`` remains
        # the single stable route string for the recipe hash and calibration matrix key; each
        # candidate still records the model that actually answered (``resolved_model`` below), so
        # calibration is keyed on real usage without fragmenting the cache. The scheduler still
        # gives CAS-safe cross-shard quota accounting and a clean deferral (a JobHandle, retried by
        # the deferred sweep / this stage's next run) instead of a raw provider error. Omitted when
        # storage isn't CAS-capable (local dev/dry runs), so tagging works there as it did pre-R13.
        additional = tuple(getattr(backend_config, "additional_models", ()) or ())
        allowed_models = (backend_model, *(m for m in additional if m != backend_model))
        # Queue-only Worker requests make one upstream attempt per reservation. Reject only a
        # batch that cannot fit a whole route minute at all, not the old direct-path's two-attempt
        # reservation threshold.
        token_estimate = input_tokens_estimate
        capped_tpm = {
            route.route_id or route.model: route.quota.tpm
            for route in candidate_routes
            if route.quota.tpm is not None
        }
        if capped_tpm and all(token_estimate + 1024 > tpm for tpm in capped_tpm.values()):
            print(
                f"llm tag budget: material estimate={token_estimate} tokens exceeds every "
                f"tpm-capped allowed route's budget ({capped_tpm}) for recipe_hash="
                f"{recipe_hash} -- this would never fit any window as-is, not just this minute. "
                "Deferring because one chapter batch cannot fit any configured route.",
                flush=True,
            )
            # A distinct sentinel, not None (the ordinary "dispatched, unresolved" value) -- see
            # the docstring above. TagsStage must be able to tell this apart from a genuine
            # capacity-exhausted defer, or one oversized episode would stop the rest of the run's
            # episodes from even attempting a dispatch.
            return [], {}, True, "payload-too-large"
        inputs["llm_policy"] = LLMRequestPolicy(
            allowed_models=allowed_models,
            allow_paid=allow_paid,
            purpose=purpose,
            deadline_at=None,
            queue_only=True,
            timeout_class="fast",
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
        # R5's initial LLM rollout is chapter-only. Keep the optional schema field for backwards
        # compatible parsing of old/deferred responses, but never admit an episode-level result.
        if suggestion.chapter_id is None:
            continue
        if suggestion.chapter_id not in chapter_by_id:
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
            **call_metadata,
        }
        chapter_suggestions.setdefault(suggestion.chapter_id, []).append(value)
    publish_call_metadata()
    return suggestions, chapter_suggestions, False, outcome.model


def llm_prelabel_candidates(
    backend: Any,
    *,
    candidates: list[dict[str, Any]],
    taxonomy: Taxonomy,
    chapters: list[dict[str, Any]] | None,
    agenda_text: str = "",
    transcript_text: str = "",
    recipe_hash: str,
    model: str,
    prompt_version: str = PRELABELER_PROMPT_VERSION,
    llm_schema_version: str = "1",
    allow_paid: bool = False,
    deadline_at: Any | None = None,
    call_metadata_out: dict[str, Any] | None = None,
    purpose: str = "topic-tags:prelabeler",
) -> tuple[dict[str, dict[str, Any]], bool, str | None]:
    """Run the independent discrete evaluator over persisted candidate subjects.

    The evaluator receives only the proposed tag, its evidence, and the smallest reliable source
    context. It deliberately has a different structured contract and route allowlist from the
    production tagger. ``({}, True, None)`` means deferred; ``({}, True, "payload-too-large")``
    is reserved for a future token-aware split/defer implementation.

    ``purpose`` names the ``llm_lanes`` entry these jobs are charged to at ingress. It defaults to
    the production lane, but the R5 benchmark runs the same evaluator over its own frozen sample
    and passes ``r5-benchmark:judge``: a shadow benchmark must not spend the catalog's prelabel
    budget, which is exactly what a hard-coded purpose made it do.
    """
    if not candidates or not model:
        return {}, False, None
    response_model_type = ensure_prelabeler_contract()
    from citypods.compute.base import InferenceJob, JobHandle, JobResult
    from citypods.compute.llm_policy import ROUTES, LLMRequestPolicy, estimate_tokens

    taxonomy_by_id = taxonomy.by_id
    chapter_by_id = {item.get("chapter_id"): item for item in (chapters or [])}
    context: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for candidate in candidates:
        tag = taxonomy_by_id.get(str(candidate.get("id") or ""))
        chapter = chapter_by_id.get(candidate.get("chapter_id"))
        context.append(
            (
                candidate,
                {
                    "candidate_id": candidate.get("candidate_id"),
                    "source_kind": candidate.get("source_kind", "llm"),
                    "tag": {
                        "id": candidate.get("id"),
                        "label": tag.label if tag else candidate.get("id"),
                        "description": tag.description if tag else "",
                    },
                    "scope": candidate.get("scope"),
                    "chapter_id": candidate.get("chapter_id"),
                    "chapter_title": (chapter or {}).get("title", ""),
                    "evidence": (candidate.get("evidence") or [])[:6],
                    "tagger_explanation": candidate.get("explanation", ""),
                    "source_excerpt": _prelabel_source_excerpt(
                        candidate,
                        chapter,
                        fallback_agenda=agenda_text,
                        fallback_transcript=transcript_text,
                    ),
                },
            )
        )
    backend_storage = getattr(backend, "storage", None)
    route = ROUTES.get(model)
    input_context_limit = int(getattr(route, "input_context_limit", 32768))
    output_context_limit = int(getattr(route, "output_context_limit", 1024))
    if getattr(route, "quota", None) is not None and route.quota.tpm is not None:
        input_context_limit = min(
            input_context_limit,
            max(0, int(route.quota.tpm) - 1024),
        )
    # Keep a response/output reserve. The evaluator may receive many candidates, but it must
    # never silently drop the tail or rely on a provider-specific implicit truncation.
    max_input_tokens = max(1, input_context_limit)
    system_content = (
        "Audit each proposed topic-tag candidate independently. Return one assessment "
        "for each candidate_id. likely_correct means the proposed tag is supported by "
        "the supplied source and the evidence quote is faithful. likely_incorrect means "
        "the tag should not be displayed. Use needs_human_review when the evidence is "
        "insufficient, ambiguous, or the proposal cannot be confidently judged. Do not "
        "add or remove candidates."
    )
    taxonomy_payload = [
        {"id": tag.id, "label": tag.label, "description": tag.description} for tag in taxonomy.tags
    ]

    def make_messages(items: list[dict[str, Any]]) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": system_content},
            {
                "role": "user",
                "content": json.dumps(
                    {"taxonomy": taxonomy_payload, "candidates": items}, ensure_ascii=False
                ),
            },
        ]

    batches: list[list[tuple[dict[str, Any], dict[str, Any]]]] = []
    current: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for pair in context:
        proposed = current + [pair]
        estimate = estimate_tokens(make_messages([item for _, item in proposed]))
        if current and (estimate > max_input_tokens or len(current) >= 100):
            batches.append(current)
            current = [pair]
        else:
            current = proposed
    if current:
        batches.append(current)

    result: dict[str, dict[str, Any]] = {}
    pending = False
    payload_too_large = False
    total_estimate = 0
    max_estimate = 0
    truncation = False
    batch_count = 0
    batch_metadata: list[dict[str, Any]] = []
    for batch_index, batch in enumerate(batches):
        batch_context = [item for _, item in batch]
        messages = make_messages(batch_context)
        input_tokens_estimate = estimate_tokens(messages)
        total_estimate += input_tokens_estimate
        max_estimate = max(max_estimate, input_tokens_estimate)
        # The fixed excerpt caps are an intentional pre-labeler reduction; record it rather than
        # pretending the evaluator saw the whole transcript.
        batch_truncated = any(
            len(str(item.get("source_excerpt") or "")) >= 12_000 for item in batch_context
        )
        truncation = truncation or batch_truncated
        input_digest = hashlib.sha1(
            json.dumps(messages, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()[:16]
        call_metadata = {
            "prelabeler_model": model,
            "prelabeler_prompt_version": prompt_version,
            "prelabeler_llm_schema_version": llm_schema_version,
            "prelabeler_input_tokens_estimate": input_tokens_estimate,
            "prelabeler_output_token_budget": 1024,
            "prelabeler_route_input_context_limit": input_context_limit,
            "prelabeler_route_output_context_limit": output_context_limit,
            "prelabeler_truncation_occurred": batch_truncated,
            "prelabeler_truncation_policy": "candidate-evidence-v2-batched",
            "prelabeler_input_digest": input_digest,
            "prelabeler_batch_index": batch_index,
        }
        batch_metadata.append(dict(call_metadata))
        if input_tokens_estimate > input_context_limit or 1024 > output_context_limit:
            pending = True
            payload_too_large = True
            continue
        inputs: dict[str, Any] = {
            "messages": messages,
            "structured_output": PRELABELER_CONTRACT,
            "max_tokens": 1024,
        }
        if getattr(backend_storage, "cas_capable", False):
            inputs["llm_policy"] = LLMRequestPolicy(
                allowed_models=(model,),
                allow_paid=allow_paid,
                purpose=purpose,
                deadline_at=None,
                queue_only=True,
                timeout_class="fast",
            )
        batch_recipe = hashlib.sha1(
            json.dumps(
                {
                    "recipe": recipe_hash,
                    "batch": batch_index,
                    "llm_schema_version": llm_schema_version,
                    "ids": [c.get("candidate_id") for c, _ in batch],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()[:16]
        outcome = backend.run_inference(
            InferenceJob(task="tag", inputs=inputs, recipe_hash=f"{recipe_hash}-{batch_recipe}")
        )
        batch_count += 1
        if isinstance(outcome, JobHandle):
            pending = True
            continue
        if not isinstance(outcome, JobResult) or not isinstance(outcome.output, dict):
            raise ValueError("LLM pre-labeler backend returned an invalid result")
        choices = outcome.output.get("choices")
        content = (
            choices[0].get("message", {}).get("content")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict)
            else None
        )
        if not isinstance(content, str):
            raise ValueError("LLM pre-labeler backend returned no structured content")
        response = response_model_type.model_validate_json(content)
        known = {str(candidate.get("candidate_id")): candidate for candidate, _ in batch}
        for assessment in response.assessments:
            candidate_id_value = str(assessment.candidate_id)
            if candidate_id_value not in known or candidate_id_value in result:
                continue
            result[candidate_id_value] = {
                **call_metadata,
                "prelabeler_model": outcome.model or model,
                "evaluator_model": outcome.model or model,
                "prelabeler_decision": str(assessment.decision),
                "prelabeler_confidence": float(assessment.confidence),
                "prelabeler_reason": assessment.reason,
                "prelabeler_evidence_supported": bool(assessment.evidence_supported),
            }
    metadata = {
        "prelabeler_model": model,
        "prelabeler_prompt_version": prompt_version,
        "prelabeler_llm_schema_version": llm_schema_version,
        "prelabeler_input_tokens_estimate": total_estimate,
        "prelabeler_max_batch_input_tokens": max_estimate,
        "prelabeler_batches": batch_count,
        "prelabeler_route_input_context_limit": input_context_limit,
        "prelabeler_route_output_context_limit": output_context_limit,
        "prelabeler_truncation_occurred": truncation,
        "prelabeler_truncation_policy": "candidate-evidence-v2-batched",
        "prelabeler_batches_metadata": batch_metadata,
        "prelabeler_input_digests": [
            str(item.get("prelabeler_input_digest"))
            for item in batch_metadata
            if item.get("prelabeler_input_digest")
        ],
    }
    if call_metadata_out is not None:
        call_metadata_out.clear()
        call_metadata_out.update(metadata)
    return (
        result,
        pending,
        ("payload-too-large" if payload_too_large else (None if pending else model)),
    )


def decorate_llm_candidates(
    candidates: list[dict[str, Any]],
    *,
    episode_uid: str | None,
    episode_title: str,
    provider_model: str,
    taxonomy: Taxonomy,
    recipe_hash: str,
    prompt_version: str = TAG_PROMPT_VERSION,
    chapter_pipeline_version: str = CHAPTER_PIPELINE_VERSION,
) -> list[dict[str, Any]]:
    """Attach generic evaluator dimensions to validated feature-specific suggestions."""
    from citypods.llm_evaluation import candidate_id

    result: list[dict[str, Any]] = []
    for candidate in candidates:
        value = {
            **candidate,
            "source_kind": "llm",
            "assessment_kind": "tagger-admission",
            "feature": TAG_FEATURE,
            "provider_model": provider_model,
            "prompt_version": prompt_version,
            "chapter_pipeline_version": chapter_pipeline_version,
            "taxonomy_version": taxonomy.version,
            "recipe_hash": recipe_hash,
            "episode_uid": episode_uid,
            "episode_title": episode_title,
            "scope": "chapter" if candidate.get("chapter_id") else "episode",
        }
        value["candidate_id"] = candidate_id(value)
        result.append(value)
    return result


def decorate_rule_candidates(
    candidates: list[dict[str, Any]],
    *,
    episode_uid: str | None,
    episode_title: str,
    taxonomy: Taxonomy,
    recipe_hash: str,
    chapter_id_value: str | None = None,
    chapter_pipeline_version: str = CHAPTER_PIPELINE_VERSION,
) -> list[dict[str, Any]]:
    """Put deterministic matches into the same persisted ledger as LLM candidates."""
    from citypods.llm_evaluation import candidate_id

    result: list[dict[str, Any]] = []
    for candidate in candidates:
        evidence = []
        for item in candidate.get("evidence") or []:
            value = dict(item)
            if chapter_id_value:
                value["chapter_id"] = chapter_id_value
            evidence.append(value)
        patterns = [str(item) for item in candidate.get("rule_patterns") or [] if str(item)]
        if not patterns:
            patterns = [str(item.get("span")) for item in evidence if item.get("span")]
        match_texts = [str(item) for item in candidate.get("rule_match_texts") or [] if str(item)]
        first_match = (
            patterns[0]
            if patterns
            else next((str(item.get("span")) for item in evidence if item.get("span")), "")
        )
        value = {
            **candidate,
            "source_kind": "rule",
            "assessment_kind": "tagger-admission",
            "feature": TAG_FEATURE,
            "provider_model": f"rule:{TAGGER_VERSION}",
            "prompt_version": f"rule-{TAGGER_VERSION}",
            "taxonomy_version": taxonomy.version,
            "recipe_hash": recipe_hash,
            "episode_uid": episode_uid,
            "episode_title": episode_title,
            "scope": "chapter" if chapter_id_value else "episode",
            "chapter_id": chapter_id_value,
            "rule_pattern": first_match,
            "rule_patterns": patterns or ([first_match] if first_match else []),
            "rule_match_texts": match_texts,
            "rule_version": TAGGER_VERSION,
            "chapter_pipeline_version": chapter_pipeline_version,
            "tagger_admission": "not_applicable",
            "admission": "admitted",
            "display": True,
            "evidence": evidence,
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
    "TAG_LLM_SCHEMA_VERSION",
    "PRELABELER_CONTRACT",
    "PRELABELER_PROMPT_VERSION",
    "TAG_FEATURE",
    "TAG_PROMPT_VERSION",
    "TAGGER_VERSION",
    "CHAPTER_PIPELINE_VERSION",
    "Taxonomy",
    "TaxonomyTag",
    "episode_tag_inputs",
    "agenda_item_context",
    "chapter_id",
    "chapter_tag_inputs",
    "decorate_llm_candidates",
    "decorate_rule_candidates",
    "agenda_document_context",
    "ensure_llm_contract",
    "ensure_prelabeler_contract",
    "llm_prelabel_candidates",
    "load_taxonomy",
    "merge_tag_sources",
    "rollup_tags",
    "tag_episode",
    "tag_recipe_hash",
    "taxonomy_from_dict",
]
