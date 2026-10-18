"""Which judged subjects go to the adjudicator, from stored judgments alone (review/53 PR5).

A subject-question is **contested** when the anchor and the sibling disagree at p = 0.5
(``validate`` and ``gate``), differ by more than one level (``grade``), or when the anchor's own
probability sits in the task's escalation band at its highest judged tier. A moment meeting's
``choose`` question is contested when the anchor's winner flips with the order of the options, or
the anchor and sibling pick different winners. A deterministic share of the uncontested
subject-questions (``judging.calibration_sample_rate``, 3%) is adjudicated too, so the adjudicator
is also measured where the judges agree.

Nothing here reads a transcript, so the judge stage's dirtiness check can use it: an episode whose
adjudication is due is dirty, one whose adjudication is done or not due is not. PR5 records data
only; no decision reads these judgments until the calibration report (PR7).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from citypods.compute.llm_lanes import LaneConfig
from citypods.judging import ledger
from citypods.judging.families import UNKNOWN_FAMILY, adjudicator_for, family_of
from citypods.judging.tasks import TIERS, QuestionSpec, Subject, TaskSpec

CONTESTED = "contested"
CALIBRATION = "calibration"


def in_sample(key: str, rate: float) -> bool:
    """Deterministic membership in a ``rate`` share of ``key``s (ten-thousandths)."""
    bucket = int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) % 10_000
    return bucket < int(rate * 10_000)


def _tier_rank(tier: Any) -> int:
    return TIERS.index(tier) if tier in TIERS else -1


@dataclass(frozen=True)
class Due:
    """One question to put to the adjudicator, and why."""

    question: QuestionSpec
    sample: str  # CONTESTED | CALIBRATION


def _by_tier(subject: Subject, question_id: str, role: str, model: str | None) -> dict[str, Any]:
    """The newest recorded value per tier for one judge on one question (unsupported rows out)."""
    found: dict[str, Any] = {}
    for row in ledger.judgments(subject):
        if (
            row.get("question_id") == question_id
            and row.get("judge_role") == role
            and (model is None or row.get("judge_model") == model)
            and row.get("quote_valid") is not False
            and row.get("context_tier") in TIERS
        ):
            found[row["context_tier"]] = row.get("value")
    return found


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def sibling_model_of(subject: Subject, question_id: str) -> str | None:
    """The sibling model that judged this question (the independence rule needs it)."""
    for row in ledger.judgments(subject):
        if row.get("question_id") == question_id and row.get("judge_role") == "sibling":
            return str(row.get("judge_model") or "") or None
    return None


def _awaiting_escalation(
    subject: Subject,
    spec: TaskSpec,
    question: QuestionSpec,
    anchor: Mapping[str, Any],
    anchor_model: str,
) -> bool:
    """Whether the anchor is still due to re-judge this question at the escalation tier.

    Its first-tier probability in the band means a second reading is coming, and adjudicating
    before it lands would spend the adjudicator on a question the anchor may settle itself."""
    if question.kind != "validate" or not spec.escalation:
        return False
    tier, low, high = spec.escalation
    if tier in anchor:
        return False
    first = _number(anchor.get(spec.first_tier))
    if first is None or not low <= first <= high:
        return False
    key = ledger.unbuildable_key(question.id, anchor_model, question.prompt_version, tier)
    return key not in ledger.unbuildable(subject)  # evidence that cannot be built never lands


def _contested(
    spec: TaskSpec,
    question: QuestionSpec,
    anchor: Mapping[str, Any],
    sibling: Mapping[str, Any],
    audit: Mapping[str, Any] | None = None,
) -> bool:
    """The anchor's final reading against the sibling's, or the anchor still in the band.

    A person's audit answer is one vote: if it disagrees with either judge the question goes to
    the adjudicator at full context; it flips nothing."""
    if not anchor or not sibling:
        return False
    a = _number(anchor[max(anchor, key=_tier_rank)])
    s = sibling[max(sibling, key=_tier_rank)]
    if a is None:
        return False
    vote = audit[max(audit, key=_tier_rank)] if audit else None
    if isinstance(vote, bool) and question.kind == "validate":
        if (a >= 0.5) != vote or (isinstance(s, bool) and s != vote):
            return True
    if question.kind in ("validate", "gate"):
        if isinstance(s, bool) and (a >= 0.5) != s:
            return True
    elif question.kind == "grade":
        level = _number(s)
        if level is not None and abs(a - level) > 1:
            return True
    if question.kind == "validate" and spec.escalation:
        _tier, low, high = spec.escalation
        if low <= a <= high:
            return True
    return False


def due_questions(
    subject: Subject, spec: TaskSpec, *, anchor_model: str, calibration_rate: float
) -> list[Due]:
    """The per-subject questions (not ``choose``) whose adjudication is due, from judgments."""
    due: list[Due] = []
    for question in spec.questions:
        if question.kind == "choose":
            continue
        anchor = _by_tier(subject, question.id, "anchor", anchor_model)
        sibling = _by_tier(subject, question.id, "sibling", None)
        if not anchor or not sibling:
            continue  # nothing to compare yet
        if _awaiting_escalation(subject, spec, question, anchor, anchor_model):
            continue
        audit = _by_tier(subject, question.id, "audit", "human")
        if _contested(spec, question, anchor, sibling, audit):
            due.append(Due(question, CONTESTED))
        elif in_sample(f"{subject.subject_id}:{question.id}:{CALIBRATION}", calibration_rate):
            due.append(Due(question, CALIBRATION))
    return due


def _winner(subject: Subject, question_id: str, role: str, model: str | None) -> str | None:
    values = _by_tier(subject, question_id, role, model)
    if not values:
        return None
    return str(values[max(values, key=_tier_rank)])


def choose_due(
    subjects: Sequence[Subject], spec: TaskSpec, *, anchor_model: str, calibration_rate: float
) -> Due | None:
    """The meeting's ``choose`` question, when the anchor's or the judges' winners disagree."""
    question = next((q for q in spec.questions if q.kind == "choose"), None)
    if question is None or len(subjects) < 2:
        return None
    key = subjects[0]  # the group's rows live on its first subject (see runner._plan_choose)
    forward = _winner(key, question.id, "anchor", anchor_model)
    reverse = _winner(key, f"{question.id}_r", "anchor", anchor_model)
    sibling = _winner(key, question.id, "sibling", None)
    if forward is None or reverse is None or sibling is None:
        return None
    if forward != reverse or forward != sibling:
        return Due(question, CONTESTED)
    if in_sample(f"{key.group or key.subject_id}:{question.id}:{CALIBRATION}", calibration_rate):
        return Due(question, CALIBRATION)
    return None


