from __future__ import annotations

from pathlib import Path

import pytest
import yaml as pyyaml

from scripts import compile_llm_limits

REPO_ROOT = Path(__file__).resolve().parent.parent
PROVIDER_LIMITS_YAML = REPO_ROOT / "config" / "provider_limits.yml"

STRUCTURED_OUTPUT_METHODS = {
    "json_schema": {"response_format": "json_schema", "include_schema_in_prompt": False},
    "json_schema_relaxed": {
        "response_format": "json_schema",
        "include_schema_in_prompt": False,
        "strip_schema_keys": ["minLength"],
    },
    "json_object": {"response_format": "json_object", "include_schema_in_prompt": True},
    "prompt_only": {"response_format": "none", "include_schema_in_prompt": True},
}


def _raw_provider_limits() -> dict:
    return pyyaml.safe_load(PROVIDER_LIMITS_YAML.read_text(encoding="utf-8"))


def test_default_compile_never_touches_the_network(monkeypatch):
    """The deploy workflow calls compile_limits() with no discovery flag -- it must be pure
    YAML-to-JSON, no network call, or the deployed artifact stops being reproducible (review/41)."""

    def _boom(*_args, **_kwargs):
        raise AssertionError("compile_limits() must not fetch anything by default")

    monkeypatch.setattr(compile_llm_limits, "urlopen", _boom)
    compiled = compile_llm_limits.compile_limits()
    assert compiled["_metadata"]["routes_count"] > 0
    assert "gemini/gemini-3-flash-preview" in compiled["model_routes_map"]


def test_worker_catalog_omits_duplicate_and_non_worker_route_data():
    compiled = compile_llm_limits.compile_limits()
    worker = compile_llm_limits._worker_catalog(compiled)

    assert set(worker) == {
        "_metadata",
        "providers",
        "routes_by_id",
        "model_routes_map",
        "model_aliases",
        "model_routing",
    }
    assert len(worker["routes_by_id"]) == len(compiled["routes"])
    assert "routes" not in worker
    assert "structured_output_methods" not in worker
    assert worker["model_aliases"]["nvidia/deepseek-v4.1-flash"] == "deepseek/deepseek-v4.1-flash"
    # One pool name per DeepSeek version (2026-09-24); the old `v4-pro` alias pool is retired.
    assert "deepseek/deepseek-v4-pro" not in worker["model_aliases"]
    assert "deepseek/deepseek-v4-pro" not in worker["model_routes_map"]
    assert worker["routes_by_id"]["nvidia_deepseek_v4_1_flash_free"]["model"] == (
        "deepseek/deepseek-v4.1-flash"
    )
    assert worker["model_aliases"]["orcarouter/deepseek-v4-flash"] == "deepseek/deepseek-v4-flash"
    gemma = worker["routes_by_id"]["gemma_4_31b_primary"]
    assert isinstance(gemma, dict)
    assert set(gemma) == set(compile_llm_limits._WORKER_ROUTE_FIELDS)
    assert gemma["route_id"] == "gemma_4_31b_primary"
    samba_gemma = worker["routes_by_id"]["sambanova_gemma_4_31b_it_primary"]
    assert samba_gemma["rpd"] == 20
    assert "sambanova_gemma_4_31b_it_primary" in worker["model_routes_map"]["google/gemma-4-31b-it"]
    assert worker["providers"]["sambanova"]["rpm"] == 20
    assert worker["providers"]["sambanova"]["ai_gateway_max_attempts"] == 1
    # Raised 2 -> 3 (2026-09-23) so Nemotron 3 Ultra always keeps an NVIDIA slot.
    assert worker["providers"]["nvidia"]["concurrency"] == 3
    assert gemma["request_start_margin_seconds"] is None
    # model_routes_map holds route-ID strings that key directly into routes_by_id -- not the
    # integer positions an earlier revision used, which could silently misresolve to a different
    # route if compile-time route order ever shifted.
    for route_id, route in worker["routes_by_id"].items():
        assert set(route) == set(compile_llm_limits._WORKER_ROUTE_FIELDS), route_id
        assert route["route_id"] == route_id
    assert worker["model_routes_map"]
    for model, route_ids in worker["model_routes_map"].items():
        assert route_ids, model
        for route_id in route_ids:
            assert isinstance(route_id, str)
            assert route_id in worker["routes_by_id"], (model, route_id)
    assert "discovery" not in worker["providers"]["openrouter"]


