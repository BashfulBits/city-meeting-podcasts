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
import re
import subprocess
import sys

from citypods.audit import Finding, audit_all
from citypods.config import load_city_configs, load_site_config

LABELS = {
    "signal:feed-health": ("0E8A16", "Automated feed-health finding"),
    "type:operations": ("5319E7", "Operational work, not a feature or bug"),
    "severity:error": ("B60205", "A feed is broken (no/dead episodes)"),
    "severity:warn": ("FBCA04", "A feed may be degraded or incomplete"),
    "needs:human-verification": ("C5DEF5", "Requires manual investigation before auto-closing"),
}
TITLE_PREFIX = "[feed-health]"
MARKER = "<!-- citypods:feed-health -->"


def _title(slug: str, check: str) -> str:
    return f"{TITLE_PREFIX} {slug}: {check}"


_MEETINGS_URL_CHECKS = frozenset({"meetings-url-dead", "meetings-url-changed"})
_DEAD_MEETINGS_URL_STATUSES = frozenset({404, 410, 451})


def _state_comment(finding: Finding) -> str:
    """A comment posted when an existing issue's computed state changes."""
    from datetime import UTC, datetime

    date = datetime.now(UTC).strftime("%Y-%m-%d")
    return (
        f"**Audit update {date}:** `{finding.severity}` — {finding.message}\n\n"
        "_Added automatically when the finding state changed. "
        "The issue body has been updated with the current state._"
    )


def _body(message: str, severity: str, check: str = "") -> str:
    footer = (
        "**Action required:** verify the city's current meeting archive page and update "
        "`meetings_url` in the city YAML. This issue will NOT auto-close while it has "
        "the `needs:human-verification` label — remove the label once the YAML has "
        "been updated and verified."
        if check in _MEETINGS_URL_CHECKS
        else "_Filed automatically by the feed-health audit. It auto-closes when the check "
        "passes again. See `citypods doctor` to reproduce locally._"
    )
    return f"{MARKER}\n\n**Severity:** {severity}\n\n{message}\n\n{footer}"


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
        "signal:feed-health",
        "--state",
        "open",
        "--json",
        "number,title,body,labels",
        "--limit",
        "1000",
    )
    issues = json.loads(out or "[]")
    return {i["title"]: i for i in issues if i["title"].startswith(TITLE_PREFIX)}


def _label_names(issue: dict) -> set[str]:
    names: set[str] = set()
    for label in issue.get("labels") or []:
        if isinstance(label, dict):
            name = label.get("name")
        else:
            name = str(label)
        if name:
            names.add(name)
    return names


def _is_obsolete_meetings_url_issue(issue: dict, check_name: str) -> bool:
    if check_name != "meetings-url-dead":
        return False
    match = re.search(r"meetings_url returned HTTP (\d{3}):", issue.get("body") or "")
    if not match:
        return False
    return int(match.group(1)) not in _DEAD_MEETINGS_URL_STATUSES


def reconcile(findings, *, dry_run: bool) -> int:
    # One issue per (slug, check); if a city somehow yields two findings for the same check,
    # the later one wins — still a single issue for that pair.
    wanted = {_title(f.slug, f.check): (f, _body(f.message, f.severity, f.check)) for f in findings}
    existing = {} if dry_run else _open_issues()

    created = updated = closed = 0
    for title, (finding, body) in wanted.items():
        sev_label = f"severity:{finding.severity}"
        if title in existing:
            if existing[title].get("body", "").strip() != body.strip():
                if dry_run:
                    print(f"UPDATE  {title}")
                else:
                    num = str(existing[title]["number"])
                    _gh("issue", "edit", num, "--body", body)
                    _gh("issue", "comment", num, "--body", _state_comment(finding))
                updated += 1
        else:
            needs_human = finding.check in _MEETINGS_URL_CHECKS
            extra_labels = ["needs:human-verification"] if needs_human else []
            if dry_run:
                suffix = " [needs:human-verification]" if needs_human else ""
                print(f"CREATE  {title}  [{sev_label}]{suffix}")
            else:
                label_args = [
                    "--label",
                    "signal:feed-health",
                    "--label",
                    "type:operations",
                    "--label",
                    sev_label,
                ]
                for lbl in extra_labels:
                    label_args += ["--label", lbl]
                _gh("issue", "create", "--title", title, "--body", body, *label_args)
            created += 1

    for title, issue in existing.items():
        if title not in wanted:
            # meetings-url issues require human verification — don't auto-close even when the
            # probe passes again (the URL may have come back up without the right content).
            # A human removes the needs:human-verification label when they've verified the YAML.
            # Legacy 403/429/5xx issues are not valid findings under the current browser-visible
            # meetings_url policy, so let the next reconcile sweep them.
            suffix = title.removeprefix(TITLE_PREFIX)
            check_name = suffix.split(":", 1)[-1].strip() if ":" in suffix else ""
            if (
                check_name in _MEETINGS_URL_CHECKS
                and "needs:human-verification" in _label_names(issue)
                and not _is_obsolete_meetings_url_issue(issue, check_name)
            ):
                if dry_run:
                    print(f"SKIP-CLOSE (needs:human-verification)  {title}")
                continue
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
    ap.add_argument(
        "--meetings-urls",
        action="store_true",
        help="HEAD-probe each city's meetings_url (one probe per unique URL)",
    )
    ap.add_argument("--city", help="audit only this slug")
    ap.add_argument("--site-config", default="config/site_config.yml")
    ap.add_argument("--config-dir", default="config")
    args = ap.parse_args(argv)

    site_config = load_site_config(args.site_config)
    cities = load_city_configs(args.config_dir, site_config.get("defaults", {}))
    if args.city:
        cities = [c for c in cities if c.slug == args.city]

    findings = audit_all(
        cities,
        site_config=site_config,
        check_enclosures_net=args.enclosures,
        check_meetings_urls_net=args.meetings_urls,
    )
    for f in findings:
        print(f"  {f.severity:5} {f.slug} [{f.check}] {f.message}")

    if not args.dry_run:
        _ensure_labels()
    reconcile(findings, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
