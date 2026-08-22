"""Tests for bounded bundled LLM dispatch v2 Python client."""

import json
import json as _json_module  # alias for use inside mocks whose own `json=` kwarg shadows the name
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests
from pydantic import BaseModel, ConfigDict

from citypods.compute.base import InferenceJob, JobHandle, JobResult
from citypods.compute.llm import (
    BatchingDispatchBackend,
    LiteLLMBackend,
    LLMBackendConfig,
    LLMBackendError,
    LLMDispatchTerminalError,
    dispatch_job_batch,
)
from citypods.compute.llm_policy import LLMRequestPolicy
from citypods.compute.structured import register_response_model


class _PongOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pong: bool


register_response_model("dispatch-v2-test-pong", _PongOutput)


class MockStorage:
    """Mirrors the real StorageBackend/CAS surface (citypods/storage/s3.py), not an invented
    shape -- enqueue_batch/poll_batch call put_cas/get_bytes on real storage, and a mock that
    doesn't match would let a AttributeError-on-real-storage regression pass silently here."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.cas_capable = True
        self._etag_counter = 0

    def _next_etag(self) -> str:
        self._etag_counter += 1
        return f'"etag-{self._etag_counter}"'

    def put_cas(self, key, data, content_type, *, if_none_match=None, if_match=None):
        self.files[key] = data if isinstance(data, bytes) else str(data).encode("utf-8")
        etag = self._next_etag()
        return f"mem://{key}", etag

    def get_bytes(self, key):
        if key not in self.files:
            return None
        return self.files[key], self._next_etag()

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


def _accept_all(url, json=None, headers=None, timeout=None):
    """A mock coordinator that accepts every job, echoing submitted_id == id for a fresh
    submission -- matching workers/llm-dispatch-v2/src/coordinator.js's real response shape."""
    jobs = json.get("jobs", [])
    return _mock_response(
        status_code=200,
        json_data={
            "accepted": [{"id": j["id"], "submitted_id": j["id"]} for j in jobs],
            "rejected": [],
        },
    )


def test_enqueue_batch_submits_jobs_and_persists_payloads_to_b2():
    storage = MockStorage()
    mock_session = MagicMock()
    mock_session.post.side_effect = _accept_all

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

    # Check that payloads were written to storage via put_cas (the real storage API)
    stored_payload_keys = [k for k in storage.files if k.startswith("payloads/")]
    assert len(stored_payload_keys) == 2


