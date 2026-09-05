from __future__ import annotations

import json
import traceback
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import requests
from pydantic import BaseModel, ConfigDict, Field

from citypods.compute.base import InferenceJob, JobHandle, JobResult
from citypods.compute.llm import (
    LiteLLMBackend,
    LLMBackendConfig,
    LLMBackendError,
    LLMDispatchTerminalError,
    LLMStructuredOutputError,
    _messages,
    _pacing_wait_seconds,
    _priced_actual,
    _retry_after_seconds,
    _safe_structured_failure_diagnostic,
    _schema_variant_model,
    _strip_schema_keys,
    _usage_tokens,
)
from citypods.compute.llm_budget import daily_reset_key, load_llm_budget_cas, mutate_llm_budget
from citypods.compute.llm_policy import (
    ROUTE_CANDIDATES,
    ROUTE_REGISTRY,
    ROUTES,
    LLMRequestPolicy,
    estimate_tokens,
)
from citypods.compute.structured import register_response_model
from tests._cas_fake import MemStorage


class ExampleOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str


register_response_model("test-output", ExampleOutput)


class ConstrainedOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=1, max_length=10)
    count: int = Field(ge=0, le=100)
    tags: list[str] = Field(default_factory=list, max_length=5)


register_response_model("constrained-output", ConstrainedOutput)


def job(task="tag", **inputs):
    return InferenceJob(task=task, inputs=inputs, recipe_hash="recipe-1")


def _ledger_for(budget, model):
    route = ROUTES[model]
    return budget.routes[route.route_id or model]


_DISPATCH_ONLY_KEYS = {
    "allow_paid",
    "allow_batch",
    "submit_next",
    "deadline_at",
    "estimated_tokens",
}


def _strict_direct_completion(**kwargs):
    """A direct-path completion double that fails loudly if a dispatch-only payload key leaks
    into the direct LiteLLM call (`_payload()` should never attach these outside the dispatch
    branch -- see `citypods/compute/llm.py` line ~937)."""
    leaked = _DISPATCH_ONLY_KEYS & set(kwargs)
    assert not leaked, f"dispatch-only keys reached the direct LiteLLM call: {sorted(leaked)}"
    return {"choices": [{"message": {"content": "direct response"}}]}


def structured_response(content: str, *, usage: dict | None = None):
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    dumped: dict = {"choices": [{"message": {"content": content}}]}
    if usage is not None:
        dumped["usage"] = usage
    return SimpleNamespace(choices=[choice], model_dump=lambda: dumped)


def test_safe_structured_failure_diagnostic_has_no_prompt_or_provider_text():
    secret_prompt = "meeting material that must never reach diagnostics"
    provider_error = SimpleNamespace(status_code=400)
    failure = SimpleNamespace(
        n_attempts=1, failed_attempts=[SimpleNamespace(exception=provider_error)]
    )
    result = _safe_structured_failure_diagnostic(
        failure,
        job(content=secret_prompt),
        ExampleOutput,
        "gemini/gemini-3.1-flash-lite",
    )
    rendered = str(result)
    assert result["provider_status"] == 400
    assert result["input_characters"] >= len(secret_prompt)
    assert secret_prompt not in rendered
    assert "BadRequest" not in rendered


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
    ledger = _ledger_for(budget, "gemini/gemini-3-flash-preview")
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
        for route in ROUTE_CANDIDATES[model]:
            ledger = budget._ledger(model, now, route=route)
            if route.quota.rpm is not None:
                ledger.requests_minute = route.quota.rpm
            if route.quota.tpm is not None:
                ledger.tokens_minute = route.quota.tpm
            if route.quota.rpd is not None:
                ledger.requests_day = route.quota.rpd
                ledger.requests_day_key = daily_reset_key(now, route.quota.reset_timezone)
            if route.quota.concurrency is not None:
                ledger.inflight = {
                    f"owner-{index}": None for index in range(route.quota.concurrency)
                }

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
    ledger = _ledger_for(budget, "gemini/gemini-3-flash-preview")
    assert ledger.inflight == {}
    assert ledger.requests_day == ROUTES["gemini/gemini-3-flash-preview"].quota.rpd

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
    ledger = _ledger_for(budget, "gemini/gemini-3-flash-preview")
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
    ledger = _ledger_for(budget, "gemini/gemini-3-flash-preview")
    assert ledger.inflight == {}
    # The rejected attempt never reached the model -- it doesn't count against our own
    # proactive ledger. `blocked_until` (not the request counters) is what stops an immediate
    # retry from hammering straight back into the same 429.
    assert ledger.requests_minute == 0
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
            model="mistral/mistral-large-2512",
            mode="dispatch",
            dispatch_url="https://dispatch.example",
        ),
        http_session=Session(),
        storage=storage,
    )
    result = backend.run_inference(
        job(
            content="meeting text",
            llm_policy=LLMRequestPolicy(allowed_models=("mistral/mistral-large-2512",)),
        )
    )

    assert isinstance(result, JobHandle)
    assert result.deferred_request is not None
    budget, _ = load_llm_budget_cas(storage)
    ledger = _ledger_for(budget, "mistral/mistral-large-2512")
    assert ledger.inflight == {}
    assert ledger.requests_minute == 0
    assert ledger.blocked_until != ""


def test_pacing_wait_seconds_gives_up_when_nothing_will_ever_free_up():
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    assert _pacing_wait_seconds(None, now + timedelta(hours=1), now) is None


def test_pacing_wait_seconds_gives_up_when_the_soonest_reset_is_at_or_past_the_deadline():
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    deadline = now + timedelta(minutes=5)
    assert _pacing_wait_seconds(deadline, deadline, now) is None
    assert _pacing_wait_seconds(deadline + timedelta(seconds=1), deadline, now) is None


def test_pacing_wait_seconds_waits_out_a_retry_at_within_the_deadline():
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    deadline = now + timedelta(hours=1)
    assert _pacing_wait_seconds(now + timedelta(seconds=5), deadline, now) == 5.0
    # Capped so the caller re-reads the freshest ledger regularly rather than sleeping the whole
    # remaining wait (still well within the deadline) in one call.
    assert _pacing_wait_seconds(now + timedelta(minutes=30), deadline, now) == 10.0


def test_pacing_wait_seconds_has_no_independent_defense_against_a_past_retry_at():
    """`_pacing_wait_seconds` only gives up via `retry_at is None` or `retry_at >= deadline_at` --
    it has no separate rule for "retry_at is in the past". This is the exact shape of the bug
    fixed in `_next_quota_reset` (a stale `blocked_until`, or the earlier unconditional
    "next minute" candidate): if the route-selection layer ever again hands this function a
    `retry_at` that's before `now` while `deadline_at` is still ahead, this function will not
    catch it -- it returns `0.0` (busy-retry, not give-up) exactly like a legitimately-imminent
    reset would. Correctness here rests entirely on `select_route`/`_next_quota_reset` upstream
    only ever producing a `retry_at` that is genuinely in the future or at/after the deadline --
    this test pins that as documented, load-bearing behavior rather than something a future
    change to this function could quietly assume away."""
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    stale_past = now - timedelta(hours=2)
    deadline = now + timedelta(hours=1)
    assert _pacing_wait_seconds(stale_past, deadline, now) == 0.0


