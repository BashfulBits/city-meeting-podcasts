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
    LLMUpstreamPassthroughError,
    _pacing_wait_seconds,
    _priced_actual,
    _retry_after_seconds,
    _safe_structured_failure_diagnostic,
    _usage_tokens,
)
from citypods.compute.llm_budget import daily_reset_key, load_llm_budget_cas, mutate_llm_budget
from citypods.compute.llm_policy import (
    ROUTE_CANDIDATES,
    ROUTE_REGISTRY,
    ROUTES,
    LLMRequestPolicy,
)
from citypods.compute.structured import register_response_model
from citypods.compute.structured_shaping import strip_schema_keys
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


@pytest.mark.parametrize("structured", [False, True], ids=["unstructured", "native-structured"])
def test_direct_litellm_call_gets_a_bounded_default_timeout(structured):
    """Without a job-level timeout LiteLLM falls back to its 6000 s default, which once hung a
    run for ~40 minutes on a stalled provider; direct calls must carry the bounded default."""
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        return structured_response('{"value":"ok"}')

    backend = LiteLLMBackend(
        LLMBackendConfig(model="gemini/gemini-3-flash-preview"), completion=completion
    )
    inputs = {"structured_output": "test-output"} if structured else {}
    backend.run_inference(job(content="meeting text", **inputs))

    assert calls[0]["timeout"] == 720.0


@pytest.mark.parametrize("structured", [False, True], ids=["unstructured", "native-structured"])
def test_job_level_timeout_overrides_the_direct_default(structured):
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        return structured_response('{"value":"ok"}')

    backend = LiteLLMBackend(
        LLMBackendConfig(model="gemini/gemini-3-flash-preview", direct_timeout_seconds=300.0),
        completion=completion,
    )
    inputs = {"structured_output": "test-output"} if structured else {}
    backend.run_inference(job(content="meeting text", timeout=45, **inputs))

    assert calls[0]["timeout"] == 45


def test_dispatch_payload_does_not_pick_up_the_direct_timeout_default():
    backend = LiteLLMBackend(LLMBackendConfig(model="gemini/gemini-3-flash-preview"))
    payload = backend._payload(
        job(content="meeting text"), resolved_model="gemini/gemini-3-flash-preview"
    )

    assert "timeout" not in payload


