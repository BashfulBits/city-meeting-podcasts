"""A maintainer exception must not rewrite raw gold or accept a different proposal/case."""

import importlib.util
import json
from pathlib import Path


def test_comparison_scopes_acceptance_and_keeps_original_score(tmp_path, monkeypatch):
    directory = Path(__file__).parents[1] / "evals/remedy"
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location(
        "runtime_comparison_test", directory / "score_runtime_qualification.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def write(name, data):
        path = tmp_path / name
        path.write_text(json.dumps(data))
        return path

    decision = {
        "finding_id": "f0",
        "action": "union",
        "target_feeds": ["a", "b"],
        "rationale": "Proposal",
    }
    rows = [
        {
            "case_id": cid,
            "role": "gemini",
            "status": "completed",
            "normalized": {"decision": decision},
        }
        for cid in ["RQ04", "RQ06"]
    ]
    baseline = write("baseline.json", {"observed_results": rows})
    candidate = write("candidate.json", {"observed_results": rows})
    acceptance = write(
        "acceptance.json", {"case_id": "RQ06", "maintainer_disposition": "accepted_exception"}
    )
    plan = write("plan.json", {})
    write(
        "model-inputs.json",
        {"cases": [{"case_id": cid, "evidence": {}} for cid in ["RQ04", "RQ06"]]},
    )
    write(
        "expected-decisions-approved.json",
        {
            "cases": [
                {
                    "case_id": cid,
                    "decision": {
                        "finding_id": "f0",
                        "action": "manual_review",
                        "rationale": "Control",
                    },
                }
                for cid in ["RQ04", "RQ06"]
            ]
        },
    )
    first = module.compare_runs([baseline], candidate, plan, acceptance)["roles"]["gemini"]
    assert first["concise"]["raw"] == {"wrong": 2}
    assert first["concise"]["reviewed"] == {"wrong": 1, "accepted_exception": 1}
    assert first["concise"]["wrong_case_ids"] == ["RQ04"]
    changed = json.loads(candidate.read_text())
    changed["observed_results"][1]["normalized"]["decision"]["target_feeds"] = ["a"]
    candidate.write_text(json.dumps(changed))
    second = module.compare_runs([baseline], candidate, plan, acceptance)["roles"]["gemini"]
    assert second["concise"]["reviewed"] == {"wrong": 2}
