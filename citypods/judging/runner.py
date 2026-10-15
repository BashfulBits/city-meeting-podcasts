"""One judge pass over a city's episodes (review/53 PR3), driven by :class:`JudgeStage`.

A pass first collects answers for questions already in flight (each subject's ``pending`` pointers
in the episode's ``judging`` block name the recipe that will answer them, so a packet may span a
source's episodes), then plans what is
due -- every subject at the task's first tier by the anchor (JEV) and one sibling chosen per entry
for independence, the anchor again at the escalation tier when its first-tier probability is in the
band, and every tier for a deterministic sample -- then packs, submits and records pointers. All
state lives in ``Episode.judging`` (per subject: judgments, pending, unbuildable); candidates
themselves are never written, in shadow mode or otherwise.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from citypods.compute.base import JobHandle, JobResult
from citypods.compute.llm_lanes import LaneConfig
from citypods.judging import ledger
from citypods.judging.adjudication import (
    adjudication_unbuildable_key,
    adjudicator_for_group,
    choose_due,
    due_questions,
    in_sample,
    pending_adjudication,
    sibling_model_of,
    subject_adjudicator,
)
from citypods.judging.backends import (
    REASON_MAX,
    STABILITY_ORDER,
    AdjudicatorBackend,
    ChatJudgeBackend,
    Item,
    JevBackend,
    Packet,
    choose_options,
)
from citypods.judging.families import UNKNOWN_FAMILY, family_of, sibling_for
from citypods.judging.packing import item_size, pack
from citypods.judging.quotes import quote_valid
from citypods.judging.tasks import (
    EpisodeTexts,
    Evidence,
    Subject,
    TaskSpec,
    make_evidence,
    transcript_identity,
)

ANCHOR_PURPOSE = "judge:anchor"
SIBLING_PURPOSE = "judge:sibling"
ADJUDICATOR_PURPOSE = "judge:adjudicator"
PURPOSES = {
    "anchor": ANCHOR_PURPOSE,
    "sibling": SIBLING_PURPOSE,
    "adjudicator": ADJUDICATOR_PURPOSE,
}
# Adjudicator packets: up to 20 questions (review/53 PR5) under a ceiling every adjudicator route
# clears; the BeatAPI DeepSeek leg, with a 128,000-token context, is the smallest.
ADJUDICATOR_CEILING = (60_000, 20)

# Chat sibling ceilings (review/53 PR3 "Packing"): Gemma's AI Studio routes have a 14,400-token
# hard ceiling and a 16k TPM, so about 10,000 estimated input tokens; Nemotron 3 Super about 24,000.
SIBLING_CEILINGS: dict[str, tuple[int, int]] = {
    "google/gemma-4-31b-it": (10_000, 25),
    "google/gemma-4-26b-a4b-it": (10_000, 25),
    "openrouter/nvidia/nemotron-3-super-120b-a12b:free": (24_000, 25),
}
DEFAULT_SIBLING_CEILING = (10_000, 20)


@dataclass
class JudgingContext:
    tasks: Sequence[TaskSpec]
    anchor: LaneConfig
    sibling: LaneConfig
    families: Mapping[str, str]
    # episode -> EpisodeTexts (transcript segments, chapters, tag definitions)
    texts_for: Callable[[Any], EpisodeTexts]
    # returns the deferred record for a recipe: JobResult, JobHandle or None
    look_up: Callable[[str], Any]
    # submits one packet's job; returns JobResult, JobHandle, or raises
    submit: Callable[[Packet, Any], Any]
    all_tiers_sample_rate: float = 0.05
    stop: Callable[[], bool] | None = None
    # purpose -> packets left this run. Shared across every source the run visits (the stage passes
    # the StageContext's dict and lock), so the lane's per-run cap binds the whole run.
    run_caps: dict[str, int] = field(default_factory=dict)
    run_caps_lock: Any = None
    # Plan and pack only: count what would be sent and submit nothing (the PR4 rehearsal).
    dry_run: bool = False
    # The Worker's learned input ratio for the JEV route (None: unreadable, so packets are sized
    # for the unmeasured default). Packets must stay under the route's ceiling once scaled by it.
    anchor_input_ratio: float | None = None
    # purpose -> most packets that may be waiting at the Worker at once. A pass submits only into
    # the room left, so a backlog the providers drain slowly cannot grow the Worker queue without
    # bound (the PR4 shadow run queued ~8,500 jobs, adding ~3,300 a day against ~700 claimed).
    max_outstanding: dict[str, int] = field(default_factory=dict)
    # PR5: the adjudicator lane (None: adjudication is off) and the shares of subjects it samples.
    adjudicator: LaneConfig | None = None
    calibration_sample_rate: float = 0.0
    stability_sample_rate: float = 0.0
    # Transcripts one pass may read to check the quotes of answers it collects; an answer whose
    # evidence cannot be read yet waits (its marker stays) for a later pass.
    quote_check_reads: int = 400


@dataclass
class JudgingStats:
    counts: Counter = field(default_factory=Counter)
    episodes_complete: set[str] = field(default_factory=set)
    episodes_with_work: set[str] = field(default_factory=set)
    # Episodes whose texts could not be read this pass (TextsUnavailable): skipped, never complete.
    episodes_unavailable: set[str] = field(default_factory=set)
    # Episodes the bounded plan reached this pass; only these can be found complete.
    episodes_planned: set[str] = field(default_factory=set)
    # purpose -> recipes of packets still waiting at the Worker (read while collecting).
    outstanding: dict[str, set[str]] = field(default_factory=dict)


class TextsUnavailable(Exception):
    """``texts_for`` could not read an episode's texts right now (a transient storage outage).

    The episode is skipped this pass and stays incomplete: nothing is marked unbuildable and the
    rest of the run goes on, so one unreadable transcript never aborts the lane."""


def _episode_uid(ep: Any) -> str:
    return str(getattr(ep, "uid", None) or getattr(ep, "guid", "") or "")


def _take_run_slot(ctx: JudgingContext, purpose: str) -> bool:
    def take() -> bool:
        left = ctx.run_caps.get(purpose)
        if left is None:
            return True
        if left <= 0:
            return False
        ctx.run_caps[purpose] = left - 1
        return True

    if ctx.run_caps_lock is None:
        return take()
    with ctx.run_caps_lock:
        return take()


def _return_run_slot(ctx: JudgingContext, purpose: str) -> None:
    def give() -> None:
        if purpose in ctx.run_caps:
            ctx.run_caps[purpose] += 1

    if ctx.run_caps_lock is None:
        give()
    else:
        with ctx.run_caps_lock:
            give()


def _sibling_backend(model: str) -> ChatJudgeBackend:
    total, items = SIBLING_CEILINGS.get(model, DEFAULT_SIBLING_CEILING)
    return ChatJudgeBackend(max_total_tokens=total, max_items=items)


def _marker(packet: Packet, item: Item, subject: Subject) -> dict[str, Any]:
    return {
        "subject_id": subject.subject_id,
        "question_id": item.question_id,
        "judge_model": packet.judge_model,
        "prompt_version": item.question.prompt_version,
        "context_tier": item.tier,
        "evidence_digest": item.evidence.digest,
        "task": subject.task,
        "kind": item.question.kind,
        "qid": item.qid,
        "judge_role": packet.role,
        "purpose": packet.purpose,
        "recipe_hash": packet.recipe_hash,
        "sample": item.sample,
        "options": [list(pair) for pair in item.options],
    }


def _adjudicator_backend() -> AdjudicatorBackend:
    total, items = ADJUDICATOR_CEILING
    return AdjudicatorBackend(max_total_tokens=total, max_items=items)


def _backend_for(role: str, model: str, ctx: JudgingContext | None = None):
    if role == "anchor":
        return JevBackend(ctx.anchor_input_ratio if ctx is not None else None)
    if role == "adjudicator":
        return _adjudicator_backend()
    return _sibling_backend(model)


def _base_question_id(question_id: str) -> str:
    """A stored question id without its order suffix (``_r`` reversed, ``_s`` stability)."""
    for suffix in ("_r", STABILITY_ORDER):
        if question_id.endswith(suffix):
            return question_id.removesuffix(suffix)
    return question_id


def _record(packet: Packet, rows, stats: JudgingStats) -> None:
    for subject, row in rows:
        if ledger.append_judgment(subject, row):
            stats.counts["judgments_appended"] += 1


class _Defer:
    """Sentinel: an answer's evidence cannot be read yet, so its marker waits for a later pass."""


