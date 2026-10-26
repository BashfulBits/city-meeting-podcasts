"""Offline pair comparisons with explicit cohort coverage and immutable source provenance."""

import hashlib
import itertools
import json
import sys
from collections import Counter
from pathlib import Path

OUT = Path(__file__).resolve().parent
BASE = OUT.parent
ROOT = OUT.parents[3]
sys.path.insert(0, str(BASE / "body-selection-2026-10-13"))
from body_selection import load_manifest, score_answer, validate_answer  # noqa: E402

LABELS = {
    "sol": "Sol (historical)",
    "glm_flash": "GLM Flash",
    "deepseek": "DeepSeek",
    "gemini": "Gemini 3.8 Flash",
    "glm_full": "Full GLM 5.3",
}


def signature(answer):
    return answer.publication_action, tuple(sorted(answer.feed_slugs))


def main():
    cases = {c.case_id: c for c in load_manifest()[1]}
    gold_path = ROOT / "evals/remedy/holdout/body-selection-2026-10-13/gold.json"
    gold = json.loads(gold_path.read_text())["cases"]
    models = {m: {} for m in LABELS}
    sources = {}

    def add(model, row, path):
        cid = row["case_id"]
        answer = validate_answer(row["answer"], cases[cid])
        if cid in models[model]:
            assert models[model][cid] == answer, "Conflicting completed answers"
            return
        models[model][cid] = answer
        sources[f"{model}/{cid}"] = {
            "path": str(path.relative_to(ROOT)),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    for path in sorted((BASE / "prompt-comparison-2026-10-14/batches").glob("*.json")):
        row = json.loads(path.read_text())
        if (
            row.get("candidate") in {"sol", "glm_flash", "deepseek"}
            and row.get("status") == "completed"
            and row.get("mode") == "prompt_comparison/original"
        ):
            add(row["candidate"], row, path)
    for model, dirs in (
        ("gemini", [BASE / "gemini-replacement-2026-10-24"]),
        ("glm_full", [OUT, OUT / "nvidia"]),
    ):
        for directory in dirs:
            for path in sorted((directory / "results").glob("*.json")):
                row = json.loads(path.read_text())
                if "answer" in row:
                    add(model, row, path)
    common = sorted(set.intersection(*(set(v) for v in models.values())))

    def compare(a, b, ids):
        counts = Counter(correct_agreement=0, wrong_agreement=0, disagreement=0)
        details = {key: [] for key in counts}
        for cid in ids:
            left, right = models[a][cid], models[b][cid]
            if signature(left) != signature(right):
                outcome = "disagreement"
            else:
                verdict = score_answer(left, gold[cid])
                passed = verdict.get("correct", verdict.get("guardrail_pass", False))
                outcome = "correct_agreement" if passed else "wrong_agreement"
            counts[outcome] += 1
            details[outcome].append(cid)
        return {"n": len(ids), "counts": counts, "case_ids": details}

    pairs = []
    for a, b in itertools.combinations(models, 2):
        ids = sorted(set(models[a]) & set(models[b]))
        native = compare(a, b, ids)
        native["holds"] = len(cases) - len(ids)
        native["held_case_ids"] = sorted(set(cases) - set(ids))
        accepted = native["counts"]["correct_agreement"] + native["counts"]["wrong_agreement"]
        native["accepted_error_rate"] = native["counts"]["wrong_agreement"] / accepted
        assert sum(native["counts"].values()) + native["holds"] == 60
        pairs.append({"models": [a, b], "cohort60": native, "common58": compare(a, b, common)})
    report = {
        "scope": "Original-prompt action and exact unordered feed set; offline reused cohort",
        "target": 60,
        "coverage": {m: len(v) for m, v in models.items()},
        "common_case_ids": common,
        "gold_sha256": hashlib.sha256(gold_path.read_bytes()).hexdigest(),
        "pairs": pairs,
        "selected_sources": sources,
        "limitations": [
            "Sol is historical and unavailable; full GLM combines separately labeled providers.",
            "Full GLM has 58 valid outputs and two contract holds, not two availability failures.",
            "Use common58 to compare all pairs fairly; native cohort60 preserves real coverage.",
            "Mixed full-GLM 16k/32k profiles remain in score-final.json.",
            "Agreement does not establish owner identity or bypass deterministic/admission gates.",
            "JEV only has disagreement-choice judgments; no independent 60-case pair is inferred.",
            "Older seven-model audit is a separate 40-case common cohort/profile.",
            "Retrospective results do not establish held-out production precision.",
        ],
    }
    (OUT / "all-pairs-final.json").write_text(json.dumps(report, indent=2) + "\n")
    lines = [
        "# All original-prompt model pairs",
        "",
        "Agreement = exact publication action and feed set. Wrong agreements would be accepted",
        "errors under an agreement-only gate; disagreements and invalid outputs "
        "require escalation.",
        "",
        "| Pair | Correct | Wrong | Disagree | Contract hold | Wrong / accepted |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for p in pairs:
        c = p["cohort60"]["counts"]
        pair = " + ".join(LABELS[m] for m in p["models"])
        rate = p["cohort60"]["accepted_error_rate"]
        lines.append(
            f"| {pair} | {c['correct_agreement']} | {c['wrong_agreement']} | "
            f"{c['disagreement']} | {p['cohort60']['holds']} | {rate:.1%} |"
        )
    lines += ["", "## Same 58 cases for every pair", ""]
    for p in pairs:
        c = p["common58"]["counts"]
        lines.append(f"- {' + '.join(p['models'])}: {dict(c)}")
    lines += ["", "## Limits", ""] + [f"- {s}" for s in report["limitations"]]
    lines += [
        "",
        "Historical seven-model comparison (Flash-Lite, Kimi, older full GLM, Flash, Sol,",
        "DeepSeek, GPT-OSS): ../body-selection-2026-10-13/",
        "pair-audit-2026-10-14T0900Z/REPORT.md. Do not pool its 40-case counts with this table.",
    ]
    (OUT / "all-pairs-final.md").write_text("\n".join(lines) + "\n")
    print(json.dumps({"coverage": report["coverage"], "pairs": pairs}, indent=2))


if __name__ == "__main__":
    main()
