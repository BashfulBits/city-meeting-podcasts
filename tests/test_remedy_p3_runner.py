from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from citypods.compute.base import JobHandle, JobResult
from citypods.compute.llm_policy import LLMRequestPolicy
from citypods.remedy_evaluation import RemedyConfig
from citypods.remedy_p3_observations import P3Observation
from citypods.remedy_p3_runner import (
    P3_MODELS,
    P3_ROUTES,
    P3DailyRunner,
    _chat_job,
    _jev_answer_for_finding,
    _jev_decision_values,
    _jev_job,
    _response_from_result,
    build_runtime_report,
    p3_enabled,
)


def _evidence() -> dict:
    return {
        "source_key": "test-city",
        "city": {"slug": "test-city", "name": "Test City"},
        "existing_feeds": [
            {
                "slug": "test-city-council",
                "podcast_title": "Test City Council",
                "remedy_policy": {},
            }
        ],
        "historical_archive": {},
        "unexpected_findings": [
            {
                "unexpected_body": "Planning Board",
                "count": 3,
                "date_range": {"earliest": "2026-01-01", "latest": "2026-03-01"},
                "month_counts": {"2026-01": 1, "2026-02": 1, "2026-03": 1},
                "episode_samples": [
                    {"episode_id": "e0", "published": "2026-01-01", "title": "Planning"}
                ],
                "existing_one_off_labels": [],
            }
        ],
    }


def _vote(role: str):
    from citypods.audit_remedy import BodyDecision, P3Vote

    return P3Vote(
        route_id=P3_ROUTES[role],
        upstream_model=f"upstream/{role}",
        reasoning_level=None,
        evidence_hash="a" * 64,
        policy_hash="b" * 64,
        response_hash="c" * 64,
        decision=BodyDecision(
            finding_id="finding-1",
            action="union",
            target_feeds=["test-city-council"],
            rationale="Recurring meetings fit the configured city council feed.",
        ),
    )


def test_live_p3_stays_closed_in_current_shadow_config():
    config = RemedyConfig.model_validate(yaml.safe_load(Path("config/remedy.yml").read_text()))
    assert config.mode == "shadow"
    assert not p3_enabled(config)


def test_daily_policy_hold_writes_report_without_storage_or_worker_access(monkeypatch, tmp_path):
    from scripts import remedy_p3_daily

    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True)
    (root / "config" / "remedy.yml").write_text(
        yaml.safe_dump(yaml.safe_load(Path("config/remedy.yml").read_text()))
    )
    output = tmp_path / "reports" / "daily.json"
    monkeypatch.setattr(remedy_p3_daily, "load_site_config", lambda _path: {})

    def forbidden(*_args, **_kwargs):
        raise AssertionError("a policy hold must not create storage or contact the Worker")

    monkeypatch.setattr(remedy_p3_daily, "make_storage", forbidden)
    monkeypatch.setattr(remedy_p3_daily, "make_default_backend", forbidden)
    result = remedy_p3_daily.run_daily(repo_root=root, output=output)

    assert result["status"] == "policy_hold"
    assert json.loads(output.read_text()) == result
    with pytest.raises(FileExistsError):
        remedy_p3_daily.run_daily(repo_root=root, output=output)


@pytest.mark.parametrize("role", ["sol", "glm_flash", "deepseek"])
def test_chat_jobs_pin_one_exact_free_route(role):
    job = _chat_job(
        role=role,
        finding_id="finding-1",
        evidence_hash="a" * 64,
        policy_hash="b" * 64,
        evidence=_evidence(),
    )
    policy = job.inputs["llm_policy"]

    assert isinstance(policy, LLMRequestPolicy)
    assert policy.allowed_models == (P3_MODELS[role],)
    assert policy.allowed_route_ids == (P3_ROUTES[role],)
    assert policy.allow_paid is False
    assert policy.queue_only is True
    assert job.inputs["num_retries"] == 0
    # The evaluated output cap, so a long reasoning answer is not cut off mid-JSON.
    assert job.inputs["max_tokens"] == 16_384


