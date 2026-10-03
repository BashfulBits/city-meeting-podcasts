from __future__ import annotations

import copy
import importlib.util
import json
from dataclasses import replace
from pathlib import Path

import pytest

from citypods.compute.llm_policy import LLMRoute, PricingPolicy, QuotaPolicy
from citypods.remedy_evaluation import (
    Manifest,
    RemedyConfig,
    canonical_hash,
    case_messages,
    compare_results,
    load_cases,
    validate_admission,
    validate_answer,
    validate_holdout,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def dataset():
    return load_cases(ROOT / "evals/remedy/manifest.json", ROOT / "evals/remedy/gold.json")


def answer(case, supported):
    return dict(
        case_id=case.id,
        supported=supported,
        proposed_owner=None,
        proposed_action=None,
        evidence_ref_ids=[],
        missing_evidence=[],
        rationale="Frozen evidence only.",
    )


def capable_route():
    return LLMRoute(
        model="test",
        transport="direct",
        free=True,
        quota=QuotaPolicy(),
        pricing=PricingPolicy(),
        route_id="test-route",
        upstream_model="test-high",
        reasoning_controls_json='{"high":{"reasoning_effort":"high"}}',
    )


def candidate():
    return dict(
        status="candidate",
        role="proposer",
        physical_route_ids=["test-route"],
        upstream_model="test-high",
        model_family="test",
        reasoning_level="high",
        request_params_hash=canonical_hash({"reasoning_effort": "high"}),
        aa=dict(
            version="4.3.2",
            variant="Test High",
            score=41,
            source="https://example.org/aa",
            date="2026-10-02",
        ),
    )


def test_seeds_migrate_without_admission_claim(dataset):
    assert len(dataset.manifest.cases) == 27
    assert dataset.manifest.split == "regression"
    assert all(truth.provenance == "seed" for truth in dataset.gold.cases.values())
    assert dataset.manifest.superseded_hash


def test_gold_not_in_serialized_input(dataset):
    case = dataset.manifest.cases[0]
    serialized = json.dumps(case_messages(case))
    assert dataset.gold.cases[case.id].rationale not in serialized
    assert '"supported"' not in serialized
    assert case.claim in serialized


def test_answer_ids_are_local(dataset):
    case = dataset.manifest.cases[0]
    value = answer(case, True)
    value["evidence_ref_ids"] = ["invented"]
    with pytest.raises(ValueError, match="unknown evidence"):
        validate_answer(value, case)
    value["evidence_ref_ids"] = []
    value["case_id"] = "other"
    with pytest.raises(ValueError, match="case ID"):
        validate_answer(value, case)


def test_failed_unknown_and_abstention_never_correct(dataset):
    cases = dataset.manifest.cases
    unknown = next(case for case in cases if dataset.gold.cases[case.id].supported is None)
    known = [case for case in cases if dataset.gold.cases[case.id].supported is not None]
    rows = [
        dict(case_id=unknown.id, status="completed", answer=answer(unknown, True)),
        dict(case_id=known[0].id, status="failed"),
        dict(case_id=known[1].id, status="completed", answer=answer(known[1], None)),
    ]
    report = compare_results(rows, dataset.gold, manifest=dataset.manifest)
    assert report.counts["correct"] == 0
    assert report.counts["unknown_truth"] == report.counts["failed"] == 1
    assert report.counts["abstained"] == 1
    assert report.accepted_precision is None
    assert report.admission_eligible_cases == 0


def test_critical_wrong_claim_counted(dataset):
    case = dataset.manifest.cases[0]
    report = compare_results(
        [dict(case_id=case.id, status="completed", answer=answer(case, True))],
        dataset.gold,
        manifest=dataset.manifest,
    )
    assert report.counts["critical_errors"] == 1
    assert report.accepted_precision == 0


def test_disjoint_and_duplicate_ids_rejected(dataset, tmp_path):
    manifest = tmp_path / "manifest.json"
    gold = tmp_path / "gold.json"
    manifest.write_text(dataset.manifest.model_dump_json())
    value = dataset.gold.model_dump()
    value["cases"].pop(next(iter(value["cases"])))
    gold.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="disjoint"):
        load_cases(manifest, gold)
    value = dataset.manifest.model_dump()
    value["cases"].append(value["cases"][0])
    manifest.write_text(json.dumps(value))
    gold.write_text(dataset.gold.model_dump_json())
    with pytest.raises(ValueError, match="duplicate"):
        load_cases(manifest, gold)


