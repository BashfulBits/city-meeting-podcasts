#!/usr/bin/env python
"""Migrate one legacy consolidated stale issue to native lifecycle sub-issues.

The command is dry-run by default. Pass ``--apply`` only after reviewing the complete plan.
Children are created and attached before the parent marker changes, so an interrupted migration
is safe to resume and the normal audit continues suppressing duplicate native stale incidents.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

from audit_feeds import (
    _attach_sub_issue,
    _cohort_title,
    _created_issue_number,
    _feed_context,
    _FeedContext,
    _FeedRow,
    _gh,
    _incident_title,
    _label_names,
    _open_stale_catalog,
    _parse_state,
    _parse_state_rows,
    _render_cohort_body,
    _render_incident_body,
)

from citypods.config import load_city_configs, load_site_config

_LEGACY_STALE_MARKER = "<!-- citypods:feed-health:key=stale -->"
_NATIVE_COHORT_MARKER = "<!-- citypods:stale-cohort:v1 -->"


@dataclass(frozen=True)
class _MigrationChild:
    slug: str
    incident_id: str
    title: str
    body: str
    row: _FeedRow


@dataclass(frozen=True)
class _MigrationPlan:
    parent: int
    parent_title: str
    parent_body: str
    children: tuple[_MigrationChild, ...]


def _load_issue(number: int) -> dict:
    output = _gh(
        "issue",
        "view",
        str(number),
        "--json",
        "number,title,body,labels,state,subIssues",
    )
    issue = json.loads(output)
    if not isinstance(issue, dict):
        raise RuntimeError(f"issue #{number} returned an invalid payload")
    return issue


def _migration_plan(
    issue: dict,
    *,
    contexts: dict[str, _FeedContext],
    now: datetime,
) -> _MigrationPlan:
    parent = int(issue["number"])
    body = issue.get("body") or ""
    if _NATIVE_COHORT_MARKER in body:
        raise RuntimeError(f"issue #{parent} is already a native stale cohort")
    if _LEGACY_STALE_MARKER not in body:
        raise RuntimeError(f"issue #{parent} is not the legacy consolidated stale issue")

    state = _parse_state(body)
    rows = _parse_state_rows(state)
    first_seen = state.get("first_seen") or {}
    if not rows:
        raise RuntimeError(f"issue #{parent} has no parseable stale rows")
    if set(rows) != set(first_seen):
        missing_dates = sorted(set(rows) - set(first_seen))
        missing_rows = sorted(set(first_seen) - set(rows))
        raise RuntimeError(
            "legacy state is incomplete: "
            f"missing first_seen={missing_dates}, missing rows={missing_rows}"
        )
    missing_configs = sorted(set(rows) - set(contexts))
    if missing_configs:
        raise RuntimeError(f"feed config(s) not found: {', '.join(missing_configs)}")

    children: list[_MigrationChild] = []
    for slug, row in sorted(rows.items()):
        try:
            observed = datetime.fromisoformat(first_seen[slug])
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"invalid first_seen for {slug}: {first_seen[slug]!r}") from exc
        incident_id = f"{observed:%Y%m%d}-1"
        prior_state = {
            "first_seen": first_seen[slug],
            "last_observed": now.isoformat(),
            "severity": row.severity,
            "count": row.count,
            "example": row.example,
        }
        child_body = _render_incident_body(
            check="stale",
            slug=slug,
            incident_id=incident_id,
            parent=parent,
            row=row,
            context=contexts[slug],
            prior_state=prior_state,
            prior_incidents=[],
            now=now,
        )
        children.append(
            _MigrationChild(
                slug=slug,
                incident_id=incident_id,
                title=_incident_title("stale", slug, incident_id),
                body=child_body,
                row=row,
            )
        )

    total = len(children)
    parent_body = _render_cohort_body(total=total, open_count=total)
    parent_body += (
        "Migrated from the legacy consolidated stale-feed table. Historical first-observed "
        "timestamps and current evidence are preserved in the native sub-issues; the original "
        "table remains available in this issue's edit history.\n"
    )
    return _MigrationPlan(
        parent=parent,
        parent_title=_cohort_title(now, total=total, open_count=total),
        parent_body=parent_body,
        children=tuple(children),
    )


def _sub_issue_numbers(issue: dict) -> set[int]:
    children = issue.get("subIssues") or []
    # ``gh issue view --json subIssues`` currently returns a GraphQL connection object, while
    # fixtures and older clients may expose the nodes directly. Accept both shapes so a partial
    # migration can always resume without trying to attach the same child twice.
    if isinstance(children, dict):
        children = children.get("nodes") or []
    return {
        int(child["number"])
        for child in children
        if isinstance(child, dict) and child.get("number") is not None
    }


def _apply_plan(plan: _MigrationPlan, *, github_repo: str, issue: dict, dry_run: bool) -> None:
    catalog = _open_stale_catalog()
    attached = _sub_issue_numbers(issue)
    existing_by_slug = {
        slug: child
        for (check, slug), child in catalog.open_children.items()
        if check == "stale" and int(child["parent"]) == plan.parent
    }

    for child in plan.children:
        existing = existing_by_slug.get(child.slug)
        if existing is not None:
            if existing["incident_id"] != child.incident_id:
                raise RuntimeError(
                    f"existing child #{existing['number']} has unexpected incident id "
                    f"{existing['incident_id']} for {child.slug}"
                )
            child_number = int(existing["number"])
            print(f"KEEP    #{child_number} {child.title}")
        elif dry_run:
            child_number = -1
            print(f"CREATE  {child.title}")
        else:
            output = _gh(
                "issue",
                "create",
                "--title",
                child.title,
                "--body",
                child.body,
                "--label",
                "signal:feed-health",
                "--label",
                "type:operations",
                "--label",
                f"severity:{child.row.severity}",
                "--label",
                "needs:human-verification",
            )
            child_number = _created_issue_number(output)
            print(f"CREATED #{child_number} {child.title}")

        if child_number not in attached:
            if dry_run:
                print(f"ATTACH  {child.title} -> #{plan.parent}")
            else:
                _attach_sub_issue(
                    github_repo=github_repo,
                    parent=plan.parent,
                    child=child_number,
                )
                attached.add(child_number)
                print(f"ATTACHED #{child_number} -> #{plan.parent}")

    if dry_run:
        print(f"UPDATE  #{plan.parent} -> {plan.parent_title}")
        return

    # This is intentionally last. Until every child exists and is attached, the legacy marker
    # keeps the scheduled audit from creating a second cohort for the same stale findings.
    _gh(
        "issue",
        "edit",
        str(plan.parent),
        "--title",
        plan.parent_title,
        "--body",
        plan.parent_body,
        "--add-label",
        "signal:feed-health",
        "--add-label",
        "type:operations",
        "--add-label",
        "severity:warn",
    )
    _gh(
        "issue",
        "comment",
        str(plan.parent),
        "--body",
        f"Migrated {len(plan.children)} legacy stale-feed rows to native sub-issues. "
        "The daily audit now owns evidence refresh, recovery closure, recurrence, and cohort "
        "closure for this issue.",
    )
    print(f"UPDATED #{plan.parent} -> {plan.parent_title}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("issue", type=int, help="legacy consolidated stale issue number")
    parser.add_argument("--apply", action="store_true", help="perform the migration")
    parser.add_argument("--site-config", default="config/site_config.yml")
    parser.add_argument("--config-dir", default="config")
    args = parser.parse_args(argv)

    now = datetime.now(UTC)
    site_config = load_site_config(args.site_config)
    github_repo = site_config.get("github_repo")
    if not github_repo:
        parser.error("site config must define github_repo")
    cities = load_city_configs(args.config_dir, site_config.get("defaults", {}))
    contexts = {city.slug: _feed_context(city, github_repo=github_repo, now=now) for city in cities}
    issue = _load_issue(args.issue)
    if str(issue.get("state") or "").lower() != "open":
        raise RuntimeError(f"issue #{args.issue} must be open")
    if "signal:feed-health" not in _label_names(issue):
        raise RuntimeError(f"issue #{args.issue} is not labeled signal:feed-health")
    plan = _migration_plan(issue, contexts=contexts, now=now)
    _apply_plan(
        plan,
        github_repo=github_repo,
        issue=issue,
        dry_run=not args.apply,
    )
    if not args.apply:
        print("\nDry run only; pass --apply to perform these actions.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
