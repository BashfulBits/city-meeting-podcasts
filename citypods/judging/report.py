"""What the shadow judges have recorded (review/53 PR3), from stored judgments only.

``python -m citypods.judging.report --state-dir state`` prints judgments per day by role, task and
tier; anchor-versus-sibling agreement at p = 0.5 by tier (the P2 input); the escalation rate; and
backfill progress (share of the last 90 days' candidates judged by both judges at the first tier).
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from citypods.judging import ledger
from citypods.judging.audit import final_reading

CANDIDATE_LISTS = {
    "tag": ("llm_tag_candidates",),
    "moment": ("moment_pullquote_candidates",),
}
FIRST_TIER = "T1"
BACKFILL_DAYS = 90


def _published(record: Mapping[str, Any]) -> datetime | None:
    value = record.get("published")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _binary(row: Mapping[str, Any]) -> bool | None:
    value = row.get("value")
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value) >= 0.5
    return None


def summarize(
    records: Iterable[Mapping[str, Any]],
    *,
    now: datetime | None = None,
    probes: Mapping[str, bool] | None = None,
) -> dict:
    """``probes`` maps a probe id to the verdict a human gave it (``evals/judge/<task>/gold.json``);
    without it a probe's answers are counted but not scored."""
    now = now or datetime.now(UTC)
    probe_results: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    audit_results: dict[str, Counter] = defaultdict(Counter)
    per_day: Counter = Counter()
    agreement: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    escalated = first_tier_anchor = 0
    quotes: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0, 0])
    adjudicated: Counter = Counter()
    stability: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    recent_total = recent_done = 0
    for record in records:
        published = _published(record)
        recent = published is not None and now - published <= timedelta(days=BACKFILL_DAYS)
        if recent:
            recent_total += sum(
                1
                for fields in CANDIDATE_LISTS.values()
                for field in fields
                for candidate in record.get(field) or []
                if isinstance(candidate, dict) and candidate.get("candidate_state") != "historical"
            )
        subject_entries = ((record.get("judging") or {}).get("subjects") or {}).items()
        for subject_id, entry in subject_entries:
            if not isinstance(entry, dict):
                continue
            if str(subject_id).startswith("probe:"):
                # [asked, scored, correct, unsupported]: a probe never enters the live statistics.
                truth = (probes or {}).get(str(subject_id).removeprefix("probe:"))
                for row in entry.get("judgments") or []:
                    if not isinstance(row, dict):
                        continue
                    cell = probe_results[str(row.get("judge_model"))]
                    cell[0] += 1
                    if row.get("quote_valid") is False:
                        cell[3] += 1
                    if not ledger.usable_vote(row):
                        continue
                    answer = _binary(row)
                    if truth is not None and answer is not None:
                        cell[1] += 1
                        cell[2] += answer == truth
                continue
            task = str(entry.get("task") or "")
            rows = [row for row in entry.get("judgments") or [] if isinstance(row, dict)]
            for row in rows:
                day = str(row.get("judged_at") or "")[:10]
                per_day[(day, row.get("judge_role"), task, row.get("context_tier"))] += 1
            for row in rows:
                role = str(row.get("judge_role"))
                if role in ("sibling", "adjudicator"):
                    cell = quotes[(role, str(row.get("judge_model")))]
                    cell[{True: 0, False: 1}.get(row.get("quote_valid"), 2)] += 1
                if role == "adjudicator":
                    adjudicated[(task, str(row.get("sample")), str(row.get("question_id")))] += 1
            routine = {
                (r.get("judge_model"), r.get("question_id"), r.get("context_tier")): _binary(r)
                for r in rows
                if r.get("sample") == "routine" and ledger.usable_vote(r)
            }
            for row in rows:
                if row.get("sample") != "stability" or not ledger.usable_vote(row):
                    continue
                base = str(row.get("question_id") or "").removesuffix("_s")
                first = routine.get((row.get("judge_model"), base, row.get("context_tier")))
                again = _binary(row)
                if first is not None and again is not None:
                    cell = stability[str(row.get("judge_model"))]
                    cell[0] += 1
                    cell[1] += first != again
            for row in rows:
                if row.get("judge_role") != "audit":
                    continue
                cell = audit_results[task]
                question = row.get("question_id")
                human = row.get("value")
                cell["answered"] += 1
                if not isinstance(human, bool):
                    cell["uncertain"] += 1
                    continue
                cell["supported" if human else "not_supported"] += 1
                for role in ("anchor", "adjudicator"):
                    own = [r for r in rows if r.get("question_id") == question]
                    reading, _model = final_reading(own, role)
                    vote = _binary({"value": reading})
                    if vote is not None:
                        cell[f"{role}_pairs"] += 1
                        cell[f"{role}_agree"] += vote == human
            by_tier: dict[tuple[str, str], dict[str, bool]] = defaultdict(dict)
            for row in rows:
                if row.get("kind") != "validate":
                    continue
                verdict = _binary(row)
                if verdict is not None and row.get("judge_role") in ("anchor", "sibling"):
                    key = (str(row.get("question_id")), str(row.get("context_tier")))
                    by_tier[key][str(row["judge_role"])] = verdict
            for (question, tier), votes in by_tier.items():
                if {"anchor", "sibling"} <= set(votes):
                    cell = agreement[(task, f"{question}@{tier}")]
                    cell[0] += votes["anchor"] == votes["sibling"]
                    cell[1] += 1
            first_tier_anchor += any(
                r.get("judge_role") == "anchor" and r.get("context_tier") == FIRST_TIER
                for r in rows
            )
            escalated += any(r.get("sample") == "escalation" for r in rows)
            if recent:
                roles = {r.get("judge_role") for r in rows if r.get("context_tier") == FIRST_TIER}
                recent_done += {"anchor", "sibling"} <= roles
    return {
        "judgments_per_day": [
            {"day": d, "role": r, "task": t, "tier": tier, "count": n}
            for (d, r, t, tier), n in sorted(per_day.items(), key=lambda kv: tuple(map(str, kv[0])))
        ],
        "agreement": {
            f"{task}:{cell}": {"agree": a, "pairs": n, "rate": round(a / n, 4) if n else None}
            for (task, cell), (a, n) in sorted(agreement.items())
        },
        # Chat answers by whether the quote in the reason is in the evidence (PR5).
        "quote_check": {
            f"{role}:{model}": {"valid": v, "invalid": bad, "unchecked": none}
            for (role, model), (v, bad, none) in sorted(quotes.items())
        },
        "adjudicated": {
            f"{task}:{sample}:{question}": n
            for (task, sample, question), n in sorted(adjudicated.items())
        },
        # The weekly blind audit: answers by task, and agreement with the anchor and adjudicator.
        "audit": {task: dict(sorted(cell.items())) for task, cell in sorted(audit_results.items())},
        # Probes: human-verified items asked invisibly. Non-gating until a task has about 20.
        "probes": {
            model: {
                "asked": asked,
                "scored": scored,
                "correct": correct,
                "unsupported": unsupported,
                "accuracy": round(correct / scored, 4) if scored else None,
            }
            for model, (asked, scored, correct, unsupported) in sorted(probe_results.items())
        },
        # Stability re-asks: pairs compared and how many answers flipped, per judge.
        "stability": {
            model: {"pairs": pairs, "flips": flips}
            for model, (pairs, flips) in sorted(stability.items())
        },
        "escalation_rate": round(escalated / first_tier_anchor, 4) if first_tier_anchor else None,
        "backfill_last_90_days": {
            "candidates": recent_total,
            "judged_by_both_at_first_tier": recent_done,
            "share": round(recent_done / recent_total, 4) if recent_total else None,
        },
    }


def _records(state_dir: Path) -> Iterable[Mapping[str, Any]]:
    from citypods.records import iter_records

    for path in sorted((state_dir / "sources").glob("*/episodes.json")):
        yield from iter_records(state_dir, path.parent.name)


def _gold_verdicts() -> dict[str, bool]:
    from citypods.judging.probes import load_probes
    from citypods.judging.tasks import registered, task

    return {probe.id: probe.verdict for name in registered() for probe in load_probes(task(name))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path("state"))
    args = parser.parse_args(argv)
    print(json.dumps(summarize(_records(args.state_dir), probes=_gold_verdicts()), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