def test_enqueue_batch_and_poll_batch_work_through_production_routing_storage():
    """Regression test for the 2026-08-18 incident: production wires LiteLLMBackend's storage=
    to citypods.storage.routing.RoutingStorage (B2 primary + R2 coordination), whose
    COORDINATION_PREFIXES deliberately excludes "payloads/"/"results/" (v2 payloads are
    B2-resident by design, not R2 coordination state). Before the fix, _storage_client()
    returned the router itself, so put_cas/get_bytes on a "payloads/" key routed to the B2
    primary and then raised NotImplementedError, because B2 is deliberately marked
    non-cas_capable at the router (real CAS semantics aren't guaranteed there) -- every v2
    enqueue_batch call failed before ever reaching the dispatch Worker, explaining zero
    Cloudflare-side v2 activity despite dispatch_v2_url being correctly configured. This test
    uses the real RoutingStorage (not the flat MockStorage above, which doesn't reproduce the
    prefix-routing gate) to prove enqueue_batch/poll_batch reach the B2 primary directly."""
    from citypods.storage.routing import RoutingStorage

    class FakeB2Primary:
        """Mirrors S3CompatibleStorage's real shape: cas_capable=False is just an advisory
        flag stored on the instance -- put_cas/get_bytes themselves never check it, matching
        citypods/storage/s3.py's S3CompatibleStorage (B2 silently accepts an unconditional
        put_object; it just isn't a real compare-and-swap)."""

        def __init__(self):
            self.name = "b2"
            self.cas_capable = False
            self.files: dict[str, bytes] = {}
            self._etag = 0

        def put_cas(self, key, data, content_type, *, if_none_match=None, if_match=None):
            self.files[key] = data if isinstance(data, bytes) else str(data).encode("utf-8")
            self._etag += 1
            return f"mem://{key}", f'"etag-{self._etag}"'

        def get_bytes(self, key):
            if key not in self.files:
                return None
            self._etag += 1
            return self.files[key], f'"etag-{self._etag}"'

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

    class FakeR2Coordination(FakeB2Primary):
        def __init__(self):
            super().__init__()
            self.name = "r2"
            self.cas_capable = True

    primary = FakeB2Primary()
    coordination = FakeR2Coordination()
    routing_storage = RoutingStorage(primary=primary, coordination=coordination)
    assert routing_storage.cas_capable is True  # router-level flag: R2 coordination present

    mock_session = MagicMock()
    mock_session.post.side_effect = _accept_all
    config = LLMBackendConfig(
        model="gemini/gemini-3-flash-preview",
        dispatch_v2_url="https://dispatch-v2.example.com",
    )
    backend = LiteLLMBackend(config, http_session=mock_session, storage=routing_storage)

    job = InferenceJob(
        task="tag",
        inputs={"messages": [{"role": "user", "content": "hello"}]},
        recipe_hash="recipe-routing-1",
    )
    results = backend.enqueue_batch([job])
    assert len(results) == 1
    assert isinstance(results[0], JobHandle)

    # The payload landed on the B2 primary directly, not the R2 coordination backend -- and not
    # nowhere (silently swallowed).
    stored_keys = [k for k in primary.files if k.startswith("payloads/")]
    assert len(stored_keys) == 1
    assert not any(k.startswith("payloads/") for k in coordination.files)

    # poll_batch's result read must reach the same B2 primary the Worker itself would use.
    handle = results[0]
    result_key = f"results/{handle.ref}/response.json"
    primary.files[result_key] = json.dumps({"choices": [{"message": {"content": "hi"}}]}).encode(
        "utf-8"
    )

    def mock_poll(url, json=None, headers=None, timeout=None):
        return _mock_response(
            status_code=200,
            json_data={
                "statuses": [{"id": handle.ref, "state": "completed", "result_key": result_key}]
            },
        )

    mock_session.post.side_effect = mock_poll
    polled = backend.poll_batch([handle])
    assert len(polled) == 1
    result = polled[handle.ref]
    assert isinstance(result, JobResult)
    assert result.output == {"choices": [{"message": {"content": "hi"}}]}


def test_enqueue_batch_requires_cas_capable_storage():
    mock_session = MagicMock()
    config = LLMBackendConfig(
        model="gemini/gemini-3-flash-preview",
        dispatch_v2_url="https://dispatch-v2.example.com",
    )
    backend = LiteLLMBackend(config, http_session=mock_session, storage=None)

    job = InferenceJob(
        task="tag", inputs={"messages": [{"role": "user", "content": "hello"}]}, recipe_hash="r1"
    )
    with pytest.raises(LLMBackendError, match="CAS-capable"):
        backend.enqueue_batch([job])
    mock_session.post.assert_not_called()


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
    mock_session.post.side_effect = _accept_all

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


def test_enqueue_batch_mixed_accepted_and_rejected():
    storage = MockStorage()
    mock_session = MagicMock()

    def mock_post(url, json=None, headers=None, timeout=None):
        jobs = json.get("jobs", [])
        accepted = [{"id": jobs[0]["id"], "submitted_id": jobs[0]["id"]}]
        rejected = [{"id": jobs[1]["id"], "reason": "idempotency_conflict"}]
        return _mock_response(
            status_code=200, json_data={"accepted": accepted, "rejected": rejected}
        )

    mock_session.post.side_effect = mock_post

    config = LLMBackendConfig(
        model="gemini/gemini-3-flash-preview",
        dispatch_v2_url="https://dispatch-v2.example.com",
    )
    backend = LiteLLMBackend(config, http_session=mock_session, storage=storage)

    job1 = InferenceJob(
        task="tag", inputs={"messages": [{"role": "user", "content": "ok"}]}, recipe_hash="r1"
    )
    job2 = InferenceJob(
        task="tag", inputs={"messages": [{"role": "user", "content": "conflict"}]}, recipe_hash="r2"
    )

    # The accepted job must still resolve even though a sibling in the same batch is rejected
    # with idempotency_conflict, which fails loudly per review/44's stated contract.
    with pytest.raises(LLMBackendError, match="idempotency conflict"):
        backend.enqueue_batch([job1, job2])


