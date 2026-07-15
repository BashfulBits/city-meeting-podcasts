"""Decide whether a public R12 request needs fresh discovery evidence."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from citypods.discovery.render import iter_evidence_markers

BOT_LOGIN = "github-actions[bot]"


def _created_at(evidence: dict[str, Any]) -> datetime | None:
    try:
        value = str(evidence["evidence_created_at"]).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(value)
    except (KeyError, TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def state(issue: dict[str, Any], comments: list[dict[str, Any]]) -> dict[str, bool]:
    """Keep research-only results quiet until 90-day expiry or an explicit recheck."""
    labels = {item.get("name") for item in issue.get("labels", []) if isinstance(item, dict)}
    if "r12:recheck" in labels:
        return {"discover": True, "expired": False}
    artifacts: list[dict[str, Any]] = []
    for comment in comments:
        if (comment.get("user") or {}).get("login") == BOT_LOGIN:
            artifacts.extend(iter_evidence_markers(str(comment.get("body") or "")))
    if not artifacts:
        return {"discover": True, "expired": False}
    minimum = datetime.min.replace(tzinfo=UTC)
    latest = max(artifacts, key=lambda item: _created_at(item) or minimum)
    created = _created_at(latest)
    if created is None or datetime.now(UTC) - created > timedelta(days=90):
        return {"discover": True, "expired": True}
    return {"discover": False, "expired": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue", required=True)
    parser.add_argument("--comments", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    issue = json.loads(Path(args.issue).read_text())
    comments = json.loads(Path(args.comments).read_text())
    result = state(issue, comments)
    Path(args.out).write_text(json.dumps(result) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
