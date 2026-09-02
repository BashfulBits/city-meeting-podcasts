from __future__ import annotations

import pytest

from scripts import compile_llm_limits


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
    assert "structured_output_profiles" not in worker
    assert worker["model_aliases"]["deepseek-v4-flash"] == "deepseek/deepseek-v4-flash"
    assert worker["model_aliases"]["deepseek/deepseek-v4-flash"] == "deepseek/deepseek-v4-flash"
    gemma = worker["routes_by_id"]["gemma_4_31b_primary"]
    assert isinstance(gemma, dict)
    assert set(gemma) == set(compile_llm_limits._WORKER_ROUTE_FIELDS)
    assert gemma["route_id"] == "gemma_4_31b_primary"
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

    deepseek_key = "deepseek/deepseek-v4-flash"
    deepseek_routes = compiled["model_routes_map"][deepseek_key]
    # SiliconFlow (paid) + DeepSeek Direct (paid) + OpenCode (free) + NVIDIA build (free) -- four
    # independent physical pools for the same logical model. The NVIDIA leg was briefly commented
    # out on 2026-08-29, blamed on NVIDIA gating this model per-key; the real cause was Cloudflare
    # AI Gateway dropping the `/v1` from the custom-provider Base URL, which broke every NVIDIA
    # route rather than this one model (see config/provider_limits.yml's `nvidia` block).
    assert len(deepseek_routes) == 4
    physical_routes = [compiled["routes_by_id"][route_id] for route_id in deepseek_routes]
    assert (
        len(
            {
                (route["provider"], route["account_id"], route["upstream_model"])
                for route in physical_routes
            }
        )
        == 4
    )
    assert compiled["model_aliases"]["deepseek/deepseek-v4-flash-0731"] == deepseek_key
    assert compiled["model_aliases"]["opencode/deepseek-v4-flash-free"] == deepseek_key
    assert compiled["model_aliases"]["nvidia/deepseek-v4-flash-0731"] == deepseek_key

    nemotron_key = "nvidia/nemotron-3-ultra-550b-a55b:free"
    # OpenRouter + Kilo + OpenCode (all broker legs) + NVIDIA build direct (added 2026-08-29).
    assert len(compiled["model_routes_map"][nemotron_key]) == 4
    assert (
        compiled["model_aliases"]["openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"]
        == nemotron_key
    )
    assert compiled["model_aliases"]["opencode/nemotron-3-ultra-free"] == nemotron_key
    assert compiled["model_aliases"]["nvidia/nemotron-3-ultra-550b-a55b"] == nemotron_key

    nemotron_super_key = "openrouter/nvidia/nemotron-3-super-120b-a12b:free"
    # OpenRouter's own canonical name for this model family, now also reachable via NVIDIA build
    # direct (added 2026-08-29) -- unlike Ultra, Super had no shared model_key before this change.
    assert len(compiled["model_routes_map"][nemotron_super_key]) == 2
    assert compiled["model_aliases"]["nvidia/nemotron-3-super-120b-a12b"] == nemotron_super_key


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


