"""Offline accepted-pair cascade scoring; approval exceptions never enter judge inputs."""

import json
import sys
from collections import Counter
from pathlib import Path

OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(OUT.parent / "body-selection-2026-10-13"))
from body_selection import SelectionInput, score_answer, validate_answer  # noqa: E402


def signature(answer):
    return answer["publication_action"], tuple(sorted(answer["feed_slugs"]))


def main():
    approval = json.loads((OUT / "adjudication-approval.json").read_text())
    acceptance = json.loads((OUT / "maintainer-acceptance.json").read_text())
    gold = json.loads(
        (OUT.parents[3] / "evals/remedy/holdout/body-selection-2026-10-13/gold.json").read_text()
    )["cases"]
    reports = []
    for initial in acceptance["accepted_pairs"]:
        pair = "-".join(initial["models"])
        judges = approval["judge_mapping"][pair]
        chat_role = next(j for j in judges if j != "jev")
        frozen = json.loads((OUT / pair / "adjudication/packet.json").read_text())
        rows = []
        for entry in frozen["cases"]:
            cid = entry["case"]["case_id"]
            row = {"case_id": cid, "status": "pending"}
            responses = {}
            for role in judges:
                paths = sorted((OUT / "adjudication").glob(f"{pair}--{role}-{cid}*-result.json"))
                attempts = [json.loads(p.read_text()) for p in paths]
                completed = next((r for r in attempts if r["status"] == "completed"), None)
                if completed:
                    responses[role] = completed
            if len(responses) == 2:
                chat, jev = responses[chat_role], responses["jev"]
                if (
                    not all(
                        r.get("identity_matches") and not r.get("schema_error")
                        for r in responses.values()
                    )
                    or "answer" not in chat
                ):
                    row["status"] = "manual_identity_or_contract_hold"
                else:
                    answer = validate_answer(
                        chat["answer"], SelectionInput.model_validate(entry["case"])
                    )
                    choice = next(
                        (v["value"] for v in jev.get("parsed", []) if v["question_id"] == "choice"),
                        None,
                    )
                    matched = choice in {"A", "B"} and signature(answer.model_dump()) == signature(
                        entry["proposals"][choice]
                    )
                    verdict = score_answer(answer, gold[cid])
                    row.update(jev_choice=choice, original_verdict=verdict, exact_match=matched)
                    if matched:
                        passed = verdict.get("correct", verdict.get("guardrail_pass", False))
                        row["status"] = "correct_resolution" if passed else "wrong_resolution"
                    else:
                        row["status"] = "manual_disagreement_or_abstention"
            rows.append(row)
        counts = Counter(r["status"] for r in rows)
        reports.append(
            {
                "pair": pair,
                "initial_original_counts": initial["original_scored_outcomes"],
                "initial_pair_specific_accepted_exceptions": initial[
                    "accepted_scored_wrong_case_ids"
                ],
                "initial_contract_holds": initial["contract_holds"],
                "judges": judges,
                "adjudication": rows,
                "counts": counts,
                "note": "New wrong resolutions are not covered by acceptance "
                "of initial agreements.",
            }
        )
    results = list((OUT / "adjudication").glob("*-result.json"))
    completed = sum(json.loads(p.read_text())["status"] == "completed" for p in results)
    report = {
        "planned_judgments": 24,
        "completed_responses": completed,
        "returned_attempts": len(results),
        "pairs": reports,
        "production_admitted": False,
    }
    (OUT / "score-current.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "completed": completed,
                "planned": 24,
                "pairs": [{"pair": r["pair"], "counts": r["counts"]} for r in reports],
            }
        )
    )


if __name__ == "__main__":
    main()
