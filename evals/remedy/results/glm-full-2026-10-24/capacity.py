"""Observed route throughput under the evaluation's conservative request controls."""

import json
import statistics
from datetime import datetime
from pathlib import Path

OUT = Path(__file__).resolve().parent
report = {}
for provider, directory in (("beatapi", OUT), ("nvidia", OUT / "nvidia")):
    result_paths = sorted((directory / "results").glob("*.json"))
    rows = [json.loads(p.read_text()) for p in result_paths]
    times = [r["elapsed_seconds"] for r in rows]
    starts = [datetime.fromisoformat(r["started_at"]).timestamp() for r in rows]
    ends = [datetime.fromisoformat(r["finished_at"]).timestamp() for r in rows]
    successes = len({r["case_id"] for r in rows if "answer" in r})
    span = max(ends) - min(starts) if starts else 0
    endpoint_groups = {}
    transport_groups = {}
    for row, result_path in zip(rows, result_paths, strict=True):
        endpoint = row.get("request_url") or (
            "https://api.beatapi.io/v1/chat/completions"
            if provider == "beatapi"
            else "https://integrate.api.nvidia.com/v1/chat/completions"
        )
        intent_path = directory / "intents" / (result_path.stem + ".json")
        intent = json.loads(intent_path.read_text())
        streaming = bool(intent.get("request", {}).get("stream"))
        transport = transport_groups.setdefault(
            "streaming" if streaming else "non_streaming",
            {"attempts": 0, "valid": 0, "failures": 0},
        )
        transport["attempts"] += 1
        transport["valid"] += int("answer" in row)
        transport["failures"] += int("answer" not in row)
        group = endpoint_groups.setdefault(endpoint, {"attempts": 0, "valid": 0, "failures": 0})
        group["attempts"] += 1
        group["valid"] += int("answer" in row)
        group["failures"] += int("answer" not in row)
    report[provider] = {
        "endpoint_groups": endpoint_groups,
        "transport_groups": transport_groups,
        "returned_attempts": len(rows),
        "valid_distinct_cases": successes,
        "failures": [
            {"case_id": r["case_id"], "status": r["status"], "http_status": r.get("http_status")}
            for r in rows
            if "answer" not in r
        ],
        "median_response_seconds": statistics.median(times) if times else None,
        "observed_window_seconds": span,
        "valid_cases_per_hour_in_observed_window": successes * 3600 / span if span else None,
        "limitation": "Single-flight with65-second spacing; not a provider maximum-capacity test. "
        "Disjoint half-cohorts differ in input length; latency comparisons descriptive.",
    }
(OUT / "capacity-current.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report))
