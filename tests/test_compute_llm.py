from __future__ import annotations

from types import SimpleNamespace

import pytest

from citypods.compute.base import InferenceJob, JobHandle, JobResult
from citypods.compute.llm import LiteLLMBackend, LLMBackendConfig, LLMBackendError


def job(task="tag", **inputs):
    return InferenceJob(task=task, inputs=inputs, recipe_hash="recipe-1")


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


def test_structured_job_uses_provider_native_strict_json_schema():
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        return {"choices": [{"message": {"content": '{"value":"ok"}'}}]}

    backend = LiteLLMBackend(
        LLMBackendConfig(model="gemini/gemini-3-flash-preview"),
        completion=completion,
        supports_response_schema=lambda _model: True,
    )
    backend.run_inference(
        job(
            content="meeting text",
            response_schema={
                "name": "test_output",
                "schema": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            },
        )
    )

    assert calls[0]["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "test_output",
            "schema": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            "strict": True,
        },
    }


def test_structured_job_rejects_route_without_json_schema_support():
    backend = LiteLLMBackend(
        LLMBackendConfig(model="gemini/gemini-3-flash-preview"),
        completion=lambda **_: {},
        supports_response_schema=lambda _model: False,
    )

    with pytest.raises(LLMBackendError, match="does not support strict JSON Schema"):
        backend.run_inference(
            job(
                content="meeting text",
                response_schema={"name": "test_output", "schema": {"type": "object"}},
            )
        )


def test_deepseek_json_mode_is_validated_then_retried_against_task_schema():
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        content = '{"value":42}' if len(calls) == 1 else '{"value":"ok"}'
        return {"choices": [{"message": {"content": content}}]}

    backend = LiteLLMBackend(
        LLMBackendConfig(model="deepseek/deepseek-v4-flash"), completion=completion
    )
    result = backend.run_inference(
        job(
            content="meeting text",
            response_schema={
                "name": "test_output",
                "schema": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            },
        )
    )

    assert result.output["choices"][0]["message"]["content"] == '{"value":"ok"}'
    assert len(calls) == 2
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert "did not match the required JSON Schema" in calls[1]["messages"][-1]["content"]


def test_deepseek_invalid_schema_reply_fails_after_one_retry():
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        return {"choices": [{"message": {"content": '{"value":42}'}}]}

    backend = LiteLLMBackend(
        LLMBackendConfig(model="deepseek/deepseek-v4-flash"), completion=completion
    )

    with pytest.raises(LLMBackendError, match="did not match its JSON Schema"):
        backend.run_inference(
            job(
                content="meeting text",
                response_schema={
                    "name": "test_output",
                    "schema": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                },
            )
        )
    assert len(calls) == 2


def test_blank_actions_variables_preserve_direct_gemini_defaults(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "")
    monkeypatch.setenv("LLM_MODE", "")

    config = LLMBackendConfig.from_env()

    assert config.model == "gemini/gemini-3-flash-preview"
    assert config.mode == "direct"


def test_dispatch_enqueue_and_poll():
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
            return Response(200, {"choices": [{"message": {"content": "done"}}]})

    backend = LiteLLMBackend(
        LLMBackendConfig(
            model="mistral/mistral-large-latest",
            mode="dispatch",
            dispatch_url="https://dispatch.example",
            dispatch_auth_token="secret",
        ),
        http_session=Session(),
    )
    handle = backend.run_inference(job(messages=[{"role": "user", "content": "hi"}]))
    assert isinstance(handle, JobHandle)
    assert handle.ref == "/v1/requests/chatcmpl-1"
    assert backend.poll(handle) is None
    assert backend.poll(handle).output["choices"][0]["message"]["content"] == "done"
    assert requests[0][2]["headers"]["idempotency-key"] == "recipe-1"


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