def test_jev_job_uses_review49_adapter_and_pins_its_route():
    job, packet, item = _jev_job(
        finding_id="finding-1",
        initial_votes=(_vote("sol"), _vote("glm_flash")),
        evidence=_evidence(),
        evidence_hash="a" * 64,
        policy_hash="b" * 64,
        extra_context={"feed_taxonomy": ["test-city-council"]},
    )
    policy = job.inputs["llm_policy"]

    assert item.qid == "finding-1:assignment-choice:T0"
    assert packet.purpose == "remedy:p3"
    assert job.inputs["systemone"]["questions"][item.qid]["type"] == "choice"
    assert set(job.inputs["systemone"]["questions"][item.qid]["criteria"]) == {
        "A",
        "B",
        "C",
        "D",
    }
    assert policy.allowed_models == (P3_MODELS["jev"],)
    assert policy.allowed_route_ids == (P3_ROUTES["jev"],)
    assert policy.allow_paid is False
    assert policy.queue_only is True


def test_completed_chat_result_must_report_pinned_physical_route():
    result = JobResult(
        task="judge",
        recipe_hash="d" * 64,
        model=P3_MODELS["sol"],
        route_id="another-route",
        output={"choices": [{"message": {"content": "{}"}}]},
    )
    with pytest.raises(ValueError, match="pinned P3 route/model"):
        _response_from_result(result, role="sol", finding_id="finding-1")


def test_jev_result_requires_expected_item_and_bounded_probabilities():
    qid = "finding-1:assignment-choice:T0"
    response = {"answers": {qid: {"choice": "C", "probabilities": {"A": 0.2, "B": 0.4}}}}
    result = JobResult(
        task="judge",
        recipe_hash="e" * 64,
        model=P3_MODELS["jev"],
        route_id=P3_ROUTES["jev"],
        output=response,
    )
    assert _response_from_result(result, role="jev", finding_id="finding-1") == response

    malformed = json.loads(json.dumps(response))
    malformed["answers"][qid]["probabilities"]["A"] = 1.2
    result = result.__class__(**{**result.__dict__, "output": malformed})
    raw = _response_from_result(result, role="jev", finding_id="finding-1")
    choice, supported_a, supported_b = _jev_decision_values(raw["answers"].get(qid))
    assert choice == "C"
    assert supported_a is None
    assert supported_b is None


def test_jev_missing_expected_qid_becomes_manual_review_values():
    response = {
        "answers": {
            "foreign:assignment-choice:T0": {
                "choice": "A",
                "probabilities": {"A": 0.9, "B": 0.1},
            }
        }
    }
    answer = _jev_answer_for_finding(response, "finding-1")
    choice, supported_a, supported_b = _jev_decision_values(answer)
    assert (choice, supported_a, supported_b) == (None, None, None)


def test_manual_report_lists_all_source_feeds_not_only_model_suggestions():
    evidence = _evidence()
    evidence["existing_feeds"].append(
        {
            "slug": "test-city-library-board",
            "podcast_title": "Test City Library Board",
            "remedy_policy": {},
        }
    )
    observations = tuple(
        P3Observation(
            schema_version=1,
            observation_id=f"{role}-observation",
            finding_id="finding-1",
            source_key="test-city",
            stage="initial",
            role=role,
            recipe_hash="d" * 64,
            request_hash="e" * 64,
            evidence_hash="a" * 64,
            config_hash="f" * 64,
            policy_hash="b" * 64,
            route_id=P3_ROUTES[role],
            logical_model=role,
            upstream_model=f"test/{role}",
            reasoning_level="high",
            response_hash="c" * 64,
            response={
                "decision": {
                    "finding_id": "f0",
                    "action": "manual_review",
                    "rationale": "The evidence does not support an automatic assignment.",
                }
            },
            observed_at="2026-10-15T00:00:00Z",
        )
        for role in ("sol", "glm_flash")
    )

    report = build_runtime_report(
        finding_id="finding-1",
        evidence_hash="a" * 64,
        policy_hash="b" * 64,
        evidence=evidence,
        observations=observations,
        admitted_routes=frozenset(P3_ROUTES[role] for role in ("sol", "glm_flash")),
        repo_root=".",
    )

    assert report["decision"]["disposition"] == "issue_candidate"
    assert {feed["slug"] for feed in report["feeds"]} == {
        "test-city-council",
        "test-city-library-board",
    }
    for slug in ("test-city-council", "test-city-library-board"):
        assert f"- [ ] Include `{slug}`" in report["manual_issue_candidate"]
        assert f"- [ ] Exclude `{slug}`" in report["manual_issue_candidate"]


