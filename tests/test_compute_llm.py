from __future__ import annotations

import traceback
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ConfigDict

from citypods.compute.base import InferenceJob, JobHandle, JobResult
from citypods.compute.llm import (
    LiteLLMBackend,
    LLMBackendConfig,
    LLMBackendError,
    LLMStructuredOutputError,
    _priced_actual,
    _usage_tokens,
)
from citypods.compute.llm_budget import daily_reset_key, load_llm_budget_cas, mutate_llm_budget
from citypods.compute.llm_policy import ROUTES, LLMRequestPolicy
from citypods.compute.structured import register_response_model
from tests._cas_fake import MemStorage


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


def test_policy_route_is_resolved_and_settled_in_cas_ledger():
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            model_dump=lambda: {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"total_tokens": 12},
            }
        )

    storage = MemStorage()
    backend = LiteLLMBackend(
        LLMBackendConfig(model="gemini/gemini-3-flash-preview"),
        completion=completion,
        storage=storage,
    )
    result = backend.run_inference(
        job(
            content="meeting text",
            llm_policy=LLMRequestPolicy(
                allowed_models=("gemini/gemini-3-flash-preview",),
                purpose="test",
            ),
        )
    )

    assert isinstance(result, JobResult)
    assert calls[0]["model"] == "gemini/gemini-3-flash-preview"
    budget, _ = load_llm_budget_cas(storage)
    ledger = budget.routes["gemini/gemini-3-flash-preview"]
    assert ledger.inflight == {}
    assert ledger.requests_minute == 1
    assert ledger.tokens_minute == 12


def test_policy_no_eligible_route_returns_a_deferred_handle_without_reservation():
    """Not eligible right now is never an exception for a policy-bearing call -- it's the same
    JobHandle shape a genuine Mistral dispatch returns, uniformly, so the caller never has to know
    which reason (or which transport) produced it."""
    storage = MemStorage()
    now = datetime.now(UTC)

    def exhaust(budget, _now):
        model = "gemini/gemini-3-flash-preview"
        route = ROUTES[model]
        ledger = budget._ledger(model, now, route=route)
        ledger.requests_day = route.quota.rpd
        ledger.requests_day_key = daily_reset_key(now, "America/Los_Angeles")

    mutate_llm_budget(storage, exhaust, now=now)
    backend = LiteLLMBackend(
        LLMBackendConfig(model="gemini/gemini-3-flash-preview"),
        completion=lambda **_: {},
        storage=storage,
    )

    result = backend.run_inference(
        job(
            content="meeting text",
            llm_policy=LLMRequestPolicy(
                allowed_models=("gemini/gemini-3-flash-preview",),
            ),
        )
    )
    assert isinstance(result, JobHandle)
    assert result.deferred_request is not None
    assert len(result.deferred_request.messages) == 2  # the default system + user prompt

    budget, _ = load_llm_budget_cas(storage)
    ledger = budget.routes["gemini/gemini-3-flash-preview"]
    assert ledger.inflight == {}
    assert ledger.requests_day == 1500

    # And it's persisted: a second ask with the same job finds the pending record instead of
    # re-running selection from scratch.
    again = backend.run_inference(
        job(
            content="meeting text",
            llm_policy=LLMRequestPolicy(
                allowed_models=("gemini/gemini-3-flash-preview",),
            ),
        )
    )
    assert isinstance(again, JobHandle)
    assert again.deferred_request is not None


def test_policy_post_network_failure_settles_reservation():
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("provider failure")

    storage = MemStorage()
    backend = LiteLLMBackend(
        LLMBackendConfig(model="gemini/gemini-3-flash-preview"),
        completion=completion,
        storage=storage,
    )
    with pytest.raises(RuntimeError, match="provider failure"):
        backend.run_inference(
            job(
                content="meeting text",
                llm_policy=LLMRequestPolicy(
                    allowed_models=("gemini/gemini-3-flash-preview",),
                ),
            )
        )

    assert calls
    budget, _ = load_llm_budget_cas(storage)
    ledger = budget.routes["gemini/gemini-3-flash-preview"]
    assert ledger.inflight == {}
    assert ledger.requests_minute == 1


def test_policy_requires_cas_storage():
    backend = LiteLLMBackend(LLMBackendConfig(), completion=lambda **_: {})
    with pytest.raises(LLMBackendError, match="CAS-capable storage"):
        backend.run_inference(job(content="meeting text", llm_policy=LLMRequestPolicy()))