def test_holdout_group_and_recording_overlap(dataset):
    value = dataset.manifest.model_dump()
    value["split"] = "holdout"
    holdout = Manifest.model_validate(value)
    with pytest.raises(ValueError, match="overlaps"):
        validate_holdout(holdout, dataset.manifest)
    for case in value["cases"]:
        case["split_group"] += "-holdout"
    with pytest.raises(ValueError, match="overlaps"):
        validate_holdout(Manifest.model_validate(value), dataset.manifest)


def test_candidate_evaluation_is_not_production_admission():
    catalog = {"test-route": capable_route()}
    assert validate_admission(candidate(), catalog, for_evaluation=True).usable
    check = validate_admission(candidate(), catalog)
    assert not check.usable
    assert "candidate is evaluation-only" in check.reasons


@pytest.mark.parametrize(
    "change",
    [
        {"upstream_model": "other"},
        {"reasoning_level": "max"},
        {"physical_route_ids": []},
        {"request_params_hash": "bad"},
        {
            "aa": dict(
                version="4.1",
                variant="Test High",
                score=99,
                source="https://example.org/aa",
                date="2026-10-02",
            )
        },
    ],
)
def test_candidate_requires_physical_quality_and_controls(change):
    entry = candidate() | change
    assert not validate_admission(
        entry, {"test-route": capable_route()}, for_evaluation=True
    ).usable


def test_unsupported_catalog_controls_fail_closed():
    route = replace(capable_route(), reasoning_controls_json="")
    check = validate_admission(candidate(), {"test-route": route}, for_evaluation=True)
    assert not check.usable
    assert any("unverified effort" in reason for reason in check.reasons)


def test_admission_requires_matching_reviewed_results():
    entry = candidate() | dict(
        status="admitted",
        reviewed_admission_ref="https://example.org/pr",
        reviewed_result_paths=["results/frozen.json"],
    )
    hashes = dict.fromkeys(
        ("manifest_hash", "gold_hash", "prompt_hash", "schema_hash", "catalog_hash"), "frozen"
    )
    entry.update(hashes)
    evaluation = hashes | {"qualified": True, "admission_eligible_cases": 1}
    catalog = {"test-route": capable_route()}
    assert validate_admission(entry, catalog, evaluation).usable
    assert not validate_admission(entry, catalog, evaluation | {"gold_hash": "new"}).usable


def cli():
    spec = importlib.util.spec_from_file_location(
        "eval_remedy_cli", ROOT / "scripts/eval_remedy.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_empty_admission_dry_run_has_no_observations(dataset, tmp_path, monkeypatch):
    import yaml

    config = RemedyConfig.model_validate(yaml.safe_load((ROOT / "config/remedy.yml").read_text()))
    module = cli()
    result = module.run_cases(dataset.manifest, config, "proposer", dry_run=True, max_cases=None)
    assert result["model_observations"] == 0
    assert result["policy_holds"]
    assert result["results"] == []
    out = tmp_path / "plan.json"
    argv = [
        "run",
        "--manifest",
        str(ROOT / "evals/remedy/manifest.json"),
        "--admission",
        str(ROOT / "config/remedy.yml"),
        "--role",
        "proposer",
        "--out",
        str(out),
        "--dry-run",
    ]
    assert module.main(argv) == 0
    with pytest.raises(SystemExit):
        module.main(argv)


def test_freeze_force_preserves_previous_manifest_and_gold(dataset, tmp_path):
    module = cli()
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({"cases": [dataset.manifest.cases[0].model_dump()]}))
    out = tmp_path / "set"
    argv = ["freeze", "--evidence", str(evidence), "--out", str(out), "--provenance", "test"]
    assert module.main(argv) == 0
    previous = json.loads((out / "manifest.json").read_text())
    (out / "gold.json").write_text(dataset.gold.model_dump_json())
    assert module.main(argv + ["--force"]) == 0
    prior = out / "superseded" / canonical_hash(previous)
    assert json.loads((prior / "manifest.json").read_text()) == previous
    assert (prior / "gold.json").exists()


