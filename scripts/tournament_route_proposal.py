#!/usr/bin/env python3
"""Parse a trusted champion ticket and prepare its narrowly-scoped route config change."""

from __future__ import annotations

import argparse
import base64
import json
import re
from pathlib import Path

MARKER = re.compile(r"<!-- citypods:tournament-ticket ([A-Za-z0-9_=-]+) -->")


def parse_ticket(body: str) -> dict:
    matches = MARKER.findall(body)
    if not matches:
        raise ValueError("missing tournament ticket marker")
    meta = json.loads(base64.urlsafe_b64decode(matches[-1]).decode())
    if meta.get("version") != 1 or meta.get("task") != "tag":
        raise ValueError("unsupported tournament ticket")
    checked = re.findall(r"^- \[x\] (.+)$", body.replace("\r\n", "\n"), flags=re.I | re.M)
    if len(checked) != 1:
        raise ValueError("select exactly one tournament routing decision")
    choice = checked[0]
    if choice.lower() == "keep current route":
        return {"action": "keep", "meta": meta}
    match = re.fullmatch(
        r"Switch to `([^`]+)` \((normal gradual refresh|retained-catalog backfill)\)", choice
    )
    if not match or match.group(1) not in meta.get("challengers", []):
        raise ValueError("ticket decision is not an eligible challenger")
    return {
        "action": "switch",
        "model": match.group(1),
        "backfill": match.group(2) == "retained-catalog backfill",
        "meta": meta,
    }


def update_tag_route(path: Path, model: str) -> None:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"(?ms)^(tagging:\n.*?^  llm_model:)\s*\"[^\"]+\"", text)
    if not match:
        raise ValueError("could not find tagging.llm_model")
    updated = text[: match.start(1)] + match.group(1) + f' "{model}"' + text[match.end() :]
    path.write_text(updated, encoding="utf-8")


def cleared(body: str) -> str:
    return re.sub(r"(?m)^- \[x\] ", "- [ ] ", body)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--body-file", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--site-config", type=Path)
    parser.add_argument("--cleared-body", type=Path)
    args = parser.parse_args(argv)
    body = args.body_file.read_text(encoding="utf-8")
    result = parse_ticket(body)
    if args.site_config and result["action"] == "switch":
        update_tag_route(args.site_config, result["model"])
    if args.cleared_body:
        args.cleared_body.write_text(cleared(body), encoding="utf-8")
    args.result.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
