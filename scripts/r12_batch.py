"""Materialize approved R12 evidence into exact config paths for one review PR.

This script is intentionally GitHub-agnostic: the workflow obtains approved issue evidence and
opens/updates the PR.  Keeping file materialization here makes the critical no-overwrite checks
offline-testable and keeps the Action from using broad staging commands.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml


class BatchError(RuntimeError):
    """An approved evidence artifact is stale, incomplete, or no longer safe to apply."""


def _sections(proposed_yaml: str) -> dict[Path, dict]:
    sections: dict[Path, list[str]] = {}
    current: Path | None = None
    for line in proposed_yaml.splitlines():
        if line.startswith("# config/") and line.endswith(".yml"):
            current = Path(line[2:])
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    if not sections:
        raise BatchError("proposal omitted exact config path markers")
    parsed: dict[Path, dict] = {}
    for path, lines in sections.items():
        if path.parts[:2] not in {("config", "cities"), ("config", "feeds")} or ".." in path.parts:
            raise BatchError("proposal target must be under config/cities or config/feeds")
        value = yaml.safe_load("\n".join(lines)) or {}
        if not isinstance(value, dict):
            raise BatchError(f"proposal for {path} is not a YAML mapping")
        parsed[path] = value
    return parsed


def _append_yaml_mapping(target: Path, addition: dict) -> None:
    """Append reviewed keys without reserializing any pre-existing YAML content."""
    original = target.read_text()
    suffix = yaml.safe_dump(addition, sort_keys=False)
    target.write_text(original.rstrip() + "\n\n" + suffix)


def _created_at(value: str) -> datetime:
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise BatchError("proposal omitted a valid evidence timestamp") from exc
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)


def apply_evidence(repo_root: Path, evidence: dict, *, now: datetime | None = None) -> list[Path]:
    """Write only the exact approved files after freshness and no-overwrite checks."""
    now = now or datetime.now(UTC)
    created = _created_at(str(evidence.get("evidence_created_at", "")))
    if now - created > timedelta(days=90):
        raise BatchError("proposal evidence expired; run discovery again")
    verification = evidence.get("verification") or {}
    proposal = evidence.get("proposed_yaml")
    request = evidence.get("request") or {}
    if not verification.get("signature_verified") or not verification.get("provider_verified"):
        raise BatchError("proposal has not passed mandatory verification")
    if not verification.get("sample_media_url") or not isinstance(proposal, str):
        raise BatchError("proposal is research-only and cannot enter a batch")
    mode = request.get("mode")
    files = _sections(proposal)
    city_slug = request.get("city_slug")
    if not isinstance(city_slug, str) or not city_slug:
        raise BatchError("proposal omitted a valid request city slug")
    changed: list[Path] = []
    if mode == "new-city":
        expected = {
            Path("config/cities") / f"{city_slug}.yml",
            Path("config/feeds") / f"{city_slug}.yml",
        }
        if set(files) != expected:
            raise BatchError("new-city proposal paths must match the requested city slug")
        if any((repo_root / path).exists() for path in files):
            raise BatchError(
                "new-city target already exists; place it in the separate review queue"
            )
        for path, value in files.items():
            target = repo_root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(yaml.safe_dump(value, sort_keys=False))
            changed.append(path)
    elif mode == "auxiliary":
        if len(files) != 1:
            raise BatchError("auxiliary proposal must target exactly one existing config file")
        path, addition = next(iter(files.items()))
        feed_target = Path("config/feeds") / f"{city_slug}.yml"
        city_target = Path("config/cities") / f"{city_slug}.yml"
        if path == feed_target:
            pass
        elif path.parent == Path("config/cities") and (repo_root / feed_target).exists():
            feed = yaml.safe_load((repo_root / feed_target).read_text()) or {}
            if not isinstance(feed, dict) or feed.get("city") != path.stem:
                raise BatchError("auxiliary city target is not owned by the requested feed slug")
        elif path != city_target:
            raise BatchError("auxiliary proposal path must match the requested city slug")
        target = repo_root / path
        if not target.exists():
            raise BatchError("auxiliary target no longer exists")
        before = yaml.safe_load(target.read_text()) or {}
        if not isinstance(before, dict) or {"aux_provider", "aux_source"} & before.keys():
            raise BatchError(
                "auxiliary target already has an auxiliary source; do not overwrite it"
            )
        if set(addition) != {"aux_provider", "aux_source"}:
            raise BatchError("auxiliary proposal may only add aux_provider and aux_source")
        # The existing mapping remains value-identical because it is left byte-for-byte in place.
        _append_yaml_mapping(target, addition)
        changed.append(path)
    else:
        raise BatchError("unknown R12 evidence mode")
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--evidence", required=True, action="append", help="approved evidence JSON")
    parser.add_argument("--changed-out", required=True)
    args = parser.parse_args(argv)
    root = Path(args.repo_root).resolve()
    changed: list[str] = []
    for raw in args.evidence:
        evidence = json.loads(Path(raw).read_text())
        changed.extend(str(path) for path in apply_evidence(root, evidence))
    Path(args.changed_out).write_text(json.dumps(sorted(set(changed))) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
