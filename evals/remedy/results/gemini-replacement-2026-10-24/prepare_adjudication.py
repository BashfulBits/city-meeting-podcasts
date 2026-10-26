"""Bind actual Gemini/GLM proposals to the existing deterministic evidence expansion."""

import importlib.util
import json
from pathlib import Path

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[3]
spec = importlib.util.spec_from_file_location(
    "replacement_evidence_expansion",
    ROOT / "evals/remedy/holdout/disagreement-2026-10-14/prepare.py",
)
expand = importlib.util.module_from_spec(spec)
spec.loader.exec_module(expand)


def pairs(cases, historical):
    glm = {}
    for row in sorted(historical, key=lambda r: r.get("finished_at", "")):
        if (
            row.get("candidate") == "glm_flash"
            and row.get("status") == "completed"
            and row.get("mode") == "prompt_comparison/original"
        ):
            glm.setdefault(row["case_id"], row)
    gemini = {}
    for path in sorted((OUT / "results").glob("*.json")):
        row = json.loads(path.read_text())
        if "answer" in row:
            gemini.setdefault(
                row["case_id"], row | {"candidate": "gemini", "attempt_id": path.stem}
            )
    if len(gemini) != 60:
        raise RuntimeError("Complete all 60 Gemini answers before freezing disagreement packets")
    result = []
    for case in sorted(cases, key=lambda c: c.case_id):
        pair = [gemini[case.case_id], glm[case.case_id]]
        signatures = [
            (r["answer"]["publication_action"], sorted(r["answer"]["feed_slugs"])) for r in pair
        ]
        if signatures[0] == signatures[1]:
            continue
        if int(expand.canonical_hash(case.case_id)[0], 16) % 2:
            pair.reverse()
        result.append((case, pair))
    return result


if __name__ == "__main__":
    expand.OUT = OUT / "adjudication"
    expand.OUT.mkdir(exist_ok=True)
    expand.disagreements = pairs
    expand.main()
