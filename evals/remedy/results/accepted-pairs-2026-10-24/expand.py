"""Expand newly bound proposals with the existing deterministic, gold-free retriever."""

import importlib.util
import json
import shutil
from pathlib import Path

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[3]
spec = importlib.util.spec_from_file_location(
    "accepted_pair_expansion", ROOT / "evals/remedy/holdout/disagreement-2026-10-14/prepare.py"
)
expand = importlib.util.module_from_spec(spec)
spec.loader.exec_module(expand)


def main():
    caches = [
        ROOT / "evals/remedy/holdout/disagreement-2026-10-14/retrieved",
        OUT.parent / "gemini-replacement-2026-10-24/adjudication/retrieved",
    ]
    for pair in ("glm_flash-deepseek", "glm_flash-glm_full"):
        directory = OUT / pair
        packet = json.loads((directory / "packet-original-evidence.json").read_text())
        provenance = json.loads((directory / "provenance.json").read_text())
        mappings = {r["case_id"]: r["proposals"] for r in provenance["cases"]}
        frozen = []
        for entry in packet["cases"]:
            case = expand.SelectionInput.model_validate(entry["case"])
            rows = []
            for letter in "AB":
                source = mappings[case.case_id][letter]
                rows.append(
                    {
                        "answer": entry["proposals"][letter],
                        "candidate": source["model"],
                        "attempt_id": Path(source["path"]).stem,
                    }
                )
            frozen.append((case, rows))
        target = directory / "adjudication"
        target.mkdir(exist_ok=True)
        (target / "retrieved").mkdir(exist_ok=True)
        for cache in caches:
            for path in cache.glob("*"):
                destination = target / "retrieved" / path.name
                if not destination.exists():
                    shutil.copyfile(path, destination)
        expand.OUT = target
        expand.disagreements = lambda cases, rows, selected=frozen: selected
        expand.main()


if __name__ == "__main__":
    main()