def test_model_keys_pool_equivalent_provider_routes_and_preserve_aliases():
    compiled = compile_llm_limits.compile_limits()

    # One pool name per DeepSeek version (2026-09-24): v4 is OrcaRouter only and v4.1 is NVIDIA
    # only, so a lane or tournament contestant always knows which model answers. Lanes that want
    # both list both names.
    deepseek_key = "deepseek/deepseek-v4-flash"
    assert compiled["model_routes_map"][deepseek_key] == ["orcarouter_deepseek_v4_flash_free"]
    assert compiled["model_routes_map"]["deepseek/deepseek-v4.1-flash"] == [
        "nvidia_deepseek_v4_1_flash_free"
    ]
    assert "deepseek/deepseek-v4-pro" not in compiled["model_routes_map"]
    assert "deepseek/deepseek-v4-pro" not in compiled["model_aliases"]
    assert compiled["model_aliases"]["orcarouter/deepseek-v4-flash"] == deepseek_key

    nemotron_key = "nvidia/nemotron-3-ultra-550b-a55b:free"
    # OpenRouter + Kilo (broker legs) + NVIDIA build direct (added 2026-08-29).
    assert len(compiled["model_routes_map"][nemotron_key]) == 3
    assert (
        compiled["model_aliases"]["openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"]
        == nemotron_key
    )
    assert compiled["model_aliases"]["nvidia/nemotron-3-ultra-550b-a55b"] == nemotron_key

    nemotron_super_key = "openrouter/nvidia/nemotron-3-super-120b-a12b:free"
    # OpenRouter's own canonical name for this model family, now also reachable via NVIDIA build
    # direct (added 2026-08-29) -- unlike Ultra, Super had no shared model_key before this change.
    assert len(compiled["model_routes_map"][nemotron_super_key]) == 2
    assert compiled["model_aliases"]["nvidia/nemotron-3-super-120b-a12b"] == nemotron_super_key

    codestral_key = "mistral/codestral-2508"
    codestral_routes = compiled["model_routes_map"][codestral_key]
    # primary + secondary Mistral accounts + airforce codestral-latest route (the tertiary
    # account was removed 2026-09-24: its key was never set on the dispatch Workers).
    assert len(codestral_routes) == 3
    assert {compiled["routes_by_id"][route_id]["provider"] for route_id in codestral_routes} == {
        "mistral",
        "airforce",
    }
    assert compiled["model_aliases"]["mistral/codestral-latest"] == codestral_key
    worker = compile_llm_limits._worker_catalog(compiled)
    assert worker["model_aliases"]["codestral-latest"] == codestral_key
    assert worker["model_aliases"]["airforce/codestral-latest"] == codestral_key


def test_compiled_routes_materialize_route_specific_input_and_output_limits():
    compiled = compile_llm_limits.compile_limits()
    gemini = compiled["routes_by_id"]["gemini_3_1_flash_lite_primary"]
    gemma = compiled["routes_by_id"]["gemma_4_31b_primary"]
    openrouter_gemma = compiled["routes_by_id"]["openrouter_google_gemma_4_26b_a4b_it_free"]
    assert (gemini["input_context_limit"], gemini["output_context_limit"]) == (1048576, 65536)
    assert (gemma["input_context_limit"], gemma["output_context_limit"]) == (262144, 32768)
    assert (openrouter_gemma["input_context_limit"], openrouter_gemma["output_context_limit"]) == (
        131072,
        32768,
    )
    assert all(
        isinstance(route["input_context_limit"], int)
        and isinstance(route["output_context_limit"], int)
        and route["input_context_limit"] > 0
        and route["output_context_limit"] > 0
        for route in compiled["routes"]
    )
    codestral = compiled["routes_by_id"]["mistral_codestral_2508_primary"]
    assert (codestral["input_context_limit"], codestral["output_context_limit"]) == (
        256000,
        256000,
    )


def test_route_limits_cannot_fall_back_to_provider_defaults():
    raw = {
        "structured_output_methods": STRUCTURED_OUTPUT_METHODS,
        "providers": {"example": {"input_context_limit": 999999}},
        "routes": [{"route_id": "example", "model": "example/model", "provider": "example"}],
    }
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(compile_llm_limits, "INPUT_YAML", compile_llm_limits.INPUT_YAML)
        monkeypatch.setattr(
            compile_llm_limits, "yaml", type("Yaml", (), {"safe_load": lambda *_: raw})
        )
        with pytest.raises(ValueError, match="provider defaults are not supported"):
            compile_llm_limits.compile_limits()


