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


def frozen_context(*, manifest=None, mode="claim_support"):
    from citypods.remedy_evaluation import RemedyEvalAnswer, prompt_for_mode

    manifest = manifest or Manifest.model_validate(
        json.loads((ROOT / "evals/remedy/manifest.json").read_text())
    )
    return {
        "manifest_hash": canonical_hash(manifest.model_dump()),
        "prompt_hash": canonical_hash(prompt_for_mode(mode)),
        "schema_hash": canonical_hash(RemedyEvalAnswer.model_json_schema()),
        "catalog_hash": canonical_hash(
            json.loads((ROOT / "citypods/compute/llm_routes.json").read_text())
        ),
    }


def candidate(*, manifest=None, mode="claim_support"):
    entry = dict(
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

    entry.update(frozen_context(manifest=manifest, mode=mode))
    return entry


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
    assert validate_admission(candidate(), catalog, frozen_context(), for_evaluation=True).usable
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
        entry, {"test-route": capable_route()}, frozen_context(), for_evaluation=True
    ).usable


def test_unsupported_catalog_controls_fail_closed():
    route = replace(capable_route(), reasoning_controls_json="")
    check = validate_admission(
        candidate(), {"test-route": route}, frozen_context(), for_evaluation=True
    )
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
    first, second = plan["plans"][::2], plan["plans"][1::2]
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
    check = validate_admission(entry, ROUTE_REGISTRY, frozen_context(), for_evaluation=True)
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


def owner_dataset(dataset):
    value = dataset.model_copy(deep=True)
    case = value.manifest.cases[0]
    case.existing_feeds = [{"slug": "dallas-tx-tif", "podcast_title": "Dallas TIF"}]
    truth = value.gold.cases[case.id]
    truth.expected_owner = "dallas-tx-tif"
    truth.supported = None
    truth.rationale = "PRIVATE_ADJUDICATION_ONLY"
    truth.provenance = "verified"
    return value


def test_blind_inputs_mask_semantic_ids_claims_metadata_and_hidden_truth(dataset):
    from citypods.remedy_evaluation import prompt_case_id, prompt_for_mode

    value = owner_dataset(dataset)
    case = value.manifest.cases[0]
    case.family_policy = {
        "policy_id": "dallas-tif",
        "version": "1",
        "aggregate_family": "tif",
        "identity_names": [case.body_label],
        "member_names": ["Downtown Connection"],
        "approval_ref": "https://example.org/approved-policy",
        "seed_evidence": {"new_feed_slug": "SECRET_PROPOSER_TARGET"},
        "proposer_rationale": "SECRET_PROPOSER_REASON",
        "expected_owner": "SECRET_OWNER_TRUTH",
        "reference": "SECRET_TUNING_REFERENCE",
    }
    messages = case_messages(case, "blind_owner")
    serialized = json.dumps(messages)
    payload = json.loads(messages[1]["content"])
    assert case.id not in serialized
    assert case.claim not in serialized
    assert "PRIVATE_ADJUDICATION_ONLY" not in serialized
    assert "SECRET_" not in serialized
    assert payload["id"] == prompt_case_id(case, "blind_owner")
    assert payload["id"] == prompt_case_id(case, "blind_owner")
    assert set(payload) == {
        "id",
        "city",
        "source_key",
        "body_label",
        "evidence_refs",
        "recordings",
        "existing_feeds",
        "family_policy",
        "completeness",
    }
    assert set(payload["family_policy"]) == {
        "policy_id",
        "version",
        "aggregate_family",
        "identity_names",
        "member_names",
        "approval_ref",
    }
    assert prompt_for_mode("blind_owner") != prompt_for_mode("claim_support")


def test_blind_owner_scores_without_claim_truth_and_never_scores_claim_support(dataset):
    value = owner_dataset(dataset)
    case = value.manifest.cases[0]
    response = answer(case, True)
    response["proposed_owner"] = "dallas-tx-tif"
    report = compare_results(
        [dict(case_id=case.id, status="completed", answer=response)],
        value.gold,
        manifest=value.manifest,
        mode="blind_owner",
    )
    assert report.mode == "blind_owner"
    assert report.owner_selection["correct"] == report.owner_selection["known_truth"] == 1
    assert report.counts["correct"] == report.counts["known_truth"] == 0
    assert report.accepted_precision is None
    assert report.supported_claim_recall is None
    assert report.admission_eligible_cases == 0


