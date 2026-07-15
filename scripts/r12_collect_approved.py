"""Collect exactly approved R12 evidence and backlog dispositions from public GitHub records."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

from citypods.discovery.render import evidence_digest, iter_evidence_markers

COMMAND_MARKER = "citypods:r12:command"
BOT_LOGIN = "github-actions[bot]"


def _gh(*args: str) -> Any:
    result = subprocess.run(["gh", *args], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def _command_markers(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prefix = f"<!-- {COMMAND_MARKER} "
    records: list[dict[str, Any]] = []
    for comment in comments:
        if (comment.get("user") or {}).get("login") != BOT_LOGIN:
            continue
        for line in str(comment.get("body") or "").splitlines():
            if line.startswith(prefix) and line.endswith(" -->"):
                try:
                    record = json.loads(line[len(prefix) : -4])
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
    return records


def _evidence(issue: dict[str, Any], comments: list[dict[str, Any]]) -> dict[str, dict]:
    artifacts: list[dict[str, Any]] = []
    if (issue.get("user") or {}).get("login") == BOT_LOGIN:
        artifacts.extend(iter_evidence_markers(str(issue.get("body") or "")))
    for comment in comments:
        if (comment.get("user") or {}).get("login") == BOT_LOGIN:
            artifacts.extend(iter_evidence_markers(str(comment.get("body") or "")))
    return {evidence_digest(item): item for item in artifacts}


def collect(
    repository: str, out: Path
) -> tuple[list[Path], list[dict[str, Any]], list[dict[str, Any]]]:
    """Download only artifacts whose immutable digest has an explicit approval record."""
    out.mkdir(parents=True, exist_ok=True)
    issues = _gh(
        "issue",
        "list",
        "--repo",
        repository,
        "--state",
        "open",
        "--limit",
        "1000",
        "--json",
        "number,title,labels,body,url",
    )
    evidence_paths: list[Path] = []
    backlog: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for issue in issues:
        labels = {item["name"] for item in issue.get("labels", [])}
        is_digest = str(issue.get("title", "")).startswith("[city-discovery]")
        if "r12:approved" not in labels and not is_digest:
            continue
        comments = _gh("api", f"repos/{repository}/issues/{issue['number']}/comments?per_page=100")
        artifacts = _evidence(issue, comments)
        for record in _command_markers(comments):
            digest = record.get("evidence_digest")
            evidence = artifacts.get(digest) if isinstance(digest, str) else None
            if evidence is None:
                continue
            if record.get("action") == "approve":
                if digest in seen:
                    continue
                seen.add(digest)
                path = out / f"evidence-{issue['number']}-{record['city_slug']}.json"
                path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
                evidence_paths.append(path)
                sources.append(
                    {
                        "number": issue["number"],
                        "city_slug": record["city_slug"],
                        "mode": (evidence.get("request") or {}).get("mode"),
                    }
                )
            elif record.get("action") in {"assign-provider", "create-provider"}:
                backlog.append(
                    {
                        "provider_key": record["provider_key"],
                        "name": record.get("name"),
                        "city_slug": record["city_slug"],
                        "origin_issue": issue["number"],
                        "evidence_url": issue["url"],
                        "checked_at": record.get("recorded_at"),
                    }
                )
    return evidence_paths, backlog, sources


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    out = Path(args.out)
    evidence, backlog, sources = collect(args.repository, out)
    (out / "evidence-files.txt").write_text("".join(f"{path}\n" for path in evidence))
    (out / "backlog-records.json").write_text(json.dumps(backlog, indent=2) + "\n")
    (out / "source-issues.json").write_text(json.dumps(sources, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
