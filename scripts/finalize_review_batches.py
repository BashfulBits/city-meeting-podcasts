#!/usr/bin/env python3
"""Close only managed parents whose native review children all resolved."""

from __future__ import annotations

import json
import subprocess

from citypods.review_issues import MANAGED_LABEL, decode_envelope


def _gh(*args: str) -> str:
    run = subprocess.run(["gh", *args], text=True, capture_output=True, check=False)
    if run.returncode:
        raise RuntimeError(run.stderr.strip() or "gh failed")
    return run.stdout.strip()


def main() -> int:
    rows = json.loads(
        _gh(
            "issue",
            "list",
            "--state",
            "open",
            "--label",
            MANAGED_LABEL,
            "--limit",
            "500",
            "--json",
            "number,body",
        )
        or "[]"
    )
    for row in rows:
        try:
            envelope = decode_envelope(str(row.get("body") or ""))
        except ValueError:
            continue
        if envelope.get("surface") != "parent":
            continue
        children = json.loads(
            _gh(
                "issue",
                "view",
                str(row["number"]),
                "--json",
                "subIssues",
                "--jq",
                "[.subIssues.nodes[]]",
            )
            or "[]"
        )
        if children and not any(child.get("state") == "OPEN" for child in children):
            _gh(
                "issue",
                "close",
                str(row["number"]),
                "--comment",
                "All managed review children are resolved — closing this batch.",
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
