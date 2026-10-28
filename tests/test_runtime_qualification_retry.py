"""The one approved retry must preserve history and share budget and provider spacing."""

import importlib.util
import json
import time
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.fixture
def retry_module(monkeypatch):
    directory = Path(__file__).parents[1] / "evals/remedy"
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location(
        "runtime_retry_test", directory / "retry_runtime_qualification.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_global_cap_includes_the_nested_retry(retry_module, tmp_path, monkeypatch):
    retry = tmp_path / "approved-retry"
    retry.mkdir()
    for n in range(48):
        directory = retry if n == 47 else tmp_path
        (directory / f"prior-{n}-intent.json").write_text(
            json.dumps({"provider": "orcarouter", "started_epoch": 1})
        )
    ctl = retry_module.RetryController({}, {}, tmp_path, Mock())
    post = Mock()
    import run_runtime_qualification as native

    monkeypatch.setattr(native.requests, "post", post)
    with pytest.raises(RuntimeError, match="cumulative approved 48-attempt"):
        ctl.call("RQ02", "glm_flash", {}, "a" * 64, "f0")
    post.assert_not_called()
    ctl.client.reserve.assert_not_called()


def test_approved_retry_preserves_failure_and_cannot_repeat(retry_module, tmp_path, monkeypatch):
    import run_runtime_qualification as native

    original = tmp_path / "RQ01--glm_flash-result.json"
    original.write_text('{"http_status":429,"provider_outcome":"response_received"}\n')
    failure_bytes = original.read_bytes()
    now = time.time()
    (tmp_path / "RQ01--glm_flash-intent.json").write_text(
        json.dumps({"provider": "orcarouter", "started_epoch": now})
    )
    ctl = retry_module.RetryController(
        {
            "source_sha256": {},
            "runtime_bindings": {
                "glm_flash": {"accepted_returned_model_ids": ["zai-org/glm-5.3-flash"]}
            },
        },
        {},
        tmp_path,
        Mock(),
    )
    ctl.client.reserve.return_value = {"ok": True}
    waits = []
    monkeypatch.setattr(ctl, "wait", waits.append)
    monkeypatch.setattr(ctl, "renew", lambda: None)
    monkeypatch.setattr(
        ctl, "status", lambda _: {"in_flight": 0, "routes": {native.PLAN.routes["glm_flash"]: {}}}
    )
    monkeypatch.setattr(native, "validate_source_url", lambda _: None)
    monkeypatch.setenv("ORCAROUTER_API_KEY", "DUMMY")
    response = Mock(status_code=200)
    response.iter_content.return_value = [
        json.dumps(
            {
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
                                    }
                                }
                            )
                        }
                    }
                ],
            }
        ).encode()
    ]
    post = Mock(return_value=response)
    monkeypatch.setattr(native.requests, "post", post)
    first = ctl.call("RQ01", "glm_flash", {"model": "z-ai/glm-5.3-flash-free"}, "a" * 64, "f0")
    second = ctl.call("RQ01", "glm_flash", {"model": "z-ai/glm-5.3-flash-free"}, "a" * 64, "f0")
    assert first == second
    assert first["status"] == "completed"
    assert original.read_bytes() == failure_bytes
    assert len(list(tmp_path.rglob("*-intent.json"))) == 2
    assert max(waits) > 60  # The global provider spacing includes the original request.
    assert post.call_count == 1
