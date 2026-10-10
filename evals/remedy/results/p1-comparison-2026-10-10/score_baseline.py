"""Score answer content after a diagnostic; identity/control holds remain explicit."""

import argparse
import json
from pathlib import Path

from citypods.compute.structured import parse_structured_json
from citypods.remedy_evaluation import (
    Gold,
    Manifest,
    canonical_hash,
    compare_results,
    prompt_case_id,
    validate_answer,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        raise SystemExit("Refusing existing report")
    data = json.loads(args.results.read_text())
    manifest = Manifest.model_validate_json(args.manifest.read_text())
    gold = Gold.model_validate_json(args.gold.read_text())
    cases = {case.id: case for case in manifest.cases}
    reports = []
    for mode in ("claim_support", "blind_owner"):
        rows = []
        for row in data["results"]:
            if row["mode"] != mode:
                continue
            scored = {"case_id": row["case_id"], "status": "failed"}
            raw = row.get("raw_response", {})
            # This is content-only scoring, not alias verification or qualification.
            if raw.get("model") == "glm-5.3-flash":
                try:
                    case = cases[row["case_id"]]
                    answer = parse_structured_json(raw["choices"][0]["message"]["content"])
                    parsed = validate_answer(
                        answer, case.model_copy(update={"id": prompt_case_id(case, mode)})
                    )
                    scored.update(
                        status="completed",
                        answer=parsed.model_copy(update={"case_id": case.id}).model_dump(),
                    )
                except (KeyError, IndexError, TypeError, ValueError):
                    pass
            rows.append(scored)
        if rows:
            reports.append(
                {
                    "mode": mode,
                    "report": compare_results(
                        rows, gold, manifest=manifest, mode=mode
                    ).model_dump(),
                }
            )
    report = {
        "kind": "content-only-baseline-report",
        "qualified": False,
        "identity_gate": "held: shortened returned model ID",
        "reasoning_gate": "held: default, maximum not verified",
        "results_hash": canonical_hash(data),
        "manifest_hash": canonical_hash(manifest.model_dump()),
        "gold_hash": canonical_hash(gold.model_dump()),
        "reports": reports,
    }
    with args.out.open("x") as output:
        json.dump(report, output, indent=2)


if __name__ == "__main__":
    main()
