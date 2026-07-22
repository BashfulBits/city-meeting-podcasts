"""Parse one maintainer-only R12 command into safe GitHub mutations.

The workflow supplies already-fetched public issue/comment JSON.  This module deliberately does
not call GitHub itself, which keeps command semantics unit-testable and makes the workflow the
single place that holds a repository token.
"""

from __future__ import annotations

import argparse
import json
import shlex
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from citypods.discovery.render import evidence_digest, iter_evidence_markers
from citypods.github_permissions import require_repository_write

MARKER = "citypods:r12:command"
BOT_LOGIN = "github-actions[bot]"


class CommandError(ValueError):
    """A command is malformed, inapplicable, or not attached to reviewable evidence."""


def _command_marker(payload: dict[str, Any]) -> str:
    return f"<!-- {MARKER} {json.dumps(payload, sort_keys=True, separators=(',', ':'))} -->"


def _evidence(issue: dict[str, Any], comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if (issue.get("user") or {}).get("login") == BOT_LOGIN:
        found.extend(iter_evidence_markers(str(issue.get("body") or "")))
    for comment in comments:
        if (comment.get("user") or {}).get("login") == BOT_LOGIN:
            found.extend(iter_evidence_markers(str(comment.get("body") or "")))
    return found


def _find_evidence(
    issue: dict[str, Any], comments: list[dict[str, Any]], city_slug: str | None
) -> dict[str, Any]:
    candidates = _evidence(issue, comments)
    if city_slug:
        candidates = [
            item for item in candidates if (item.get("request") or {}).get("city_slug") == city_slug
        ]
    if not candidates:
        raise CommandError("no matching R12 evidence artifact was found")
    return candidates[-1]


def _proposal_evidence(
    issue: dict[str, Any], comments: list[dict[str, Any]], city_slug: str | None
) -> dict[str, Any]:
    evidence = _find_evidence(issue, comments, city_slug)
    verification = evidence.get("verification") or {}
    if not evidence.get("proposed_yaml") or not all(
        (
            verification.get("signature_verified"),
            verification.get("provider_verified"),
            verification.get("sample_media_url"),
        )
    ):
        raise CommandError("approval is available only for fully verified proposal evidence")
    return evidence


def parse_command(
    issue: dict[str, Any], comments: list[dict[str, Any]], command_text: str
) -> dict[str, Any]:
    """Return a declarative command outcome for an authorized maintainer comment."""
    try:
        parts = shlex.split(command_text.strip())
    except ValueError as exc:
        raise CommandError("invalid command quoting") from exc
    if len(parts) < 2 or parts[0] != "/r12":
        raise CommandError("not an R12 command")
    action = parts[1]
    now = datetime.now(UTC).isoformat()
    is_auxiliary = str(issue.get("title") or "").startswith("[city-discovery]")

    if action == "approve":
        city_slug = parts[2] if len(parts) == 3 else None
        if is_auxiliary and not city_slug:
            raise CommandError("auxiliary approvals require `/r12 approve <city-slug>`")
        if not is_auxiliary and city_slug:
            raise CommandError("new-city approvals use bare `/r12 approve`")
        evidence = _proposal_evidence(issue, comments, city_slug)
        request = evidence["request"]
        payload = {
            "action": "approve",
            "city_slug": request["city_slug"],
            "evidence_digest": evidence_digest(evidence),
            "approved_at": now,
        }
        return {
            "add_labels": ["r12:approved"],
            "comment": "R12 proposal approved for the next maintainer-review batch.\n\n"
            + _command_marker(payload),
        }

    if action == "reject":
        if is_auxiliary and len(parts) < 3:
            raise CommandError("auxiliary rejection requires `/r12 reject <city-slug> reason=...`")
        city_slug = parts[2] if is_auxiliary else None
        evidence = _find_evidence(issue, comments, city_slug)
        payload = {
            "action": "reject",
            "city_slug": evidence["request"]["city_slug"],
            "evidence_digest": evidence_digest(evidence),
            "rejected_at": now,
            "reason": " ".join(parts[3:] if is_auxiliary else parts[2:]) or "not specified",
        }
        return {
            "add_labels": ["r12:rejected"],
            "remove_labels": ["r12:approved"],
            "comment": "R12 proposal disposition recorded.\n\n" + _command_marker(payload),
        }

    if action in {"assign-provider", "create-provider"}:
        if len(parts) < 3:
            raise CommandError(f"`/r12 {action}` requires a provider key")
        provider_key = parts[2].lower()
        if not provider_key.replace("-", "").isalnum():
            raise CommandError("provider key may contain only letters, digits, and hyphens")
        if is_auxiliary and len(parts) < 4:
            raise CommandError(
                f"auxiliary `{action}` requires `/r12 {action} <provider-key> <city-slug>`"
            )
        city_slug = parts[3] if is_auxiliary else None
        name = next(
            (part[5:] for part in parts[4 if is_auxiliary else 3 :] if part.startswith("name=")),
            None,
        )
        evidence = _find_evidence(issue, comments, city_slug)
        payload = {
            "action": action,
            "city_slug": evidence["request"]["city_slug"],
            "evidence_digest": evidence_digest(evidence),
            "provider_key": provider_key,
            "name": name,
            "recorded_at": now,
        }
        return {
            "add_labels": ["needs:provider"],
            "comment": "R12 provider backlog disposition recorded for the next batch PR.\n\n"
            + _command_marker(payload),
        }

    if action == "defer-agenda":
        if not is_auxiliary:
            raise CommandError("agenda deferral is available only on the auxiliary digest")
        if len(parts) < 3:
            raise CommandError(
                "agenda deferral requires `/r12 defer-agenda <city-slug> "
                "until=YYYY-MM-DD reason=...`"
            )
        city_slug = parts[2]
        until = next((part[6:] for part in parts[3:] if part.startswith("until=")), None)
        if not until:
            raise CommandError('use `/r12 defer-agenda until=YYYY-MM-DD reason="..."`')
        evidence = _find_evidence(issue, comments, city_slug)
        payload = {
            "action": action,
            "city_slug": evidence["request"]["city_slug"],
            "evidence_digest": evidence_digest(evidence),
            "until": until,
            "reason": " ".join(part[7:] for part in parts[3:] if part.startswith("reason=")),
            "recorded_at": now,
        }
        return {"comment": "R12 agenda deferral recorded.\n\n" + _command_marker(payload)}

    if action == "recheck":
        return {
            "add_labels": ["r12:recheck", "needs:discovery"],
            "remove_labels": ["r12:approved", "r12:expired", "needs:more-information"],
            "comment": "R12 `recheck` queued for the next daily discovery run.",
        }
    if action == "clear-disposition":
        return {
            "remove_labels": ["r12:approved", "r12:rejected", "needs:provider"],
            "comment": "R12 disposition cleared; existing evidence remains visible.",
        }
    if action == "batch":
        return {"dispatch_batch": True, "comment": "R12 batch dispatch acknowledged."}
    raise CommandError("unknown R12 command")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue", required=True)
    parser.add_argument("--comments", required=True)
    parser.add_argument("--command", required=True)
    parser.add_argument("--permission", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    issue = json.loads(Path(args.issue).read_text())
    comments = json.loads(Path(args.comments).read_text())
    permission = json.loads(Path(args.permission).read_text())
    if not isinstance(permission, dict):
        raise CommandError("the permission payload must be a JSON object")
    require_repository_write(permission)
    result = parse_command(issue, comments, args.command)
    Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
