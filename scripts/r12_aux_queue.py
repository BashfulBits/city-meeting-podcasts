"""Filter weekly auxiliary candidates using recorded R12 dispositions."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

MARKER = "citypods:r12:command"
BOT_LOGIN = "github-actions[bot]"


def _commands(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prefix = f"<!-- {MARKER} "
    found: list[dict[str, Any]] = []
    for comment in comments:
        if (comment.get("user") or {}).get("login") != BOT_LOGIN:
            continue
        for line in str(comment.get("body") or "").splitlines():
            if not line.startswith(prefix) or not line.endswith(" -->"):
                continue
            try:
                record = json.loads(line[len(prefix) : -4])
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and isinstance(record.get("city_slug"), str):
                found.append(record)
    return found


def queue(cities: list[str], comments: list[dict[str, Any]]) -> list[str]:
    """Suppress explicitly deferred or provider-assigned cities without erasing evidence."""
    latest = {record["city_slug"]: record for record in _commands(comments)}
    today = datetime.now(UTC).date()
    queued: list[str] = []
    for city in cities:
        record = latest.get(city)
        action = record.get("action") if record else None
        if action in {"assign-provider", "create-provider"}:
            continue
        if action == "defer-agenda":
            try:
                if date.fromisoformat(str(record["until"])) > today:
                    continue
            except (KeyError, ValueError):
                pass
        queued.append(city)
    return queued


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cities", required=True, help="whitespace-separated city slugs")
    parser.add_argument("--issue", required=True)
    parser.add_argument("--comments", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    del args.issue  # The issue body has no private state; disposition records are comments.
    comments = json.loads(Path(args.comments).read_text())
    Path(args.out).write_text(json.dumps({"cities": queue(args.cities.split(), comments)}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
