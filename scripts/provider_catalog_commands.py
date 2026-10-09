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
from citypods.compute.llm_lanes import parse_lanes  # noqa: E402
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
from citypods.provider_catalog.evidence import (  # noqa: E402
    discover_rate_references,
    verified_rate_history,
)
from citypods.provider_catalog.issue import decode_state, find_issue, is_catalog_issue  # noqa: E402
from citypods.provider_catalog.limits import configured_limit_changes, rate_edit_plan  # noqa: E402
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


def prepare(body, base_commit, proposal_kind=None):
    state = decode_state(body)
    snapshot = Report()
    _merge_into_last_full(snapshot, state.get("last_full") or {}, set())
    decisions = parse_decisions(body, snapshot)
    rate_selections = tuple(d.model for d in decisions if d.action == "increase_rate")
    if proposal_kind == "limits" or (
        proposal_kind is None and all(d.action == "increase_rate" for d in decisions)
    ):
        return prepare_limits(base_commit, selected=rate_selections)
    decisions = tuple(d for d in decisions if d.action != "increase_rate")
    texts = {p: (ROOT / p).read_text() for p in SOURCE_PATHS}
    limits = load_config(texts[SOURCE_PATHS[0]])
    lanes = parse_lanes(load_config(texts[SOURCE_PATHS[1]])["llm_lanes"])
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


def prepare_limits(base_commit, *, selected=(), automatic=False):
    """Rebuild rate changes entirely from main and authenticated scheduled artifacts."""
    texts = {p: (ROOT / p).read_text() for p in SOURCE_PATHS}
    limits = load_config(texts[SOURCE_PATHS[0]])
    now = datetime.now(UTC)
    config = ApplyConfig(
        limits,
        parse_lanes(load_config(texts[SOURCE_PATHS[1]])["llm_lanes"]),
        texts,
        base_commit,
        now.date(),
    )
    references = discover_rate_references(repository=os.environ["GITHUB_REPOSITORY"], now=now)
    observations, accepted, deferred = verified_rate_history(
        references,
        limits,
        repository=os.environ["GITHUB_REPOSITORY"],
        now=now,
    )
    if deferred:
        raise ValueError("rate history unavailable; no scalar changes prepared")
    recent = tuple(
        r["run_id"]
        for r in sorted(accepted, key=lambda r: (r["observed_at"], r["run_id"]), reverse=True)
    )
    changes, deferred = configured_limit_changes(observations, limits, now=now, recent_runs=recent)
    plan = rate_edit_plan(
        changes, config, selected=selected, automatic=automatic, deferred=deferred
    )
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
            "tests/test_provider_catalog_limits.py",
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
    elif plan.proposal_kind == "limits":
        branch, marker = (
            "automation/provider-catalog-limits",
            "<!-- citypods:provider-catalog-limits -->",
        )
        title = "Provider catalog: verified rate maintenance"
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
    if (
        plan.proposal_kind == "limits"
        and prs
        and (
            "Source: explicit `/apply` rate increase" in prs[0]["body"]
            and not any(c.action == "offer_increase" for c in plan.rate_changes)
        )
    ):
        raise ValueError("automatic tightening waits for the open reviewed-increase PR")
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
        if (
            plan.proposal_kind == "limits"
            and "Source: explicit `/apply` rate increase" in message
            and not any(c.action == "offer_increase" for c in plan.rate_changes)
        ):
            raise ValueError("automatic tightening waits for the reviewed-increase branch")
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
            f"{title}\n\n{marker}"
            + (
                "\nSource: explicit `/apply` rate increase"
                if any(c.action == "offer_increase" for c in plan.rate_changes)
                else ""
            ),
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
    parser.add_argument("--permission")
    parser.add_argument("--automatic-limits", action="store_true")
    parser.add_argument("--out", required=True)
    # All publication operations are confined to the repository's own trusted Actions runner.
    args = parser.parse_args(argv)
    event = json.loads(Path(args.event).read_text())
    if args.automatic_limits:
        source = event.get("workflow_run") or {}
        result = {
            "accepted": bool(
                event.get("action") == "completed"
                and source.get("conclusion") == "success"
                and source.get("event") == "schedule"
                and source.get("head_branch") == "main"
                and source.get("name") == "Provider catalog reconciliation"
                and (source.get("head_repository") or {}).get("full_name")
                == os.environ.get("GITHUB_REPOSITORY")
            ),
            "comment": "Preparing verified automatic rate tightening.",
        }
    else:
        if not args.permission:
            parser.error("/apply requires --permission")
        permission = json.loads(Path(args.permission).read_text())
        result = process_event(event, permission)
    try:
        if result["accepted"] and os.environ.get("GITHUB_ACTIONS") != "true":
            raise ValueError("publication only runs in an ephemeral GitHub Actions checkout")
        if result["accepted"] and args.automatic_limits:
            for _ in range(3):
                run(["git", "fetch", "origin", "main"])
                base = run(["git", "rev-parse", "origin/main"])
                run(["git", "reset", "--hard", base])
                plan = prepare_limits(base, automatic=True)
                if not plan.rate_changes:
                    result["comment"] = "No proven automatic tightening is available."
                    break
                validate()
                url = publish(plan)
                if url is not None:
                    result["comment"] = url + "\n\n" + proposal_body(plan)
                    result["plan"] = asdict(plan)
                    break
            else:
                raise ValueError("main changed repeatedly; defer rate maintenance")
        elif result["accepted"]:
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
            snapshot = Report()
            _merge_into_last_full(snapshot, decode_state(body).get("last_full") or {}, set())
            decisions = parse_decisions(body, snapshot)
            kinds = []
            if any(d.action != "increase_rate" for d in decisions):
                kinds.append("additions")
            if any(d.action == "increase_rate" for d in decisions):
                kinds.append("limits")
            comments, plans = [], []
            for kind in kinds:
                for _ in range(3):
                    run(["git", "fetch", "origin", "main"])
                    base = run(["git", "rev-parse", "origin/main"])
                    # Each proposal rebuilds from main; policy and rates use separate branches.
                    run(["git", "reset", "--hard", base])
                    plan = prepare(body, base, proposal_kind=kind)
                    validate()
                    current = json.loads(
                        run(
                            [
                                "gh",
                                "api",
                                f"repos/{os.environ['GITHUB_REPOSITORY']}/issues/{number}",
                            ]
                        )
                    )
                    if current["body"] != body:
                        raise ValueError("issue changed during verification; retry /apply")
                    url = publish(plan)
                    if url is not None:
                        comments.append(url + "\n\n" + proposal_body(plan))
                        plans.append(asdict(plan))
                        break
                else:
                    raise ValueError("main changed repeatedly; retry /apply")
            result["comment"] = "\n\n".join(comments)
            result["plans"] = plans
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
