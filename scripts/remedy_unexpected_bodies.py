#!/usr/bin/env python3
"""Propose and optionally apply fixes for the audit's ``unexpected-body`` findings.

Two phases, deliberately separable so a maintainer can read the proposal before anything is
written:

    # 1. collect evidence during a normal audit run (one provider fetch, no LLM)
    PYTHONPATH=. python scripts/audit_feeds.py --unexpected-body-evidence evidence.json

    # 2. classify and report; add --apply to edit files, --issue N to comment
    PYTHONPATH=. python scripts/remedy_unexpected_bodies.py --evidence-file evidence.json
    PYTHONPATH=. python scripts/remedy_unexpected_bodies.py --evidence-file evidence.json --apply

Reporting is the default. ``--apply`` edits feed YAML and runs the repository's own gate; it
opens a pull request only when ``--issue`` is given and the gate passed. Nothing is ever pushed
to an existing branch: the branch name carries the evidence digest, so re-running for the same
findings is idempotent and a re-run after new findings gets its own branch.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from citypods.audit_remedy import (
    SourceContext,
    apply_remedy_plan,
    classify_unexpected_bodies,
    feed_paths_by_slug,
    format_remedy_markdown,
    validate_proposals,
    verify_remedy_mutations,
)
from citypods.config import load_city_configs, load_site_config
from citypods.state import pull_canonical_state
from citypods.storage import make_storage


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence-file",
        required=True,
        help="Evidence JSON written by `audit_feeds.py --unexpected-body-evidence`",
    )
    parser.add_argument("--issue", help="GitHub issue number to comment on / resolve")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Edit feed YAML and run verification (default: report proposals only)",
    )
    parser.add_argument("--repo-root", default=".", help="Repository root directory")
    parser.add_argument(
        "--output", help="Also write the rendered markdown report to this path", default=""
    )
    return parser.parse_args(argv)


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _run(command: list[str], *, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=cwd, check=check, capture_output=True, text=True)


# Commit identity for an unattended runner, passed per-invocation so global git config is never
# mutated (a bare `git commit` on a fresh runner fails outright with no identity configured).
GIT_IDENTITY = [
    "-c",
    "user.name=citypods-audit-remedy[bot]",
    "-c",
    "user.email=citypods-audit-remedy@users.noreply.github.com",
]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = Path(args.repo_root).resolve()

    evidence_path = Path(args.evidence_file)
    if not evidence_path.exists():
        _log(f"Error: evidence file not found: {evidence_path}")
        return 1
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    bundles: list[dict[str, Any]] = payload.get("sources", [])
    if not bundles:
        _log("No unexpected-body evidence to process.")
        return 0

    site_config = load_site_config(repo_root / "config" / "site_config.yml")
    output_dir = repo_root / site_config.get("output_dir", "docs")
    pull_canonical_state(site_config, output_dir)
    storage = make_storage(site_config, site_config.get("base_url", ""), output_dir)

    cities = load_city_configs(repo_root / "config", {})
    cities_by_slug = {city.slug: city for city in cities}
    feed_paths = feed_paths_by_slug(repo_root)

    reports: list[str] = []
    modified: list[Path] = []
    accepted_total = rejected_total = 0

    for bundle in bundles:
        source_key = bundle.get("source_key", "")
        _log(f"Classifying {source_key} ({len(bundle.get('unexpected_findings', []))} label(s))…")
        try:
            # One call per source: bundles are independent, and a single combined prompt would
            # grow past the route's input ceiling as findings accumulate.
            remedy = classify_unexpected_bodies(bundle, storage=storage)
        except (RuntimeError, ValueError) as exc:
            _log(f"  classification failed for {source_key}: {exc}")
            reports.append(f"#### `{source_key}`\n\n> Classification failed: {exc}")
            continue

        plan = validate_proposals(remedy, bundle, feed_paths)
        accepted_total += len(plan.accepted)
        rejected_total += len(plan.rejected)
        for rejected in plan.rejected:
            _log(f"  rejected {rejected.proposal.unexpected_body!r}: {rejected.reason}")
        reports.append(format_remedy_markdown(plan, bundle))

        if args.apply and plan.accepted:
            sibling = next(
                (
                    cities_by_slug[feed["slug"]]
                    for feed in bundle.get("existing_feeds", [])
                    if feed["slug"] in cities_by_slug
                ),
                None,
            )
            if sibling is None:
                _log(f"  no configured sibling feed for {source_key}; skipping apply")
                continue
            modified += apply_remedy_plan(
                plan,
                feed_paths=feed_paths,
                source_context=SourceContext.from_city(sibling),
                repo_root=repo_root,
            )

    report_md = "\n\n".join(reports)
    print(report_md)
    if args.output:
        Path(args.output).write_text(report_md + "\n", encoding="utf-8")
    _log(f"\n{accepted_total} proposal(s) accepted, {rejected_total} rejected.")

    if not args.apply:
        _log("Report-only mode: no files were modified. Re-run with --apply to edit feeds.")
        return 0
    if not modified:
        _log("No files needed modification.")
        return 0

    _log(f"Modified {len(modified)} file(s); verifying…")
    ok, message = verify_remedy_mutations(repo_root=repo_root)
    _log(f"Verification: {message}")

    if not ok:
        _log("Verification failed; reverting the working tree.")
        _run(["git", "checkout", "--", "config/feeds"], cwd=repo_root, check=False)
        _run(["git", "clean", "-fd", "config/feeds"], cwd=repo_root, check=False)
        _comment_on_issue(
            args.issue,
            f"### Automated remedy proposal (verification failed)\n\n{report_md}\n\n"
            "> Verification failed, so no changes were kept. Manual review required.",
            cwd=repo_root,
        )
        return 1

    if not args.issue:
        _log("Changes applied and verified. Pass --issue N to open a pull request.")
        return 0
    return _open_pull_request(args.issue, report_md, evidence_path, repo_root)


def _comment_on_issue(issue: str | None, body: str, *, cwd: Path) -> None:
    if not issue or not os.environ.get("GH_TOKEN"):
        return
    _run(["gh", "issue", "comment", str(issue), "--body", body], cwd=cwd, check=False)


def _open_pull_request(issue: str, report_md: str, evidence_path: Path, repo_root: Path) -> int:
    if not os.environ.get("GH_TOKEN"):
        _log("GH_TOKEN not set; leaving changes in the working tree.")
        return 0

    # Digest-suffixed so a re-run over the same findings reuses one branch instead of colliding,
    # and a run over *new* findings never force-pushes over an open PR's history.
    digest = json.loads(evidence_path.read_text(encoding="utf-8")).get("digest", "manual")[:12]
    branch = f"fix/{issue}-unexpected-feed-bodies-{digest}"

    existing = _run(["git", "rev-parse", "--verify", branch], cwd=repo_root, check=False)
    if existing.returncode == 0:
        _run(["git", "checkout", branch], cwd=repo_root)
    else:
        _run(["git", "checkout", "-b", branch], cwd=repo_root)

    _run(["git", "add", "config/feeds"], cwd=repo_root)
    staged = _run(["git", "diff", "--cached", "--quiet"], cwd=repo_root, check=False)
    if staged.returncode == 0:
        _log("Nothing staged; skipping commit and PR.")
        return 0

    _run(
        [
            "git",
            *GIT_IDENTITY,
            "commit",
            "-m",
            f"fix(config): resolve unexpected meeting bodies (#{issue})",
        ],
        cwd=repo_root,
    )
    _run(["git", "push", "-u", "origin", branch], cwd=repo_root)

    body = (
        f"Resolves #{issue}\n\n"
        f"Proposals were generated from the audit's own `unexpected-body` rows and re-validated "
        f"against that evidence before being applied; anything unverifiable was rejected and is "
        f"listed below.\n\n{report_md}\n"
    )
    _run(
        [
            "gh",
            "pr",
            "create",
            "--title",
            f"fix(config): resolve unexpected feed bodies (#{issue})",
            "--body",
            body,
        ],
        cwd=repo_root,
    )
    _log(f"Opened a pull request from {branch}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
