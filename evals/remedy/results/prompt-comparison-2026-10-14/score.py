"""Offline prompt/model-pair scoring; preserve all attempts and distinguish FP from FN."""

import argparse
import hashlib
import importlib.util
import itertools
import json
import statistics
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from variants import MODELS, VARIANTS

spec = importlib.util.spec_from_file_location(
    "comparison_runner", Path(__file__).with_name("run.py")
)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
from body_selection import (  # noqa: E402
    load_manifest,
    score_answer,
    validate_answer,
    write_exclusive,
)

from citypods.remedy_evaluation import canonical_hash  # noqa: E402


def signature(answer):
    return answer.publication_action, tuple(sorted(answer.feed_slugs))


def feed_errors(answer, truth):
    if truth["category"] == "insufficient_owner_proof":
        return {"fp": None, "fn": None, "unknown_owner": True}
    actual, expected = set(answer.feed_slugs), set(truth["expected_feeds"] or [])
    return {
        "fp": sorted(actual - expected),
        "fn": sorted(expected - actual),
        "action_mismatch": answer.publication_action != truth["expected_action"],
    }


def compare(left, right, truth):
    if signature(left) != signature(right):
        return "disagreement"
    verdict = score_answer(left, truth)
    if verdict.get("correct", verdict.get("guardrail_pass")):
        return "correct_agreement"
    return "wrong_agreement"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--coordination-bypass", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    frozen, cases = load_manifest()
    truth = json.loads(args.gold.read_text())
    by_id = {c.case_id: c for c in cases}
    if set(truth["cases"]) != set(by_id):
        raise ValueError("truth/input mismatch")
    rows = runner.observations(runner.OUT, runner.binding_for(frozen, args.coordination_bypass))
    chosen = {}
    for row in sorted(rows, key=lambda r: r.get("finished_at", r["observed_at"])):
        if row["status"] == "completed":
            chosen.setdefault((row["candidate"], row["variant"], row["case_id"]), row)
    report = {
        "scored_at": datetime.now(UTC).isoformat(),
        "qualified": False,
        "manifest_hash": canonical_hash(frozen),
        "gold_hash": canonical_hash(truth),
        "prompt_hashes": {k: canonical_hash(v) for k, v in VARIANTS.items()},
        "models": {},
        "pairs": {},
        "target": 540,
        "answered": len(chosen),
        "source_files": [
            {"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in sorted((runner.OUT / "batches").glob("*.json"))
        ],
        "interpretation": (
            "Shared wrong agreement differs from feed-level extra(FP)/missing(FN). "
            "Unknown-owner proof has no fabricated institutional truth. "
            "Optimization on reused historical evidence is not independent qualification."
        ),
    }
    for variant in VARIANTS:
        evaluated = {}
        for model in MODELS:
            categories = {}
            details = []
            times, provider_times = [], []
            fp = fn = identity = serving = 0
            for cid in sorted(by_id):
                row = chosen.get((model, variant, cid))
                if row is None:
                    continue
                answer = validate_answer(row["answer"], by_id[cid])
                evaluated[(model, cid)] = answer
                verdict = score_answer(answer, truth["cases"][cid])
                category = verdict["category"]
                counts = categories.setdefault(category, Counter(correct=0, incorrect=0))
                correct = verdict.get("correct", verdict.get("guardrail_pass"))
                counts["correct" if correct else "incorrect"] += 1
                errors = feed_errors(answer, truth["cases"][cid])
                fp += len(errors["fp"] or [])
                fn += len(errors["fn"] or [])
                details.append({"case_id": cid, "verdict": verdict, "feed_errors": errors})
                times.append(row["end_to_end_seconds"])
                provider_times.extend(d["duration_seconds"] for d in row["diagnostics"])
                identity += bool(row["identity_held"])
                serving += bool(row["serving_family_held"])
            report["models"][f"{model}/{variant}"] = {
                "answered": len(details),
                "unanswered": 60 - len(details),
                "categories": categories,
                "extra_feeds_fp": fp,
                "missing_feeds_fn": fn,
                "identity_held": identity,
                "serving_family_held": serving,
                "details": details,
                "median_provider_seconds": statistics.median(provider_times) if times else None,
                "median_end_to_end_seconds": statistics.median(times) if times else None,
            }
        common = sorted(
            set.intersection(*({cid for own, cid in evaluated if own == model} for model in MODELS))
        )
        pairs = []
        for first, second in itertools.combinations(MODELS, 2):
            counts = Counter(correct_agreement=0, wrong_agreement=0, disagreement=0)
            details = []
            fp = fn = over_cases = under_cases = action_only = unsupported = 0
            for cid in common:
                a, b = evaluated[(first, cid)], evaluated[(second, cid)]
                outcome = compare(a, b, truth["cases"][cid])
                counts[outcome] += 1
                if outcome != "disagreement":
                    errors = feed_errors(a, truth["cases"][cid])
                    fp += len(errors["fp"] or [])
                    fn += len(errors["fn"] or [])
                    over_cases += bool(errors["fp"])
                    under_cases += bool(errors["fn"])
                    action_only += bool(
                        errors.get("action_mismatch") and not errors["fp"] and not errors["fn"]
                    )
                    unsupported += bool(errors.get("unknown_owner") and a.feed_slugs)
                    details.append({"case_id": cid, "outcome": outcome, "feed_errors": errors})
            pairs.append(
                {
                    "models": [first, second],
                    "counts": counts,
                    "extra_feeds_fp_on_agreements": fp,
                    "missing_feeds_fn_on_agreements": fn,
                    "cases_with_extra_feeds": over_cases,
                    "cases_with_missing_feeds": under_cases,
                    "action_only_mismatch_cases": action_only,
                    "unknown_owner_unsupported_assignment_cases": unsupported,
                    "details": details,
                }
            )
        report["pairs"][variant] = {
            "common_count": len(common),
            "common_case_ids": common,
            "common_categories": Counter(truth["cases"][c]["category"] for c in common),
            "combinations": pairs,
        }
    write_exclusive(args.out, report)
    print(json.dumps({"answered": len(chosen), "pairs": report["pairs"]}, indent=2))


if __name__ == "__main__":
    main()
