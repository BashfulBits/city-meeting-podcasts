#!/usr/bin/env python3
"""Offline correlation of Google quota diagnostics and identity-verified NVIDIA responses.

Response manifests must carry job_id, request_digest (verified against the actual submitted
payload generation), route_id, model, and usage. Never infer identity from job ID or model alone.
No network, production imports, or raw provider error/completion output.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


def correlate(events: Sequence[dict], responses: Sequence[dict]) -> dict[str, Any]:
    google: dict[tuple[str, str], list[dict]] = {}
    excluded: Counter[str] = Counter()
    for event in events:
        row = event.get("source", event)
        if not isinstance(row, Mapping) or row.get("event") != "gemma_provider_attempt":
            continue
        if row.get("provider_status_code") != 429 or row.get("failure_class") != "own_tpm":
            continue
        identity = row.get("request_digest")
        if not isinstance(identity, str) or not identity:
            excluded["google_missing_generation_digest"] += 1
            continue
        bucket = google.setdefault((str(row.get("job_id")), identity), [])
        if row not in bucket:
            bucket.append(row)
    conflicting = set()
    signatures = {}
    for response in responses:
        key = (str(response.get("job_id")), str(response.get("request_digest", "")))
        if response.get("identity_verified") is not True or not key[1]:
            continue
        signature = json.dumps(response, sort_keys=True)
        if key in signatures and signatures[key] != signature:
            conflicting.add(key)
        signatures[key] = signature
    pairs = []
    seen: set[tuple[str, str]] = set()
    for response in responses:
        identity = (str(response.get("job_id")), str(response.get("request_digest", "")))
        if identity in conflicting:
            excluded["conflicting_response_generation"] += 1
            continue
        if response.get("identity_verified") is not True or not identity[1]:
            excluded["response_unverified_generation"] += 1
            continue
        if not str(response.get("route_id", "")).startswith("nvidia_"):
            excluded["not_verified_nvidia_route"] += 1
            continue
        prior = google.get(identity, [])
        # Fail closed on mixed models, changed generations, or absent chronology.
        model = str(response.get("model", "")).removeprefix("google/").lower()
        end = response.get("actual_end_at")
        if not model.startswith("gemma-4-") or not isinstance(end, int | float):
            excluded["response_missing_model_or_time"] += 1
            continue
        prior = [
            r
            for r in prior
            if str(r.get("model", "")).lower() == model
            and isinstance(r.get("actual_end_at"), int | float)
            and r["actual_end_at"] < end
        ]
        if not prior:
            excluded["no_prior_matching_google_refusal"] += 1
            continue
        if identity in seen:
            excluded["duplicate_response_generation"] += 1
            continue
        usage = response.get("usage", {})
        usage = usage if isinstance(usage, Mapping) else {}
        full = usage.get("prompt_tokens")
        if isinstance(full, bool) or not isinstance(full, int) or full <= 0:
            excluded["missing_full_input_usage"] += 1
            continue
        seen.add(identity)
        quotas = []
        for row in prior:
            q = row.get("quota")
            if not isinstance(q, Mapping):
                continue
            violations = q.get("violations")
            for violation in violations[:4] if isinstance(violations, list) else []:
                if not isinstance(violation, Mapping):
                    continue
                # Only known quota identifiers; no arbitrary error strings or dimensions.
                qid = violation.get("quota_id")
                limit = violation.get("quota_value")
                if qid == "GenerateContentInputTokensPerModelPerMinute-FreeTier" and (
                    isinstance(limit, int) and not isinstance(limit, bool) and limit >= 0
                ):
                    quotas.append({"quota_id": qid, "limit": limit})
        output = usage.get("completion_tokens")
        if isinstance(output, bool) or not isinstance(output, int) or output < 0:
            excluded["missing_output_usage"] += 1
            continue
        total = full + output
        pairs.append(
            {
                "job_id": identity[0],
                "request_digest": identity[1],
                "model": model,
                "google_refusal_count": len(prior),
                "google_quota_details": quotas,
                "nvidia_full_input_tokens": full,
                "nvidia_output_tokens": output,
                "nvidia_combined_tokens": total,
                "correlated_size_overflow": total > 16000,
                "combined_only_over_16k": total > 16000 and full <= 16000,
            }
        )
    return {
        "scope": "verified resolved pairs only; not full Google acceptance or usable completion",
        "pairs": pairs,
        "paired_packets": len(pairs),
        "correlated_size_overflows": sum(p["correlated_size_overflow"] for p in pairs),
        "excluded": dict(excluded),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("events", nargs="+", type=Path)
    parser.add_argument("--responses", required=True, type=Path)
    args = parser.parse_args()
    events = []
    for path in args.events:
        data = json.loads(path.read_text())
        if isinstance(data, list):
            events.extend(data)
        else:
            if data.get("success") is False:
                raise ValueError("failed event export")
            events.extend(data["result"]["events"]["events"])
    manifest = json.loads(args.responses.read_text())
    print(json.dumps(correlate(events, manifest["results"]), indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