def test_daily_runner_reconciles_deferred_worker_handle_before_recording(monkeypatch):
    decision = {
        "finding_id": "f0",
        "action": "union",
        "target_feeds": ["test-city-council"],
        "episode_ids": [],
        "all_observed_episodes": False,
        "new_feed_slug": "",
        "new_feed_title": "",
        "new_feed_description": "",
        "rationale": "Three observed meetings use the council feed taxonomy.",
    }
    result = JobResult(
        task="judge",
        recipe_hash="d" * 64,
        model=P3_MODELS["sol"],
        route_id=P3_ROUTES["sol"],
        upstream_model="gpt-6.1-sol-free",
        output={"choices": [{"message": {"content": json.dumps({"decision": decision})}}]},
    )
    backend = Mock()
    backend.run_inference.return_value = JobHandle(
        task="judge", recipe_hash="d" * 64, backend="llm-dispatch-v2", ref="worker-job"
    )
    backend.reconcile.return_value = result
    runner = P3DailyRunner(
        storage=object(),
        backend=backend,
        lease_factory=lambda: None,
        config=RemedyConfig.model_validate(
            yaml.safe_load(Path("config/remedy.yml").read_text())
        ).model_copy(update={"p3_role_plan": "legacy-sol-v1"}),
        repo_root=".",
    )
    recorded = []
    monkeypatch.setattr(runner, "_load", lambda *_args: ())
    monkeypatch.setattr(runner, "_append", recorded.append)

    completed, pending = runner._submit(
        role="sol",
        stage="initial",
        source_key="test-city",
        finding_id="finding-1",
        evidence_hash="a" * 64,
        policy_hash="b" * 64,
        evidence=_evidence(),
    )

    assert (completed, pending) == (True, False)
    backend.reconcile.assert_called_once()
    assert len(recorded) == 1
    assert recorded[0].route_id == P3_ROUTES["sol"]


def test_selected_plan_pins_new_initial_and_independent_chat_roles():
    from citypods.audit_remedy import P3_ROLE_PLANS

    plan = P3_ROLE_PLANS["flash-deepseek-v2"]
    assert plan.initial == ("glm_flash", "deepseek")
    assert plan.chat == "gemini"
    job = _chat_job(
        role="gemini",
        role_plan=plan.name,
        finding_id="f0",
        evidence_hash="a" * 64,
        policy_hash="b" * 64,
        evidence=_evidence(),
    )
    assert job.inputs["llm_policy"].allowed_route_ids == (plan.routes["gemini"],)
    assert "sol" not in plan.routes


def test_role_plan_rejects_self_adjudication():
    from dataclasses import replace

    from citypods.audit_remedy import P3_ROLE_PLANS

    with pytest.raises(ValueError, match="four distinct roles"):
        replace(P3_ROLE_PLANS["flash-deepseek-v2"], chat="deepseek")


@pytest.mark.parametrize(
    "plan,roles,captions",
    [
        ("legacy-sol-v1", ("sol", "glm_flash"), ("Sol proposal", "GLM Flash proposal")),
        (
            "flash-deepseek-v2",
            ("glm_flash", "deepseek"),
            ("Initial proposal A", "Initial proposal B"),
        ),
    ],
)
def test_runtime_jev_choice_captions_follow_role_plan(plan, roles, captions):
    _, _, item = _jev_job(
        role_plan=plan,
        finding_id="f0",
        initial_votes=tuple(_vote(r) for r in roles),
        evidence=_evidence(),
        evidence_hash="a" * 64,
        policy_hash="b" * 64,
        extra_context={},
    )
    assert item.options[:2] == (("A", captions[0]), ("B", captions[1]))


