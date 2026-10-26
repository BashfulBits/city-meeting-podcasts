"""Read-only progress/quality summary using the measurement's frozen semantic scorer."""

import argparse
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from run_runtime_qualification import ROOT, digest, read, semantic

from citypods.audit_remedy import BodyDecision, _p3_assignment_key


def summarize(directory, plan_path, authority_path):
    authority = read(authority_path)
    if authority["run_plan_sha256"] != digest(plan_path):
        raise ValueError("measurement plan changed")
    if authority["executor_sha256"] != digest(ROOT / "evals/remedy/run_runtime_qualification.py"):
        raise ValueError("the pre-registered semantic scorer changed")
    cases = {c["case_id"]: c for c in read(plan_path.parent / "model-inputs.json")["cases"]}
    expected = {
        c["case_id"]: c
        for c in read(plan_path.parent / "expected-decisions-approved.json")["cases"]
    }
    intents = [read(p) for p in sorted(directory.glob("*-intent.json"))]
    rows = []
    for path in sorted(directory.glob("*-result.json")):
        try:
            rows.append(read(path))
        except json.JSONDecodeError:
            continue  # A controller may still be writing this immutable result.
    roles = defaultdict(Counter)
    verdicts, completed = {}, {}
    for row in rows:
        roles[row["role"]][row["status"]] += 1
        if row["status"] == "completed" and row["role"] != "jev":
            cid = row["case_id"]
            decision = BodyDecision.model_validate(row["normalized"]["decision"])
            verdict = semantic(decision, expected[cid], cases[cid]["evidence"])
            verdicts[(cid, row["role"])] = verdict
            completed[(cid, row["role"])] = decision
    role_quality = {
        role: dict(Counter(v for (_, r), v in verdicts.items() if r == role))
        for role in roles
        if role != "jev"
    }
    intersection = [
        cid for cid in cases if all((cid, r) in completed for r in ("glm_flash", "deepseek"))
    ]
    extra_escalations = [
        cid
        for cid in intersection
        if all(verdicts[(cid, r)] == "correct" for r in ("glm_flash", "deepseek"))
        and _p3_assignment_key(completed[(cid, "glm_flash")])
        != _p3_assignment_key(completed[(cid, "deepseek")])
    ]
    summaries = [read(p) for p in sorted(directory.glob("RQ*-summary.json"))]
    outcomes = Counter(
        row.get("consensus_simulation", {}).get("disposition", row["status"]) for row in summaries
    )
    automatic = [
        row
        for row in summaries
        if row.get("consensus_simulation", {}).get("disposition") == "pr_candidate"
    ]
    automatic_quality = Counter(row["automatic_candidate_semantic_verdict"] for row in automatic)
    providers = {}
    for provider in {i["provider"] for i in intents}:
        calls = [i for i in intents if i["provider"] == provider]
        responses = [
            r for r in rows if r.get("provider") == provider and r.get("http_status") is not None
        ]
        times = [r["elapsed_seconds"] for r in responses]
        providers[provider] = dict(
            attempts=len(calls),
            http_responses=len(responses),
            statuses=dict(Counter(str(r["http_status"]) for r in responses)),
            median_response_seconds=statistics.median(times) if times else None,
        )
    return dict(
        scope="12-case historical runtime challenge; not independent production qualification",
        attempt_budget=48,
        provider_attempts=len(intents),
        result_records=len(rows),
        http_responses=sum(r.get("http_status") is not None for r in rows),
        role_statuses={k: dict(v) for k, v in roles.items()},
        chat_semantic_quality=role_quality,
        identical_initial_case_intersection=intersection,
        correct_semantics_but_exact_initial_disagreement=extra_escalations,
        terminal_cases=len(summaries),
        total_cases=12,
        sandbox_outcomes=dict(outcomes),
        automatic_candidate_semantic_quality=dict(automatic_quality),
        providers=providers,
        production_admitted=False,
        note="Consensus assumes eligibility only inside the sandbox; no admission or deployment.",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--authority", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(summarize(args.directory, args.plan, args.authority), indent=2))
