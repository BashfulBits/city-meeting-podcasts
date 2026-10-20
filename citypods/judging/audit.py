"""The weekly blind human audit (review/49 section 4a, review/53 PR6).

A person answers at most ``MAX_ITEMS`` binary claims a week, chosen by expected information rather
than at random: items where the judges and adjudicator disagree, items near the anchor's decision
threshold, items judged by the judge with the newest or noisiest record, plus about
``RANDOM_ITEMS`` random ones so the sample stays honest. Each claim is framed to disprove, shows the
evidence text first, and **hides every judge's verdict until it is answered**. The choices are
Supported, Not supported and Uncertain; Uncertain is first-class and never a label.

A human answer is **one vote, never an override**: it joins the ledger as a ``judge_role: "audit"``
judgment, a disagreement with the stack queues full-context adjudication, repeated disagreement
with the adjudicator on a task files a ticket, and nothing flips automatically. If more than 30% of
a task's answers in a week are Uncertain the format is judged broken for that task: its labels from
that batch are discarded, the next batch shrinks, and a ticket says which item type failed.

Items are delivered through the shared review pipeline (``citypods.review_issues``): one child issue
per claim under one weekly parent, answered by checking exactly one box. State lives in
``state/judge_audit/{items,answers}.json``; the judge lane imports finalized answers into each
episode's ``judging`` block, the only place judgments are written.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from citypods.judging import ledger
from citypods.judging.tasks import TIERS, QuestionSpec, Subject, TaskSpec

FAMILY = "judge-audit"
LABEL = "agent:judge-audit"
TICKET_LABEL = "signal:judge-audit"
CHOICES = ("Supported", "Not supported", "Uncertain")
VERDICTS = {"Supported": "supported", "Not supported": "not_supported", "Uncertain": "uncertain"}
MAX_ITEMS = 12
RANDOM_ITEMS = 2
CLEAN_PROBE_ITEMS = 4  # a week's items once probes are clean and stable ("0 to 4")
MIN_ITEMS = 2
UNCERTAIN_LIMIT = 0.30
SETTLE_DAYS = 6
THRESHOLD_BAND = 0.15  # an anchor probability within this of 0.5 is "near the threshold"
DISAGREEMENT_TICKET_MIN = 3
DISAGREEMENT_WINDOW_DAYS = 28
PROMPT_VERSION = "audit-1"
AUDIT_MODEL = "human"
AUDIT_ROLE = "audit"
STATE_DIR = "judge_audit"


def item_id(subject_id: str, question_id: str) -> str:
    """One audit item per subject-question, for ever: a person's one vote on it."""
    return hashlib.sha256(f"{subject_id}|{question_id}".encode()).hexdigest()[:24]


def batch_id(now: datetime) -> str:
    year, week, _ = now.isocalendar()
    return f"{year}-W{week:02d}"


# ---- state ------------------------------------------------------------------------------------


