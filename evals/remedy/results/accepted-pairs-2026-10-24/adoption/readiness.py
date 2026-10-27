"""Read-only review/51 integration audit; no credentials, network or state-store access."""

import ast
import hashlib
import importlib.util
import json
from pathlib import Path

import yaml

from citypods.audit_remedy import BodyDecision, P3Adjudication
from citypods.compute.llm_policy import ROUTE_REGISTRY
from citypods.remedy_evaluation import AdmissionEntry, RemedyConfig
from citypods.remedy_p3_observations import P3_ROLES
from citypods.remedy_p3_runner import (
    P3_MODELS,
    P3_ROUTES,
    _accepted_returned_models,
    p3_enabled,
)

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[4]
RESULTS = OUT.parent


def main():
    config = RemedyConfig.model_validate(yaml.safe_load((ROOT / "config/remedy.yml").read_text()))
    spec = importlib.util.spec_from_file_location(
        "body_contract_for_readiness",
        ROOT / "evals/remedy/results/body-selection-2026-10-13/body_selection.py",
    )
    contract = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(contract)
    hashes = {}
    for name in ("maintainer-acceptance.json", "adjudication-approval.json", "score-final.json"):
        path = RESULTS / name
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    approval_path = OUT / "glm-alias-acceptance.json"
    alias_approval = json.loads(approval_path.read_text())
    hashes["glm-alias-acceptance.json"] = hashlib.sha256(approval_path.read_bytes()).hexdigest()
    alias_accepted = (
        alias_approval.get("report_only_identity_accepted") is True
        and alias_approval.get("production_identity_accepted") is True
        and alias_approval["accepted_returned_model"]
        in _accepted_returned_models(alias_approval["route_id"], alias_approval["requested_model"])
    )
    desired = {"initial": ["glm_flash", "deepseek"], "adjudication": []}
    route = "beatapi_gemini_3_8_flash_free"
    module = ast.parse((ROOT / "citypods/remedy_p3_runner.py").read_text())
    enabled_source = next(
        n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == "p3_enabled"
    )
    admission_calls = [
        n
        for n in ast.walk(enabled_source)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "validate_admission"
    ]
    report = {
        "schema_version": 1,
        "status": "manual_publication_authorized_operational_verification_pending",
        "evidence_artifact_sha256": hashes,
        "desired_roles": desired,
        "glm_alias_identity_accepted": alias_accepted,
        "glm_alias_serving_weights_attested": False,
        "legacy_compatibility_models": P3_MODELS,
        "legacy_compatibility_routes": P3_ROUTES,
        "legacy_observation_roles": sorted(P3_ROLES),
        "selected_role_plan": __import__("citypods.audit_remedy", fromlist=["P3_ROLE_PLANS"])
        .P3_ROLE_PLANS[config.p3_role_plan]
        .__dict__,
        "runtime_adjudication_fields": sorted(P3Adjudication.model_fields),
        "evaluated_answer_fields": sorted(contract.BodySelectionAnswer.model_fields),
        "runtime_decision_fields": sorted(BodyDecision.model_fields),
        "gemini_candidate_registered_locally": route in ROUTE_REGISTRY,
        "validator_calls_supply_evaluation": all(
            len(n.args) >= 3 or any(k.arg == "evaluation" for k in n.keywords)
            for n in admission_calls
        ),
        "admission_aa_required": AdmissionEntry.model_fields["aa"].is_required(),
        "production_config": {
            "mode": config.mode,
            "publication_mode": config.publication_mode,
            "admission_count": len(config.admissions),
            "enabled": p3_enabled(config, repo_root=ROOT),
        },
        "daily_caller_requires_cas": "cas_capable"
        in (ROOT / "scripts/remedy_p3_daily.py").read_text(),
        "unresolved_design_gates": [
            "Verify deployment and a bounded production PR/issue cycle after merge.",
            "General AA/holdout qualification is not asserted by manual publication authority.",
            "Qualified-alias automation remains disabled and separately gated.",
        ],
        "provider_calls": 0,
        "remote_storage_writes": 0,
        "production_mutations": 0,
    }
    (OUT / "readiness.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
