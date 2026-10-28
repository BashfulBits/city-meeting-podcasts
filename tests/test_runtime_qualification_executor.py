"""Offline invariants for the explicitly approved measurement, never live provider tests."""

import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.fixture
def executor():
    path = Path(__file__).parents[1] / "evals/remedy/run_runtime_qualification.py"
    spec = importlib.util.spec_from_file_location("runtime_measurement_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def controller(executor, tmp_path, monkeypatch):
    plan = {
        "source_sha256": {},
        "runtime_bindings": {
            "glm_flash": {
                "accepted_returned_model_ids": ["zai-org/glm-5.3-flash"],
            }
        },
    }
    client = Mock()
    client.reserve.return_value = {"ok": True}
    result = executor.Controller(plan, {}, tmp_path, client)
    monkeypatch.setattr(result, "wait", lambda _: None)
    monkeypatch.setattr(result, "renew", lambda: None)
    monkeypatch.setattr(
        result,
        "status",
        lambda p: {
            "in_flight": 0,
            "routes": {executor.PLAN.routes["glm_flash"]: {"provider": p}},
        },
    )
    monkeypatch.setattr(executor, "validate_source_url", lambda _: None)
    return result


def test_terminal_result_reused_without_second_call_or_changed_request(
    executor, tmp_path, monkeypatch
):
    ctl = controller(executor, tmp_path, monkeypatch)
    raw = {
        "model": "zai-org/glm-5.3-flash",
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
    }
    response = Mock(status_code=200)
    response.iter_content.return_value = [json.dumps(raw).encode()]
    post = Mock(return_value=response)
    monkeypatch.setattr(executor.requests, "post", post)
    monkeypatch.setenv("ORCAROUTER_API_KEY", "DUMMY_NOT_A_CREDENTIAL")
    payload = {"model": "z-ai/glm-5.3-flash-free", "max_tokens": 16384}
    first = ctl.call("RQ01", "glm_flash", payload, "a" * 64, "f0")
    assert first["status"] == "completed"
    assert ctl.call("RQ01", "glm_flash", payload, "a" * 64, "f0") == first
    with pytest.raises(ValueError, match="different frozen request"):
        ctl.call("RQ01", "glm_flash", {}, "a" * 64, "f0")
    assert post.call_count == 1
    assert post.call_args.kwargs["allow_redirects"] is False
    assert "DUMMY_NOT_A_CREDENTIAL" not in "".join(p.read_text() for p in tmp_path.glob("*.json"))


def test_unresolved_intent_blocks_provider_before_reservation(executor, tmp_path, monkeypatch):
    ctl = controller(executor, tmp_path, monkeypatch)
    (tmp_path / "RQ01--glm_flash-intent.json").write_text("{}")
    post = Mock()
    monkeypatch.setattr(executor.requests, "post", post)
    with pytest.raises(RuntimeError, match="unknown provider request"):
        ctl.call("RQ01", "glm_flash", {}, "a" * 64, "f0")
    post.assert_not_called()
    ctl.client.reserve.assert_not_called()


def test_attempt_49_is_blocked_before_reservation(executor, tmp_path, monkeypatch):
    ctl = controller(executor, tmp_path, monkeypatch)
    for n in range(48):
        (tmp_path / f"prior-{n}-intent.json").write_text("{}")
    post = Mock()
    monkeypatch.setattr(executor.requests, "post", post)
    with pytest.raises(RuntimeError, match="attempt budget exhausted"):
        ctl.call("RQ01", "glm_flash", {}, "a" * 64, "f0")
    post.assert_not_called()
    ctl.client.reserve.assert_not_called()


def test_foreign_pause_is_not_overwritten(executor, tmp_path):
    client = Mock()
    client.status.return_value = {
        "ok": True,
        "pauses": [
            {"scope": "provider:beatapi", "reason": "foreign-owner"},
        ],
    }
    ctl = executor.Controller({}, {}, tmp_path, client)
    with pytest.raises(RuntimeError, match="foreign pause"):
        ctl.arm("beatapi")
    client.pause.assert_not_called()
    assert not ctl.owned