def test_isolated_plan_is_held_before_any_backend_or_production_storage(monkeypatch, tmp_path):
    import hashlib

    from scripts import remedy_p3_daily

    root = tmp_path
    directory = root / "evals/remedy"
    directory.mkdir(parents=True)
    config = yaml.safe_load(Path("config/remedy.yml").read_text())
    config.update(mode="manual", p3_role_plan="flash-deepseek-v2")
    raw = json.dumps({"version": 1, "config": config, "evidence": {}}).encode()
    plan = directory / "run.json"
    plan.write_bytes(raw)

    def forbidden(*args, **kwargs):
        pytest.fail("gate must precede storage/backend initialization")

    monkeypatch.setattr(remedy_p3_daily, "make_default_backend", forbidden)
    monkeypatch.setattr(remedy_p3_daily, "make_storage", forbidden)
    result = remedy_p3_daily.run_daily(
        repo_root=root,
        output=root / "report.json",
        report_only_plan=plan,
        plan_sha256=hashlib.sha256(raw).hexdigest(),
    )
    assert result["status"] == "policy_hold"
    assert not (root / "report.artifacts").exists()


def test_selected_plan_evidence_hash_uses_runtime_schema_and_nonrecursive_policy():
    from types import SimpleNamespace

    from citypods.remedy_p3_runner import p3_runtime_bindings

    config = RemedyConfig.model_validate(yaml.safe_load(Path("config/remedy.yml").read_text()))
    config = config.model_copy(update={"p3_role_plan": "flash-deepseek-v2"})
    entry = SimpleNamespace(
        physical_route_ids=[P3_ROUTES["glm_flash"]],
        upstream_model="z-ai/glm-5.3-flash-free",
        request_params_hash="a" * 64,
    )
    first = p3_runtime_bindings(config, entry)
    second = p3_runtime_bindings(config.model_copy(update={"admissions": [entry]}), entry)
    assert first == second  # Trust-anchor SHA must not recursively depend on itself.
    assert first["output_tokens"] == 16384
    assert first["accepted_returned_model_ids"] == [
        "z-ai/glm-5.3-flash-free",
        "zai-org/glm-5.3-flash",
    ]


@pytest.mark.parametrize("returned", ["z-ai/glm-5.3-flash-free", "zai-org/glm-5.3-flash"])
@pytest.mark.parametrize("publication_mode", ["report_only", "live"])
def test_approved_glm_alias_preserves_actual_returned_id(returned, publication_mode, monkeypatch):
    from citypods.audit_remedy import P3_ROLE_PLANS

    plan = P3_ROLE_PLANS["flash-deepseek-v2"]
    result = JobResult(
        task="judge",
        recipe_hash="d" * 64,
        model=plan.models["glm_flash"],
        route_id=plan.routes["glm_flash"],
        upstream_model="z-ai/glm-5.3-flash-free",  # Catalog identity supplied by Worker backend.
        output={
            "model": returned,
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "decision": {
                                    "finding_id": "f0",
                                    "action": "manual_review",
                                    "rationale": "Hold",
                                },
                            }
                        )
                    }
                }
            ],
        },
    )
    backend = Mock()
    backend.run_inference.return_value = result
    config = RemedyConfig.model_validate(yaml.safe_load(Path("config/remedy.yml").read_text()))
    config = config.model_copy(
        update={
            "p3_role_plan": plan.name,
            "publication_mode": publication_mode,
        }
    )
    assert not p3_enabled(config, repo_root=Path.cwd())  # Identity approval never grants admission.
    runner = P3DailyRunner(
        storage=object(),
        backend=backend,
        lease_factory=lambda: None,
        config=config,
        repo_root=".",
    )
    recorded = []
    monkeypatch.setattr(runner, "_append", recorded.append)
    assert runner._submit(
        role="glm_flash",
        stage="initial",
        source_key="test-city",
        finding_id="finding-1",
        evidence_hash="a" * 64,
        policy_hash="b" * 64,
        evidence=_evidence(),
    ) == (True, False)
    assert recorded[0].upstream_model == returned
    assert result.upstream_model == "z-ai/glm-5.3-flash-free"


@pytest.mark.parametrize("returned", [None, "z-ai/glm-5.3", "ZAI-ORG/GLM-5.3-FLASH", "other"])
def test_glm_catalog_metadata_does_not_mask_unapproved_returned_model(returned):
    from citypods.audit_remedy import P3_ROLE_PLANS

    plan = P3_ROLE_PLANS["flash-deepseek-v2"]
    result = JobResult(
        task="judge",
        recipe_hash="d" * 64,
        model=plan.models["glm_flash"],
        route_id=plan.routes["glm_flash"],
        upstream_model="z-ai/glm-5.3-flash-free",
        output={"model": returned},
    )
    with pytest.raises(ValueError, match="returned-model identity mismatch"):
        _response_from_result(result, role="glm_flash", finding_id="f0", role_plan=plan.name)


