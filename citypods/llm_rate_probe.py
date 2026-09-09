"""LLM endpoint rate-limit characterization and failure-class probe harness.

This tool intentionally uses a fixed harmless prompt and never opens storage, records,
or the LLM budget ledger. It measures enforced RPM, burst capacity, input ceilings,
and recovery timing across catalog routes without consuming production state.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests

from citypods.compute.llm_failure_class import classify_provider_failure

FIXED_PROMPT = "Ping"
HEADER_REGEX = re.compile(
    r"^(x-ratelimit-|ratelimit-|retry-after|cf-aig-|cf-ray|x-request-id)",
    re.IGNORECASE,
)
ROUTES_FILE = Path(__file__).resolve().parent / "compute" / "llm_routes.json"


def load_route_catalog() -> list[dict[str, Any]]:
    """Load routes directly from compiled llm_routes.json."""
    if not ROUTES_FILE.exists():
        raise FileNotFoundError(f"Missing compiled routes at {ROUTES_FILE}")
    data = json.loads(ROUTES_FILE.read_text(encoding="utf-8"))
    return list(data.get("routes", []))


class ProbeBudgetExceeded(Exception):
    """Raised when total request or wall-clock budget is exceeded."""


class RateProbeRunner:
    """Manages probe execution, safety limits, and live provider calls."""

    def __init__(
        self,
        *,
        apply: bool = False,
        max_requests_per_route: int = 40,
        max_requests_total: int = 400,
        max_wall_seconds: int = 900,
        session: requests.Session | None = None,
    ) -> None:
        self.apply = apply
        self.max_requests_per_route = max_requests_per_route
        self.max_requests_total = max_requests_total
        self.max_wall_seconds = max_wall_seconds
        self.session = session or requests.Session()
        self.total_requests = 0
        self.route_request_counts: dict[str, int] = {}
        self.start_time = time.monotonic()

    def check_budgets(self, route_id: str) -> None:
        """Check wall-clock, total-request, and per-route request ceilings."""
        elapsed = time.monotonic() - self.start_time
        if elapsed >= self.max_wall_seconds:
            raise ProbeBudgetExceeded(
                f"Max wall time of {self.max_wall_seconds}s exceeded ({elapsed:.1f}s elapsed)"
            )
        if self.total_requests >= self.max_requests_total:
            raise ProbeBudgetExceeded(f"Max total requests of {self.max_requests_total} reached")
        route_count = self.route_request_counts.get(route_id, 0)
        if route_count >= self.max_requests_per_route:
            raise ProbeBudgetExceeded(
                f"Max requests per route ({self.max_requests_per_route}) reached for {route_id}"
            )

    def send_request(
        self,
        route: dict[str, Any],
        prompt: str,
        *,
        max_tokens: int = 1,
    ) -> dict[str, Any]:
        """Send a single live request to a route, enforcing safety budgets."""
        route_id = route.get("route_id", "unknown")
        self.check_budgets(route_id)

        if not self.apply:
            # Dry-run: zero live calls
            return {
                "dry_run": True,
                "status": None,
                "headers": {},
                "body": None,
            }

        api_key_env = route.get("api_key_env")
        api_key = os.environ.get(api_key_env or "")
        if not api_key:
            return {
                "error": "missing_api_key",
                "status": None,
                "headers": {},
                "body": None,
            }

        api_base = str(route.get("api_base", "")).rstrip("/")
        chat_path = route.get("chat_path", "/v1/chat/completions")
        url = f"{api_base}{chat_path}"

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        payload = {
            "model": route.get("upstream_model"),
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "stream": False,
        }

        self.total_requests += 1
        self.route_request_counts[route_id] = self.route_request_counts.get(route_id, 0) + 1

        try:
            resp = self.session.post(url, headers=headers, json=payload, timeout=15)
            status = resp.status_code
            captured_headers = {
                k.lower(): v for k, v in resp.headers.items() if HEADER_REGEX.search(k)
            }
            try:
                body = resp.json()
            except Exception:
                body = resp.text
            return {
                "status": status,
                "headers": captured_headers,
                "body": body,
            }
        except requests.RequestException as exc:
            return {
                "exception": type(exc).__name__,
                "error": str(exc),
                "status": None,
                "headers": {},
                "body": None,
            }


def run_phase_0(runner: RateProbeRunner, route: dict[str, Any]) -> dict[str, Any]:
    """Phase 0: Reachability & header inventory (1 request/route)."""
    resp = runner.send_request(route, FIXED_PROMPT, max_tokens=1)
    if resp.get("dry_run"):
        return {"phase": "0", "dry_run": True}

    status = resp.get("status")
    headers = resp.get("headers", {})
    body = resp.get("body")

    error_info: dict[str, Any] = {}
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            error_info = {
                "type": err.get("type"),
                "code": err.get("code"),
                "status": err.get("status"),
                "message": str(err.get("message") or "")[:300],
            }

    cls = None
    if status is not None:
        cls = classify_provider_failure(
            status=status,
            body=body,
            headers=headers,
            route=route,
        )

    return {
        "phase": "0",
        "status": status,
        "headers": headers,
        "error": error_info,
        "classification": (
            {
                "failure_class": cls.failure_class,
                "rule_id": cls.rule_id,
                "scope": cls.scope,
            }
            if cls
            else None
        ),
    }


def run_phase_1(runner: RateProbeRunner, route: dict[str, Any]) -> dict[str, Any]:
    """Phase 1: Declared-vs-enforced RPM."""
    rpm = route.get("rpm") or 10
    target_count = min(rpm, 20) + 2
    spacing = 60.0 / float(rpm)

    first_429_index = None
    first_429_class = None
    successful_requests = 0

    for i in range(1, target_count + 1):
        if i > 1 and runner.apply:
            time.sleep(spacing)
        resp = runner.send_request(route, FIXED_PROMPT, max_tokens=1)
        if resp.get("dry_run"):
            return {"phase": "1", "dry_run": True, "target_requests": target_count}
        if resp.get("status") == 429:
            first_429_index = i
            cls = classify_provider_failure(
                status=429,
                body=resp.get("body"),
                headers=resp.get("headers"),
                route=route,
            )
            first_429_class = cls.failure_class
            break
        if resp.get("status") == 200:
            successful_requests += 1

    return {
        "phase": "1",
        "target_requests": target_count,
        "first_429_index": first_429_index,
        "first_429_class": first_429_class,
        "enforced_rpm_at_least": successful_requests if first_429_index is None else None,
    }


def run_phase_1b(runner: RateProbeRunner, route: dict[str, Any]) -> dict[str, Any]:
    """Phase 1b: Burst capacity."""
    rpm = route.get("rpm") or 10
    burst_limit = min(2 * rpm + 5, 40)
    observed_burst = 0

    for _ in range(burst_limit):
        resp = runner.send_request(route, FIXED_PROMPT, max_tokens=1)
        if resp.get("dry_run"):
            return {"phase": "1b", "dry_run": True, "max_burst_probes": burst_limit}
        if resp.get("status") == 429:
            break
        if resp.get("status") == 200:
            observed_burst += 1

    return {
        "phase": "1b",
        "max_burst_probes": burst_limit,
        "observed_burst": observed_burst,
    }


def run_phase_2(runner: RateProbeRunner, route: dict[str, Any]) -> dict[str, Any]:
    """Phase 2: Enforced input ceiling via binary search (max 8 probes)."""
    context_limit = route.get("input_context_limit") or 32000
    low = 1000
    high = min(context_limit, 250000)
    observed_ceiling = low

    filler_sentence = "The quick brown fox jumps over the lazy dog. "
    chars_per_token = 4

    for _ in range(8):
        if low >= high - 1000:
            break
        mid = (low + high) // 2
        char_count = mid * chars_per_token
        multiplier = max(1, char_count // len(filler_sentence))
        prompt = (filler_sentence * multiplier)[:char_count]

        resp = runner.send_request(route, prompt, max_tokens=1)
        if resp.get("dry_run"):
            return {"phase": "2", "dry_run": True, "context_limit": context_limit}

        status = resp.get("status")
        if status == 200:
            observed_ceiling = mid
            low = mid
        elif status in (400, 413, 429):
            high = mid
        else:
            break

    return {
        "phase": "2",
        "observed_input_ceiling": observed_ceiling,
        "advertised_context_limit": context_limit,
    }


def run_phase_3(runner: RateProbeRunner, route: dict[str, Any]) -> dict[str, Any]:
    """Phase 3: Recovery timing after 429."""
    intervals = [5, 15, 30, 60, 120, 300]
    observed_recovery = None

    for delay in intervals:
        if runner.apply:
            time.sleep(delay)
        resp = runner.send_request(route, FIXED_PROMPT, max_tokens=1)
        if resp.get("dry_run"):
            return {"phase": "3", "dry_run": True}
        if resp.get("status") == 200:
            observed_recovery = delay
            break

    return {
        "phase": "3",
        "observed_recovery_seconds": observed_recovery,
    }


def run_phase_4(
    p0_observation: dict[str, Any],
) -> dict[str, Any] | None:
    """Phase 4: Upstream-vs-own labeling against cold first request from Phase 0."""
    if p0_observation.get("status") != 429:
        return None

    cls_info = p0_observation.get("classification") or {}
    classifier_class = cls_info.get("failure_class")
    ground_truth = "upstream_capacity"
    agreed = classifier_class == ground_truth

    return {
        "phase": "4",
        "ground_truth": ground_truth,
        "classified_as": classifier_class,
        "rule_id": cls_info.get("rule_id"),
        "agreed": agreed,
    }


def run_probes(
    routes: list[dict[str, Any]],
    phases: list[str],
    runner: RateProbeRunner,
) -> dict[str, Any]:
    """Run specified phases across target routes and collect report data."""
    results: list[dict[str, Any]] = []

    for route in routes:
        route_id = route.get("route_id", "unknown")
        api_key_env = route.get("api_key_env")
        if runner.apply and (not api_key_env or not os.environ.get(api_key_env)):
            results.append(
                {
                    "route_id": route_id,
                    "provider": route.get("provider"),
                    "status": "skipped",
                    "reason": f"Missing {api_key_env} in environment",
                }
            )
            continue

        route_record: dict[str, Any] = {
            "route_id": route_id,
            "provider": route.get("provider"),
            "model": route.get("model"),
            "observations": {},
        }

        try:
            p0_res = None
            if "0" in phases:
                p0_res = run_phase_0(runner, route)
                route_record["observations"]["phase_0"] = p0_res

            if "1" in phases:
                route_record["observations"]["phase_1"] = run_phase_1(runner, route)

            if "1b" in phases:
                route_record["observations"]["phase_1b"] = run_phase_1b(runner, route)

            if "2" in phases:
                route_record["observations"]["phase_2"] = run_phase_2(runner, route)

            if "3" in phases:
                route_record["observations"]["phase_3"] = run_phase_3(runner, route)

            if "4" in phases:
                if p0_res:
                    p4_res = run_phase_4(p0_res)
                    if p4_res:
                        route_record["observations"]["phase_4"] = p4_res

            results.append(route_record)
        except ProbeBudgetExceeded as exc:
            route_record["budget_interrupted"] = str(exc)
            results.append(route_record)
            break

    # Calculate Phase 4 agreement rate if applicable
    p4_agreements = 0
    p4_total = 0
    for r in results:
        obs = r.get("observations", {})
        p4 = obs.get("phase_4")
        if p4 and isinstance(p4, dict):
            p4_total += 1
            if p4.get("agreed"):
                p4_agreements += 1

    summary: dict[str, Any] = {
        "apply": runner.apply,
        "total_requests": runner.total_requests,
        "elapsed_seconds": round(time.monotonic() - runner.start_time, 2),
        "routes_probed": len(results),
        "phase_4_agreement_rate": (round(p4_agreements / p4_total, 3) if p4_total > 0 else None),
        "phase_4_total_evaluations": p4_total,
    }

    return {"summary": summary, "routes": results}


def emit_github_step_summary(report: dict[str, Any]) -> None:
    """Render markdown summary into GITHUB_STEP_SUMMARY if present."""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    summary = report.get("summary", {})
    routes = report.get("routes", [])

    lines = [
        "## LLM Rate-Limit Probe Summary",
        "",
        f"- **Mode:** {'LIVE' if summary.get('apply') else 'DRY-RUN'}",
        f"- **Requests:** {summary.get('total_requests')}",
        f"- **Routes Probed:** {summary.get('routes_probed')}",
        f"- **Elapsed Time:** {summary.get('elapsed_seconds')}s",
    ]
    if summary.get("phase_4_total_evaluations", 0) > 0:
        rate = summary.get("phase_4_agreement_rate")
        pct = f"{rate * 100:.1f}%" if rate is not None else "N/A"
        evals = summary.get("phase_4_total_evaluations")
        lines.append(f"- **Phase 4 Agreement Rate:** {pct} ({evals} 429s evaluated)")
    lines.extend(
        [
            "",
            "| Route | Provider | Status | Failure Class | Rule ID |",
            "|---|---|---|---|---|",
        ]
    )

    for r in routes:
        route_id = r.get("route_id", "")
        provider = r.get("provider", "")
        obs = r.get("observations", {})
        p0 = obs.get("phase_0", {})
        status = p0.get("status") or r.get("status") or "-"
        cls = p0.get("classification") or {}
        f_class = cls.get("failure_class", "-")
        rule_id = cls.get("rule_id", "-")
        lines.append(f"| `{route_id}` | `{provider}` | {status} | `{f_class}` | `{rule_id}` |")

    Path(summary_path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="LLM endpoint rate-limit characterization probe harness"
    )
    parser.add_argument(
        "--route",
        action="append",
        dest="routes",
        help="Route ID to probe (repeatable; default: all routes)",
    )
    parser.add_argument(
        "--provider",
        action="append",
        dest="providers",
        help="Provider to probe (repeatable; expands to that provider's routes)",
    )
    parser.add_argument(
        "--phase",
        action="append",
        dest="phases",
        choices=["0", "1", "1b", "2", "3", "4"],
        help="Probe phase to run (repeatable; default: 0)",
    )
    parser.add_argument(
        "--include-paid",
        action="store_true",
        default=False,
        help="Include paid routes (default: free routes only)",
    )
    parser.add_argument(
        "--max-requests-per-route",
        type=int,
        default=40,
        help="Safety ceiling per route (default: 40)",
    )
    parser.add_argument(
        "--max-requests-total",
        type=int,
        default=400,
        help="Safety ceiling across all routes (default: 400)",
    )
    parser.add_argument(
        "--max-wall-seconds",
        type=int,
        default=900,
        help="Safety wall-clock timeout in seconds (default: 900)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("llm_route_characterization.json"),
        help="Output report JSON path (default: llm_route_characterization.json)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Execute live HTTP calls (without this, dry-run only)",
    )

    args = parser.parse_args(argv)
    phases = args.phases or ["0"]

    catalog = load_route_catalog()
    selected: list[dict[str, Any]] = []

    for route in catalog:
        if not args.include_paid and not route.get("free"):
            continue
        if args.providers and route.get("provider") not in args.providers:
            continue
        if args.routes and route.get("route_id") not in args.routes:
            continue
        selected.append(route)

    print(
        f"LLM Rate Probe: selected {len(selected)} routes across "
        f"{len(set(r.get('provider') for r in selected))} providers. "
        f"Phases: {', '.join(phases)}. "
        f"Mode: {'LIVE (--apply)' if args.apply else 'DRY RUN'}"
    )

    runner = RateProbeRunner(
        apply=args.apply,
        max_requests_per_route=args.max_requests_per_route,
        max_requests_total=args.max_requests_total,
        max_wall_seconds=args.max_wall_seconds,
    )

    report = run_probes(selected, phases, runner)

    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    emit_github_step_summary(report)

    print(f"Probe complete. Report saved to {args.out}")
    if not args.apply:
        print(
            "\nNote: Ran in DRY-RUN mode (0 live requests issued). Pass --apply to execute probes."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