def test_persisted_unknown_fields_and_gold_revision(dataset):
    value = copy.deepcopy(dataset.manifest.model_dump())
    value["cases"][0]["surprise"] = True
    with pytest.raises(ValueError):
        Manifest.model_validate(value)
    value = dataset.gold.model_dump()
    value["cases"][next(iter(value["cases"]))]["revision"] = 2
    from citypods.remedy_evaluation import Gold

    with pytest.raises(ValueError, match="correction"):
        Gold.model_validate(value)


def test_identical_inputs_across_candidates_and_unknown_returned_model(dataset, monkeypatch):
    from types import SimpleNamespace

    import yaml

    import citypods.compute.llm_policy as policy_module
    from citypods.compute.base import JobResult

    route = capable_route()
    monkeypatch.setattr(policy_module, "ROUTE_REGISTRY", {route.route_id: route})
    config = yaml.safe_load((ROOT / "config/remedy.yml").read_text())
    second = candidate() | {"role": "proposer", "model_family": "alternative-experiment"}
    config["admissions"] = [candidate(), second]
    config = RemedyConfig.model_validate(config)
    module = cli()
    plan = module.run_cases(dataset.manifest, config, "proposer", dry_run=True, max_cases=None)
    first, second = plan["plans"][:27], plan["plans"][27:]
    assert [row["messages"] for row in first] == [row["messages"] for row in second]
    called = []

    def complete(job):
        called.append(job)
        return JobResult(
            task=job.task,
            recipe_hash=job.recipe_hash,
            output={"model": "unadmitted-upstream"},
            route_id=route.route_id,
            upstream_model=route.upstream_model,
            reasoning_level="high",
            request_params_hash=candidate()["request_params_hash"],
        )

    live = module.run_cases(
        dataset.manifest,
        config,
        "proposer",
        dry_run=False,
        max_cases=1,
        backend=SimpleNamespace(run_immediate=complete),
    )
    assert len(called) == 1
    assert live["results"][0]["status"] == "failed"
    assert live["results"][0]["raw_response"] == {"model": "unadmitted-upstream"}
    assert all(row["status"] == "unattempted" for row in live["results"][1:])
    assert live["model_observations"] == 1


def test_adapted_grounding_cannot_become_admission_truth(dataset):
    manifest = dataset.manifest.model_copy(deep=True)
    manifest.split = "holdout"
    for item in manifest.cases:
        item.source_key += "-test-heldout-source"
        item.split_group += "-test-heldout-family"
    case = manifest.cases[0]
    case.input_kind = "adapted"
    truth = dataset.gold.model_copy(deep=True)
    truth.cases[case.id].provenance = "verified"
    report = compare_results(
        [dict(case_id=case.id, status="completed", answer=answer(case, False))],
        truth,
        manifest=manifest,
    )
    assert report.admission_eligible_cases == 0


def test_current_route_without_required_controls_is_a_visible_hold():
    from citypods.compute.llm_policy import ROUTE_REGISTRY, route_reasoning_controls

    unverified = next(
        (r for r in ROUTE_REGISTRY.values() if r.free and not route_reasoning_controls(r, "high")),
        None,
    )
    if unverified is None:
        pytest.skip("catalog now verifies high effort on every free route")
    entry = candidate() | dict(
        physical_route_ids=[unverified.route_id], upstream_model=unverified.upstream_model
    )
    check = validate_admission(entry, ROUTE_REGISTRY, for_evaluation=True)
    assert not check.usable
    assert any("unverified effort" in reason for reason in check.reasons)


def test_seed_only_qualified_claim_cannot_admit():
    entry = candidate() | dict(
        status="admitted",
        reviewed_admission_ref="https://example.org/pr",
        reviewed_result_paths=["results/frozen.json"],
    )
    hashes = dict.fromkeys(
        ("manifest_hash", "gold_hash", "prompt_hash", "schema_hash", "catalog_hash"), "frozen"
    )
    entry.update(hashes)
    evaluation = hashes | {"qualified": True, "admission_eligible_cases": 0}
    assert not validate_admission(entry, {"test-route": capable_route()}, evaluation).usable


def test_supplied_candidate_version_hashes_cannot_mismatch():
    entry = candidate() | {"manifest_hash": "original"}
    check = validate_admission(
        entry, {"test-route": capable_route()}, {"manifest_hash": "changed"}, for_evaluation=True
    )
    assert not check.usable
    assert "evaluation version mismatch: manifest_hash" in check.reasons