def test_glm_alias_is_exact_route_and_requested_id_only():
    from citypods.remedy_p3_runner import _accepted_returned_models

    alias = "zai-org/glm-5.3-flash"
    assert alias not in _accepted_returned_models("other-route", "z-ai/glm-5.3-flash-free")
    assert alias not in _accepted_returned_models(
        "orcarouter_zai_glm_5_3_flash_free", "z-ai/glm-5.3"
    )


def test_selected_consensus_orders_judge_choice_by_plan_not_vote_input_order():
    from citypods.audit_remedy import (
        P3_ROLE_PLANS,
        P3Adjudication,
        build_p3_review_decision,
    )

    plan = P3_ROLE_PLANS["flash-deepseek-v2"]
    flash, deepseek = _vote("glm_flash"), _vote("deepseek")
    deepseek = deepseek.model_copy(
        update={
            "decision": deepseek.decision.model_copy(update={"target_feeds": []}),
        }
    )
    chat = flash.model_copy(update={"route_id": plan.routes["gemini"]})
    judge = P3Adjudication(
        chat=chat,
        jev_route_id=plan.routes["jev"],
        jev_upstream_model="jev-1.13-free",
        evidence_hash="a" * 64,
        policy_hash="b" * 64,
        response_hash="c" * 64,
        choice="A",
        supported_a=0.9,
        supported_b=0.1,
    )
    result = build_p3_review_decision(
        role_plan=plan.name,
        finding_id="finding-1",
        evidence_hash="a" * 64,
        policy_hash="b" * 64,
        initial_votes=[deepseek, flash],
        initial_validators={plan.routes[r]: True for r in plan.initial},
        adjudication=judge,
        adjudication_validator_passed=True,
        admitted_routes=frozenset(plan.routes.values()),
    )
    assert result.disposition == "pr_candidate"
    with pytest.raises(ValueError, match="legacy DeepSeek field"):
        build_p3_review_decision(
            role_plan=plan.name,
            finding_id="finding-1",
            evidence_hash="a" * 64,
            policy_hash="b" * 64,
            initial_votes=[deepseek, flash],
            initial_validators={plan.routes[r]: True for r in plan.initial},
            adjudication=judge.model_copy(update={"deepseek": chat}),
            admitted_routes=frozenset(plan.routes.values()),
        )


def test_report_only_intent_precedes_submit_and_saved_handle_resumes_without_resubmit(tmp_path):
    from citypods.remedy_p3_runner import P3LocalReportRunner

    config = RemedyConfig.model_validate(yaml.safe_load(Path("config/remedy.yml").read_text()))
    config = config.model_copy(update={"p3_role_plan": "flash-deepseek-v2"})
    backend = Mock()
    runner = P3LocalReportRunner(
        artifact_root=tmp_path,
        storage=None,
        backend=backend,
        config=config,
        repo_root=".",
    )
    job = _chat_job(
        role="deepseek",
        role_plan=config.p3_role_plan,
        finding_id="f0",
        evidence_hash="a" * 64,
        policy_hash="b" * 64,
        evidence=_evidence(),
    )
    handle = JobHandle(task=job.task, recipe_hash=job.recipe_hash, backend="worker", ref="job-1")

    def submit(_job):
        assert (tmp_path / "calls" / (job.recipe_hash + "-intent.json")).exists()
        return handle

    backend.run_inference.side_effect = submit
    assert runner._run_job(job) == handle
    assert runner._run_job(job) == handle
    backend.run_inference.assert_called_once()
    receipt = tmp_path / "calls" / (job.recipe_hash + "-receipt.json")
    saved = json.loads(receipt.read_text())
    saved["value"]["recipe_hash"] = "f" * 64
    receipt.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match="request binding"):
        runner._run_job(job)
    backend.run_inference.assert_called_once()
    receipt.unlink()
    with pytest.raises(RuntimeError, match="unresolved"):
        runner._run_job(job)
    backend.run_inference.assert_called_once()


