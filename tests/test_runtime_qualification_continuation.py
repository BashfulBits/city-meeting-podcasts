"""Continuation acknowledges one pinned input failure without weakening other holds."""

import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def continuation(tmp_path, monkeypatch):
    folder = Path(__file__).parents[1] / "evals/remedy"
    monkeypatch.syspath_prepend(str(folder))
    spec = importlib.util.spec_from_file_location(
        "runtime_continuation_test", folder / "continue_runtime_qualification.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    output = tmp_path / "measurement"
    output.mkdir()
    packet = tmp_path / "packet"
    packet.mkdir()

    def save(path, value):
        path.write_text(json.dumps(value))

    save(packet / "plan.json", {})
    save(packet / "model-inputs.json", {"cases": [{"case_id": "RQ11"}, {"case_id": "RQ12"}]})
    save(
        tmp_path / "authority.json", {"plan_path": "packet/plan.json", "output_path": "measurement"}
    )
    result = output / "RQ10--jev-result.json"
    save(
        result,
        {
            "http_status": 400,
            "provider_outcome": "response_received",
            "raw_response": {
                "error": {
                    "retryable": False,
                    "message": (
                        "The input is too long for this model. "
                        "Shorten it, or split it across several requests."
                    ),
                }
            },
        },
    )
    save(output / "RQ10--jev-intent.json", {})
    approval = tmp_path / "approval.json"
    save(
        approval,
        {
            "approved": True,
            "executor_sha256": module.digest(module.__file__),
            "allowed_case_ids": ["RQ11", "RQ12"],
            "retry_completed_or_failed_requests": False,
            "original_authority_path": "authority.json",
            "original_authority_sha256": module.digest(tmp_path / "authority.json"),
            "acknowledged_terminal_holds": {result.name: module.digest(result)},
        },
    )

    def blocked_preflight(*_args):
        raise RuntimeError("unresolved access/profile/identity hold; no automatic resume")

    monkeypatch.setattr(module, "preflight", blocked_preflight)
    return module, approval, output, save


def test_only_untouched_cases_are_selected(continuation):
    module, approval, _, _ = continuation
    assert [c["case_id"] for c in module.validate(approval)[3]] == ["RQ11", "RQ12"]


def test_new_auth_hold_and_unknown_intent_still_block(continuation):
    module, approval, output, save = continuation
    intent = output / "another--jev-intent.json"
    save(intent, {})
    with pytest.raises(RuntimeError, match="unresolved intent"):
        module.validate(approval)
    save(
        output / "another--jev-result.json",
        {"http_status": 403, "provider_outcome": "response_received"},
    )
    with pytest.raises(RuntimeError, match="new access/profile/identity hold"):
        module.validate(approval)


def test_touched_continuation_case_cannot_be_repeated(continuation):
    module, approval, output, save = continuation
    save(output / "RQ11--glm_flash-result.json", {"status": "completed"})
    with pytest.raises(ValueError, match="already touched"):
        module.validate(approval)