def test_default_live_backend_requires_cas_storage_before_provider_attempts(dataset, monkeypatch):
    import yaml

    import citypods.compute.llm_policy as policy_module
    import citypods.storage as storage_module

    route = capable_route()
    monkeypatch.setattr(policy_module, "ROUTE_REGISTRY", {route.route_id: route})
    configured = []

    def no_cas(*args):
        configured.append(args)
        return None

    monkeypatch.setattr(storage_module, "make_storage", no_cas)
    config = yaml.safe_load((ROOT / "config/remedy.yml").read_text())
    config["admissions"] = [candidate()]
    result = cli().run_cases(
        dataset.manifest,
        RemedyConfig.model_validate(config),
        "proposer",
        dry_run=False,
        max_cases=1,
    )
    assert len(configured) == 1
    assert result["model_observations"] == 0
    assert any("CAS-capable" in reason for reason in result["policy_holds"])
    assert all(row["status"] == "unattempted" for row in result["results"])


def test_public_load_freeze_and_report_reject_overlapping_holdout(dataset, tmp_path):
    from citypods.remedy_evaluation import freeze_cases

    value = dataset.manifest.model_dump()
    value["split"] = "holdout"
    manifest = tmp_path / "manifest.json"
    gold = tmp_path / "gold.json"
    manifest.write_text(json.dumps(value))
    gold.write_text(dataset.gold.model_dump_json())
    with pytest.raises(ValueError, match="overlaps"):
        load_cases(manifest, gold)
    with pytest.raises(ValueError, match="overlaps"):
        freeze_cases({"cases": value["cases"]}, {}, split="holdout", provenance="test")
    with pytest.raises(ValueError, match="overlaps"):
        compare_results([], dataset.gold, manifest=value)


def admitted_run_entry(dataset):
    from citypods.remedy_evaluation import PROMPT, RemedyEvalAnswer

    context = dict(
        manifest_hash=canonical_hash(dataset.manifest.model_dump()),
        prompt_hash=canonical_hash(PROMPT),
        schema_hash=canonical_hash(RemedyEvalAnswer.model_json_schema()),
        catalog_hash=canonical_hash(
            json.loads((ROOT / "citypods/compute/llm_routes.json").read_text())
        ),
    )
    return (
        candidate()
        | context
        | dict(
            status="admitted",
            gold_hash=canonical_hash(dataset.gold.model_dump()),
            reviewed_result_paths=["results/previous-admission.json"],
            reviewed_admission_ref="https://example.org/previous-admission",
        )
    )


def test_admitted_route_can_rerun_on_matching_inputs_without_gold_or_prior_results(
    dataset,
    monkeypatch,
):
    import yaml

    import citypods.compute.llm_policy as policy_module

    route = capable_route()
    monkeypatch.setattr(policy_module, "ROUTE_REGISTRY", {route.route_id: route})
    entry = admitted_run_entry(dataset)
    config = yaml.safe_load((ROOT / "config/remedy.yml").read_text())
    config["admissions"] = [entry]
    result = cli().run_cases(
        dataset.manifest,
        RemedyConfig.model_validate(config),
        "proposer",
        dry_run=True,
        max_cases=None,
    )
    assert not result["policy_holds"]
    assert len(result["plans"]) == len(dataset.manifest.cases)
    assert result["model_observations"] == 0
    assert all(plan["candidate_status"] == "admitted" for plan in result["plans"])
    execution_context = {
        name: entry[name]
        for name in ("manifest_hash", "prompt_hash", "schema_hash", "catalog_hash")
    }
    production = validate_admission(entry, {route.route_id: route}, execution_context)
    assert not production.usable
    assert any("gold_hash" in reason for reason in production.reasons)
    assert any("not qualified" in reason for reason in production.reasons)


@pytest.mark.parametrize("failure", ["input_hash", "unknown_route", "below_baseline", "effort"])
def test_admitted_rerun_still_holds_input_or_physical_quality_failures(
    dataset, monkeypatch, failure
):
    import yaml

    import citypods.compute.llm_policy as policy_module

    route = capable_route()
    entry = admitted_run_entry(dataset)
    if failure == "input_hash":
        entry["manifest_hash"] = canonical_hash("different-frozen-inputs")
    elif failure == "unknown_route":
        entry["physical_route_ids"] = ["unknown-route"]
    elif failure == "below_baseline":
        entry["aa"]["score"] = 20
    else:
        route = replace(route, reasoning_controls_json="")
    monkeypatch.setattr(policy_module, "ROUTE_REGISTRY", {route.route_id: route})
    config = yaml.safe_load((ROOT / "config/remedy.yml").read_text())
    config["admissions"] = [entry]
    result = cli().run_cases(
        dataset.manifest,
        RemedyConfig.model_validate(config),
        "proposer",
        dry_run=True,
        max_cases=None,
    )
    assert result["policy_holds"]
    assert result["plans"] == []
    assert result["model_observations"] == 0


