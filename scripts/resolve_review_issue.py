#!/usr/bin/env python3
"""Run the durable, feature-owned ingest command selected by a shared issue envelope."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from citypods.review_issues import decode_envelope

COMMANDS = {
    "h15": ("transcript-quality", "ingest-review"),
    "r5": ("llm-evaluation", "ingest"),
    "r6": ("r6-review", "ingest"),
    "r7": ("speaker-review", "ingest"),
    "h16": ("availability-review", "ingest"),
}


def _result(text: str) -> dict:
    match = re.search(r"(\{[\s\S]*\})\s*$", text)
    if not match:
        return {"stored": False, "reason": "no_structured_result"}
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {"stored": False, "reason": "invalid_structured_result"}
    return value if isinstance(value, dict) else {"stored": False, "reason": "invalid_result"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue-number", required=True, type=int)
    parser.add_argument("--issue-url", default="")
    parser.add_argument("--actor", required=True)
    parser.add_argument("--body-file", required=True, type=Path)
    parser.add_argument("--family", choices=sorted(COMMANDS))
    args = parser.parse_args(argv)
    body = args.body_file.read_text(encoding="utf-8")
    envelope = decode_envelope(body)
    if envelope.get("surface") != "child":
        print(json.dumps({"stored": False, "reason": "not_actionable_child"}, sort_keys=True))
        return 0
    family = args.family or str(envelope["family"])
    if family not in COMMANDS:
        print(json.dumps({"stored": False, "reason": "unsupported_family"}, sort_keys=True))
        return 0
    command = COMMANDS[family]
    invocation = [
        sys.executable,
        "-m",
        "citypods.cli",
        *command,
        "--issue-number",
        str(args.issue_number),
        "--issue-body-file",
        str(args.body_file),
        "--actor",
        args.actor,
    ]
    if family in {"h15", "r5", "h16"}:
        invocation.extend(("--issue-url", args.issue_url))
    completed = subprocess.run(invocation, text=True, capture_output=True, check=False)
    if completed.stdout:
        print(completed.stdout, end="")
        if not completed.stdout.endswith("\n"):
            print()
    if completed.returncode:
        combined = completed.stdout + completed.stderr
        if "select exactly one" in combined or "exactly one primary" in combined:
            print(json.dumps({"stored": False, "reason": "no_decision"}, sort_keys=True))
            return 0
        if completed.stderr:
            print(completed.stderr, file=sys.stderr, end="")
        return completed.returncode
    print(json.dumps(_result(completed.stdout), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
