#!/usr/bin/env python
"""Merge blinded agenda-review key slices while rejecting duplicate meeting identities."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--write", type=Path, required=True)
    args = parser.parse_args(argv)
    rows = [row for path in args.input for row in json.loads(path.read_text())]
    identities = [tuple(row["episode"]) for row in rows]
    if len(set(identities)) != len(identities):
        raise RuntimeError("cannot merge adjudication keys with duplicate provider/slug/UID rows")
    ids = [row["adjudication_id"] for row in rows]
    if len(set(ids)) != len(ids):
        raise RuntimeError("cannot merge adjudication keys with duplicate IDs")
    args.write.parent.mkdir(parents=True, exist_ok=True)
    args.write.write_text(json.dumps(rows, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"inputs": len(args.input), "meetings": len(rows)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
