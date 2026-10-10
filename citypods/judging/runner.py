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

import hashlib
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from citypods.compute.base import JobHandle, JobResult
from citypods.compute.llm_lanes import LaneConfig
from citypods.judging import ledger
from citypods.judging.backends import (
    ChatJudgeBackend,
    Item,
    JevBackend,
    Packet,
    choose_options,
    estimate_tokens,
)
from citypods.judging.families import UNKNOWN_FAMILY, family_of, sibling_for
from citypods.judging.packing import pack
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


@dataclass
class JudgingStats:
    counts: Counter = field(default_factory=Counter)
    episodes_complete: set[str] = field(default_factory=set)
    episodes_with_work: set[str] = field(default_factory=set)


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


def in_sample(subject_id: str, rate: float) -> bool:
    bucket = int(hashlib.sha256(subject_id.encode()).hexdigest()[:8], 16) % 10_000
    return bucket < int(rate * 10_000)


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


def _backend_for(role: str, model: str):
    return JevBackend() if role == "anchor" else _sibling_backend(model)


def _record(packet: Packet, rows, stats: JudgingStats) -> None:
    for subject, row in rows:
        if ledger.append_judgment(subject, row):
            stats.counts["judgments_appended"] += 1


def _collect(subjects: Sequence[Subject], ctx: JudgingContext, stats: JudgingStats) -> None:
    """Turn finished in-flight questions into judgments; drop markers that can never finish."""
    from citypods.judging.tasks import task as task_spec

    outputs: dict[str, Any] = {}
    for subject in subjects:
        for marker in ledger.pending(subject):
            recipe = str(marker.get("recipe_hash") or "")
            if recipe not in outputs:
                outputs[recipe] = ctx.look_up(recipe) if recipe else None
            record = outputs[recipe]
            if isinstance(record, JobHandle):
                stats.counts["still_pending"] += 1
                continue
            ledger.drop_pending(subject, marker)
            if not isinstance(record, JobResult):
                # No record (a failed submission, a cancelled or structurally blocked packet): the
                # question becomes due again and is re-planned for the current judges. This is the
                # Slice 3a rebatch branch -- never an unchanged-recipe resubmission.
                stats.counts["replanned"] += 1
                continue
            question_id = str(marker.get("question_id") or "")
            question = task_spec(subject.task).question(question_id.removesuffix("_r"))
            tier = str(marker["context_tier"])
            item = Item(
                question=question,
                tier=tier,
                # Answers are keyed by qid; the digest is the one the question was asked with.
                evidence=Evidence(tier, "", str(marker["evidence_digest"])),
                subjects=(subject,),
                sample=str(marker.get("sample") or "routine"),
                options=tuple(tuple(pair) for pair in marker.get("options") or ()),
                order="_r" if question_id.endswith("_r") else "",
                asked_qid=str(marker["qid"]),
            )
            packet = Packet(
                str(marker.get("purpose") or ""),
                str(marker.get("judge_role") or ""),
                str(marker["judge_model"]),
                (item,),
            )
            rows = _backend_for(packet.role, packet.judge_model).parse(packet, record.output)
            for _subject, row in rows:
                row["packet_id"] = recipe
            if not rows:
                stats.counts["answer_missing"] += 1
            _record(packet, rows, stats)


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


def plan(
    episodes: Sequence[Any], ctx: JudgingContext, stats: JudgingStats
) -> dict[tuple[str, str], list[list[Item]]]:
    """Units of due items per (role, judge model), in recent-first episode order."""
    units: dict[tuple[str, str], list[list[Item]]] = {}
    anchor_model = ctx.anchor.primary_model
    for ep in episodes:
        texts: EpisodeTexts | None = None
        for spec in ctx.tasks:
            subjects = spec.subjects(ep)
            if not subjects:
                continue
            if texts is None:
                texts = ctx.texts_for(ep)
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


def choose_sibling(
    subjects: Sequence[Subject], sibling: LaneConfig, families: Mapping[str, str]
) -> str | None:
    """One sibling for a whole meeting's choose question: independent of every producer in it."""
    producer_families = {family_of(s.producer_model, families) for s in subjects}
    if UNKNOWN_FAMILY in producer_families:
        return None
    return next((m for m in sibling.models if families.get(m) not in producer_families), None)


def _plan_choose(spec, subjects, texts, ctx, group_units) -> None:
    choose = [q for q in spec.questions if q.kind == "choose"]
    if not choose or len(subjects) < 2:
        return
    question = choose[0]
    sibling = choose_sibling(subjects, ctx.sibling, ctx.families)
    judges = [("anchor", ctx.anchor.primary_model)] + ([("sibling", sibling)] if sibling else [])
    for reverse, order in ((False, ""), (True, "_r")):
        options = choose_options(subjects, reverse=reverse)
        by_id = {s.subject_id: s for s in subjects}
        lines = [f"{letter}: {by_id[sid].payload.get('quote')}" for letter, sid in options]
        evidence = make_evidence(spec.first_tier, "\n".join(lines))
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


def run(episodes: Sequence[Any], ctx: JudgingContext) -> JudgingStats:
    stats = JudgingStats()
    # Evidence is rebuilt when planning and again when deciding completeness; read each episode's
    # transcript once per pass.
    texts_cache: dict[int, EpisodeTexts] = {}
    texts_for = ctx.texts_for

    def cached_texts(ep: Any) -> EpisodeTexts:
        if id(ep) not in texts_cache:
            texts_cache[id(ep)] = texts_for(ep)
        return texts_cache[id(ep)]

    ctx.texts_for = cached_texts
    ordered = sorted(episodes, key=lambda ep: str(getattr(ep, "published", "") or ""), reverse=True)
    all_subjects = [s for ep in ordered for spec in ctx.tasks for s in spec.subjects(ep)]
    _collect(all_subjects, ctx, stats)
    units = plan(ordered, ctx, stats)
    for (role, model), judge_units in sorted(units.items()):
        purpose = ANCHOR_PURPOSE if role == "anchor" else SIBLING_PURPOSE
        backend = _backend_for(role, model)
        result = pack(judge_units, backend, purpose=purpose, role=role, judge_model=model)
        stats.counts["payload_too_large"] += result.payload_too_large
        for packet in result.packets:
            if ctx.stop is not None and ctx.stop():
                stats.counts["stopped"] += 1
                return _finish(ordered, ctx, stats)
            if ctx.dry_run:
                stats.counts[f"dry_run_packets:{role}"] += 1
                stats.counts["dry_run_items"] += len(packet.items)
                stats.counts["dry_run_estimated_tokens"] += sum(
                    estimate_tokens(item.text()) for item in packet.items
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
    """An episode is complete when nothing is due and nothing is in flight for it."""
    due = plan(episodes, ctx, JudgingStats())
    due_episodes = {
        subject.episode_uid
        for units in due.values()
        for unit in units
        for item in unit
        for subject in item.subjects
    }
    for ep in episodes:
        uid = str(getattr(ep, "uid", None) or getattr(ep, "guid", "") or "")
        subjects = [s for spec in ctx.tasks for s in spec.subjects(ep)]
        if not subjects or uid in due_episodes:
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
