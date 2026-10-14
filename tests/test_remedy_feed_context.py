"""A small separate diagnostic changes context, not original evaluation answers or attempts."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKET = ROOT / "evals/remedy/holdout/feed-context-2026-10-14/packet.json"


@pytest.fixture
def probe(monkeypatch):
    from citypods.compute import llm, llm_policy

    registries = [llm_policy.ROUTE_REGISTRY, llm_policy.ROUTE_CANDIDATES, llm_policy.ROUTES]
    originals = [dict(registry) for registry in registries]
    monkeypatch.setattr(llm, "SUPPORTED_MODELS", llm.SUPPORTED_MODELS)
    body = ROOT / "evals/remedy/results/body-selection-2026-10-13"
    monkeypatch.syspath_prepend(str(body))
    path = ROOT / "evals/remedy/results/feed-context-2026-10-14/run.py"
    spec = importlib.util.spec_from_file_location("feed_context_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    for registry, original in zip(registries, originals, strict=True):
        registry.clear()
        registry.update(original)


def test_two_contexts_keep_evidence_and_exclude_target_from_examples(probe):
    packet = json.loads(PACKET.read_text())
    assert len(packet["models"]) == 7
    assert "qwen38" not in packet["models"]
    original = packet["case"]
    for payload in packet["variants"].values():
        case = json.loads(payload[1]["content"])
        assert case["evidence_refs"] == original["evidence_refs"]
        assert case["recordings"] == original["recordings"]
        assert case["existing_feeds"] == original["existing_feeds"]
    titles = json.loads(packet["variants"]["title_examples"][1]["content"])
    assert "publication_membership_policy" not in titles
    assert len(titles["existing_feed_title_examples"]) == 6
    assert sum(map(len, titles["existing_feed_title_examples"].values())) <= 12
    for inventory in packet["inventories"]:
        assert all(r["guid"] != original["recordings"][0]["uid"] for r in inventory["items"])
    assert all("gold" not in json.dumps(v) for v in packet["variants"].values())


@pytest.mark.parametrize("status", ["completed", "failed", "unknown_outcome"])
def test_submitted_diagnostic_job_is_never_retried(probe, status):
    packet = json.loads(PACKET.read_text())
    jobs = probe.job_plan(packet, [])
    assert len(jobs) == 14
    row = jobs[0] | {"status": status, "provider_attempts": 1}
    assert len(probe.job_plan(packet, [row])) == 13
    assert len(probe.job_plan(packet, [row, row | {"provider_attempts": 0}])) == 13


def test_unknown_intent_preserves_variant_and_holds_submission(probe, tmp_path):
    (tmp_path / "intents").mkdir()
    (tmp_path / "batches").mkdir()
    row = {
        "mode": "feed_context/title_examples",
        "candidate": "sol",
        "provider_attempts": 1,
        "attempt_id": "pending",
    }
    (tmp_path / "intents/pending.json").write_text(json.dumps(row))
    loaded = probe.observations(tmp_path)
    assert loaded[0]["variant"] == "title_examples"
    assert loaded[0]["status"] == "unknown_outcome"
    assert len(probe.job_plan(json.loads(PACKET.read_text()), loaded)) == 13


def test_provider_cooldown_keeps_other_variant_waiting(probe):
    packet = json.loads(PACKET.read_text())
    until = "2026-10-14T10:00:00+00:00"
    jobs = probe.job_plan(packet, [], cooldowns={"sol": until})
    assert all(j["next_eligible_at"] == until for j in jobs if j["candidate"] == "sol")
    assert all(j["next_eligible_at"] is None for j in jobs if j["candidate"] != "sol")


def test_optional_messages_leave_default_schema_shape_unchanged(probe):
    packet = json.loads(PACKET.read_text())
    case = probe.SelectionInput.model_validate(packet["case"])
    route = probe.runner.route_for("gptoss")
    ordinary = probe.runner.shaped_messages(case, route)
    assert ordinary == probe.runner.shaped_messages(case, route, None)
    variant = probe.runner.shaped_messages(case, route, packet["variants"]["title_examples"])
    assert ordinary != variant
    assert "existing_feed_title_examples" in variant[1]["content"]


@pytest.mark.parametrize(
    "candidate,variant",
    [
        (model, variant)
        for model in ["flashlite", "kimi", "glm_full", "glm_flash", "sol", "deepseek", "gptoss"]
        for variant in ["explicit_policy", "title_examples"]
    ],
)
def test_actual_request_uses_variant_and_single_attempt(
    probe, monkeypatch, tmp_path, candidate, variant
):
    packet = json.loads(PACKET.read_text())
    case = probe.SelectionInput.model_validate(packet["case"])
    route = probe.runner.route_for(candidate)
    payload = packet["variants"][variant]
    seen = []
    expected = {
        "case_id": case.case_id,
        "bodies": [{"name": case.body_label, "evidence_ref_ids": ["provider-player"]}],
        "feed_slugs": ["dallas-tx-2024-bond-streets-and-transportation-subcommittee"],
        "publication_action": "assign_existing",
        "evidence_ref_ids": ["provider-player"],
        "missing_evidence": [],
        "rationale": "Fixture",
    }

    class Backend:
        def __init__(self, *_a, **_k):
            pass

        def _completion_fn(self):
            def response(**_kwargs):
                assert len(list((tmp_path / "intents").glob("*.json"))) == 1
                return {
                    "model": route.upstream_model,
                    "choices": [{"message": {"content": json.dumps(expected)}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 10},
                }

            return response

        def run_immediate(self, job):
            seen.append(job.inputs)
            controls = {"reasoning_effort": "high", "reasoning_format": "parsed"}
            return SimpleNamespace(
                output=self._completion(extra_body=controls), upstream_model=route.upstream_model
            )

    monkeypatch.setattr(probe.runner.transport, "LiteLLMBackend", Backend)
    monkeypatch.setattr(
        probe.runner.transport, "headroom", lambda *_a, **_kw: (True, {"kind": "available"})
    )
    monkeypatch.setattr(
        probe.runner, "token_headroom", lambda *_a, **_kw: (True, {"kind": "available"})
    )
    row = probe.runner.attempt(
        tmp_path,
        candidate,
        case,
        0,
        "packet-hash",
        object(),
        None,
        coordination_bypass=True,
        request_messages=payload,
        experiment_mode=f"feed_context/{variant}",
    )
    assert row["status"] == "completed"
    assert row["mode"] == f"feed_context/{variant}"
    assert seen[0]["messages"] == payload
    assert seen[0]["max_provider_attempts"] == 1
    assert seen[0]["num_retries"] == 0
    assert row["prompt_hash"] == probe.canonical_hash(payload)
    assert row["provider_attempts"] == 1
    assert row["provider_usage"]["prompt_tokens"] == 100
