"""Reconcile LLM provider catalogs against the configured free routes (review/48 Slice 1).

Observe and propose only: this never edits config. By default it prints the would-be issue body.

    python scripts/reconcile_provider_routes.py                    # dry run, all providers
    python scripts/reconcile_provider_routes.py --provider nvidia  # one provider
    python scripts/reconcile_provider_routes.py --sync-issues      # update the rolling issue
    python scripts/reconcile_provider_routes.py --due-only --sync-issues
        # only health checks deferred until a daily-quota reset (the daily cron)
    python scripts/reconcile_provider_routes.py --evidence-report --provider gemini
        # classify configured + a few free-marked models per provider, printing status and
        # verdict only (never response bodies): how a provider plugin's signals are validated

Probes run inside a provider-scoped v2 dispatch pause when LLM_DISPATCH_V2_URL is set; without it
they still run but are reported as possibly contended by production traffic.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import zipfile
import zlib
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from citypods.compute.llm_dispatch_pause import (  # noqa: E402
    DispatchPauseClient,
    DispatchPauseError,
    Selection,
    paused,
)
from citypods.compute.llm_lanes import load_lanes  # noqa: E402
from citypods.provider_catalog.decisions import load_decisions  # noqa: E402
from citypods.provider_catalog.evidence import (  # noqa: E402
    discover_rate_references,
    rate_artifact,
    verified_rate_history,
)
from citypods.provider_catalog.issue import (  # noqa: E402
    decode_state,
    find_issue,
    render_body,
    sync_issue,
)
from citypods.provider_catalog.limits import (  # noqa: E402
    RateChange,
    configured_limit_changes,
    early_rate_routes,
    merge_observations,
    scope_recent_runs,
)
from citypods.provider_catalog.quality import fetch_quality_index  # noqa: E402
from citypods.provider_catalog.reconcile import (  # noqa: E402
    NoDispatchControl,
    backtest_discovery,
    backtest_summary,
    reconcile,
)

LIMITS_PATH = REPO_ROOT / "config" / "provider_limits.yml"
PAUSE_SECONDS = 900
# NVIDIA calls run up to ~400 s; a 300 s drain left the 2026-09-24 dry run contended.
DRAIN_TIMEOUT_SECONDS = 600


class WorkerDispatchControl:
    """review/48 R3 on top of the v2 Worker's pause endpoints (PR A)."""

    def __init__(self, client: DispatchPauseClient) -> None:
        self.client = client

    @contextmanager
    def paused(self, provider: str) -> Iterator[Any]:
        with paused(
            Selection("provider", provider),
            seconds=PAUSE_SECONDS,
            drain_timeout=DRAIN_TIMEOUT_SECONDS,
            reason="provider catalog reconciliation",
            client=self.client,
        ) as outcome:
            yield outcome

    def route_quota(self, provider: str) -> Mapping[str, Mapping[str, Any]]:
        try:
            return self.client.status(Selection("provider", provider)).get("routes") or {}
        except DispatchPauseError:
            return {}

    def reserve(self, route_id: str) -> None:
        try:
            self.client.reserve(route_id, 1)
        except DispatchPauseError as exc:
            # A failed ledger write cannot authorize another scarce-quota method request.
            raise DispatchPauseError(f"could not reserve {route_id}: {exc}") from exc


def _control(no_pause: bool) -> Any:
    if no_pause or not (
        os.environ.get("LLM_DISPATCH_V2_URL") or os.environ.get("CITYPODS_LLM_DISPATCH_V2_URL")
    ):
        return NoDispatchControl()
    return WorkerDispatchControl(DispatchPauseClient())


def evidence_report(limits: Mapping[str, Any], providers: set[str], control: Any) -> int:
    """Use the production planner so evidence checks obey spacing, quota and JSON verification."""
    from citypods.provider_catalog.quality import QualityIndex

    report = reconcile(
        limits,
        load_lanes(),
        load_decisions(),
        QualityIndex(),
        {},
        session=requests.Session(),
        control=control,
        today=datetime.now(UTC).date(),
        providers=providers or None,
    )
    for observation in report.observations:
        print(observation)
    for anomaly in report.anomalies:
        print(f"{anomaly.route_id}: {anomaly.verdict}: {anomaly.reason}")
    for candidate in report.candidates:
        print(f"{candidate.key}: proven; JSON method {candidate.structured_output_method}")
    return 0


