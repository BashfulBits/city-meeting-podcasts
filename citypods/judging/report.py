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

CANDIDATE_LISTS = {
    "tag": ("tags", "llm_tag_candidates"),
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


def summarize(records: Iterable[Mapping[str, Any]], *, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    per_day: Counter = Counter()
    agreement: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    escalated = first_tier_anchor = 0
    recent_total = recent_done = 0
    for record in records:
        published = _published(record)
        recent = published is not None and now - published <= timedelta(days=BACKFILL_DAYS)
        if recent:
            recent_total += sum(
                len(record.get(field) or [])
                for fields in CANDIDATE_LISTS.values()
                for field in fields
            )
        subjects = ((record.get("judging") or {}).get("subjects") or {}).values()
        for entry in subjects:
            if not isinstance(entry, dict):
                continue
            task = str(entry.get("task") or "")
            rows = [row for row in entry.get("judgments") or [] if isinstance(row, dict)]
            for row in rows:
                day = str(row.get("judged_at") or "")[:10]
                per_day[(day, row.get("judge_role"), task, row.get("context_tier"))] += 1
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path("state"))
    args = parser.parse_args(argv)
    print(json.dumps(summarize(_records(args.state_dir)), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
