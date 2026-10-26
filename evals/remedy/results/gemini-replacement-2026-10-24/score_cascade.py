"""Offline model-gate cascade scoring; route admission remains a separate gate."""

import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[3]
sys.path.insert(0, str(ROOT / "evals/remedy/results/body-selection-2026-10-13"))
from body_selection import SelectionInput, score_answer, validate_answer  # noqa: E402


def signature(answer):
    return answer["publication_action"], tuple(sorted(answer["feed_slugs"]))


def main():
    initial = json.loads((OUT / "score-final-initial-pair.json").read_text())
    gold = json.loads(
        (ROOT / "evals/remedy/holdout/body-selection-2026-10-13/gold.json").read_text()
    )
    directory = OUT / "adjudication"
    packet = json.loads((directory / "packet.json").read_text())
    cases = []
    for entry in packet["cases"]:
        cid = entry["case"]["case_id"]
        row = {"case_id": cid, "status": "pending"}
        responses = {}
        for role in ("deepseek", "jev"):
            attempts = [
                json.loads(p.read_text())
                for p in sorted(directory.glob(f"{role}-{cid}*-result.json"))
            ]
            result = next((r for r in attempts if r["status"] == "completed"), None)
            if result:
                responses[role] = result
        if len(responses) == 2:
            deepseek, jev = responses["deepseek"], responses["jev"]
            if (
                not all(
                    r.get("identity_matches") and not r.get("schema_error")
                    for r in responses.values()
                )
                or "answer" not in deepseek
            ):
                row["status"] = "manual_identity_or_schema_hold"
            else:
                answer = validate_answer(
                    deepseek["answer"], SelectionInput.model_validate(entry["case"])
                )
                values = {r["question_id"]: r["value"] for r in jev.get("parsed", [])}
                choice = values.get("choice")
                matched = choice in {"A", "B"} and signature(answer.model_dump()) == signature(
                    entry["proposals"][choice]
                )
                verdict = score_answer(answer, gold["cases"][cid])
                row.update(
                    jev_choice=choice, deepseek_verdict=verdict, matching_full_feed_set=matched
                )
                if matched:
                    correct = verdict.get("correct", verdict.get("guardrail_pass"))
                    row["status"] = "correct_resolution" if correct else "wrong_resolution"
                else:
                    row["status"] = "manual_disagreement_or_abstention"
        cases.append(row)
    report = {
        "initial_pair": initial["pair_action_feed_metrics"],
        "adjudication": cases,
        "total_correct_model_gate_outcomes": 53
        + sum(r["status"] == "correct_resolution" for r in cases),
        "total_wrong_model_gate_outcomes": 4
        + sum(r["status"] == "wrong_resolution" for r in cases),
        "manual_cases": sum(r["status"].startswith("manual") for r in cases),
        "pending_cases": sum(r["status"] == "pending" for r in cases),
        "production_admitted": False,
        "interpretation": "Action/full-feed model gates only; owner normalization, deterministic "
        "validation, identity/family admission and human PR review remain required. "
        "Reused historical cases; no generalization guarantee.",
    }
    (OUT / "cascade-score-current.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