DEFER = _Defer()


def _choose_text(spec: TaskSpec, group: Sequence[Subject], options) -> str | None:
    by_id = {subject.subject_id: subject for subject in group}
    try:
        return "\n".join(f"{letter}: {by_id[sid].payload.get('quote')}" for letter, sid in options)
    except KeyError:
        return None


def _evidence_text(
    subject: Subject,
    item: Item,
    marker: Mapping[str, Any],
    ctx: JudgingContext,
    episodes: Mapping[str, Any],
    read: set[str],
) -> str | _Defer | None:
    """The evidence a chat judge was sent, rebuilt from the transcript, for its quote check.

    ``None`` when it cannot be rebuilt identically (the digest differs, the episode is gone):
    the row is kept with ``quote_valid`` unset. ``DEFER`` when the transcript cannot be read now
    (an outage, or this pass's read budget is spent): the marker stays for the next pass."""
    from citypods.judging.tasks import task as task_spec

    episode = episodes.get(subject.episode_uid)
    if episode is None:
        return None
    uid = subject.episode_uid
    if uid not in read and len(read) >= ctx.quote_check_reads:
        return DEFER
    try:
        texts = ctx.texts_for(episode)
    except TextsUnavailable:
        return DEFER
    read.add(uid)
    spec = task_spec(subject.task)
    if item.question.kind == "choose":
        text = _choose_text(spec, spec.subjects(episode), item.options)
        evidence = make_evidence(item.tier, text) if text is not None else None
    else:
        evidence = spec.evidence(subject, item.tier, texts)
    if evidence is None or evidence.digest != item.evidence.digest:
        return None
    return evidence.text


