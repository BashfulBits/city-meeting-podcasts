#!/usr/bin/env python
"""Build source-span-only soft hints for the GH#1078 hybrid LLM benchmark.

The full agenda remains the authority.  High hints are the union of the two strongest compact
weak-label classifiers; every other structural candidate is retained as a low-priority recall
cue.  Titles and classifier scores deliberately do not reach the LLM, avoiding a second source
of title bias.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path


def _read_jsonl(path: Path):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            yield json.loads(line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--agenda-candidates", type=Path, required=True)
    parser.add_argument("--high-suggestions", type=Path, action="append", required=True)
    parser.add_argument("--write", type=Path, required=True)
    args = parser.parse_args(argv)

    adjudication_to_uid = {
        entry["adjudication_id"]: entry["episode"][2]
        for entry in json.loads(args.key.read_text())
    }
    high_spans: dict[str, set[tuple[int, int]]] = defaultdict(set)
    for path in args.high_suggestions:
        payload = json.loads(path.read_text())
        for adjudication_id, rows in payload["suggestions"].items():
            uid = adjudication_to_uid.get(adjudication_id)
            if uid:
                high_spans[uid].update((row["line_start"], row["line_end"]) for row in rows)

    candidate_spans: dict[str, set[tuple[int, int]]] = defaultdict(set)
    selected_uids = set(adjudication_to_uid.values())
    for row in _read_jsonl(args.agenda_candidates):
        if row["uid"] in selected_uids:
            candidate_spans[row["uid"]].add((row["agenda_line_number"], row["agenda_line_number"]))

    hints = {}
    for uid in sorted(selected_uids):
        high = high_spans[uid]
        low = candidate_spans[uid] - high
        hints[uid] = [
            {"line_start": start, "line_end": end, "priority": "high"}
            for start, end in sorted(high)
        ] + [
            {"line_start": start, "line_end": end, "priority": "low"}
            for start, end in sorted(low)
        ]
    args.write.parent.mkdir(parents=True, exist_ok=True)
    args.write.write_text(
        json.dumps({"version": 1, "hints": hints}, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps({"episodes": len(hints), "hints": sum(map(len, hints.values()))}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
