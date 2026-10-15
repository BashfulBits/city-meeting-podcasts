#!/usr/bin/env python3
"""Apply a checked P3 issue disposition through the existing remedy command workflow."""

from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from citypods.config import load_site_config
from citypods.ops.maintenance_leases import acquire
from citypods.remedy_actions import parse_disposition_comment, record_decision_action
from citypods.remedy_ledger import REMEDY_LEASE_KEY
from citypods.storage import make_storage


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _binding(issue_body: str) -> dict:
    matches = re.findall(r"<!-- citypods:remedy-p3:binding=([^\n]+) -->", issue_body)
    if len(matches) != 1:
        raise ValueError("issue must contain exactly one generated P3 binding")
    value = json.loads(matches[0], object_pairs_hook=_unique_object)
    if not isinstance(value, dict):
        raise ValueError("P3 issue binding must be an object")
    return value


def apply_comment(event: dict, permission: dict, *, storage, lease_factory) -> str:
    """Validate trusted webhook snapshots and append the checked human event."""
    issue = event.get("issue") or {}
    comment = event.get("comment") or {}
    if issue.get("pull_request") is not None:
        raise ValueError("P3 decisions must be issue comments, not PR comments")
    if (issue.get("user") or {}).get("login") != "github-actions[bot]":
        raise ValueError("P3 decision issue must be opened by github-actions[bot]")
    body = comment.get("body")
    action = parse_disposition_comment(body)
    if not isinstance(action, dict) or "feed_dispositions" not in action:
        raise ValueError("P3 issue commands must include typed feed_dispositions")
    issue_body = issue.get("body")
    binding = _binding(issue_body)
    evidence = {
        name: binding[name]
        for name in (
            "city",
            "source_key",
            "policy_id",
            "normalized_label",
            "evidence_hash",
            "config_hash",
            "policy_hash",
        )
    }
    evidence.update(
        recording_refs=action.get("recording_refs"),
        observed_at=binding.get("observed_at"),
    )
    repository = (event.get("repository") or {}).get("full_name")
    if not isinstance(repository, str):
        raise ValueError("webhook repository identity is missing")
    snapshot = {
        "repository": repository,
        "comment": comment,
        "permission": permission,
        "checked_at": datetime.now(UTC),
        "issue_number": issue.get("number"),
        "issue_body": issue_body,
    }
    with lease_factory() as lease:
        return record_decision_action(
            storage,
            action,
            lease=lease,
            trusted_snapshot=snapshot,
            evidence=evidence,
            now=datetime.now(UTC),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", required=True, type=Path)
    parser.add_argument("--permission", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        event = json.loads(args.event.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        body = (event.get("comment") or {}).get("body", "")
        action = parse_disposition_comment(body)
        permission = json.loads(
            args.permission.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        )
        repo_root = Path.cwd()
        site_config = load_site_config(repo_root / "config/site_config.yml")
        storage = make_storage(
            site_config,
            site_config.get("base_url", ""),
            repo_root / site_config.get("output_dir", "docs"),
        )
        if storage is None or not getattr(storage, "cas_capable", False):
            raise RuntimeError("P3 decision requires configured B2 storage and R2 CAS coordination")

        def lease_factory():
            return acquire(storage, owner="remedy-p3-decision", key=REMEDY_LEASE_KEY)

        event_key = apply_comment(event, permission, storage=storage, lease_factory=lease_factory)
        result = {
            "accepted": True,
            "event_key": event_key,
            "close_issue": action["state"] in {"covered", "excluded", "resolved"},
        }
    except Exception as exc:  # surfaced to the issue-comment workflow without exposing secrets
        error = f"{type(exc).__name__}: {exc}"
        if "stale or future snapshot" in error:
            error = "Evidence is stale. Wait for the next refresh, then submit the disposition."
        result = {"accepted": False, "error": error}
    args.result.parent.mkdir(parents=True, exist_ok=True)
    args.result.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if result["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
