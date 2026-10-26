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
    jev_quality = Counter()
    for row in rows:
        if row["role"] != "jev" or row["status"] != "completed":
            continue
        answers = row["normalized"]["answers"]
        answer = next(iter(answers.values()))
        selected_role = {"A": "glm_flash", "B": "deepseek"}.get(answer.get("choice"))
        key = (row["case_id"], selected_role)
        jev_quality[verdicts[key] if key in verdicts else "no_supported_proposal"] += 1
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
        jev_selected_proposal_semantic_quality=dict(jev_quality),
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


def freeze_additional(directory, plan_path, authority_path, baseline_path, output_path):
    """Keep only irreproducible new observations beyond an immutable compact baseline."""
    import fcntl

    with (directory / "controller.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for intent in directory.glob("*-intent.json"):
            result = intent.with_name(intent.name.replace("-intent.json", "-result.json"))
            if not result.exists() or read(result).get("provider_outcome") == "unknown":
                raise ValueError("cannot freeze unresolved provider requests")
        baseline = read(baseline_path)
        known = {(r["case_id"], r["role"]) for r in baseline["observed_results"]}
        results = []
        for path in sorted(directory.glob("*-result.json")):
            row = read(path)
            if (row["case_id"], row["role"]) in known:
                old = next(
                    r
                    for r in baseline["observed_results"]
                    if (r["case_id"], r["role"]) == (row["case_id"], row["role"])
                )
                if old["original_result_sha256"] != digest(path):
                    raise ValueError("baseline result changed")
                continue
            keep = {
                k: v
                for k, v in row.items()
                if k not in {"raw_response", "partial_response", "started_at", "finished_at"}
            }
            keep["original_result_sha256"] = digest(path)
            results.append(keep)
        known_cases = {c["case_id"] for c in baseline["case_outcomes"]}
        outcomes = []
        for path in sorted(directory.glob("RQ*-summary.json")):
            row = read(path)
            if row["case_id"] in known_cases:
                continue
            consensus = row.get("consensus_simulation", {})
            outcomes.append(
                dict(
                    case_id=row["case_id"],
                    semantic_verdicts=row.get("semantic_verdicts", {}),
                    disposition=consensus.get("disposition", row["status"]),
                    reason=consensus.get("reason"),
                    automatic_candidate_semantic_verdict=row.get(
                        "automatic_candidate_semantic_verdict"
                    ),
                    original_summary_sha256=digest(path),
                )
            )
        artifact = dict(
            schema_version=1,
            baseline_sha256=digest(baseline_path),
            observed_results=results,
            case_outcomes=outcomes,
            cumulative_summary=summarize(directory, plan_path, authority_path),
            note="Additional normalized observations; raw receipts remain local. Original scores.",
        )
        with output_path.open("x") as stream:
            stream.write(json.dumps(artifact, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--authority", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--freeze-additional", type=Path)
    args = parser.parse_args()
    if args.freeze_additional:
        if not args.baseline:
            parser.error("--freeze-additional requires --baseline")
        freeze_additional(
            args.directory, args.plan, args.authority, args.baseline, args.freeze_additional
        )
    print(json.dumps(summarize(args.directory, args.plan, args.authority), indent=2))