def test_route_limits_cannot_fall_back_to_provider_defaults():
    raw = {
        "structured_output_profiles": {"standard": {}},
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


def test_compiled_routes_materialize_structured_output_profiles():
    # This used to also assert on groq_llama_3_3_70b_versatile_primary, whose route entry had no
    # route-level structured_output_profile of its own and so exercised the provider-level
    # fallback (`route.get("structured_output_profile", provider_cfg.get(...))`) for a non-default
    # ("json_object") value. That route was removed 2026-08-26 (Groq stopped serving the model);
    # every remaining json_object-profile route sets structured_output_profile explicitly at the
    # route level (see deepseek below), so this fallback branch's non-default case is currently
    # untested against the real config -- its default case (falling through to
    # "standard_json_schema") is still exercised by every other route that sets nothing at all.
    compiled = compile_llm_limits.compile_limits()
    gemma = compiled["routes_by_id"]["gemma_4_31b_primary"]
    deepseek = compiled["routes_by_id"]["deepseek_v4_flash_primary"]

    assert gemma["structured_output_profile"] == "relaxed_json_schema"
    assert gemma["structured_output_response_format"] == "json_schema"
    assert gemma["structured_output_direct_handler"] == "native"
    assert "minLength" in gemma["structured_output_schema_strip_keys"]
    assert deepseek["structured_output_profile"] == "json_object"
    assert deepseek["structured_output_response_format"] == "json_object"
    assert deepseek["structured_output_include_schema_in_prompt"] is True


def test_google_routes_use_live_model_identifiers():
    compiled = compile_llm_limits.compile_limits()
    google_models = {
        route["upstream_model"] for route in compiled["routes"] if route["provider"] == "gemini"
    }

    assert "gemma-4-26b-a4b-it" in google_models
    assert "gemma-4-26b-it" not in google_models
    assert "gemini-2.5-flash" not in google_models
    assert "gemini-2.5-flash-lite" not in google_models


def test_deepseek_v4_flash_uses_current_direct_api_identifier():
    compiled = compile_llm_limits.compile_limits()
    route = compiled["routes_by_id"]["deepseek_v4_flash_primary"]
    assert route["upstream_model"] == "deepseek-v4-flash"


def test_deepseek_pricing_periods_compile_with_input_output_rates_and_peak_windows():
    compiled = compile_llm_limits.compile_limits()
    route = compiled["routes_by_id"]["deepseek_v4_flash_primary"]
    periods = route["pricing"]["periods"]
    assert periods[1]["effective_at"].isoformat() == "2026-08-16T16:00:00+00:00"
    assert periods[1]["input_per_token"] == pytest.approx(0.22e-6)
    assert [(window["start"], window["end"]) for window in periods[1]["windows"]] == [
        ("01:00", "04:00"),
        ("06:00", "10:00"),
    ]


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
    assert compiled["model_routing"] == {}
    assert compiled["model_routes_map"]["mistral/mistral-medium-latest"] == [
        "mistral_medium_latest_primary",
        "airforce_mistral_medium_3_5_primary",
        "mistral_medium_latest_secondary",
    ]
    assert (
        compiled["model_aliases"]["mistral/mistral-medium-2508"] == "mistral/mistral-medium-latest"
    )
    assert (
        compiled["model_aliases"]["mistral/mistral-medium-3-5"] == "mistral/mistral-medium-latest"
    )
    assert (
        compiled["model_aliases"]["mistral/mistral-medium-2505"] == "mistral/mistral-medium-latest"
    )
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
    for provider in ("kilo", "opencode", "siliconflow"):
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
        {"route_id": "dup", "model": "mistral/mistral-large-2512"},
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
    assert compiled["_metadata"]["split_cap_multiplier"] == 0.50

    # Route TPM scaling with 0.90 buffer and 0.50 split-cap:
    # 250,000 * 0.9 * 0.5 = 112,500; 16,000 * 0.9 * 0.5 = 7,200
    gemini = compiled["routes_by_id"]["gemini_3_5_flash_lite_primary"]
    gemma = compiled["routes_by_id"]["gemma_4_31b_primary"]
    assert gemini["tpm"] == 112_500
    assert gemma["tpm"] == 7_200

    # RPM / RPD scaled by split-cap (0.50)
    assert gemini["rpm"] == 7
    assert gemini["rpd"] == 250
    assert gemini["input_context_limit"] == 1048576
    assert gemini["output_context_limit"] == 65536

    # Provider monthly_tpm scaling: hotfixed to 0 (Mistral's new account-wide monthly metering,
    # see config/provider_limits.yml), preserved as 0.
    mistral = compiled["providers"]["mistral"]
    assert mistral["monthly_tpm"] == 0


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
        "structured_output_profiles": {
            "standard_json_schema": {
                "response_format": "json_schema",
                "direct_handler": "instructor",
                "include_schema_in_prompt": False,
                "strip_schema_keys": [],
            }
        },
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
        "structured_output_profiles": {
            "standard_json_schema": {
                "response_format": "json_schema",
                "direct_handler": "instructor",
                "include_schema_in_prompt": False,
                "strip_schema_keys": [],
            }
        },
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
        "structured_output_profiles": {
            "standard_json_schema": {
                "response_format": "json_schema",
                "direct_handler": "instructor",
                "include_schema_in_prompt": False,
                "strip_schema_keys": [],
            }
        },
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