def test_direct_mode_429_defers_and_blocks_the_route_reactively():
    """A real rate-limit response overrides our own proactive RPM/RPD/TPM estimate -- it must not
    surface as a raw exception, and it must not let the next attempt immediately retry into the
    same 429 (this codebase's own counters might have said the route was still available)."""

    class RateLimited(Exception):
        status_code = 429
        headers = {"retry-after": "30"}

    def completion(**kwargs):
        raise RateLimited("rate limited")

    storage = MemStorage()
    backend = LiteLLMBackend(
        LLMBackendConfig(model="gemini/gemini-3-flash-preview"),
        completion=completion,
        storage=storage,
    )
    result = backend.run_inference(
        job(
            content="meeting text",
            llm_policy=LLMRequestPolicy(allowed_models=("gemini/gemini-3-flash-preview",)),
        )
    )

    assert isinstance(result, JobHandle)
    assert result.deferred_request is not None
    budget, _ = load_llm_budget_cas(storage)
    ledger = budget.routes["gemini/gemini-3-flash-preview"]
    assert ledger.inflight == {}
    # The attempted request still counts (the provider's own counter already moved) -- only
    # release, never settle, would have undercounted it.
    assert ledger.requests_minute == 1
    assert ledger.blocked_until != ""


def test_dispatch_mode_429_defers_and_blocks_the_route_reactively():
    class Response:
        def __init__(self, status, body, headers=None):
            self.status_code = status
            self._body = body
            self.headers = headers or {}

        def json(self):
            return self._body

    class Session:
        def post(self, url, **kwargs):
            return Response(429, {"error": "rate limited"}, {"retry-after": "45"})

    storage = MemStorage()
    backend = LiteLLMBackend(
        LLMBackendConfig(
            model="mistral/mistral-large-latest",
            mode="dispatch",
            dispatch_url="https://dispatch.example",
        ),
        http_session=Session(),
        storage=storage,
    )
    result = backend.run_inference(
        job(
            content="meeting text",
            llm_policy=LLMRequestPolicy(allowed_models=("mistral/mistral-large-latest",)),
        )
    )

    assert isinstance(result, JobHandle)
    assert result.deferred_request is not None
    budget, _ = load_llm_budget_cas(storage)
    ledger = budget.routes["mistral/mistral-large-latest"]
    assert ledger.inflight == {}
    assert ledger.requests_minute == 1
    assert ledger.blocked_until != ""


def test_usage_tokens_returns_none_not_zero_for_missing_or_invalid_usage():
    assert _usage_tokens({}) is None
    assert _usage_tokens({"usage": {}}) is None
    assert _usage_tokens({"usage": {"total_tokens": -5}}) is None
    assert _usage_tokens({"usage": {"total_tokens": 12}}) == 12
    assert _usage_tokens({"usage": {"prompt_tokens": 8, "completion_tokens": 4}}) == 12


def test_priced_actual_prices_prompt_and_completion_tokens_separately():
    output = {"usage": {"total_tokens": 100, "prompt_tokens": 80, "completion_tokens": 20}}
    tokens, cost = _priced_actual(output, input_per_token=0.14e-6, output_per_token=0.28e-6)
    assert tokens == 100
    assert cost == pytest.approx(80 * 0.14e-6 + 20 * 0.28e-6)
    # A naive combined-rate charge against every token would have given a different (larger,
    # here, since output is pricier) number -- the split must actually change the result.
    assert cost != pytest.approx(100 * (0.14e-6 + 0.28e-6))


def test_priced_actual_falls_back_to_combined_rate_without_a_split():
    output = {"usage": {"total_tokens": 100}}
    tokens, cost = _priced_actual(output, input_per_token=0.14e-6, output_per_token=0.28e-6)
    assert tokens == 100
    assert cost == pytest.approx(100 * (0.14e-6 + 0.28e-6))


def test_structured_policy_call_reserves_worst_case_two_requests():
    """Instructor's `max_retries=1` can send up to two provider requests for one logical
    dispatch; the ledger must reserve that worst case up front even when only one attempt
    actually happens, so RPM/RPD/TPM can never be breached by an unlucky retry."""

    def completion(**kwargs):
        return structured_response('{"value":"ok"}')

    storage = MemStorage()
    backend = LiteLLMBackend(
        LLMBackendConfig(model="gemini/gemini-3-flash-preview"),
        completion=completion,
        storage=storage,
    )
    backend.run_inference(
        job(
            content="meeting text",
            structured_output="test-output",
            llm_policy=LLMRequestPolicy(allowed_models=("gemini/gemini-3-flash-preview",)),
        )
    )

    budget, _ = load_llm_budget_cas(storage)
    ledger = budget.routes["gemini/gemini-3-flash-preview"]
    assert ledger.inflight == {}
    # The reservation's `requests=2` sticks even after settlement -- only tokens/cost are
    # corrected to actual, request counts are never walked back down (review/33 §10.2).
    assert ledger.requests_minute == 2