def test_compiled_routes_resolve_a_structured_output_method_per_route():
    # review/48 R10: a route's own verified method wins; otherwise its provider's method.
    compiled = compile_llm_limits.compile_limits()
    routes = compiled["routes_by_id"]
    gemma = routes["gemma_4_31b_primary"]
    v41 = routes["nvidia_deepseek_v4_1_flash_free"]
    nemotron = routes["nvidia_nemotron_3_ultra_550b_a55b_free"]

    assert (gemma["structured_output_method"], gemma["structured_output_method_source"]) == (
        "json_schema_relaxed",
        "provider",
    )
    assert "minLength" in gemma["structured_output_schema_strip_keys"]
    assert (v41["structured_output_method"], v41["structured_output_method_source"]) == (
        "prompt_only",
        "route",
    )
    assert v41["structured_output_verified_on"] == "2026-09-24"
    assert v41["structured_output_response_format"] == "none"
    assert v41["structured_output_include_schema_in_prompt"] is True
    # Same provider, different model: the v4.1 override is not a provider-wide change.
    assert nemotron["structured_output_method"] == "json_schema"
    worker_route = compile_llm_limits._worker_catalog(compiled)["routes_by_id"][
        "nvidia_deepseek_v4_1_flash_free"
    ]
    assert worker_route["structured_output_response_format"] == "none"
    assert worker_route["structured_output_include_schema_in_prompt"] is True


def _methods_raw(routes, providers=None):
    return {
        "structured_output_methods": STRUCTURED_OUTPUT_METHODS,
        "providers": providers
        or {
            "example": {
                "api_base": "https://example.com",
                "structured_output_method": "json_schema",
            }
        },
        "routes": routes,
    }


def _route(route_id, model, **extra):
    return {
        "route_id": route_id,
        "model": model,
        "provider": "example",
        "input_context_limit": 1000,
        "output_context_limit": 1000,
        **extra,
    }


def test_an_unverified_route_inherits_the_method_verified_for_its_model_before_its_provider():
    routes = [
        _route(
            "a",
            "m/one",
            upstream_model="one-a",
            structured_output_method="prompt_only",
            structured_output_verified_on="2026-09-24",
        ),
        _route("b", "m/one", upstream_model="one-b"),
        _route("c", "m/two", upstream_model="two"),
    ]
    resolved = compile_llm_limits._resolve_structured_output_methods(
        [dict(r) for r in routes], _methods_raw(routes)["providers"]
    )
    assert resolved["a"] == ("prompt_only", "route", "2026-09-24")
    assert resolved["b"] == ("prompt_only", "model", None)
    assert resolved["c"] == ("json_schema", "provider", None)


def test_a_provider_without_a_method_falls_back_to_prompt_only():
    resolved = compile_llm_limits._resolve_structured_output_methods(
        [_route("a", "m/one")], {"example": {}}
    )
    assert resolved["a"] == ("prompt_only", "default", None)


@pytest.mark.parametrize(
    ("route_extra", "providers", "match"),
    [
        ({"structured_output_method": "tool_call"}, None, "unknown structured_output_method"),
        ({"structured_output_verified_on": "2026-09-24"}, None, "verified_on without a method"),
        ({"structured_output_profile": "json_object"}, None, "retired key"),
        ({}, {"example": {"structured_output_method": "xml"}}, "unknown structured_output_method"),
    ],
)
def test_structured_output_method_config_errors_fail_the_compile(route_extra, providers, match):
    with pytest.raises(ValueError, match=match):
        compile_llm_limits._resolve_structured_output_methods(
            [_route("a", "m/one", **route_extra)],
            providers or {"example": {"structured_output_method": "json_schema"}},
        )


def test_the_method_table_must_be_exactly_the_closed_set():
    partial = dict(STRUCTURED_OUTPUT_METHODS)
    partial.pop("prompt_only")
    with pytest.raises(ValueError, match="must define exactly"):
        compile_llm_limits._normalize_structured_output_methods(partial)
    no_prompt = {**STRUCTURED_OUTPUT_METHODS, "prompt_only": {"response_format": "none"}}
    with pytest.raises(ValueError, match="must include the schema in the prompt"):
        compile_llm_limits._normalize_structured_output_methods(no_prompt)


def test_google_routes_use_live_model_identifiers():
    compiled = compile_llm_limits.compile_limits()
    google_models = {
        route["upstream_model"] for route in compiled["routes"] if route["provider"] == "gemini"
    }

    assert "gemma-4-26b-a4b-it" in google_models
    assert "gemma-4-26b-it" not in google_models
    assert "gemini-2.5-flash" not in google_models
    assert "gemini-2.5-flash-lite" not in google_models


def test_full_day_pricing_surcharge_is_rejected():
    with pytest.raises(ValueError, match="full-day pricing surcharge"):
        compile_llm_limits._validate_pricing_windows(
            {
                "route_id": "always-peak",
                "pricing": {
                    "windows": [{"tz": "UTC", "start": "00:00", "end": "00:00", "multiplier": 2}]
                },
            }
        )


