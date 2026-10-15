#!/usr/bin/env python3
"""Publish bounded P3 consensus candidates as review PRs or reconciled disposition issues."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import yaml

from citypods import remedy_feedback as fb
from citypods import remedy_publication as pub
from citypods.audit_remedy import (
    SourceContext,
    apply_remedy_plan,
    feed_paths_by_slug,
    verify_remedy_mutations,
)
from citypods.bodies import body_key
from citypods.config import load_city_configs, load_site_config
from citypods.ops.maintenance_leases import acquire
from citypods.remedy_actions import _p3_generated_section
from citypods.remedy_evaluation import RemedyConfig, config_fingerprint
from citypods.remedy_ledger import REMEDY_LEASE_KEY, decision_id, load_decisions
from citypods.remedy_p3_publication import validated_p3_plan
from citypods.remedy_p3_runner import admitted_p3_routes, p3_enabled, stable_finding_id
from citypods.remedy_policy import canonical_hash
from citypods.remedy_queue import (
    Candidate,
    GitHubSnapshot,
    LedgerView,
    parse_snapshot,
    plan_publications,
    plan_report,
    render_binding,
    render_markers,
)
from citypods.storage import make_storage

# Match the audit's generated-section reconciliation so human notes survive refreshes.
from scripts.audit_feeds import (
    _GENERATED_END,
    _GENERATED_START,
    _gh,
    _replace_generated,
)

P3_POLICY_ID = "p3-unexpected-body-routing-v1"
P3_ISSUE_LABEL = "signal:remedy-p3"
P3_ISSUE_KEY = "<!-- citypods:remedy-p3:key={} -->"
MAX_PUBLISH_PER_RUN = 5
MAX_OPEN_P3_ISSUES = 12
MAX_ISSUE_BODY_CHARS = 55_000


_TIMELINE = (
    "timelineItems(itemTypes: [REOPENED_EVENT, CLOSED_EVENT], first: 50) { nodes { __typename "
    "... on ReopenedEvent { createdAt } ... on ClosedEvent { createdAt actor { login } } } }"
)
SNAPSHOT_QUERY = f"""
query($q: String!) {{
  search(query: $q, type: ISSUE, first: 100) {{
    issueCount
    pageInfo {{ hasNextPage }}
    nodes {{
      ... on PullRequest {{
        number url state createdAt mergedAt body headRefName author {{ login }}
        headRepository {{ nameWithOwner }} mergedBy {{ login }}
        {_TIMELINE}
      }}
      ... on Issue {{
        number url state createdAt body author {{ login }}
        {_TIMELINE}
      }}
    }}
  }}
}}
"""


def _fetch_snapshot() -> GitHubSnapshot:
    """Every remedy PR and issue on GitHub, marked by whoever opened it; incomplete on any error
    (an incomplete snapshot reserves nothing, so a failed read can only hold work back)."""
    try:
        repo = _gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner").strip()
        raw = _gh(
            "api",
            "graphql",
            "-f",
            f"query={SNAPSHOT_QUERY}",
            "-f",
            f'q=repo:{repo} "citypods:remedy" in:body sort:created-desc',
        )
        return parse_snapshot(json.loads(raw)["data"]["search"])
    except (RuntimeError, ValueError, KeyError, TypeError):
        return GitHubSnapshot((), complete=False)


def _ledger_views(storage, source_keys) -> dict[str, LedgerView]:
    """The P2 ledger's tip state per decision, read-only; damaged histories are left out."""
    views: dict[str, LedgerView] = {}
    for identity, history in load_decisions(storage, source_keys=sorted(source_keys)).items():
        if history.diagnostics or len(history.tip_ids) != 1:
            continue
        tip = next((e for e in history.events if e.event_id == history.tip_ids[0]), None)
        if tip is not None:
            views[identity] = LedgerView(
                history.state,
                tip.disposition,
                tip.evidence_hash,
                reopened=fb.reopened(history),
                artifact_ref=tip.artifact_ref,
            )
    return views