def test_blind_owner_unknown_abstention_failure_and_unattempted_accounting(dataset):
    value = owner_dataset(dataset)
    cases = value.manifest.cases
    for case in cases[1:3]:
        case.existing_feeds = [{"slug": "dallas-tx-tif"}]
        value.gold.cases[case.id].expected_owner = "dallas-tx-tif"
    abstained = answer(cases[0], None)
    unknown = answer(cases[3], True)
    unknown["proposed_owner"] = "unreviewed-owner"
    rows = [
        dict(case_id=cases[0].id, status="completed", answer=abstained),
        dict(case_id=cases[1].id, status="failed"),
        dict(case_id=cases[2].id, status="unattempted"),
        dict(case_id=cases[3].id, status="completed", answer=unknown),
    ]
    report = compare_results(rows, value.gold, manifest=value.manifest, mode="blind_owner")
    assert report.owner_selection["correct"] == 0
    assert report.owner_selection["known_truth"] == report.owner_selection["abstained"] == 1
    assert report.owner_selection["failed"] == report.owner_selection["unknown_truth"] == 1
    assert report.owner_selection["unattempted"] == len(cases) - 3
    assert not any(report.counts.values())


def test_hidden_owner_truth_requires_nonblank_existing_feed(dataset, tmp_path):
    from citypods.remedy_evaluation import Gold

    value = dataset.gold.model_dump()
    case = dataset.manifest.cases[0]
    value["cases"][case.id]["expected_owner"] = " "
    with pytest.raises(ValueError, match="blank"):
        Gold.model_validate(value)
    value["cases"][case.id]["expected_owner"] = "not-an-existing-feed"
    manifest_path, gold_path = tmp_path / "manifest.json", tmp_path / "gold.json"
    manifest_path.write_text(dataset.manifest.model_dump_json())
    gold_path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="existing feed slug"):
        load_cases(manifest_path, gold_path)
    with pytest.raises(ValueError, match="existing feed slug"):
        compare_results([], value, manifest=dataset.manifest, mode="blind_owner")


def test_blind_run_maps_opaque_response_ids_locally_preserving_raw_answer(dataset, monkeypatch):
    from types import SimpleNamespace

    import yaml

    import citypods.compute.llm_policy as policy_module
    from citypods.compute.base import JobResult

    value = owner_dataset(dataset)
    route = capable_route()
    monkeypatch.setattr(policy_module, "ROUTE_REGISTRY", {route.route_id: route})
    config = yaml.safe_load((ROOT / "config/remedy.yml").read_text())
    entry = candidate(manifest=value.manifest, mode="blind_owner")
    entry["role"] = "reviewer"
    config["admissions"] = [entry]
    observed = []

    def complete(job):
        payload = json.loads(job.inputs["messages"][1]["content"])
        observed.append(payload)
        response = answer(value.manifest.cases[0], None)
        response.update(case_id=payload["id"], proposed_owner="dallas-tx-tif")
        return JobResult(
            task=job.task,
            recipe_hash=job.recipe_hash,
            output={
                "model": route.upstream_model,
                "choices": [{"message": {"content": json.dumps(response)}}],
            },
            route_id=route.route_id,
            upstream_model=route.upstream_model,
            reasoning_level="high",
            request_params_hash=entry["request_params_hash"],
        )

    result = cli().run_cases(
        value.manifest,
        RemedyConfig.model_validate(config),
        "reviewer",
        dry_run=False,
        max_cases=1,
        mode="blind_owner",
        backend=SimpleNamespace(run_immediate=complete),
    )
    row = result["results"][0]
    assert result["mode"] == "blind_owner"
    assert row["status"] == "completed"
    assert row["case_id"] == row["answer"]["case_id"] == value.manifest.cases[0].id
    raw = json.loads(row["raw_response"]["choices"][0]["message"]["content"])
    assert raw["case_id"] == observed[0]["id"] == row["prompt_case_id"]
    assert raw["case_id"] != row["case_id"]
    report = compare_results(
        result["results"], value.gold, manifest=value.manifest, mode="blind_owner"
    )
    assert report.owner_selection["correct"] == 1


@pytest.mark.parametrize("name", ["manifest_hash", "prompt_hash", "schema_hash", "catalog_hash"])
def test_candidate_missing_frozen_hash_holds_even_dry_run(dataset, monkeypatch, name):
    import yaml

    import citypods.compute.llm_policy as policy_module

    route = capable_route()
    monkeypatch.setattr(policy_module, "ROUTE_REGISTRY", {route.route_id: route})
    config = yaml.safe_load((ROOT / "config/remedy.yml").read_text())
    entry = candidate()
    entry.pop(name)
    config["admissions"] = [entry]
    result = cli().run_cases(
        dataset.manifest,
        RemedyConfig.model_validate(config),
        "proposer",
        dry_run=True,
        max_cases=None,
    )
    assert result["plans"] == []
    assert f"missing frozen input hash: {name}" in result["policy_holds"]
    assert result["model_observations"] == 0