def test_reconcile_settles_actual_requests_after_a_202_dispatch():
    """A structured dispatch call reserves the worst case (2, matching a direct route's possible
    corrective retry) even though the dispatch transport is always exactly one real POST -- the
    202 branch returns a ``JobHandle`` before anything is settled, deliberately leaving the
    reservation inflight until ``reconcile()`` observes the Worker's terminal response. That later
    settle must still correct the reservation down to the one real attempt, not leave it frozen at
    2 forever because the attempt count never reached the handle (CodeRabbit, PR #1007)."""

    class Response:
        def __init__(self, status, body, headers=None):
            self.status_code = status
            self._body = body
            self.headers = headers or {}

        def json(self):
            return self._body

    class Session:
        def post(self, url, **kwargs):
            return Response(202, {"id": "chatcmpl-1"}, {"location": "/v1/requests/chatcmpl-1"})

        def get(self, url, **kwargs):
            return Response(200, {"choices": [{"message": {"content": '{"value":"ok"}'}}]})

    storage = MemStorage()
    backend = LiteLLMBackend(
        LLMBackendConfig(
            model="mistral/mistral-large-2512",
            mode="dispatch",
            dispatch_url="https://dispatch.example",
        ),
        http_session=Session(),
        storage=storage,
    )
    handle = backend.run_inference(
        job(
            content="meeting text",
            structured_output="test-output",
            llm_policy=LLMRequestPolicy(allowed_models=("mistral/mistral-large-2512",)),
        )
    )
    assert isinstance(handle, JobHandle)
    assert handle.attempted_requests == 1

    result = backend.reconcile(handle)

    assert result.output["choices"][0]["message"]["content"] == '{"value":"ok"}'
    budget, _ = load_llm_budget_cas(storage)
    ledger = _ledger_for(budget, "mistral/mistral-large-2512")
    assert ledger.inflight == {}
    assert ledger.requests_minute == 1


def test_reconcile_settles_reservation_before_rejecting_malformed_dispatch_output():
    """A malformed completed reply still consumed its one Worker/provider request.

    The sweep will submit a separate bounded correction, so the original reservation must be
    settled before local schema validation propagates its error.
    """

    class Response:
        status_code = 200
        headers = {}

        def json(self):
            return {"choices": [{"message": {"content": '{"value": 3}'}}]}

    class Session:
        def get(self, _url, **_kwargs):
            return Response()

    storage = MemStorage()
    backend = LiteLLMBackend(
        LLMBackendConfig(
            model="mistral/mistral-large-2512",
            mode="dispatch",
            dispatch_url="https://dispatch.example",
        ),
        http_session=Session(),
        storage=storage,
    )
    route = ROUTES["mistral/mistral-large-2512"]
    owner = "malformed-owner"
    mutate_llm_budget(
        storage,
        lambda budget, now: budget.reserve(
            owner,
            route.route_id or route.model,
            requests=1,
            tokens=10,
            cost=0.0,
            route=route,
            now=now,
        ),
    )

    with pytest.raises(LLMStructuredOutputError, match="Pydantic validation"):
        backend.reconcile(
            JobHandle(
                task="tag",
                recipe_hash="recipe-malformed",
                backend="litellm",
                ref="chatcmpl-malformed123",
                structured_output="test-output",
                model="mistral/mistral-large-2512",
                owner=owner,
                route_id=route.route_id,
                attempted_requests=1,
            )
        )

    budget, _ = load_llm_budget_cas(storage)
    ledger = _ledger_for(budget, "mistral/mistral-large-2512")
    assert ledger.inflight == {}
    assert ledger.requests_minute == 1


def test_usage_tokens_returns_none_not_zero_for_missing_or_invalid_usage():
    assert _usage_tokens({}) is None
    assert _usage_tokens({"usage": {}}) is None
    assert _usage_tokens({"usage": {"total_tokens": -5}}) is None
    assert _usage_tokens({"usage": {"total_tokens": 12}}) == 12
    assert _usage_tokens({"usage": {"prompt_tokens": 8, "completion_tokens": 4}}) == 12