def test_enqueue_batch_idempotent_replay_uses_canonical_id_as_ref():
    # The coordinator returns the ORIGINAL row's id on a replay, distinct from the fresh id this
    # client always generates -- enqueue_batch must match on submitted_id and use the returned
    # canonical id as the JobHandle ref, not its own freshly-generated job_id.
    storage = MockStorage()
    mock_session = MagicMock()

    def mock_post(url, json=None, headers=None, timeout=None):
        jobs = json.get("jobs", [])
        submitted_id = jobs[0]["id"]
        return _mock_response(
            status_code=200,
            json_data={
                "accepted": [{"id": "original-canonical-id", "submitted_id": submitted_id}],
                "rejected": [],
            },
        )

    mock_session.post.side_effect = mock_post

    config = LLMBackendConfig(
        model="gemini/gemini-3-flash-preview",
        dispatch_v2_url="https://dispatch-v2.example.com",
    )
    backend = LiteLLMBackend(config, http_session=mock_session, storage=storage)

    job = InferenceJob(
        task="tag", inputs={"messages": [{"role": "user", "content": "retry"}]}, recipe_hash="r1"
    )
    results = backend.enqueue_batch([job])
    assert isinstance(results[0], JobHandle)
    assert results[0].ref == "original-canonical-id"


