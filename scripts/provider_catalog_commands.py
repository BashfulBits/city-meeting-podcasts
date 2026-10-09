#!/usr/bin/env python3
"""Authorized /apply on the rolling catalog issue; prepares a reviewed additions/ignore PR."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from citypods.compute.llm_dispatch_pause import DispatchPauseError  # noqa: E402
from citypods.compute.llm_lanes import load_lanes, parse_lanes  # noqa: E402
from citypods.github_permissions import (  # noqa: E402
    RepositoryPermissionError,
    require_repository_write,
)
from citypods.provider_catalog.apply import (  # noqa: E402
    BRANCH,
    COMPILED_PATHS,
    PR_MARKER,
    SOURCE_PATHS,
    ApplyConfig,
    paid_fulfilled,
    parse_decisions,
    plan_apply,
    proposal_body,
)
from citypods.provider_catalog.config_edit import apply_config_edits, load_config  # noqa: E402
from citypods.provider_catalog.decisions import load_decisions  # noqa: E402
from citypods.provider_catalog.issue import decode_state, find_issue, is_catalog_issue  # noqa: E402
from citypods.provider_catalog.quality import QualityIndex, fetch_quality_index  # noqa: E402
from citypods.provider_catalog.reconcile import (  # noqa: E402
    NoDispatchControl,
    Report,
    _merge_into_last_full,
    reconcile,
)
from scripts.reconcile_provider_routes import _control  # noqa: E402


def run(args):
    return subprocess.run(args, cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()


def process_event(event, permission):
    issue = event.get("issue") or {}
    number = issue.get("number")
    result = {"accepted": False, "issue_number": number, "comment": "Rejected /apply command."}
    try:
        require_repository_write(permission)
    except RepositoryPermissionError as exc:
        return {**result, "comment": str(exc)}
    if (
        event.get("action") != "created"
        or (event.get("comment") or {}).get("body") != "/apply"
        or issue.get("pull_request") is not None
        or isinstance(number, bool)
        or not isinstance(number, int)
        or number <= 0
        or not is_catalog_issue(issue.get("body") or "")
    ):
        return result
    return {**result, "accepted": True, "comment": "Checking current catalog selections."}


def prepare(body, base_commit):
    state = decode_state(body)
    snapshot = Report()
    _merge_into_last_full(snapshot, state.get("last_full") or {}, set())
    decisions = parse_decisions(body, snapshot)
    texts = {p: (ROOT / p).read_text() for p in SOURCE_PATHS}
    limits = load_config(texts[SOURCE_PATHS[0]])
    lanes = load_lanes()
    config = ApplyConfig(limits, lanes, texts, base_commit, datetime.now(UTC).date())
    selected = {d.key for d in decisions if d.action == "add"}
    # Ignores spend no provider quota. Already-configured selections require no new-route proof.
    configured = {f"{r['provider']}/{r['upstream_model']}" for r in limits.get("routes") or []}
    selected -= configured
    paid = {
        d.route_id for d in decisions if d.action == "keep_paid" and not paid_fulfilled(d, config)
    }
    report = Report()
    if selected or paid:
        control = _control(False)
        if isinstance(control, NoDispatchControl):
            raise ValueError("route changes require a configured exclusive dispatch pause")
        session = requests.Session()
        report = reconcile(
            limits,
            lanes,
            load_decisions(),
            fetch_quality_index(session),
            {},
            session=session,
            control=control,
            today=config.today,
            providers={d.provider for d in decisions if d.key in selected or d.route_id in paid},
            candidate_keys=selected,
            route_ids=paid,
        )
    plan = plan_apply(report, decisions, config)
    output = apply_config_edits(texts, plan)
    for path in SOURCE_PATHS:
        (ROOT / path).write_text(output[path])
    return plan


def prepare_retirements(base_commit):
    """Dormant automatic-removal preparation; activation is a separate reviewed change."""
    from citypods.provider_catalog.retire import RETIREMENTS_ENABLED, plan_retirements

    if not RETIREMENTS_ENABLED:
        raise ValueError("removal publication awaits reviewed recovery-canary activation")
    texts = {p: (ROOT / p).read_text() for p in SOURCE_PATHS}
    limits = load_config(texts[SOURCE_PATHS[0]])
    # Rebuilds must use this main snapshot, rather than the process-level lane cache.
    lanes = parse_lanes(load_config(texts[SOURCE_PATHS[1]])["llm_lanes"])
    config = ApplyConfig(limits, lanes, texts, base_commit, datetime.now(UTC).date())
    control = _control(False)
    if isinstance(control, NoDispatchControl):
        raise ValueError("removals require a configured exclusive dispatch pause")
    route_ids = {r["route_id"] for r in limits.get("routes") or []}
    report = reconcile(
        limits,
        lanes,
        load_decisions(),
        QualityIndex(),
        {},
        session=requests.Session(),
        control=control,
        today=config.today,
        candidate_keys=set(),
        route_ids=route_ids,
    )
    plan = plan_retirements(report, config)
    output = apply_config_edits(texts, plan)
    for path in SOURCE_PATHS:
        (ROOT / path).write_text(output[path])
    return plan


def validate(run_fn=run):
    run_fn([sys.executable, "scripts/compile_llm_limits.py"])
    run_fn([sys.executable, "scripts/compile_llm_lanes.py"])
    run_fn(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_llm_lanes.py",
            "tests/test_compile_llm_limits.py",
            "tests/test_provider_catalog_contract.py",
            "tests/test_provider_catalog_reconcile.py",
            "tests/test_provider_catalog_apply.py",
            "tests/test_provider_catalog_config_edit.py",
            "tests/test_provider_catalog_evidence.py",
            "tests/test_provider_catalog_retire.py",
        ]
    )


def publish(plan, *, run_fn=run):
    """Bounded managed-branch publication. Return None on changed main for a full rebuild."""
    branch, marker = BRANCH, PR_MARKER
    title = "Provider catalog: selected additions, ignores and paid decisions"
    if plan.proposal_kind == "removals":
        from citypods.provider_catalog.retire import BRANCH as removal_branch
        from citypods.provider_catalog.retire import PR_MARKER as removal_marker
        from citypods.provider_catalog.retire import RETIREMENTS_ENABLED

        if not RETIREMENTS_ENABLED:
            raise ValueError("removal publication awaits reviewed recovery-canary activation")
        branch, marker = removal_branch, removal_marker
        title = "Provider catalog: proven retirements and lane repair"
    elif plan.proposal_kind != "additions":
        raise ValueError("unsupported proposal kind")
    run_fn(["git", "fetch", "origin", "main"])
    if run_fn(["git", "rev-parse", "origin/main"]) != plan.base_commit:
        return None
    prs = json.loads(
        run_fn(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "open",
                "--head",
                branch,
                "--json",
                "number,body,author,baseRefName,isCrossRepository",
            ]
        )
    )
    if len(prs) > 1 or any(
        marker not in p["body"]
        or p["author"]["login"] not in {"github-actions[bot]", "app/github-actions"}
        or p["baseRefName"] != "main"
        or p["isCrossRepository"]
        for p in prs
    ):
        raise ValueError("automation branch has a human-owned/unmanaged PR")
    remote = run_fn(["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"])
    expected = remote.split()[0] if remote else ""
    if expected:
        run_fn(["git", "fetch", "origin", f"refs/heads/{branch}"])
        email = run_fn(["git", "show", "-s", "--format=%ae", "FETCH_HEAD"])
        message = run_fn(["git", "show", "-s", "--format=%B", "FETCH_HEAD"])
        if (
            email != "41898282+github-actions[bot]@users.noreply.github.com"
            or marker not in message
        ):
            raise ValueError("refusing to replace a human-owned automation branch")
    run_fn(["git", "add", "--", *SOURCE_PATHS, *COMPILED_PATHS])
    if not run_fn(["git", "diff", "--cached", "--name-only"]):
        return "Selections already present on main; no PR needed."
    run_fn(["git", "config", "user.name", "github-actions[bot]"])
    run_fn(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"])
    run_fn(
        [
            "git",
            "commit",
            "-m",
            f"{title}\n\n{marker}",
        ]
    )
    # Recheck immediately before pushing, including the long compiler/test period.
    run_fn(["git", "fetch", "origin", "main"])
    if run_fn(["git", "rev-parse", "origin/main"]) != plan.base_commit:
        return None
    run_fn(
        [
            "git",
            "push",
            f"--force-with-lease=refs/heads/{branch}:{expected}",
            "origin",
            f"HEAD:refs/heads/{branch}",
        ]
    )
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "body.md"
        path.write_text(proposal_body(plan))
        if prs:
            run_fn(
                [
                    "gh",
                    "pr",
                    "edit",
                    str(prs[0]["number"]),
                    "--title",
                    title,
                    "--body-file",
                    str(path),
                ]
            )
            if plan.primary_changes:
                run_fn(
                    [
                        "gh",
                        "pr",
                        "edit",
                        str(prs[0]["number"]),
                        "--add-label",
                        "needs:human-verification",
                    ]
                )
            return run_fn(
                ["gh", "pr", "view", str(prs[0]["number"]), "--json", "url", "--jq", ".url"]
            )
        return run_fn(
            [
                "gh",
                "pr",
                "create",
                "--base",
                "main",
                "--head",
                branch,
                "--title",
                title,
                "--body-file",
                str(path),
                *(["--label", "needs:human-verification"] if plan.primary_changes else []),
            ]
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", required=True)
    parser.add_argument("--permission", required=True)
    parser.add_argument("--out", required=True)
    # All publication operations are confined to the repository's own trusted Actions runner.
    args = parser.parse_args(argv)
    event = json.loads(Path(args.event).read_text())
    permission = json.loads(Path(args.permission).read_text())
    result = process_event(event, permission)
    try:
        if result["accepted"] and os.environ.get("GITHUB_ACTIONS") != "true":
            raise ValueError("publication only runs in an ephemeral GitHub Actions checkout")
        if result["accepted"]:
            number = result["issue_number"]
            current = json.loads(
                run(["gh", "api", f"repos/{os.environ['GITHUB_REPOSITORY']}/issues/{number}"])
            )
            rolling = find_issue()
            if (
                not rolling
                or rolling["number"] != number
                or current.get("pull_request")
                or current.get("state") != "open"
                or not is_catalog_issue(current.get("body") or "")
            ):
                raise ValueError("/apply only runs on the current open rolling catalog issue")
            body = current["body"]
            for _ in range(3):
                run(["git", "fetch", "origin", "main"])
                base = run(["git", "rev-parse", "origin/main"])
                # Ephemeral trusted Actions checkout only; local publication is rejected.
                run(["git", "reset", "--hard", base])
                plan = prepare(body, base)
                validate()
                current = json.loads(
                    run(["gh", "api", f"repos/{os.environ['GITHUB_REPOSITORY']}/issues/{number}"])
                )
                if current["body"] != body:
                    raise ValueError("issue changed during verification; retry /apply")
                url = publish(plan)
                if url is not None:
                    result["comment"] = url + "\n\n" + proposal_body(plan)
                    result["plan"] = asdict(plan)
                    break
            else:
                raise ValueError("main changed repeatedly; retry /apply")
    except (
        ValueError,
        TypeError,
        KeyError,
        subprocess.CalledProcessError,
        DispatchPauseError,
    ) as exc:
        # Never include subprocess stdout/stderr (could contain provider secrets).
        result.update(
            accepted=False,
            comment=f"/apply deferred: {type(exc).__name__}: "
            + (
                str(exc)
                if not isinstance(exc, (subprocess.CalledProcessError, DispatchPauseError))
                else "verification or publication command failed"
            ),
        )
    Path(args.out).write_text(json.dumps(result, indent=2) + "\n")
    return 0 if result["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
