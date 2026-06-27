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
from pathlib import Path

from citypods.audit import Finding, audit_all
from citypods.config import load_city_configs, load_site_config
from citypods.state import pull_canonical_state
from citypods.statesync import push_state
from citypods.storage import make_storage

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


def reconcile(findings, *, dry_run: bool, audited_slugs: set[str] | None = None) -> int:
    # One issue per (slug, check); if a city somehow yields two findings for the same check,
    # the later one wins — still a single issue for that pair.
    wanted = {_title(f.slug, f.check): (f, _body(f.message, f.severity, f.check)) for f in findings}
    # _open_issues() is read-only (gh issue list), so it's safe to call in dry-run too -- forcing
    # existing={} there collapsed every UPDATE/CLOSE/SKIP-CLOSE branch into CREATE, making the
    # preview useless for telling a stamping-everything-new run from a real reconcile.
    existing = _open_issues()
    if audited_slugs is not None:
        # --city (or any other subset of cities) was used: an issue belonging to a city outside
        # that scope was never re-evaluated this run, so it must not be touched (in particular,
        # never closed as "stale" just because it's absent from this run's `wanted`).
        existing = {
            title: issue
            for title, issue in existing.items()
            if title.removeprefix(f"{TITLE_PREFIX} ").split(":", 1)[0] in audited_slugs
        }

    created = updated = closed = 0
    for title, (finding, body) in wanted.items():
        sev_label = f"severity:{finding.severity}"
        if title in existing:
            issue = existing[title]
            needs_human = finding.check in _MEETINGS_URL_CHECKS
            desired_labels = {"signal:feed-health", "type:operations", sev_label}
            if needs_human:
                desired_labels.add("needs:human-verification")
            current_labels = _label_names(issue)
            # Severity is the only label that can change meaning between runs (a finding's
            # severity can shift); any other stale severity:* label must go so exactly one
            # severity label is ever present.
            to_remove = {
                lbl for lbl in current_labels if lbl.startswith("severity:") and lbl != sev_label
            }
            to_add = desired_labels - current_labels
            body_changed = issue.get("body", "").strip() != body.strip()
            if body_changed or to_add or to_remove:
                if dry_run:
                    label_note = ""
                    if to_add or to_remove:
                        label_note = f"  +{sorted(to_add)} -{sorted(to_remove)}"
                    print(f"UPDATE  {title}{label_note}")
                else:
                    num = str(issue["number"])
                    if body_changed:
                        _gh("issue", "edit", num, "--body", body)
                        _gh("issue", "comment", num, "--body", _state_comment(finding))
                    if to_add or to_remove:
                        label_args = []
                        for lbl in sorted(to_add):
                            label_args += ["--add-label", lbl]
                        for lbl in sorted(to_remove):
                            label_args += ["--remove-label", lbl]
                        _gh("issue", "edit", num, *label_args)
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
    ap.add_argument(
        "--timeline-diagnostics",
        help="write timeline/audio duration diagnostics as JSONL for review/PR6 gating",
    )
    ap.add_argument(
        "--persist-timeline-integrity",
        action="store_true",
        help="persist confirmed timeline/audio repair flags to durable state",
    )
    ap.add_argument("--site-config", default="config/site_config.yml")
    ap.add_argument("--config-dir", default="config")
    args = ap.parse_args(argv)

    site_config = load_site_config(args.site_config)
    cities = load_city_configs(args.config_dir, site_config.get("defaults", {}))
    if args.city:
        cities = [c for c in cities if c.slug == args.city]

    # Pull the canonical record store from the bucket before auditing it. Without this, the
    # audit only ever saw whatever actions/cache/restore's "build-state-" prefix match happened
    # to land on — which collides with audio.yml's per-shard caches and preview.yml's PR caches,
    # so it could compare an EDL and a served-duration captured at two different points in the
    # pipeline's history and file a false-positive timeline-duration-mismatch/
    # timeline-short-coverage finding.
    output_dir = "docs"
    state_dir = pull_canonical_state(site_config, output_dir)

    timeline_diagnostics: list[dict] | None = [] if args.timeline_diagnostics else None
    findings = audit_all(
        cities,
        site_config=site_config,
        output_dir=output_dir,
        check_enclosures_net=args.enclosures,
        check_meetings_urls_net=args.meetings_urls,
        timeline_diagnostics=timeline_diagnostics,
        persist_timeline_integrity=args.persist_timeline_integrity,
    )
    if args.timeline_diagnostics and timeline_diagnostics is not None:
        path = Path(args.timeline_diagnostics)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            for row in timeline_diagnostics:
                f.write(json.dumps(row, sort_keys=True) + "\n")
        print(f"timeline diagnostics: wrote {len(timeline_diagnostics)} row(s) to {path}")
    if args.persist_timeline_integrity:
        storage = make_storage(site_config, site_config.get("base_url", ""), output_dir)
        prefixes = sorted(
            {f"sources/{p.parent.name}/" for p in Path(state_dir).glob("sources/*/episodes.json")}
        )
        pushed = push_state(storage, Path(state_dir), only_prefixes=prefixes)
        print(f"timeline integrity: pushed {pushed} state file(s)")
    for f in findings:
        print(f"  {f.severity:5} {f.slug} [{f.check}] {f.message}")

    if not args.dry_run:
        _ensure_labels()
    audited_slugs = {c.slug for c in cities} if args.city else None
    reconcile(findings, dry_run=args.dry_run, audited_slugs=audited_slugs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
