#!/usr/bin/env python
"""Build an origin-blind, high-recall agenda-item review packet for GH#1078.

This is a *development-set* labeling aid, not a production extractor.  It combines direct-model
items with the durable structural agenda candidates for meetings already materialized in the local
review data.  The output deliberately does not reveal which source proposed an item: the reviewer
labels the agenda itself, not a model.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

EpisodeKey = tuple[str, str, str]


def _normalize(value: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", value.casefold()).split())


def _episode_keys(key_path: Path) -> dict[str, EpisodeKey]:
    return {
        entry["adjudication_id"]: tuple(entry["episode"])
        for entry in json.loads(key_path.read_text())
    }


def _structural_candidates(
    candidate_path: Path, keys: dict[str, EpisodeKey]
) -> dict[str, list[dict]]:
    by_key = {key: adjudication_id for adjudication_id, key in keys.items()}
    found: dict[str, list[dict]] = defaultdict(list)
    with candidate_path.open(encoding="utf-8") as handle:
        for line in handle:
            candidate = json.loads(line)
            key = (candidate["provider"], candidate["slug"], candidate["uid"])
            adjudication_id = by_key.get(key)
            if adjudication_id is None:
                continue
            line_number = candidate["agenda_line_number"]
            found[adjudication_id].append(
                {
                    "title": candidate["agenda_candidate_title"],
                    "line_start": line_number,
                    "line_end": line_number,
                    "evidence_quote": candidate["agenda_candidate_title"],
                }
            )
    return found


def _same_item(left: dict, right: dict) -> bool:
    """Merge duplicate source evidence, not generated titles.

    Different models routinely make faithful but differently worded summaries of the same agenda
    item.  The source span/quote is the stable identity.  Overlap is intentionally enough here:
    a gold-review proposal must not make the reviewer decide the same cited source region twice.
    A reviewer can split a genuinely composite candidate by adding its distinct child action.
    """
    left_quote = _normalize(left["evidence_quote"])
    right_quote = _normalize(right["evidence_quote"])
    same_span = (left["line_start"], left["line_end"]) == (
        right["line_start"],
        right["line_end"],
    )
    same_quote = bool(left_quote and left_quote == right_quote)
    overlapping_span = (
        left["line_start"] <= right["line_end"]
        and right["line_start"] <= left["line_end"]
    )
    return same_span or same_quote or overlapping_span


def _union_items(meeting: dict, structural: list[dict]) -> list[dict]:
    proposed = [
        {
            "title": item["title"],
            "line_start": item["line_start"],
            "line_end": item["line_end"],
            "evidence_quote": item["evidence_quote"],
        }
        for title_set in meeting["generated_title_sets"]
        for item in title_set["items"]
    ]
    proposed.extend(structural)
    unique: list[dict] = []
    for item in proposed:
        if not item["title"].strip():
            continue
        if any(_same_item(item, existing) for existing in unique):
            continue
        unique.append(item)
    return sorted(unique, key=lambda item: (item["line_start"], item["line_end"], item["title"]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-data", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--agenda-candidates", type=Path, required=True)
    parser.add_argument("--write", type=Path, required=True)
    args = parser.parse_args(argv)

    keys = _episode_keys(args.key)
    structural = _structural_candidates(args.agenda_candidates, keys)
    raw = json.loads(args.review_data.read_text())
    meetings = []
    for meeting in raw["meetings"]:
        adjudication_id = meeting["adjudication_id"]
        items = _union_items(meeting, structural[adjudication_id])
        meetings.append(
            {
                "adjudication_id": adjudication_id,
                "provider": meeting["provider"],
                "coverage_bucket": meeting["coverage_bucket"],
                "official_agenda_url": meeting["official_agenda_url"],
                "agenda_text": meeting["agenda_text"],
                "retrieval_error": meeting["retrieval_error"],
                "proposed_items": items,
            }
        )
    args.write.parent.mkdir(parents=True, exist_ok=True)
    args.write.write_text(json.dumps({"meetings": meetings}, indent=2) + "\n")
    counts = [len(meeting["proposed_items"]) for meeting in meetings]
    print(
        json.dumps(
            {
                "meetings": len(meetings),
                "proposed_items": sum(counts),
                "min_per_meeting": min(counts, default=0),
                "max_per_meeting": max(counts, default=0),
                "mean_per_meeting": round(sum(counts) / len(counts), 2) if counts else 0,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
