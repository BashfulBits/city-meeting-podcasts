"""Offline policy-only replay of retained initial answers; never model admission or inference."""

import argparse
import json
from collections import Counter
from pathlib import Path

from citypods.audit_remedy import P3_ROLE_PLANS, BodyDecision, P3Vote, build_p3_review_decision
from citypods.remedy_evaluation import canonical_hash
from citypods.remedy_p3_runner import (
    _accepted_returned_models,
    _upstream_for,
    _validator_passed,
    stable_finding_id,
)

ROOT = Path(__file__).resolve().parents[2]


def replay(observed_path, inputs_path):
    observed = json.loads(observed_path.read_text())
    inputs = json.loads(inputs_path.read_text())["cases"]
    rows = {
        (r["case_id"], r["role"]): r
        for r in observed["observed_results"]
        if r["status"] == "completed"
    }
    plan = P3_ROLE_PLANS["flash-deepseek-human-v3"]
    outcomes = []
    for case in inputs:
        cid, evidence = case["case_id"], case["evidence"]
        finding = stable_finding_id(
            evidence["source_key"], evidence["unexpected_findings"][0]["unexpected_body"]
        )
        votes, validators = [], {}
        for role in plan.initial:
            row = rows[(cid, role)]
            requested = _upstream_for(role, plan.name)
            if (
                row["route_id"] != plan.routes[role]
                or row["requested_model"] != requested
                or row["returned_model"]
                not in _accepted_returned_models(row["route_id"], requested)
                or row["normalized"]["decision"]["finding_id"] != "f0"
            ):
                raise ValueError("saved answer does not match this initial role/contract")
            decision = BodyDecision.model_validate(row["normalized"]["decision"]).model_copy(
                update={"finding_id": finding}
            )
            votes.append(
                P3Vote(
                    route_id=row["route_id"],
                    upstream_model=row["returned_model"],
                    reasoning_level=None,
                    evidence_hash=canonical_hash(evidence),
                    policy_hash=canonical_hash({"policy_only_replay": plan.fingerprint}),
                    response_hash=canonical_hash(row["normalized"]),
                    decision=decision,
                )
            )
            validators[plan.routes[role]] = _validator_passed(decision, evidence, str(ROOT))
        result = build_p3_review_decision(
            finding_id=finding,
            evidence_hash=votes[0].evidence_hash,
            policy_hash=votes[0].policy_hash,
            initial_votes=votes,
            initial_validators=validators,
            role_plan=plan.name,
            admitted_routes=frozenset(plan.routes.values()),
        )
        outcomes.append(dict(case_id=cid, disposition=result.disposition, reason=result.reason))
    return dict(
        scope="Offline control-flow replay of previously answered cases; not new model evidence",
        role_plan=plan.name,
        assumes_route_eligibility=True,
        production_admitted=False,
        new_provider_calls=0,
        counts=dict(Counter(r["disposition"] for r in outcomes)),
        cases=outcomes,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observed", type=Path, required=True)
    parser.add_argument("--inputs", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(replay(args.observed, args.inputs), indent=2))
