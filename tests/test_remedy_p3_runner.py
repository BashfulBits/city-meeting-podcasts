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
        config=RemedyConfig.model_validate(yaml.safe_load(Path("config/remedy.yml").read_text())),
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
