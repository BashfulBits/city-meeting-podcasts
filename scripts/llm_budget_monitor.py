#!/usr/bin/env python3
"""Report token-budget and request-shape failures from the v2 dispatch Worker.

The Worker classifies every provider reply it rejects and counts it per route and UTC day
(``/v2/stats?detail=1`` ``route_failures``). Several of those classes point at a budget or shape
that a maintainer can correct in config, and are otherwise only visible as retries:

* ``output_budget_exhausted`` -- the reply stopped at its output-token limit (finish_reason
  "length"). Jobs sent the route's own limit (``max_tokens_mode: "route_max"``) that still hit it
  are a model reasoning without end: lower that model's reasoning for the lane
  (``llm_lanes[lane].reasoning``), or drop the model from the lane.
* ``structured_output_empty`` / ``structured_output_invalid`` -- a 200 without usable JSON: the
  route's structured-output method may be wrong, or the output budget was cut first.
* ``own_tpm`` -- our own token-rate 429s: batches or reservations sized past the route's TPM.
* ``route_input_limit`` -- requests larger than the route accepts: re-batch or record the ceiling.
* ``input_over_route_ceiling`` -- queued jobs the Worker failed without calling a provider,
  because at its learned input ratio they were over every usable route's ``hard_input_ceiling``
  even with its tolerance. The producer re-plans them; a steady count means it sizes batches
  larger than the Worker will send.

It also reads ``usage_today`` (per lane and route, computed by the Worker from rows it already
writes): output above the job's reservation, output far below it (over-reservation that wastes
TPM admission), and calls near the Worker's 720 s response ceiling.

The report maps each route to the lanes that can send it work (``llm_lanes``), so a finding names
the lane and the config key to change. It never changes config itself. The workflow runs it near
the end of each UTC day, because the Worker's counters reset at 00:00 UTC.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from citypods.compute.llm_lanes import load_lanes  # noqa: E402

WORKER_CATALOG = REPO_ROOT / "workers" / "llm-dispatch-v2" / "src" / "dispatch_limits.json"
ISSUE_TITLE = "LLM token-budget monitor"
# Jobs built with max_tokens_mode "route_max" (citypods/chapter_jobs.py, stages.py moments,
# tags.py): for these a length stop happened at the route's own limit.
ROUTE_MAX_LANES = frozenset(
    {"chapter-agenda", "chapter-locator", "r6-moments", "topic-tags:tagger"}
)
THRESHOLDS = {
    "output_budget_exhausted": 3,
    "structured_output_empty": 3,
    "structured_output_invalid": 3,
    "own_tpm": 20,
    "route_input_limit": 3,
    "input_over_route_ceiling": 1,
}


# usage_today thresholds: judged only once a lane/route pair has this many calls in the day.
MIN_USAGE_CALLS = 10
OVER_RESERVATION_SHARE = 0.20  # >= 20% of calls wrote more than their reservation
UNDER_USE_RATIO = 0.25  # the p90 output is below a quarter of the reservation
SLOW_CALLS = 3  # calls at or past 600 s (the Worker's ceiling is 720 s)


def fetch_stats(url: str, token: str) -> dict[str, Any]:
    response = requests.get(
        urljoin(url.rstrip("/") + "/", "v2/stats"),
        # Only the classes this report acts on, so unrelated high-count rows cannot crowd them out
        # of the Worker's row limit.
        params={"detail": "1", "limit": "100", "failure_class": ",".join(sorted(THRESHOLDS))},
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def usage_findings(usage: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Reservation and latency findings from the Worker's per lane/route usage for the day."""
    findings = []
    for row in usage:
        calls = int(row.get("calls") or 0)
        # Calls that returned a usage figure; older Workers did not report it separately.
        measured = int(row.get("measured_calls", calls) or 0)
        lane, route_id = row.get("purpose"), row.get("route_id")
        reserved = int(row.get("reserved_output_mean") or 0)
        p90 = int(row.get("output_tokens_p90") or 0)
        base = {
            "lane": lane,
            "route_id": route_id,
            "calls": calls,
            "reserved": reserved,
            "p90": p90,
        }
        if int(row.get("slow_calls") or 0) >= SLOW_CALLS:
            findings.append(
                {
                    **base,
                    "kind": "slow_calls",
                    "detail": f"{row['slow_calls']} calls took >= 600 s (max "
                    f"{int(row.get('max_duration_ms') or 0) // 1000} s; ceiling 720 s)",
                    "suggestion": f"Lower this model's reasoning for `{lane}` "
                    "(`llm_lanes[<lane>].reasoning`) or move the lane off this route.",
                }
            )
        if measured < MIN_USAGE_CALLS or not reserved:
            continue
        over = int(row.get("over_reservation_calls") or 0)
        if over >= OVER_RESERVATION_SHARE * measured:
            findings.append(
                {
                    **base,
                    "kind": "reservation_too_small",
                    "detail": f"{over} of {measured} calls wrote more than the {reserved}-token "
                    f"reservation (p90 {p90})",
                    "suggestion": f"Raise `{lane}`'s output reservation toward its p90 ({p90}) so "
                    "TPM admission reflects real use.",
                }
            )
        elif p90 < UNDER_USE_RATIO * reserved:
            findings.append(
                {
                    **base,
                    "kind": "reservation_too_large",
                    "detail": f"p90 output {p90} vs a {reserved}-token reservation",
                    "suggestion": f"Lower `{lane}`'s output reservation toward {max(p90, 1024)} "
                    "to free TPM for other jobs (the route's own limit is still what is sent).",
                }
            )
    return findings


def _lanes_for_model(model: str, lanes: Mapping[str, Any]) -> list[str]:
    return sorted(
        purpose for purpose, lane in lanes.items() if model in (*lane.models, *lane.backup_models)
    )


