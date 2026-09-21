#!/usr/bin/env python3
"""Capture and render payload-free LLM producer telemetry for GitHub Actions."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urljoin

import requests

from citypods.compute.llm_submission_telemetry import (
    TELEMETRY_FILE_ENV,
    record,
    render_markdown,
)


def _scheduler_summary(body: Mapping[str, object]) -> dict[str, object]:
    """Keep the useful scheduler constraints without copying unbounded queue diagnostics."""
    scheduler = body.get("scheduler")
    scheduler = scheduler if isinstance(scheduler, Mapping) else {}
    jobs = body.get("jobs")
    jobs = jobs if isinstance(jobs, Mapping) else {}
    by_state = jobs.get("by_state")
    by_state = by_state if isinstance(by_state, Mapping) else {}
    bundles = body.get("bundles")
    bundles = bundles if isinstance(bundles, Mapping) else {}
    claim = body.get("claim")
    claim = claim if isinstance(claim, Mapping) else {}
    queued_by_model = body.get("queued_by_model")
    return {
        "active_bundles": bundles.get("active"),
        "active_calls": bundles.get("active_call_count"),
        "queued": by_state.get("queued"),
        "claimed": scheduler.get("bundle_count_today"),
        "ingested": scheduler.get("jobs_ingested_today"),
        "last_reason": claim.get("last_reason"),
        "empty_claims": claim.get("empty_count_today"),
        "reason_counts": claim.get("reason_counts_today"),
        "queued_by_model": queued_by_model if isinstance(queued_by_model, Mapping) else None,
    }


def snapshot(phase: str) -> int:
    """Record one best-effort, payload-free snapshot of the v2 scheduler."""
    url = os.environ.get("CITYPODS_LLM_DISPATCH_V2_URL") or os.environ.get("LLM_DISPATCH_V2_URL")
    token = os.environ.get("CITYPODS_LLM_DISPATCH_V2_AUTH_TOKEN") or os.environ.get(
        "LLM_DISPATCH_V2_AUTH_TOKEN"
    )
    if not url:
        record("llm_submission_worker_snapshot", phase=phase, error="v2_url_not_configured")
        return 0
    headers = {"authorization": f"Bearer {token}"} if token else {}
    try:
        response = requests.get(
            urljoin(url.rstrip("/") + "/", "v2/stats?limit=20"), headers=headers, timeout=20
        )
        if response.status_code != 200:
            raise RuntimeError(f"http_{response.status_code}")
        body = response.json()
        if not isinstance(body, Mapping):
            raise RuntimeError("malformed_json")
        record("llm_submission_worker_snapshot", phase=phase, summary=_scheduler_summary(body))
    except Exception as exc:  # noqa: BLE001 -- telemetry must not fail the workflow
        record("llm_submission_worker_snapshot", phase=phase, error=type(exc).__name__)
    return 0


def report() -> int:
    """Render recorded workflow telemetry to the Actions summary and standard output."""
    path = Path(os.environ.get(TELEMETRY_FILE_ENV, ""))
    events: list[Mapping[str, object]] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, Mapping):
                events.append(event)
    markdown = render_markdown(events)
    print(markdown)
    destination = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if destination:
        try:
            with Path(destination).open("a", encoding="utf-8") as handle:
                handle.write(markdown)
        except OSError:
            pass
    return 0


def main() -> int:
    """Parse the telemetry command and run its selected subcommand."""
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    snapshot_parser = subcommands.add_parser("snapshot")
    snapshot_parser.add_argument("phase", choices=("start", "end"))
    subcommands.add_parser("report")
    args = parser.parse_args()
    return snapshot(args.phase) if args.command == "snapshot" else report()


if __name__ == "__main__":
    raise SystemExit(main())
