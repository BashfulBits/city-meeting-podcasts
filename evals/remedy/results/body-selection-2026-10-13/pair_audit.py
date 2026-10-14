"""Offline common-question agreement audit; no provider calls or raw-result mutations."""

import argparse
import csv
import hashlib
import itertools
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from body_selection import history, load_manifest, score_answer, validate_answer, write_exclusive

from citypods.remedy_evaluation import canonical_hash

MODELS = ("flashlite", "kimi", "glm_full", "glm_flash", "sol", "deepseek", "gptoss")
LABELS = {
    "flashlite": "Flash-Lite",
    "kimi": "Kimi",
    "glm_full": "Full GLM",
    "glm_flash": "GLM Flash",
    "sol": "Sol",
    "deepseek": "DeepSeek",
    "gptoss": "GPT-OSS",
}


def signature(answer):
    return answer.publication_action, tuple(sorted(answer.feed_slugs))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    frozen, cases = load_manifest(args.manifest)
    truth = json.loads(args.gold.read_text())
    by_id = {c.case_id: c for c in cases}
    assert set(truth["cases"]) == set(by_id)
    rows = history(args.results_dir, canonical_hash(frozen), set(by_id))
    chosen = {name: {} for name in MODELS}
    for row in sorted(rows, key=lambda r: r.get("finished_at", r["observed_at"])):
        if row["candidate"] in MODELS and row["status"] == "completed":
            chosen[row["candidate"]].setdefault(row["case_id"], row)
    common = sorted(set.intersection(*(set(v) for v in chosen.values())))
    assert common
    answers = {
        name: {cid: validate_answer(chosen[name][cid]["answer"], by_id[cid]) for cid in common}
        for name in MODELS
    }
    report = {
        "scored_at": datetime.now(UTC).isoformat(),
        "models": list(MODELS),
        "excluded_models": ["qwen38"],
        "manifest_hash": canonical_hash(frozen),
        "gold_hash": canonical_hash(truth),
        "common_case_ids": common,
        "common_count": len(common),
        "common_categories": dict(Counter(truth["cases"][c]["category"] for c in common)),
        "answered_counts": {name: len(chosen[name]) for name in MODELS},
        "excluded_cases": {
            cid: [name for name in MODELS if cid not in chosen[name]]
            for cid in sorted(set(by_id) - set(common))
        },
        "agreement_definition": "Identical publication action and unordered exact feed set.",
        "truth_definition": "Existing frozen scorer; no changes to approved gold or policy.",
        "free_text_scored": False,
        "qualified": False,
        "source_files": [
            {"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in sorted((args.results_dir / "batches").glob("*.json"))
        ],
        "selected_attempt_ids": {
            name: {cid: chosen[name][cid]["attempt_id"] for cid in common} for name in MODELS
        },
        "pairs": [],
        "shared_wrong_cases": {},
    }
    for first, second in itertools.combinations(MODELS, 2):
        groups = {"true_positive": [], "false_positive": [], "negative": []}
        for cid in common:
            left, right = answers[first][cid], answers[second][cid]
            if signature(left) != signature(right):
                groups["negative"].append(cid)
                continue
            verdict = score_answer(left, truth["cases"][cid])
            other = score_answer(right, truth["cases"][cid])
            assert verdict == other
            correct = verdict.get("correct", verdict.get("guardrail_pass"))
            groups["true_positive" if correct else "false_positive"].append(cid)
            if not correct:
                case = by_id[cid]
                item = report["shared_wrong_cases"].setdefault(
                    cid,
                    {
                        "city": case.city,
                        "title": case.recordings[0].title,
                        "truth": truth["cases"][cid],
                        "pairs": [],
                    },
                )
                item["pairs"].append(
                    {"models": [first, second], "agreed_signature": signature(left)}
                )
        counts = {key: len(value) for key, value in groups.items()}
        assert sum(counts.values()) == len(common)
        agreements = counts["true_positive"] + counts["false_positive"]
        report["pairs"].append(
            {
                "models": [first, second],
                "counts": counts,
                "percent_of_common": {k: v * 100 / len(common) for k, v in counts.items()},
                "agreement_precision_percent": (
                    counts["true_positive"] * 100 / agreements if agreements else None
                ),
                "case_ids": groups,
            }
        )
    assert len(report["pairs"]) == len(MODELS) * (len(MODELS) - 1) // 2
    if "recording-0312d144c749c190446edfe0" in report["shared_wrong_cases"]:
        report["shared_wrong_cases"]["recording-0312d144c749c190446edfe0"]["audit"] = {
            "verdict": "Shared under-inclusion, with ambiguous publication-policy input.",
            "body": "All seven identify the Bond Streets subcommittee correctly.",
            "omissions": "Six select only its own feed; gold also requires two broader feeds.",
            "sol": "Sol adds Bond Program but omits Transportation and Infrastructure.",
            "input_gap": (
                "Generic family policy does not explicitly map this subcommittee to the separate "
                "Transportation and Infrastructure feed; body identity prohibits topic-only joins."
            ),
            "resolution": (
                "Keep frozen score; audit mapping policy separately before qualification."
            ),
        }
    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_exclusive(args.out_dir / "report.json", report)
    with (args.out_dir / "pairs.csv").open("x", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(["model_a", "model_b", "true_positive", "false_positive", "negative"])
        for pair in report["pairs"]:
            writer.writerow([*pair["models"], *pair["counts"].values()])
    lines = [
        "# Pair agreement on the shared answered set",
        "",
        f"All seven models completed these same {len(common)} recordings. Qwen is excluded.",
        "Agreement means the same publication action and exact unordered feed set.",
        "Free-text names, rationale wording and citation wording are not equality tests.",
        "True positive = agreement with frozen truth; false positive = shared wrong answer;",
        "negative = different recommendations. All rates use the common set as denominator.",
        "",
        "| Pair | True positive | False positive | Negative |",
        "|---|---:|---:|---:|",
    ]
    for pair in report["pairs"]:
        label = " + ".join(LABELS[n] for n in pair["models"])
        cells = [
            f"{pair['counts'][k]} ({pair['percent_of_common'][k]:.1f}%)"
            for k in ("true_positive", "false_positive", "negative")
        ]
        lines.append("| " + " | ".join([label, *cells]) + " |")
    lines.extend(
        [
            "",
            "Common-set categories: " + json.dumps(report["common_categories"], sort_keys=True),
            "",
            "Partial completion can exclude hard cases and inflate agreement. No uncertain-owner",
            "guardrail case is in the current common set. No production qualification follows.",
            "The one shared wrong case identifies the correct body but omits broader feed policy.",
            "See report.json for exact pairs, titles, source hashes and unchanged frozen truth.",
        ]
    )
    with (args.out_dir / "REPORT.md").open("x") as output:
        output.write("\n".join(lines) + "\n")
    print(json.dumps({"common": len(common), "categories": report["common_categories"]}))
    for pair in report["pairs"]:
        print(" + ".join(LABELS[n] for n in pair["models"]), pair["counts"])


if __name__ == "__main__":
    main()
