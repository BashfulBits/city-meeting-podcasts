"""Score immutable varied-backlog observations against the separately approved gold."""

import argparse
import hashlib
import json
import statistics
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from citypods.remedy_evaluation import Gold, Manifest, canonical_hash, compare_results

ROOT = Path(__file__).resolve().parents[4]
DEFAULT_PACKET = ROOT / "evals/remedy/holdout/backlog-varied-2026-10-11/approved-v2"
DEFAULT_RESULTS = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_PACKET / "manifest.json")
    parser.add_argument("--gold", type=Path, default=DEFAULT_PACKET / "gold.json")
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS / "batches")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    manifest = Manifest.model_validate_json(args.manifest.read_text())
    gold = Gold.model_validate_json(args.gold.read_text())
    manifest_hash = canonical_hash(manifest.model_dump())
    gold_hash = canonical_hash(gold.model_dump())
    latest = {}
    provenance = []
    for path in sorted(args.results.glob("*/*.json")):
        item = json.loads(path.read_text())
        if item.get("manifest_hash") != manifest_hash:
            raise SystemExit(f"manifest mismatch in {path}")
        key = (item["candidate"], item["case_id"])
        prior = latest.get(key)
        if prior and prior["status"] == item["status"] == "completed":
            raise SystemExit(f"duplicate completed observation for {key}")
        if prior is None or item["observed_at"] > prior["observed_at"]:
            latest[key] = item
        provenance.append(
            {
                "path": str(path.relative_to(ROOT)),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )

    grouped = {}
    for (candidate, _case_id), item in latest.items():
        grouped.setdefault(candidate, []).append(item)

    report = {
        "version": 1,
        "scored_at": datetime.now(UTC).isoformat(),
        "manifest_hash": manifest_hash,
        "gold_hash": gold_hash,
        "manifest_split": manifest.split,
        "cases_in_manifest": len(manifest.cases),
        "owner_truth_cases": sum(value.expected_owner is not None for value in gold.cases.values()),
        "admission_eligible_cases": 0,
        "candidates": {},
        "input_observations": provenance,
        "interpretation": (
            "This is a regression challenge, not an independent admission holdout. "
            "It scores approved claim-support and publication-policy answers; it has no "
            "blind-owner truth, so these results cannot qualify production admissions."
        ),
    }
    for candidate, observations in sorted(grouped.items()):
        scores = compare_results(
            [
                {
                    "case_id": item["case_id"],
                    "status": item["status"],
                    "answer": item.get("answer"),
                }
                for item in observations
                if item["status"] != "deferred"
            ],
            gold,
            manifest=manifest,
        )
        completed = [item for item in observations if item["status"] == "completed"]
        timings = [item["end_to_end_seconds"] for item in completed]
        provider_timings = [
            diagnostic["duration_seconds"]
            for item in completed
            for diagnostic in item.get("diagnostics", [])
        ]
        status_counts = Counter(item["status"] for item in observations)
        wrong_ids = [
            item["case_id"]
            for item in observations
            if item["status"] == "completed"
            and gold.cases[item["case_id"]].supported is not None
            and item["answer"].get("supported") != gold.cases[item["case_id"]].supported
        ]
        report["candidates"][candidate] = {
            "counts": scores.counts,
            "accepted_precision": scores.accepted_precision,
            "supported_claim_recall": scores.supported_claim_recall,
            "admission_eligible_cases": scores.admission_eligible_cases,
            "latest_observation_statuses": dict(sorted(status_counts.items())),
            "wrong_case_ids": wrong_ids,
            "identity_held_completed": sum(bool(item.get("identity_held")) for item in completed),
            "serving_family_held_completed": sum(
                bool(item.get("serving_family_held")) for item in completed
            ),
            "latency_seconds": {
                "provider_median": statistics.median(provider_timings)
                if provider_timings
                else None,
                "end_to_end_median": statistics.median(timings) if timings else None,
            },
        }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x") as output:
        json.dump(report, output, indent=2, sort_keys=True)
        output.write("\n")


if __name__ == "__main__":
    main()
