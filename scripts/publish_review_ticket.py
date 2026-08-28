#!/usr/bin/env python3
"""Upsert a standard rolling review ticket through ``gh``."""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

from citypods.review_issues import MANAGED_LABEL, append_envelope, bounded_body


def _gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "gh failed")
    return result.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--family", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--body-file", required=True, type=Path)
    args = parser.parse_args(argv)
    _gh(
        "label",
        "create",
        MANAGED_LABEL,
        "--color",
        "5319e7",
        "--force",
        "--description",
        "Bot-managed weekly review issue",
    )
    _gh(
        "label",
        "create",
        args.label,
        "--color",
        "5319e7",
        "--force",
        "--description",
        f"Bot-managed {args.family} review ticket",
    )
    body = append_envelope(
        args.body_file.read_text(encoding="utf-8"), family=args.family, surface="ticket"
    )
    body, _ = bounded_body(body)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md") as temp:
        temp.write(body)
        temp.flush()
        rows = _gh(
            "issue",
            "list",
            "--state",
            "open",
            "--label",
            args.label,
            "--limit",
            "500",
            "--json",
            "number,title",
        )
        existing = next(
            (
                str(row["number"])
                for row in json.loads(rows or "[]")
                if row.get("title") == args.title
            ),
            "",
        )
        if existing:
            _gh("issue", "edit", existing, "--body-file", temp.name, "--add-label", MANAGED_LABEL)
            print(f"updated ticket #{existing}")
        else:
            print(
                _gh(
                    "issue",
                    "create",
                    "--title",
                    args.title,
                    "--label",
                    args.label,
                    "--label",
                    MANAGED_LABEL,
                    "--body-file",
                    temp.name,
                )
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
