import json
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

from citypods.judging import decision_batch_cloud as cloud
from citypods.judging.decision_batch_transport import PreconditionFailed
from scripts.decision_batch_pilot import _active_windows

JOB = "projects/test/locations/us-central1/jobs/decision-batch"


def test_reservation_covers_billing_month_boundary():
    pending_at = datetime(2026, 12, 1, 1, tzinfo=UTC)
    assert _active_windows(pending_at) == {"canary", "2026-11", "2026-12"}


def test_gcs_reads_pinned_generation(monkeypatch):
    calls = []

    def read(request):
        calls.append(request)
        return b'{"generation":"8"}' if len(calls) == 1 else b"data"

    monkeypatch.setattr(cloud, "_read", read)
    store = cloud.GCSObjectStore("test-bucket", lambda: "opaque")
    assert store.get("pending/item.jsonl.gz") == (b"data", 8)
    assert ["8"] == parse_qs(urlsplit(calls[1].full_url).query)["generation"]
    assert "%2F" in calls[0].full_url
    assert calls[1].get_header("Authorization") == "Bearer opaque"


def test_gcs_cas_and_missing_object(monkeypatch):
    def read(request):
        query = parse_qs(urlsplit(request.full_url).query)
        if request.get_method() == "POST":
            assert query["ifGenerationMatch"] == ["0"]
            assert request.data == b"payload"
            raise HTTPError(request.full_url, 412, "precondition", {}, None)
        raise HTTPError(request.full_url, 404, "not found", {}, None)

    monkeypatch.setattr(cloud, "_read", read)
    store = cloud.GCSObjectStore("test-bucket", lambda: "opaque")
    with pytest.raises(PreconditionFailed):
        store.put("body", b"payload", 0)
    assert store.get("body", 10) is None


def test_launcher_checks_acknowledgement(monkeypatch):
    commands = []

    def run(args, **kwargs):
        commands.append(args)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"metadata": {"name": "e"}}))

    monkeypatch.setattr(cloud.subprocess, "run", run)
    launcher = cloud.CloudRunLauncher(JOB, lambda: "opaque")
    assert launcher.launch("m", "a", "regular") == f"{JOB}/executions/e"
    assert "--no-delay-execution" in commands[0]
    assert "--async" in commands[0]
    assert commands[0][5:7] == ["decision-batch", "--project=test"]
    with pytest.raises(ValueError):
        launcher.launch("m,OTHER=value", "a", "delayed")
    assert len(commands) == 1


def test_terminal_execution_must_match_attempt(monkeypatch):
    response = {
        "completionTime": "2026-11-15T00:00:00Z",
        "conditions": [{"type": "Completed", "state": "CONDITION_FAILED"}],
        "template": {
            "containers": [
                {
                    "env": [
                        {"name": "DECISION_BATCH_MANIFEST_ID", "value": "m"},
                        {"name": "DECISION_BATCH_ATTEMPT_ID", "value": "a"},
                    ],
                }
            ],
        },
    }
    monkeypatch.setattr(cloud, "_read", lambda request: json.dumps(response).encode())
    launcher = cloud.CloudRunLauncher(JOB, lambda: "opaque")
    assert launcher.execution_state(f"{JOB}/executions/e", "m", "a") == "CONDITION_FAILED"
    with pytest.raises(ValueError, match="does not match"):
        launcher.execution_state(f"{JOB}/executions/e", "m", "wrong")
    with pytest.raises(ValueError, match="configured job"):
        launcher.execution_state(f"{JOB}2/executions/e", "m", "a")


def test_canary_refuses_more_tasks_or_retries_than_reserved(monkeypatch):
    digest = "sha256:" + "a" * 64
    task = {
        "maxRetries": 0,
        "timeout": "300s",
        "gpuZonalRedundancyDisabled": True,
        "nodeSelector": {"accelerator": "nvidia-l4"},
        "containers": [
            {
                "image": f"registry/project/image@{digest}",
                "env": [{"name": "CITYPODS_IMAGE_DIGEST", "value": digest}],
                "resources": {"limits": {"cpu": "4", "memory": "16Gi", "nvidia.com/gpu": "1"}},
            }
        ],
    }
    template = {"taskCount": 1, "parallelism": 1, "template": task}
    permissions = ["run.jobs.run", "run.jobs.runWithOverrides"]

    def read(request):
        if request.full_url.endswith(":testIamPermissions"):
            assert json.loads(request.data)["permissions"] == [
                "run.jobs.run",
                "run.jobs.runWithOverrides",
            ]
            return json.dumps({"permissions": permissions}).encode()
        return json.dumps({"template": template}).encode()

    monkeypatch.setattr(cloud, "_read", read)
    launcher = cloud.CloudRunLauncher(JOB, lambda: "opaque")
    launcher.verify_canary_job(digest)
    permissions.pop()
    with pytest.raises(PermissionError):
        launcher.verify_canary_job(digest)
    task["containers"][0]["env"][0]["value"] = "sha256:" + "b" * 64
    with pytest.raises(ValueError, match="image or resource"):
        launcher.verify_canary_job(digest)
    task["maxRetries"] = 1
    with pytest.raises(ValueError, match="canary envelope"):
        launcher.verify_canary_job(digest)
