"""Fan an authoritative R12 lifecycle transition back to private/community origins."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import urllib.error
import urllib.request
from hashlib import sha256
from pathlib import Path

ORIGIN_RE = re.compile(r"<!-- citypods:r12:origin ([A-Za-z0-9_=-]+) -->")
MESSAGES = {
    "evidence_ready": "Research is ready for maintainer review.",
    "batched_for_review": "This request is now in a maintainer-review pull request.",
    "applied": "This request was merged and is being published.",
    "needs_more_information": "More information is needed before research can continue.",
    "research_only": "An unsupported-provider finding was recorded for future adapter work.",
    "evidence_expired": "The 90-day evidence window expired and fresh research is queued.",
}


def origin_marker(origin: dict) -> str:
    payload = json.dumps(origin, sort_keys=True, separators=(",", ":")).encode()
    return f"<!-- citypods:r12:origin {base64.urlsafe_b64encode(payload).decode()} -->"


def parse_origin(body: str) -> dict | None:
    matches = ORIGIN_RE.findall(body)
    if not matches:
        return None
    # The trusted intake marker is appended after requester-controlled Discussion text.
    try:
        value = json.loads(base64.urlsafe_b64decode(matches[-1]).decode())
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _json_request(url: str, payload: dict, headers: dict[str, str]) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read() or b"{}")


def notify(issue: dict, status: str, target_url: str = "") -> list[str]:
    if status not in MESSAGES:
        raise ValueError(f"unknown R12 lifecycle status: {status}")
    errors: list[str] = []
    issue_number = int(issue["number"])
    issue_url = str(issue["url"])
    worker_url = os.environ.get("CITY_REQUEST_STATUS_WEBHOOK_URL", "")
    if worker_url:
        try:
            _json_request(
                worker_url,
                {
                    "issue_number": issue_number,
                    "status": status,
                    "issue_url": issue_url,
                    "target_url": target_url,
                },
                {"user-agent": "citymeetings-r12/1.0"},
            )
        except (OSError, ValueError, urllib.error.HTTPError) as exc:
            errors.append(f"Worker callback failed: {exc}")

    origin = parse_origin(str(issue.get("body") or ""))
    token = os.environ.get("GH_TOKEN", "")
    if origin and origin.get("source") == "discussion" and token:
        marker = sha256(f"{status}|{issue_url}|{target_url}".encode()).hexdigest()
        link = f"\n\n{target_url}" if target_url and target_url != issue_url else ""
        body = (
            f"{MESSAGES[status]}\n\nCanonical issue: {issue_url}{link}\n\n"
            f"<!-- citypods:r12:status {marker} -->"
        )
        try:
            cursor: str | None = None
            while True:
                existing = _json_request(
                    "https://api.github.com/graphql",
                    {
                        "query": (
                            "query($discussionId:ID!,$cursor:String){node(id:$discussionId){"
                            "... on Discussion{comments(last:100,before:$cursor){nodes{body}"
                            "pageInfo{hasPreviousPage startCursor}}}}}"
                        ),
                        "variables": {
                            "discussionId": origin["discussion_node_id"],
                            "cursor": cursor,
                        },
                    },
                    {"authorization": f"Bearer {token}", "user-agent": "citymeetings-r12/1.0"},
                )
                if existing.get("errors"):
                    errors.append("Discussion callback returned GraphQL errors")
                    return errors
                comments_data = ((existing.get("data") or {}).get("node") or {}).get(
                    "comments"
                ) or {}
                if any(
                    marker in str(comment.get("body") or "")
                    for comment in comments_data.get("nodes", [])
                ):
                    return errors
                page = comments_data.get("pageInfo") or {}
                if not page.get("hasPreviousPage"):
                    break
                cursor = page.get("startCursor")
            result = _json_request(
                "https://api.github.com/graphql",
                {
                    "query": (
                        "mutation($discussionId:ID!,$body:String!){"
                        "addDiscussionComment(input:{discussionId:$discussionId,body:$body})"
                        "{comment{id}}}"
                    ),
                    "variables": {"discussionId": origin["discussion_node_id"], "body": body},
                },
                {"authorization": f"Bearer {token}", "user-agent": "citymeetings-r12/1.0"},
            )
            if result.get("errors"):
                errors.append("Discussion callback returned GraphQL errors")
        except (OSError, ValueError, KeyError, urllib.error.HTTPError) as exc:
            errors.append(f"Discussion callback failed: {exc}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue", required=True)
    parser.add_argument("--status", required=True, choices=sorted(MESSAGES))
    parser.add_argument("--target-url", default="")
    args = parser.parse_args(argv)
    issue = json.loads(Path(args.issue).read_text())
    for error in notify(issue, args.status, args.target_url):
        print(f"::warning::{error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
