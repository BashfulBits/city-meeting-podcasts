"""The two-model plan stops at human review and never calls automated adjudicators."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from citypods.audit_remedy import P3_ROLE_PLANS
from citypods.compute.base import JobHandle, JobResult
from citypods.compute.llm import LLMBackendError, LLMStructuredOutputError
from citypods.remedy_evaluation import RemedyConfig
from citypods.remedy_p3_runner import P3LocalReportRunner, p3_enabled


@pytest.mark.parametrize(
    "outcome",
    [
        "agree",
        "disagree",
        "invalid",
        "failure",
        "validator_hold",
        "pending_peer_failure",
        "unknown_finding",
        "wrong_recipe",
        "wrong_returned_model",
        "transport_error",
        "capacity_error",
        "unknown_request",
    ],
)
def test_two_model_flow_preserves_results_and_never_calls_judges(tmp_path, monkeypatch, outcome):
    import citypods.remedy_p3_runner as runtime

    plan = P3_ROLE_PLANS["flash-deepseek-human-v3"]
    config = RemedyConfig.model_validate(yaml.safe_load(Path("config/remedy.yml").read_text()))
    assert config.p3_role_plan == plan.name
    assert config.mode == "manual" and config.publication_mode == "live"
    assert not config.qualified_alias_types and p3_enabled(config, repo_root=Path.cwd())
    config = config.model_copy(
        update={
            "mode": "shadow",
            "publication_mode": "report_only",
            "admissions": [],
            "manual_review_authorization": None,
        }
    )
    assert not p3_enabled(config, repo_root=Path.cwd())
    assert set(plan.routes) == {"glm_flash", "deepseek"} and plan.chat is None
    monkeypatch.setattr(runtime, "p3_enabled", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(runtime, "admitted_p3_routes", lambda _: frozenset(plan.routes.values()))
    monkeypatch.setattr(runtime, "_validator_passed", lambda *_args: outcome != "validator_hold")
    backend = Mock()
    backend.reconcile.return_value = None

    def complete(job):
        route = job.inputs["llm_policy"].allowed_route_ids[0]
        assert route in plan.routes.values()
        role = next(r for r in plan.initial if plan.routes[r] == route)
        if role == "glm_flash" and outcome == "pending_peer_failure":
            return JobHandle(
                task=job.task, recipe_hash=job.recipe_hash, backend="llm-dispatch-v2", ref="pending"
            )
        if role == "deepseek" and outcome in {"failure", "pending_peer_failure"}:
            raise LLMStructuredOutputError(
                "Do not copy secret-bearing exception messages to reports"
            )
        if role == "deepseek" and outcome in {
            "transport_error",
            "capacity_error",
            "unknown_request",
        }:
            if outcome == "unknown_request":
                raise RuntimeError(outcome)
            raise LLMBackendError(outcome)
        content = {
            "decision": {
                "finding_id": "f0",
                "action": "union",
                "target_feeds": ["test-city-council"],
                "all_observed_episodes": True,
                "rationale": "Observed council meetings",
            }
        }
        if role == "deepseek" and outcome == "disagree":
            content["decision"].update(action="manual_review", target_feeds=[])
        if role == "deepseek" and outcome == "invalid":
            content = {"invalid_contract": True}
        if role == "deepseek" and outcome == "unknown_finding":
            content["decision"]["finding_id"] = "unknown"
        upstream = runtime._upstream_for(role, plan.name)
        return JobResult(
            task=job.task,
            recipe_hash=(
                "a" * 64 if role == "deepseek" and outcome == "wrong_recipe" else job.recipe_hash
            ),
            model=plan.models[role],
            route_id=route,
            upstream_model=upstream,
            output={
                "model": (
                    "other-model"
                    if role == "deepseek" and outcome == "wrong_returned_model"
                    else upstream
                ),
                "choices": [{"message": {"content": json.dumps(content)}}],
            },
        )

    backend.run_inference.side_effect = complete
    runner = P3LocalReportRunner(
        artifact_root=tmp_path,
        storage=object(),
        backend=backend,
        config=config,
        repo_root=".",
    )
    finding = {"unexpected_body": "Council", "episodes": []}
    source = {
        "source_key": "test-city",
        "city": {"slug": "test-city"},
        "existing_feeds": [{"slug": "test-city-council", "podcast_title": "Council"}],
        "unexpected_findings": [finding],
    }
    if outcome in {"transport_error", "capacity_error", "unknown_request"}:
        with pytest.raises(RuntimeError, match=outcome):
            runner.run_finding(source, finding)
        records = [json.loads(p.read_text()) for p in tmp_path.glob("*.json")]
        assert not any("failure" in row.get("response", {}) for row in records)
        with pytest.raises(RuntimeError, match="unresolved report-only intent"):
            runner.run_finding(source, finding)
        assert backend.run_inference.call_count == 2
        return
    result = runner.run_finding(source, finding)
    assert result["status"] == "report_ready"
    report = result["report"]
    expected = "pr_candidate" if outcome == "agree" else "issue_candidate"
    assert report["decision"]["disposition"] == expected
    assert report["decision"]["adjudication"] is None
    assert backend.run_inference.call_count == 2
    assert runner.run_finding(source, finding)["report"] == report
    assert backend.run_inference.call_count == 2  # Terminal failures also do not re-ask models.
    if outcome != "agree":
        assert "proposal_candidate" not in report
        assert "manual_issue_candidate" in report
    if outcome in {
        "invalid",
        "failure",
        "pending_peer_failure",
        "unknown_finding",
        "wrong_recipe",
        "wrong_returned_model",
    }:
        assert len(report["decision"]["initial_votes"]) == (
            0 if outcome == "pending_peer_failure" else 1
        )
        assert plan.routes["deepseek"] in report["initial_failures"]
        records = [json.loads(p.read_text()) for p in tmp_path.glob("*.json")]
        failed = next(r for r in records if "failure" in r.get("response", {}))
        assert failed["upstream_model"] == "unverified"
        assert failed["recipe_hash"] in report["evidence_summary"]
        assert "secret-bearing" not in json.dumps(failed)
        assert "secret-bearing" not in json.dumps(report)


def test_human_plan_rejects_adjudicator_observations():
    from citypods.remedy_p3_observations import make_observation

    with pytest.raises(ValueError, match="role is not valid"):
        make_observation(
            finding_id="finding-1",
            source_key="test-city",
            stage="adjudication",
            role="jev",
            recipe_hash="a" * 64,
            request_hash="b" * 64,
            evidence_hash="c" * 64,
            config_hash="d" * 64,
            policy_hash="e" * 64,
            route_id="beatapi_jev_1_13_free",
            logical_model="typesafe/jev-1.13",
            upstream_model="jev-1.13-free",
            reasoning_level=None,
            response={},
            role_plan="flash-deepseek-human-v3",
        )
