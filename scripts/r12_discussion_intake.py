"""Convert one City requests Discussion event into a canonical add-city issue payload."""

from __future__ import annotations

import argparse
import base64
import json
import re
from pathlib import Path


def parse_fields(body: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    current = ""
    for line in body.splitlines():
        if line.startswith("### "):
            current = line[4:].strip().lower()
        elif current and line.strip() and not line.lstrip().startswith("<!--"):
            fields.setdefault(current, line.strip())
    return fields


def origin_marker(origin: dict) -> str:
    payload = json.dumps(origin, sort_keys=True, separators=(",", ":")).encode()
    return f"<!-- citypods:r12:origin {base64.urlsafe_b64encode(payload).decode()} -->"


def issue_payload(event: dict) -> dict:
    discussion = event["discussion"]
    fields = parse_fields(str(discussion.get("body") or ""))
    city_state = fields.get("city and state", "")
    if not re.fullmatch(r".+,\s*[A-Za-z]{2}", city_state):
        raise ValueError("Discussion must include a `### City and state` field in `City, ST` form")
    origin = {
        "source": "discussion",
        "discussion_node_id": discussion["node_id"],
        "discussion_number": discussion["number"],
        "discussion_url": discussion["html_url"],
    }
    body = str(discussion.get("body") or "").rstrip()
    body += (
        "\n\nSubmitted through GitHub Discussions; this issue is the canonical research and "
        "approval record.\n\n"
    )
    body += origin_marker(origin)
    return {
        "title": f"Add city: {city_state}",
        "body": body,
        "labels": ["add-city", "source:discussion", "needs:discovery"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    payload = issue_payload(json.loads(Path(args.event).read_text()))
    Path(args.out).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