def _collect(
    subjects: Sequence[Subject],
    ctx: JudgingContext,
    stats: JudgingStats,
    episodes: Mapping[str, Any] | None = None,
) -> None:
    """Turn finished in-flight questions into judgments; drop markers that can never finish.

    A chat judge's or adjudicator's answer is stored with ``quote_valid``: whether the quote in its
    reason occurs in the evidence it was sent (review/53 PR5)."""
    from citypods.judging.tasks import task as task_spec

    episodes = episodes or {}
    outputs: dict[str, Any] = {}
    read: set[str] = set()
    for subject in subjects:
        for marker in ledger.pending(subject):
            recipe = str(marker.get("recipe_hash") or "")
            if recipe not in outputs:
                outputs[recipe] = ctx.look_up(recipe) if recipe else None
            record = outputs[recipe]
            if isinstance(record, JobHandle):
                stats.counts["still_pending"] += 1
                stats.outstanding.setdefault(str(marker.get("purpose") or ""), set()).add(recipe)
                continue
            if not isinstance(record, JobResult):
                # No record (a failed submission, a cancelled or structurally blocked packet): the
                # question becomes due again and is re-planned for the current judges. This is the
                # Slice 3a rebatch branch -- never an unchanged-recipe resubmission.
                ledger.drop_pending(subject, marker)
                stats.counts["replanned"] += 1
                continue
            question_id = str(marker.get("question_id") or "")
            question = task_spec(subject.task).question(_base_question_id(question_id))
            tier = str(marker["context_tier"])
            item = Item(
                question=question,
                tier=tier,
                # Answers are keyed by qid; the digest is the one the question was asked with.
                evidence=Evidence(tier, "", str(marker["evidence_digest"])),
                subjects=(subject,),
                sample=str(marker.get("sample") or "routine"),
                options=tuple(tuple(pair) for pair in marker.get("options") or ()),
                order=question_id[len(question.id) :],
                asked_qid=str(marker["qid"]),
            )
            role = str(marker.get("judge_role") or "")
            packet = Packet(
                str(marker.get("purpose") or ""),
                role,
                str(marker["judge_model"]),
                (item,),
            )
            rows = _backend_for(packet.role, packet.judge_model).parse(packet, record.output)
            evidence_text: str | _Defer | None = None
            if role != "anchor" and rows:
                evidence_text = _evidence_text(subject, item, marker, ctx, episodes, read)
                if evidence_text is DEFER:
                    stats.counts["quote_check_deferred"] += 1
                    continue  # keep the marker: the answer is recorded once it can be checked
            ledger.drop_pending(subject, marker)
            for _subject, row in rows:
                row["packet_id"] = recipe
                if role != "anchor":
                    if isinstance(evidence_text, str):
                        row["quote_valid"] = quote_valid(row.get("reason"), evidence_text)
                        stats.counts[f"quote_{'valid' if row['quote_valid'] else 'invalid'}"] += 1
                    else:
                        stats.counts["quote_unchecked"] += 1
                    if row.get("reason"):
                        # Checked in full above; the stored copy stays short.
                        row["reason"] = str(row["reason"])[:REASON_MAX]
            if not rows:
                stats.counts["answer_missing"] += 1
            _record(packet, rows, stats)
    _count_superseded_outstanding(subjects, ctx, stats, outputs)