def test_direct_timeout_default_reads_its_own_env_var(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("LLM_DIRECT_TIMEOUT_SECONDS", "900")

    config = LLMBackendConfig.from_env()

    assert config.timeout_seconds == 30.0
    assert config.direct_timeout_seconds == 900.0


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


def test_upstream_capacity_429_retries_sibling_route_without_deferring():
    """An upstream capacity 429 retries an available sibling route rather than deferring,
    marking the exhausted route in cooldown."""

    class CapacityLimited(Exception):
        status_code = 429
        headers = {"retry-after": "15"}

    call_count = 0

    def completion(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise CapacityLimited("upstream provider overloaded, please try again later")
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
    policy = LLMRequestPolicy(allowed_models=("gemini/gemini-3-flash-preview",))
    result = backend.run_inference(job(content="meeting text", llm_policy=policy))

    assert isinstance(result, JobResult)
    assert result.output["choices"][0]["message"]["content"] == "ok"
    assert call_count == 2

    budget, _ = load_llm_budget_cas(storage)
    ledger1 = budget.routes["gemini_3_flash_preview_primary"]
    assert ledger1.inflight == {}
    assert ledger1.requests_minute == 0
    assert ledger1.blocked_until != ""

    ledger2 = budget.routes["gemini_3_flash_preview_secondary"]
    assert ledger2.inflight == {}
    assert ledger2.requests_minute == 1
    assert ledger2.blocked_until == ""


def test_upstream_capacity_429_retries_across_models_when_all_sibling_routes_capacity_fail():
    """When both primary and secondary routes for model A hit capacity, the direct loop
    retries an eligible fallback model B."""

    class CapacityLimited(Exception):
        status_code = 429
        headers = {"retry-after": "15"}

    attempted_models: list[str] = []

    def completion(**kwargs):
        model = kwargs.get("model")
        attempted_models.append(model)
        if model == "gemini/gemini-3-flash-preview":
            raise CapacityLimited("upstream provider overloaded, please try again later")
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
    policy = LLMRequestPolicy(
        allowed_models=(
            "gemini/gemini-3-flash-preview",
            "gemini/gemini-3.5-flash",
        )
    )
    result = backend.run_inference(job(content="meeting text", llm_policy=policy))

    assert isinstance(result, JobResult)
    assert result.output["choices"][0]["message"]["content"] == "ok"
    assert attempted_models == [
        "gemini/gemini-3-flash-preview",
        "gemini/gemini-3-flash-preview",
        "gemini/gemini-3.5-flash",
    ]

    budget, _ = load_llm_budget_cas(storage)
    assert budget.routes["gemini_3_flash_preview_primary"].blocked_until != ""
    assert budget.routes["gemini_3_flash_preview_secondary"].blocked_until != ""
    assert budget.routes["gemini_3_5_flash_primary"].requests_minute == 1


def test_own_rpd_429_blocks_until_next_local_midnight():
    """An own_rpd 429 blocks the route until the provider's next zoned midnight."""

    class RateLimitedRPD(Exception):
        status_code = 429
        headers = {"retry-after": "60"}

    def completion(**kwargs):
        raise RateLimitedRPD(
            "Resource has been exhausted (e.g. check quota): "
            "GenerateRequestsPerDayPerProjectPerRegion"
        )

    storage = MemStorage()
    backend = LiteLLMBackend(
        LLMBackendConfig(model="gemini/gemini-3-flash-preview"),
        completion=completion,
        storage=storage,
    )
    now = datetime.now(UTC)
    result = backend.run_inference(
        job(
            content="meeting text",
            llm_policy=LLMRequestPolicy(allowed_models=("gemini/gemini-3-flash-preview",)),
        )
    )

    assert isinstance(result, JobHandle)
    budget, _ = load_llm_budget_cas(storage)
    ledger = _ledger_for(budget, "gemini/gemini-3-flash-preview")
    assert ledger.blocked_until != ""
    blocked_until = datetime.fromisoformat(ledger.blocked_until)
    assert blocked_until > now + timedelta(minutes=5)


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
            model="mistral/codestral-2508",
            mode="dispatch",
            dispatch_v2_url="https://dispatch.example",
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
    stripped = strip_schema_keys(schema, frozenset({"minLength", "maximum"}))
    assert stripped == {
        "type": "object",
        "properties": {
            "a": {"type": "string"},
            "b": {"type": "array", "items": {"type": "integer"}},
        },
    }
    assert schema["properties"]["a"]["minLength"] == 1, "must not mutate the caller's schema"


def test_deepseek_invalid_reply_fails_after_one_corrective_retry():
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


def test_rejects_gpu_and_unknown_routes():
    with pytest.raises(ValueError):
        LiteLLMBackend(LLMBackendConfig(model="openai/gpt-4o"))
    backend = LiteLLMBackend(LLMBackendConfig(), completion=lambda **_: {})
    with pytest.raises(ValueError):
        backend.run_inference(InferenceJob(task="transcribe", inputs={}))


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
            dispatch_v2_url="https://dispatch.example",
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
    backend, _ = _recording_backend("google/gemma-4-31b-it")

    _, headers = backend._resolve_api_base_and_headers(route, direct=True)

    assert headers == {
        "cf-aig-authorization": "Bearer test-auth-token",
        "cf-aig-max-attempts": "1",
    }


# How each custom provider is registered on the Cloudflare side, and therefore what
# `ai_gateway_chat_path` has to be. This table exists because the Cloudflare-side Base URL is not
# represented in this repo, and the gateway's undocumented join changed on 2026-09-15: it now
# honors the registered path instead of rewriting its last segment to `v1`.
# Because the Cloudflare-side Base URL is not represented in this repo, the mapping cannot be
# derived -- so it is written down here, and a new custom provider trips the completeness check
# below until someone records how it is registered.
CUSTOM_PROVIDER_GATEWAY_PATHS = {
    # Registered at api_base verbatim; the `/v1` in each Base URL is preserved by the current
    # gateway join, so the caller path stays root-relative.
    "siliconflow": "/chat/completions",
    "sambanova": "/chat/completions",
    "nvidia": "/chat/completions",
    "airforce": "/chat/completions",
    "orcarouter": "/chat/completions",
    # Registered as `https://api.kilo.ai/api/gateway/v1` -- Kilo serves that path too, so the
    # caller path stays bare under either gateway join behavior.
    "kilo": "/chat/completions",
    # custom-zai is registered at `https://api.z.ai/api/paas`, which serves at
    # `/v4/chat/completions`.
    "zai": "/v4/chat/completions",
}


def test_every_custom_provider_records_how_it_is_registered():
    """A new custom provider must state its gateway path, since the rule cannot be inferred."""
    configured = {
        route.provider
        for route in ROUTE_REGISTRY.values()
        if (route.ai_gateway_slug or "").startswith("custom-")
    }
    assert configured <= set(CUSTOM_PROVIDER_GATEWAY_PATHS), (
        "an active custom provider is missing its Cloudflare-side registration in "
        "CUSTOM_PROVIDER_GATEWAY_PATHS (and see workers/llm-provider-shim/README.md for the "
        "gateway join compatibility contract)"
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


# Only single-provider models belong here. A logical model served by several providers -- such as
# `deepseek/deepseek-v4-flash`, which spans OrcaRouter and custom-nvidia -- has
# no fixed gateway slug: the scheduler picks whichever physical route has
# capacity, so pinning one slug end-to-end would assert on scheduler choice rather than on URL
# construction. The catalog test above covers those routes directly.
@pytest.mark.parametrize(
    ("model", "expected_request_url"),
    [
        (
            "gemini/gemini-3.6-flash",
            f"{_GW}/google-ai-studio/v1beta/models/gemini-3.6-flash:generateContent",
        ),
        # Mistral's only configured model (Codestral) is now also served by Airforce, so a
        # single-provider Groq model stands in for the plain OpenAI-compatible case.
        ("qwen/qwen3.8-27b", f"{_GW}/groq/chat/completions"),
        ("zai/glm-4.7-flash", f"{_GW}/custom-zai/v4/chat/completions"),
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
    for model in ("gemini/gemini-3.6-flash", "qwen/qwen3.8-27b", "zai/glm-4.7-flash"):
        slugs = {route.ai_gateway_slug or route.provider for route in ROUTE_CANDIDATES[model]}
        assert len(slugs) == 1, f"{model} now spans {slugs}; move it to the catalog-level test"


def test_direct_call_uses_ai_gateway_base_url_override(monkeypatch):
    monkeypatch.delenv("LLM_AI_GATEWAY", raising=False)
    monkeypatch.setenv("AI_GATEWAY_BASE_URL", "https://custom-gw.example.com/v1/custom-gw")
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("AI_GATEWAY_AUTH_TOKEN", raising=False)
    backend, calls = _recording_backend("mistral/codestral-2508")

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


def test_dispatch_v2_stats_requests_diagnostics_only_when_explicitly_requested():
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
        ),
        http_session=Session(),
    )

    backend.dispatch_v2_stats(detail=True)

    assert calls[0][0] == "https://dispatch.example/v2/stats?detail=1&limit=20"


def test_backend_rejects_cleartext_dispatch_urls_before_any_request():
    calls = []

    class Session:
        def get(self, *args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("an insecure dispatch URL must not be requested")

    with pytest.raises(ValueError, match="must be an HTTPS URL"):
        LiteLLMBackend(
            LLMBackendConfig(
                model="gemini/gemini-3.6-flash",
                dispatch_v2_url="http://dispatch.example",
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
            dispatch_v2_url="https://dispatch.example",
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


def test_structured_content_distinguishes_upstream_passthrough_from_malformed_reply():
    """A stored "completed" result that is actually a provider/gateway error object -- Airforce's
    HTTP 200 body containing {"error": {"message": "the provider refused this request (HTTP
    524)", ...}} when its own upstream times out (confirmed live 2026-09, 413 of 6,561 stored v2
    results during an outage) -- must raise LLMUpstreamPassthroughError, not
    LLMStructuredOutputError. The two are NOT interchangeable: llm_deferred_sweep.py's
    recover_terminal routes LLMStructuredOutputError into a "fix your JSON" corrective retry,
    which is nonsensical when the model never produced output, and burns one of the bounded
    MAX_TERMINAL_FAILURE_RETRIES attempts on a retry that cannot possibly succeed."""
    airforce_524 = {
        "error": {
            "message": "the provider refused this request (HTTP 524)",
            "type": "upstream_error",
            "code": "524",
        }
    }
    with pytest.raises(LLMUpstreamPassthroughError, match="error passthrough"):
        LiteLLMBackend._structured_content(airforce_524)

    airforce_429 = {
        "error": {
            "message": "the provider is at capacity right now — try again shortly",
            "type": "upstream_error",
            "code": "429",
        }
    }
    with pytest.raises(LLMUpstreamPassthroughError, match="error passthrough"):
        LiteLLMBackend._structured_content(airforce_429)

    # A genuinely malformed reply (no error key, no usable content) keeps its original class --
    # this class of failure legitimately can benefit from the corrective retry.
    with pytest.raises(LLMStructuredOutputError, match="did not contain message content"):
        LiteLLMBackend._structured_content({"choices": []})
    with pytest.raises(LLMStructuredOutputError, match="did not contain message content"):
        LiteLLMBackend._structured_content({})

    # LLMUpstreamPassthroughError IS a LLMDispatchTerminalError (so every existing
    # isinstance(result, (LLMStructuredOutputError, LLMDispatchTerminalError)) gate in
    # scripts/llm_deferred_sweep.py still matches it) but is NOT a LLMStructuredOutputError (so
    # recover_terminal's isinstance(exc, LLMStructuredOutputError) branch, which triggers the
    # schema-correction retry, correctly skips it).
    assert issubclass(LLMUpstreamPassthroughError, LLMDispatchTerminalError)
    assert not issubclass(LLMUpstreamPassthroughError, LLMStructuredOutputError)


def test_completed_dispatch_result_catches_upstream_passthrough_for_an_unstructured_job():
    """_validate_reconciled returns immediately when structured_output is unset, so it never
    calls _structured_content at all for an ordinary (non-structured) job -- an error passthrough
    for one of those used to fall straight through _completed_dispatch_result to a JobResult built
    directly from the raw {"error": {...}} body, persisted and acknowledged as a genuine
    successful completion (CodeRabbit, 2026-09-13: the one call site ff936d4 missed)."""
    backend = LiteLLMBackend(LLMBackendConfig(model="gemini/gemini-3-flash-preview"))
    airforce_524 = {"error": {"message": "the provider refused this request (HTTP 524)"}}
    with pytest.raises(LLMUpstreamPassthroughError, match="error passthrough"):
        backend._completed_dispatch_result(
            task="chapter-locator",
            recipe_hash="r1",
            output=airforce_524,
            structured_output=None,
        )


def test_validate_reconciled_propagates_upstream_passthrough_uncaught():
    """_validate_reconciled's except clause only catches (ValueError, TypeError) from Pydantic
    validation -- LLMUpstreamPassthroughError (a RuntimeError subclass) must pass through
    unchanged, not get rewrapped as a generic LLMStructuredOutputError."""
    backend = LiteLLMBackend(LLMBackendConfig(model="gemini/gemini-3-flash-preview"))
    airforce_524 = {"error": {"message": "the provider refused this request (HTTP 524)"}}
    with pytest.raises(LLMUpstreamPassthroughError):
        backend._validate_reconciled(airforce_524, "test-output")


@pytest.mark.parametrize("raw", ["0", "-5", "nan", "inf"])
def test_the_direct_timeout_must_be_a_finite_positive_number(monkeypatch, raw):
    monkeypatch.setenv("LLM_DIRECT_TIMEOUT_SECONDS", raw)
    with pytest.raises(ValueError, match="LLM_DIRECT_TIMEOUT_SECONDS"):
        LLMBackendConfig.from_env()


def test_a_blank_direct_timeout_keeps_the_default(monkeypatch):
    monkeypatch.setenv("LLM_DIRECT_TIMEOUT_SECONDS", " ")
    assert LLMBackendConfig.from_env().direct_timeout_seconds == 720.0


def test_a_rebuilt_deferred_job_keeps_its_lane_timeout_and_output_mode(monkeypatch):
    # The lane (policy.purpose) picks per-lane reasoning controls on the direct path; a rebuild
    # without it would send the provider's default for that model.
    from citypods.compute.llm_policy import DeferredLLMRequest

    storage = MemStorage()
    backend = LiteLLMBackend(LLMBackendConfig(model="gemini/gemini-3.5-flash"), storage=storage)
    seen = {}

    def fake_paced(job, policy, structured, messages):
        seen["inputs"] = dict(job.inputs)
        return JobResult(task=job.task, recipe_hash=job.recipe_hash, output={}, model="m")

    monkeypatch.setattr(backend, "_run_policy_job_paced", fake_paced)
    policy = LLMRequestPolicy(purpose="chapter-agenda")
    backend._reconcile_deferred(
        JobHandle(
            task="tag",
            recipe_hash="r-rebuild",
            backend="litellm",
            ref="deferred:r-rebuild",
            deferred_request=DeferredLLMRequest(
                messages=({"role": "user", "content": "hi"},),
                policy=policy,
                output_token_budget=16_384,
                timeout=45.0,
                max_tokens_mode="route_max",
            ),
        )
    )
    assert seen["inputs"]["llm_policy"] is policy
    assert seen["inputs"]["max_tokens"] == 16_384
    assert seen["inputs"]["timeout"] == 45.0
    assert seen["inputs"]["max_tokens_mode"] == "route_max"
