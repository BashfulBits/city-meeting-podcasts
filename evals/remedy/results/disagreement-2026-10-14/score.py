"""Offline scoring of full DeepSeek assignments versus bounded JEV candidate choices."""

import argparse
import json
from pathlib import Path

import run
from body_selection import score_answer, validate_answer, write_exclusive

from citypods.remedy_evaluation import canonical_hash


def expected_choice(entry, truth):
    acceptable = []
    for letter, proposal in entry["proposals"].items():
        verdict = score_answer(
            validate_answer(proposal, run.SelectionInput.model_validate(entry["case"])), truth
        )
        if verdict.get("correct", verdict.get("guardrail_pass")):
            acceptable.append(letter)
    return acceptable or ["C"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    frozen = json.loads(run.PACKET_FILE.read_text())
    gold = json.loads(args.gold.read_text())
    rows, unknown = run.history()
    latest = {}
    for row in sorted(rows, key=lambda r: r["finished_at"]):
        latest[(row["candidate"], row["case_id"])] = row
    report = {
        "binding": canonical_hash(frozen),
        "gold_hash": canonical_hash(gold),
        "qualified": False,
        "unknown_intents": len(unknown),
        "cases": [],
    }
    for entry in frozen["cases"]:
        cid = entry["case"]["case_id"]
        truth = gold["cases"][cid]
        own = {
            "case_id": cid,
            "city": entry["case"]["city"],
            "title": entry["case"]["recordings"][0]["title"],
            "expected_choice": expected_choice(entry, truth),
            "category": truth["category"],
        }
        for model in ("deepseek", "jev"):
            row = latest.get((model, cid))
            if row is None or row["status"] != "completed":
                own[model] = {"status": row["status"] if row else "missing"}
                continue
            if model == "deepseek":
                answer = validate_answer(
                    row["answer"], run.SelectionInput.model_validate(entry["case"])
                )
                verdict = score_answer(answer, truth)
                original = row["answer"]
                own[model] = {
                    "status": "completed",
                    "verdict": verdict,
                    "choice": run.choice_for(original, entry),
                    "answer": original,
                    "usage": row.get("provider_usage"),
                    "provider_seconds": sum(d["duration_seconds"] for d in row["diagnostics"]),
                    "end_to_end_seconds": row["end_to_end_seconds"],
                    "identity_held": row["identity_held"],
                    "serving_family_held": row["serving_family_held"],
                    "extra_feeds": sorted(
                        set(answer.feed_slugs) - set(truth["expected_feeds"] or [])
                    )
                    if truth["category"] != "insufficient_owner_proof"
                    else None,
                    "missing_feeds": sorted(
                        set(truth["expected_feeds"] or []) - set(answer.feed_slugs)
                    )
                    if truth["category"] != "insufficient_owner_proof"
                    else None,
                }
            else:
                answers = {r["question_id"]: r["value"] for r in row["parsed"]}
                own[model] = {
                    "status": "completed",
                    "answers": answers,
                    "choice_correct": answers.get("choice") in own["expected_choice"],
                    "raw_usage": row["raw_response"].get("usage"),
                    "enqueue_seconds": row["elapsed_seconds"],
                    "end_to_end_seconds": row.get("end_to_end_seconds", row["elapsed_seconds"]),
                    "scope": "Bounded candidate choice, not a free-form feed assignment",
                }
        report["cases"].append(own)
    write_exclusive(args.out, report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