def _count_superseded_outstanding(
    subjects: Sequence[Subject],
    ctx: JudgingContext,
    stats: JudgingStats,
    outputs: dict[str, Any],
) -> None:
    """Count packets still waiting at the Worker for subjects that are no longer current.

    A candidate whose evidence changes gets a new subject id, so its old subject's pending markers
    are never visited by the loop above while the packet they name is still queued. They stay in
    the episode's ``judging`` block, so they are read from there and added to the outstanding
    count: otherwise repeated candidate changes could admit packets past the outstanding limit."""
    current = {subject.subject_id for subject in subjects}
    stores = {id(subject.store): subject.store for subject in subjects}
    for store in stores.values():
        for subject_id, entry in (store.get("subjects") or {}).items():
            if subject_id in current or not isinstance(entry, dict):
                continue
            for marker in entry.get("pending") or []:
                recipe = str((marker or {}).get("recipe_hash") or "")
                if not recipe:
                    continue
                if recipe not in outputs:
                    outputs[recipe] = ctx.look_up(recipe)
                if isinstance(outputs[recipe], JobHandle):
                    purpose = str(marker.get("purpose") or "")
                    stats.outstanding.setdefault(purpose, set()).add(recipe)
                    stats.counts["superseded_pending"] += 1


def _apply_backpressure(ctx: JudgingContext, stats: JudgingStats) -> None:
    """Shrink each purpose's run cap to the room left under its outstanding-packet limit."""
    for purpose, limit in ctx.max_outstanding.items():
        outstanding = len(stats.outstanding.get(purpose, ()))
        room = max(0, int(limit) - outstanding)
        stats.counts[f"outstanding:{purpose}"] = outstanding
        if ctx.run_caps_lock is not None:
            ctx.run_caps_lock.acquire()
        try:
            left = ctx.run_caps.get(purpose)
            ctx.run_caps[purpose] = room if left is None else min(left, room)
        finally:
            if ctx.run_caps_lock is not None:
                ctx.run_caps_lock.release()
        if room == 0:
            stats.counts[f"run_cap_outstanding:{purpose}"] += 1


def _due_tiers(subject: Subject, spec: TaskSpec, role: str, ctx: JudgingContext) -> list[tuple]:
    tiers: list[tuple[str, str]] = [(spec.first_tier, "routine")]
    if role == "anchor" and spec.escalation:
        tier, low, high = spec.escalation
        for question in spec.questions:
            if question.kind != "validate":
                continue
            value = ledger.anchor_value(
                subject,
                question.id,
                spec.first_tier,
                ctx.anchor.primary_model,
            )
            if value is not None and low <= value <= high:
                tiers.append((tier, "escalation"))
                break
    if in_sample(subject.subject_id, ctx.all_tiers_sample_rate):
        tiers.extend((tier, "all_tiers") for tier in spec.tiers if tier != spec.first_tier)
    seen: set[str] = set()
    return [(t, s) for t, s in tiers if not (t in seen or seen.add(t))]


def _item_budget(ctx: JudgingContext) -> dict[str, int] | None:
    """Items one pass can submit per purpose: run-cap packets left times the fullest packet.

    None when either purpose is uncapped. Planning stops once both budgets are met, so a pass reads
    only the transcripts it can act on instead of the whole backlog (the first PR4 dry run spent
    over an hour planning a cold backlog it could submit a fraction of)."""
    anchor_left = ctx.run_caps.get(ANCHOR_PURPOSE)
    sibling_left = ctx.run_caps.get(SIBLING_PURPOSE)
    if anchor_left is None or sibling_left is None:
        return None
    sibling_items = max(
        (_sibling_backend(model).max_items for model in ctx.sibling.models),
        default=DEFAULT_SIBLING_CEILING[1],
    )
    return {
        ANCHOR_PURPOSE: max(0, anchor_left) * JevBackend.max_items,
        SIBLING_PURPOSE: max(0, sibling_left) * sibling_items,
    }