def test_evaluation_adapter_preserves_feed_set_and_one_recording_scope():
    from citypods.remedy_p3_runner import adapt_evaluated_body_selection

    case = {
        "case_id": "case-1",
        "existing_feeds": [{"slug": "test-city-council"}],
        "evidence_refs": [{"id": "official"}],
        "recordings": [{"uid": "uid-1", "provider_guid": "guid-1"}],
    }
    answer = {
        "case_id": "case-1",
        "publication_action": "assign_existing",
        "feed_slugs": ["test-city-council"],
        "evidence_ref_ids": ["official"],
        "bodies": [{"name": "Council"}],
        "missing_evidence": [],
    }
    result = adapt_evaluated_body_selection(answer, case)
    assert result.action == "single_uid_inclusion"
    assert result.target_feeds == ["test-city-council"]
    assert result.episode_ids == ["e0"] and result.all_observed_episodes is False
    for action in [
        "unresolved",
        "archive_only",
        "archive_series",
        "exclude_event",
        "unconfigured_body",
    ]:
        held = adapt_evaluated_body_selection(answer | {"publication_action": action}, case)
        assert held.action == "manual_review" and not held.target_feeds
    with pytest.raises(ValueError, match="case ID"):
        adapt_evaluated_body_selection(answer | {"case_id": "other"}, case)
    with pytest.raises(ValueError, match="unknown or duplicate feed"):
        adapt_evaluated_body_selection(answer | {"feed_slugs": ["invented"]}, case)
    held = adapt_evaluated_body_selection(answer | {"missing_evidence": ["ownership"]}, case)
    assert held.action == "manual_review"


def test_concise_adjudicators_keep_each_evidence_block_once():
    from citypods.audit_remedy import P3_ROLE_PLANS, _compact_evidence
    from citypods.remedy_p3_runner import _adjudication_context

    plan = P3_ROLE_PLANS["flash-deepseek-v2"]
    evidence = _evidence()
    evidence["historical_archive"] = {"note": "unique_archive_marker"}
    votes = [_vote(role) for role in plan.initial]
    context = _adjudication_context(votes, evidence, plan.name)
    assert set(context) == {"initial_proposals", "archive_context"}
    taxonomy = json.dumps(_compact_evidence(evidence)["existing_feeds"], sort_keys=True)
    chat = _chat_job(
        role="gemini",
        role_plan=plan.name,
        finding_id="f0",
        evidence_hash="a" * 64,
        policy_hash="b" * 64,
        evidence=evidence,
        extra_context=context,
    )
    prompt = chat.inputs["messages"][0]["content"]
    assert prompt.count(taxonomy) == 1
    assert prompt.count("unique_archive_marker") == 1
    assert all(v.route_id not in prompt for v in votes)
    job, _, item = _jev_job(
        role_plan=plan.name,
        finding_id="f0",
        initial_votes=tuple(votes),
        evidence=evidence,
        evidence_hash="a" * 64,
        policy_hash="b" * 64,
        extra_context=context,
    )
    wire = next(iter(job.inputs["systemone"]["questions"].values()))["instructions"]
    assert wire.count(taxonomy) == 1
    assert wire.count(item.question.instruction) == 1
    assert wire.count("unique_archive_marker") == 1
    data = json.loads(item.evidence.text)
    assert data["source_evidence"] == _compact_evidence(evidence)
    assert "initial_proposals" not in data
    assert data["A"] == votes[0].decision.model_dump(mode="json")
    assert data["B"] == votes[1].decision.model_dump(mode="json")


def test_concise_jev_recipe_changes_when_actual_proposal_changes():
    from citypods.audit_remedy import P3_ROLE_PLANS
    from citypods.remedy_p3_runner import _adjudication_context

    plan = P3_ROLE_PLANS["flash-deepseek-v2"]
    evidence = _evidence()
    votes = [_vote(role) for role in plan.initial]

    def build(current):
        return _jev_job(
            role_plan=plan.name,
            finding_id="f0",
            initial_votes=tuple(current),
            evidence=evidence,
            evidence_hash="a" * 64,
            policy_hash="b" * 64,
            extra_context=_adjudication_context(current, evidence, plan.name),
        )[0]

    original = build(votes)
    changed = votes[0].model_copy(
        update={"decision": votes[0].decision.model_copy(update={"rationale": "Changed candidate"})}
    )
    assert build([changed, votes[1]]).recipe_hash != original.recipe_hash
