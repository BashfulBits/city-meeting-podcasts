"""Offline Gemini/GLM paired scoring on the frozen 60-case original prompt."""

import json
import sys
from collections import Counter
from pathlib import Path

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[3]
sys.path.insert(0, str(ROOT / "evals/remedy/results/body-selection-2026-10-13"))
from body_selection import load_manifest, score_answer, validate_answer  # noqa: E402


def main():
    cases = {c.case_id: c for c in load_manifest()[1]}
    gold = json.loads(
        (ROOT / "evals/remedy/holdout/body-selection-2026-10-13/gold.json").read_text()
    )
    glm = {}
    directory = ROOT / "evals/remedy/results/prompt-comparison-2026-10-14/batches"
    for path in sorted(directory.glob("*.json")):
        row = json.loads(path.read_text())
        if (
            row.get("candidate") == "glm_flash"
            and row.get("status") == "completed"
            and row.get("mode") == "prompt_comparison/original"
        ):
            glm.setdefault(row["case_id"], row)
    categories = {}
    pairs = Counter()
    details = []
    for case_id, case in cases.items():
        attempts = [
            json.loads(p.read_text()) for p in sorted((OUT / "results").glob(f"{case_id}*.json"))
        ]
        if not attempts:
            continue
        row = next((r for r in attempts if "answer" in r), attempts[-1])
        detail = {
            "case_id": case_id,
            "status": row["status"],
            "identity_matches": row.get("identity_matches"),
        }
        if "answer" in row:
            answer = validate_answer(row["answer"], case)
            truth = gold["cases"][case_id]
            verdict = score_answer(answer, truth)
            passed = verdict.get("correct", verdict.get("guardrail_pass"))
            category = categories.setdefault(verdict["category"], Counter())
            category["answered"] += 1
            category["correct_or_guardrail_pass"] += int(passed)
            detail["verdict"] = verdict
            if truth["expected_feeds"] is not None:
                detail["extra_feeds"] = sorted(
                    set(answer.feed_slugs) - set(truth["expected_feeds"])
                )
                detail["missing_feeds"] = sorted(
                    set(truth["expected_feeds"]) - set(answer.feed_slugs)
                )
            if case_id in glm:
                partner = validate_answer(glm[case_id]["answer"], case)
                agrees = answer.publication_action == partner.publication_action and set(
                    answer.feed_slugs
                ) == set(partner.feed_slugs)
                outcome = (
                    ("correct_agreement" if passed else "wrong_agreement")
                    if agrees
                    else "disagreement"
                )
                pairs[outcome] += 1
                detail["pair_outcome"] = outcome
                detail["owner_identity_review_required"] = True
        details.append(detail)
    report = {
        "target": 60,
        "returned": len(details),
        "categories": categories,
        "pair_action_feed_metrics": pairs,
        "details": details,
        "production_admitted": False,
        "limits": "Reused historical challenge; owner normalization and serving-family "
        "identity require separate review. Transport failures have no quality score.",
    }
    (OUT / "score-current.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {k: report[k] for k in ("target", "returned", "categories", "pair_action_feed_metrics")}
        )
    )


if __name__ == "__main__":
    main()
