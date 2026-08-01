#!/usr/bin/env python
"""Merge independently reviewed agenda-gold slices without changing their labels."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _read_jsonl(path: Path):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            yield json.loads(line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--write", type=Path, required=True)
    args = parser.parse_args(argv)

    rows = [row for path in args.input for row in _read_jsonl(path)]
    gold_ids = [row["gold_id"] for row in rows]
    if len(set(gold_ids)) != len(gold_ids):
        raise RuntimeError("cannot merge agenda gold labels with duplicate gold IDs")
    args.write.parent.mkdir(parents=True, exist_ok=True)
    args.write.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
    )
    print(json.dumps({"inputs": len(args.input), "rows": len(rows)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