def test_retry_after_seconds_rejects_non_finite_and_non_positive_values():
    """`nan`/`inf` would raise inside the caller's `timedelta(seconds=...)`, and a negative or
    zero value would immediately unblock the route -- all three must fall back to the default
    backoff (`None`) rather than propagating or defeating the block."""
    for bad in ("nan", "inf", "-inf", "-5", "0"):
        response = SimpleNamespace(headers={"retry-after": bad})
        assert _retry_after_seconds(response) is None
    response = SimpleNamespace(headers={"retry-after": "30"})
    assert _retry_after_seconds(response) == 30.0


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
    can actually happen, then settle back to the real request count once the call succeeds. That
    keeps the proactive ledger aligned with provider dashboards instead of halving daily capacity
    for the common one-attempt success path."""

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
    ledger = _ledger_for(budget, "gemini/gemini-3-flash-preview")
    assert ledger.inflight == {}
    assert ledger.requests_minute == 1


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
    ledger = _ledger_for(budget, "gemini/gemini-3-flash-preview")
    assert ledger.inflight == {}
    assert ledger.requests_minute == 1


def test_policy_bearing_call_requires_non_empty_recipe_hash():
    """Unconditional now, not just for dispatch mode: the deferred-request registry is keyed by
    recipe_hash too, and an empty one would let unrelated jobs collide in it."""
    storage = MemStorage()
    backend = LiteLLMBackend(
        LLMBackendConfig(
            model="mistral/mistral-large-2512",
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
    route = ROUTES["mistral/mistral-large-2512"]
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
            model="mistral/mistral-large-2512",
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
    ledger = budget.routes[route.route_id or route.model]
    assert ledger.inflight == {}
    assert ledger.cost_used == pytest.approx(80 * 0.14e-6 + 20 * 0.28e-6)


def test_structured_job_uses_native_json_schema_mode_for_gemini():
    """Gemini gets native JSON_SCHEMA mode via a direct LiteLLM call (unlike DeepSeek's
    prompt-embedded JSON mode below), bypassing Instructor entirely -- Instructor's own
    (provider, mode) compatibility table has no ``(Provider.GEMINI, Mode.JSON_SCHEMA)`` entry in
    the pinned release, so routing this through ``instructor.from_litellm()`` fails before any
    request reaches Gemini regardless of LiteLLM's version. citypods/llm_compat_probe.py's
    subtractive bisection against the live API found the actual native-mode rejection is narrower
    than "the whole schema," so only the offending keywords need to drop out; see the
    constraint-stripping test below."""
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        return structured_response('{"value":"ok"}')

    backend = LiteLLMBackend(
        LLMBackendConfig(model="gemini/gemini-3-flash-preview"), completion=completion
    )
    result = backend.run_inference(job(content="meeting text", structured_output="test-output"))

    assert result.output["choices"][0]["message"]["content"] == '{"value":"ok"}'
    sent = calls[0]["response_format"]
    assert sent["type"] == "json_schema"
    assert sent["json_schema"]["name"] == "ExampleOutput"
    assert sent["json_schema"]["schema"] == ExampleOutput.model_json_schema()


def test_gemma_route_uses_its_compiled_relaxed_schema_profile():
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        return structured_response('{"value":"ok","count":1,"tags":[]}')

    backend = LiteLLMBackend(LLMBackendConfig(model="google/gemma-4-31b-it"), completion=completion)
    result = backend.run_inference(
        job(content="meeting text", structured_output="constrained-output")
    )

    assert result.output["choices"][0]["message"]["content"]
    schema = calls[0]["response_format"]["json_schema"]["schema"]
    rendered = json.dumps(schema)
    assert "minLength" not in rendered
    assert "maxLength" not in rendered
    assert "minimum" not in rendered
    assert "maximum" not in rendered
    assert "minItems" not in rendered
    assert "maxItems" not in rendered


def test_gemma_dispatch_payload_uses_the_same_compiled_schema_profile():
    backend = LiteLLMBackend(LLMBackendConfig(model="google/gemma-4-31b-it"))
    payload = backend._payload(
        job(content="meeting text", structured_output="constrained-output"),
        ConstrainedOutput,
        resolved_model="google/gemma-4-31b-it",
    )

    schema = payload["response_format"]["json_schema"]["schema"]
    rendered = json.dumps(schema)
    assert payload["response_format"]["type"] == "json_schema"
    assert "minLength" not in rendered
    assert "maximum" not in rendered


def test_deepseek_structured_request_includes_schema_in_initial_prompt():
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        return structured_response('{"value":"ok"}')

    backend = LiteLLMBackend(
        LLMBackendConfig(model="deepseek/deepseek-v4-flash"), completion=completion
    )
    backend.run_inference(job(content="meeting text", structured_output="test-output"))

    sent = calls[0]
    assert sent["response_format"] == {"type": "json_object"}
    system = next(message for message in sent["messages"] if message["role"] == "system")
    assert "JSON Schema" in system["content"]
    assert json.dumps(ExampleOutput.model_json_schema(), sort_keys=True) in system["content"]


def test_deepseek_queue_payload_counts_the_rendered_schema_message():
    backend = LiteLLMBackend(LLMBackendConfig(model="deepseek/deepseek-v4-flash"))
    policy = LLMRequestPolicy(allowed_models=("deepseek/deepseek-v4-flash",), queue_only=True)
    inference_job = job(content="x", structured_output="test-output", max_tokens=1024)
    payload = backend._payload(
        inference_job,
        ExampleOutput,
        resolved_model="deepseek/deepseek-v4-flash",
        policy=policy,
        estimated_tokens=1,
        input_tokens_estimate=1,
        output_token_budget=1024,
    )
    assert estimate_tokens(payload["messages"]) > estimate_tokens(_messages(inference_job))


def test_gemini_structured_request_relaxes_constraint_keywords_only():
    """Gemini's native schema mode 400s specifically on minLength/maxLength/minimum/maximum/
    minItems/maxItems (confirmed against the live API via citypods/llm_compat_probe.py's
    subtractive bisection: stripping exactly this key set was the only strip, of defaults,
    additionalProperties, these constraints, enum, and $ref/$defs, that turned a 400 into a
    200). The request schema Instructor builds for Gemini must drop only those keys and keep
    everything else -- the contract's actual name, required list, and default-factory-driven
    optionality all still round-trip."""
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        return structured_response('{"value":"ok","count":1,"tags":[]}')

    backend = LiteLLMBackend(
        LLMBackendConfig(model="gemini/gemini-3-flash-preview"), completion=completion
    )
    result = backend.run_inference(
        job(content="meeting text", structured_output="constrained-output")
    )

    assert result.output["choices"][0]["message"]["content"] == '{"value":"ok","count":1,"tags":[]}'
    sent = calls[0]["response_format"]
    assert sent["type"] == "json_schema"
    assert sent["json_schema"]["name"] == "ConstrainedOutput"

    sent_schema = sent["json_schema"]["schema"]
    sent_schema_text = json.dumps(sent_schema)
    for key in ("minLength", "maxLength", "minimum", "maximum", "minItems", "maxItems"):
        assert key not in sent_schema_text
    assert sent_schema["required"] == ["value", "count"]

    # The real contract is untouched and still enforces the same bounds locally once a reply is
    # parsed -- only Gemini's copy of the request schema lost server-side enforcement of them.
    full_schema = ConstrainedOutput.model_json_schema()
    assert full_schema["properties"]["value"]["minLength"] == 1
    assert full_schema["properties"]["count"]["maximum"] == 100


def test_gemini_structured_retries_once_on_invalid_reply_then_succeeds():
    """Gemini's direct native-schema path replicates Instructor's own "one bounded corrective
    retry" contract by hand: an invalid first reply gets fed back as validation feedback, and a
    valid second reply completes normally -- no runtime fallback to a different mode, just the
    same retry-with-feedback loop every other provider gets via Instructor."""
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return structured_response("not valid json")
        return structured_response('{"value":"ok"}')

    backend = LiteLLMBackend(
        LLMBackendConfig(model="gemini/gemini-3-flash-preview"), completion=completion
    )
    result = backend.run_inference(job(content="meeting text", structured_output="test-output"))

    assert len(calls) == 2
    assert result.output["choices"][0]["message"]["content"] == '{"value":"ok"}'
    # The retry's messages include the first (invalid) reply and corrective feedback, not just
    # the original prompt repeated verbatim.
    retry_messages = calls[1]["messages"]
    assert retry_messages[-2] == {"role": "assistant", "content": "not valid json"}
    assert retry_messages[-1]["role"] == "user"


def test_gemini_structured_defers_after_exhausting_the_one_retry():
    """Two invalid replies in a row (the original attempt plus the one retry) is a
    ``LLMStructuredOutputError`` -- same outcome and same safe, content-free error message
    Instructor's ``InstructorRetryException`` produces for every other provider, so the caller's
    defer-and-retry-next-run handling needs no Gemini-specific branch."""

    def completion(**kwargs):
        return structured_response("still not valid json")

    backend = LiteLLMBackend(
        LLMBackendConfig(model="gemini/gemini-3-flash-preview"), completion=completion
    )
    with pytest.raises(LLMStructuredOutputError, match="failed Pydantic validation"):
        backend.run_inference(job(content="meeting text", structured_output="test-output"))


def test_gemini_structured_retry_settles_combined_usage_from_both_attempts():
    """A failed first attempt still reached Gemini and spent real tokens -- settlement must price
    the SUM of both attempts' usage, not just the successful retry's, or the first attempt's
    already-spent reservation is silently released back to the ledger (CodeRabbit, PR #1000)."""
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return structured_response(
                "not valid json",
                usage={"total_tokens": 50, "prompt_tokens": 40, "completion_tokens": 10},
            )
        return structured_response(
            '{"value":"ok"}',
            usage={"total_tokens": 30, "prompt_tokens": 20, "completion_tokens": 10},
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
            structured_output="test-output",
            llm_policy=LLMRequestPolicy(allowed_models=("gemini/gemini-3-flash-preview",)),
        )
    )

    assert len(calls) == 2
    assert isinstance(result, JobResult)
    # 50 + 30, not just the retry's 30 -- both attempts reached the provider.
    assert result.output["usage"] == {
        "total_tokens": 80,
        "prompt_tokens": 60,
        "completion_tokens": 20,
    }
    budget, _ = load_llm_budget_cas(storage)
    ledger = _ledger_for(budget, "gemini/gemini-3-flash-preview")
    assert ledger.inflight == {}
    assert ledger.tokens_minute == 80