def test_model_routing_compiles_from_the_committed_yaml_and_resolves_aliases():
    compiled = compile_llm_limits.compile_limits()
    assert compiled["model_routing"] == {
        "gemini/gemini-3.7-flash": [
            "gemini/gemini-3.6-flash",
            "gemini/gemini-3.8-flash",
            "gemini/gemini-3.5-flash",
        ]
    }
    assert compiled["model_routes_map"]["mistral/codestral-2508"] == [
        "mistral_codestral_2508_primary",
        "mistral_codestral_2508_secondary",
        "mistral_codestral_airforce_primary",
    ]
    worker = compile_llm_limits._worker_catalog(compiled)
    python_catalog = compile_llm_limits._python_routes(compiled)
    assert worker["model_routing"] == compiled["model_routing"]
    assert python_catalog["model_routing"] == compiled["model_routing"]


def test_validated_model_routing_resolves_legacy_aliases_and_dedups():
    routing = compile_llm_limits._validated_model_routing(
        {"legacy/source": ["legacy/target", "canonical/target"]},
        model_aliases={"legacy/source": "canonical/source", "legacy/target": "canonical/target"},
        canonical_models={"canonical/source", "canonical/target"},
    )
    assert routing == {"canonical/source": ["canonical/target"]}


def test_validated_model_routing_rejects_an_unknown_model():
    with pytest.raises(ValueError, match="unknown model"):
        compile_llm_limits._validated_model_routing(
            {"canonical/source": ["missing/target"]},
            model_aliases={},
            canonical_models={"canonical/source"},
        )
    with pytest.raises(ValueError, match="unknown model"):
        compile_llm_limits._validated_model_routing(
            {"missing/source": ["canonical/target"]},
            model_aliases={},
            canonical_models={"canonical/target"},
        )


def test_validated_model_routing_rejects_self_routing():
    with pytest.raises(ValueError, match="route a model to itself"):
        compile_llm_limits._validated_model_routing(
            {"canonical/model": ["canonical/model"]},
            model_aliases={},
            canonical_models={"canonical/model"},
        )


def test_validated_model_routing_rejects_a_non_mapping_top_level_value():
    for invalid in ([], "canonical/model", 1, True):
        with pytest.raises(ValueError, match="mapping of source models"):
            compile_llm_limits._validated_model_routing(
                invalid, model_aliases={}, canonical_models=set()
            )
    assert (
        compile_llm_limits._validated_model_routing(None, model_aliases={}, canonical_models=set())
        == {}
    )


def test_validated_model_routing_rejects_a_non_list_or_empty_target():
    with pytest.raises(ValueError, match="non-empty list"):
        compile_llm_limits._validated_model_routing(
            {"canonical/model": "canonical/other"},
            model_aliases={},
            canonical_models={"canonical/model", "canonical/other"},
        )
    with pytest.raises(ValueError, match="non-empty list"):
        compile_llm_limits._validated_model_routing(
            {"canonical/model": []},
            model_aliases={},
            canonical_models={"canonical/model"},
        )


def test_model_key_aliases_must_not_conflict_with_a_canonical_key():
    routes = [
        {
            "route_id": "alias",
            "model": "shared/model",
            "model_key": "other/model",
        },
        {
            "route_id": "canonical",
            "model": "shared/model",
        },
    ]
    with pytest.raises(ValueError, match="also a canonical model"):
        compile_llm_limits._validated_routes(routes)


def test_physical_aliases_with_conflicting_limits_are_rejected():
    routes = [
        {
            "route_id": "first",
            "model": "provider/model",
            "provider": "provider",
            "account_id": "primary",
            "upstream_model": "vendor/model",
            "rpm": 10,
        },
        {
            "route_id": "second",
            "model": "provider/model-alias",
            "model_key": "provider/model",
            "provider": "provider",
            "account_id": "primary",
            "upstream_model": "vendor/model",
            "rpm": 20,
        },
    ]
    with pytest.raises(ValueError, match="conflicting limits"):
        compile_llm_limits._validated_routes(routes)


def test_python_catalog_rejects_an_unknown_route_account():
    compiled = {
        "_metadata": {},
        "providers": {"example": {"accounts": [{"id": "real", "api_key_env": "REAL_KEY"}]}},
        "routes": [
            {
                "route_id": "example_route",
                "model": "example/model",
                "provider": "example",
                "upstream_model": "model",
                "account_id": "missing",
            }
        ],
    }
    with pytest.raises(ValueError, match="unknown account_id 'missing'"):
        compile_llm_limits._python_routes(compiled)


def test_openai_compatible_provider_selectors_use_litellms_openai_adapter():
    for provider in ("airforce", "kilo", "opencode", "siliconflow", "orcarouter"):
        assert compile_llm_limits._direct_model(provider, "vendor/model") == "openai/vendor/model"