def plan(
    episodes: Sequence[Any],
    ctx: JudgingContext,
    stats: JudgingStats,
    item_budget: Mapping[str, int] | None = None,
    *,
    bounded: bool = False,
) -> dict[tuple[str, str], list[list[Item]]]:
    """Units of due items per (role, judge model), in recent-first episode order.

    A ``bounded`` (submitting) plan stops before the next episode when ``ctx.stop`` fires or once
    every purpose has ``item_budget`` items planned; episodes it does not reach stay dirty for a
    later pass. The completeness re-plan is unbounded over the episodes already planned."""
    units: dict[tuple[str, str], list[list[Item]]] = {}
    anchor_model = ctx.anchor.primary_model

    def planned(purpose: str) -> int:
        role = "anchor" if purpose == ANCHOR_PURPOSE else "sibling"
        return sum(len(unit) for (r, _m), us in units.items() if r == role for unit in us)

    for ep in episodes:
        if bounded:
            if ctx.stop is not None and ctx.stop():
                stats.counts["plan_stopped"] += 1
                break
            if item_budget is not None and all(
                planned(purpose) >= budget for purpose, budget in item_budget.items()
            ):
                stats.counts["plan_budget_reached"] += 1
                break
        stats.episodes_planned.add(_episode_uid(ep))
        texts: EpisodeTexts | None = None
        for spec in ctx.tasks:
            subjects = spec.subjects(ep)
            if not subjects:
                continue
            if texts is None:
                try:
                    texts = ctx.texts_for(ep)
                except TextsUnavailable:
                    stats.counts["texts_unavailable"] += 1
                    stats.episodes_unavailable.add(_episode_uid(ep))
                    break
            per_question = [q for q in spec.questions if q.kind != "choose"]
            group_units: dict[tuple[str, str], list[Item]] = {}
            for subject in subjects:
                sibling = sibling_for(subject.producer_model, ctx.sibling, ctx.families)
                if sibling is None:
                    stats.counts["no_independent_sibling"] += 1
                    if family_of(subject.producer_model, ctx.families) == UNKNOWN_FAMILY:
                        stats.counts["unknown_producer_family"] += 1
                judges = [("anchor", anchor_model)] + ([("sibling", sibling)] if sibling else [])
                for role, model in judges:
                    for tier, sample in _due_tiers(subject, spec, role, ctx):
                        evidence = spec.evidence(subject, tier, texts)
                        if evidence is None:
                            # Recorded so an unchanged episode is not revisited for it every pass
                            # (episode_needs_judging); a new transcript re-opens it.
                            if texts.identity and not ctx.dry_run:
                                for question in per_question:
                                    ledger.mark_unbuildable(
                                        subject,
                                        ledger.unbuildable_key(
                                            question.id, model, question.prompt_version, tier
                                        ),
                                        texts.identity,
                                    )
                            continue
                        for question in per_question:
                            key = (
                                subject.subject_id,
                                question.id,
                                model,
                                question.prompt_version,
                                tier,
                                evidence.digest,
                            )
                            if ledger.has_key(subject, key):
                                # Judged already; a sampled subject is asked once more, worded
                                # differently, so the report can count the judge's flips (PR5).
                                if _stability_due(subject, question, model, tier, sample, key, ctx):
                                    stats.episodes_with_work.add(subject.episode_uid)
                                    units.setdefault((role, model), []).append(
                                        [
                                            Item(
                                                question,
                                                tier,
                                                evidence,
                                                (subject,),
                                                "stability",
                                                order=STABILITY_ORDER,
                                            )
                                        ]
                                    )
                                continue
                            item = Item(question, tier, evidence, (subject,), sample)
                            stats.episodes_with_work.add(subject.episode_uid)
                            if spec.selection_policy == "top_k_per_group":
                                group_units.setdefault((role, model), []).append(item)
                            else:
                                units.setdefault((role, model), []).append([item])
            _plan_choose(spec, subjects, texts, ctx, group_units)
            for judge, items in group_units.items():
                if items:
                    units.setdefault(judge, []).append(items)
    return units


def _stability_due(
    subject: Subject,
    question,
    model: str,
    tier: str,
    sample: str,
    key: tuple,
    ctx: JudgingContext,
) -> bool:
    """Whether this already-judged question should be asked again as a stability probe.

    A deterministic ``stability_sample_rate`` share of subjects, at the first tier on the routine
    pass, once the original answer is recorded (a pending one has nothing to compare with yet)."""
    if sample != "routine" or ctx.stability_sample_rate <= 0:
        return False
    if not in_sample(f"{subject.subject_id}:stability", ctx.stability_sample_rate):
        return False
    if not any(ledger.key_of(row) == key for row in ledger.judgments(subject)):
        return False
    again = (key[0], f"{question.id}{STABILITY_ORDER}", *key[2:])
    return not ledger.has_key(subject, again)


def choose_sibling(
    subjects: Sequence[Subject], sibling: LaneConfig, families: Mapping[str, str]
) -> str | None:
    """One sibling for a whole meeting's choose question: independent of every producer in it."""
    producer_families = {family_of(s.producer_model, families) for s in subjects}
    if UNKNOWN_FAMILY in producer_families:
        return None
    return next((m for m in sibling.models if families.get(m) not in producer_families), None)


def _choose_evidence(spec: TaskSpec, subjects: Sequence[Subject], options) -> Evidence:
    """The evidence for a meeting's choose question: each option's quote beside its letter."""
    by_id = {s.subject_id: s for s in subjects}
    lines = [f"{letter}: {by_id[sid].payload.get('quote')}" for letter, sid in options]
    return make_evidence(spec.first_tier, "\n".join(lines))


