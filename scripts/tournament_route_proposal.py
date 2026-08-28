#!/usr/bin/env python3
"""Parse a trusted champion ticket and prepare its narrowly-scoped route config change."""

from __future__ import annotations

import argparse
import base64
import json
import re
from pathlib import Path

from citypods.review_issues import checked_decisions, clear_decision_block

MARKER = re.compile(r"<!-- citypods:tournament-ticket ([A-Za-z0-9_=-]+) -->")
ROUTE_MARKER = re.compile(
    r"<!-- citypods:tournament-route v=(?P<version>\d+) issue=(?P<issue>\d+) "
    r"model_b64=(?P<model>[A-Za-z0-9_=-]+) backfill=(?P<backfill>true|false) "
    r"ticket_b64=(?P<ticket>[A-Za-z0-9_=-]+) -->"
)


def parse_ticket(body: str) -> dict:
    matches = MARKER.findall(body)
    if not matches:
        raise ValueError("missing tournament ticket marker")
    meta = json.loads(base64.urlsafe_b64decode(matches[-1]).decode())
    if meta.get("version") != 1 or meta.get("task") != "tag":
        raise ValueError("unsupported tournament ticket")
    choices = ["Keep current route"]
    for challenger in meta.get("challengers", []):
        choices.append(f"Switch to `{challenger}` (normal gradual refresh)")
        choices.append(f"Switch to `{challenger}` (retained-catalog backfill)")
    checked = checked_decisions(body, tuple(choices))
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


def parse_route_proposal(body: str) -> dict:
    """Validate the immutable ticket metadata carried by a bot-authored route proposal PR."""
    matches = list(ROUTE_MARKER.finditer(body))
    if not matches:
        raise ValueError("missing tournament route proposal marker")
    match = matches[-1]
    if match.group("version") != "1":
        raise ValueError("unsupported tournament route proposal marker")
    try:
        model = base64.urlsafe_b64decode(match.group("model")).decode("utf-8")
        ticket = json.loads(base64.urlsafe_b64decode(match.group("ticket")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid tournament route proposal metadata") from exc
    if ticket.get("version") != 1 or ticket.get("task") != "tag":
        raise ValueError("unsupported tournament route proposal task")
    if model not in ticket.get("challengers", []):
        raise ValueError("route proposal model was not an eligible ticket challenger")
    return {
        "issue_number": int(match.group("issue")),
        "model": model,
        "backfill": match.group("backfill") == "true",
        "ticket": ticket,
    }


def update_tag_route(path: Path, model: str) -> None:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"(?ms)^(tagging:\n.*?^  llm_model:)\s*\"[^\"]+\"", text)
    if not match:
        raise ValueError("could not find tagging.llm_model")
    updated = text[: match.start(1)] + match.group(1) + f' "{model}"' + text[match.end() :]
    path.write_text(updated, encoding="utf-8")


def cleared(body: str) -> str:
    return clear_decision_block(body)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--body-file", type=Path)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--site-config", type=Path)
    parser.add_argument("--cleared-body", type=Path)
    parser.add_argument("--route-pr-body", type=Path)
    args = parser.parse_args(argv)
    if args.route_pr_body and args.cleared_body:
        parser.error("--cleared-body cannot be used with --route-pr-body")
    if args.route_pr_body:
        result = parse_route_proposal(args.route_pr_body.read_text(encoding="utf-8"))
    else:
        if not args.body_file:
            parser.error("--body-file is required unless --route-pr-body is used")
        body = args.body_file.read_text(encoding="utf-8")
        result = parse_ticket(body)
    if args.site_config and result.get("action") == "switch":
        update_tag_route(args.site_config, result["model"])
    if args.cleared_body:
        args.cleared_body.write_text(cleared(body), encoding="utf-8")
    args.result.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