def _suggestion(failure_class: str, route: Mapping[str, Any], lanes: list[str]) -> str:
    model = route.get("model", "?")
    if failure_class == "output_budget_exhausted":
        levels = sorted((route.get("reasoning_controls") or {}).keys())
        at_max = [lane for lane in lanes if lane in ROUTE_MAX_LANES]
        capped = [lane for lane in lanes if lane not in ROUTE_MAX_LANES]
        parts = []
        if at_max:
            fix = (
                f'set `llm_lanes[<lane>].reasoning["{model}"]` to one of {levels}'
                if levels
                else "this route declares no `reasoning_controls`; verify one (e.g. a thinking "
                "switch) or drop the model from the lane"
            )
            parts.append(
                f"{', '.join(at_max)} already send the route's limit, so the model reasoned to it: "
                f"{fix}."
            )
        if capped:
            parts.append(
                f"{', '.join(capped)} send a fixed budget: raise it or opt the job into "
                '`max_tokens_mode: "route_max"`.'
            )
        return " ".join(parts) or "No lane uses this model; check for stray jobs."
    if failure_class in {"structured_output_empty", "structured_output_invalid"}:
        return (
            f"Verify the route's structured-output method (now "
            f"`{route.get('structured_output_method', '?')}`) with a live canary; if replies are "
            "reasoning-only with finish_reason stop, change `structured_output_method`. A reply "
            "cut by its budget is reported as output_budget_exhausted instead."
        )
    if failure_class == "own_tpm":
        return (
            f"Our own token-rate limit (route tpm {route.get('tpm')}): shrink the lane's batches "
            "or output reservation, or correct the route's `tpm` if the provider allows more."
        )
    if failure_class == "route_input_limit":
        return "Requests exceed what the route accepts: re-batch the lane or record the ceiling."
    if failure_class == "input_over_route_ceiling":
        return (
            f"Queued jobs were over this route's `hard_input_ceiling` "
            f"({route.get('hard_input_ceiling')}) at the Worker's learned input ratio and were "
            "failed for the producer to re-plan: size the lane's batches from "
            "`GET /v2/calibration` or lower them."
        )
    return ""


def build_report(
    failures: list[Mapping[str, Any]],
    routes: Mapping[str, Mapping[str, Any]],
    lanes: Mapping[str, Any],
    usage: list[Mapping[str, Any]] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    findings = []
    for row in failures:
        failure_class = str(row.get("failure_class") or "")
        threshold = THRESHOLDS.get(failure_class)
        if threshold is None or int(row.get("count") or 0) < threshold:
            continue
        route = routes.get(str(row.get("route_id")), {})
        lane_names = _lanes_for_model(str(route.get("model", "")), lanes)
        findings.append(
            {
                "utc_day": row.get("utc_day"),
                "route_id": row.get("route_id"),
                "model": route.get("model"),
                "failure_class": failure_class,
                "count": int(row.get("count") or 0),
                "last_status": row.get("last_status"),
                "lanes": lane_names,
                "suggestion": _suggestion(failure_class, route, lane_names),
            }
        )
    findings.sort(key=lambda f: (f["failure_class"], -f["count"]))
    usage_rows = usage_findings(usage or [])
    if not findings and not usage_rows:
        return "No token-budget or request-shape findings today.\n", []
    if not findings:
        return _usage_section(usage_rows), usage_rows
    lines = [
        f"Findings for UTC day {findings[0]['utc_day']} from the v2 Worker's `route_failures` "
        "(thresholds: " + ", ".join(f"{k} ≥ {v}" for k, v in THRESHOLDS.items()) + ").",
        "",
        "| Class | Route | Model | Count | Lanes | Suggested correction |",
        "|---|---|---|---|---|---|",
    ]
    for f in findings:
        lines.append(
            f"| `{f['failure_class']}` | `{f['route_id']}` | `{f['model']}` | {f['count']} | "
            f"{', '.join(f['lanes']) or '-'} | {f['suggestion']} |"
        )
    if usage_rows:
        lines += ["", _usage_section(usage_rows)]
    lines += [
        "",
        "Evidence for a finding: the AI Gateway logs for the route's model on this day show the "
        "reply's `finish_reason`, output tokens and reasoning tokens. Corrections are config "
        "changes made by a maintainer; this report never edits config.",
    ]
    return "\n".join(lines) + "\n", findings + usage_rows


def _usage_section(rows: list[Mapping[str, Any]]) -> str:
    lines = [
        "**Reservations and latency** (the Worker's `usage_today`, from rows it already writes):",
        "",
        "| Finding | Lane | Route | Calls | Detail | Suggested correction |",
        "|---|---|---|---|---|---|",
    ]
    for f in rows:
        lines.append(
            f"| `{f['kind']}` | `{f['lane']}` | `{f['route_id']}` | {f['calls']} | "
            f"{f['detail']} | {f['suggestion']} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, help="write report.md and has_findings here")
    parser.add_argument("--stats-json", type=Path, help="read /v2/stats output from a file")
    args = parser.parse_args(argv)
    if args.stats_json:
        stats = json.loads(args.stats_json.read_text())
    else:
        stats = fetch_stats(
            os.environ["LLM_DISPATCH_V2_URL"], os.environ["LLM_DISPATCH_V2_AUTH_TOKEN"]
        )
    failures = list(stats.get("route_failures") or [])
    routes = json.loads(WORKER_CATALOG.read_text())["routes_by_id"]
    report, findings = build_report(
        failures, routes, load_lanes(), list(stats.get("usage_today") or [])
    )
    print(report)
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "report.md").write_text(report, encoding="utf-8")
        (args.out / "has_findings").write_text("true" if findings else "false", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
