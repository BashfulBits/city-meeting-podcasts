#!/usr/bin/env python3
"""Report observed Google-first Gemma attempts from saved logs, without network access."""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_MATURITY_SECONDS = 780


def timestamp(value: str) -> float:
    """Require an explicit offset so local time cannot silently move the cohort cutoff."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamps require an explicit UTC offset")
    return parsed.timestamp() * 1000


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _events(export: Any) -> list[dict[str, Any]]:
    if isinstance(export, list):
        return export
    if isinstance(export, Mapping) and export.get("success") is not False:
        result = export.get("result")
        result = result if isinstance(result, Mapping) else {}
        container = result.get("events")
        events = container.get("events") if isinstance(container, Mapping) else None
        if isinstance(events, list):
            return events
    raise ValueError("expected an event list or a successful Cloudflare event-query export")


def _interval(accepted: int, total: int) -> list[float] | None:
    if total == 0:
        return None
    z = 1.96
    fraction = accepted / total
    denominator = 1 + z * z / total
    center = (fraction + z * z / (2 * total)) / denominator
    half = z * math.sqrt(fraction * (1 - fraction) / total + z * z / (4 * total * total))
    half /= denominator
    return [max(0, center - half), min(1, center + half)]


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    accepted = sum(
        _number(row.get("provider_status_code")) and 200 <= row["provider_status_code"] < 300
        for row in rows
    )
    full_inputs = [
        row["observed_input_tokens"] for row in rows if _number(row.get("observed_input_tokens"))
    ]
    estimates = [
        row["input_token_estimate"] for row in rows if _number(row.get("input_token_estimate"))
    ]
    quota_ids: Counter[str] = Counter()
    quota_limits: Counter[str] = Counter()
    for row in rows:
        quota = row.get("quota")
        if not isinstance(quota, Mapping):
            continue
        violations = quota.get("violations")
        for violation in violations[:4] if isinstance(violations, list) else []:
            if not isinstance(violation, Mapping):
                continue
            quota_id = violation.get("quota_id")
            if isinstance(quota_id, str) and re.fullmatch(
                r"GenerateContent[A-Za-z0-9-]{1,160}", quota_id
            ):
                quota_ids[quota_id] += 1
            limit = violation.get("quota_value")
            if _number(limit) and limit >= 0:
                quota_limits[str(limit)] += 1
    return {
        "jobs": len(rows),
        "accepted_http_2xx": accepted,
        "acceptance_fraction": accepted / len(rows) if rows else None,
        "wilson_95_interval": _interval(accepted, len(rows)),
        "statuses": dict(Counter(str(row.get("provider_status_code")) for row in rows)),
        "failure_classes": dict(Counter(row.get("failure_class") or "none" for row in rows)),
        "quota_ids": dict(quota_ids),
        "quota_limits": dict(quota_limits),
        "raw_input_estimate_range": [min(estimates), max(estimates)] if estimates else None,
        "observed_full_input_range": [min(full_inputs), max(full_inputs)] if full_inputs else None,
        "observed_full_input_samples": len(full_inputs),
        "job_ids": sorted(row["job_id"] for row in rows),
    }


def build_report(
    exports: Sequence[Any],
    *,
    since: float,
    as_of: float,
    version: str,
    maturity_seconds: float = DEFAULT_MATURITY_SECONDS,
) -> dict[str, Any]:
    """Count matured, observed ordinal-one jobs; never substitute retries for initial refusals."""
    if not all(_number(v) for v in (since, as_of, maturity_seconds)):
        raise ValueError("cutoffs and maturity must be finite numbers")
    if as_of < since or maturity_seconds < 0 or not version:
        raise ValueError("invalid cohort window, maturity, or deployment version")
    mature_through = as_of - maturity_seconds * 1000
    attempts: dict[str, dict[str, Any]] = {}
    conflicts: set[str] = set()
    discarded: Counter[str] = Counter()
    for export in exports:
        for event in _events(export):
            if not isinstance(event, Mapping):
                continue
            row = event.get("source")
            if not isinstance(row, dict) or row.get("event") != "gemma_provider_attempt":
                continue
            worker = event.get("$workers")
            worker = worker if isinstance(worker, Mapping) else {}
            deployed = worker.get("scriptVersion")
            deployed = deployed if isinstance(deployed, Mapping) else {}
            if deployed.get("id") != version:
                discarded["different_or_missing_version"] += 1
                continue
            start = row.get("actual_start_at")
            if not _number(start):
                discarded["missing_start"] += 1
                continue
            end = row.get("actual_end_at")
            if not _number(end) or end < start or end > as_of:
                discarded["invalid_or_unobserved_end"] += 1
                continue
            if not all(
                isinstance(row.get(k), str) and row[k]
                for k in ("job_id", "attempt_id", "model", "route_id", "purpose")
            ):
                discarded["missing_identity"] += 1
                continue
            key = row["attempt_id"]
            if key in attempts:
                discarded["duplicate_attempt_event"] += 1
                if row != attempts[key]:
                    conflicts.add(key)
            else:
                attempts[key] = row
    for key in conflicts:
        attempts.pop(key)
    first_by_job: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in attempts.values():
        if type(row.get("attempt_ordinal")) is int and row["attempt_ordinal"] == 1:
            first_by_job[(row["model"], row["job_id"])].append(row)
    ambiguous = {key for key, rows in first_by_job.items() if len(rows) != 1}
    eligible = {}
    for key, row in attempts.items():
        if row["actual_start_at"] < since:
            discarded["before_cutoff"] += 1
        elif row["actual_start_at"] > mature_through:
            discarded["not_yet_mature"] += 1
        else:
            eligible[key] = row
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for key, rows in first_by_job.items():
        if key not in ambiguous and rows[0]["attempt_id"] in eligible:
            row = rows[0]
            groups[(row["model"], row["purpose"])].append(row)
    reports = []
    for (model, purpose), rows in sorted(groups.items()):
        routes = sorted({row["route_id"] for row in rows})
        reports.append(
            {
                "model": model,
                "purpose": purpose,
                **_summary(rows),
                "first_routes": {
                    route: _summary([row for row in rows if row["route_id"] == route])
                    for route in routes
                },
            }
        )
    return {
        "deployment_version": version,
        "since": datetime.fromtimestamp(since / 1000, UTC).isoformat(),
        "as_of": datetime.fromtimestamp(as_of / 1000, UTC).isoformat(),
        "maturity_seconds": maturity_seconds,
        "has_mature_start_window": mature_through >= since,
        "scope": (
            "Observed Google-first initial attempts; sampling and lost-call completeness unknown"
        ),
        "coverage": "Requires full producer-cohort and eventual-outcome joins; not inferred here",
        "usable_completion": (
            "Requires producer schema/quote validation; not inferred from HTTP 2xx"
        ),
        "producer_provenance": (
            "Preparation time and policy revision are not established by these logs"
        ),
        "observed_mature_attempts": len(eligible),
        "conflicting_attempt_ids": len(conflicts),
        "ambiguous_initial_jobs": len(ambiguous),
        "discarded_events": dict(discarded),
        "groups": reports,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("exports", type=Path, nargs="+")
    parser.add_argument("--since", required=True, type=timestamp)
    parser.add_argument("--as-of", required=True, type=timestamp)
    parser.add_argument("--version", required=True)
    parser.add_argument("--maturity-seconds", type=float, default=DEFAULT_MATURITY_SECONDS)
    args = parser.parse_args(argv)
    try:
        exports = [json.loads(path.read_text()) for path in args.exports]
        result = build_report(
            exports,
            since=args.since,
            as_of=args.as_of,
            version=args.version,
            maturity_seconds=args.maturity_seconds,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
