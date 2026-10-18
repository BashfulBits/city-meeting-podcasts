#!/usr/bin/env python3
"""File the tickets a judge-audit batch asked for, once each (review/53 PR6)."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

LABEL = "signal:judge-audit"


def _gh(*args: str) -> str:
    run = subprocess.run(["gh", *args], text=True, capture_output=True, check=False)
    if run.returncode:
        raise RuntimeError(run.stderr.strip() or "gh failed")
    return run.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args(argv)
    tickets = json.loads(args.manifest.read_text(encoding="utf-8")).get("tickets") or []
    if not tickets:
        print(json.dumps({"filed": 0, "skipped": 0}))
        return 0
    _gh("label", "create", LABEL, "--color", "5319e7", "--force", "--description", "Judge audit")
    open_titles = {
        row["title"]
        for row in json.loads(
            _gh(
                "issue",
                "list",
                "--state",
                "open",
                "--label",
                LABEL,
                "--limit",
                "200",
                "--json",
                "title",
            )  # fmt: skip
            or "[]"
        )
    }
    filed = skipped = 0
    for ticket in tickets:
        if ticket["title"] in open_titles:
            skipped += 1  # the same ticket is already open; a person has not looked yet
            continue
        _gh(
            "issue",
            "create",
            "--title",
            ticket["title"],
            "--body",
            ticket["body"],
            "--label",
            LABEL,
        )
        filed += 1
    print(json.dumps({"filed": filed, "skipped": skipped}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
