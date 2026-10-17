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
import time
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
)
from citypods.judging.families import (
    UNKNOWN_FAMILY,
    choose_sibling_candidates,
    family_of,
    sibling_candidates,
)
from citypods.judging.packing import item_size, pack
from citypods.judging.tasks import (
    EpisodeTexts,
    Evidence,
    Subject,
    TaskSpec,
    make_evidence,
    transcript_identity,
)
from citypods.storage.base import StorageReadUnavailable

ANCHOR_PURPOSE = "judge:anchor"
SIBLING_PURPOSE = "judge:sibling"

# Chat sibling ceilings (review/53 PR3 "Packing"): Gemma's AI Studio routes have a 14,400-token
# hard ceiling and a 14,400-token minute budget. The Worker reserves estimate x its input ratio
# (1.4 for these keys) plus the forecast output (about 1,900), so a packet must stay under
# (14,400 - 1,900) / 1.4 = about 8,900 estimated tokens or it can never be admitted there
# (10,000 was measured to strand ~2,600 queued packets, 2026-10-16). Nemotron 3 Super about 24,000.
SIBLING_CEILINGS: dict[str, tuple[int, int]] = {
    "google/gemma-4-31b-it": (8_500, 25),
    "google/gemma-4-26b-a4b-it": (8_500, 25),
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
    # model -> usable route capacity from the Worker's cheap route view (0 when blocked). A missing
    # model counts as 1.0, so with no reading the choice is by backlog alone.
    sibling_headroom: dict[str, float] = field(default_factory=dict)
    # Per-pass load that decides which sibling takes a new subject: packets still waiting at the
    # Worker per model (read while collecting) and items assigned so far this pass.
    sibling_recipes: dict[str, set[str]] = field(default_factory=dict)
    sibling_assigned: Counter = field(default_factory=Counter)
    # model -> packets it may still take this pass under the sibling outstanding limit, which
    # applies to each judge model separately (set by _apply_backpressure; empty: unlimited).
    sibling_room: dict[str, int] = field(default_factory=dict)


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
    # Consecutive unreadable record reads, and whether this pass has stopped trying to read them.
    lookup_streak: int = 0
    lookup_gave_up: bool = False
    lookup_waited: float = 0.0


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


def _backend_for(role: str, model: str, ctx: JudgingContext | None = None):
    if role == "anchor":
        return JevBackend(ctx.anchor_input_ratio if ctx is not None else None)
    return _sibling_backend(model)


def _record(packet: Packet, rows, stats: JudgingStats) -> None:
    for subject, row in rows:
        if ledger.append_judgment(subject, row):
            stats.counts["judgments_appended"] += 1


# A pending packet whose record could not be read this pass (storage outage). Its marker stays and
# it counts as outstanding, exactly like a packet still waiting at the Worker.
_UNREADABLE = object()
# This many unreadable reads in a row means storage itself is down, not one bad object. A down store
# would make each of thousands of markers wait out its own retry budget, so the pass instead waits
# for it to recover (probing the same record), for at most _RECOVERY_WAIT_SECONDS in total. If it
# does not recover in that time the rest are not tried this pass and keep their markers: giving up
# on the first blip (3 reads, 2026-10-17 run 38984100770) threw away a whole pass of collection.
_MAX_UNREADABLE_STREAK = 3
_RECOVERY_WAIT_SECONDS = 300.0
_RECOVERY_PROBE_SECONDS = 30.0
_sleep = time.sleep


def _wait_for_storage(ctx: JudgingContext, recipe: str, stats: JudgingStats) -> Any:
    """Probe ``recipe`` until it reads or the pass's recovery budget is spent."""
    while stats.lookup_waited < _RECOVERY_WAIT_SECONDS:
        pause = min(_RECOVERY_PROBE_SECONDS, _RECOVERY_WAIT_SECONDS - stats.lookup_waited)
        _sleep(pause)
        stats.lookup_waited += pause
        try:
            record = ctx.look_up(recipe)
        except StorageReadUnavailable:
            stats.counts["lookup_probe_failed"] += 1
            continue
        stats.lookup_streak = 0
        stats.counts["lookup_recovered"] += 1
        print(f"[judge] storage readable again after {stats.lookup_waited:.0f}s", flush=True)
        return record
    stats.lookup_gave_up = True
    print("[judge] storage still unavailable; skipping record reads for this pass", flush=True)
    return _UNREADABLE


def _look_up(ctx: JudgingContext, recipe: str, stats: JudgingStats) -> Any:
    """The packet's record, or ``_UNREADABLE`` when storage is unavailable for it (never raises)."""
    if stats.lookup_gave_up:
        stats.counts["lookup_unavailable"] += 1
        stats.counts["lookup_skipped"] += 1
        return _UNREADABLE
    try:
        record = ctx.look_up(recipe)
    except StorageReadUnavailable as exc:
        stats.counts["lookup_unavailable"] += 1
        stats.lookup_streak += 1
        print(f"[judge] record read unavailable for {exc.key}; keeping its marker", flush=True)
        if stats.lookup_streak >= _MAX_UNREADABLE_STREAK:
            return _wait_for_storage(ctx, recipe, stats)
        return _UNREADABLE
    stats.lookup_streak = 0
    return record


def _collect(subjects: Sequence[Subject], ctx: JudgingContext, stats: JudgingStats) -> None:
    """Turn finished in-flight questions into judgments; drop markers that can never finish.

    A record that cannot be read right now (``StorageReadUnavailable``, a transient storage outage)
    is not a missing record: its marker stays and counts as outstanding, and a later pass collects
    it. One unreadable object must never abort the whole lane.
    """
    from citypods.judging.tasks import task as task_spec

    outputs: dict[str, Any] = {}
    for subject in subjects:
        for marker in ledger.pending(subject):
            recipe = str(marker.get("recipe_hash") or "")
            if recipe not in outputs:
                outputs[recipe] = _look_up(ctx, recipe, stats) if recipe else None
            record = outputs[recipe]
            if record is _UNREADABLE or isinstance(record, JobHandle):
                stats.counts["still_pending"] += 1
                purpose = str(marker.get("purpose") or "")
                stats.outstanding.setdefault(purpose, set()).add(recipe)
                if purpose == SIBLING_PURPOSE:
                    ctx.sibling_recipes.setdefault(str(marker.get("judge_model") or ""), set()).add(
                        recipe
                    )
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
                    outputs[recipe] = _look_up(ctx, recipe, stats)
                if outputs[recipe] is _UNREADABLE or isinstance(outputs[recipe], JobHandle):
                    purpose = str(marker.get("purpose") or "")
                    stats.outstanding.setdefault(purpose, set()).add(recipe)
                    if purpose == SIBLING_PURPOSE:
                        ctx.sibling_recipes.setdefault(
                            str(marker.get("judge_model") or ""), set()
                        ).add(recipe)
                    stats.counts["superseded_pending"] += 1


def _apply_backpressure(ctx: JudgingContext, stats: JudgingStats) -> None:
    """Shrink each purpose's run cap to the room left under its outstanding-packet limit.

    The sibling limit applies to each judge model on its own: a backlog on one model (2,800
    Gemma 31B packets, 2026-10-16) must not stop the pool's other models taking new work, so the
    purpose's room is the sum of every sibling model's room.
    """
    ctx.sibling_room.clear()
    for purpose, limit in ctx.max_outstanding.items():
        outstanding = len(stats.outstanding.get(purpose, ()))
        room = max(0, int(limit) - outstanding)
        if purpose == SIBLING_PURPOSE:
            ctx.sibling_room.update(
                {
                    model: max(0, int(limit) - len(ctx.sibling_recipes.get(model, ())))
                    for model in ctx.sibling.models
                }
            )
            room = sum(ctx.sibling_room.values())
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
                sibling = _pick_sibling(
                    sibling_candidates(subject.producer_model, ctx.sibling, ctx.families),
                    _models_used(subject),
                    ctx,
                )
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


def _models_used(subject: Subject) -> set[str]:
    """Judge models that already have a judgment or an in-flight packet for this subject."""
    return {
        str(row.get("judge_model"))
        for row in (*ledger.judgments(subject), *ledger.pending(subject))
        if row.get("judge_model")
    }


def _pick_sibling(
    candidates: Sequence[str], used: set[str], ctx: JudgingContext | None
) -> str | None:
    """The sibling for a subject with no recorded judge: the least-loaded independent candidate.

    A subject keeps the sibling that already judged it (or has a packet in flight): the ledger key
    includes the judge model, so switching would re-ask the same question of a second judge.
    Otherwise the candidate with the fewest packets waiting at the Worker, plus the items already
    assigned this pass, per unit of route headroom wins, ties going to lane order. A model whose
    routes are all blocked has no headroom and is skipped while another candidate has some.
    """
    if not candidates:
        return None
    for model in candidates:
        if model in used:
            return model
    if ctx is None or len(candidates) == 1:
        return candidates[0]

    def load(model: str) -> float:
        weight = ctx.sibling_headroom.get(model, 1.0)
        if weight <= 0 or ctx.sibling_room.get(model, 1) <= 0:
            return float("inf")
        per_packet = SIBLING_CEILINGS.get(model, DEFAULT_SIBLING_CEILING)[1]
        waiting = len(ctx.sibling_recipes.get(model, ())) + ctx.sibling_assigned[model] / per_packet
        return waiting / weight

    chosen = min(enumerate(candidates), key=lambda pair: (load(pair[1]), pair[0]))[1]
    ctx.sibling_assigned[chosen] += 1
    return chosen


def _plan_choose(spec, subjects, texts, ctx, group_units) -> None:
    choose = [q for q in spec.questions if q.kind == "choose"]
    if not choose or len(subjects) < 2:
        return
    question = choose[0]
    sibling = _pick_sibling(
        choose_sibling_candidates([s.producer_model for s in subjects], ctx.sibling, ctx.families),
        _models_used(subjects[0]),
        ctx,
    )
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
    ctx.sibling_recipes.clear()
    ctx.sibling_assigned.clear()
    _collect(all_subjects, ctx, stats)
    _apply_backpressure(ctx, stats)
    units = plan(ordered, ctx, stats, _item_budget(ctx), bounded=True)
    for (role, model), judge_units in sorted(units.items()):
        purpose = ANCHOR_PURPOSE if role == "anchor" else SIBLING_PURPOSE
        backend = _backend_for(role, model, ctx)
        result = pack(judge_units, backend, purpose=purpose, role=role, judge_model=model)
        stats.counts["payload_too_large"] += result.payload_too_large
        if result.units_split:
            stats.counts["units_split"] += result.units_split
        for packet in result.packets:
            if ctx.stop is not None and ctx.stop():
                stats.counts["stopped"] += 1
                return _finish(ordered, ctx, stats)
            if role == "sibling" and ctx.sibling_room.get(model, 1) <= 0:
                stats.counts[f"run_cap_outstanding:{purpose}"] += 1
                break
            if ctx.dry_run:
                # A rehearsal of one real pass: it spends the shared run caps (submitting nothing),
                # so its counts and its bounded planning match what a scheduled run would send.
                if not _take_run_slot(ctx, purpose):
                    stats.counts[f"run_cap:{purpose}"] += 1
                    break
                stats.counts[f"dry_run_packets:{role}"] += 1
                if role == "sibling" and model in ctx.sibling_room:
                    ctx.sibling_room[model] -= 1
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
            if role == "sibling" and model in ctx.sibling_room:
                ctx.sibling_room[model] -= 1
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
            judge_model = _pick_sibling(
                sibling_candidates(subject.producer_model, sibling, families),
                _models_used(subject),
                None,
            )
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
            meeting_sibling = _pick_sibling(
                choose_sibling_candidates([s.producer_model for s in subjects], sibling, families),
                _models_used(first),
                None,
            )
            if meeting_sibling:
                judges.append(meeting_sibling)
            for model in judges:
                for order in ("", "_r"):
                    if (f"{choose[0].id}{order}", model) not in asked:
                        return True
    return False
