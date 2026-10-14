"""Explicit offline scoring of body-selection answers; original attempts remain immutable."""

import argparse
import hashlib
import json
import statistics
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from body_selection import (
    MODELS,
    history,
    load_manifest,
    score_answer,
    validate_answer,
    write_exclusive,
)

from citypods.remedy_evaluation import canonical_hash


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    frozen, cases = load_manifest(args.manifest)
    gold = json.loads(args.gold.read_text())
    ids = {c.case_id for c in cases}
    if set(gold["cases"]) != ids:
        raise ValueError("gold and frozen inputs do not align")
    rows = history(args.results_dir, canonical_hash(frozen), ids)
    report = {
        "mode": "blind_body_selection",
        "scored_at": datetime.now(UTC).isoformat(),
        "manifest_hash": canonical_hash(frozen),
        "gold_hash": canonical_hash(gold),
        "target": 60 * len(MODELS),
        "answered": 0,
        "models": {},
        "qualified": False,
        "interpretation": (
            "Independent feed selection on reused historical recordings. Exact feed-set accuracy "
            "on 40 assignments is separate from 15 policy controls and five unknown-owner "
            "guardrails. Free-text body names are retained for human evidence review. "
            "This historical challenge is not a newly disjoint admission holdout."
        ),
        "provenance": [
            {"path": str(p.resolve()), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in sorted((args.results_dir / "batches").glob("*.json"))
        ],
    }
    by_id = {c.case_id: c for c in cases}
    for model in MODELS:
        chosen = {}
        own = [r for r in rows if r["candidate"] == model]
        for row in sorted(own, key=lambda r: r.get("finished_at", r["observed_at"])):
            prior = chosen.get(row["case_id"])
            if prior is None or prior["status"] != "completed":
                chosen[row["case_id"]] = row
        counts = Counter(completed=0, failed=0, deferred=0, unattempted=0)
        metrics = {}
        details, provider_times, total_times = [], [], []
        identity_held = serving_held = 0
        for case_id in sorted(ids):
            row = chosen.get(case_id)
            if row is None:
                counts["unattempted"] += 1
                continue
            counts[row["status"]] += 1
            if row["status"] != "completed":
                continue
            answer = validate_answer(row["answer"], by_id[case_id])
            verdict = score_answer(answer, gold["cases"][case_id])
            category = verdict["category"]
            metric = metrics.setdefault(category, Counter(evaluated=0, correct=0, incorrect=0))
            metric["evaluated"] += 1
            correct = verdict.get("correct", verdict.get("guardrail_pass"))
            metric["correct" if correct else "incorrect"] += 1
            details.append({"case_id": case_id, "verdict": verdict, "answer": row["answer"]})
            total_times.append(row["end_to_end_seconds"])
            provider_times.extend(d["duration_seconds"] for d in row["diagnostics"])
            identity_held += bool(row["identity_held"])
            serving_held += bool(row["serving_family_held"])
        report["answered"] += counts["completed"]
        report["models"][model] = {
            "counts": dict(counts),
            "metrics": metrics,
            "details": details,
            "provider_attempts": sum(r["provider_attempts"] for r in own),
            "identity_held_completed": identity_held,
            "serving_family_held_completed": serving_held,
            "latency_seconds": {
                "provider_median": statistics.median(provider_times) if provider_times else None,
                "end_to_end_median": statistics.median(total_times) if total_times else None,
            },
        }
    write_exclusive(args.out, report)
    print(report["answered"], "/", report["target"], "answers scored")


if __name__ == "__main__":
    main()
