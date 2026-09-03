from datetime import UTC, datetime

from citypods.compute.llm import SUPPORTED_MODELS
from citypods.compute.llm_policy import (
    DEFAULT_OUTPUT_TOKEN_MARGIN,
    MODEL_ALIASES,
    ROUTE_CANDIDATES,
    ROUTES,
    LLMRequestPolicy,
    LLMRoute,
    PeakWindow,
    PricingPolicy,
    QuotaPolicy,
    canonical_model,
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
    assert route.direct_model == route.model
    assert estimate_tokens([{"role": "user", "content": "12345"}]) == 2
    assert DEFAULT_OUTPUT_TOKEN_MARGIN == 1024


def test_generated_catalog_deduplicates_logical_models_across_direct_routes():
    # groq_llama_3_3_70b_versatile_primary removed 2026-08-26 (config/provider_limits.yml):
    # Groq stopped serving llama-3.3-70b-versatile; NVIDIA direct route added as third provider.
    candidates = ROUTE_CANDIDATES["meta-llama/llama-3.3-70b-instruct"]
    assert {candidate.provider for candidate in candidates} == {"sambanova", "openrouter", "nvidia"}
    assert all(set(candidate.transports) == {"direct", "llm-dispatch"} for candidate in candidates)
    assert all(candidate.route_id and candidate.direct_model for candidate in candidates)


def test_generated_catalog_unifies_deepseek_and_nemotron_provider_aliases():
    # NVIDIA build's leg for this model (added 2026-08-29) was briefly commented out the same day
    # on a misdiagnosis -- the 404s were NOT NVIDIA-side model gating but a dropped `/v1` in the
    # Cloudflare AI Gateway custom-provider path (see config/provider_limits.yml's `nvidia` block).
    # Restored once the path fix was verified end-to-end against the live gateway.
    deepseek = ROUTE_CANDIDATES["deepseek/deepseek-v4-flash"]
    assert {candidate.provider for candidate in deepseek} == {
        "deepseek",
        "siliconflow",
        "opencode",
        "nvidia",
    }
    assert canonical_model("opencode/deepseek-v4-flash-free") == "deepseek/deepseek-v4-flash"
    assert MODEL_ALIASES["deepseek/deepseek-v4-flash-0731"] == "deepseek/deepseek-v4-flash"

    # NVIDIA build's direct Nemotron 3 Ultra leg (added 2026-08-29) bypasses the OpenRouter/Kilo/
    # OpenCode broker legs -- see nvidia_nemotron_3_ultra_550b_a55b_free.
    nemotron = ROUTE_CANDIDATES["nvidia/nemotron-3-ultra-550b-a55b:free"]
    assert {candidate.provider for candidate in nemotron} == {
        "openrouter",
        "kilo",
        "opencode",
        "nvidia",
    }
    assert (
        canonical_model("opencode/nemotron-3-ultra-free")
        == "nvidia/nemotron-3-ultra-550b-a55b:free"
    )


def test_deepseek_pricing_selects_the_effective_period_and_peak_windows():
    route = next(
        candidate
        for candidate in ROUTE_CANDIDATES["deepseek/deepseek-v4-flash"]
        if candidate.provider == "deepseek"
    )
    before = route.pricing.rates_at(datetime(2026, 8, 16, 15, 59, tzinfo=UTC))
    after = route.pricing.rates_at(datetime(2026, 8, 16, 16, 0, tzinfo=UTC))
    assert before[:2] == (0.14e-6, 0.28e-6)
    assert after[:2] == (0.22e-6, 0.66e-6)
    assert [(window.start.isoformat(), window.end.isoformat()) for window in after[2]] == [
        ("01:00:00", "04:00:00"),
        ("06:00:00", "10:00:00"),
    ]