def test_openrouter_transform_dedups_within_the_same_discovery_pass():
    """Two discovered model IDs that normalize to the same route_id must not both be appended --
    the CodeRabbit-flagged bug: `existing_route_ids` was never updated inside the loop."""
    discovered = [
        {"id": "a/b:free", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "a/b_free", "pricing": {"prompt": "0", "completion": "0"}},
    ]
    new_routes = compile_llm_limits._openrouter_routes(discovered, existing_route_ids=set())
    assert len(new_routes) == 1
    assert new_routes[0]["route_id"] == "openrouter_a_b_free"


def test_openrouter_transform_skips_already_existing_route_ids():
    discovered = [{"id": "a/b", "pricing": {"prompt": "0.001", "completion": "0.002"}}]
    new_routes = compile_llm_limits._openrouter_routes(
        discovered, existing_route_ids={"openrouter_a_b"}
    )
    assert new_routes == []


def test_validated_routes_reports_the_offending_index_not_a_bare_keyerror():
    routes = [
        {"route_id": "ok_route", "model": "gemini/gemini-3-flash-preview"},
        {"model": "missing-route-id"},
    ]
    with pytest.raises(ValueError, match=r"route #1 is missing"):
        compile_llm_limits._validated_routes(routes)


def test_validated_routes_rejects_a_duplicate_hand_authored_route_id():
    routes = [
        {"route_id": "dup", "model": "gemini/gemini-3-flash-preview"},
        {"route_id": "dup", "model": "mistral/codestral-2508"},
    ]
    with pytest.raises(ValueError, match=r"route #1 redeclares route_id 'dup'"):
        compile_llm_limits._validated_routes(routes)


def test_fetch_openrouter_models_rejects_a_non_https_discovery_endpoint():
    with pytest.raises(ValueError, match="https://"):
        compile_llm_limits.fetch_openrouter_models(
            {"discovery": {"endpoint": "http://openrouter.ai/api/v1/models"}}
        )


def test_run_discovery_rejects_a_provider_with_no_discovery_endpoint():
    raw = {"providers": {"mistral": {"api_base": "https://api.mistral.ai"}}, "routes": []}
    with pytest.raises(ValueError, match="mistral"):
        compile_llm_limits.run_discovery(raw, ["mistral"])


def test_run_discovery_bare_flag_covers_only_providers_with_a_discovery_block(monkeypatch):
    calls = []

    def fake_fetch(provider_cfg):
        calls.append(provider_cfg)
        return []

    monkeypatch.setitem(compile_llm_limits.DISCOVERY_FETCHERS, "openrouter", fake_fetch)
    raw = {
        "providers": {
            "mistral": {"api_base": "https://api.mistral.ai"},
            "openrouter": {"discovery": {"endpoint": "https://openrouter.ai/api/v1/models"}},
        },
        "routes": [],
    }
    changed = compile_llm_limits.run_discovery(raw, [])
    assert changed is False  # fake_fetch returns no models
    assert len(calls) == 1  # only the provider with a discovery block was touched


def test_token_estimate_buffer_scales_route_and_provider_token_budgets():
    compiled = compile_llm_limits.compile_limits()
    assert compiled["_metadata"]["token_estimate_buffer"] == 0.9
    assert compiled["_metadata"]["split_cap_multiplier"] == 1.0

    # Route TPM scaling with 0.90 buffer and 1.0 split-cap:
    # 250,000 * 0.9 = 225,000; 16,000 * 0.9 = 14,400
    gemini = compiled["routes_by_id"]["gemini_3_5_flash_lite_primary"]
    gemma = compiled["routes_by_id"]["gemma_4_31b_primary"]
    assert gemini["tpm"] == 225_000
    assert gemma["tpm"] == 14_400

    # RPM / RPD unscaled with split-cap (1.0)
    assert gemini["rpm"] == 15
    assert gemini["rpd"] == 500
    assert gemini["input_context_limit"] == 1048576
    assert gemini["output_context_limit"] == 65536

    # Provider monthly_tpm scaling. The 2026-08-18 `monthly_tpm: 0` hotfix was reverted on
    # 2026-09-09: it gated nothing (no consumer reads monthly_tpm, and the compiled provider block
    # drops it), so what actually stops consumption is now the `insufficient-budget` ->
    # payment_required cooldown ladder. Scaled by token_estimate_buffer like any token budget.
    mistral = compiled["providers"]["mistral"]
    assert mistral["monthly_tpm"] == 900_000_000


def test_validate_token_buffer_accepts_valid_formats():
    assert compile_llm_limits._validate_token_buffer(None) == 1.0
    assert compile_llm_limits._validate_token_buffer(0.9) == 0.9
    assert compile_llm_limits._validate_token_buffer(1.0) == 1.0
    assert compile_llm_limits._validate_token_buffer("0.85") == 0.85
    assert compile_llm_limits._validate_token_buffer("90%") == 0.9
    assert compile_llm_limits._validate_token_buffer("100%") == 1.0


