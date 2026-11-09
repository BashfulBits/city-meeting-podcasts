"""Rebuild a fresh, approved runtime experiment and audit evidence duplication offline."""

import argparse
import contextlib
import io
import json
from pathlib import Path

from run_runtime_qualification import PLAN, ROOT, digest, read
from runtime_qualification import SPEC, build, write

from citypods.audit_remedy import P3Vote, _compact_evidence
from citypods.compute.llm_policy import estimate_tokens
from citypods.remedy_evaluation import canonical_hash
from citypods.remedy_p3_runner import _adjudication_context, _jev_job, stable_finding_id
from evals.remedy.evidence_archive import load_observations


def prepare(approval_path, output):
    """No credentials/network: infer nothing and never overwrite an earlier packet."""
    approval = read(approval_path)
    if (
        approval.get("approved") is not True
        or approval.get("max_provider_attempts") != 48
        or approval.get("max_attempts_per_case_role") != 1
        or approval["generator_sha256"] != digest(__file__)
        or approval["executor_sha256"] != digest(ROOT / "evals/remedy/run_runtime_qualification.py")
    ):
        raise ValueError("missing exact new-run authority")
    for relative, expected in approval["reference_sha256"].items():
        if digest(ROOT / relative) != expected:
            raise ValueError("reviewed reference changed: " + relative)
    original = read(ROOT / approval["original_outcome_acceptance_path"])
    with contextlib.redirect_stdout(io.StringIO()):
        build(SPEC, output)
    if (
        digest(output / "expected-decisions-draft.json")
        != original["reviewed_expected_draft_sha256"]
    ):
        raise ValueError("expected outcomes or validator changed; not a prompt-only rerun")
    expected = read(output / "expected-decisions-draft.json")
    write(
        output / "expected-decisions-approved.json",
        dict(
            status="same_previously_approved_outcomes_new_prompt_run",
            acceptance_sha256=digest(approval_path),
            original_acceptance_sha256=digest(ROOT / approval["original_outcome_acceptance_path"]),
            cases=[
                {
                    **case,
                    "approved": True,
                    "outcome_acceptance": original["case_dispositions"].get(case["case_id"], {}),
                }
                for case in expected["cases"]
            ],
            call_budget_approved=True,
            runtime_qualified=False,
        ),
    )
    plan = read(output / "run-plan-draft.json")
    plan.update(
        status="approved_prompt_only_reevaluation",
        expected_outcomes_approved=True,
        call_budget_approved=True,
        outcome_acceptance_sha256=digest(approval_path),
        new_run_approval_sha256=digest(approval_path),
        baseline_results_sha256=approval["reference_sha256"][approval["baseline_path"]],
    )
    for relative in (
        "evals/remedy/prepare_runtime_reevaluation.py",
        "citypods/judging/backends.py",
        "citypods/compute/llm_policy.py",
        "citypods/compute/structured_shaping.py",
    ):
        plan["source_sha256"][relative] = digest(ROOT / relative)
    write(output / "run-plan-draft.json", plan)
    # Canonical execution paths stay pinned even when rebuilding into a QA scratch directory.
    canonical = Path(approval["output_path"])
    write(
        output / "execution-authority.json",
        dict(
            approved=True,
            authority="Explicit maintainer request for another evaluation with concise prompts",
            approval_sha256=digest(approval_path),
            max_provider_attempts=48,
            max_attempts_per_case_role=1,
            run_plan_sha256=digest(output / "run-plan-draft.json"),
            executor_sha256=approval["executor_sha256"],
            approved_expectations_sha256=digest(output / "expected-decisions-approved.json"),
            plan_path=str(canonical / "packet/run-plan-draft.json"),
            output_path=str(canonical / "measurement"),
            scope="Same twelve cases, fresh ledger, unchanged scoring; no production activation",
        ),
    )
    audit = audit_packets(read(output / "model-inputs.json"), approval)
    write(output / "packet-audit.json", audit)
    return dict(cases=len(expected["cases"]), audit_cases=len(audit), maximum_attempts=48)


def audit_packets(inputs, approval):
    """Use retained initial outputs only to audit worst-case judge shaping, never as new answers."""
    observed = load_observations(ROOT / approval["baseline_path"])["observed_results"]
    observed += load_observations(ROOT / approval["continuation_path"])["observed_results"]
    rows = {(r["case_id"], r["role"]): r for r in observed}
    audit = []
    for case in inputs["cases"]:
        cid, evidence = case["case_id"], case["evidence"]
        compact = _compact_evidence(evidence)
        taxonomy = json.dumps(compact["existing_feeds"], sort_keys=True)
        fid = stable_finding_id(
            evidence["source_key"], evidence["unexpected_findings"][0]["unexpected_body"]
        )
        votes = []
        for role in PLAN.initial:
            old = rows[(cid, role)]
            votes.append(
                P3Vote(
                    route_id=PLAN.reference_routes[role],
                    upstream_model=old["returned_model"],
                    reasoning_level=None,
                    evidence_hash=canonical_hash(evidence),
                    policy_hash="a" * 64,
                    response_hash=canonical_hash(old["normalized"]),
                    decision={**old["normalized"]["decision"], "finding_id": fid},
                )
            )
        context = _adjudication_context(votes, evidence, PLAN.name)
        from run_runtime_qualification import chat_payload

        from citypods.remedy_p3_runner import _chat_job

        gemini = _chat_job(
            role="gemini",
            role_plan=PLAN.name,
            finding_id=fid,
            evidence_hash=canonical_hash(evidence),
            policy_hash="a" * 64,
            evidence=evidence,
            extra_context=context,
        )
        gemini_payload = chat_payload(gemini, "gemini")
        chat_text = "\n".join(m["content"] for m in gemini_payload["messages"])
        job, _, item = _jev_job(
            finding_id=fid,
            role_plan=PLAN.name,
            initial_votes=tuple(votes),
            evidence=evidence,
            evidence_hash=canonical_hash(evidence),
            policy_hash="a" * 64,
            extra_context=context,
        )
        jev = dict(model="jev-1.13-free", **job.inputs["systemone"])
        wire = next(iter(jev["questions"].values()))["instructions"]
        if wire.count(taxonomy) != 1 or chat_text.count(taxonomy) != 1:
            raise ValueError("duplicated or missing taxonomy in " + cid)
        if wire.count(item.question.instruction) != 1:
            raise ValueError("duplicated JEV instruction in " + cid)
        data = json.loads(item.evidence.text)
        if data["source_evidence"] != compact or "initial_proposals" in data:
            raise ValueError("evidence lost or proposals duplicated in " + cid)
        audit.append(
            dict(
                case_id=cid,
                use="offline sizing audit with prior proposals, not executable new jobs",
                taxonomy_copies_gemini=1,
                taxonomy_copies_jev=1,
                jev_local_token_estimate=estimate_tokens([{"content": json.dumps(jev)}]),
                gemini_payload_sha256=canonical_hash(gemini_payload),
                jev_payload_sha256=canonical_hash(jev),
                initial_payload_sha256={
                    role: canonical_hash(case["jobs"][role]["direct_measurement_payload"])
                    for role in PLAN.initial
                },
            )
        )
    return audit


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approval", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.approval, args.output)))
