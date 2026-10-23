"""Join historical JEV probabilities with frozen chat decisions, offline.

This is a matched-candidate comparison, not a fresh production panel run. Adjudicator
counterfactuals require valid quotes and never override the two-positive admission rule.
"""

import argparse
import hashlib
import json
import math
from pathlib import Path

from scripts import score_judge_pairs as pairs


def probabilities(bundling, context, layout):
    if bundling["set_version"] != context["set_version"]:
        raise ValueError("JEV result set versions differ")
    result = {f"eval-{key}": value for key, value in bundling["answers"][layout].items()}
    result.update({f"eval-{key}": value for key, value in context["answers"]["T1"].items()})
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= 1
        for value in result.values()
    ):
        raise ValueError("JEV probabilities must be finite numbers in [0, 1]")
    return result


def evaluate(observations, gold, anchor, threshold):
    truth = pairs.labeled_truth(gold)
    if set(truth) - set(anchor):
        raise ValueError("Missing JEV probabilities for labeled candidates")
    votes = {key: anchor[key] >= threshold for key in truth}
    panels = []
    for sibling in observations:
        if sibling["model"] not in {
            "google/gemma-4-31b-it",
            "openrouter/nvidia/nemotron-3.5-super-vl:free",
        }:
            continue
        decisions = sibling["decisions"]
        contested = [
            key
            for key in truth
            if isinstance(decisions.get(key), bool) and (anchor[key] >= 0.5) != decisions[key]
        ]
        adjudicators = []
        for judge in observations:
            if judge["id"] not in {
                "orcarouter_zai_glm_5_3_flash_free",
                "nvidia_deepseek_v4_1_flash_free",
            }:
                continue
            usable = {
                key: judge["decisions"].get(key)
                for key in contested
                if judge.get("evidence_valid", {}).get(key) is True
                and isinstance(judge["decisions"].get(key), bool)
            }
            positive = [key for key, value in usable.items() if value]
            adjudicators.append(
                {
                    "route": judge["id"],
                    "contested": len(contested),
                    "usable": len(usable),
                    "correct": sum(value == truth[key] for key, value in usable.items()),
                    "positive_ids": positive,
                    "false_positive_ids": [key for key in positive if not truth[key]],
                    "unusable_ids": [key for key in contested if key not in usable],
                    "decisions": usable,
                }
            )
        panels.append(
            {
                "sibling_route": sibling["id"],
                "actual_routes": sibling.get("actual_routes", []),
                "actual_credentials": sibling.get("actual_credentials", []),
                **pairs.score_pair(votes, decisions, truth),
                "accepted_ids": [key for key in truth if votes[key] and decisions.get(key) is True],
                "contested_ids": contested,
                "both_negative_ids": [
                    key for key in truth if not votes[key] and decisions.get(key) is False
                ],
                "invalid_ids": [key for key in truth if not isinstance(decisions.get(key), bool)],
                "adjudicators": adjudicators,
            }
        )
    return {
        "threshold": threshold,
        "jev_alone": pairs.score_pair(votes, votes, truth),
        "panels": panels,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--archive", type=Path)
    source.add_argument("--observations", type=Path)
    parser.add_argument("--jev-bundling", required=True, type=Path)
    parser.add_argument("--jev-context", required=True, type=Path)
    parser.add_argument("--gold", type=Path, default=pairs.ROOT / "evals/judge/gold.json")
    parser.add_argument("--manifest", type=Path, default=pairs.ROOT / "evals/judge/manifest.json")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.out.exists():
        parser.error("Output already exists; preserve previous results")
    manifest = json.loads(args.manifest.read_text())
    observations = (
        pairs.replay_archive(args.archive, manifest)
        if args.archive
        else json.loads(args.observations.read_text())["observations"]
    )
    gold = json.loads(args.gold.read_text())
    bundling = json.loads(args.jev_bundling.read_text())
    context = json.loads(args.jev_context.read_text())
    result = {
        "policy": "JEV >= threshold AND sibling true; every other result goes to human review.",
        "adjudication": "Each adjudicator is scored separately on binary disagreements only. "
        "Disagreements use p >= 0.5 regardless of the admission sensitivity cutoff. "
        "Quotes are required; answers do not override panel admission. "
        "Producer-family eligibility must be checked before deployment.",
        "limits": "Historical JEV T1 with chapter titles/rubric versus newer chat T1 prompts; "
        "same candidates, not identical requests. Saved JEV probabilities, not raw "
        "responses re-parsed today. No population or holdout guarantee. "
        "No moment gate/grade/choose gold. Missing quote metadata means abstention.",
        "inputs": {
            str(path.relative_to(pairs.ROOT))
            if path.is_relative_to(pairs.ROOT)
            else str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (
                args.gold,
                args.manifest,
                args.jev_bundling,
                args.jev_context,
                args.observations or args.archive,
            )
        },
        "jev_metadata": [
            {
                key: data.get(key)
                for key in (
                    "experiment",
                    "generated_at",
                    "git_sha",
                    "set_version",
                    "prompt_version",
                    "calls_asked",
                    "calls_answered",
                    "errors",
                    "inconclusive",
                )
            }
            for data in (bundling, context)
        ],
        "threshold_sweep": [],
    }
    runs = {}
    for layout in ("question-order1", "question-order2"):
        anchor = probabilities(bundling, context, layout)
        runs[layout] = [
            evaluate(observations, gold, anchor, threshold)
            for threshold in (0.5, 0.6, 0.7, 0.8, 0.9, 0.95)
        ]
    result["reference"] = runs["question-order1"][0]
    result["question_orders_identical_scoring"] = runs["question-order1"] == runs["question-order2"]
    for layout, scores in runs.items():
        for score in scores:
            result["threshold_sweep"].append(
                {
                    "layout": layout,
                    "threshold": score["threshold"],
                    "jev_alone": score["jev_alone"],
                    "panels": [
                        {
                            key: panel[key]
                            for key in (
                                "sibling_route",
                                "true_accepts",
                                "false_accepts",
                                "human_review",
                            )
                        }
                        for panel in score["panels"]
                    ],
                }
            )
    args.out.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