def test_validate_token_buffer_rejects_invalid_values():
    for invalid in (
        True,
        False,
        0,
        0.0,
        -0.1,
        "-10%",
        1.5,
        "150%",
        "invalid",
        "foo%",
        float("nan"),
        float("inf"),
        float("-inf"),
        [],
        {},
    ):
        with pytest.raises(ValueError, match="token_estimate_buffer"):
            compile_llm_limits._validate_token_buffer(invalid)


def test_token_estimate_buffer_omitted_defaults_to_one():
    raw = {
        "structured_output_methods": STRUCTURED_OUTPUT_METHODS,
        "providers": {
            "example": {
                "api_base": "https://example.com",
                "monthly_tpm": 1000,
            }
        },
        "routes": [
            {
                "route_id": "example_route",
                "model": "example/model",
                "provider": "example",
                "upstream_model": "model",
                "input_context_limit": 1000,
                "output_context_limit": 500,
                "tpm": 5000,
            }
        ],
    }
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            compile_llm_limits, "yaml", type("Yaml", (), {"safe_load": lambda *_: raw})
        )
        compiled = compile_llm_limits.compile_limits()
        assert compiled["_metadata"]["token_estimate_buffer"] == 1.0
        assert compiled["routes_by_id"]["example_route"]["tpm"] == 5000
        assert compiled["providers"]["example"]["monthly_tpm"] == 1000


def test_token_usage_buffer_fallback_key():
    raw = {
        "token_usage_buffer": 0.80,
        "structured_output_methods": STRUCTURED_OUTPUT_METHODS,
        "providers": {
            "example": {
                "api_base": "https://example.com",
                "monthly_tpm": 1000,
            }
        },
        "routes": [
            {
                "route_id": "example_route",
                "model": "example/model",
                "provider": "example",
                "upstream_model": "model",
                "input_context_limit": 1000,
                "output_context_limit": 500,
                "tpm": 5000,
            }
        ],
    }
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            compile_llm_limits, "yaml", type("Yaml", (), {"safe_load": lambda *_: raw})
        )
        compiled = compile_llm_limits.compile_limits()
        assert compiled["_metadata"]["token_estimate_buffer"] == 0.8
        assert compiled["routes_by_id"]["example_route"]["tpm"] == 4000
        assert compiled["providers"]["example"]["monthly_tpm"] == 800


def test_split_cap_multiplier_validation():
    assert compile_llm_limits._validate_split_cap(None) == 1.0
    assert compile_llm_limits._validate_split_cap(0.50) == 0.50
    assert compile_llm_limits._validate_split_cap("50%") == 0.50
    assert compile_llm_limits._validate_split_cap("0.75") == 0.75

    with pytest.raises(ValueError, match="split_cap_multiplier must be a number"):
        compile_llm_limits._validate_split_cap(True)
    with pytest.raises(ValueError, match="split_cap_multiplier must be a positive finite number"):
        compile_llm_limits._validate_split_cap(0)
    with pytest.raises(ValueError, match="split_cap_multiplier must be <= 1.0"):
        compile_llm_limits._validate_split_cap(1.5)


def test_split_cap_multiplier_halves_rpm_rpd_tpm():
    raw = {
        "split_cap_multiplier": 0.50,
        "token_estimate_buffer": 1.0,
        "structured_output_methods": STRUCTURED_OUTPUT_METHODS,
        "providers": {
            "example": {
                "api_base": "https://example.com",
                "rpm": 60,
                "monthly_tpm": 10000,
                "tpm": 20000,
            }
        },
        "routes": [
            {
                "route_id": "example_route",
                "model": "example/model",
                "provider": "example",
                "upstream_model": "model",
                "input_context_limit": 1000,
                "output_context_limit": 500,
                "rpm": 100,
                "rpd": 1000,
                "tpm": 40000,
            },
            {
                "route_id": "paused_route",
                "model": "example/paused",
                "provider": "example",
                "upstream_model": "paused",
                "input_context_limit": 1000,
                "output_context_limit": 500,
                "rpm": 10,
                "rpd": 0,
                "tpm": 0,
            },
        ],
    }
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            compile_llm_limits, "yaml", type("Yaml", (), {"safe_load": lambda *_: raw})
        )
        compiled = compile_llm_limits.compile_limits()
        assert compiled["_metadata"]["split_cap_multiplier"] == 0.50
        assert compiled["providers"]["example"]["rpm"] == 30
        assert compiled["providers"]["example"]["monthly_tpm"] == 5000
        assert compiled["providers"]["example"]["tpm"] == 10000

        route = compiled["routes_by_id"]["example_route"]
        assert route["rpm"] == 50
        assert route["rpd"] == 500
        assert route["tpm"] == 20000

        paused = compiled["routes_by_id"]["paused_route"]
        assert paused["rpm"] == 5
        assert paused["rpd"] == 0  # 0 stays 0, not unpaused to 1
        assert paused["tpm"] == 0