def _carry_rate_state(report, previous_state):
    """Retain advisory offers on manual scans or unavailable history; writers reauthenticate."""
    for key in ("rate_evidence_refs", "rate_observations", "rate_changes"):
        report.state[key] = previous_state.get(key, [])
    prior = (previous_state.get("last_full") or {}).get(
        "rate_changes", previous_state.get("rate_changes", [])
    )
    try:
        report.rate_changes = [RateChange(**c) for c in prior or []]
    except (TypeError, ValueError):
        report.rate_changes = []
    report.state["last_full"]["rate_changes"] = [asdict(c) for c in report.rate_changes]
    report.state["rate_changes"] = [asdict(c) for c in report.rate_changes]


def manual_context_canary(args, limits, control):
    """Run only selected, server-approved contexts; never discover models or publish choices."""
    import time

    from citypods.llm_rate_probe import RateProbeRunner
    from citypods.provider_catalog.evidence import (
        _context_catalog_digest,
        context_artifact,
        digest,
        discover_context_references,
        verified_context_history,
    )
    from citypods.provider_catalog.probe import build_context_request
    from citypods.provider_catalog.reconcile import Report, _measure_context_routes
    from citypods.provider_catalog.registry import rules_for
    from citypods.provider_catalog.rules import context_parser_support

    if (
        os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or os.environ.get("GITHUB_REF") != "refs/heads/main"
        or args.due_only
        or args.sync_issues
        or args.no_pause
        or args.provider
        or args.rate_evidence
        or args.backtest
        or args.evidence_report
        or not isinstance(control, WorkerDispatchControl)
    ):
        raise ValueError(
            "manual context requires isolated workflow_dispatch on main with Worker control"
        )
    route_ids = args.context_routes.split(",")
    if (
        not 1 <= len(route_ids) <= 8
        or len(set(route_ids)) != len(route_ids)
        or any(not rid or rid != rid.strip() for rid in route_ids)
        or not 0 < args.context_calls <= 8
        or not 0 < args.context_input <= 524288
        or not 0 < args.context_output <= 32768
        or not 0 < args.context_input_budget <= 2097152
        or not 0 < args.context_output_budget <= 131072
        or not 0 < len(args.context_purpose.strip()) <= 256
    ):
        raise ValueError("invalid manual context selection or limits")
    dimensions = (
        ["input", "output"] if args.context_dimension == "both" else [args.context_dimension]
    )
    now = datetime.now(UTC)
    repository = os.environ["GITHUB_REPOSITORY"]
    run_id, head_sha = os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_SHA"]
    from citypods.provider_catalog.evidence import _gh_json

    run = _gh_json(f"repos/{repository}/actions/runs/{run_id}")
    workflow = _gh_json(f"repos/{repository}/actions/workflows/provider-catalog-reconcile.yml")
    if (
        str(run.get("id")) != run_id
        or run.get("event") != "workflow_dispatch"
        or run.get("head_sha") != head_sha
        or run.get("head_branch") != "main"
        or run.get("workflow_id") != workflow.get("id")
        or workflow.get("path") != ".github/workflows/provider-catalog-reconcile.yml"
        or (run.get("head_repository") or {}).get("full_name") != repository
        or run.get("status") != "in_progress"
    ):
        raise ValueError("untrusted manual context workflow")
    compiled = json.loads(
        (REPO_ROOT / "workers/llm-dispatch-v2/src/dispatch_limits.json").read_text()
    )
    status = control.client.context_status(Selection("global"))
    if _context_catalog_digest(compiled) != status["catalog_digest"]:
        raise ValueError("deployed context catalog differs from checkout")
    routes = {route["route_id"]: route for route in limits["routes"]}
    selected = []
    for rid in route_ids:
        route = routes.get(rid)
        ready = status["routes"].get(rid) or {}
        if (
            not route
            or route.get("free") is not True
            or route.get("rpd") == 0
            or not ready.get("enabled")
            or ready.get("quota_scope")
            != f"{route['provider']}:{route['account_id']}:{route['upstream_model']}"
        ):
            raise ValueError("manual context route not approved or quota unknown")
        if "input" in dimensions and args.context_input > status.get("input_ceilings", {}).get(
            rid, 524288
        ):
            raise ValueError("manual input ceiling exceeds approved route authority")
        if dimensions == ["input"] and args.context_output > 256:
            raise ValueError("input canary output reservation cannot exceed 256")
        if "output" in dimensions and not status.get("output_enabled"):
            raise ValueError("manual output is not approved")
        cfg, rules = limits["providers"][route["provider"]], rules_for(route["provider"])
        for dimension in dimensions:
            request = build_context_request(
                route,
                cfg,
                rules,
                dimension=dimension,
                target=1000,
                ratio=route.get("input_token_ratio") or 1,
                attempt_ordinal=1,
                nonce="offline-identity-" * 4,
            )
            if not context_parser_support(rules, request)[0]:
                raise ValueError("manual context parser unsupported")
        selected.append(route)
    references = discover_context_references(repository=repository, now=now, manual=True)
    history, _accepted, gaps = verified_context_history(
        references, limits, repository=repository, now=now, manual=True
    )
    if gaps:
        raise ValueError("manual context history unavailable")
    authority = {
        "route_ids": route_ids,
        "dimensions": dimensions,
        "max_requests": args.context_calls,
        "max_input": args.context_input_budget,
        "max_output": args.context_output_budget,
        "per_call_input": args.context_input,
        "per_call_output": args.context_output,
        "purpose": args.context_purpose.strip(),
    }
    opened = control.client.start_manual_context(run_id, status["catalog_digest"], **authority)
    report = Report()
    context = {
        "manual": True,
        "run_id": run_id,
        "head_sha": head_sha,
        "deadline_ms": opened.deadline_ms,
        "history": history,
        "rotation": {},
        "catalog_digest": status["catalog_digest"],
        "readiness": status["routes"],
        "dimensions": dimensions,
        "per_call_input": args.context_input,
        "per_call_output": args.context_output,
        "budget": {
            "remaining_input": opened.remaining_input,
            "remaining_output": opened.remaining_output,
            "remaining_requests": opened.remaining_requests,
        },
    }
    before = None
    failure = None
    errors = []
    try:
        before = control.client.context_status(Selection("global")).get("manual_session")
        for provider in dict.fromkeys(route["provider"] for route in selected):
            cfg, rules = limits["providers"][provider], rules_for(provider)
            runner = RateProbeRunner(
                apply=True,
                max_requests_total=args.context_calls,
                max_requests_per_route=args.context_calls,
            )
            with control.paused(provider) as pause, requests.Session() as session:
                _measure_context_routes(
                    report,
                    context,
                    [route for route in selected if route["provider"] == provider],
                    cfg,
                    rules,
                    runner,
                    pause,
                    control,
                    session,
                    sleep=time.sleep,
                    cooldown=rules.canary_interval_seconds,
                )
    except Exception as exc:
        failure = exc
        errors.append({"stage": "measurement", "type": type(exc).__name__})
    finally:
        try:
            control.client.finish_manual_context(run_id, status["catalog_digest"])
        except Exception as exc:
            failure = failure if failure is not None else exc
            errors.append({"stage": "cleanup", "type": type(exc).__name__})
    after = None
    try:
        after = control.client.context_status(Selection("global")).get("manual_session")
    except Exception as exc:
        failure = failure if failure is not None else exc
        errors.append({"stage": "status", "type": type(exc).__name__})
    try:
        artifact = context_artifact(
            report.context_observations,
            limits,
            repository=repository,
            run_id=run_id,
            head_sha=head_sha,
            now=datetime.now(UTC),
            catalog_digest=status["catalog_digest"],
            attempted_routes=report.context_attempted_routes,
            states=report.context_states,
        )
        artifact["payload"].update(
            {
                "kind": "manual_context",
                "authority": authority,
                "accounting_before": before,
                "accounting_after": after,
                "run_status": "failed" if failure is not None else "success",
                "errors": errors,
            }
        )
        artifact["payload_digest"] = digest(artifact["payload"])
        args.context_evidence.write_text(json.dumps(artifact, sort_keys=True, indent=2) + "\n")
    except Exception:
        if failure is not None:
            raise failure from None
        raise
    if failure is not None:
        raise failure
    for observation in report.observations:
        print(observation)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--provider", action="append", default=[])
    parser.add_argument("--sync-issues", action="store_true")
    parser.add_argument("--due-only", action="store_true")
    parser.add_argument("--no-pause", action="store_true")
    parser.add_argument("--rate-evidence", type=Path)
    parser.add_argument("--context-evidence", type=Path)
    parser.add_argument("--manual-context", action="store_true")
    parser.add_argument("--context-routes", default="")
    parser.add_argument("--context-dimension", choices=("input", "output", "both"), default="input")
    parser.add_argument("--context-calls", type=int, default=2)
    parser.add_argument("--context-input", type=int, default=8192)
    parser.add_argument("--context-output", type=int, default=256)
    parser.add_argument("--context-input-budget", type=int, default=2097152)
    parser.add_argument("--context-output-budget", type=int, default=131072)
    parser.add_argument("--context-purpose", default="")
    parser.add_argument("--evidence-report", action="store_true")
    parser.add_argument(
        "--backtest",
        action="store_true",
        help="would discovery re-find each configured model? (catalog reads only, no canaries)",
    )
    args = parser.parse_args(argv)

    limits = yaml.safe_load(LIMITS_PATH.read_text(encoding="utf-8"))
    if args.sync_issues and args.provider:
        parser.error("--sync-issues rewrites the whole issue; run it without --provider")
    unknown = set(args.provider) - set(limits.get("providers") or {})
    if unknown:
        parser.error(f"unknown provider(s): {', '.join(sorted(unknown))}")
    control = _control(args.no_pause)
    if args.manual_context:
        if args.context_evidence is None:
            parser.error("manual context requires an evidence path")
        return manual_context_canary(args, limits, control)
    if args.evidence_report:
        return evidence_report(limits, set(args.provider), control)
    if args.backtest:
        session = requests.Session()
        rows = backtest_discovery(
            limits,
            load_lanes(),
            fetch_quality_index(session),
            session=session,
            providers=set(args.provider) or None,
        )
        found = sum(r.gate == "discoverable" for r in rows)
        print(f"discovery recall: {found}/{len(rows)} configured models")
        for r in rows:
            score = f"{r.score:g}" if r.score is not None else "-"
            missed = (
                sorted(set(r.lanes_actual) - set(r.lanes_offered))
                if r.gate == "discoverable"
                else []
            )
            print(
                f"  {r.provider:10} {r.model:48} {r.gate:26} AA={score:5} "
                f"lanes={','.join(r.lanes_actual) or '-'}"
                + (f" NOT-OFFERED={','.join(missed)}" if missed else "")
            )
        return 0

    today = datetime.now(UTC).date()
    existing = find_issue() if args.sync_issues else None
    previous_state = decode_state((existing or {}).get("body") or "")
    rate_run_id = ""
    early = set()
    if args.rate_evidence:
        if os.environ.get("GITHUB_EVENT_NAME") != "schedule":
            parser.error("rate artifacts require a scheduled workflow run")
        rate_run_id = os.environ["GITHUB_RUN_ID"]
        if isinstance(control, WorkerDispatchControl) and args.due_only:
            try:
                stats = control.client.rate_failures()
                early = early_rate_routes(
                    stats.get("route_failures") or [], limits, previous_state, today=today
                )
            except DispatchPauseError:
                print("early rate checks deferred: Worker telemetry unavailable")
    if args.due_only and not previous_state.get("deferred") and not early:
        # The daily run: nothing is waiting on a quota reset, so no network calls at all.
        print("due-only: no deferred checks; nothing to do")
        return 0
    context_run = None
    context_status = None
    if args.context_evidence:
        if (
            os.environ.get("GITHUB_EVENT_NAME") != "schedule"
            or args.due_only
            or args.provider
            or os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_EVENT_SCHEDULE") != "17 10 * * 1"
        ):
            parser.error("context evidence is weekly scheduled main only")
        if isinstance(control, WorkerDispatchControl):
            from citypods.compute.llm_dispatch_pause import Selection
            from citypods.provider_catalog.evidence import (
                _context_catalog_digest,
                discover_context_references,
                verified_context_history,
            )

            try:
                context_status = control.client.context_status(Selection("global"))
                if context_status["enabled"]:
                    compiled = json.loads(
                        (REPO_ROOT / "workers/llm-dispatch-v2/src/dispatch_limits.json").read_text()
                    )
                    if _context_catalog_digest(compiled) != context_status["catalog_digest"]:
                        raise ValueError("deployed context catalog differs from this checkout")
                    now = datetime.now(UTC)
                    references = discover_context_references(
                        repository=os.environ["GITHUB_REPOSITORY"], now=now
                    )
                    history, _accepted, gaps = verified_context_history(
                        references, limits, repository=os.environ["GITHUB_REPOSITORY"], now=now
                    )
                    if gaps:
                        raise ValueError("context history unavailable")
                    opened = control.client.start_context(
                        os.environ["GITHUB_RUN_ID"], context_status["catalog_digest"]
                    )
                    context_run = {
                        "run_id": opened.run_id,
                        "head_sha": os.environ["GITHUB_SHA"],
                        "deadline_ms": opened.deadline_ms,
                        "catalog_digest": context_status["catalog_digest"],
                        "readiness": context_status["routes"],
                        "history": history,
                        "rotation": dict(
                            (previous_state.get("context_v1") or {}).get("rotation") or {}
                        ),
                        "budget": {
                            "remaining_input": opened.remaining_input,
                            "remaining_output": opened.remaining_output,
                            "remaining_requests": opened.remaining_requests,
                        },
                    }
            except (
                DispatchPauseError,
                ValueError,
                TypeError,
                KeyError,
                AttributeError,
                OSError,
                RuntimeError,
                subprocess.CalledProcessError,
                zipfile.BadZipFile,
                zlib.error,
            ):
                print("context calibration deferred: authority/history unavailable")
    session = requests.Session()
    quality = fetch_quality_index(session)
    report = reconcile(
        limits,
        load_lanes(),
        load_decisions(),
        quality,
        previous_state,
        session=session,
        control=control,
        today=today,
        providers=set(args.provider) or None,
        due_only=args.due_only,
        rate_run_id=rate_run_id,
        early_rate_checks=early,
        context_run=context_run,
    )
    _carry_rate_state(report, previous_state)
    if args.rate_evidence:
        now = datetime.now(UTC)
        repository = os.environ["GITHUB_REPOSITORY"]
        envelope = rate_artifact(
            report.rate_observations,
            limits,
            repository=repository,
            run_id=rate_run_id,
            head_sha=os.environ["GITHUB_SHA"],
            now=now,
            attempted_routes=report.rate_attempted_routes,
        )
        args.rate_evidence.write_text(json.dumps(envelope, indent=2) + "\n")
        try:
            references = discover_rate_references(repository=repository, now=now)
            history, accepted, deferred = verified_rate_history(
                references,
                limits,
                repository=repository,
                now=now,
            )
            # Current observations may offer advisory choices; only a completed successful
            # artifact can authorize the subsequent trusted writer.
            current = {
                "run_id": rate_run_id,
                "payload_digest": envelope["payload_digest"],
                "observed_at": now.isoformat(),
                "attempted_routes": sorted(report.rate_attempted_routes),
            }
            accepted = (*accepted, current)
            recent = scope_recent_runs(accepted, limits)
            merged = merge_observations(history, report.rate_observations, now=now)
            changes, mismatch = configured_limit_changes(
                merged, limits, now=now, recent_runs=recent
            )
            report.rate_changes = list(changes)
            report.observations.extend((*deferred, *mismatch))
            report.state["rate_evidence_refs"] = list(accepted)
            report.state["rate_observations"] = [asdict(o) for o in merged]
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            RuntimeError,
            subprocess.CalledProcessError,
            zipfile.BadZipFile,
            zlib.error,
        ):
            report.observations.append("rate history unavailable; offers deferred")
        report.state["last_full"]["rate_changes"] = [asdict(c) for c in report.rate_changes]
        report.state["rate_changes"] = [asdict(c) for c in report.rate_changes]
    if args.context_evidence:
        from citypods.provider_catalog.evidence import context_artifact
        from citypods.provider_catalog.limits import context_cap_changes

        envelope = context_artifact(
            report.context_observations,
            limits,
            repository=os.environ["GITHUB_REPOSITORY"],
            run_id=os.environ["GITHUB_RUN_ID"],
            head_sha=os.environ["GITHUB_SHA"],
            now=datetime.now(UTC),
            catalog_digest=(context_status or {}).get("catalog_digest", "0" * 64),
            attempted_routes=report.context_attempted_routes,
            states=report.context_states,
        )
        args.context_evidence.write_text(json.dumps(envelope, indent=2) + "\n")
        if context_run:
            report.context_changes = list(
                # Advisory until this run succeeds; /apply independently authenticates its artifact.
                context_cap_changes(
                    (*context_run["history"], *report.context_observations),
                    limits,
                    now=datetime.now(UTC),
                )
            )
        report.state["last_full"]["context_changes"] = list(report.context_changes)
    if not args.due_only and not args.provider:
        # Self-check (catalog reads only): would discovery re-find what is already configured?
        # A drop here means a plugin gate drifted from how routes are actually chosen.
        report.observations.append(
            backtest_summary(backtest_discovery(limits, load_lanes(), quality, session=session))
        )
        report.state["last_full"]["observations"] = list(report.observations)
    print(
        f"reconciliation: {len(report.candidates)} candidate(s), "
        f"{len(report.anomalies)} anomaly(ies), {len(report.observations)} observation(s)"
    )
    if args.sync_issues:
        print(
            sync_issue(
                report,
                run_date=today.isoformat(),
                limits=limits,
                lanes=load_lanes(),
                decisions=load_decisions(),
                today=today,
            )
        )
    else:
        print(render_body(report, run_date=today.isoformat()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