def test_second_call_with_the_same_recipe_hash_returns_the_cached_result():
    """A completed result is cached in the registry the same way a deferred handle is -- a second
    ask with the same job (the "just call run_inference again" pattern) must never pay for a
    second real provider call."""
    storage = MemStorage()
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(model_dump=lambda: {"choices": [{"message": {"content": "ok"}}]})

    backend = LiteLLMBackend(
        LLMBackendConfig(model="gemini/gemini-3-flash-preview"),
        completion=completion,
        storage=storage,
    )
    policy = LLMRequestPolicy(allowed_models=("gemini/gemini-3-flash-preview",))
    first = backend.run_inference(job(content="meeting text", llm_policy=policy))
    second = backend.run_inference(job(content="meeting text", llm_policy=policy))

    assert len(calls) == 1
    assert isinstance(first, JobResult) and isinstance(second, JobResult)
    assert second.output == first.output
    budget, _ = load_llm_budget_cas(storage)
    ledger = budget.routes["gemini/gemini-3-flash-preview"]
    assert ledger.inflight == {}
    assert ledger.requests_minute == 1


def test_policy_bearing_call_requires_non_empty_recipe_hash():
    """Unconditional now, not just for dispatch mode: the deferred-request registry is keyed by
    recipe_hash too, and an empty one would let unrelated jobs collide in it."""
    storage = MemStorage()
    backend = LiteLLMBackend(
        LLMBackendConfig(
            model="mistral/mistral-large-latest",
            mode="dispatch",
            dispatch_url="https://dispatch.example",
        ),
        storage=storage,
    )
    empty_hash_job = InferenceJob(
        task="tag",
        inputs={"content": "meeting text", "llm_policy": LLMRequestPolicy()},
        recipe_hash="",
    )
    with pytest.raises(LLMBackendError, match="non-empty recipe_hash"):
        backend.run_inference(empty_hash_job)
    budget, _ = load_llm_budget_cas(storage)
    assert budget.routes == {}


def test_reconcile_prices_actual_usage_from_the_handle_not_live_route_config():
    """A JobHandle captures the route's pricing at reservation time; a later reconcile() must use
    those captured rates, not whatever ROUTES says at poll time (Mistral is $0 in ROUTES today,
    so if reconcile() used live config instead of the handle, cost_used would stay zero here)."""
    storage = MemStorage()
    route = ROUTES["mistral/mistral-large-latest"]
    now = datetime.now(UTC)
    mutate_llm_budget(
        storage,
        lambda budget, attempt_now: budget.reserve(
            "owner-1", route.model, route=route, requests=1, tokens=100, cost=0.0, now=attempt_now
        ),
        now=now,
    )

    class Response:
        def __init__(self, status, body):
            self.status_code = status
            self._body = body

        def json(self):
            return self._body

    class Session:
        def get(self, url, **kwargs):
            return Response(
                200,
                {
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {"total_tokens": 100, "prompt_tokens": 80, "completion_tokens": 20},
                },
            )

    backend = LiteLLMBackend(
        LLMBackendConfig(
            model="mistral/mistral-large-latest",
            mode="dispatch",
            dispatch_url="https://dispatch.example",
        ),
        http_session=Session(),
        storage=storage,
    )
    handle = JobHandle(
        task="tag",
        recipe_hash="recipe-1",
        backend=backend.name,
        ref="/v1/requests/chatcmpl-1",
        model=route.model,
        owner="owner-1",
        input_per_token=0.14e-6,
        output_per_token=0.28e-6,
    )
    result = backend.reconcile(handle)

    assert result.output["choices"][0]["message"]["content"] == "ok"
    budget, _ = load_llm_budget_cas(storage)
    ledger = budget.routes[route.model]
    assert ledger.inflight == {}
    assert ledger.cost_used == pytest.approx(80 * 0.14e-6 + 20 * 0.28e-6)


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
