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
    _load_generated_catalog,
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
    # One logical model, five physical routes across four providers (the llama-3.3 example this
    # test used was removed with its last, paused route on 2026-09-24).
    candidates = ROUTE_CANDIDATES["google/gemma-4-31b-it"]
    assert {candidate.provider for candidate in candidates} == {
        "gemini",
        "sambanova",
        "openrouter",
        "nvidia",
    }
    assert len({candidate.route_id for candidate in candidates}) == len(candidates)
    assert all(set(candidate.transports) == {"direct", "llm-dispatch"} for candidate in candidates)
    assert all(candidate.route_id and candidate.direct_model for candidate in candidates)


def test_generated_catalog_unifies_deepseek_and_nemotron_provider_aliases():
    # NVIDIA build's leg for this model (added 2026-08-29) was briefly commented out the same day
    # on a misdiagnosis -- the 404s were NOT NVIDIA-side model gating but a custom-provider path
    # mismatch in Cloudflare AI Gateway (see config/provider_limits.yml's `nvidia` block). Restored
    # once the path fix was verified end-to-end against the live gateway. Paid routes are absent.
    # One pool name per DeepSeek version (2026-09-24): v4 is OrcaRouter only, v4.1 NVIDIA only,
    # and the old `v4-pro` alias pool is retired.
    deepseek = ROUTE_CANDIDATES["deepseek/deepseek-v4-flash"]
    assert {candidate.provider for candidate in deepseek} == {"orcarouter"}
    assert canonical_model("orcarouter/deepseek-v4-flash") == "deepseek/deepseek-v4-flash"
    assert MODEL_ALIASES["nvidia/deepseek-v4.1-flash"] == "deepseek/deepseek-v4.1-flash"
    assert "deepseek/deepseek-v4-pro" not in ROUTE_CANDIDATES
    assert [route.route_id for route in ROUTE_CANDIDATES["deepseek/deepseek-v4.1-flash"]] == [
        "nvidia_deepseek_v4_1_flash_free"
    ]

    # NVIDIA build's direct Nemotron 3 Ultra leg (added 2026-08-29) bypasses the OpenRouter/Kilo
    # broker legs -- see nvidia_nemotron_3_ultra_550b_a55b_free.
    nemotron = ROUTE_CANDIDATES["nvidia/nemotron-3-ultra-550b-a55b:free"]
    assert {candidate.provider for candidate in nemotron} == {
        "openrouter",
        "kilo",
        "nvidia",
    }


def test_generated_deepseek_routes_are_all_free_after_paid_catalog_removal():
    assert all(route.free for route in ROUTE_CANDIDATES["deepseek/deepseek-v4-flash"])


def test_generated_catalog_includes_observed_characterization_fields() -> None:
    routes, _, _ = _load_generated_catalog()
    groq = next((r for r in routes if r.route_id == "groq_gpt_oss_120b_primary"), None)
    assert groq is not None
    assert groq.observed_burst == 30
    # The 1,000 recorded on 2026-09-09 was the ceiling search's own floor, not a measurement --
    # removed. Re-measured 2026-09-12 by a throttle-tolerant 3h endurance probe (contention
    # confirmed absent): the route conclusively accepts up to 7,125 estimated input tokens,
    # consistent with Groq's own quoted 8,000 TPM budget once output tokens are accounted for.
    # Written explicitly here (not auto-promoted -- review/45 §20.8 still requires a
    # human-reviewed promotion for every route; this one was).
    assert groq.observed_on == "2026-09-12"
    assert groq.observed_input_ceiling == 7125
    assert groq.hard_input_ceiling == 7125

    codestral = next((r for r in routes if r.route_id == "mistral_codestral_2508_primary"), None)
    assert codestral is not None
    assert codestral.observed_on == "2026-09-12"
    assert codestral.observed_burst == 40
    assert codestral.observed_input_ceiling == 249027
