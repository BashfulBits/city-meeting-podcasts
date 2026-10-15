#!/usr/bin/env python3
"""Publish bounded P3 consensus candidates as review PRs or reconciled disposition issues."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

import yaml

from citypods.audit_remedy import (
    SourceContext,
    apply_remedy_plan,
    verify_remedy_mutations,
)
from citypods.bodies import body_key
from citypods.config import load_city_configs, load_site_config
from citypods.ops.maintenance_leases import acquire
from citypods.remedy_actions import _p3_generated_section
from citypods.remedy_evaluation import RemedyConfig
from citypods.remedy_ledger import REMEDY_LEASE_KEY, decision_id, load_decisions
from citypods.remedy_p3_publication import validated_p3_plan
from citypods.remedy_p3_runner import admitted_p3_routes, p3_enabled, stable_finding_id
from citypods.remedy_policy import canonical_hash
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


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


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
    key = canonical_hash([binding["decision_id"], binding["evidence_hash"]])
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
    key = canonical_hash([binding["decision_id"], binding["evidence_hash"]])
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
    existing = [issue for issue in issues if P3_ISSUE_KEY.format(key) in (issue.get("body") or "")]
    if len(existing) > 1:
        raise RuntimeError(f"duplicate reconciled P3 issues for {key}")
    title = f"[P3 disposition] {binding['normalized_label']} ({binding['city']})"
    prior = existing[0] if existing else None
    body = _render_issue(report, binding, prior.get("body", "") if prior else "")
    if len(body) > MAX_ISSUE_BODY_CHARS:
        return "deferred: generated issue exceeds the safe GitHub body limit"
    if prior is not None:
        if str(prior.get("state", "")).lower() != "open":
            return str(prior.get("url") or f"issue #{prior['number']} (closed; unchanged evidence)")
        if prior.get("title") != title or prior.get("body", "").strip() != body.strip():
            if not dry_run:
                _gh("issue", "edit", str(prior["number"]), "--title", title, "--body", body)
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
        "repository config, Ruff, format, and offline test gates. Human review and merge required."
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
            if len(plans) >= MAX_PUBLISH_PER_RUN:
                deferred.append(
                    {"finding_id": decision["finding_id"], "reason": "per-run proposal cap"}
                )
                continue
            plan, paths = validated_p3_plan(
                p3_report, evidence, repo_root=repo_root, admitted_routes=admitted_p3_routes(config)
            )
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
        elif decision.get("disposition") == "issue_candidate":
            if len(issue_rows) >= MAX_PUBLISH_PER_RUN:
                deferred.append(
                    {"finding_id": decision["finding_id"], "reason": "per-run issue cap"}
                )
                continue
            config_hash = canonical_hash(config.model_dump(mode="json"))
            with acquire(storage, owner="remedy-p3-publish", key=REMEDY_LEASE_KEY) as lease:
                binding = _binding_for(source, finding, p3_report, config_hash, [], collected_at)
                states = load_decisions(storage, source_keys=[source["source_key"]])
                history = states.get(binding["decision_id"])
                if history is not None and history.diagnostics:
                    raise ValueError("P2 ledger history is damaged; refusing to publish issue")
                binding["expected_parent_event_ids"] = list(history.tip_ids) if history else []
                # Refresh lease before using the just-read parent snapshot.
                lease.assert_held()
            issue_rows.append((p3_report, binding))

    published = []
    if plans:
        digest = hashlib.sha256(
            "\n".join(
                sorted(f"{finding_id}:{evidence_hash}" for *_, finding_id, evidence_hash in plans)
            ).encode()
        ).hexdigest()
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
    return {"status": status, "published": published, "deferred": deferred}


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