def test_claim_mode_candidate_cannot_silently_enter_blind_experiment(dataset, monkeypatch):
    import yaml

    import citypods.compute.llm_policy as policy_module

    route = capable_route()
    monkeypatch.setattr(policy_module, "ROUTE_REGISTRY", {route.route_id: route})
    config = yaml.safe_load((ROOT / "config/remedy.yml").read_text())
    config["admissions"] = [candidate()]
    result = cli().run_cases(
        dataset.manifest,
        RemedyConfig.model_validate(config),
        "proposer",
        dry_run=True,
        max_cases=None,
        mode="blind_owner",
    )
    assert result["plans"] == []
    assert "evaluation version mismatch: prompt_hash" in result["policy_holds"]
    assert result["mode"] == "blind_owner"


def test_cli_blind_mode_keeps_gold_outside_run_and_labels_report(dataset, tmp_path, monkeypatch):
    from citypods.remedy_evaluation import prompt_for_mode

    module = cli()
    manifest_path = tmp_path / "manifest.json"
    gold_path = tmp_path / "gold.json"
    manifest_path.write_text(dataset.manifest.model_dump_json())
    gold_path.write_text("invalid gold must not be read by run")
    out = tmp_path / "blind.json"
    assert (
        module.main(
            [
                "run",
                "--manifest",
                str(manifest_path),
                "--admission",
                str(ROOT / "config/remedy.yml"),
                "--mode",
                "blind_owner",
                "--role",
                "reviewer",
                "--dry-run",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    raw = json.loads(out.read_text())
    assert raw["mode"] == "blind_owner"
    assert raw["prompt_hash"] == canonical_hash(prompt_for_mode("blind_owner"))
    assert raw["prompt_hash"] != canonical_hash(prompt_for_mode("claim_support"))
    assert raw["model_observations"] == 0
    gold_path.write_text(dataset.gold.model_dump_json())
    report_path = tmp_path / "report.json"
    argv = [
        "report",
        "--manifest",
        str(manifest_path),
        "--gold",
        str(gold_path),
        "--results",
        str(out),
        "--out",
        str(report_path),
    ]
    assert module.main(argv) == 0
    group = json.loads(report_path.read_text())["reports"][0]
    assert group["mode"] == group["report"]["mode"] == "blind_owner"
    assert group["report"]["owner_selection"]["unattempted"] == len(dataset.manifest.cases)
    assert not any(group["report"]["counts"].values())
    raw["prompt_hash"] = canonical_hash(prompt_for_mode("claim_support"))
    mismatched = tmp_path / "mismatched.json"
    mismatched.write_text(json.dumps(raw))
    argv[argv.index(str(out))] = str(mismatched)
    argv[-1] = str(tmp_path / "invalid-report.json")
    with pytest.raises(SystemExit):
        module.main(argv)


def test_regression_seed_owner_truth_remains_unreviewed(dataset):
    assert len(dataset.manifest.cases) == 27
    assert all(truth.expected_owner is None for truth in dataset.gold.cases.values())
    raw = json.loads((ROOT / "evals/remedy/gold.json").read_text())
    assert all("expected_owner" not in truth for truth in raw["cases"].values())


def test_blind_provisional_policy_is_not_presented_as_approved(dataset):
    case = dataset.manifest.cases[0].model_copy(deep=True)
    case.policy_status = "provisional"
    case.family_policy = {"policy_id": "unapproved", "identity_names": [case.body_label]}
    payload = json.loads(case_messages(case, "blind_owner")[1]["content"])
    assert payload["family_policy"] == {}


def test_blind_owner_preserves_frozen_feed_policies_without_proposer_metadata(dataset):
    case = dataset.manifest.cases[0].model_copy(deep=True)
    case.policy_status = "approved"
    case.existing_feeds = [
        {
            "slug": "dallas-tx-tif",
            "title": "Dallas TIF boards",
            "claim": "SECRET_PROPOSED_CLAIM",
            "source": {"body_exact": ["TIF Board"], "target": "SECRET_TARGET"},
        }
    ]
    case.family_policy = {
        "dallas-tx-tif": {
            "aggregate_family": "tif",
            "identity_names": ["TIF Board"],
            "seed_evidence": {"target": "SECRET_SEED_OWNER"},
        },
        "SECRET_UNRELATED_POLICY": {"aggregate_family": "tif"},
    }
    messages = case_messages(case, "blind_owner")
    payload = json.loads(messages[1]["content"])
    assert "SECRET_" not in json.dumps(messages)
    assert payload["family_policy"] == {
        "dallas-tx-tif": {"aggregate_family": "tif", "identity_names": ["TIF Board"]}
    }
    assert payload["existing_feeds"][0]["source"] == {"body_exact": ["TIF Board"]}


def test_bounded_candidates_interleave_and_timeout_is_configuration_local(dataset, monkeypatch):
    from types import SimpleNamespace

    import yaml

    import citypods.compute.llm_policy as policy_module

    first = capable_route()
    second = replace(first, route_id="other-route")
    monkeypatch.setattr(
        policy_module, "ROUTE_REGISTRY", {first.route_id: first, second.route_id: second}
    )
    config = yaml.safe_load((ROOT / "config/remedy.yml").read_text())
    other = candidate()
    other["physical_route_ids"] = [second.route_id]
    config["admissions"] = [candidate(), other]
    calls = []

    def complete(job):
        calls.append(job.inputs["llm_policy"].allowed_route_ids)
        if calls[-1] == (first.route_id,):
            raise TimeoutError("no capacity")
        raise ValueError("ordinary failure does not exhaust candidate")

    module = cli()
    result = module.run_cases(
        dataset.manifest,
        RemedyConfig.model_validate(config),
        "proposer",
        dry_run=False,
        max_cases=3,
        backend=SimpleNamespace(run_immediate=complete),
    )
    assert calls == [(first.route_id,), (second.route_id,), (second.route_id,)]
    rows = result["results"]
    assert rows[0]["case_id"] == rows[1]["case_id"] == dataset.manifest.cases[0].id
    assert rows[2]["status"] == "unattempted"
    assert rows[3]["status"] == "failed"
    assert sum(row["status"] == "failed" for row in rows) == 3
    assert all(row["status"] == "unattempted" for row in rows[4:])


def test_git_preflight_failure_happens_before_live_attempts(dataset, monkeypatch, tmp_path):
    import subprocess

    module = cli()

    def missing_git(*args, **kwargs):
        raise FileNotFoundError("git unavailable")

    monkeypatch.setattr(subprocess, "check_output", missing_git)
    monkeypatch.setattr(module, "run_cases", lambda *a, **kw: pytest.fail("live run started"))
    output = tmp_path / "results.json"
    with pytest.raises(FileNotFoundError, match="git unavailable"):
        module.main(
            [
                "run",
                "--manifest",
                str(ROOT / "evals/remedy/manifest.json"),
                "--role",
                "proposer",
                "--live",
                "--max-cases",
                "1",
                "--out",
                str(output),
            ]
        )
    assert not output.exists()


@pytest.mark.parametrize(
    ("level", "variant"),
    [("low", "Test Flow"), ("max", "Qwen3 Max (Non-reasoning)"), ("high", "Test Highlight")],
)
def test_aa_model_name_substrings_and_non_reasoning_are_not_effort(level, variant):
    route = replace(
        capable_route(), reasoning_controls_json=json.dumps({level: {"reasoning_effort": level}})
    )
    entry = candidate()
    entry["reasoning_level"] = level
    entry["aa"]["variant"] = variant
    entry["request_params_hash"] = canonical_hash({"reasoning_effort": level})
    result = validate_admission(
        entry, {route.route_id: route}, frozen_context(), for_evaluation=True
    )
    assert "AA reasoning variant mismatch" in result.reasons


def test_reports_separate_first_attempt_and_retried_completion(dataset, tmp_path):
    from citypods.remedy_evaluation import prompt_for_mode

    rows = []
    for case, count in zip(dataset.manifest.cases[:3], [1, 2, None], strict=True):
        rows.append(
            dict(
                case_id=case.id,
                configuration_id="configuration",
                status="completed",
                provider_attempts=count,
                answer=answer(case, False),
            )
        )
    raw = dict(
        manifest_hash=canonical_hash(dataset.manifest.model_dump()),
        prompt_hash=canonical_hash(prompt_for_mode("claim_support")),
        mode="claim_support",
        results=rows,
    )
    results_path = tmp_path / "raw.json"
    results_path.write_text(json.dumps(raw))
    output = tmp_path / "report.json"
    assert (
        cli().main(
            [
                "report",
                "--manifest",
                str(ROOT / "evals/remedy/manifest.json"),
                "--gold",
                str(ROOT / "evals/remedy/gold.json"),
                "--results",
                str(results_path),
                "--out",
                str(output),
            ]
        )
        == 0
    )
    counts = json.loads(output.read_text())["reports"][0]["attempt_counts"]
    assert counts == dict(
        first_attempt_completed=1,
        retried_completed=1,
        known_provider_attempts=3,
        unknown_attempt_count=1,
    )


def test_policy_template_default_preserves_existing_remedy_config():
    import yaml

    from citypods.remedy_evaluation import RemedyConfig
    from citypods.remedy_policy import load_policy_templates

    config = yaml.safe_load(Path("config/remedy.yml").read_text())
    config.pop("policy_templates")
    model = RemedyConfig.model_validate(config)
    assert model.policy_templates == []
    assert load_policy_templates(model).entries == ()
