"""The ``moment`` task: is a pull quote supported, publishable, useful, and its meeting's best?"""

from __future__ import annotations

import re
from typing import Any

from citypods.judging.subjects import moment_subject_id
from citypods.judging.tasks import (
    ContextTier,
    EpisodeTexts,
    Evidence,
    QuestionSpec,
    Subject,
    TaskSpec,
    judging_store,
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
                store=judging_store(ep),
            )
        )
    return subjects


_WORD_CHARS = re.compile(r"[^a-z0-9]+")


def _norm(word: str) -> str:
    return _WORD_CHARS.sub("", word.lower())


def _window_around_quote(words: list[str], quote: str, cap: int) -> list[str]:
    """At most ``cap`` words that keep the whole quote in view.

    Provider-aligned transcripts carry whole minutes in one segment, so the segments overlapping
    the quote can start far before it and a head-truncation cuts the quote off (review/60: 34 of
    60 sampled moments). When the quote is already inside the first ``cap`` words nothing changes
    (same evidence, same digest); otherwise the window is centred on the quote. If the quote
    cannot be found the head is kept, as before.
    """
    if len(words) <= cap:
        return words
    needle = [n for n in (_norm(w) for w in quote.split()) if n]
    normed = [_norm(w) for w in words]
    at = -1
    if needle:
        probe = needle[: min(len(needle), 6)]
        for i in range(len(normed) - len(probe) + 1):
            if normed[i : i + len(probe)] == probe:
                at = i
                break
    if at < 0 or at + len(needle) <= cap:
        return words[:cap]
    pad = max(0, (cap - len(needle)) // 2)
    begin = min(max(0, at - pad), len(words) - cap)
    return words[begin : begin + cap]


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
    context = " ".join(
        _window_around_quote(context.split(), str(candidate.get("quote") or ""), CONTEXT_WORD_CAP)
    )
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
        tiers=("T1", "T2"),
    )
)
