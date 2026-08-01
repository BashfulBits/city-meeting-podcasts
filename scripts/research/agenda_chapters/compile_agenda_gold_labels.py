#!/usr/bin/env python
"""Compile local GH#1078 human-review decisions into stable, scoreable gold rows.

The compiler preserves the reviewer's original decisions.  Its deliberately narrow procedural
classification prevents optional section headings from dominating the primary action-item metric;
it never relabels a substantive-looking kept item as optional.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

_PROCEDURAL_RE = re.compile(
    r"^(?:\d+[.\s]*)?(?:call to order|opening|pledge(?: of allegiance)?|"
    r"public (?:comment|communication)|consent (?:agenda|calendar)|regular agenda|"
    r"public hearings?|adjourn(?:ment)?|closing(?: remarks| items)?|"
    r"concluding items|announcements?)\.?$",
    re.I,
)


def _stable_id(meeting_id: str, item: dict) -> str:
    material = "\x1f".join(
        (
            meeting_id,
            str(item.get("line_start") or ""),
            str(item.get("line_end") or ""),
            " ".join(str(item["title"]).casefold().split()),
        )
    )
    return hashlib.sha256(material.encode()).hexdigest()[:20]


def _scope(title: str) -> str:
    """Return a secondary scope only for unmistakably procedural/section-only labels."""
    normalized = " ".join(title.split())
    return "optional_procedural" if _PROCEDURAL_RE.fullmatch(normalized) else "required"


def _source_backed(item: dict, agenda_text: str | None) -> bool:
    if not isinstance(agenda_text, str) or not agenda_text.strip():
        return False
    start, end = item.get("line_start"), item.get("line_end")
    return isinstance(start, int) and isinstance(end, int) and start >= 1 and end >= start


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-data", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--write", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument(
        "--exclude-meeting",
        action="append",
        default=[],
        help="adjudication ID to omit from a corrected/supplemented review; repeatable",
    )
    args = parser.parse_args(argv)

    review = json.loads(args.review_data.read_text())
    decisions = json.loads(args.decisions.read_text())
    rows: list[dict] = []
    excluded_meetings = set(args.exclude_meeting)
    for meeting in review["meetings"]:
        meeting_id = meeting["adjudication_id"]
        if meeting_id in excluded_meetings:
            continue
        for index, item in enumerate(meeting["proposed_items"]):
            decision_id = f"{meeting_id}:candidate:{index}"
            decision = decisions[decision_id]["decision"]
            feedback = decisions.get(f"{decision_id}:title-feedback", {}).get("note", "")
            scope = _scope(item["title"])
            source_backed = _source_backed(item, meeting.get("agenda_text"))
            if decision == "keep":
                label = "positive_required" if scope == "required" else "positive_optional"
            elif decision == "remove":
                label = "negative_excluded" if scope == "required" else "neutral_optional"
            elif decision == "unsure":
                label = "uncertain"
            else:
                raise RuntimeError(f"unexpected candidate decision {decision!r} for {decision_id}")
            rows.append(
                {
                    "gold_id": _stable_id(meeting_id, item),
                    "meeting_id": meeting_id,
                    "provider": meeting["provider"],
                    "coverage_bucket": meeting["coverage_bucket"],
                    "candidate_index": index,
                    "decision": decision,
                    "label": label,
                    "scope": scope,
                    "source_backed": source_backed,
                    "title": item["title"],
                    "line_start": item["line_start"],
                    "line_end": item["line_end"],
                    "evidence_quote": item["evidence_quote"],
                    "title_feedback": feedback,
                }
            )
        for _decision_id, value in decisions.items():
            if (
                value.get("decision") != "added"
                or value.get("item", {}).get("meeting") != meeting_id
            ):
                continue
            item = value["item"]
            source_backed = _source_backed(item, meeting.get("agenda_text"))
            rows.append(
                {
                    "gold_id": _stable_id(meeting_id, item),
                    "meeting_id": meeting_id,
                    "provider": meeting["provider"],
                    "coverage_bucket": meeting["coverage_bucket"],
                    "candidate_index": None,
                    "decision": "added",
                    "label": "positive_required" if source_backed else "source_unavailable",
                    "scope": _scope(item["title"]),
                    "source_backed": source_backed,
                    "title": item["title"],
                    "line_start": item.get("line_start"),
                    "line_end": item.get("line_end"),
                    "evidence_quote": None,
                    "title_feedback": value.get("note", ""),
                }
            )
    args.write.parent.mkdir(parents=True, exist_ok=True)
    args.write.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
    summary = {
        "rows": len(rows),
        "labels": dict(sorted(Counter(row["label"] for row in rows).items())),
        "scopes": dict(sorted(Counter(row["scope"] for row in rows).items())),
        "source_backed": sum(row["source_backed"] for row in rows),
        "source_unavailable": sum(not row["source_backed"] for row in rows),
        "feedback_rows": sum(bool(row["title_feedback"].strip()) for row in rows),
    }
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
