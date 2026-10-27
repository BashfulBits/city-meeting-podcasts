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
    if authority["approved_expectations_sha256"] != digest(
        plan_path.parent / "expected-decisions-approved.json"
    ) or read(plan_path)["model_inputs_sha256"] != digest(plan_path.parent / "model-inputs.json"):
        raise ValueError("frozen expectations or model inputs changed")
    cases = {c["case_id"]: c for c in read(plan_path.parent / "model-inputs.json")["cases"]}
    expected = {
        c["case_id"]: c
        for c in read(plan_path.parent / "expected-decisions-approved.json")["cases"]
    }
    intents = [read(p) for p in sorted(directory.rglob("*-intent.json"))]
    rows = []
    for path in sorted(directory.rglob("*-result.json")):
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
    """Freeze normalized observations; optionally exclude an immutable compact baseline."""
    import fcntl

    with (directory / "controller.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for intent in directory.rglob("*-intent.json"):
            result = intent.with_name(intent.name.replace("-intent.json", "-result.json"))
            if not result.exists() or read(result).get("provider_outcome") == "unknown":
                raise ValueError("cannot freeze unresolved provider requests")
        baseline = (
            read(baseline_path) if baseline_path else {"observed_results": [], "case_outcomes": []}
        )
        known = {(r["case_id"], r["role"]) for r in baseline["observed_results"]}
        results = []
        for path in sorted(directory.rglob("*-result.json")):
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
            if isinstance(row.get("raw_response"), dict) and "error" in row["raw_response"]:
                keep["provider_error"] = row["raw_response"]["error"]
            if isinstance(row.get("raw_response"), dict) and "usage" in row["raw_response"]:
                keep["usage"] = row["raw_response"]["usage"]
            intent = path.with_name(path.name.replace("-result.json", "-intent.json"))
            if intent.exists():
                previous = read(intent)
                keep["original_intent_sha256"] = digest(intent)
                for key in ("payload_sha256", "recipe_hash", "ordinal", "timeout_seconds"):
                    keep[key] = previous.get(key)
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
            baseline_sha256=digest(baseline_path) if baseline_path else None,
            run_plan_sha256=digest(plan_path),
            authority_sha256=digest(authority_path),
            observed_results=results,
            case_outcomes=outcomes,
            cumulative_summary=summarize(directory, plan_path, authority_path),
            note="Normalized observations; raw receipts local. Original scores, not acceptance.",
        )
        with output_path.open("x") as stream:
            stream.write(json.dumps(artifact, indent=2) + "\n")


def compare_runs(baseline_paths, candidate_path, plan_path, acceptance_path):
    """Compare completed judgments on identical cases; keep original and accepted views apart."""
    baseline = [r for path in baseline_paths for r in read(path)["observed_results"]]
    candidate = read(candidate_path)["observed_results"]
    acceptance = read(acceptance_path)
    cid_accepted = acceptance["case_id"]

    def signature(decision):
        return {k: v for k, v in decision.items() if k not in {"rationale", "finding_id"}}

    accepted_signatures = [
        signature(r["normalized"]["decision"])
        for r in baseline
        if r["case_id"] == cid_accepted and r["role"] != "jev" and r["status"] == "completed"
    ]
    if acceptance.get("maintainer_disposition") != "accepted_exception":
        raise ValueError("missing separate observed-case acceptance")
    cases = {c["case_id"]: c for c in read(plan_path.parent / "model-inputs.json")["cases"]}
    wanted = {
        c["case_id"]: c
        for c in read(plan_path.parent / "expected-decisions-approved.json")["cases"]
    }

    def judgments(rows):
        decisions = {
            (r["case_id"], r["role"]): r["normalized"]["decision"]
            for r in rows
            if r["status"] == "completed" and r["role"] != "jev"
        }
        for r in rows:
            if r["status"] == "completed" and r["role"] == "jev":
                answer = next(iter(r["normalized"]["answers"].values()))
                role = {"A": "glm_flash", "B": "deepseek"}.get(answer.get("choice"))
                if (r["case_id"], role) in decisions:
                    decisions[(r["case_id"], "jev")] = decisions[(r["case_id"], role)]
        result = {}
        for (cid, role), decision in decisions.items():
            raw = semantic(
                BodyDecision.model_validate(decision), wanted[cid], cases[cid]["evidence"]
            )
            accepted = (
                raw == "wrong"
                and cid == cid_accepted
                and signature(decision) in accepted_signatures
            )
            result[(cid, role)] = dict(raw=raw, reviewed="accepted_exception" if accepted else raw)
        return result

    first, second = judgments(baseline), judgments(candidate)
    report = {}
    for role in ("glm_flash", "deepseek", "gemini", "jev"):
        common = sorted(cid for cid, r in first if r == role and (cid, role) in second)
        roles = {}
        for label, scores in (("baseline", first), ("concise", second)):
            selected = {cid: v for (cid, r), v in scores.items() if r == role}
            roles[label] = dict(
                completed=len(selected),
                raw=dict(Counter(v["raw"] for v in selected.values())),
                reviewed=dict(Counter(v["reviewed"] for v in selected.values())),
                wrong_case_ids=[cid for cid, v in selected.items() if v["reviewed"] == "wrong"],
                matched_reviewed=dict(Counter(scores[(cid, role)]["reviewed"] for cid in common)),
            )
        report[role] = dict(identical_case_intersection=common, **roles)
    return dict(
        roles=report,
        acceptance_sha256=digest(acceptance_path),
        note="Completed judgments only; matched-case comparison, not independent causal proof.",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--authority", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--freeze-additional", type=Path)
    parser.add_argument("--freeze", type=Path, help="freeze a complete independent run")
    parser.add_argument("--compare-baseline", type=Path, action="append")
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--acceptance", type=Path)
    args = parser.parse_args()
    if args.freeze and args.freeze_additional:
        parser.error("choose either --freeze or --freeze-additional")
    if args.freeze:
        freeze_additional(args.directory, args.plan, args.authority, None, args.freeze)
    if args.freeze_additional:
        if not args.baseline:
            parser.error("--freeze-additional requires --baseline")
        freeze_additional(
            args.directory, args.plan, args.authority, args.baseline, args.freeze_additional
        )
    summary = summarize(args.directory, args.plan, args.authority)
    if args.compare_baseline:
        if not args.candidate or not args.acceptance:
            parser.error("comparison requires --candidate and --acceptance")
        print(
            json.dumps(
                compare_runs(args.compare_baseline, args.candidate, args.plan, args.acceptance),
                indent=2,
            )
        )
    else:
        print(json.dumps(summary, indent=2))