def _pr_candidate(plan, binding: dict, eligible_since: datetime) -> Candidate:
    proposal = plan.accepted[0]
    family = (
        f"new-{proposal.new_feed_slug}"
        if proposal.action == "new_feed"
        else (sorted(proposal.target_feeds) or [binding["normalized_label"]])[0]
    )
    return Candidate(
        decision_id=binding["decision_id"],
        city=binding["city"],
        family=family,
        outcome=proposal.action,
        kind="pr",
        eligible_since=eligible_since,
        evidence_hash=binding["evidence_hash"],
        new_body=proposal.action == "new_feed",
    )


def _issue_candidate(binding: dict, eligible_since: datetime) -> Candidate:
    return Candidate(
        decision_id=binding["decision_id"],
        city=binding["city"],
        family=binding["normalized_label"],
        outcome="manual",
        kind="issue",
        eligible_since=eligible_since,
        evidence_hash=binding["evidence_hash"],
    )


def _publication_report(candidates, source_keys, storage, limits) -> dict:
    """The plan this run would make under the P4 budgets. Report only: it writes nothing and the
    publisher below keeps its existing behaviour until the plan is accepted."""
    try:
        plan = plan_publications(
            candidates,
            _ledger_views(storage, source_keys),
            _fetch_snapshot(),
            limits=limits,
            now=datetime.now(UTC),
        )
        return {"mode": "report_only", **plan_report(plan)}
    except Exception as exc:  # noqa: BLE001 -- a report must never stop the publisher
        return {"mode": "report_only", "error": f"{type(exc).__name__}: {exc}"}


class GhCli:
    """The GitHub operations the live stage uses, through the authenticated ``gh`` CLI."""

    def find_pr(self, branch: str) -> list[dict]:
        rows = _gh(
            "pr", "list", "--head", branch, "--state", "all", "--json", "number,url,state",
            "--limit", "5",
        )  # fmt: skip
        return json.loads(rows or "[]")

    def create_pr(self, branch: str, title: str, body: str) -> str:
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
            handle.write(body)
        try:
            return _gh(
                "pr", "create", "--base", "main", "--head", branch, "--title", title,
                "--body-file", handle.name,
            ).strip()  # fmt: skip
        finally:
            Path(handle.name).unlink(missing_ok=True)

    def edit_body(self, kind: str, number: int, body: str) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
            handle.write(body)
        try:
            _gh(kind if kind == "issue" else "pr", "edit", str(number), "--body-file", handle.name)
        finally:
            Path(handle.name).unlink(missing_ok=True)

    def reopen(self, kind: str, number: int) -> None:
        _gh("pr" if kind == "pr" else "issue", "reopen", str(number))

    def create_issue(self, title: str, body: str) -> str:
        _gh("label", "create", P3_ISSUE_LABEL, "--color", "5319E7", "--force", check=False)
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
            handle.write(body)
        try:
            return _gh(
                "issue", "create", "--title", title, "--body-file", handle.name,
                "--label", P3_ISSUE_LABEL, "--label", "type:operations",
            ).strip()  # fmt: skip
        finally:
            Path(handle.name).unlink(missing_ok=True)


def _unit_detail(report: dict, binding: dict, plan, finding_id: str, evidence_hash: str):
    """What a PR body says about one decision, from its P3 report and validated plan."""
    decision = report.get("decision") or {}
    routes = [
        {
            "route_id": vote.get("route_id"),
            "upstream_model": vote.get("upstream_model"),
            "reasoning_level": vote.get("reasoning_level"),
        }
        for vote in decision.get("initial_votes") or []
    ]
    adjudication = decision.get("adjudication") or {}
    note = ""
    if adjudication:
        note = (
            f"JEV `{adjudication.get('jev_upstream_model')}` chose "
            f"{adjudication.get('choice')}; DeepSeek agreed on the full assignment."
        )
        deepseek = adjudication.get("deepseek") or {}
        if deepseek:
            routes.append(
                {
                    "route_id": deepseek.get("route_id"),
                    "upstream_model": deepseek.get("upstream_model"),
                    "reasoning_level": deepseek.get("reasoning_level"),
                }
            )
    proposal = plan.accepted[0]
    return pub.UnitDetail(
        decision_id=binding["decision_id"],
        binding=binding,
        finding_id=finding_id,
        evidence_hash=evidence_hash,
        action=proposal.action,
        target_feeds=list(proposal.target_feeds),
        new_feed_slug=proposal.new_feed_slug,
        routes=routes,
        adjudication=note,
        recording_count=len(binding.get("recording_refs") or []),
    )


