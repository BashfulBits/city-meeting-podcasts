#!/usr/bin/env python
"""Run the feed-health audit and reconcile findings to GitHub issues (idempotently).

One issue per ``(slug, check)`` — title ``[feed-health] <slug>: <check>``. On each run:

  * a current finding with no matching open issue  -> create it;
  * a current finding whose issue exists           -> update the body if it changed;
  * an open feed-health issue with no matching finding -> close it (the problem resolved).

This keeps the issue tracker a live, deduplicated view of feed health instead of a pile of
duplicates. Designed for the daily ``audit.yml`` cron (GITHUB_TOKEN -> ``gh``), but runs
locally with ``--dry-run`` to preview without touching GitHub.

Usage:
    PYTHONPATH=. python scripts/audit_feeds.py [--dry-run] [--enclosures] [--city SLUG]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from citypods.audit import audit_all
from citypods.config import load_city_configs, load_site_config

LABELS = {
    "feed-health": ("0E8A16", "Automated feed-health finding"),
    "severity:error": ("B60205", "A feed is broken (no/dead episodes)"),
    "severity:warn": ("FBCA04", "A feed may be degraded or incomplete"),
}
TITLE_PREFIX = "[feed-health]"
MARKER = "<!-- citypods:feed-health -->"


def _title(slug: str, check: str) -> str:
    return f"{TITLE_PREFIX} {slug}: {check}"


def _body(message: str, severity: str) -> str:
    return (
        f"{MARKER}\n\n"
        f"**Severity:** {severity}\n\n"
        f"{message}\n\n"
        "_Filed automatically by the feed-health audit. It auto-closes when the check "
        "passes again. See `citypods doctor` to reproduce locally._"
    )


def _gh(*args: str, check: bool = True) -> str:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def _ensure_labels() -> None:
    for name, (color, desc) in LABELS.items():
        _gh(
            "label", "create", name, "--color", color, "--description", desc, "--force", check=False
        )


def _open_issues() -> dict[str, dict]:
    out = _gh(
        "issue",
        "list",
        "--label",
        "feed-health",
        "--state",
        "open",
        "--json",
        "number,title,body",
        "--limit",
        "1000",
    )
    issues = json.loads(out or "[]")
    return {i["title"]: i for i in issues if i["title"].startswith(TITLE_PREFIX)}


def reconcile(findings, *, dry_run: bool) -> int:
    # One issue per (slug, check); if a city somehow yields two findings for the same check,
    # the later one wins — still a single issue for that pair.
    wanted = {_title(f.slug, f.check): (f, _body(f.message, f.severity)) for f in findings}
    existing = {} if dry_run else _open_issues()

    created = updated = closed = 0
    for title, (finding, body) in wanted.items():
        sev_label = f"severity:{finding.severity}"
        if title in existing:
            if existing[title].get("body", "").strip() != body.strip():
                if dry_run:
                    print(f"UPDATE  {title}")
                else:
                    _gh("issue", "edit", str(existing[title]["number"]), "--body", body)
                updated += 1
        else:
            if dry_run:
                print(f"CREATE  {title}  [{sev_label}]")
            else:
                _gh(
                    "issue",
                    "create",
                    "--title",
                    title,
                    "--body",
                    body,
                    "--label",
                    "feed-health",
                    "--label",
                    sev_label,
                )
            created += 1

    for title, issue in existing.items():
        if title not in wanted:
            if dry_run:
                print(f"CLOSE   {title}")
            else:
                _gh(
                    "issue",
                    "close",
                    str(issue["number"]),
                    "--comment",
                    "✅ Resolved — the feed-health check now passes.",
                )
            closed += 1

    print(
        f"\n{len(wanted)} active finding(s): {created} created, {updated} updated, {closed} closed."
    )
    return created + updated  # nonzero exit-ish signal handled by caller if desired


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="print actions; touch nothing")
    ap.add_argument("--enclosures", action="store_true", help="also HEAD-probe enclosures")
    ap.add_argument("--city", help="audit only this slug")
    ap.add_argument("--site-config", default="site_config.yml")
    ap.add_argument("--cities-dir", default="cities")
    args = ap.parse_args(argv)

    site_config = load_site_config(args.site_config)
    cities = load_city_configs(args.cities_dir, site_config.get("defaults", {}))
    if args.city:
        cities = [c for c in cities if c.slug == args.city]

    findings = audit_all(cities, site_config=site_config, check_enclosures_net=args.enclosures)
    for f in findings:
        print(f"  {f.severity:5} {f.slug} [{f.check}] {f.message}")

    if not args.dry_run:
        _ensure_labels()
    reconcile(findings, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