def _plan_choose(spec, subjects, texts, ctx, group_units) -> None:
    choose = [q for q in spec.questions if q.kind == "choose"]
    if not choose or len(subjects) < 2:
        return
    question = choose[0]
    sibling = choose_sibling(subjects, ctx.sibling, ctx.families)
    judges = [("anchor", ctx.anchor.primary_model)] + ([("sibling", sibling)] if sibling else [])
    for reverse, order in ((False, ""), (True, "_r")):
        options = choose_options(subjects, reverse=reverse)
        evidence = _choose_evidence(spec, subjects, options)
        for role, model in judges:
            key_subject = subjects[0]
            key = (
                key_subject.subject_id,
                f"{question.id}{order}",
                model,
                question.prompt_version,
                spec.first_tier,
                evidence.digest,
            )
            if ledger.has_key(key_subject, key):
                continue
            group_units.setdefault((role, model), []).append(
                Item(
                    question, spec.first_tier, evidence, tuple(subjects), "routine", options, order
                )
            )


def _largest_tier_evidence(spec: TaskSpec, subject: Subject, texts: EpisodeTexts):
    """The evidence at the largest tier this task's builder can produce for the subject."""
    for tier in reversed(spec.tiers):
        evidence = spec.evidence(subject, tier, texts)
        if evidence is not None:
            return tier, evidence
    return None


def plan_adjudication(
    episodes: Sequence[Any],
    ctx: JudgingContext,
    stats: JudgingStats,
    item_budget: int | None = None,
    *,
    bounded: bool = False,
) -> dict[tuple[str, str], list[list[Item]]]:
    """Units for the adjudicator: contested subject-questions and the calibration sample.

    Each goes to an adjudicator independent of its producer and its sibling, at the largest
    context tier, asked the same way whether it was contested or sampled. A transcript is read
    only for an episode that has something due. ``bounded`` stops at ``ctx.stop`` and at
    ``item_budget`` items (the adjudicator's run cap times its packet size)."""
    units: dict[tuple[str, str], list[list[Item]]] = {}
    lane = ctx.adjudicator
    if lane is None:
        return units
    anchor_model = ctx.anchor.primary_model
    planned = 0
    for ep in episodes:
        if bounded:
            if ctx.stop is not None and ctx.stop():
                stats.counts["adjudication_plan_stopped"] += 1
                break
            if item_budget is not None and planned >= item_budget:
                stats.counts["adjudication_budget_reached"] += 1
                break
        texts: EpisodeTexts | None = None
        for spec in ctx.tasks:
            subjects = spec.subjects(ep)
            if not subjects:
                continue
            work = [
                (subject, due)
                for subject in subjects
                for due in due_questions(
                    subject,
                    spec,
                    anchor_model=anchor_model,
                    calibration_rate=ctx.calibration_sample_rate,
                )
            ]
            group_due = choose_due(
                subjects,
                spec,
                anchor_model=anchor_model,
                calibration_rate=ctx.calibration_sample_rate,
            )
            if not work and group_due is None:
                continue
            if texts is None:
                try:
                    texts = ctx.texts_for(ep)
                except TextsUnavailable:
                    stats.counts["texts_unavailable"] += 1
                    stats.episodes_unavailable.add(_episode_uid(ep))
                    break
            meeting: dict[str, list[Item]] = {}
            for subject, due in work:
                model = subject_adjudicator(subject, due.question.id, lane, ctx.families)
                if model is None:
                    stats.counts["no_independent_adjudicator"] += 1
                    continue
                built = _largest_tier_evidence(spec, subject, texts)
                if built is None:
                    stats.counts["adjudication_unbuildable"] += 1
                    if texts.identity and not ctx.dry_run:
                        ledger.mark_unbuildable(
                            subject,
                            adjudication_unbuildable_key(due.question, model),
                            texts.identity,
                        )
                    continue
                tier, evidence = built
                key = (
                    subject.subject_id,
                    due.question.id,
                    model,
                    due.question.prompt_version,
                    tier,
                    evidence.digest,
                )
                if ledger.has_key(subject, key):
                    continue
                stats.episodes_with_work.add(subject.episode_uid)
                stats.counts[f"adjudication_{due.sample}"] += 1
                item = Item(due.question, tier, evidence, (subject,), due.sample)
                if spec.selection_policy == "top_k_per_group":
                    meeting.setdefault(model, []).append(item)
                else:
                    units.setdefault(("adjudicator", model), []).append([item])
                    planned += 1
            if group_due is not None:
                key_subject = subjects[0]
                model = adjudicator_for_group(
                    subjects,
                    sibling_model_of(key_subject, group_due.question.id),
                    lane,
                    ctx.families,
                )
                if model is None:
                    stats.counts["no_independent_adjudicator"] += 1
                else:
                    for order, reverse in (("", False), ("_r", True)):
                        options = choose_options(subjects, reverse=reverse)
                        evidence = _choose_evidence(spec, subjects, options)
                        key = (
                            key_subject.subject_id,
                            f"{group_due.question.id}{order}",
                            model,
                            group_due.question.prompt_version,
                            spec.first_tier,
                            evidence.digest,
                        )
                        if ledger.has_key(key_subject, key):
                            continue
                        stats.episodes_with_work.add(key_subject.episode_uid)
                        stats.counts[f"adjudication_{group_due.sample}"] += 1
                        meeting.setdefault(model, []).append(
                            Item(
                                group_due.question,
                                spec.first_tier,
                                evidence,
                                tuple(subjects),
                                group_due.sample,
                                options,
                                order,
                            )
                        )
            for model, items in meeting.items():
                units.setdefault(("adjudicator", model), []).append(items)
                planned += len(items)
    return units