def _refinement_candidate(identity: str, tip) -> Candidate:
    return Candidate(
        decision_id=identity,
        city=tip.city,
        family=tip.normalized_label,
        outcome="refinement",
        kind="issue",
        eligible_since=_parse_time(tip.occurred_at),
        evidence_hash=tip.evidence_hash,
        refines=tip.artifact_ref,
    )


def _tip_binding(tip) -> dict:
    return {
        "city": tip.city,
        "source_key": tip.source_key,
        "policy_id": tip.policy_id,
        "normalized_label": tip.normalized_label,
        "evidence_hash": tip.evidence_hash,
        "config_hash": tip.config_hash,
        "policy_hash": tip.policy_hash,
        "recording_refs": [
            {"source_key": r.source_key, "uid": r.uid, "provider_guid": r.provider_guid}
            for r in tip.recording_refs
        ],
    }


def _publish_live(
    *,
    config,
    candidates: list[Candidate],
    pr_meta: dict,
    issue_meta: dict,
    storage,
    repo_root: Path,
    dry_run: bool,
    deferred: list,
    source_keys: set[str],
    github=None,
    git=None,
    apply=None,
    verify=None,
    snapshot=None,
    now: datetime | None = None,
) -> dict:
    """Execute the P4 plan: one isolated PR per unit, updates, reopenings and new issues."""
    github = github or GhCli()
    now = now or datetime.now(UTC)
    histories = load_decisions(storage, source_keys=sorted(source_keys))
    views: dict[str, LedgerView] = {}
    for identity, history in histories.items():
        if history.diagnostics or len(history.tip_ids) != 1:
            continue
        tip = next(e for e in history.events if e.event_id == history.tip_ids[0])
        views[identity] = LedgerView(
            history.state,
            tip.disposition,
            tip.evidence_hash,
            reopened=fb.reopened(history),
            artifact_ref=tip.artifact_ref,
        )
    refinements = {}
    for identity, tip in fb.refinement_candidates(histories):
        candidates.append(_refinement_candidate(identity, tip))
        refinements[identity] = tip
    candidates = [
        replace(c, reopen_requested=views[c.decision_id].reopened) if c.decision_id in views else c
        for c in candidates
    ]
    plan = plan_publications(
        candidates,
        views,
        snapshot if snapshot is not None else _fetch_snapshot(),
        limits=config.limits,
        now=now,
    )
    report = {"mode": "live", **plan_report(plan)}
    if dry_run:
        return {
            "status": "dry_run",
            "published": [],
            "deferred": deferred,
            "publication_plan": report,
        }
    published: list[dict] = []
    failures: list[dict] = []
    for item in plan.reopens:
        github.reopen(item.artifact.kind, item.artifact.number)
        published.append({"type": "reopened", "url": item.artifact.url})
    for item in plan.updates:
        if item.artifact.kind == "issue":
            for identity in item.decision_ids:
                if identity in issue_meta and not isinstance(issue_meta[identity], tuple):
                    p3_report, binding = issue_meta[identity]
                    published.append(
                        {
                            "type": "issue_updated",
                            "url": _reconcile_issue(p3_report, binding, dry_run=False),
                        }
                    )
    git = git or pub.Git(repo_root)
    if plan.prs:
        git.run("fetch", "origin", "main")
        if apply is None:
            subprocess.run(["gh", "auth", "setup-git"], cwd=repo_root, check=True)
    work_root = Path(tempfile.mkdtemp(prefix="remedy-worktrees-"))
    try:
        for unit in plan.prs:
            metas = [pr_meta[i] for i in unit.decision_ids]
            details = [
                _unit_detail(
                    m["report"], m["binding"], m["plan"], m["finding_id"], m["evidence_hash"]
                )
                for m in metas
            ]

            def apply_unit(worktree: Path, metas=metas) -> list[Path]:
                changed: list[Path] = []
                for meta in metas:
                    changed += apply_remedy_plan(
                        meta["plan"],
                        feed_paths=feed_paths_by_slug(worktree),
                        source_context=meta["context"],
                        repo_root=worktree,
                    )
                return changed

            try:
                result = pub.publish_pr(
                    unit,
                    details,
                    git=git,
                    github=github,
                    work_root=work_root,
                    base="origin/main",
                    apply=apply or apply_unit,
                    verify=verify or verify_remedy_mutations,
                )
            except pub.PublicationError as exc:
                failures.append({"branch": unit.branch, "reason": str(exc)})
                continue
            published.append({"type": "proposal_pr", "url": result.url, "created": result.created})
    finally:
        shutil.rmtree(work_root, ignore_errors=True)
    for planned in plan.issues:
        meta = issue_meta.get(planned.decision_id)
        if isinstance(meta, tuple):
            p3_report, binding = meta
            published.append(
                {"type": "manual_issue", "url": _reconcile_issue(p3_report, binding, dry_run=False)}
            )
        elif planned.decision_id in refinements:
            tip = refinements[planned.decision_id]
            body = pub.refinement_issue_body(
                planned.decision_id,
                _tip_binding(tip),
                rejected_pr=tip.artifact_ref or "",
                evidence_hash=tip.evidence_hash,
            )
            title = f"[Remedy refinement] {tip.normalized_label} ({tip.city})"
            published.append({"type": "refinement_issue", "url": github.create_issue(title, body)})
    status = (
        "published"
        if published
        else "deferred"
        if (deferred or failures)
        else "queued"
        if plan.queued
        else "no_candidates"
    )
    return {
        "status": status,
        "published": published,
        "deferred": deferred,
        "failures": failures,
        "publication_plan": report,
    }


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _eligibility(collected_at: object) -> tuple[datetime | None, str]:
    """The time a finding became publishable, or why it cannot be read.

    The publication plan is a report: an unreadable collection time only drops the plan and never
    stops the publisher."""
    try:
        return _parse_time(str(collected_at)), ""
    except (TypeError, ValueError) as exc:
        return None, f"unreadable evidence collection time: {exc}"


