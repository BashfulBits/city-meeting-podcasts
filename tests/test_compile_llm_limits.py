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
    }
    assert len(worker["routes_by_id"]) == len(compiled["routes"])
    assert "routes" not in worker
    assert "structured_output_profiles" not in worker
    assert worker["model_aliases"]["deepseek-v4-flash"] == "deepseek/deepseek-v4-flash"
    assert worker["model_aliases"]["deepseek/deepseek-v4-flash"] == "deepseek/deepseek-v4-flash"
    gemma_index = next(
        index
        for index, route in enumerate(compiled["routes"])
        if route["route_id"] == "gemma_4_31b_primary"
    )
    assert isinstance(worker["routes_by_id"][gemma_index], list)
    assert len(worker["routes_by_id"][gemma_index]) == len(compile_llm_limits._WORKER_ROUTE_FIELDS)
    assert "discovery" not in worker["providers"]["openrouter"]


def test_model_keys_pool_equivalent_provider_routes_and_preserve_aliases():
    compiled = compile_llm_limits.compile_limits()

    deepseek_key = "deepseek/deepseek-v4-flash"
    deepseek_routes = compiled["model_routes_map"][deepseek_key]
    assert len(deepseek_routes) == 3
    physical_routes = [compiled["routes_by_id"][route_id] for route_id in deepseek_routes]
    assert (
        len(
            {
                (route["provider"], route["account_id"], route["upstream_model"])
                for route in physical_routes
            }
        )
        == 3
    )
    assert compiled["model_aliases"]["deepseek/deepseek-v4-flash-0731"] == deepseek_key
    assert compiled["model_aliases"]["opencode/deepseek-v4-flash-free"] == deepseek_key

    nemotron_key = "nvidia/nemotron-3-ultra-550b-a55b:free"
    assert len(compiled["model_routes_map"][nemotron_key]) == 3
    assert (
        compiled["model_aliases"]["openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"]
        == nemotron_key
    )
    assert compiled["model_aliases"]["opencode/nemotron-3-ultra-free"] == nemotron_key


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
    compiled = compile_llm_limits.compile_limits()
    gemma = compiled["routes_by_id"]["gemma_4_31b_primary"]
    deepseek = compiled["routes_by_id"]["deepseek_v4_flash_primary"]
    groq = compiled["routes_by_id"]["groq_llama_3_3_70b_versatile_primary"]

    assert gemma["structured_output_profile"] == "relaxed_json_schema"
    assert gemma["structured_output_response_format"] == "json_schema"
    assert gemma["structured_output_direct_handler"] == "native"
    assert "minLength" in gemma["structured_output_schema_strip_keys"]
    assert deepseek["structured_output_profile"] == "json_object"
    assert deepseek["structured_output_response_format"] == "json_object"
    assert deepseek["structured_output_include_schema_in_prompt"] is True
    assert groq["structured_output_profile"] == "json_object"
    assert groq["structured_output_response_format"] == "json_object"


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