def run(episodes: Sequence[Any], ctx: JudgingContext) -> JudgingStats:
    # The per-pass texts cache below wraps ctx.texts_for; restore it so a reused context retries
    # reads that failed in this pass.
    texts_for = ctx.texts_for
    try:
        return _run(episodes, ctx)
    finally:
        ctx.texts_for = texts_for


def _run(episodes: Sequence[Any], ctx: JudgingContext) -> JudgingStats:
    stats = JudgingStats()
    # Evidence is rebuilt when planning and again when deciding completeness; read each episode's
    # transcript once per pass. A read that failed stays failed for the pass (no second attempt).
    texts_cache: dict[int, EpisodeTexts | TextsUnavailable] = {}
    texts_for = ctx.texts_for

    def cached_texts(ep: Any) -> EpisodeTexts:
        if id(ep) not in texts_cache:
            try:
                texts_cache[id(ep)] = texts_for(ep)
            except TextsUnavailable as exc:
                texts_cache[id(ep)] = exc
        cached = texts_cache[id(ep)]
        if isinstance(cached, TextsUnavailable):
            raise cached
        return cached

    ctx.texts_for = cached_texts
    ordered = sorted(episodes, key=lambda ep: str(getattr(ep, "published", "") or ""), reverse=True)
    all_subjects = [s for ep in ordered for spec in ctx.tasks for s in spec.subjects(ep)]
    episodes_by_uid = {_episode_uid(ep): ep for ep in ordered}
    _collect(all_subjects, ctx, stats, episodes_by_uid)
    _apply_backpressure(ctx, stats)
    units = plan(ordered, ctx, stats, _item_budget(ctx), bounded=True)
    adjudicator_left = ctx.run_caps.get(ADJUDICATOR_PURPOSE)
    follow_up = plan_adjudication(
        ordered,
        ctx,
        stats,
        None if adjudicator_left is None else max(0, adjudicator_left) * ADJUDICATOR_CEILING[1],
        bounded=True,
    )
    for judge, judge_units in follow_up.items():
        units.setdefault(judge, []).extend(judge_units)
    for (role, model), judge_units in sorted(units.items()):
        purpose = PURPOSES[role]
        backend = _backend_for(role, model, ctx)
        result = pack(judge_units, backend, purpose=purpose, role=role, judge_model=model)
        stats.counts["payload_too_large"] += result.payload_too_large
        if result.units_split:
            stats.counts["units_split"] += result.units_split
        for packet in result.packets:
            if ctx.stop is not None and ctx.stop():
                stats.counts["stopped"] += 1
                return _finish(ordered, ctx, stats)
            if ctx.dry_run:
                # A rehearsal of one real pass: it spends the shared run caps (submitting nothing),
                # so its counts and its bounded planning match what a scheduled run would send.
                if not _take_run_slot(ctx, purpose):
                    stats.counts[f"run_cap:{purpose}"] += 1
                    break
                stats.counts[f"dry_run_packets:{role}"] += 1
                stats.counts["dry_run_items"] += len(packet.items)
                stats.counts["dry_run_estimated_tokens"] += sum(
                    item_size(backend, item) for item in packet.items
                )
                continue
            if not _take_run_slot(ctx, purpose):
                stats.counts[f"run_cap:{purpose}"] += 1
                break
            try:
                outcome = ctx.submit(packet, backend.build(packet))
            except Exception as exc:  # noqa: BLE001 -- a failed submission writes no marker
                _return_run_slot(ctx, purpose)
                stats.counts["errored"] += 1
                stats.counts[f"errored:{type(exc).__name__}"] += 1
                continue
            stats.counts[f"packets:{role}"] += 1
            stats.counts["items_submitted"] += len(packet.items)
            if isinstance(outcome, JobResult):
                _record(packet, backend.parse(packet, outcome.output), stats)
            elif isinstance(outcome, JobHandle):
                for item in packet.items:
                    for subject in item.subjects:
                        ledger.add_pending(subject, _marker(packet, item, subject))
            else:
                stats.counts["errored"] += 1
    return _finish(ordered, ctx, stats)