def test_poll_batch_fetches_completed_results_from_b2():
    storage = MockStorage()
    mock_session = MagicMock()

    # Store completed result in mock storage via the real put_cas API
    storage.put_cas(
        "results/j1/lt1.json",
        json.dumps({"choices": [{"message": {"content": "completed result"}}]}).encode("utf-8"),
        "application/json",
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

    with pytest.raises(LLMDispatchTerminalError, match="failed permanently"):
        backend.poll_batch([h1])


def test_poll_batch_missing_result_bytes_stays_pending_not_cached_empty():
    # A completed state whose result_key isn't (yet) readable from storage must NOT be cached as
    # an empty JobResult -- write_deferred never downgrades a completed record, so an empty
    # result would be permanent. It must be reported as still-pending (None) instead.
    storage = MockStorage()
    mock_session = MagicMock()

    mock_session.post.return_value = _mock_response(
        status_code=200,
        json_data={
            "statuses": [
                {
                    "id": "j1",
                    "state": "completed",
                    "result_key": "results/j1/missing.json",
                    "attempts": 1,
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
    poll_results = backend.poll_batch([h1])
    assert poll_results["j1"] is None

    from citypods.compute.llm_deferred import look_up_deferred

    # Nothing was ever persisted for this recipe_hash -- it must remain resolvable later, not
    # permanently stuck as an empty completed record.
    assert look_up_deferred(storage, "r1") is None


def test_poll_batch_validates_structured_output_and_preserves_sibling_results():
    storage = MockStorage()
    mock_session = MagicMock()

    storage.put_cas(
        "results/j-good/lt1.json",
        json.dumps({"choices": [{"message": {"content": '{"pong": true}'}}]}).encode("utf-8"),
        "application/json",
    )
    storage.put_cas(
        "results/j-bad/lt1.json",
        json.dumps({"choices": [{"message": {"content": "not json"}}]}).encode("utf-8"),
        "application/json",
    )

    mock_session.post.return_value = _mock_response(
        status_code=200,
        json_data={
            "statuses": [
                {
                    "id": "j-good",
                    "state": "completed",
                    "result_key": "results/j-good/lt1.json",
                    "attempts": 1,
                },
                {
                    "id": "j-bad",
                    "state": "completed",
                    "result_key": "results/j-bad/lt1.json",
                    "attempts": 1,
                },
            ]
        },
    )

    config = LLMBackendConfig(
        model="gemini/gemini-3-flash-preview",
        dispatch_v2_url="https://dispatch-v2.example.com",
    )
    backend = LiteLLMBackend(config, http_session=mock_session, storage=storage)

    h_good = JobHandle(
        task="tag",
        recipe_hash="r-good",
        backend="llm-dispatch-v2",
        ref="j-good",
        structured_output="dispatch-v2-test-pong",
    )
    h_bad = JobHandle(
        task="tag",
        recipe_hash="r-bad",
        backend="llm-dispatch-v2",
        ref="j-bad",
        structured_output="dispatch-v2-test-pong",
    )

    # A malformed structured reply for one handle in the batch must not discard the sibling's
    # already-resolved, valid outcome -- the exception is raised only after both are processed,
    # and the caller (scripts/llm_deferred_sweep.py) is expected to fall back to polling each
    # handle individually, at which point j-good is skipped (already resolved) and j-bad's
    # precise LLMStructuredOutputError surfaces on its own.
    with pytest.raises(LLMDispatchTerminalError):
        backend.poll_batch([h_good, h_bad])


def _router(routes: dict):
    """Route a mocked session's POST calls by URL suffix -- lets a single mock_session simulate
    both the enqueue-batch and poll-batch endpoints dispatch_job_batch calls in sequence."""

    def _post(url, json=None, headers=None, timeout=None):
        for suffix, handler in routes.items():
            if url.endswith(suffix):
                return handler(url, json=json, headers=headers, timeout=timeout)
        raise AssertionError(f"unexpected POST to {url}")

    return _post


def test_dispatch_job_batch_submits_all_jobs_in_one_call_and_reconciles_pending():
    """The actual point of this function (see review/44's 2026-08-18 incident retrospective): N
    jobs must produce exactly one enqueue-batch call and, for any still-pending v2 handle, exactly
    one poll-batch call -- not N of each."""
    storage = MockStorage()
    enqueue_calls = []
    poll_calls = []

    def _enqueue(url, json=None, **_kw):
        enqueue_calls.append(json)
        return _accept_all(url, json=json)

    def _poll(url, json=None, **_kw):
        poll_calls.append(json)
        ids = json.get("ids", [])
        # Resolve only the first id; the second stays pending, matching a real still-in-flight
        # handle -- proves per-handle results are mapped back correctly, not just "all resolved".
        statuses = [{"id": ids[0], "state": "completed", "result_key": f"results/{ids[0]}.json"}]
        storage.put_cas(
            f"results/{ids[0]}.json",
            _json_module.dumps({"choices": [{"message": {"content": "resolved"}}]}).encode("utf-8"),
            "application/json",
        )
        return _mock_response(status_code=200, json_data={"statuses": statuses})

    mock_session = MagicMock()
    mock_session.post.side_effect = _router({":enqueue-batch": _enqueue, ":poll-batch": _poll})

    config = LLMBackendConfig(
        model="gemini/gemini-3-flash-preview",
        dispatch_v2_url="https://dispatch-v2.example.com",
    )
    backend = LiteLLMBackend(config, http_session=mock_session, storage=storage)

    jobs = [
        InferenceJob(
            task="tag",
            inputs={"messages": [{"role": "user", "content": f"job {i}"}]},
            recipe_hash=f"recipe-batch-{i}",
        )
        for i in range(2)
    ]

    results = dispatch_job_batch(backend, jobs)

    assert len(enqueue_calls) == 1
    assert len(enqueue_calls[0]["jobs"]) == 2
    assert len(poll_calls) == 1
    assert len(poll_calls[0]["ids"]) == 2

    assert len(results) == 2
    assert isinstance(results[0], JobResult)
    assert isinstance(results[1], JobHandle)


def test_batching_dispatch_backend_collects_run_scoped_queue_only_jobs():
    """R6 can defer per-episode finalization while issuing one v2 ingress request per run."""
    storage = MockStorage()
    enqueue_calls = []

    def _enqueue(url, json=None, **_kw):
        enqueue_calls.append(json)
        return _accept_all(url, json=json)

    def _poll(*_args, **_kwargs):
        return _mock_response(status_code=200, json_data={})

    session = MagicMock()
    session.post.side_effect = _router({":enqueue-batch": _enqueue, ":poll-batch": _poll})
    backend = LiteLLMBackend(
        LLMBackendConfig(
            model="gemini/gemini-3.6-flash",
            dispatch_v2_url="https://dispatch-v2.example.com",
        ),
        http_session=session,
        storage=storage,
    )
    batching = BatchingDispatchBackend(backend)
    jobs = [
        InferenceJob(
            task="moment-extraction",
            inputs={
                "messages": [{"role": "user", "content": f"moment {index}"}],
                "llm_policy": LLMRequestPolicy(queue_only=True),
            },
            recipe_hash=f"moment-recipe-{index}",
        )
        for index in range(3)
    ]

    provisional = [batching.run_inference(job) for job in jobs]

    assert all(isinstance(result, JobHandle) for result in provisional)
    assert all(result.ref.startswith("batch-pending:") for result in provisional)
    session.post.assert_not_called()

    outcomes = batching.flush()

    assert len(outcomes) == 3
    assert len(enqueue_calls) == 1
    assert len(enqueue_calls[0]["jobs"]) == 3


def test_dispatch_job_batch_returns_empty_list_for_no_jobs():
    storage = MockStorage()
    mock_session = MagicMock()
    config = LLMBackendConfig(
        model="gemini/gemini-3-flash-preview", dispatch_v2_url="https://dispatch-v2.example.com"
    )
    backend = LiteLLMBackend(config, http_session=mock_session, storage=storage)

    assert dispatch_job_batch(backend, []) == []
    mock_session.post.assert_not_called()


def test_dispatch_job_batch_isolates_a_single_rejected_job():
    """enqueue_batch raises for the WHOLE call when one job in the batch is rejected (e.g.
    idempotency_conflict) -- dispatch_job_batch must retry one job at a time in that case so the
    other, otherwise-valid jobs in the batch still succeed instead of all being lost."""
    storage = MockStorage()
    call_count = {"enqueue": 0}

    def _enqueue(url, json=None, **_kw):
        jobs = json.get("jobs", [])
        call_count["enqueue"] += 1
        if len(jobs) > 1:
            # Simulate the whole-batch rejection enqueue_batch raises on for a single bad job.
            return _mock_response(
                status_code=200,
                json_data={
                    "accepted": [],
                    "rejected": [{"id": j["id"], "reason": "idempotency_conflict"} for j in jobs],
                },
            )
        return _accept_all(url, json=json)

    mock_session = MagicMock()
    mock_session.post.side_effect = _router({":enqueue-batch": _enqueue})

    config = LLMBackendConfig(
        model="gemini/gemini-3-flash-preview", dispatch_v2_url="https://dispatch-v2.example.com"
    )
    backend = LiteLLMBackend(config, http_session=mock_session, storage=storage)

    jobs = [
        InferenceJob(
            task="tag",
            inputs={"messages": [{"role": "user", "content": f"job {i}"}]},
            recipe_hash=f"recipe-isolate-{i}",
        )
        for i in range(2)
    ]

    results = dispatch_job_batch(backend, jobs)

    # The whole-batch call was attempted once (and rejected), then each job was retried alone.
    assert call_count["enqueue"] == 1 + len(jobs)
    assert len(results) == 2
    assert all(isinstance(r, JobHandle) for r in results)


def test_dispatch_job_batch_chunks_large_batch_over_one_thousand():
    """Batches exceeding ENQUEUE_BATCH_MAX (1000) are partitioned across multiple calls."""
    storage = MockStorage()
    enqueue_batch_sizes = []

    def _enqueue(url, json=None, **_kw):
        jobs = json.get("jobs", [])
        enqueue_batch_sizes.append(len(jobs))
        return _accept_all(url, json=json)

    mock_session = MagicMock()
    mock_session.post.side_effect = _router({":enqueue-batch": _enqueue})

    config = LLMBackendConfig(
        model="gemini/gemini-3-flash-preview", dispatch_v2_url="https://dispatch-v2.example.com"
    )
    backend = LiteLLMBackend(config, http_session=mock_session, storage=storage)

    jobs = [
        InferenceJob(
            task="tag",
            inputs={"messages": [{"role": "user", "content": f"job {i}"}]},
            recipe_hash=f"recipe-chunk-{i}",
        )
        for i in range(1050)
    ]

    results = dispatch_job_batch(backend, jobs)

    assert len(results) == 1050
    assert enqueue_batch_sizes == [1000, 50]
    assert all(isinstance(r, JobHandle) for r in results)