def test_rejects_non_positive_provider_tpm():
    raw = {
        "providers": {
            "bad_tpm_prov": {
                "tpm": 0,
            },
        },
        "routes": [],
    }
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            compile_llm_limits, "yaml", type("Yaml", (), {"safe_load": lambda *_: raw})
        )
        with pytest.raises(ValueError, match="invalid non-positive tpm"):
            compile_llm_limits.compile_limits()


def test_observed_characterization_fields_validation_and_compilation():
    raw = {
        "providers": {
            "test_prov": {
                "api_base": "https://api.test.com",
                "structured_output_method": "json_schema",
                "accounts": [{"id": "primary", "api_key_env": "TEST_KEY"}],
            }
        },
        "structured_output_methods": STRUCTURED_OUTPUT_METHODS,
        "routes": [
            {
                "route_id": "test_route_1",
                "model": "test/model",
                "provider": "test_prov",
                "input_context_limit": 100000,
                "output_context_limit": 4096,
                "observed_on": "2026-09-09",
                "observed_burst": 15,
                "observed_input_ceiling": 50000,
                "observed_recovery_seconds": 30.5,
                "retry_after_trustworthy": False,
                "upstream_429_default": "upstream_capacity",
            }
        ],
    }
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            compile_llm_limits, "yaml", type("Yaml", (), {"safe_load": lambda *_: raw})
        )
        compiled = compile_llm_limits.compile_limits()
        r = compiled["routes_by_id"]["test_route_1"]
        assert r["observed_on"] == "2026-09-09"
        assert r["observed_burst"] == 15
        assert r["observed_input_ceiling"] == 50000
        # An observation is evidence, never enforcement: observed_input_ceiling must NOT be
        # promoted to hard_input_ceiling. Auto-promoting the two let a single bad probe run block
        # five routes on 2026-09-09 (review/45 §20.8 requires a human-reviewed promotion).
        assert r.get("hard_input_ceiling") is None
        assert r["observed_recovery_seconds"] == 30.5
        assert r["retry_after_trustworthy"] is False
        assert r["upstream_429_default"] == "upstream_capacity"


def test_observed_characterization_fields_invalid_rejects():
    raw = {
        "providers": {"test_prov": {"api_base": "https://api.test.com"}},
        "structured_output_methods": STRUCTURED_OUTPUT_METHODS,
        "routes": [
            {
                "route_id": "test_route_bad",
                "model": "test/model",
                "provider": "test_prov",
                "input_context_limit": 100000,
                "output_context_limit": 4096,
                "observed_input_ceiling": 200000,  # exceeds input_context_limit
            }
        ],
    }
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            compile_llm_limits, "yaml", type("Yaml", (), {"safe_load": lambda *_: raw})
        )
        with pytest.raises(ValueError, match="observed_input_ceiling"):
            compile_llm_limits.compile_limits()


def test_observed_rpm_lowers_the_effective_limit_but_never_raises_it():
    """`observed_rpm` is consumed in one direction only.

    Lowering is safety-positive (it prevents 429s; worst case the route runs slower than it
    could). Raising would bet a route can absorb more than its authored limit on the strength of
    one probe run, whose worst case is sustained overdrive into throttling. And no measurement may
    drive a limit to 0 -- that is this repository's "paused" convention, and it would silently
    remove the route from dispatch, which is exactly how a bad probe run blocked five routes on
    2026-09-09.
    """

    def _rpm(observed, declared):
        raw = {
            "providers": {
                "test_prov": {
                    "api_base": "https://api.test.com",
                    "structured_output_method": "json_schema",
                    "accounts": [{"id": "primary", "api_key_env": "TEST_KEY"}],
                }
            },
            "structured_output_methods": STRUCTURED_OUTPUT_METHODS,
            "routes": [
                {
                    "route_id": "r1",
                    "model": "test/model",
                    "provider": "test_prov",
                    "input_context_limit": 100000,
                    "output_context_limit": 4096,
                    "rpm": declared,
                    "observed_rpm": observed,
                }
            ],
        }
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(
                compile_llm_limits, "yaml", type("Yaml", (), {"safe_load": lambda *_: raw})
            )
            return compile_llm_limits.compile_limits()["routes_by_id"]["r1"]["rpm"]

    assert _rpm(5, 30) == 5, "a measured limit below the declared one must clamp it down"
    assert _rpm(90, 30) == 30, "a measured limit above the declared one must NOT raise it"
    # A positive fractional observation must stay exactly as measured, not be floored up to 1.0
    # (CodeRabbit, 2026-09-13): validation already rejects `observed_rpm <= 0` outright, so there
    # is no path through which a real observation could reach here as 0 -- a floor of 1.0 instead
    # silently raised a genuine sub-1.0 measurement, the opposite of what a one-way clamp permits.
    assert _rpm(0.2, 30) == 0.2, "positive measurements must remain fractional, never floored up"