def test_provider_exception_reports_failed_row_and_remaining_bounded_cases(
    dataset,
    monkeypatch,
    tmp_path,
):
    from types import SimpleNamespace

    import yaml

    import citypods.compute.llm_policy as policy_module
    from citypods.compute.base import JobResult

    class OrdinaryProviderError(Exception):
        pass

    route = capable_route()
    monkeypatch.setattr(policy_module, "ROUTE_REGISTRY", {route.route_id: route})
    config = yaml.safe_load((ROOT / "config/remedy.yml").read_text())
    config["admissions"] = [candidate()]
    calls = []
    secret = "provider exception contains private-auth-secret"

    def complete(job):
        calls.append(job)
        if len(calls) == 1:
            raise OrdinaryProviderError(secret)
        case = dataset.manifest.cases[1]
        return JobResult(
            task=job.task,
            recipe_hash=job.recipe_hash,
            route_id=route.route_id,
            upstream_model=route.upstream_model,
            reasoning_level="high",
            request_params_hash=candidate()["request_params_hash"],
            output={
                "model": route.upstream_model,
                "choices": [{"message": {"content": json.dumps(answer(case, False))}}],
            },
        )

    module = cli()
    result = module.run_cases(
        dataset.manifest,
        RemedyConfig.model_validate(config),
        "proposer",
        dry_run=False,
        max_cases=2,
        backend=SimpleNamespace(run_immediate=complete),
    )
    assert len(calls) == 2
    assert result["results"][0]["status"] == "failed"
    assert result["results"][0]["error_class"] == "OrdinaryProviderError"
    assert "error" not in result["results"][0]
    assert secret not in json.dumps(result)
    assert result["results"][1]["status"] == "completed"
    assert all(row["status"] == "unattempted" for row in result["results"][2:])
    output = tmp_path / "provider-failure.json"
    module.write_new(output, result)
    original = output.read_bytes()
    with pytest.raises(FileExistsError):
        module.write_new(output, {"replacement": True})
    assert output.read_bytes() == original


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_provider_execution_does_not_swallow_process_interruptions(
    dataset, monkeypatch, interruption
):
    from types import SimpleNamespace

    import yaml

    import citypods.compute.llm_policy as policy_module

    route = capable_route()
    monkeypatch.setattr(policy_module, "ROUTE_REGISTRY", {route.route_id: route})
    config = yaml.safe_load((ROOT / "config/remedy.yml").read_text())
    config["admissions"] = [candidate()]

    def interrupted(job):
        raise interruption()

    with pytest.raises(interruption):
        cli().run_cases(
            dataset.manifest,
            RemedyConfig.model_validate(config),
            "proposer",
            dry_run=False,
            max_cases=1,
            backend=SimpleNamespace(run_immediate=interrupted),
        )


def test_timeout_exception_message_is_redacted_and_remaining_cases_are_unattempted(
    dataset,
    monkeypatch,
):
    from types import SimpleNamespace

    import yaml

    import citypods.compute.llm_policy as policy_module

    route = capable_route()
    monkeypatch.setattr(policy_module, "ROUTE_REGISTRY", {route.route_id: route})
    config = yaml.safe_load((ROOT / "config/remedy.yml").read_text())
    config["admissions"] = [candidate()]
    secret = "timeout URL contains private-auth-secret"

    def timed_out(job):
        raise TimeoutError(secret)

    result = cli().run_cases(
        dataset.manifest,
        RemedyConfig.model_validate(config),
        "proposer",
        dry_run=False,
        max_cases=2,
        backend=SimpleNamespace(run_immediate=timed_out),
    )
    assert result["results"][0]["error_class"] == "TimeoutError"
    assert secret not in json.dumps(result)
    assert all(row["status"] == "unattempted" for row in result["results"][1:])