def _binding_for(
    source: dict,
    finding: dict,
    report: dict,
    config_hash: str,
    parents: list[str],
    collected_at: str,
):
    city = source["city"]["slug"]
    source_key = source["source_key"]
    label = finding["unexpected_body"]
    normalized_label = body_key(label)
    refs = sorted(
        {
            canonical_hash(
                {
                    "source_key": source_key,
                    "uid": episode.get("uid"),
                    "provider_guid": episode.get("provider_guid"),
                }
            ): {
                "source_key": source_key,
                "uid": episode.get("uid"),
                "provider_guid": episode.get("provider_guid"),
            }
            for episode in finding.get("episodes", [])
            if episode.get("uid") or episode.get("provider_guid")
        }.values(),
        key=canonical_hash,
    )
    identity = decision_id(city, source_key, P3_POLICY_ID, normalized_label)
    feeds = report.get("feeds") or source.get("existing_feeds") or []
    feed_slugs = sorted({item["slug"] for item in feeds})
    return {
        "city": city,
        "source_key": source_key,
        "policy_id": P3_POLICY_ID,
        "normalized_label": normalized_label,
        "decision_id": identity,
        "evidence_hash": report["decision"]["evidence_hash"],
        "config_hash": config_hash,
        "policy_hash": report["decision"]["policy_hash"],
        "recording_refs": refs,
        "expected_parent_event_ids": sorted(parents),
        "observed_at": collected_at,
        "feed_slugs": feed_slugs,
    }