def test_gemini_structured_429_on_retry_still_bills_the_real_first_attempt():
    """A rejected retry doesn't erase the call's earlier real usage: only the specific attempt
    that hit the 429 is excluded from settlement, not the whole call (contrast with the
    single-attempt 429 tests, which settle to 0 because their one and only attempt was the
    rejected one)."""

    class RateLimited(Exception):
        status_code = 429
        headers = {"retry-after": "30"}

    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return structured_response("not valid json")
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
            structured_output="test-output",
            llm_policy=LLMRequestPolicy(allowed_models=("gemini/gemini-3-flash-preview",)),
        )
    )

    assert len(calls) == 2
    assert isinstance(result, JobHandle)
    budget, _ = load_llm_budget_cas(storage)
    ledger = _ledger_for(budget, "gemini/gemini-3-flash-preview")
    # The first attempt reached the provider and got a real (if invalid) response -- it stays
    # charged. Only the second, rejected-as-429 attempt is excluded.
    assert ledger.requests_minute == 1


def test_strip_schema_keys_removes_matching_keys_at_every_depth():
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "string", "minLength": 1},
            "b": {"type": "array", "items": {"type": "integer", "maximum": 5}},
        },
    }
    stripped = _strip_schema_keys(schema, frozenset({"minLength", "maximum"}))
    assert stripped == {
        "type": "object",
        "properties": {
            "a": {"type": "string"},
            "b": {"type": "array", "items": {"type": "integer"}},
        },
    }
    assert schema["properties"]["a"]["minLength"] == 1, "must not mutate the caller's schema"


def test_schema_variant_model_preserves_name_and_leaves_original_untouched():
    Relaxed = _schema_variant_model(
        ConstrainedOutput,
        frozenset({"minLength", "maxLength", "minimum", "maximum", "maxItems"}),
    )

    assert Relaxed.__name__ == "ConstrainedOutput"
    assert issubclass(Relaxed, ConstrainedOutput)
    assert "minLength" not in json.dumps(Relaxed.model_json_schema())
    assert ConstrainedOutput.model_json_schema()["properties"]["value"]["minLength"] == 1


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
            model="mistral/mistral-large-2512",
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
            model="mistral/mistral-large-2512",
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
            model="mistral/mistral-large-2512",
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


def test_schema_correction_enqueue_uses_a_separate_idempotency_key():
    calls = []

    class Response:
        status_code = 202
        headers = {"location": "/v1/requests/chatcmpl-corrected"}

        def json(self):
            return {"id": "chatcmpl-corrected"}

    class Session:
        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()

    backend = LiteLLMBackend(
        LLMBackendConfig(
            model="mistral/mistral-large-2512",
            mode="dispatch",
            dispatch_url="https://dispatch.example",
            dispatch_auth_token="dispatch-token",
        ),
        http_session=Session(),
    )
    corrected = backend.retry_malformed_dispatched(
        JobHandle(
            task="tag",
            recipe_hash="recipe-1",
            backend="litellm",
            ref="/v1/requests/chatcmpl-original",
            structured_output="test-output",
            model="mistral/mistral-large-2512",
        )
    )

    assert corrected.ref == "/v1/requests/chatcmpl-corrected"
    assert corrected.structured_output == "test-output"
    assert calls == [
        (
            "https://dispatch.example/v1/requests/chatcmpl-original/schema-retry",
            {
                "json": {},
                "headers": {
                    "content-type": "application/json",
                    "idempotency-key": "recipe-1:schema-correction-v1",
                    "authorization": "Bearer dispatch-token",
                },
                "timeout": 30.0,
            },
        )
    ]


def test_schema_correction_rejects_an_invalid_dispatch_reference_before_posting():
    class Session:
        def post(self, *_args, **_kwargs):
            raise AssertionError("invalid refs must not be sent to the Worker")

    backend = LiteLLMBackend(
        LLMBackendConfig(
            model="mistral/mistral-large-2512",
            mode="dispatch",
            dispatch_url="https://dispatch.example",
        ),
        http_session=Session(),
    )

    with pytest.raises(LLMBackendError, match="valid dispatch request reference"):
        backend.retry_malformed_dispatched(
            JobHandle(
                task="tag",
                recipe_hash="recipe-invalid-ref",
                backend="litellm",
                ref="not-a-dispatch-request",
            )
        )


