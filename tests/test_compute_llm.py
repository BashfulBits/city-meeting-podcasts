from __future__ import annotations

import traceback
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ConfigDict

from citypods.compute.base import InferenceJob, JobHandle, JobResult
from citypods.compute.llm import (
    LiteLLMBackend,
    LLMBackendConfig,
    LLMBackendError,
    LLMStructuredOutputError,
)
from citypods.compute.structured import register_response_model


class ExampleOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str


register_response_model("test-output", ExampleOutput)


def job(task="tag", **inputs):
    return InferenceJob(task=task, inputs=inputs, recipe_hash="recipe-1")


def structured_response(content: str):
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(
        choices=[choice],
        model_dump=lambda: {"choices": [{"message": {"content": content}}]},
    )


def test_direct_litellm_call_is_normalized():
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(model_dump=lambda: {"choices": [{"message": {"content": "ok"}}]})

    backend = LiteLLMBackend(
        LLMBackendConfig(model="gemini/gemini-3-flash-preview"), completion=completion
    )
    result = backend.run_inference(job(content="meeting text"))

    assert isinstance(result, JobResult)
    assert result.output["choices"][0]["message"]["content"] == "ok"
    assert calls[0]["model"] == "gemini/gemini-3-flash-preview"
    assert calls[0]["stream"] is False


def test_structured_job_uses_instructor_pydantic_json_schema_mode():
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        return structured_response('{"value":"ok"}')

    backend = LiteLLMBackend(
        LLMBackendConfig(model="gemini/gemini-3-flash-preview"), completion=completion
    )
    result = backend.run_inference(job(content="meeting text", structured_output="test-output"))

    assert result.output["choices"][0]["message"]["content"] == '{"value":"ok"}'
    assert calls[0]["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "ExampleOutput", "schema": ExampleOutput.model_json_schema()},
    }


def test_deepseek_instructor_json_mode_retries_pydantic_validation_once():
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        content = '{"value":42}' if len(calls) == 1 else '{"value":"ok"}'
        return structured_response(content)

    backend = LiteLLMBackend(
        LLMBackendConfig(model="deepseek/deepseek-v4-flash"), completion=completion
    )
    result = backend.run_inference(job(content="meeting text", structured_output="test-output"))

    assert result.output["choices"][0]["message"]["content"] == '{"value":"ok"}'
    assert len(calls) == 2
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert any("validation" in str(message["content"]).lower() for message in calls[1]["messages"])


def test_deepseek_invalid_reply_fails_after_one_instructor_retry():
    calls = []
    private_marker = "untrusted-output-marker"
    invalid = '{"value":42,"extra":"' + private_marker + '"}'

    def completion(**kwargs):
        calls.append(kwargs)
        return structured_response(invalid)

    backend = LiteLLMBackend(
        LLMBackendConfig(model="deepseek/deepseek-v4-flash"), completion=completion
    )

    with pytest.raises(LLMStructuredOutputError, match="failed Pydantic validation") as raised:
        backend.run_inference(job(content="meeting text", structured_output="test-output"))
    assert len(calls) == 2
    traceback_text = "".join(traceback.format_exception(raised.type, raised.value, raised.tb))
    assert private_marker not in traceback_text


def test_blank_actions_variables_preserve_direct_gemini_defaults(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "")
    monkeypatch.setenv("LLM_MODE", "")

    config = LLMBackendConfig.from_env()

    assert config.model == "gemini/gemini-3-flash-preview"
    assert config.mode == "direct"


def test_dispatch_enqueues_pydantic_schema_and_validates_completed_response():
    requests = []

    class Response:
        def __init__(self, status, body, headers=None):
            self.status_code = status
            self._body = body
            self.headers = headers or {}

        def json(self):
            return self._body

    class Session:
        def post(self, url, **kwargs):
            requests.append(("post", url, kwargs))
            return Response(202, {"id": "chatcmpl-1"}, {"location": "/v1/requests/chatcmpl-1"})

        def get(self, url, **kwargs):
            requests.append(("get", url, kwargs))
            if len(requests) == 2:
                return Response(202, {})
            return Response(200, {"choices": [{"message": {"content": '{"value":"done"}'}}]})

    backend = LiteLLMBackend(
        LLMBackendConfig(
            model="mistral/mistral-large-latest",
            mode="dispatch",
            dispatch_url="https://dispatch.example",
            dispatch_auth_token="secret",
        ),
        http_session=Session(),
    )
    handle = backend.run_inference(
        job(messages=[{"role": "user", "content": "hi"}], structured_output="test-output")
    )
    assert isinstance(handle, JobHandle)
    assert handle.ref == "/v1/requests/chatcmpl-1"
    assert handle.structured_output == "test-output"
    assert (
        requests[0][2]["json"]["response_format"]["json_schema"]["schema"]
        == ExampleOutput.model_json_schema()
    )
    assert backend.poll(handle) is None
    assert backend.poll(handle).output["choices"][0]["message"]["content"] == '{"value":"done"}'
    assert requests[0][2]["headers"]["idempotency-key"] == "recipe-1"


