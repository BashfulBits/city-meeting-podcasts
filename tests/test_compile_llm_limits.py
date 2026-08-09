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
