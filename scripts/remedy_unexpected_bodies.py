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
findings is idempotent and a re-run after new findings gets its own branch. When ``--issue`` and
``--apply`` are both given, every terminal outcome (opened/reused PR, nothing to change, or
verification failure) posts exactly one comment on that issue with the full classification --
accepted proposals, rejected ones and why, and a link to the PR when one exists.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from citypods.audit_remedy import (
    SourceContext,
    _markdown_table_cell,
    apply_remedy_plan,
    classify_unexpected_bodies,
    feed_paths_by_slug,
    format_remedy_markdown,
    remedy_batches,
    safe_classification_error,
    validate_proposals,
    verify_remedy_mutations,
)
from citypods.config import load_city_configs, load_site_config
from citypods.storage import make_storage


def _issue_number(value: str) -> str:
    """Accept a decimal GitHub issue number, never a flag or branch-path fragment."""
    if not value.isascii() or not value.isdecimal():
        raise argparse.ArgumentTypeError(f"--issue must be a number, got {value!r}")
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence-file",
        required=True,
        help="Evidence JSON written by `audit_feeds.py --unexpected-body-evidence`",
    )
    parser.add_argument(
        "--issue",
        type=_issue_number,
        help="GitHub issue number to comment on / resolve",
    )
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
    if not bundles or not any(b.get("unexpected_findings") for b in bundles):
        report = "The refreshed audit found no unexpected labels to classify."
        _write_report(args.output, [report])
        _post_final_comment(
            args.issue,
            report_md=report,
            pr_url=None,
            verification_failed=False,
            accepted_total=0,
            cwd=repo_root,
        )
        return 0

    site_config = load_site_config(repo_root / "config" / "site_config.yml")
    output_dir = repo_root / site_config.get("output_dir", "docs")
    storage = make_storage(site_config, site_config.get("base_url", ""), output_dir)

    cities = load_city_configs(repo_root / "config", {})
    cities_by_slug = {city.slug: city for city in cities}
    feed_paths = feed_paths_by_slug(repo_root)

    # Leave ten minutes for verification/PR/reporting inside the workflow's outer timeout.
    deadline = datetime.now(UTC) + timedelta(minutes=30)
    if os.environ.get("REMEDY_DEADLINE_EPOCH"):
        deadline = min(
            deadline,
            datetime.fromtimestamp(float(os.environ["REMEDY_DEADLINE_EPOCH"]), UTC)
            - timedelta(minutes=10),
        )
    reports: list[str] = []
    modified: list[Path] = []
    accepted_total = rejected_total = 0

    failed_total = unresolved_total = 0
    for source in bundles:
        for batch_number, bundle in enumerate(remedy_batches(source), 1):
            source_key = bundle.get("source_key", "")
            city = bundle.get("city", {}).get("slug", source_key)
            labels = [f["unexpected_body"] for f in bundle.get("unexpected_findings", [])]
            _log(f"Classifying {city} batch {batch_number} ({len(labels)} labels; direct only)…")
            try:
                if datetime.now(UTC) >= deadline:
                    raise TimeoutError("Run classification deadline reached")
                remedy = classify_unexpected_bodies(bundle, storage=storage, deadline_at=deadline)
            except Exception as exc:
                failed_total += len(labels)
                reason = safe_classification_error(exc)
                _log(f"  classification failed for {source_key}: {reason}")
                reports.append(
                    f"#### `{city}` — batch {batch_number}\n\n"
                    f"> Classification failed: {reason}.\n\n"
                    + "\n".join(f"- {_markdown_table_cell(label)}" for label in labels)
                )
                _write_report(args.output, reports)
                continue

            plan = validate_proposals(remedy, bundle, feed_paths)
            accepted_total += len(plan.accepted)
            rejected_total += len(plan.rejected)
            unresolved_total += len(remedy.unresolved)
            reports.append(
                f"Direct model: `{remedy.model}`; response cache disabled.\n\n"
                + format_remedy_markdown(plan, bundle)
            )
            if remedy.unresolved:
                reports.append(
                    "Manual review required:\n\n"
                    + "\n".join(
                        f"- {_markdown_table_cell(label)}: {_markdown_table_cell(reason)}"
                        for label, reason in remedy.unresolved.items()
                    )
                )
            _write_report(args.output, reports)

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
                    failed_total += len(plan.accepted)
                    reports.append(f"No configured sibling feed for `{city}`; could not apply.")
                    _write_report(args.output, reports)
                    continue
                modified += apply_remedy_plan(
                    plan,
                    feed_paths=feed_paths,
                    source_context=SourceContext.from_city(sibling),
                    repo_root=repo_root,
                )
                # New slugs become reserved immediately, preventing collisions across batches.
                feed_paths = feed_paths_by_slug(repo_root)

    report_md = "\n\n".join(reports)
    print(report_md)
    if args.output:
        Path(args.output).write_text(report_md + "\n", encoding="utf-8")
    _log(f"\n{accepted_total} proposal(s) accepted, {rejected_total} rejected.")

    report_md = (
        f"{accepted_total} accepted; {rejected_total} rejected; "
        f"{unresolved_total} need manual review; {failed_total} classification/apply failures.\n\n"
        + report_md
    )
    _write_report(args.output, [report_md])
    if not args.apply:
        _log("Report-only mode: no files were modified. Re-run with --apply to edit feeds.")
        _post_final_comment(
            args.issue,
            report_md=report_md,
            pr_url=None,
            verification_failed=False,
            accepted_total=accepted_total,
            failed_total=failed_total,
            cwd=repo_root,
        )
        return int(bool(failed_total))
    if not modified:
        _log("No files needed modification.")
        _post_final_comment(
            args.issue,
            report_md=report_md,
            pr_url=None,
            verification_failed=False,
            accepted_total=accepted_total,
            failed_total=failed_total,
            cwd=repo_root,
        )
        return int(bool(failed_total))

    _log(f"Modified {len(modified)} file(s); verifying…")
    ok, message = verify_remedy_mutations(repo_root=repo_root)
    _log(f"Verification: {message}")
    report_md += f"\n\nVerification: {message}"
    _write_report(args.output, [report_md])

    if not ok:
        _log("Verification failed; reverting the working tree.")
        _run(["git", "checkout", "--", "config/feeds"], cwd=repo_root, check=False)
        _run(["git", "clean", "-fd", "config/feeds"], cwd=repo_root, check=False)
        _post_final_comment(
            args.issue,
            report_md=report_md,
            pr_url=None,
            verification_failed=True,
            accepted_total=accepted_total,
            failed_total=failed_total,
            cwd=repo_root,
        )
        return 1

    pr_url = _open_pull_request(args.issue, evidence_path, repo_root) if args.issue else None
    _post_final_comment(
        args.issue,
        report_md=report_md,
        pr_url=pr_url,
        verification_failed=False,
        accepted_total=accepted_total,
        failed_total=failed_total,
        cwd=repo_root,
    )
    if not args.issue:
        _log("Changes applied and verified. Pass --issue N to open a pull request.")
    return int(bool(failed_total))