def test_dispatch_unknown_response_contract_remains_a_version_skew_error():
    class Response:
        status_code = 200
        headers = {}

        def json(self):
            return {"choices": []}

    backend = LiteLLMBackend(
        LLMBackendConfig(
            model="mistral/mistral-large-2512",
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
            model="mistral/mistral-large-2512",
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
            model="mistral/mistral-large-2512",
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


def test_delete_dispatched_ref_normalizes_ref_formats():
    """delete_dispatched_ref must accept bare IDs, path-style refs, and full URLs --
    handles store the `location` header (path-style), not a bare ID."""
    deleted_urls = []

    class RecordingSession(requests.Session):
        def delete(self, url, **_kwargs):
            deleted_urls.append(url)

    backend = LiteLLMBackend(
        LLMBackendConfig(
            model="mistral/mistral-large-2512",
            mode="dispatch",
            dispatch_url="https://dispatch.example",
            dispatch_auth_token="test-token",
        ),
        http_session=RecordingSession(),
    )

    # Bare ID
    backend.delete_dispatched_ref("chatcmpl-abc12345678")
    assert len(deleted_urls) == 1
    assert deleted_urls[-1] == "https://dispatch.example/v1/requests/chatcmpl-abc12345678"

    # Path-style ref (what handles actually store from the Worker's location header)
    backend.delete_dispatched_ref("/v1/requests/chatcmpl-xyz99999999")
    assert len(deleted_urls) == 2
    assert deleted_urls[-1] == "https://dispatch.example/v1/requests/chatcmpl-xyz99999999"

    # Full URL ref
    backend.delete_dispatched_ref("https://dispatch.example/v1/requests/chatcmpl-full00000001")
    assert len(deleted_urls) == 3
    assert deleted_urls[-1] == "https://dispatch.example/v1/requests/chatcmpl-full00000001"

    # No-op for non-chatcmpl refs
    backend.delete_dispatched_ref("something-else")
    assert len(deleted_urls) == 3

    # No-op for empty ref
    backend.delete_dispatched_ref("")
    assert len(deleted_urls) == 3


def test_reconcile_purges_r2_after_deferred_write():
    """reconcile() must DELETE the R2 object after a successful deferred write (the post-persist
    purge path), including when the handle ref is a path-style location."""
    deleted_urls = []

    class TrackingSession(requests.Session):
        def get(self, url, **_kwargs):
            res = requests.Response()
            res.status_code = 200
            res._content = json.dumps(
                {
                    "id": "chatcmpl-purge1",
                    "choices": [{"message": {"content": "ok"}}],
                }
            ).encode()
            return res

        def delete(self, url, **_kwargs):
            deleted_urls.append(url)

    storage = MemStorage()
    backend = LiteLLMBackend(
        LLMBackendConfig(
            model="mistral/mistral-large-2512",
            mode="dispatch",
            dispatch_url="https://dispatch.example",
        ),
        http_session=TrackingSession(),
        storage=storage,
    )

    handle = JobHandle(
        task="summarize",
        recipe_hash="purge-test-recipe",
        backend="litellm",
        ref="/v1/requests/chatcmpl-purge1",
        model="mistral/mistral-large-2512",
    )

    result = backend.reconcile(handle)
    assert result is not None
    # Should have issued a DELETE for the R2 object
    assert len(deleted_urls) == 1
    assert "chatcmpl-purge1" in deleted_urls[0]


def test_dispatch_payload_includes_policy_fields_and_estimated_tokens():
    post_json = None

    class CaptureSession(requests.Session):
        def post(self, url, json=None, headers=None, timeout=None):
            nonlocal post_json
            post_json = json
            res = requests.Response()
            res.status_code = 200
            res._content = b'{"id":"resp-1","choices":[{"message":{"content":"ok"}}]}'
            return res

    storage = MemStorage()
    backend = LiteLLMBackend(
        LLMBackendConfig(
            model="gemini/gemini-3-flash-preview",
            mode="dispatch",
            dispatch_url="https://dispatch.example",
        ),
        http_session=CaptureSession(),
        storage=storage,
    )

    # Relative to "now", not a fixed calendar timestamp: a hardcoded absolute deadline that was
    # comfortably in the future when this test was written silently becomes a past deadline (and
    # a spurious "deadline gate" rejection -> JobHandle instead of JobResult) once real time
    # passes it -- exactly what broke this test in CI after this file's own authoring date caught
    # up to a hardcoded "2026-08-07T12:00:00Z" (review/41).
    deadline = datetime.now(UTC) + timedelta(hours=1)
    pol = LLMRequestPolicy(
        allowed_models=("gemini/gemini-3-flash-preview",),
        allow_paid=True,
        allow_batch=True,
        submit_next=True,
        deadline_at=deadline,
        # Gemini also offers `direct`; without this the call would go direct by default
        # (review/41 -- a dual-transport route only dispatches when a caller opts in), and this
        # test is specifically exercising the dispatch payload.
        allow_dispatch_overflow=True,
    )

    res = backend.run_inference(
        InferenceJob(
            task="summarize",
            recipe_hash="test-recipe-1",
            inputs={"content": "hello test content", "llm_policy": pol},
        )
    )

    assert isinstance(res, JobResult)
    assert post_json is not None
    assert post_json["allow_paid"] is True
    assert post_json["allow_batch"] is True
    assert post_json["submit_next"] is True
    assert post_json["deadline_at"] == deadline.isoformat()
    assert "estimated_tokens" in post_json
    assert post_json["estimated_tokens"] > 0
    assert post_json["input_tokens_estimate"] > 0
    assert post_json["output_token_budget"] == 1024


def test_dual_transport_route_prefers_direct_without_opt_in():
    """A route offering both `direct` and `llm-dispatch` (Gemini) must not dispatch just because
    the backend has `dispatch_url` configured -- only when the caller explicitly sets
    `allow_dispatch_overflow`. This is the regression this test guards: a prior version routed
    every such call over the Worker whenever `dispatch_url` was set at all, which silently broke
    city discovery's same-run-completion requirement (review/41)."""
    posted = False

    class NoPostSession(requests.Session):
        def post(self, *_args, **_kwargs):
            nonlocal posted
            posted = True
            res = requests.Response()
            res.status_code = 200
            res._content = b"{}"
            return res

    storage = MemStorage()
    backend = LiteLLMBackend(
        LLMBackendConfig(
            model="gemini/gemini-3-flash-preview",
            mode="direct",
            dispatch_url="https://dispatch.example",
        ),
        completion=_strict_direct_completion,
        http_session=NoPostSession(),
        storage=storage,
    )

    pol = LLMRequestPolicy(allowed_models=("gemini/gemini-3-flash-preview",), allow_paid=False)

    res = backend.run_inference(
        InferenceJob(
            task="summarize",
            recipe_hash="test-recipe-direct-default",
            inputs={"content": "hello test content", "llm_policy": pol},
        )
    )

    assert isinstance(res, JobResult)
    assert posted is False


def test_dual_transport_route_dispatches_with_explicit_overflow_and_reserves_by_recipe_hash():
    """The Gemini/`allow_dispatch_overflow=True` opt-in path, asserting the ledger reservation
    owner is the deterministic `recipe_hash` -- not a fresh UUID -- so a retry before settlement
    resolves to the Worker's own `idempotency-key: recipe_hash` dedup instead of double-reserving
    (the bug CodeRabbit flagged against `llm_scheduler.py::_owner_for`, review/41)."""

    class PendingSession(requests.Session):
        def post(self, url, json=None, headers=None, timeout=None):
            res = requests.Response()
            res.status_code = 202
            res._content = b'{"id":"chatcmpl-pending"}'
            res.headers["location"] = "/v1/requests/chatcmpl-pending"
            return res

    storage = MemStorage()
    backend = LiteLLMBackend(
        LLMBackendConfig(
            model="gemini/gemini-3-flash-preview",
            mode="dispatch",
            dispatch_url="https://dispatch.example",
        ),
        http_session=PendingSession(),
        storage=storage,
    )

    pol = LLMRequestPolicy(
        allowed_models=("gemini/gemini-3-flash-preview",),
        allow_paid=False,
        allow_dispatch_overflow=True,
    )
    recipe_hash = "test-recipe-overflow-owner"

    res = backend.run_inference(
        InferenceJob(
            task="summarize",
            recipe_hash=recipe_hash,
            inputs={"content": "hello test content", "llm_policy": pol},
        )
    )

    assert isinstance(res, JobHandle)
    budget, _ = load_llm_budget_cas(storage)
    ledger = _ledger_for(budget, "gemini/gemini-3-flash-preview")
    assert recipe_hash in ledger.inflight


def test_queue_only_policy_enqueues_without_a_runner_quota_reservation():
    """Durable backlog work is accepted by the Worker, not locally rate-limited first."""

    class PendingSession(requests.Session):
        def post(self, _url, json=None, headers=None, timeout=None):
            assert json["model"] == "gemini/gemini-3-flash-preview"
            assert json["allowed_models"] == [
                "gemini/gemini-3-flash-preview",
                "gemini/gemini-3.1-flash-lite",
            ]
            assert headers["idempotency-key"] == "test-durable-queue:durable-queue-v1"
            response = requests.Response()
            response.status_code = 202
            response._content = b'{"id":"chatcmpl-durable"}'
            response.headers["location"] = "/v1/requests/chatcmpl-durable"
            return response

    backend = LiteLLMBackend(
        LLMBackendConfig(
            model="gemini/gemini-3-flash-preview",
            mode="dispatch",
            dispatch_url="https://dispatch.example",
        ),
        http_session=PendingSession(),
        storage=MemStorage(),
    )
    result = backend.run_inference(
        InferenceJob(
            task="tag",
            recipe_hash="test-durable-queue",
            inputs={
                "content": "meeting text",
                "llm_policy": LLMRequestPolicy(
                    allowed_models=(
                        "gemini/gemini-3-flash-preview",
                        "gemini/gemini-3.1-flash-lite",
                    ),
                    queue_only=True,
                ),
            },
        )
    )
    assert isinstance(result, JobHandle)
    assert result.owner is None


def test_require_direct_policy_bypasses_dispatch():
    posted = False

    class NoPostSession(requests.Session):
        def post(self, *_args, **_kwargs):
            nonlocal posted
            posted = True
            res = requests.Response()
            res.status_code = 200
            res._content = b"{}"
            return res

    storage = MemStorage()
    backend = LiteLLMBackend(
        LLMBackendConfig(
            model="gemini/gemini-3-flash-preview",
            mode="dispatch",
            dispatch_url="https://dispatch.example",
        ),
        completion=_strict_direct_completion,
        http_session=NoPostSession(),
        storage=storage,
    )

    pol = LLMRequestPolicy(
        allowed_models=("gemini/gemini-3-flash-preview",),
        require_direct=True,
    )

    res = backend.run_inference(
        InferenceJob(
            task="summarize",
            recipe_hash="test-recipe-direct",
            inputs={"content": "hello direct test", "llm_policy": pol},
        )
    )

    assert isinstance(res, JobResult)
    assert res.output["choices"][0]["message"]["content"] == "direct response"
    assert not posted


def test_reconcile_emits_warning_on_retrying_upstream_timeout(capsys):
    class TimeoutRetrySession(requests.Session):
        def get(self, *_args, **_kwargs):
            res = requests.Response()
            res.status_code = 202
            res._content = json.dumps(
                {
                    "id": "chatcmpl-test-1",
                    "status": "pending",
                    "attempts": 2,
                    "available_at": "2026-08-09T06:00:00Z",
                    "last_error": {
                        "code": "upstream_timeout",
                        "duration_seconds": 720,
                        "model": "deepseek/deepseek-v4-pro",
                        "route_id": "deepseek_v4_pro_primary",
                    },
                }
            ).encode()
            return res

    storage = MemStorage()
    backend = LiteLLMBackend(
        LLMBackendConfig(
            model="deepseek/deepseek-v4-pro",
            mode="dispatch",
            dispatch_url="https://dispatch.example",
        ),
        http_session=TimeoutRetrySession(),
        storage=storage,
    )

    handle = JobHandle(
        backend="litellm",
        task="summarize",
        recipe_hash="recipe-timeout-retry",
        ref="chatcmpl-test-1",
        model="deepseek/deepseek-v4-pro",
    )

    result = backend.reconcile(handle)
    assert result is None
    captured = capsys.readouterr()
    assert "::warning title=LLM Upstream Timeout Warning::" in captured.out
    assert "timed out after 720s" in captured.out
    assert "deepseek_v4_pro_primary" in captured.out


def test_reconcile_emits_error_on_terminal_upstream_timeout(capsys):
    class TimeoutFailedSession(requests.Session):
        def get(self, *_args, **_kwargs):
            res = requests.Response()
            res.status_code = 502
            res._content = json.dumps(
                {
                    "error": {
                        "code": "upstream_timeout",
                        "message": (
                            "Upstream LLM provider timed out after 720s without completing response"
                        ),
                        "duration_seconds": 720,
                        "attempts": 5,
                        "route_id": "deepseek_v4_pro_primary",
                    }
                }
            ).encode()
            return res

    storage = MemStorage()
    backend = LiteLLMBackend(
        LLMBackendConfig(
            model="deepseek/deepseek-v4-pro",
            mode="dispatch",
            dispatch_url="https://dispatch.example",
        ),
        http_session=TimeoutFailedSession(),
        storage=storage,
    )

    handle = JobHandle(
        backend="litellm",
        task="summarize",
        recipe_hash="recipe-terminal-timeout",
        ref="chatcmpl-test-terminal",
        model="deepseek/deepseek-v4-pro",
    )

    with pytest.raises(LLMBackendError, match="timed out after 720s"):
        backend.reconcile(handle)
    captured = capsys.readouterr()
    assert "::error title=LLM Terminal Timeout Failure::" in captured.out
    assert "failed permanently after 5 attempts exceeding 720s timeout" in captured.out


def test_reconcile_treats_an_operator_retired_dispatch_record_as_terminal():
    class RetiredSession(requests.Session):
        def get(self, *_args, **_kwargs):
            res = requests.Response()
            res.status_code = 410
            res._content = b'{"error":{"code":"retired"}}'
            return res

    backend = LiteLLMBackend(
        LLMBackendConfig(model="google/gemma-4-31b-it", mode="dispatch", dispatch_url="https://x"),
        http_session=RetiredSession(),
        storage=MemStorage(),
    )
    handle = JobHandle(
        backend="litellm",
        task="tag",
        recipe_hash="legacy-prelabel",
        ref="chatcmpl-retired",
        model="google/gemma-4-31b-it",
    )

    with pytest.raises(LLMDispatchTerminalError, match="HTTP 410"):
        backend.reconcile(handle)


@pytest.fixture
def gateway_env(monkeypatch):
    """A configured gateway with no inherited overrides leaking in from the environment."""
    monkeypatch.delenv("AI_GATEWAY_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_AI_GATEWAY", raising=False)
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "cf-acc-123")
    monkeypatch.setenv("AI_GATEWAY_ID", "citypods-dispatch")
    monkeypatch.setenv("AI_GATEWAY_AUTH_TOKEN", "test-auth-token")
    return monkeypatch


def _recording_backend(model, **config_kwargs):
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        return structured_response(json.dumps({"value": "ok"}))

    backend = LiteLLMBackend(
        LLMBackendConfig(model=model, **config_kwargs),
        completion=completion,
        storage=MemStorage(),
    )
    return backend, calls


_GW = "https://gateway.ai.cloudflare.com/v1/cf-acc-123/citypods-dispatch"


# For OpenAI-compatible routes, LiteLLM appends "/chat/completions", so the assertions below
# reconstruct that whole URL. For Gemini, LiteLLM's native Google AI Studio adapter (VertexLLM)
# appends `/models/{model}:{endpoint}` rather than `/chat/completions`.
@pytest.mark.parametrize(
    "route",
    sorted(ROUTE_REGISTRY.values(), key=lambda r: r.route_id or r.model),
    ids=lambda r: r.route_id or r.model,
)
def test_every_catalog_route_builds_its_configured_gateway_url(route, gateway_env):
    """Covers all routes, including providers no single logical model would exercise."""
    backend, _ = _recording_backend("gemini/gemini-3.6-flash")

    api_base, headers = backend._resolve_api_base_and_headers(route, direct=True)

    slug = route.ai_gateway_slug or route.provider
    if route.provider == "gemini":
        assert (
            api_base + f"/models/{route.upstream_model}:generateContent"
            == f"{_GW}/{slug}/v1beta/models/{route.upstream_model}:generateContent"
        )
    else:
        assert api_base + "/chat/completions" == f"{_GW}/{slug}{route.ai_gateway_chat_path}"
    expected_headers = {"cf-aig-authorization": "Bearer test-auth-token"}
    if route.ai_gateway_max_attempts is not None:
        expected_headers["cf-aig-max-attempts"] = str(route.ai_gateway_max_attempts)
    assert headers == expected_headers


def test_sambanova_routes_use_a_single_gateway_attempt(gateway_env):
    route = next(route for route in ROUTE_REGISTRY.values() if route.provider == "sambanova")
    backend, _ = _recording_backend("meta-llama/llama-3.3-70b-instruct")

    _, headers = backend._resolve_api_base_and_headers(route, direct=True)

    assert headers == {
        "cf-aig-authorization": "Bearer test-auth-token",
        "cf-aig-max-attempts": "1",
    }


# How each custom provider is registered on the Cloudflare side, and therefore what
# `ai_gateway_chat_path` has to be. This table exists because AI Gateway does NOT join a Custom
# Provider's Base URL the way its documentation says: instead of `{base_url}/{provider-path}`, it
# rewrites the base URL's LAST path segment to a hardcoded `v1` and appends the caller path
# (established 2026-08-29 by registering a throwaway custom provider against an echo service).
# Because the Cloudflare-side Base URL is not represented in this repo, the mapping cannot be
# derived -- so it is written down here, and a new custom provider trips the completeness check
# below until someone records how it is registered.
CUSTOM_PROVIDER_GATEWAY_PATHS = {
    # Registered at api_base verbatim; the `/v1` in api_base is also the substituted segment, so
    # the chat path must carry it or the dispatch lands on the origin root and 404s.
    "siliconflow": "/v1/chat/completions",
    "sambanova": "/v1/chat/completions",
    "nvidia": "/v1/chat/completions",
    "airforce": "/v1/chat/completions",
    # Registered as `https://api.kilo.ai/api/gateway/v1` -- Kilo serves that path too, so the
    # forced `v1` substitution lands correctly and the caller path stays bare.
    "kilo": "/chat/completions",
    # Routed through workers/llm-provider-shim, which restores the real upstream prefix, so the
    # caller path is bare here as well.
    "zai": "/chat/completions",
    "opencode": "/chat/completions",
}


def test_every_custom_provider_records_how_it_is_registered():
    """A new custom provider must state its gateway path, since the rule cannot be inferred."""
    configured = {
        route.provider
        for route in ROUTE_REGISTRY.values()
        if (route.ai_gateway_slug or "").startswith("custom-")
    }
    assert configured == set(CUSTOM_PROVIDER_GATEWAY_PATHS), (
        "custom providers changed; record the new provider's Cloudflare-side registration in "
        "CUSTOM_PROVIDER_GATEWAY_PATHS (and see workers/llm-provider-shim/README.md for why the "
        "documented base-URL join does not apply)"
    )


@pytest.mark.parametrize(
    "route",
    sorted(
        (r for r in ROUTE_REGISTRY.values() if (r.ai_gateway_slug or "").startswith("custom-")),
        key=lambda r: r.route_id or r.model,
    ),
    ids=lambda r: r.route_id or r.model,
)
def test_custom_provider_routes_use_their_recorded_gateway_path(route):
    expected = CUSTOM_PROVIDER_GATEWAY_PATHS[route.provider]
    assert route.ai_gateway_chat_path == expected, (
        f"{route.route_id}: ai_gateway_chat_path {route.ai_gateway_chat_path!r} does not match the "
        f"recorded registration for {route.provider!r} ({expected!r})"
    )


# Only single-provider models belong here. A logical model served by several providers (6 of 31 in
# the catalog -- `deepseek/deepseek-v4-flash` spans deepseek, custom-siliconflow and
# custom-opencode) has no fixed gateway slug: the scheduler picks whichever physical route has
# capacity, so pinning one slug end-to-end would assert on scheduler choice rather than on URL
# construction. The catalog test above covers those routes directly.
@pytest.mark.parametrize(
    ("model", "expected_request_url"),
    [
        (
            "gemini/gemini-3.6-flash",
            f"{_GW}/google-ai-studio/v1beta/models/gemini-3.6-flash:generateContent",
        ),
        ("mistral/mistral-large-2512", f"{_GW}/mistral/v1/chat/completions"),
        ("zai/glm-4.7-flash", f"{_GW}/custom-zai/chat/completions"),
    ],
)
def test_direct_call_requests_the_gateway_url(model, expected_request_url, gateway_env):
    backend, calls = _recording_backend(model)

    assert isinstance(backend.run_inference(job(content="test")), JobResult)
    assert len(calls) == 1
    if model.startswith("gemini/"):
        route = ROUTE_CANDIDATES[model][0]
        actual_url = calls[0]["api_base"] + f"/models/{route.upstream_model}:generateContent"
        assert actual_url == expected_request_url
    else:
        assert calls[0]["api_base"] + "/chat/completions" == expected_request_url
    assert calls[0]["extra_headers"] == {"cf-aig-authorization": "Bearer test-auth-token"}


def test_gemini_direct_gateway_url_matches_litellm_request(gateway_env):
    """LiteLLM's native Google AI Studio adapter must produce the exact gateway URL."""
    from litellm.llms.vertex_ai.gemini.vertex_and_google_ai_studio_gemini import VertexLLM

    route = ROUTE_CANDIDATES["gemini/gemini-3.5-flash"][0]
    backend, _ = _recording_backend("gemini/gemini-3.5-flash")
    api_base, _ = backend._resolve_api_base_and_headers(route, direct=True)

    auth, url = VertexLLM()._get_token_and_url(
        model=route.upstream_model,
        auth_header=None,
        gemini_api_key="test-gemini-key",
        vertex_project=None,
        vertex_location=None,
        vertex_credentials=None,
        stream=False,
        custom_llm_provider="gemini",
        api_base=api_base,
    )
    expected = f"{_GW}/google-ai-studio/v1beta/models/{route.upstream_model}:generateContent"
    assert url == expected
    assert auth == {"x-goog-api-key": "test-gemini-key"}


def test_single_provider_models_used_end_to_end_really_are_single_provider():
    """Guards the parametrization above: a second provider would make those cases flaky."""
    for model in ("gemini/gemini-3.6-flash", "mistral/mistral-large-2512", "zai/glm-4.7-flash"):
        slugs = {route.ai_gateway_slug or route.provider for route in ROUTE_CANDIDATES[model]}
        assert len(slugs) == 1, f"{model} now spans {slugs}; move it to the catalog-level test"


def test_direct_call_uses_ai_gateway_base_url_override(monkeypatch):
    monkeypatch.delenv("LLM_AI_GATEWAY", raising=False)
    monkeypatch.setenv("AI_GATEWAY_BASE_URL", "https://custom-gw.example.com/v1/custom-gw")
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("AI_GATEWAY_AUTH_TOKEN", raising=False)
    backend, calls = _recording_backend("mistral/mistral-large-2512")

    assert isinstance(backend.run_inference(job(content="test")), JobResult)
    assert calls[0]["api_base"] == "https://custom-gw.example.com/v1/custom-gw/mistral/v1"
    assert not calls[0].get("extra_headers")


@pytest.mark.parametrize("disabled", ["0", "false", "off", "no", "FALSE"])
def test_llm_ai_gateway_kill_switch_restores_the_direct_upstream(disabled, gateway_env):
    gateway_env.setenv("LLM_AI_GATEWAY", disabled)
    backend, calls = _recording_backend("gemini/gemini-3.6-flash")

    assert isinstance(backend.run_inference(job(content="test")), JobResult)
    assert calls[0]["api_base"] == "https://generativelanguage.googleapis.com/v1beta"
    assert not calls[0].get("extra_headers")


def test_direct_call_without_gateway_configured_keeps_the_provider_upstream(monkeypatch):
    monkeypatch.delenv("AI_GATEWAY_BASE_URL", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("LLM_AI_GATEWAY", raising=False)
    backend, calls = _recording_backend("gemini/gemini-3.6-flash")

    assert isinstance(backend.run_inference(job(content="test")), JobResult)
    assert calls[0]["api_base"] == "https://generativelanguage.googleapis.com/v1beta"
    assert not calls[0].get("extra_headers")


def test_dispatch_payload_is_never_rewritten_for_the_gateway(gateway_env):
    """The Worker fronts its own providers with the gateway; the payload must stay untouched.

    Handing it a gateway `api_base` would double-proxy the call, and `cf-aig-authorization` is a
    credential the Worker has no use for and should never receive.
    """
    posted = {}

    class Response:
        status_code = 200
        headers: dict[str, str] = {}
        text = ""

        def json(self):
            return {"choices": [{"message": {"content": json.dumps({"value": "ok"})}}]}

    class Session:
        def post(self, url, **kwargs):
            posted.update(kwargs)
            return Response()

    backend = LiteLLMBackend(
        LLMBackendConfig(
            model="gemini/gemini-3.6-flash",
            mode="dispatch",
            dispatch_url="https://dispatch.example",
        ),
        http_session=Session(),
        storage=MemStorage(),
    )
    backend.run_inference(job(content="test"))

    payload = posted["json"]
    # The payload has always carried the route's own upstream, and still should -- what must not
    # leak is the *gateway* rewrite and its credential.
    assert payload["api_base"] == "https://generativelanguage.googleapis.com/v1beta/openai"
    assert "gateway.ai.cloudflare.com" not in json.dumps(posted)
    assert "extra_headers" not in payload
    assert "cf-aig-authorization" not in json.dumps(posted).lower()


def test_dispatch_v2_stats_returns_the_bounded_scheduler_snapshot():
    calls = []

    class Response:
        status_code = 200

        def json(self):
            return {"jobs": {"by_state": {"queued": 3}}, "bundles": {"active": 1}}

    class Session:
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()

    backend = LiteLLMBackend(
        LLMBackendConfig(
            model="gemini/gemini-3.6-flash",
            dispatch_v2_url="https://dispatch.example/",
            dispatch_v2_auth_token="v2-secret",
        ),
        http_session=Session(),
    )

    assert backend.dispatch_v2_stats(limit=7) == {
        "jobs": {"by_state": {"queued": 3}},
        "bundles": {"active": 1},
    }
    assert calls[0][0] == "https://dispatch.example/v2/stats?limit=7"
    assert calls[0][1]["headers"] == {"authorization": "Bearer v2-secret"}


@pytest.mark.parametrize("config_name", ["dispatch_url", "dispatch_v2_url"])
def test_backend_rejects_cleartext_dispatch_urls_before_any_request(config_name):
    calls = []

    class Session:
        def get(self, *args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("an insecure dispatch URL must not be requested")

    with pytest.raises(ValueError, match="must be an HTTPS URL"):
        LiteLLMBackend(
            LLMBackendConfig(
                model="gemini/gemini-3.6-flash",
                **{config_name: "http://dispatch.example"},
            ),
            http_session=Session(),
        )
    assert calls == []


def test_immediate_direct_ignores_poisoned_cache_and_never_persists_results():
    from citypods.compute.llm_deferred import write_deferred

    storage = MemStorage()
    write_deferred(
        storage,
        "recipe-1",
        JobResult(task="tag", recipe_hash="recipe-1", output={"bad": "old unstructured response"}),
    )
    old_registry = {k: v for k, v in storage.objs.items() if "llm_deferred" in k}
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        return {"choices": [{"message": {"content": "fresh"}}]}

    class NoWorker:
        def post(self, *args, **kwargs):
            pytest.fail("immediate inference contacted the dispatch Worker")

    backend = LiteLLMBackend(
        LLMBackendConfig(
            model="gemini/gemini-3.5-flash",
            mode="dispatch",
            dispatch_url="https://dispatch.example",
        ),
        completion=completion,
        storage=storage,
        http_session=NoWorker(),
    )
    result = backend.run_immediate(
        job(
            content="test",
            timeout=12,
            num_retries=0,
            llm_policy=LLMRequestPolicy(
                require_direct=True,
                allowed_models=("gemini/gemini-3.5-flash",),
                deadline_at=datetime.now(UTC) + timedelta(minutes=1),
            ),
        )
    )
    assert result.output["choices"][0]["message"]["content"] == "fresh"
    assert calls[0]["timeout"] == 12
    assert calls[0]["num_retries"] == 0
    assert {k: v for k, v in storage.objs.items() if "llm_deferred" in k} == old_registry
    budget, _ = load_llm_budget_cas(storage)
    assert any(route.requests_minute == 1 for route in budget.routes.values())


def test_immediate_capacity_failure_never_leaves_a_sweep_job(monkeypatch):
    storage = MemStorage()
    backend = LiteLLMBackend(LLMBackendConfig(model="gemini/gemini-3.5-flash"), storage=storage)
    monkeypatch.setattr(
        backend,
        "_run_policy_job_paced",
        lambda *args: JobHandle(
            task="tag", recipe_hash="recipe-1", backend="litellm", ref="deferred:recipe-1"
        ),
    )
    with pytest.raises(TimeoutError, match="direct route capacity"):
        backend.run_immediate(
            job(
                content="test",
                llm_policy=LLMRequestPolicy(
                    require_direct=True, deadline_at=datetime.now(UTC) + timedelta(minutes=1)
                ),
            )
        )
    assert not storage.objs


def test_immediate_rejects_queue_policy_before_any_io():
    backend = LiteLLMBackend(storage=MemStorage())
    with pytest.raises(ValueError, match="forbids dispatch"):
        backend.run_immediate(
            job(
                llm_policy=LLMRequestPolicy(
                    require_direct=True,
                    queue_only=True,
                    deadline_at=datetime.now(UTC) + timedelta(minutes=1),
                )
            )
        )


def test_safe_schema_diagnostics_preserve_field_paths_but_not_extra_keys():
    from pydantic import ValidationError

    try:
        ExampleOutput.model_validate({"value": [], "secret-key": "secret-value"})
    except ValidationError as exc:
        diagnostic = _safe_structured_failure_diagnostic(
            exc, job(content="secret-prompt"), ExampleOutput, "model"
        )
    errors = diagnostic["validation_errors"]
    assert errors[0]["loc"] == ["value"]
    assert errors[1]["loc"] == ["<field>"]
    assert "secret" not in json.dumps(diagnostic)


def test_immediate_schema_repair_is_local_and_never_persists_a_handle():
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        return structured_response("not json" if len(calls) == 1 else '{"value":"fixed"}')

    storage = MemStorage()
    backend = LiteLLMBackend(
        LLMBackendConfig(model="gemini/gemini-3.5-flash"), completion=completion, storage=storage
    )
    result = backend.run_immediate(
        job(
            content="test",
            structured_output="test-output",
            llm_policy=LLMRequestPolicy(
                require_direct=True,
                allowed_models=("gemini/gemini-3.5-flash",),
                deadline_at=datetime.now(UTC) + timedelta(minutes=1),
            ),
        )
    )
    assert len(calls) == 2
    assert "corrected JSON" in calls[1]["messages"][-1]["content"]
    assert result.output["choices"][0]["message"]["content"] == '{"value":"fixed"}'
    assert not any("llm_deferred" in key for key in storage.objs)
