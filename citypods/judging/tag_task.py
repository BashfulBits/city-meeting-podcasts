"""The ``tag`` task: is this tag supported by this chapter? (review/49 sections 4b, 3a)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from citypods.judging.subjects import tag_subject_id
from citypods.judging.tasks import (
    ContextTier,
    EpisodeTexts,
    Evidence,
    QuestionSpec,
    Subject,
    TaskSpec,
    make_evidence,
    register,
)

T0_WORD_CAP = 120
T1_HALF_WINDOW_SECONDS = 45.0
T1_WORD_CAP = 400
T2_WORD_CAP = 1800

# Maintainer policy 2026-09-30 (review/49 section 4b). A rubric change bumps prompt_version.
TAG_RUBRIC = (
    "Decide whether the meeting segment supports the proposed topic tag. The tag is correct when "
    "the segment involves a specific project, contract, program or policy on the topic, even when "
    "it is approved routinely (for example on a consent agenda). It is not correct for a generic "
    "mention, a passing reference in a list or summary, a read-back of past items, or a "
    "general-purpose services contract not tied to a specific project or policy on the topic."
)
SUPPORTED = QuestionSpec(id="supported", kind="validate", instruction=TAG_RUBRIC)


def _subjects(ep: Any) -> list[Subject]:
    uid = str(getattr(ep, "uid", None) or getattr(ep, "guid", "") or "")
    if not uid:
        return []
    subjects: list[Subject] = []
    for source in (getattr(ep, "tags", None) or [], getattr(ep, "llm_tag_candidates", None) or []):
        for candidate in source:
            if not isinstance(candidate, dict) or not candidate.get("id"):
                continue
            producer = (
                None if candidate.get("source_kind") == "rule" else candidate.get("provider_model")
            )
            subjects.append(
                Subject(
                    task="tag",
                    subject_id=tag_subject_id(uid, candidate),
                    episode_uid=uid,
                    group=None,
                    producer_model=str(producer) if producer else None,
                    payload=candidate,
                )
            )
    return subjects


def _words(text: str, cap: int) -> str:
    words = text.split()
    return " ".join(words[:cap])


def _window(segments, start: float, end: float, cap: int) -> str:
    text = " ".join(
        str(row.get("text") or "")
        for row in segments
        if float(row.get("end") or 0) > start and float(row.get("start") or 0) < end
    )
    return _words(text, cap)


def _first_time(candidate: Mapping[str, Any]) -> float | None:
    for item in candidate.get("evidence") or []:
        if isinstance(item, Mapping) and item.get("t") is not None:
            try:
                return float(item["t"])
            except (TypeError, ValueError):
                continue
    return None


def _chapter(texts: EpisodeTexts, chapter_id: Any):
    return next((c for c in texts.chapters if c[0] == chapter_id), None)


def _evidence(subject: Subject, tier: ContextTier, texts: EpisodeTexts) -> Evidence | None:
    candidate = subject.payload
    tag_id = str(candidate.get("id") or "")
    chapter = _chapter(texts, candidate.get("chapter_id"))
    header = [f"Proposed tag: {texts.definitions.get(tag_id, tag_id)}"]
    if chapter:
        header.append(f"Chapter: {chapter[1]}")
    if tier == "T0":
        spans = [
            str(item.get("span") or "")
            for item in candidate.get("evidence") or []
            if isinstance(item, Mapping) and item.get("span")
        ]
        if not spans:
            return None
        body = _words(" … ".join(spans), T0_WORD_CAP)
    elif tier == "T1":
        center = _first_time(candidate)
        if center is None and chapter:
            center = chapter[2]
        if center is None or not texts.segments:
            return None
        body = _window(
            texts.segments,
            center - T1_HALF_WINDOW_SECONDS,
            center + T1_HALF_WINDOW_SECONDS,
            T1_WORD_CAP,
        )
    elif tier == "T2":
        if not texts.segments:
            return None
        if chapter:
            end = chapter[3] if chapter[3] is not None else float("inf")
            body = _window(texts.segments, chapter[2], end, T2_WORD_CAP)
        else:
            center = _first_time(candidate)
            if center is None:
                return None
            # About 1,800 words around the first evidence time (roughly 12 minutes of speech).
            body = _window(texts.segments, center - 360.0, center + 360.0, T2_WORD_CAP)
    else:
        return None
    if not body.strip():
        return None
    return make_evidence(tier, "\n".join([*header, "Transcript:", body]))


TAG_TASK = register(
    TaskSpec(
        name="tag",
        questions=(SUPPORTED,),
        subjects=_subjects,
        evidence=_evidence,
        first_tier="T1",
        escalation=("T2", 0.3, 0.7),
        consensus_policy="validate-anchor-and-sibling",
        selection_policy="all_admitted",
    )
)