def _finish(episodes, ctx: JudgingContext, stats: JudgingStats) -> JudgingStats:
    """An episode is complete when nothing is due and nothing is in flight for it.

    Only episodes this pass planned are considered: one the bounded plan never reached was not
    examined and stays dirty. Their texts are cached, so this re-plan reads no transcript."""
    episodes = [ep for ep in episodes if _episode_uid(ep) in stats.episodes_planned]
    due = plan(episodes, ctx, JudgingStats())
    for judge, judge_units in plan_adjudication(episodes, ctx, JudgingStats()).items():
        due.setdefault(judge, []).extend(judge_units)
    due_episodes = {
        subject.episode_uid
        for units in due.values()
        for unit in units
        for item in unit
        for subject in item.subjects
    }
    for ep in episodes:
        uid = _episode_uid(ep)
        subjects = [s for spec in ctx.tasks for s in spec.subjects(ep)]
        if not subjects or uid in due_episodes or uid in stats.episodes_unavailable:
            continue
        if any(ledger.pending(s) for s in subjects):
            continue
        stats.episodes_complete.add(uid)
    return stats


def episode_needs_judging(
    ep: Any,
    tasks: Sequence[TaskSpec],
    *,
    anchor: LaneConfig,
    sibling: LaneConfig,
    families: Mapping[str, str],
    all_tiers_sample_rate: float,
    adjudicator: LaneConfig | None = None,
    calibration_sample_rate: float = 0.0,
    stability_sample_rate: float = 0.0,
) -> bool:
    """Whether a pass would find work for this episode, from stored judgments alone.

    This is the judge stage's dirtiness check (``stages.stage_is_dirty``). It reads no transcript,
    so an unchanged, fully judged episode costs nothing on the two-hourly cadence: a judgment is
    "present" when a row exists for the (subject, question, judge model, prompt version, tier),
    whatever its evidence digest. An episode without a transcript is never dirty -- every tier
    but a tag's matched spans needs one.
    """
    if not getattr(ep, "transcript_key", None):
        return False
    identity = transcript_identity(ep)
    anchor_model = anchor.primary_model
    for spec in tasks:
        subjects = spec.subjects(ep)
        for subject in subjects:
            if ledger.pending(subject):
                return True
            rows = ledger.judgments(subject)
            skipped = ledger.unbuildable(subject)
            present = {
                (
                    r.get("question_id"),
                    r.get("judge_model"),
                    r.get("prompt_version"),
                    r.get("context_tier"),
                )
                for r in rows
            }
            judge_model = sibling_for(subject.producer_model, sibling, families)
            judges = [("anchor", anchor_model)] + (
                [("sibling", judge_model)] if judge_model else []
            )
            ctx = JudgingContext(
                tasks=tasks,
                anchor=anchor,
                sibling=sibling,
                families=families,
                texts_for=lambda _ep: EpisodeTexts(),
                look_up=lambda _recipe: None,
                submit=lambda _packet, _job: None,
                all_tiers_sample_rate=all_tiers_sample_rate,
            )
            for role, model in judges:
                for tier, _sample in _due_tiers(subject, spec, role, ctx):
                    for question in spec.questions:
                        if question.kind == "choose":
                            continue
                        if (question.id, model, question.prompt_version, tier) in present:
                            continue
                        key = ledger.unbuildable_key(
                            question.id, model, question.prompt_version, tier
                        )
                        if skipped.get(key) == identity:
                            continue
                        return True
            if stability_sample_rate > 0 and in_sample(
                f"{subject.subject_id}:stability", stability_sample_rate
            ):
                for _role, model in judges:
                    for question in spec.questions:
                        if question.kind == "choose":
                            continue
                        base = (question.id, model, question.prompt_version, spec.first_tier)
                        again = (f"{question.id}{STABILITY_ORDER}", *base[1:])
                        if base in present and again not in present:
                            return True
        if adjudicator is not None and pending_adjudication(
            subjects,
            spec,
            anchor_model=anchor_model,
            calibration_rate=calibration_sample_rate,
            adjudicator=adjudicator,
            families=families,
            identity=identity,
        ):
            return True
        choose = [q for q in spec.questions if q.kind == "choose"]
        if choose and len(subjects) >= 2:
            first = subjects[0]
            asked = {(r.get("question_id"), r.get("judge_model")) for r in ledger.judgments(first)}
            judges = [anchor_model]
            meeting_sibling = choose_sibling(subjects, sibling, families)
            if meeting_sibling:
                judges.append(meeting_sibling)
            for model in judges:
                for order in ("", "_r"):
                    if (f"{choose[0].id}{order}", model) not in asked:
                        return True
    return False
