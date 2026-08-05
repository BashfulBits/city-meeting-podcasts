from datetime import UTC, datetime

from citypods.compute.llm import SUPPORTED_MODELS
from citypods.compute.llm_policy import (
    DEFAULT_OUTPUT_TOKEN_MARGIN,
    ROUTES,
    LLMRequestPolicy,
    LLMRoute,
    PeakWindow,
    PricingPolicy,
    QuotaPolicy,
    estimate_tokens,
)


def test_route_table_matches_litellm_supported_models():
    assert set(ROUTES) == set(SUPPORTED_MODELS)


def test_policy_and_route_dataclasses_and_token_estimate():
    policy = LLMRequestPolicy(
        allowed_models=("deepseek/deepseek-v4-pro",),
        allow_paid=True,
        deadline_at=datetime(2026, 7, 16, 12, tzinfo=UTC),
        purpose="evaluation",
    )
    window = PeakWindow("UTC", start=datetime.min.time(), end=datetime.max.time(), multiplier=0.5)
    route = LLMRoute(
        model="example/model",
        transport="direct",
        free=False,
        quota=QuotaPolicy(rpm=1),
        pricing=PricingPolicy(windows=(window,)),
    )
    assert policy.allow_paid is True
    assert route.quota.rpm == 1
    assert estimate_tokens([{"role": "user", "content": "12345"}]) == 2
    assert DEFAULT_OUTPUT_TOKEN_MARGIN == 1024


def test_gemini_full_flash_locator_routes_are_experimental_and_capped():
    for model in (
        "gemini/gemini-3.5-flash",
        "gemini/gemini-3.6-flash",
    ):
        route = ROUTES[model]
        assert route.experimental is True
        assert route.quota.rpd == 20
        assert route.quota.tpm == 1_000_000


def test_zai_flash_locator_route_is_experimental_and_serial():
    route = ROUTES["zai/glm-4.7-flash"]
    assert route.experimental is True
    assert route.free is True
    assert route.quota.concurrency == 1


def test_openrouter_qwen_flash_route_is_experimental_and_priced():
    route = ROUTES["openrouter/qwen/qwen3.7-flash"]
    assert route.experimental is True
    assert route.free is False
    assert route.quota.concurrency == 1
    assert route.pricing.input_per_token == 0.03e-6