@dataclass
class AuditState:
    items: dict[str, dict[str, Any]] = field(default_factory=dict)
    batches: dict[str, dict[str, Any]] = field(default_factory=dict)
    answers: dict[str, dict[str, Any]] = field(default_factory=dict)
    shrink: dict[str, float] = field(default_factory=dict)

    @classmethod
    def load(cls, state_dir: Path) -> AuditState:
        root = Path(state_dir) / STATE_DIR
        items = _read(root / "items.json")
        answers = _read(root / "answers.json")
        return cls(
            items=dict(items.get("items") or {}),
            batches=dict(items.get("batches") or {}),
            shrink={k: float(v) for k, v in (items.get("shrink") or {}).items()},
            answers=dict(answers.get("answers") or {}),
        )

    def save(self, state_dir: Path) -> list[Path]:
        root = Path(state_dir) / STATE_DIR
        root.mkdir(parents=True, exist_ok=True)
        paths = [root / "items.json", root / "answers.json"]
        _write(
            paths[0],
            {"version": 1, "items": self.items, "batches": self.batches, "shrink": self.shrink},
        )
        _write(paths[1], {"version": 1, "answers": self.answers})
        return paths


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _write(path: Path, value: Mapping[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


# ---- selection --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    item_id: str
    task: str
    city: str
    episode: Any
    subject: Subject
    question: QuestionSpec
    spec: TaskSpec
    score: float
    reasons: tuple[str, ...]
    judges: frozenset[str]


def _binary(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value) >= 0.5
    return None


def final_reading(rows: Sequence[Mapping[str, Any]], role: str) -> tuple[Any, str | None]:
    """A judge role's value at its highest tier (adjudicator evidence required), and its model."""
    best: tuple[int, Any, str | None] | None = None
    for row in rows:
        if row.get("judge_role") != role or not ledger.usable_vote(row):
            continue
        tier = row.get("context_tier")
        rank = TIERS.index(tier) if tier in TIERS else -1
        if best is None or rank >= best[0]:
            best = (rank, row.get("value"), row.get("judge_model"))
    return (None, None) if best is None else (best[1], best[2])


def judge_noise(episodes: Iterable[Any]) -> dict[str, float]:
    """Per judge model: the share of its rows that flipped on a stability re-ask or whose quote
    was not in the evidence. The noisiest model's items carry extra weight in selection."""
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for ep in episodes:
        entries = ((getattr(ep, "judging", None) or {}).get("subjects") or {}).values()
        for entry in entries:
            rows = [r for r in (entry or {}).get("judgments") or [] if isinstance(r, dict)]
            routine = {
                (r.get("judge_model"), r.get("question_id"), r.get("context_tier")): _binary(
                    r.get("value")
                )
                for r in rows
                if r.get("sample") == "routine"
            }
            for row in rows:
                model = str(row.get("judge_model"))
                if row.get("judge_role") in ("sibling", "adjudicator"):
                    counts[model][1] += 1
                    counts[model][0] += row.get("quote_valid") is False
                if row.get("sample") == "stability":
                    base = str(row.get("question_id") or "").removesuffix("_s")
                    first = routine.get((row.get("judge_model"), base, row.get("context_tier")))
                    again = _binary(row.get("value"))
                    if first is not None and again is not None:
                        counts[model][1] += 1
                        counts[model][0] += first != again
    return {model: bad / total for model, (bad, total) in counts.items() if total}


NEWEST_JUDGE_DAYS = 14


def newest_judge(episodes: Iterable[Any], now: datetime | None = None) -> str | None:
    """A judge model first seen in the last two weeks, when others were seen before it.

    A newly seated route has no track record yet, so its items are worth a person's look; with a
    single judge, or one that has been judging for a while, there is no newest judge."""
    first_seen: dict[str, str] = {}
    for ep in episodes:
        entries = ((getattr(ep, "judging", None) or {}).get("subjects") or {}).values()
        for entry in entries:
            for row in (entry or {}).get("judgments") or []:
                model, at = str(row.get("judge_model")), str(row.get("judged_at") or "")
                if row.get("judge_role") == AUDIT_ROLE or not at:
                    continue
                if model not in first_seen or at < first_seen[model]:
                    first_seen[model] = at
    if len(first_seen) < 2:
        return None
    newest = max(first_seen, key=lambda m: first_seen[m])
    if first_seen[newest] <= min(first_seen.values()):
        return None  # seated together: none is newer than the rest
    cutoff = ((now or datetime.now().astimezone()) - timedelta(days=NEWEST_JUDGE_DAYS)).isoformat()
    return newest if first_seen[newest] >= cutoff else None


def gather(
    episodes: Iterable[tuple[str, Any]],
    tasks: Sequence[TaskSpec],
    *,
    audited: Iterable[str] = (),
    noisy: str | None = None,
    newest: str | None = None,
) -> list[Candidate]:
    """Every binary (``validate``) claim with enough judgments to be worth a human's minute.

    A claim is worth auditing when it has both an anchor and a sibling reading. Its score is the
    expected information of the answer: an adjudicator that disagrees with either judge is worth
    the most, then judge disagreement, then closeness to the anchor's threshold, then a judge
    that is the noisiest or newest."""
    seen = set(audited)
    found: list[Candidate] = []
    for city, ep in episodes:
        for spec in tasks:
            for subject in spec.subjects(ep):
                rows = ledger.judgments(subject)
                for question in spec.questions:
                    ident = item_id(subject.subject_id, question.id)
                    if question.kind != "validate" or ident in seen:
                        continue
                    own = [r for r in rows if r.get("question_id") == question.id]
                    anchor, anchor_model = final_reading(own, "anchor")
                    sibling, sibling_model = final_reading(own, "sibling")
                    adjudicator, adjudicator_model = final_reading(own, "adjudicator")
                    p = anchor if isinstance(anchor, (int, float)) else None
                    if p is None or _binary(sibling) is None:
                        continue
                    score, reasons = 0.0, []
                    a_vote, s_vote = _binary(anchor), _binary(sibling)
                    j_vote = _binary(adjudicator)
                    if j_vote is not None and (j_vote != a_vote or j_vote != s_vote):
                        score += 3.0
                        reasons.append("adjudicator_disagrees")
                    if a_vote != s_vote:
                        score += 2.0
                        reasons.append("judges_disagree")
                    closeness = max(0.0, THRESHOLD_BAND - abs(float(p) - 0.5)) / THRESHOLD_BAND
                    if closeness > 0:
                        score += 2.0 * closeness
                        reasons.append("near_threshold")
                    judges = frozenset(
                        str(m) for m in (anchor_model, sibling_model, adjudicator_model) if m
                    )
                    if noisy and noisy in judges:
                        score += 1.0
                        reasons.append("noisiest_judge")
                    if newest and newest in judges:
                        score += 1.0
                        reasons.append("newest_judge")
                    found.append(
                        Candidate(
                            item_id=ident,
                            task=spec.name,
                            city=city,
                            episode=ep,
                            subject=subject,
                            question=question,
                            spec=spec,
                            score=score,
                            reasons=tuple(reasons),
                            judges=judges,
                        )
                    )
    return found


def select(candidates: Sequence[Candidate], *, limit: int, week: str) -> list[Candidate]:
    """The top by expected information, then up to two random ones, at most ``limit`` in all.

    The random ones keep the sample honest: they come from everything not already chosen, so a
    well-judged item can be picked too. A week with little to ask returns fewer than ``limit``."""
    if limit <= 0 or not candidates:
        return []
    randoms = 0 if limit <= 1 else 1 if limit <= 4 else RANDOM_ITEMS
    informative = sorted(
        (c for c in candidates if c.score > 0), key=lambda c: (-c.score, _tie(c, week))
    )[: max(0, limit - randoms)]
    chosen = {c.item_id for c in informative}
    rest = sorted((c for c in candidates if c.item_id not in chosen), key=lambda c: _tie(c, week))
    return informative + rest[: min(randoms, limit - len(informative))]


def _tie(candidate: Candidate, week: str) -> str:
    return hashlib.sha256(f"{week}|{candidate.item_id}".encode()).hexdigest()


def weekly_item_count(
    *, probes_clean: bool = False, shrink: Mapping[str, float] | None = None
) -> int:
    """``min(12, n)``: a quiet week is a valid outcome. Clean, stable probes shrink it toward
    ``CLEAN_PROBE_ITEMS``; a task whose last batch was discarded halves it again."""
    count = CLEAN_PROBE_ITEMS if probes_clean else MAX_ITEMS
    factor = min((shrink or {}).values(), default=1.0)
    return max(MIN_ITEMS, int(count * factor)) if factor < 1.0 else count


# ---- rendering --------------------------------------------------------------------------------

_CLAIMS = {
    ("tag", "supported"): "The proposed tag is correct for this part of the meeting.",
    ("moment", "supported"): "The quote, as spoken, says what the stated reason claims.",
}


def claim_for(task: str, question: QuestionSpec) -> str:
    return _CLAIMS.get((task, question.id), question.instruction)


def item_type(candidate: Candidate) -> str:
    """The kind of item, for the ticket that says which type fails the format."""
    payload = candidate.subject.payload or {}
    if candidate.task == "tag":
        return "tag:rule" if payload.get("source_kind") == "rule" else "tag:llm"
    return candidate.task


def render_child(candidate: Candidate, evidence_text: str, *, audio_url: str | None = None) -> str:
    """The issue body: the claim framed to disprove, the evidence, the rubric. No verdicts."""
    lines = [
        f"# Judge audit: {candidate.task}",
        "",
        "**Claim. Try to disprove it:** " + claim_for(candidate.task, candidate.question),
        "",
        *(f"> {line}" if line else ">" for line in evidence_text.splitlines()),
    ]
    if audio_url:
        lines += ["", f"Listen to the moment: {audio_url}"]
    lines += [
        "",
        "**Rubric.** " + candidate.question.instruction,
        "",
        "Choose exactly one. *Uncertain* is a fine answer and is never counted as a label. "
        "The automated verdicts are hidden until you answer.",
        "",
    ]
    from citypods.review_issues import render_decision_block

    lines.append(render_decision_block(CHOICES))
    return "\n".join(lines) + "\n"


def render_parent(batch: str, count: int, tasks: Mapping[str, int]) -> str:
    detail = ", ".join(f"{n} {task}" for task, n in sorted(tasks.items())) or "none"
    return (
        f"# Weekly judge audit {batch}\n\n"
        f"{count} claim(s) ({detail}), about 45 seconds each. Answer each child with exactly one "
        "choice: Supported, Not supported or Uncertain. Your answer is one vote: it never "
        "overrides a judge on its own, and a disagreement queues a full-context adjudication.\n"
    )


# ---- answers and finalisation -----------------------------------------------------------------


def record_answer(
    state: AuditState, ident: str, choice: str, *, actor: str, issue: int, now: datetime
) -> dict[str, Any]:
    """Store a person's answer for a packaged item; replace it only until its batch is settled."""
    item = state.items.get(ident)
    if item is None:
        raise ValueError("audit item is not in the durable ledger")
    if choice not in VERDICTS:
        raise ValueError(f"unknown audit choice {choice!r}")
    batch = state.batches.get(item.get("batch", ""), {})
    if batch.get("status", "open") != "open":
        return {"stored": False, "reason": "batch_settled", "item_id": ident}
    previous = state.answers.get(ident)
    state.answers[ident] = {
        "verdict": VERDICTS[choice],
        "actor": actor,
        "issue": issue,
        "at": now.isoformat(timespec="seconds"),
        "revisions": (previous or {}).get("revisions", -1) + 1,
    }
    return {"stored": True, "item_id": ident, "verdict": VERDICTS[choice]}


@dataclass(frozen=True)
class Settlement:
    batch: str
    task: str
    answered: int
    uncertain: int
    discarded: bool
    failing_types: tuple[tuple[str, int], ...] = ()


def settle(state: AuditState, now: datetime, *, force: bool = False) -> list[Settlement]:
    """Settle every open batch old enough: keep a task's labels, or discard them when more than
    30% of its answers were Uncertain (and shrink its next batch).

    Ingest runs in another workflow, so an answer can land while a batch settles. It is stored and
    later imported all the same (import reads every answer of a finalized batch); at worst the 30%
    tally of that one week missed it."""
    results: list[Settlement] = []
    for name, batch in sorted(state.batches.items()):
        if batch.get("status", "open") != "open":
            continue
        created = datetime.fromisoformat(batch["created"])
        if not force and now - created < timedelta(days=SETTLE_DAYS):
            continue
        per_task: dict[str, list[str]] = defaultdict(list)
        kinds: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for ident in batch.get("item_ids", []):
            item = state.items.get(ident) or {}
            answer = state.answers.get(ident)
            if answer is None:
                continue
            per_task[item.get("task", "")].append(answer["verdict"])
            if answer["verdict"] == "uncertain":
                kinds[item.get("task", "")][item.get("item_type", "")] += 1
        task_status: dict[str, str] = {}
        for task, verdicts in sorted(per_task.items()):
            uncertain = verdicts.count("uncertain")
            broken = uncertain / len(verdicts) > UNCERTAIN_LIMIT
            task_status[task] = "discarded" if broken else "finalized"
            if broken:
                state.shrink[task] = max(0.25, state.shrink.get(task, 1.0) / 2)
            results.append(
                Settlement(
                    name,
                    task,
                    len(verdicts),
                    uncertain,
                    broken,
                    tuple(sorted(kinds[task].items(), key=lambda kv: -kv[1])) if broken else (),
                )
            )
        batch["tasks"] = task_status
        batch["status"] = "finalized"
        batch["settled"] = now.isoformat(timespec="seconds")
    return results


def import_rows(state: AuditState) -> dict[str, list[dict[str, Any]]]:
    """Ledger rows for finalized, un-discarded answers, by subject id (the judge lane appends them).

    An *Uncertain* answer is stored (value ``None``) so it is never asked again, and is excluded
    from every statistic by having no value."""
    rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ident, answer in state.answers.items():
        item = state.items.get(ident)
        if item is None:
            continue
        batch = state.batches.get(item.get("batch", ""), {})
        if batch.get("status") != "finalized" or (batch.get("tasks") or {}).get(
            item.get("task", "")
        ) not in (None, "finalized"):
            continue
        verdict = answer["verdict"]
        rows[item["subject_id"]].append(
            {
                "task": item["task"],
                "subject_id": item["subject_id"],
                "question_id": item["question_id"],
                "kind": "validate",
                "judge_model": AUDIT_MODEL,
                "judge_role": AUDIT_ROLE,
                "prompt_version": PROMPT_VERSION,
                "context_tier": item["tier"],
                "evidence_digest": item["evidence_digest"],
                "packet_id": f"audit-{ident}",
                "sample": "audit",
                "value": None if verdict == "uncertain" else verdict == "supported",
                "probabilities": None,
                "confidence": None,
                "reason": None,
                "quote_valid": None,
                "judged_at": answer["at"],
            }
        )
    return dict(rows)


def disagreements(
    episodes: Iterable[Any], tasks: Sequence[TaskSpec], now: datetime
) -> dict[str, int]:
    """Audit-versus-adjudicator disagreements per task in the last four weeks (supported rows)."""
    since = (now - timedelta(days=DISAGREEMENT_WINDOW_DAYS)).isoformat()
    counts: dict[str, int] = defaultdict(int)
    for ep in episodes:
        for spec in tasks:
            for subject in spec.subjects(ep):
                rows = ledger.judgments(subject)
                for question in spec.questions:
                    own = [r for r in rows if r.get("question_id") == question.id]
                    audit = next(
                        (
                            r
                            for r in own
                            if r.get("judge_role") == AUDIT_ROLE
                            and isinstance(r.get("value"), bool)
                            and str(r.get("judged_at") or "") >= since
                        ),
                        None,
                    )
                    adjudicator, _model = final_reading(own, "adjudicator")
                    if audit is not None and _binary(adjudicator) not in (None, audit["value"]):
                        counts[spec.name] += 1
    return dict(counts)


def audit_rows_pending(
    rows_by_subject: Mapping[str, list[dict[str, Any]]], subjects: Sequence[Subject]
) -> bool:
    """Whether any imported audit row is not yet in these subjects' ledgers."""
    for subject in subjects:
        existing = {ledger.key_of(r) for r in ledger.judgments(subject)}
        if any(
            ledger.key_of(row) not in existing
            for row in rows_by_subject.get(subject.subject_id, [])
        ):
            return True
    return False


__all__ = [
    "AuditState",
    "Candidate",
    "CHOICES",
    "FAMILY",
    "batch_id",
    "disagreements",
    "gather",
    "import_rows",
    "item_id",
    "item_type",
    "judge_noise",
    "newest_judge",
    "record_answer",
    "render_child",
    "render_parent",
    "select",
    "settle",
    "weekly_item_count",
]