def _write_report(output: str, reports: list[str]) -> None:
    if output:
        Path(output).write_text("\n\n".join(reports) + "\n", encoding="utf-8")


def _post_final_comment(
    issue: str | None,
    *,
    report_md: str,
    pr_url: str | None,
    verification_failed: bool,
    accepted_total: int,
    failed_total: int = 0,
    cwd: Path,
) -> None:
    """Post exactly one comment summarizing this run's outcome -- the report, plus why."""
    if not issue or not os.environ.get("GH_TOKEN"):
        return
    if verification_failed:
        header = (
            "### Automated remedy proposal (verification failed)\n\n"
            "Applying these changes failed Ruff lint/format or the test suite, so nothing was "
            "kept. Manual review required."
        )
    elif pr_url:
        header = (
            "### Automated remedy proposal\n\n"
            "Classified from this issue's own audit evidence and re-validated against it before "
            f"being applied; anything unverifiable was rejected and is listed below.\n\n{pr_url}"
        )
    elif failed_total and not accepted_total:
        header = (
            "### Automated remedy failed\n\n"
            "Classification did not produce applicable changes. The failures below are separate "
            "from proposal rejections; no PR was opened."
        )
    elif accepted_total:
        header = (
            "### Automated remedy proposal\n\n"
            "Proposals were accepted (see below) but none produced a file change -- already "
            "applied by an earlier run, or no configured sibling feed exists for that source --"
            " so no pull request was opened."
        )
    else:
        header = (
            "### Automated remedy report\n\n"
            "No applicable file changes were produced. See the outcomes below."
        )
    run_url = (
        f"https://github.com/{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/"
        f"{os.environ.get('GITHUB_RUN_ID', '')}"
    )
    # GitHub issue comments are bounded; the artifact always carries every label and rationale.
    body = f"{header}\n\n{report_md[:45000]}\n\n[Full report and evidence]({run_url}).\n"
    body_path = cwd / "remedy-comment.md"
    body_path.write_text(body, encoding="utf-8")
    _run(["gh", "issue", "comment", str(issue), "--body-file", str(body_path)], cwd=cwd)
    (cwd / "remedy-comment-posted").write_text("posted\n", encoding="utf-8")