def adjudicated(subject: Subject, question_id: str, model: str | None = None) -> bool:
    """Whether any adjudicator judgment (or pending question) exists for this question."""
    rows = [*ledger.judgments(subject), *ledger.pending(subject)]
    return any(
        row.get("judge_role") == "adjudicator"
        and row.get("question_id") == question_id
        and (model is None or row.get("judge_model") == model)
        for row in rows
    )


def adjudicator_for_group(
    subjects: Sequence[Subject],
    sibling_model: str | None,
    adjudicator: LaneConfig,
    families: Mapping[str, str],
) -> str | None:
    """An adjudicator independent of every producer in the meeting and of its sibling."""
    excluded = {family_of(subject.producer_model, families) for subject in subjects}
    if UNKNOWN_FAMILY in excluded:
        return None
    if sibling_model:
        excluded.add(family_of(sibling_model, families))
    return next((m for m in adjudicator.models if families.get(m) not in excluded), None)


def subject_adjudicator(
    subject: Subject, question_id: str, adjudicator: LaneConfig, families: Mapping[str, str]
) -> str | None:
    return adjudicator_for(
        subject.producer_model, sibling_model_of(subject, question_id), adjudicator, families
    )


def adjudication_unbuildable_key(question: QuestionSpec, model: str) -> str:
    """The ledger key marking evidence the adjudicator could not be given (tier ``adj``)."""
    return ledger.unbuildable_key(question.id, model, question.prompt_version, "adj")


def pending_adjudication(
    subjects: Sequence[Subject],
    spec: TaskSpec,
    *,
    anchor_model: str,
    calibration_rate: float,
    adjudicator: LaneConfig,
    families: Mapping[str, str],
    identity: str = "",
) -> bool:
    """Whether any subject-question of this task still needs its adjudication (no transcript).

    A question no adjudicator can take (none independent of its producer and sibling) or whose
    evidence is recorded as unbuildable for this transcript is not pending, so it does not keep
    an episode dirty forever."""
    for subject in subjects:
        skipped = ledger.unbuildable(subject)
        for due in due_questions(
            subject, spec, anchor_model=anchor_model, calibration_rate=calibration_rate
        ):
            model = subject_adjudicator(subject, due.question.id, adjudicator, families)
            if model is None or adjudicated(subject, due.question.id, model):
                continue
            if skipped.get(adjudication_unbuildable_key(due.question, model)) == identity:
                continue
            return True
    group = choose_due(subjects, spec, anchor_model=anchor_model, calibration_rate=calibration_rate)
    if group is None:
        return False
    key = subjects[0]
    model = adjudicator_for_group(
        subjects, sibling_model_of(key, group.question.id), adjudicator, families
    )
    if model is None:
        return False
    return not (
        adjudicated(key, group.question.id, model)
        and adjudicated(key, f"{group.question.id}_r", model)
    )
