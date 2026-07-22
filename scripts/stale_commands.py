#!/usr/bin/env python3
"""Validate a maintainer ``/stale`` command and prepare its feed-lifecycle YAML edit.

The GitHub workflow owns git and pull-request mutations. This module treats the event payload as
untrusted data, resolves the target only from a generated stale-incident marker, updates exactly one
feed YAML, validates the full catalog, and emits a declarative PR plan for the workflow to apply.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml
from yaml.nodes import MappingNode, ScalarNode

from citypods.config import load_city_configs, load_site_config
from citypods.github_permissions import require_repository_write
from citypods.security import SecurityError, validate_source_url

INCIDENT_RE = re.compile(
    r"<!-- citypods:stale-incident:v1 check=(stale|dormant-resumed) "
    r"slug=([a-z0-9]+(?:-[a-z0-9]+)*) incident=(\S+) parent=(\d+) -->"
)
ALLOWED_ACTIONS = frozenset({"activate", "pause", "dormant", "retire"})
MAX_REASON_CHARS = 500
MAX_EVIDENCE_CHARS = 2048


class CommandError(ValueError):
    """The event or command is unauthorized, malformed, or not a generated stale incident."""


@dataclass(frozen=True)
class StaleCommand:
    status: str
    reason: str
    recheck_after: date | None = None
    evidence_url: str | None = None


def _clean_text(value: str, *, name: str, limit: int) -> str:
    value = value.strip()
    if not value:
        raise CommandError(f"--{name} requires a non-empty value")
    if len(value) > limit:
        raise CommandError(f"--{name} must be at most {limit} characters")
    if any(char in value for char in "\x00\r\n"):
        raise CommandError(f"--{name} may not contain control characters or newlines")
    return value


def parse_command(command_text: str, *, today: date | None = None) -> StaleCommand:
    """Parse the strict command grammar without passing comment text through a shell."""
    try:
        parts = shlex.split(command_text.strip())
    except ValueError as exc:
        raise CommandError("invalid command quoting") from exc
    if len(parts) < 2 or parts[0] != "/stale" or parts[1] not in ALLOWED_ACTIONS:
        raise CommandError("expected `/stale activate|pause|dormant|retire`")
    action = parts[1]
    values: dict[str, str] = {}
    index = 2
    while index < len(parts):
        flag = parts[index]
        if flag not in {"--until", "--reason", "--evidence"}:
            raise CommandError(f"unknown argument: {flag}")
        if flag in values:
            raise CommandError(f"duplicate argument: {flag}")
        if index + 1 >= len(parts) or parts[index + 1].startswith("--"):
            raise CommandError(f"{flag} requires a value")
        values[flag] = parts[index + 1]
        index += 2

    allowed = (
        {"--reason", "--evidence", "--until"}
        if action == "pause"
        else {
            "--reason",
            "--evidence",
        }
    )
    extra = set(values) - allowed
    if extra:
        raise CommandError(f"{action} does not accept {sorted(extra)[0]}")
    reason = _clean_text(values.get("--reason", ""), name="reason", limit=MAX_REASON_CHARS)

    recheck_after: date | None = None
    if action == "pause":
        raw_until = values.get("--until")
        if not raw_until:
            raise CommandError("pause requires --until YYYY-MM-DD")
        try:
            recheck_after = date.fromisoformat(raw_until)
        except ValueError as exc:
            raise CommandError("--until must be a valid YYYY-MM-DD date") from exc
        if recheck_after <= (today or datetime.now(UTC).date()):
            raise CommandError("--until must be a future date")

    evidence_url = values.get("--evidence")
    if evidence_url is not None:
        evidence_url = _clean_text(evidence_url, name="evidence", limit=MAX_EVIDENCE_CHARS)
        try:
            validate_source_url(evidence_url, resolve=False)
        except SecurityError as exc:
            raise CommandError(f"--evidence must be a valid HTTPS URL: {exc}") from exc

    status = {
        "activate": "active",
        "pause": "paused",
        "dormant": "dormant",
        "retire": "retired",
    }[action]
    return StaleCommand(status, reason, recheck_after, evidence_url)


def _label_names(issue: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for label in issue.get("labels") or []:
        name = label.get("name") if isinstance(label, dict) else str(label)
        if name:
            names.add(name)
    return names


def _resolve_incident(event: dict[str, Any]) -> tuple[int, str, str]:
    issue = event.get("issue") or {}
    comment = event.get("comment") or {}
    if not isinstance(issue, dict) or not isinstance(comment, dict):
        raise CommandError("the event has no valid issue or comment payload")
    if issue.get("pull_request") is not None:
        raise CommandError("/stale commands apply only to generated child issues")
    if str(issue.get("state") or "").lower() != "open":
        raise CommandError("the stale-feed child issue is not open")
    if "signal:feed-health" not in _label_names(issue):
        raise CommandError("the issue is not a generated feed-health incident")
    match = INCIDENT_RE.search(str(issue.get("body") or ""))
    if not match:
        raise CommandError("the issue has no valid stale-incident marker")
    try:
        issue_number = int(issue["number"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CommandError("the event has no valid issue number") from exc
    if issue_number < 1:
        raise CommandError("the event has no valid issue number")
    return issue_number, match.group(2), match.group(1)


def _lifecycle_block(command: StaleCommand) -> str:
    lines = ["lifecycle:\n", f"  status: {command.status}\n"]
    if command.recheck_after is not None:
        lines.append(f"  recheck_after: {command.recheck_after.isoformat()}\n")
    lines.append(f"  reason: {json.dumps(command.reason, ensure_ascii=False)}\n")
    if command.evidence_url:
        lines.append(f"  evidence_url: {json.dumps(command.evidence_url)}\n")
    return "".join(lines)


def _mapping_entries(document: MappingNode, name: str) -> list[tuple[ScalarNode, Any]]:
    return [
        (key, value)
        for key, value in document.value
        if isinstance(key, ScalarNode) and key.value == name
    ]


def apply_lifecycle(path: Path, command: StaleCommand) -> bool:
    """Replace or insert one top-level lifecycle block while preserving unrelated YAML text."""
    original = path.read_text()
    document = yaml.compose(original)
    if not isinstance(document, MappingNode):
        raise CommandError(f"{path} must contain one top-level YAML mapping")
    lifecycle_entries = _mapping_entries(document, "lifecycle")
    if len(lifecycle_entries) > 1:
        raise CommandError(f"{path} contains duplicate lifecycle keys")

    lines = original.splitlines(keepends=True)
    block = (
        [] if command.status == "active" else _lifecycle_block(command).splitlines(keepends=True)
    )
    if lifecycle_entries:
        key, value = lifecycle_entries[0]
        start = key.start_mark.line
        end = value.end_mark.line + (1 if value.end_mark.column else 0)
        lines[start:end] = block
    else:
        podcast_entries = _mapping_entries(document, "podcast_title")
        insert_at = podcast_entries[0][0].start_mark.line if podcast_entries else len(lines)
        lines[insert_at:insert_at] = block
    updated = "".join(lines)
    if updated == original:
        return False
    path.write_text(updated)
    return True


def process_event(
    event: dict[str, Any],
    *,
    repo_root: Path,
    permission: dict[str, Any],
    today: date | None = None,
) -> dict[str, Any]:
    """Validate an issue-comment event, edit its feed YAML, and return the PR plan."""
    require_repository_write(permission)
    issue_number, slug, check = _resolve_incident(event)
    comment = event.get("comment") or {}
    actor = str((comment.get("user") or {}).get("login") or "")
    if not actor or not re.fullmatch(r"[A-Za-z0-9-]+", actor):
        raise CommandError("the event has no valid comment author")
    command = parse_command(str(comment.get("body") or ""), today=today)
    if command.status == "active" and check != "dormant-resumed":
        raise CommandError("`/stale activate` applies only to dormant-resumed incidents")
    config_path = Path("config") / "feeds" / f"{slug}.yml"
    target = (repo_root / config_path).resolve()
    feeds_root = (repo_root / "config" / "feeds").resolve()
    if target.parent != feeds_root or not target.is_file():
        raise CommandError(f"feed config does not exist: {config_path}")
    apply_lifecycle(target, command)

    site_config = load_site_config(repo_root / "config" / "site_config.yml")
    load_city_configs(repo_root / "config", site_config.get("defaults", {}))

    command_name = {
        "active": "activate",
        "paused": "pause",
        "dormant": "dormant",
        "retired": "retire",
    }[command.status]
    marker = f"<!-- citypods:stale-disposition:v1 issue={issue_number} slug={slug} -->"
    details = [
        f"- Feed: `{slug}`",
        f"- Requested lifecycle: `{command.status}`",
        f"- Reason: {command.reason}",
    ]
    if command.recheck_after:
        details.append(f"- Recheck after: `{command.recheck_after.isoformat()}`")
    if command.evidence_url:
        details.append(f"- Evidence: {command.evidence_url}")
    pr_body = "\n".join(
        [
            f"Requested by @{actor} from stale-feed incident #{issue_number}.",
            "",
            *details,
            "",
            "Merging this PR is the durable maintainer approval. The incident remains open until "
            "a later feed-health audit observes the committed lifecycle disposition.",
            "",
            "No pipeline version changes and no audio/ASR backfill.",
            "",
            marker,
        ]
    )
    return {
        "ok": True,
        "issue_number": issue_number,
        "slug": slug,
        "status": command.status,
        "config_path": config_path.as_posix(),
        "branch": f"chore/stale-{issue_number}-lifecycle",
        "commit_message": f"config: {command_name} stale feed {slug}",
        "pr_title": f"Set {slug} feed lifecycle to {command.status}",
        "pr_body": pr_body,
        "issue_comment": (
            f"Lifecycle `{command.status}` requested by @{actor}. A reviewable YAML PR is "
            "being created or updated; this incident remains open until that PR merges and a "
            "later audit observes the committed disposition."
        ),
    }


def _write_result(path: Path, result: dict[str, Any]) -> None:
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", required=True, help="GitHub issue_comment event JSON")
    parser.add_argument("--permission", required=True, help="GitHub repository permission JSON")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    out = Path(args.out)
    event: Any = None
    try:
        event = json.loads(Path(args.event).read_text())
        if not isinstance(event, dict):
            raise CommandError("the event payload must be a JSON object")
        permission = json.loads(Path(args.permission).read_text())
        if not isinstance(permission, dict):
            raise CommandError("the permission payload must be a JSON object")
        result = process_event(
            event,
            repo_root=Path(args.repo_root).resolve(),
            permission=permission,
        )
    except (CommandError, json.JSONDecodeError, OSError, ValueError, yaml.YAMLError) as exc:
        _write_result(
            out,
            {
                "ok": False,
                "issue_number": (event.get("issue") or {}).get("number")
                if isinstance(event, dict) and isinstance(event.get("issue"), dict)
                else None,
                "comment": f"❌ `/stale` command rejected: {exc}",
            },
        )
        return 2
    _write_result(out, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