def _action_template(binding: dict) -> dict:
    return {
        "schema_version": 1,
        "action": "decide",
        "decision_id": binding["decision_id"],
        "expected_parent_event_ids": binding["expected_parent_event_ids"],
        "city": binding["city"],
        "source_key": binding["source_key"],
        "policy_id": binding["policy_id"],
        "normalized_label": binding["normalized_label"],
        "state": "blocked",
        "disposition": "watch",
        "evidence_hash": binding["evidence_hash"],
        "config_hash": binding["config_hash"],
        "policy_hash": binding["policy_hash"],
        "recording_refs": binding["recording_refs"],
        "rationale": "Replace with the human review rationale.",
        "feed_dispositions": [],
    }


def _render_issue(report: dict, binding: dict, prior_body: str = "") -> str:
    key = binding["decision_id"]
    action = _action_template(binding)
    body = "\n".join(
        [
            P3_ISSUE_KEY.format(key),
            "## Human disposition required",
            "",
            f"Finding: `{report['decision']['finding_id']}`  ",
            f"Evidence SHA-256: `{binding['evidence_hash']}`  ",
            f"Policy SHA-256: `{binding['policy_hash']}`",
            "",
            report.get("manual_issue_candidate", "").strip(),
            "",
            "To record the selection, check an include/exclude option for every feed, or select an",
            "overall option, then comment `/body-disposition` followed by the structured JSON",
            "template below. For an assignment set `state=covered`, `disposition=assigned`, and",
            "include every feed choice. For no assignment use `state=excluded`,",
            "`disposition=not_pursued`, and exclude every feed. For a future-evidence hold use",
            "`state=blocked`, `disposition=watch`, and an empty `feed_dispositions` list.",
            "",
            "```text",
            "/body-disposition",
            json.dumps(action, indent=2, sort_keys=True),
            "```",
            "",
            "<!-- citypods:remedy-p3:binding="
            + json.dumps(binding, sort_keys=True, separators=(",", ":"))
            + " -->",
            render_markers([key]),
            render_binding(key, binding),
        ]
    )
    generated = f"{_GENERATED_START}\n{body}\n{_GENERATED_END}"
    if prior_body:
        # Carry selections only when the evidence binding is identical.
        old = _find_binding(prior_body)
        same_observation = old is not None and {
            key: value
            for key, value in old.items()
            if key not in {"observed_at", "expected_parent_event_ids"}
        } == {
            key: value
            for key, value in binding.items()
            if key not in {"observed_at", "expected_parent_event_ids"}
        }
        if same_observation:
            prior_section = _p3_generated_section(prior_body)
            for slug in binding["feed_slugs"]:
                pattern = rf"(?m)^- \[x\] (Include|Exclude) `{re.escape(slug)}`[ \t]*\r?$"
                matches = re.findall(pattern, prior_section)
                if len(matches) > 1:
                    raise ValueError(f"prior issue has conflicting choices for feed {slug}")
                if matches:
                    choice = matches[0]
                    unchecked = rf"(?m)^- \[ \] {choice} `{re.escape(slug)}`[ \t]*$"
                    generated = re.sub(unchecked, f"- [x] {choice} `{slug}`", generated, count=1)
            for option in ("No feed assignment", "Hold for future evidence"):
                if re.search(rf"(?m)^- \[x\] {re.escape(option)}[ \t]*\r?$", prior_section):
                    generated = re.sub(
                        rf"(?m)^- \[ \] {re.escape(option)}[ \t]*$",
                        f"- [x] {option}",
                        generated,
                        count=1,
                    )
        return _replace_generated(prior_body, generated)
    return generated


def _find_binding(body: str) -> dict | None:
    matches = re.findall(r"<!-- citypods:remedy-p3:binding=([^\n]+) -->", body or "")
    if len(matches) != 1:
        return None
    try:
        return json.loads(matches[0])
    except ValueError:
        return None


