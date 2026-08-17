#!/usr/bin/env python3
"""Handle the `/remedy` issue-comment command.

`remedy-unexpected-bodies.yml` dispatches automatically the moment `audit.yml` *creates* a new
consolidated `unexpected-body` issue (see `on_issue_created` in `scripts/audit_feeds.py`) -- but
deliberately not again while that issue stays open and later runs add or change rows on it, so a
finding added after the first automated pass has nothing kicking off remediation for it. `/remedy`,
posted as a comment on that issue by anyone with repository write access, re-dispatches it.

Usage (see .github/workflows/remedy-commands.yml):
    python scripts/remedy_commands.py --event "$GITHUB_EVENT_PATH" \\
        --permission actor-permission.json --out remedy-command.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from citypods.github_permissions import RepositoryPermissionError, require_repository_write

# Must match citypods/audit.py's `_key_marker(_issue_key("", "unexpected-body"))` exactly -- this
# is how the command confirms the commented-on issue really is the audit's own consolidated
# unexpected-body issue, not just something a commenter typed `/remedy` on.
UNEXPECTED_BODY_MARKER = "<!-- citypods:feed-health:key=unexpected-body -->"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", required=True, help="Path to the issue_comment webhook payload")
    parser.add_argument(
        "--permission", required=True, help="Path to the actor's collaborator-permission JSON"
    )
    parser.add_argument("--out", required=True, help="Where to write the command result JSON")
    return parser.parse_args(argv)


def process_event(event: dict[str, Any], permission: dict[str, Any]) -> dict[str, Any]:
    """Return ``{"accepted": bool, "issue_number": int | None, "comment": str}``.

    Kept separate from ``main`` so it's directly unit-testable without touching the filesystem.
    """
    issue = event.get("issue") or {}
    issue_number = issue.get("number")

    try:
        require_repository_write(permission)
    except RepositoryPermissionError as exc:
        return {"accepted": False, "issue_number": issue_number, "comment": f"❌ {exc}."}

    if UNEXPECTED_BODY_MARKER not in (issue.get("body") or ""):
        return {
            "accepted": False,
            "issue_number": issue_number,
            "comment": (
                "❌ `/remedy` only runs on the feed-health audit's own consolidated "
                "`unexpected-body` issue."
            ),
        }

    return {
        "accepted": True,
        "issue_number": issue_number,
        "comment": (
            f"🔄 Re-running automated remedy for issue #{issue_number}. I'll classify this "
            "issue's current findings and post the result here, with a link to the PR if one "
            "is opened."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    event = json.loads(Path(args.event).read_text(encoding="utf-8"))
    permission = json.loads(Path(args.permission).read_text(encoding="utf-8"))

    result = process_event(event, permission)
    Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if result["accepted"] else 1


if __name__ == "__main__":
    sys.exit(main())
