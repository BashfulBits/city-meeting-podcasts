"""Tests for bounded bundled LLM dispatch v2 Python client."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from citypods.compute.base import InferenceJob, JobHandle, JobResult
from citypods.compute.llm import LiteLLMBackend, LLMBackendConfig, LLMBackendError


class MockStorage:
    def __init__(self):
        self.files = {}
        self.cas_capable = True

    def write_bytes(self, key, data, content_type=None):
        self.files[key] = data if isinstance(data, bytes) else str(data).encode("utf-8")

    def read_bytes(self, key):
        return self.files.get(key)

    def exists(self, key):
        return key in self.files

    def delete(self, key):
        self.files.pop(key, None)

    def put_file(self, key, local_path, content_type=None):
        self.files[key] = Path(local_path).read_bytes()
        return "mem://" + key

    def get_file(self, key, local_path):
        if key not in self.files:
            return False
        p = Path(local_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(self.files[key])
        return True

    def list_objects(self, prefix=""):
        for k in sorted(self.files):
            if k.startswith(prefix):
                yield k, None


def _mock_response(status_code=200, json_data=None, headers=None, text=""):
    resp = requests.Response()
    resp.status_code = status_code
    resp._content = json.dumps(json_data or {}).encode("utf-8")
    resp.headers = headers or {"content-type": "application/json"}
    return resp


def test_enqueue_batch_submits_jobs_and_persists_payloads_to_b2():
    storage = MockStorage()
    mock_session = MagicMock()

    def mock_post(url, json=None, headers=None, timeout=None):
        jobs = json.get("jobs", [])
        return _mock_response(
            status_code=200,
            json_data={
                "accepted": [{"id": j["id"]} for j in jobs],
                "rejected": [],
            },
        )

    mock_session.post.side_effect = mock_post

    config = LLMBackendConfig(
        model="gemini/gemini-3-flash-preview",
        dispatch_v2_url="https://dispatch-v2.example.com",
        dispatch_v2_auth_token="secret-v2",
    )
    backend = LiteLLMBackend(config, http_session=mock_session, storage=storage)

    job1 = InferenceJob(
        task="tag",
        inputs={"messages": [{"role": "user", "content": "hello"}]},
        recipe_hash="recipe1",
    )
    job2 = InferenceJob(
        task="tag",
        inputs={"messages": [{"role": "user", "content": "world"}]},
        recipe_hash="recipe2",
    )

    results = backend.enqueue_batch([job1, job2])
    assert len(results) == 2
    assert all(isinstance(r, JobHandle) for r in results)
    assert results[0].backend == "llm-dispatch-v2"
    assert results[1].backend == "llm-dispatch-v2"
    assert backend._daily_ingest_admitted == 2

    # Check that payloads were written to storage
    stored_payload_keys = [k for k in storage.files if k.startswith("payloads/")]
    assert len(stored_payload_keys) == 2


def test_enqueue_batch_cached_results():
    storage = MockStorage()
    mock_session = MagicMock()

    cached_result = JobResult(
        task="tag",
        recipe_hash="recipe_cached",
        output={"tags": ["civic", "budget"]},
        model="gemini/gemini-3-flash-preview",
    )
    from citypods.compute.llm_deferred import write_deferred

    write_deferred(storage, "recipe_cached", cached_result)

    config = LLMBackendConfig(
        model="gemini/gemini-3-flash-preview",
        dispatch_v2_url="https://dispatch-v2.example.com",
    )
    backend = LiteLLMBackend(config, http_session=mock_session, storage=storage)

    job = InferenceJob(
        task="tag",
        inputs={"messages": [{"role": "user", "content": "hello"}]},
        recipe_hash="recipe_cached",
    )

    results = backend.enqueue_batch([job])
    assert len(results) == 1
    assert isinstance(results[0], JobResult)
    assert results[0].output == {"tags": ["civic", "budget"]}
    # No HTTP call made
    mock_session.post.assert_not_called()


def test_enqueue_batch_client_side_throttling():
    storage = MockStorage()
    mock_session = MagicMock()

    def mock_post(url, json=None, headers=None, timeout=None):
        jobs = json.get("jobs", [])
        return _mock_response(
            status_code=200,
            json_data={
                "accepted": [{"id": j["id"]} for j in jobs],
                "rejected": [],
            },
        )

    mock_session.post.side_effect = mock_post

    config = LLMBackendConfig(
        model="gemini/gemini-3-flash-preview",
        dispatch_v2_url="https://dispatch-v2.example.com",
        daily_ingest_cap=1,
    )
    backend = LiteLLMBackend(config, http_session=mock_session, storage=storage)

    job1 = InferenceJob(
        task="tag", inputs={"messages": [{"role": "user", "content": "test1"}]}, recipe_hash="r1"
    )
    job2 = InferenceJob(
        task="tag", inputs={"messages": [{"role": "user", "content": "test2"}]}, recipe_hash="r2"
    )

    results1 = backend.enqueue_batch([job1])
    assert isinstance(results1[0], JobHandle)
    assert results1[0].backend == "llm-dispatch-v2"
    assert backend._daily_ingest_admitted == 1

    # Second batch should be throttled client-side without HTTP request
    mock_session.post.reset_mock()
    results2 = backend.enqueue_batch([job2])
    assert isinstance(results2[0], JobHandle)
    assert results2[0].deferred_request is not None
    mock_session.post.assert_not_called()


def test_enqueue_batch_server_rejections():
    storage = MockStorage()
    mock_session = MagicMock()

    def mock_post(url, json=None, headers=None, timeout=None):
        jobs = json.get("jobs", [])
        return _mock_response(
            status_code=200,
            json_data={
                "accepted": [],
                "rejected": [{"id": j["id"], "reason": "daily_cap_exceeded"} for j in jobs],
            },
        )

    mock_session.post.side_effect = mock_post

    config = LLMBackendConfig(
        model="gemini/gemini-3-flash-preview",
        dispatch_v2_url="https://dispatch-v2.example.com",
    )
    backend = LiteLLMBackend(config, http_session=mock_session, storage=storage)

    job = InferenceJob(
        task="tag", inputs={"messages": [{"role": "user", "content": "test"}]}, recipe_hash="r1"
    )
    results = backend.enqueue_batch([job])
    assert isinstance(results[0], JobHandle)
    assert results[0].deferred_request is not None
    assert backend._daily_ingest_exhausted is True


def test_poll_batch_fetches_completed_results_from_b2():
    storage = MockStorage()
    mock_session = MagicMock()

    # Store completed result in mock storage
    storage.write_bytes(
        "results/j1/lt1.json",
        json.dumps({"choices": [{"message": {"content": "completed result"}}]}).encode("utf-8"),
    )

    mock_session.post.return_value = _mock_response(
        status_code=200,
        json_data={
            "statuses": [
                {
                    "id": "j1",
                    "state": "completed",
                    "result_key": "results/j1/lt1.json",
                    "attempts": 1,
                },
                {"id": "j2", "state": "queued", "result_key": None, "attempts": 0},
            ]
        },
    )

    config = LLMBackendConfig(
        model="gemini/gemini-3-flash-preview",
        dispatch_v2_url="https://dispatch-v2.example.com",
    )
    backend = LiteLLMBackend(config, http_session=mock_session, storage=storage)

    h1 = JobHandle(task="tag", recipe_hash="r1", backend="llm-dispatch-v2", ref="j1")
    h2 = JobHandle(task="tag", recipe_hash="r2", backend="llm-dispatch-v2", ref="j2")

    poll_results = backend.poll_batch([h1, h2])
    assert "j1" in poll_results
    assert isinstance(poll_results["j1"], JobResult)
    assert poll_results["j1"].output == {"choices": [{"message": {"content": "completed result"}}]}
    assert poll_results["j2"] is None


def test_poll_batch_failed_job_raises():
    storage = MockStorage()
    mock_session = MagicMock()

    mock_session.post.return_value = _mock_response(
        status_code=200,
        json_data={
            "statuses": [
                {
                    "id": "j1",
                    "state": "failed",
                    "error": "upstream_timeout",
                    "attempts": 3,
                }
            ]
        },
    )

    config = LLMBackendConfig(
        model="gemini/gemini-3-flash-preview",
        dispatch_v2_url="https://dispatch-v2.example.com",
    )
    backend = LiteLLMBackend(config, http_session=mock_session, storage=storage)

    h1 = JobHandle(task="tag", recipe_hash="r1", backend="llm-dispatch-v2", ref="j1")

    with pytest.raises(LLMBackendError, match="failed permanently"):
        backend.poll_batch([h1])