def _reconcile_issue(report: dict, binding: dict, *, dry_run: bool) -> str:
    key = binding["decision_id"]
    if not dry_run:
        _gh(
            "label",
            "create",
            P3_ISSUE_LABEL,
            "--color",
            "5319E7",
            "--description",
            "Unexpected-body P3 human disposition",
            "--force",
            check=False,
        )
    issues = json.loads(
        _gh(
            "issue",
            "list",
            "--label",
            P3_ISSUE_LABEL,
            "--state",
            "all",
            "--json",
            "number,title,body,state,url",
            "--limit",
            "1000",
        )
        or "[]"
    )
    existing = [
        issue
        for issue in issues
        if (_find_binding(issue.get("body") or "") or {}).get("decision_id") == key
    ]
    if len(existing) > 1:
        raise RuntimeError(f"duplicate reconciled P3 issues for {key}")
    title = f"[P3 disposition] {binding['normalized_label']} ({binding['city']})"
    prior = existing[0] if existing else None
    body = _render_issue(report, binding, prior.get("body", "") if prior else "")
    if len(body) > MAX_ISSUE_BODY_CHARS:
        return "deferred: generated issue exceeds the safe GitHub body limit"
    if prior is not None:
        prior_binding = _find_binding(prior.get("body", "")) or {}
        same_evidence = prior_binding.get("evidence_hash") == binding["evidence_hash"]
        if str(prior.get("state", "")).lower() != "open" and same_evidence:
            return str(prior.get("url") or f"issue #{prior['number']} (closed; unchanged evidence)")
        if prior.get("title") != title or prior.get("body", "").strip() != body.strip():
            if not dry_run:
                _gh("issue", "edit", str(prior["number"]), "--title", title, "--body", body)
        if str(prior.get("state", "")).lower() != "open" and not dry_run:
            _gh("issue", "reopen", str(prior["number"]))
        return str(prior.get("url") or f"issue #{prior['number']}")
    open_issues = [issue for issue in issues if str(issue.get("state", "")).lower() == "open"]
    if len(open_issues) >= MAX_OPEN_P3_ISSUES:
        return "deferred: P3 manual-issue cap reached"
    if dry_run:
        return "dry-run: would create reconciled P3 issue"
    url = _gh(
        "issue",
        "create",
        "--title",
        title,
        "--body",
        body,
        "--label",
        P3_ISSUE_LABEL,
        "--label",
        "type:operations",
    ).strip()
    return url


def _terminal_disposition_is_current(history, binding: dict) -> bool:
    if (
        history is None
        or history.diagnostics
        or history.state not in {"covered", "excluded", "resolved"}
        or len(history.tip_ids) != 1
    ):
        return False
    tip = next((event for event in history.events if event.event_id == history.tip_ids[0]), None)
    return tip is not None and tip.evidence_hash == binding["evidence_hash"]


def _proposal_digest(plans) -> str:
    assignments = sorted(
        f"{finding_id}:{canonical_hash([_assignment_payload(item) for item in plan.accepted])}"
        for plan, _paths, _context, finding_id, _evidence_hash in plans
    )
    return hashlib.sha256("\n".join(assignments).encode()).hexdigest()


def _assignment_payload(proposal) -> dict:
    return {
        "source_key": proposal.source_key,
        "unexpected_body": proposal.unexpected_body,
        "action": proposal.action,
        "target_feeds": sorted(proposal.target_feeds),
        "provider_guids": sorted(proposal.provider_guids),
        "body_selector": proposal.body_selector,
        "new_feed_slug": proposal.new_feed_slug,
        "new_feed_title": proposal.new_feed_title,
        "new_feed_description": proposal.new_feed_description,
        "lifecycle_status": proposal.lifecycle_status,
        "lifecycle_reason": proposal.lifecycle_reason,
    }