def test_dispatch_consumes_completed_idempotent_resubmit():
    class Response:
        def __init__(self, status, body, headers=None):
            self.status_code = status
            self._body = body
            self.headers = headers or {}

        def json(self):
            return self._body

    class Session:
        def __init__(self):
            self.posts = 0

        def post(self, *_args, **_kwargs):
            self.posts += 1
            if self.posts == 1:
                return Response(202, {"id": "chatcmpl-1"}, {"location": "/v1/requests/chatcmpl-1"})
            return Response(200, {"choices": [{"message": {"content": '{"value":"done"}'}}]})

    backend = LiteLLMBackend(
        LLMBackendConfig(
            model="mistral/mistral-large-latest",
            mode="dispatch",
            dispatch_url="https://dispatch.example",
        ),
        http_session=Session(),
    )
    queued = backend.run_inference(job(content="meeting text", structured_output="test-output"))
    completed = backend.run_inference(job(content="meeting text", structured_output="test-output"))

    assert isinstance(queued, JobHandle)
    assert isinstance(completed, JobResult)
    assert completed.output["choices"][0]["message"]["content"] == '{"value":"done"}'


def test_dispatch_rejects_invalid_structured_result():
    private_marker = "untrusted-dispatch-output-marker"

    class Response:
        status_code = 200
        headers = {}

        def json(self):
            return {
                "choices": [
                    {"message": {"content": '{"value":42,"extra":"' + private_marker + '"}'}}
                ]
            }

    backend = LiteLLMBackend(
        LLMBackendConfig(
            model="mistral/mistral-large-latest",
            mode="dispatch",
            dispatch_url="https://dispatch.example",
        ),
        http_session=SimpleNamespace(get=lambda *_args, **_kwargs: Response()),
    )
    with pytest.raises(LLMStructuredOutputError, match="failed Pydantic validation") as raised:
        backend.reconcile(
            JobHandle(
                task="tag",
                recipe_hash="recipe-1",
                backend="litellm",
                ref="request-1",
                structured_output="test-output",
            )
        )
    traceback_text = "".join(traceback.format_exception(raised.type, raised.value, raised.tb))
    assert private_marker not in traceback_text


def test_dispatch_unknown_response_contract_remains_a_version_skew_error():
    class Response:
        status_code = 200
        headers = {}

        def json(self):
            return {"choices": []}

    backend = LiteLLMBackend(
        LLMBackendConfig(
            model="mistral/mistral-large-latest",
            mode="dispatch",
            dispatch_url="https://dispatch.example",
        ),
        http_session=SimpleNamespace(get=lambda *_args, **_kwargs: Response()),
    )

    with pytest.raises(ValueError, match="unknown structured-output contract"):
        backend.reconcile(
            JobHandle(
                task="tag",
                recipe_hash="recipe-1",
                backend="litellm",
                ref="request-1",
                structured_output="missing-output",
            )
        )


def test_dispatch_rejects_malformed_body_and_cross_host_location():
    class Response:
        status_code = 202
        headers = {"location": "https://evil.example/v1/requests/1"}

        def json(self):
            return None

    class Session:
        def post(self, *_args, **_kwargs):
            return Response()

        def get(self, *_args, **_kwargs):
            return Response()

    backend = LiteLLMBackend(
        LLMBackendConfig(
            model="mistral/mistral-large-latest",
            mode="dispatch",
            dispatch_url="https://dispatch.example",
        ),
        http_session=Session(),
    )
    handle = backend.run_inference(job(content="hello"))
    with pytest.raises(LLMBackendError, match="unexpected host"):
        backend.reconcile(handle)

    class MalformedSession(Session):
        def post(self, *_args, **_kwargs):
            response = Response()
            response.headers = {}
            return response

    backend = LiteLLMBackend(
        LLMBackendConfig(
            model="mistral/mistral-large-latest",
            mode="dispatch",
            dispatch_url="https://dispatch.example",
        ),
        http_session=MalformedSession(),
    )
    with pytest.raises(LLMBackendError, match="omitted a request reference"):
        backend.run_inference(job(content="hello"))


def test_rejects_gpu_and_unknown_routes():
    with pytest.raises(ValueError):
        LiteLLMBackend(LLMBackendConfig(model="openai/gpt-4o"))
    backend = LiteLLMBackend(LLMBackendConfig(), completion=lambda **_: {})
    with pytest.raises(ValueError):
        backend.run_inference(InferenceJob(task="transcribe", inputs={}))
