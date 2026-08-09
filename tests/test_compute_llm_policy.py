from datetime import UTC, datetime

from citypods.compute.llm import SUPPORTED_MODELS
from citypods.compute.llm_policy import (
    DEFAULT_OUTPUT_TOKEN_MARGIN,
    ROUTE_CANDIDATES,
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


def test_generated_catalog_deduplicates_logical_models_across_direct_routes():
    candidates = ROUTE_CANDIDATES["meta-llama/llama-3.3-70b-instruct"]
    assert {candidate.provider for candidate in candidates} == {"groq", "sambanova", "openrouter"}
    assert all(set(candidate.transports) == {"direct", "llm-dispatch"} for candidate in candidates)
    assert all(candidate.route_id and candidate.direct_model for candidate in candidates)
