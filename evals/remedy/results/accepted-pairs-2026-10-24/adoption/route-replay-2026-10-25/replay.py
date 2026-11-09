"""Replay immutable evaluated answers into bounded runtime report proposals; no gold/network."""

import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

from citypods.remedy_evaluation import canonical_hash
from citypods.remedy_p3_runner import P3ChatResponse, adapt_evaluated_body_selection

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[5]
RESULTS = ROOT / "evals/remedy/results"
sys.path.insert(0, str(RESULTS / "body-selection-2026-10-13"))
from body_selection import load_manifest, validate_answer  # noqa: E402


def project(model, case, answer, source, *, stage):
    validate_answer(answer, case)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    decision = adapt_evaluated_body_selection(answer, case.model_dump(mode="json"))
    mapped = decision.action == "single_uid_inclusion"
    if mapped:
        assert set(decision.target_feeds) == set(answer["feed_slugs"])
        assert decision.episode_ids == ["e0"] and not decision.all_observed_episodes
    return {
        "model": model,
        "stage": stage,
        "case_id": case.case_id,
        "source_path": str(source.relative_to(ROOT)),
        "source_sha256": digest,
        "original_action": answer["publication_action"],
        "original_feeds": answer["feed_slugs"],
        "status": "bounded_candidate_needs_source_policy" if mapped else "manual_semantic_hold",
        "runtime_decision": decision.model_dump(mode="json"),
        "runtime_response": P3ChatResponse(decision=decision).model_dump(mode="json"),
        "episode_binding": {
            "e0": {
                k: getattr(case.recordings[0], k)
                for k in (
                    "uid",
                    "provider_guid",
                    "title",
                    "date",
                    "canonical_url",
                )
            }
        },
        "runtime_admitted": False,
    }


def main():
    cases = {c.case_id: c for c in load_manifest()[1]}
    index_path = RESULTS / "glm-full-2026-10-24/all-pairs-final.json"
    index = json.loads(index_path.read_text())
    rows = []
    for key, ref in sorted(index["selected_sources"].items()):
        model, cid = key.split("/", 1)
        path = ROOT / ref["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == ref["sha256"]
        answer = json.loads(path.read_text())["answer"]
        rows.append(project(model, cases[cid], answer, path, stage="initial_comparison"))
    base = RESULTS / "accepted-pairs-2026-10-24"
    for pair in ["glm_flash-deepseek", "glm_flash-glm_full"]:
        packet = json.loads((base / pair / "adjudication/packet.json").read_text())
        by_id = {e["case"]["case_id"]: e for e in packet["cases"]}
        for path in sorted((base / "adjudication").glob(pair + "--*-result.json")):
            response = json.loads(path.read_text())
            entry = by_id[response["case_id"]]
            case = cases[response["case_id"]].model_validate(entry["case"])
            if response["role"] == "jev":
                choice = next(
                    v["value"] for v in response["parsed"] if v["question_id"] == "choice"
                )
                if choice not in {"A", "B"}:
                    raise ValueError("This frozen run contains an unsupported JEV choice")
                answer = entry["proposals"][choice]
            else:
                answer = response["answer"]
            rows.append(project(response["role"], case, answer, path, stage=pair))
    counts = {}
    for row in rows:
        counter = counts.setdefault(row["stage"] + "/" + row["model"], Counter())
        counter[row["status"]] += 1
    report = {
        "version": 1,
        "runtime_schema_hash": canonical_hash(P3ChatResponse.model_json_schema()),
        "initial_valid_answers": 298,
        "adjudication_responses": 24,
        "replayed": len(rows),
        "counts": counts,
        "rows": rows,
        "scope": "Pure response/schema projection on frozen per-recording evidence. "
        "Not source-level taxonomy qualification or evidence for the different runtime prompt.",
        "source_policy_validation": "Source policy validation is required before publication. "
        "The single-UID representation does not establish that a body is truly one-off.",
        "invalid_full_glm_outputs": "Two terminal contract failures remain excluded and held.",
        "new_provider_calls": 0,
        "production_changes": 0,
        "admission_qualified": False,
    }
    assert len(rows) == 322
    (OUT / "runtime-replay.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"replayed": len(rows), "counts": counts}, indent=2))


if __name__ == "__main__":
    main()
