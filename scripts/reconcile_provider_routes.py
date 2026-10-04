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
import os
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
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
from citypods.provider_catalog.classify import classify  # noqa: E402
from citypods.provider_catalog.decisions import load_decisions  # noqa: E402
from citypods.provider_catalog.issue import (  # noqa: E402
    decode_state,
    find_issue,
    render_body,
    sync_issue,
)
from citypods.provider_catalog.probe import account_key, canary, fetch_catalog  # noqa: E402
from citypods.provider_catalog.quality import fetch_quality_index  # noqa: E402
from citypods.provider_catalog.reconcile import (  # noqa: E402
    NoDispatchControl,
    backtest_discovery,
    backtest_summary,
    reconcile,
)
from citypods.provider_catalog.registry import all_rules  # noqa: E402

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
            print(f"warning: could not reserve {route_id}: {exc}", file=sys.stderr)


def _control(no_pause: bool) -> Any:
    if no_pause or not (
        os.environ.get("LLM_DISPATCH_V2_URL") or os.environ.get("CITYPODS_LLM_DISPATCH_V2_URL")
    ):
        return NoDispatchControl()
    return WorkerDispatchControl(DispatchPauseClient())


def evidence_report(limits: Mapping[str, Any], providers: set[str], control: Any) -> int:
    session = requests.Session()
    rules_by_name = all_rules()
    for provider in sorted(providers or limits["providers"]):
        rules, cfg = rules_by_name.get(provider), limits["providers"].get(provider)
        if rules is None or cfg is None:
            continue
        catalog = fetch_catalog(rules, cfg, session)
        configured = sorted(
            {
                (r["upstream_model"], r.get("account_id"))
                for r in limits["routes"]
                if r.get("provider") == provider and r.get("rpd") != 0
            }
        )
        ctx = rules.prepare(session) if rules.prepare else {}
        free = [
            m
            for m, rec in sorted(catalog.models.items())
            if rules.free_evidence
            and rules.chat_filter(m, rec)
            and m not in {c[0] for c in configured}
            and rules.free_evidence(m, rec, ctx)
        ][:3]
        print(f"## {provider}: catalog {len(catalog.models)} {catalog.error or ''}")
        with control.paused(provider) as pause:
            for model, account in [*configured, *((m, None) for m in free)]:
                pause.renew()
                result = classify(
                    canary(rules, cfg, model, account_key(cfg, account) or "", session), rules
                )
                print(f"  {model:55} {result.status!s:5} {result.verdict:16} {result.reason}")
            if getattr(pause, "contended", False):
                print("  (contended: dispatch was not paused and drained)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--provider", action="append", default=[])
    parser.add_argument("--sync-issues", action="store_true")
    parser.add_argument("--due-only", action="store_true")
    parser.add_argument("--no-pause", action="store_true")
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
    if args.due_only and not previous_state.get("deferred"):
        # The daily run: nothing is waiting on a quota reset, so no network calls at all.
        print("due-only: no deferred checks; nothing to do")
        return 0
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
    )
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
        print(sync_issue(report, run_date=today.isoformat()))
    else:
        print(render_body(report, run_date=today.isoformat()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