def _publish_pr(plans, *, repo_root: Path, digest: str) -> str:
    branch = f"fix/p3-remedy-{digest[:12]}"
    existing = _gh(
        "pr",
        "list",
        "--head",
        branch,
        "--state",
        "all",
        "--json",
        "url,state",
        check=False,
    )
    existing_prs = json.loads(existing or "[]")
    if len(existing_prs) > 1:
        raise RuntimeError(f"duplicate P3 proposal PRs for {branch}")
    if existing_prs:
        return existing_prs[0]["url"]
    current = subprocess.run(
        ["git", "branch", "--show-current"], cwd=repo_root, capture_output=True, text=True
    ).stdout.strip()
    if current != "main":
        raise RuntimeError("P3 publisher must start from main")
    subprocess.run(["git", "switch", "-c", branch], cwd=repo_root, check=True)
    for plan, paths, source_context, _finding_id, _evidence_hash in plans:
        apply_remedy_plan(
            plan, feed_paths=paths, source_context=source_context, repo_root=repo_root
        )
    ok, message = verify_remedy_mutations(repo_root)
    if not ok:
        raise RuntimeError(f"P3 mutation validation failed: {message}")
    subprocess.run(["git", "add", "config/feeds"], cwd=repo_root, check=True)
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo_root, check=False)
    if staged.returncode == 0:
        return "no feed changes required"
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=citypods-p3[bot]",
            "-c",
            "user.email=citypods-p3@users.noreply.github.com",
            "commit",
            "-m",
            "fix(config): propose validated P3 body assignments",
        ],
        cwd=repo_root,
        check=True,
    )
    subprocess.run(["gh", "auth", "setup-git"], cwd=repo_root, check=True)
    subprocess.run(["git", "push", "-u", "origin", branch], cwd=repo_root, check=True)
    detail = "\n".join(
        f"- `{finding_id}` — evidence `{evidence_hash}`"
        for _plan, _paths, _context, finding_id, evidence_hash in plans
    )
    body = (
        "Validated P3 proposal from route-bound consensus. Feed changes were generated through "
        "the existing remedy mutation API, revalidated against current evidence, and passed the "
        "repository config, Ruff, format, and offline test gates locally. GitHub Actions does not "
        "automatically run checks for PRs created with GITHUB_TOKEN; a maintainer must manually "
        "dispatch required checks against this PR branch and head before merge. Human review and "
        "merge are required."
        f"\n\nFindings:\n{detail}"
    )
    created = subprocess.run(
        [
            "gh",
            "pr",
            "create",
            "--base",
            "main",
            "--head",
            branch,
            "--title",
            "fix(config): propose validated P3 body assignments",
            "--body",
            body,
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return created.stdout.strip()


def publish(report_path: Path, evidence_path: Path, repo_root: Path, *, dry_run=False) -> dict:
    report_payload = _json(report_path)
    evidence_payload = _json(evidence_path)
    sources = evidence_payload.get("sources")
    if not isinstance(sources, list):
        raise ValueError("evidence file must contain sources")
    site = load_site_config(repo_root / "config/site_config.yml")
    config = RemedyConfig.model_validate(
        yaml.safe_load((repo_root / "config/remedy.yml").read_text(encoding="utf-8"))
    )
    if not p3_enabled(config):
        return {"status": "policy_hold", "published": []}
    collected_at = evidence_payload.get("collected_at")
    if not isinstance(collected_at, str):
        raise ValueError("evidence bundle is missing its collection timestamp; refresh evidence")
    storage = make_storage(site, site.get("base_url", ""), repo_root / "docs")
    if storage is None or not getattr(storage, "cas_capable", False):
        raise RuntimeError("P3 publication requires B2 storage and R2 CAS coordination")
    cities = {city.slug: city for city in load_city_configs(repo_root / "config", {})}
    plans = []
    issue_rows = []
    deferred = []
    candidates: list[Candidate] = []
    candidate_sources: set[str] = set()
    live = config.publication_mode == "live"
    pr_meta: dict[str, dict] = {}
    issue_meta: dict[str, object] = {}
    eligible_since, publication_plan_error = _eligibility(collected_at)
    findings_by_id = {}
    for source in sources:
        for finding in source.get("unexpected_findings", []):
            findings_by_id[stable_finding_id(source["source_key"], finding["unexpected_body"])] = (
                source,
                finding,
            )

    for row in report_payload.get("findings", []):
        p3_report = row.get("report")
        if not isinstance(p3_report, dict):
            continue
        decision = p3_report.get("decision") or {}
        source, finding = findings_by_id[decision["finding_id"]]
        evidence = {**source, "unexpected_findings": [finding]}
        if decision.get("disposition") == "pr_candidate":
            if not live and len(plans) >= MAX_PUBLISH_PER_RUN:
                deferred.append(
                    {"finding_id": decision["finding_id"], "reason": "per-run proposal cap"}
                )
                continue
            try:
                plan, paths = validated_p3_plan(
                    p3_report,
                    evidence,
                    repo_root=repo_root,
                    admitted_routes=admitted_p3_routes(config),
                )
            except ValueError as exc:
                deferred.append({"finding_id": decision["finding_id"], "reason": str(exc)})
                continue
            sibling = next(
                (
                    cities[feed["slug"]]
                    for feed in source.get("existing_feeds", [])
                    if feed["slug"] in cities
                ),
                None,
            )
            if sibling is None:
                raise ValueError("P3 proposal has no configured source sibling")
            plans.append(
                (
                    plan,
                    paths,
                    SourceContext.from_city(sibling),
                    decision["finding_id"],
                    decision["evidence_hash"],
                )
            )
            if eligible_since is not None:
                pr_binding = _binding_for(
                    source, finding, p3_report, config_fingerprint(config), [], collected_at
                )
                candidates.append(_pr_candidate(plan, pr_binding, eligible_since))
                candidate_sources.add(source["source_key"])
                pr_meta[pr_binding["decision_id"]] = {
                    "report": p3_report,
                    "binding": pr_binding,
                    "plan": plan,
                    "context": SourceContext.from_city(sibling),
                    "finding_id": decision["finding_id"],
                    "evidence_hash": decision["evidence_hash"],
                }
        elif decision.get("disposition") == "issue_candidate":
            if not live and len(issue_rows) >= MAX_PUBLISH_PER_RUN:
                deferred.append(
                    {"finding_id": decision["finding_id"], "reason": "per-run issue cap"}
                )
                continue
            config_hash = config_fingerprint(config)
            with acquire(storage, owner="remedy-p3-publish", key=REMEDY_LEASE_KEY) as lease:
                binding = _binding_for(source, finding, p3_report, config_hash, [], collected_at)
                states = load_decisions(storage, source_keys=[source["source_key"]])
                history = states.get(binding["decision_id"])
                if history is not None and history.diagnostics:
                    raise ValueError("P2 ledger history is damaged; refusing to publish issue")
                if _terminal_disposition_is_current(history, binding):
                    deferred.append(
                        {
                            "finding_id": decision["finding_id"],
                            "reason": "current terminal P2 disposition already exists",
                        }
                    )
                    continue
                binding["expected_parent_event_ids"] = list(history.tip_ids) if history else []
                # Refresh lease before using the just-read parent snapshot.
                lease.assert_held()
            issue_rows.append((p3_report, binding))
            issue_meta[binding["decision_id"]] = (p3_report, binding)
            if eligible_since is not None:
                candidates.append(_issue_candidate(binding, eligible_since))
                candidate_sources.add(source["source_key"])

    if live and not publication_plan_error:
        return _publish_live(
            config=config,
            candidates=candidates,
            pr_meta=pr_meta,
            issue_meta=issue_meta,
            storage=storage,
            repo_root=repo_root,
            dry_run=dry_run,
            deferred=deferred,
            source_keys=candidate_sources | {source["source_key"] for source in sources},
        )
    published = []
    if plans:
        digest = _proposal_digest(plans)
        if dry_run:
            published.append({"type": "proposal_pr", "url": "dry-run", "count": len(plans)})
        else:
            published.append(
                {
                    "type": "proposal_pr",
                    "url": _publish_pr(plans, repo_root=repo_root, digest=digest),
                }
            )
    for p3_report, binding in issue_rows:
        url = _reconcile_issue(p3_report, binding, dry_run=dry_run)
        published.append({"type": "manual_issue", "url": url})
    status = "published" if published else "deferred" if deferred else "no_candidates"
    return {
        "status": status,
        "published": published,
        "deferred": deferred,
        "publication_plan": (
            {"mode": "report_only", "error": publication_plan_error}
            if publication_plan_error
            else _publication_report(candidates, candidate_sources, storage, config.limits)
        ),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--evidence-file", required=True, type=Path)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    result = publish(
        args.report,
        args.evidence_file,
        args.repo_root.resolve(),
        dry_run=args.dry_run,
    )
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
