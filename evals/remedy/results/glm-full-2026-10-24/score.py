"""Compare full GLM and Flash pair errors on exactly the same completed cohort."""

import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[3]
BASE = OUT.parent
sys.path.insert(0, str(BASE / "body-selection-2026-10-13"))
from body_selection import load_manifest, score_answer, validate_answer  # noqa: E402


def signature(answer):
    return answer["publication_action"], tuple(sorted(answer["feed_slugs"]))


def main():
    cases = {c.case_id: c for c in load_manifest()[1]}
    gold_path = ROOT / "evals/remedy/holdout/body-selection-2026-10-13/gold.json"
    gold = json.loads(gold_path.read_text())["cases"]
    models = {m: {} for m in ("glm_full", "glm_flash", "deepseek", "gemini")}
    for path in sorted((BASE / "prompt-comparison-2026-10-14/batches").glob("*.json")):
        row = json.loads(path.read_text())
        if (
            row.get("candidate") in {"glm_flash", "deepseek"}
            and row.get("status") == "completed"
            and row.get("mode") == "prompt_comparison/original"
        ):
            models[row["candidate"]].setdefault(row["case_id"], row["answer"])
    selected_profiles = {}
    for model, directory in (("gemini", BASE / "gemini-replacement-2026-10-24"), ("glm_full", OUT)):
        for path in sorted(directory.glob("**/results/*.json")):
            row = json.loads(path.read_text())
            if "answer" in row:
                validate_answer(row["answer"], cases[row["case_id"]])
                models[model].setdefault(row["case_id"], row["answer"])
                if model == "glm_full" and row["case_id"] not in selected_profiles:
                    intent_path = path.parent.parent / "intents" / path.name
                    intent = json.loads(intent_path.read_text())
                    selected_profiles[row["case_id"]] = {
                        "provider": "nvidia" if path.parent.parent.name == "nvidia" else "beatapi",
                        "output_cap": intent["request"]["max_tokens"],
                        "streaming": intent["request"]["stream"],
                        "result_path": str(path.relative_to(ROOT)),
                        "intent_sha256": hashlib.sha256(intent_path.read_bytes()).hexdigest(),
                    }
    common = sorted(set.intersection(*(set(rows) for rows in models.values())))
    categories = {}
    for cid in common:
        verdict = score_answer(validate_answer(models["glm_full"][cid], cases[cid]), gold[cid])
        counts = categories.setdefault(verdict["category"], Counter())
        counts["answered"] += 1
        counts["passed"] += int(verdict.get("correct", verdict.get("guardrail_pass", False)))
    pairs = []
    for partner in ("deepseek", "gemini"):
        for glm in ("glm_full", "glm_flash"):
            counts = Counter()
            wrong, disagreements = [], []
            for cid in common:
                a, b = models[glm][cid], models[partner][cid]
                if signature(a) != signature(b):
                    counts["disagreement"] += 1
                    disagreements.append(cid)
                    continue
                verdict = score_answer(validate_answer(a, cases[cid]), gold[cid])
                passed = verdict.get("correct", verdict.get("guardrail_pass", False))
                counts["correct_agreement" if passed else "wrong_agreement"] += 1
                if not passed:
                    wrong.append(cid)
            pairs.append(
                {
                    "models": [glm, partner],
                    "counts": counts,
                    "shared_wrong_case_ids": wrong,
                    "disagreement_case_ids": disagreements,
                }
            )
    results = [json.loads(p.read_text()) for p in OUT.glob("**/results/*.json")]
    contract_failures = []
    budget_holds = []
    for path in sorted(OUT.glob("**/results/*.json")):
        result = json.loads(path.read_text())
        if "answer" in result or result.get("status") != "completed":
            continue
        finish = [c.get("finish_reason") for c in result.get("raw_response", {}).get("choices", [])]
        if "stop" in finish and result.get("schema_error"):
            contract_failures.append(
                {
                    "case_id": result["case_id"],
                    "path": str(path.relative_to(ROOT)),
                    "schema_error": result["schema_error"],
                }
            )
        elif "length" in finish:
            intent = json.loads((path.parent.parent / "intents" / path.name).read_text())
            budget_holds.append(
                {
                    "case_id": result["case_id"],
                    "output_cap": intent["request"]["max_tokens"],
                    "path": str(path.relative_to(ROOT)),
                }
            )
    report = {
        "completed_contract_failures": contract_failures,
        "completed_output_budget_holds": budget_holds,
        "quality_note": "Invalid completed answers are retained as contract failures. "
        "They are not retried "
        "to obtain a better answer. They fail the schema gate and require manual review; "
        "report separately from transient availability and scored valid answers.",
        "target": 60,
        "valid_full_glm": len(models["glm_full"]),
        "matched_case_count": len(common),
        "selected_profiles": selected_profiles,
        "profile_counts": Counter(
            f"{p['provider']}/max_tokens={p['output_cap']}" for p in selected_profiles.values()
        ),
        "common_case_ids": common,
        "full_glm_categories": categories,
        "pairs": pairs,
        "attempts_returned": len(results),
        "identity_mismatch_attempts": sum(
            not r.get("identity_matches") for r in results if r.get("http_status") == 200
        ),
        "gold_sha256": hashlib.sha256(gold_path.read_bytes()).hexdigest(),
        "production_admitted": False,
        "interpretation": "Original prompt; full GLM and Flash use the same completed case "
        "intersection. Quality excludes transport failures. Reused historical "
        "packet;16,384 and approved32,768 output profiles labeled separately; "
        "owner normalization and serving-family admission separate.",
    }
    (OUT / "score-current.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {k: report[k] for k in ("target", "valid_full_glm", "full_glm_categories", "pairs")}
        )
    )


if __name__ == "__main__":
    main()
