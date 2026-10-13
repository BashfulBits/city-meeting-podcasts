"""Resume only missing Gemini cases with new, explicit subset provenance."""

import json
import os
import subprocess
from pathlib import Path

import yaml

from citypods.remedy_evaluation import Manifest, RemedyConfig, canonical_hash

ROOT = Path(__file__).resolve().parents[4]
OUT = Path(__file__).resolve().parent
OLD = ROOT / "evals/remedy/results/p1-comparison-2026-10-10/artifacts"
manifest = Manifest.model_validate_json(
    (ROOT / "evals/remedy/holdout/p1-denton-2026-10-10/manifest.json").read_text()
)
catalog_hash = canonical_hash(json.loads((ROOT / "citypods/compute/llm_routes.json").read_text()))
plans = []
for mode, original in (
    ("claim_support", "p1-denton-current-day-final.json"),
    ("blind_owner", "p1-denton-blind-current-day-final.json"),
):
    prior = json.loads((OLD / original).read_text())
    config = RemedyConfig.model_validate(
        yaml.safe_load((OLD / f"p1-four-route-holdout-{mode}.yml").read_text())
    )
    known = {canonical_hash(entry.model_dump()) for entry in config.admissions}
    if any(row["configuration_id"] not in known for row in prior["results"]):
        raise SystemExit("Unknown original configuration; refusing calls")
    for entry in config.admissions:
        configuration = canonical_hash(entry.model_dump())
        missing = {
            row["case_id"]
            for row in prior["results"]
            if row["configuration_id"] == configuration and row["status"] != "completed"
        }
        if not missing:
            continue
        subset = manifest.model_copy(
            update={"cases": [c for c in manifest.cases if c.id in missing]}
        )
        candidate = entry.model_copy(
            update={
                "catalog_hash": catalog_hash,
                "manifest_hash": canonical_hash(subset.model_dump()),
            }
        )
        prefix = entry.physical_route_ids[0] + "-" + mode
        m = OUT / (prefix + "-manifest.json")
        admission = OUT / (prefix + "-admission.json")
        result = OUT / (prefix + "-results.json")
        if any(p.exists() for p in (m, admission, result)):
            raise SystemExit("Existing immutable output; refusing duplicate")
        m.write_text(subset.model_dump_json(indent=2))
        admission.write_text(
            config.model_copy(update={"admissions": [candidate]}).model_dump_json(indent=2)
        )
        plans.append((m, admission, result, mode, prefix))
(OUT / "selection.json").write_text(
    json.dumps(
        {
            "parent_result_hashes": {
                n: canonical_hash(json.loads((OLD / n).read_text()))
                for n in (
                    "p1-denton-current-day-final.json",
                    "p1-denton-blind-current-day-final.json",
                )
            },
            "max_cases_per_route_mode": 2,
            "reason": "Conservative initial round; keys may share project-level daily capacity",
        },
        indent=2,
    )
)
for m, admission, result, mode, prefix in plans:
    args = [
        "python",
        str(ROOT / "scripts/eval_remedy.py"),
        "run",
        "--manifest",
        str(m),
        "--admission",
        str(admission),
        "--role",
        "proposer",
        "--mode",
        mode,
        "--out",
        str(result),
        "--max-cases",
        "2",
        "--request-timeout",
        "120",
        "--case-deadline",
        "150",
        "--checkpoint-dir",
        str(OUT / (prefix + "-observations")),
    ]
    with (OUT / (prefix + "-log.txt")).open("x") as log:
        status = subprocess.call(
            args + ["--live"], cwd=ROOT, env=os.environ, stdout=log, stderr=subprocess.STDOUT
        )
    print(prefix, "exit", status, flush=True)
    if status:
        raise SystemExit(status)
