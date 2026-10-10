"""The ``moment`` task: is a pull quote supported, publishable, useful, and its meeting's best?"""

from __future__ import annotations

from typing import Any

from citypods.judging.subjects import moment_subject_id
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

QUOTE_CONTEXT_SECONDS = 60.0
CONTEXT_WORD_CAP = 400

SUPPORTED = QuestionSpec(
    id="supported",
    kind="validate",
    instruction=(
        "Does the quote, as spoken in this transcript, actually say what the stated reason claims? "
        "Answer from the transcript only."
    ),
)
PUBLISHABLE = QuestionSpec(
    id="publishable",
    kind="gate",
    instruction=(
        "Is this clip unsafe or unfit to publish on its own? Flag it if it exposes a member of the "
        "public's personal details, mocks or embarrasses a person, is procedural filler, or "
        "depends on context the listener will not have."
    ),
)
USEFULNESS = QuestionSpec(
    id="usefulness",
    kind="grade",
    instruction="How useful is this quote to a resident who sees only the clip?",
    levels=("not useful", "somewhat useful", "useful", "very useful"),
)
BEST = QuestionSpec(
    id="best",
    kind="choose",
    instruction="Which of these quotes from the same meeting is the best one to publish?",
)


def _subjects(ep: Any) -> list[Subject]:
    uid = str(getattr(ep, "uid", None) or getattr(ep, "guid", "") or "")
    if not uid:
        return []
    subjects = []
    for candidate in getattr(ep, "moment_pullquote_candidates", None) or []:
        if not isinstance(candidate, dict) or not str(candidate.get("quote") or "").strip():
            continue
        producer = candidate.get("provider_model")
        subjects.append(
            Subject(
                task="moment",
                subject_id=moment_subject_id(uid, candidate),
                episode_uid=uid,
                group=uid,
                producer_model=str(producer) if producer else None,
                payload=candidate,
            )
        )
    return subjects


def _evidence(subject: Subject, tier: ContextTier, texts: EpisodeTexts) -> Evidence | None:
    candidate = subject.payload
    if tier not in ("T1", "T2") or not texts.segments:
        return None
    start = float(candidate.get("quote_start") or candidate.get("start") or 0.0)
    end = float(candidate.get("quote_end") or candidate.get("end") or start)
    context = " ".join(
        str(row.get("text") or "")
        for row in texts.segments
        if float(row.get("end") or 0) > start - QUOTE_CONTEXT_SECONDS
        and float(row.get("start") or 0) < end + QUOTE_CONTEXT_SECONDS
    )
    context = " ".join(context.split()[:CONTEXT_WORD_CAP])
    lines = [
        f"Quote: {candidate.get('quote')}",
        f"Stated reason: {candidate.get('why') or ''}",
        f"Transcript around the quote: {context}",
    ]
    if tier == "T2":
        chapter = next((c for c in texts.chapters if c[0] == candidate.get("chapter_id")), None)
        if chapter:
            lines.insert(0, f"Agenda item: {chapter[1]}")
    return make_evidence(tier, "\n".join(lines))


MOMENT_TASK = register(
    TaskSpec(
        name="moment",
        questions=(SUPPORTED, PUBLISHABLE, USEFULNESS, BEST),
        subjects=_subjects,
        evidence=_evidence,
        first_tier="T1",
        escalation=("T2", 0.3, 0.7),
        consensus_policy="moment-gate-validate-choose",
        selection_policy="top_k_per_group",
        top_k=3,
    )
)
