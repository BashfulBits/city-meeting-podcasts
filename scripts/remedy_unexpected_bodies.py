#!/usr/bin/env python3
"""CLI script for automated unexpected-body remediation.

Usage:
    PYTHONPATH=. python scripts/remedy_unexpected_bodies.py --issue 1231 [--dry-run]
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
    apply_remedy_mutations,
    classify_unexpected_bodies,
    format_remedy_markdown_table,
    gather_unexpected_body_evidence,
    verify_remedy_mutations,
)
from citypods.config import load_city_configs, load_site_config
from citypods.records import load_records, source_key
from citypods.state import pull_canonical_state
from citypods.storage import make_storage


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue", required=False, help="GitHub Issue number to resolve")
    parser.add_argument(
        "--evidence-file",
        required=False,
        help="Path to pre-extracted evidence JSON file",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run LLM classification and print proposed changes without modifying files or git",
    )
    parser.add_argument("--repo-root", default=".", help="Repository root directory")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    repo_root = Path(args.repo_root)

    site_config = load_site_config(repo_root / "config" / "site_config.yml")
    storage = make_storage(site_config, "", repo_root / ".citypods-state")

    evidence_bundle: list[dict[str, Any]] = []

    if args.evidence_file:
        evidence_path = Path(args.evidence_file)
        if not evidence_path.exists():
            print(f"Error: evidence file not found: {evidence_path}", file=sys.stderr)
            sys.exit(1)
        evidence_bundle = json.loads(evidence_path.read_text(encoding="utf-8"))
    else:
        print("Gathering evidence across configured city feeds...", file=sys.stderr)
        cities_map = load_city_configs(repo_root / "config")
        # Pull state if needed for records
        pull_canonical_state(storage, repo_root / ".citypods-state")

        # When no specific evidence file is given, check all sources
        sources_seen = set()
        for city in cities_map.values():
            src_key = source_key(city.source)
            if src_key in sources_seen:
                continue
            sources_seen.add(src_key)
            records = load_records(repo_root / ".citypods-state", src_key)
            related = [c for c in cities_map.values() if source_key(c.source) == src_key]
            # Gather available historical context
            ev = gather_unexpected_body_evidence(
                source_key=src_key,
                city_slug=city.city,
                unmatched_episodes=[],
                related_cities=related,
                records=records,
                repo_root=repo_root,
            )
            if ev.get("unexpected_findings"):
                evidence_bundle.append(ev)

    if not evidence_bundle:
        print("No unexpected body evidence to process.", file=sys.stderr)
        return

    print(f"Classifying {len(evidence_bundle)} source finding(s) via LLM...", file=sys.stderr)
    remedy = classify_unexpected_bodies(evidence_bundle, storage=storage)

    table_md = format_remedy_markdown_table(remedy)
    print("\nProposed Remedies:\n", file=sys.stderr)
    print(table_md, file=sys.stderr)

    if args.dry_run:
        print("\nDry-run mode: skipping file modifications and git actions.", file=sys.stderr)
        return

    modified = apply_remedy_mutations(remedy, repo_root=repo_root)
    print(f"\nApplied mutations to {len(modified)} file(s).", file=sys.stderr)

    ok, message = verify_remedy_mutations(repo_root=repo_root)
    print(f"Verification: {message}", file=sys.stderr)

    if not ok:
        print("Verification checks failed; aborting automated PR creation.", file=sys.stderr)
        if args.issue and os.environ.get("GH_TOKEN"):
            comment_body = (
                f"### Automated Remedy Proposal (Verification Failed)\n\n"
                f"{table_md}\n\n"
                f"> **Notice:** Automated verification failed. Manual review required."
            )
            subprocess.run(
                ["gh", "issue", "comment", str(args.issue), "--body", comment_body],
                check=False,
            )
        sys.exit(1)

    if args.issue and os.environ.get("GH_TOKEN"):
        branch = f"fix/{args.issue}-unexpected-feed-bodies"
        subprocess.run(["git", "checkout", "-b", branch], cwd=repo_root, check=True)
        subprocess.run(["git", "add", "config/feeds/"], cwd=repo_root, check=True)
        subprocess.run(
            ["git", "commit", "-m", f"fix(config): resolve unexpected bodies (#{args.issue})"],
            cwd=repo_root,
            check=True,
        )
        subprocess.run(["git", "push", "-u", "origin", branch], cwd=repo_root, check=True)

        pr_body = f"Resolves #{args.issue}\n\n### Automated Remedy Classifications\n\n{table_md}\n"
        subprocess.run(
            [
                "gh",
                "pr",
                "create",
                "--title",
                f"fix(config): resolve unexpected feed bodies (#{args.issue})",
                "--body",
                pr_body,
            ],
            cwd=repo_root,
            check=True,
        )
        print(f"Successfully created pull request for issue #{args.issue}.", file=sys.stderr)


if __name__ == "__main__":
    main()