def _existing_pr_url(branch: str, *, cwd: Path) -> str | None:
    result = _run(
        [
            "gh",
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            "url",
            "--jq",
            ".[0].url",
        ],
        cwd=cwd,
        check=False,
    )
    return result.stdout.strip() or None


def _checkout_remedy_branch(branch: str, repo_root: Path) -> None:
    """Check out a prior digest branch when it exists remotely, otherwise create it."""
    # Checkout runs with `persist-credentials: false` (repo-wide policy, so a compromised
    # package script cannot read a token out of the git config during `pip install -e .`). Wire
    # credentials in only now. A fresh runner does not have branches created by earlier runs, so
    # fetch the digest-named branch before deciding whether to create it.
    _run(["gh", "auth", "setup-git"], cwd=repo_root, check=False)
    fetched = _run(["git", "fetch", "origin", f"{branch}:{branch}"], cwd=repo_root, check=False)
    existing = _run(["git", "rev-parse", "--verify", branch], cwd=repo_root, check=False)
    if fetched.returncode == 0 or existing.returncode == 0:
        _run(["git", "checkout", branch], cwd=repo_root)
    else:
        _run(["git", "checkout", "-b", branch], cwd=repo_root)


def _open_pull_request(issue: str, evidence_path: Path, repo_root: Path) -> str | None:
    """Push the applied changes and open (or reuse) a PR. Returns its URL, or None."""
    if not os.environ.get("GH_TOKEN"):
        _log("GH_TOKEN not set; leaving changes in the working tree.")
        return None

    # Digest-suffixed so a re-run over the same findings reuses one branch instead of colliding,
    # and a run over *new* findings never force-pushes over an open PR's history.
    digest = json.loads(evidence_path.read_text(encoding="utf-8")).get("digest", "manual")[:12]
    branch = f"fix/{issue}-unexpected-feed-bodies-{digest}"

    _checkout_remedy_branch(branch, repo_root)

    _run(["git", "add", "config/feeds"], cwd=repo_root)
    staged = _run(["git", "diff", "--cached", "--quiet"], cwd=repo_root, check=False)
    if staged.returncode == 0:
        # Same content digest as a prior run: nothing new to commit. If that prior run already
        # has an open PR, surface it rather than silently doing nothing -- a repeat trigger for
        # the same issue (e.g. a manual re-run) should still get a comment pointing somewhere.
        _log("Nothing staged for this content digest.")
        return _existing_pr_url(branch, cwd=repo_root)

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

    # A branch can already have an open PR from an earlier run over this exact digest (e.g. the
    # push above was a no-op fast-forward of an existing remote branch) -- `gh pr create` errors
    # on a duplicate, so check first rather than treating that as a new PR to open.
    reused = _existing_pr_url(branch, cwd=repo_root)
    if reused:
        _log(f"Reusing existing pull request {reused}.")
        return reused

    body = (
        f"Related to #{issue}; the audit closes it after observing no unmatched rows.\n\n"
        f"Proposals were generated from the audit's own `unexpected-body` rows and re-validated "
        f"against that evidence before being applied; anything unverifiable was rejected."
    )
    report = repo_root / "remedy-report.md"
    if report.exists():
        body += "\n\n" + report.read_text(encoding="utf-8")[:40000]
    body_path = repo_root / "remedy-pr.md"
    body_path.write_text(body, encoding="utf-8")
    created = _run(
        [
            "gh",
            "pr",
            "create",
            "--title",
            f"fix(config): resolve unexpected feed bodies (#{issue})",
            "--body-file",
            str(body_path),
        ],
        cwd=repo_root,
    )
    pr_url = created.stdout.strip() or None
    if pr_url:
        _log(f"Opened a pull request: {pr_url}")
    return pr_url


if __name__ == "__main__":
    raise SystemExit(main())