def test_nvidia_provider_wide_rpm_is_not_below_the_sum_of_its_own_routes():
    """Regression guard for the nvidia block's stale comment/value (fixed alongside the Nemotron
    migration): its provider-wide `rpm` is explicitly documented as a safety net only, with
    per-route pacing meant to be the real binding constraint -- unlike e.g. Mistral, whose
    provider-wide `rpm` genuinely IS an account-wide limit shared by every model by design, so
    this check is NVIDIA-specific rather than a rule for every provider."""
    raw = _raw_provider_limits()
    nvidia_routes = [route for route in raw["routes"] if route["provider"] == "nvidia"]
    assert nvidia_routes, "expected at least one nvidia route in config/provider_limits.yml"
    route_rpm_sum = sum(route.get("rpm", 0) or 0 for route in nvidia_routes)
    provider_rpm = raw["providers"]["nvidia"]["rpm"]
    assert provider_rpm >= route_rpm_sum, (
        f"providers['nvidia'].rpm ({provider_rpm}) is below the sum of its own routes' rpm "
        f"({route_rpm_sum}); per-route pacing can never be the binding constraint like this"
    )


def _also_serves_route(**overrides):
    route = {
        "route_id": "nv_v41",
        "model": "nvidia/v41",
        "model_key": "ds/v4.1",
        "provider": "nvidia",
        "account_id": "primary",
        "upstream_model": "deepseek-ai/deepseek-v4.1-flash",
        "input_context_limit": 1000,
        "output_context_limit": 100,
        "also_serves": ["ds/v4-flash", "ds/v4-pro"],
    }
    route.update(overrides)
    return route


def test_also_serves_lists_one_route_in_several_pools_without_aliasing_them():
    orca = {
        "route_id": "orca",
        "model": "ds/v4-flash",
        "provider": "orcarouter",
        "account_id": "primary",
        "upstream_model": "deepseek-v4-flash",
        "input_context_limit": 1000,
        "output_context_limit": 100,
    }
    _routes, by_id, model_map, aliases = compile_llm_limits._validated_routes(
        [orca, _also_serves_route()]
    )
    assert model_map["ds/v4.1"] == ["nv_v41"]
    assert model_map["ds/v4-flash"] == ["orca", "nv_v41"]
    assert model_map["ds/v4-pro"] == ["nv_v41"]
    assert by_id["nv_v41"]["model"] == "ds/v4.1"
    assert "ds/v4-pro" not in aliases and "ds/v4-flash" not in aliases


@pytest.mark.parametrize(
    "also_serves",
    [
        "ds/v4-pro",  # not a list
        ["ds/v4-pro", "ds/v4-pro"],  # duplicate
        ["ds/v4.1"],  # its own primary pool
        [""],  # blank
    ],
)
def test_also_serves_rejects_malformed_lists(also_serves):
    with pytest.raises(ValueError, match="also_serves"):
        compile_llm_limits._validated_routes([_also_serves_route(also_serves=also_serves)])


def test_also_serves_rejects_an_alias_instead_of_a_canonical_pool():
    aliased = {
        "route_id": "orca",
        "model": "orcarouter/ds-v4-flash",
        "model_key": "ds/v4-flash",
        "provider": "orcarouter",
        "account_id": "primary",
        "upstream_model": "deepseek-v4-flash",
        "input_context_limit": 1000,
        "output_context_limit": 100,
    }
    with pytest.raises(ValueError, match="alias"):
        compile_llm_limits._validated_routes(
            [aliased, _also_serves_route(also_serves=["orcarouter/ds-v4-flash"])]
        )


@pytest.mark.parametrize(
    ("params", "match"),
    [
        ({"model": "other"}, "unsupported keys"),
        ({}, "non-empty mapping"),
        ("enable_thinking=false", "non-empty mapping"),
    ],
)
def test_request_params_are_limited_to_provider_controls(params, match):
    with pytest.raises(ValueError, match=match):
        compile_llm_limits._validate_request_params({"route_id": "r", "request_params": params})


def test_request_params_are_compiled_for_the_worker():
    compiled = compile_llm_limits.compile_limits()
    worker = compile_llm_limits._worker_catalog(compiled)["routes_by_id"]
    assert worker["nvidia_deepseek_v4_1_flash_free"]["request_params"] == {
        "chat_template_kwargs": {"enable_thinking": False}
    }
    assert worker["nvidia_nemotron_3_ultra_550b_a55b_free"]["request_params"] is None
