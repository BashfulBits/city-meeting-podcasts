#!/usr/bin/env python3
"""Compile config/provider_limits.yml into workers/llm-dispatch-proxy/src/dispatch_limits.json.

Statically parses provider accounts, models, and rate limits into pre-indexed lookup maps for
sub-10ms Cloudflare Worker execution. The default invocation (no flags) touches only the local
filesystem -- no network call, deterministic, safe to run in CI/deploy.

Auto-discovery of a provider's models/pricing (e.g. OpenRouter's `GET /v1/models`) is a *separate*,
explicit, maintainer-run step: `--discover [provider ...]`. It is never invoked by the deploy
workflow (review/41 -- a live network call inside a deploy job made the deployed artifact able to
differ from the reviewed one, and a transient failure would have silently shipped a build missing
those routes). Run it locally, review the resulting diff to `config/provider_limits.yml`, and commit
both that file and the recompiled `dispatch_limits.json` together.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
INPUT_YAML = REPO_ROOT / "config" / "provider_limits.yml"
OUTPUT_JSON = REPO_ROOT / "workers" / "llm-dispatch-proxy" / "src" / "dispatch_limits.json"
# Same catalog shape as OUTPUT_JSON, for review/44's v2 executor Worker (Unit 4's
# routeHasCapacityFor/routesEligibleFor need the same physical route/provider data v1 has). Kept
# as a second write of the same compiled catalog, not a cross-Worker-directory import, so v2 has
# no build/deploy dependency on v1's directory continuing to exist past its Phase 3 retirement.
V2_OUTPUT_JSON = REPO_ROOT / "workers" / "llm-dispatch-v2" / "src" / "dispatch_limits.json"
PYTHON_OUTPUT_JSON = REPO_ROOT / "citypods" / "compute" / "llm_routes.json"

# review/48 R10: the closed set of structured-output methods. Python's direct path and the v2
# Worker each implement exactly these four shapes (asserted against one shared fixture), so a name
# outside this set is a compile error rather than a request shape nobody implements.
_STRUCTURED_OUTPUT_METHODS = frozenset(
    {"json_schema", "json_schema_relaxed", "json_object", "prompt_only"}
)
_STRUCTURED_OUTPUT_FORMATS = frozenset({"json_schema", "json_object", "none"})
# The shape every chat endpoint accepts: no response_format, schema in the prompt.
_FALLBACK_STRUCTURED_OUTPUT_METHOD = "prompt_only"


def _json_default(value: object) -> str:
    """Serialize YAML's native date/time scalars without losing their UTC offset."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _validate_token_buffer(raw_buffer: Any) -> float:
    """Validate and normalize the global token estimate buffer multiplier.

    Returns a float multiplier in (0.0, 1.0]. A missing/null value defaults to 1.0 (unbuffered).
    Strings with a trailing '%' (e.g. '90%') are parsed as percentages. Values outside (0.0, 1.0]
    or non-numeric types are rejected.
    """
    if raw_buffer is None:
        return 1.0
    if isinstance(raw_buffer, bool):
        raise ValueError(f"token_estimate_buffer must be a number, got boolean {raw_buffer!r}")
    if isinstance(raw_buffer, str):
        val_str = raw_buffer.strip()
        if val_str.endswith("%"):
            try:
                numeric_val = float(val_str[:-1].strip()) / 100.0
            except ValueError as exc:
                raise ValueError(
                    f"token_estimate_buffer has invalid percentage value: {raw_buffer!r}"
                ) from exc
        else:
            try:
                numeric_val = float(val_str)
            except ValueError as exc:
                raise ValueError(
                    f"token_estimate_buffer has invalid numeric value: {raw_buffer!r}"
                ) from exc
    elif isinstance(raw_buffer, (int, float)):
        numeric_val = float(raw_buffer)
    else:
        raise ValueError(f"token_estimate_buffer must be a number, got {type(raw_buffer).__name__}")

    if not math.isfinite(numeric_val) or numeric_val <= 0:
        raise ValueError(
            f"token_estimate_buffer must be a positive finite number, got {raw_buffer!r}"
        )
    if numeric_val > 1.0:
        raise ValueError(
            f"token_estimate_buffer must be <= 1.0 (e.g. 0.90 for 90%), got {raw_buffer!r}"
        )
    return numeric_val


def _validate_split_cap(raw_multiplier: Any) -> float:
    """Validate and normalize the split-cap multiplier for multi-dispatcher coexistence.

    Returns a float multiplier in (0.0, 1.0]. A missing/null value defaults to 1.0 (unscaled).
    Strings with a trailing '%' (e.g. '50%') are parsed as percentages. Values outside (0.0, 1.0]
    or non-numeric types are rejected.
    """
    if raw_multiplier is None:
        return 1.0
    if isinstance(raw_multiplier, bool):
        raise ValueError(f"split_cap_multiplier must be a number, got boolean {raw_multiplier!r}")
    if isinstance(raw_multiplier, str):
        val_str = raw_multiplier.strip()
        if val_str.endswith("%"):
            try:
                numeric_val = float(val_str[:-1].strip()) / 100.0
            except ValueError as exc:
                raise ValueError(
                    f"split_cap_multiplier has invalid percentage value: {raw_multiplier!r}"
                ) from exc
        else:
            try:
                numeric_val = float(val_str)
            except ValueError as exc:
                raise ValueError(
                    f"split_cap_multiplier has invalid numeric value: {raw_multiplier!r}"
                ) from exc
    elif isinstance(raw_multiplier, (int, float)):
        numeric_val = float(raw_multiplier)
    else:
        raise ValueError(
            f"split_cap_multiplier must be a number, got {type(raw_multiplier).__name__}"
        )

    if not math.isfinite(numeric_val) or numeric_val <= 0:
        raise ValueError(
            f"split_cap_multiplier must be a positive finite number, got {raw_multiplier!r}"
        )
    if numeric_val > 1.0:
        raise ValueError(
            f"split_cap_multiplier must be <= 1.0 (e.g. 0.50 for 50%), got {raw_multiplier!r}"
        )
    return numeric_val


def _scale_rate_limit(value: Any, multiplier: float) -> Any:
    """Scale a numeric rate limit by multiplier, preserving None and 0.

    Rates below one-per-window are preserved as floats rather than floored. Clamping them to 1
    would *raise* the rate -- ``0.25`` became ``1``, four times what the config asked for -- which
    inverts the whole point of scaling a limit down. A route slower than one request per minute is
    a legitimate configuration: NVIDIA's free NIM endpoints lock out for ~26 minutes once roughly
    30 successful requests land inside an hour, so staying under it means pacing below 1 rpm.
    """
    if value is None or multiplier == 1.0:
        return value
    if isinstance(value, (int, float)):
        if value == 0:
            return 0
        scaled = value * multiplier
        if scaled >= 1:
            return int(math.floor(scaled))
        return scaled
    return value


def _validate_ai_gateway_max_attempts(value: Any, provider: str) -> int | None:
    """Validate a provider-specific AI Gateway retry override.

    Cloudflare permits one through five attempts. Keeping the validation in the compiler prevents
    a malformed provider entry from becoming a live header that the gateway silently ignores.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
        raise ValueError(
            f"provider {provider!r} ai_gateway_max_attempts must be an integer from 1 through 5"
        )
    return value


def _direct_model(provider: str, upstream_model: str) -> str:
    """Return the LiteLLM model selector for a compiled provider route.

    Airforce, Kilo, OpenCode, SiliconFlow, and NVIDIA (build.nvidia.com) expose
    OpenAI-compatible gateways
    rather than stable LiteLLM provider adapters. Selecting the OpenAI adapter and supplying the
    compiled ``api_base`` keeps those routes usable directly without teaching the scheduler
    provider-specific URL logic.
    """
    if provider in {"airforce", "kilo", "opencode", "siliconflow", "nvidia", "orcarouter"}:
        return f"openai/{upstream_model}"
    return f"{provider}/{upstream_model}"


def _normalize_structured_output_methods(raw_methods: Any) -> dict[str, dict[str, Any]]:
    """Validate the structured-output method table against the closed set of methods."""
    if not isinstance(raw_methods, dict) or set(raw_methods) != _STRUCTURED_OUTPUT_METHODS:
        raise ValueError(
            "structured_output_methods must define exactly "
            f"{sorted(_STRUCTURED_OUTPUT_METHODS)} (review/48 R10)"
        )
    methods: dict[str, dict[str, Any]] = {}
    for name, raw_method in raw_methods.items():
        if not isinstance(raw_method, dict):
            raise ValueError(f"structured output method {name!r} must be a mapping")
        response_format = raw_method.get("response_format")
        if response_format not in _STRUCTURED_OUTPUT_FORMATS:
            raise ValueError(
                f"structured output method {name!r} has unsupported response_format "
                f"{response_format!r}"
            )
        strip_schema_keys = raw_method.get("strip_schema_keys", [])
        if not isinstance(strip_schema_keys, list) or any(
            not isinstance(key, str) or not key for key in strip_schema_keys
        ):
            raise ValueError(
                f"structured output method {name!r} strip_schema_keys must be a list of strings"
            )
        include_schema_in_prompt = raw_method.get("include_schema_in_prompt", False)
        if not isinstance(include_schema_in_prompt, bool):
            raise ValueError(
                f"structured output method {name!r} include_schema_in_prompt must be boolean"
            )
        if response_format != "json_schema" and not include_schema_in_prompt:
            # Without a json_schema response_format the prompt is the only place the schema can
            # reach the model.
            raise ValueError(
                f"structured output method {name!r} must include the schema in the prompt"
            )
        methods[name] = {
            "response_format": response_format,
            "include_schema_in_prompt": include_schema_in_prompt,
            "strip_schema_keys": list(dict.fromkeys(strip_schema_keys)),
        }
    return methods


# Provider-specific request parameters a route may always send. Deliberately small: each key is
# a documented provider control (thinking/reasoning), never a way to override what the pipeline
# sends (model, messages, response_format, max_tokens ...), which the Worker owns.
_ALLOWED_REQUEST_PARAMS = frozenset({"chat_template_kwargs", "reasoning_effort"})


# Reasoning levels a lane may request (llm_lanes[...].reasoning); a route maps each level it
# supports to provider-specific request parameters in `reasoning_controls`.
_REASONING_LEVELS = frozenset({"off", "low"})


def _validate_reasoning_controls(route: dict[str, Any]) -> None:
    controls = route.get("reasoning_controls")
    if controls is None:
        return
    if not isinstance(controls, dict) or not controls:
        raise ValueError(
            f"route {route['route_id']!r} reasoning_controls must be a non-empty mapping"
        )
    for level, params in controls.items():
        if level not in _REASONING_LEVELS:
            raise ValueError(
                f"route {route['route_id']!r} reasoning_controls has unknown level {level!r}; "
                f'allowed: {sorted(_REASONING_LEVELS)} (quote "off": bare off is YAML false)'
            )
        # A null or empty level would compile to "send nothing", silently ignoring the lane.
        if not isinstance(params, dict) or not params:
            raise ValueError(
                f"route {route['route_id']!r} reasoning_controls[{level!r}] must be a non-empty "
                "mapping of provider parameters"
            )
        _validate_request_params(
            {"route_id": f"{route['route_id']}.{level}", "request_params": params}
        )


def _validate_request_params(route: dict[str, Any]) -> None:
    params = route.get("request_params")
    if params is None:
        return
    if not isinstance(params, dict) or not params:
        raise ValueError(f"route {route['route_id']!r} request_params must be a non-empty mapping")
    unknown = set(params) - _ALLOWED_REQUEST_PARAMS
    if unknown:
        raise ValueError(
            f"route {route['route_id']!r} request_params has unsupported keys {sorted(unknown)}; "
            f"allowed: {sorted(_ALLOWED_REQUEST_PARAMS)}"
        )
    json.dumps(params)  # must be plain JSON


def _resolve_structured_output_methods(
    routes: list[dict[str, Any]], providers: dict[str, Any]
) -> dict[str, tuple[str, str, str | None]]:
    """Resolve every route's method as ``route_id -> (method, source, verified_on)``.

    Support is a property of the provider *serving* a model, so a route's own verified method is
    authoritative. An unverified route falls back to a method verified for the same model on
    another route, then its provider's method, then ``prompt_only`` (review/48 R10).
    """
    for scope, cfg in [("route", r) for r in routes] + [
        ("provider", c) for c in providers.values()
    ]:
        if "structured_output_profile" in cfg:
            raise ValueError(
                f"{scope} uses retired key structured_output_profile; use structured_output_method"
            )
    verified_by_model: dict[str, set[str]] = {}
    for route in routes:
        method = route.get("structured_output_method")
        verified_on = route.get("structured_output_verified_on")
        if method is not None and method not in _STRUCTURED_OUTPUT_METHODS:
            raise ValueError(
                f"route {route['route_id']!r} has unknown structured_output_method {method!r}"
            )
        if verified_on is not None and method is None:
            raise ValueError(
                f"route {route['route_id']!r} has structured_output_verified_on without a method"
            )
        if method is not None and verified_on is not None:
            verified_by_model.setdefault(route["model"], set()).add(method)
    resolved: dict[str, tuple[str, str, str | None]] = {}
    for route in routes:
        route_id = route["route_id"]
        own = route.get("structured_output_method")
        verified_on = route.get("structured_output_verified_on")
        provider_method = (providers.get(route.get("provider")) or {}).get(
            "structured_output_method"
        )
        if provider_method is not None and provider_method not in _STRUCTURED_OUTPUT_METHODS:
            raise ValueError(
                f"provider {route.get('provider')!r} has unknown structured_output_method "
                f"{provider_method!r}"
            )
        model_methods = verified_by_model.get(route["model"], set())
        if own is not None:
            resolved[route_id] = (own, "route", str(verified_on) if verified_on else None)
        elif len(model_methods) == 1:
            resolved[route_id] = (next(iter(model_methods)), "model", None)
        elif provider_method is not None:
            resolved[route_id] = (provider_method, "provider", None)
        else:
            resolved[route_id] = (_FALLBACK_STRUCTURED_OUTPUT_METHOD, "default", None)
    return resolved


def _python_routes(compiled: dict[str, Any]) -> dict[str, Any]:
    """Build the Python-side catalog from the exact data sent to the Worker.

    The Worker keeps the full route/account list because it must select a physical credential.
    Python receives the same list plus direct-adapter fields; it must not maintain a second list of
    logical models that can silently drift from the dispatch registry.
    """
    providers = compiled.get("providers", {})
    routes = []
    for source in compiled.get("routes", []):
        provider_cfg = providers.get(source.get("provider"), {})
        requested_account_id = source.get("account_id")
        account = next(
            (
                account
                for account in provider_cfg.get("accounts", [])
                if account.get("id") == requested_account_id
            ),
            None,
        )
        if requested_account_id and account is None:
            raise ValueError(
                f"route {source.get('route_id', source.get('model'))!r} references unknown "
                f"account_id {requested_account_id!r} for provider {source.get('provider')!r}"
            )
        if account is None and provider_cfg.get("accounts"):
            account = provider_cfg["accounts"][0]
        route = dict(source)
        route.update(
            {
                "transports": ["direct", "llm-dispatch"],
                "direct_model": _direct_model(
                    str(source.get("provider", "")), str(source.get("upstream_model", ""))
                ),
                "api_base": provider_cfg.get("api_base", ""),
                "ai_gateway_slug": provider_cfg.get("ai_gateway_slug", source.get("provider", "")),
                "ai_gateway_chat_path": provider_cfg.get(
                    "ai_gateway_chat_path", "/chat/completions"
                ),
                "chat_path": provider_cfg.get("chat_path", "/v1/chat/completions"),
                "api_key_env": (account or {}).get("api_key_env", ""),
                "reset_timezone": source.get(
                    "reset_timezone", provider_cfg.get("reset_timezone", "UTC")
                ),
                # Limits are required on the physical route.  Do not fall back to a provider
                # default: the same provider can expose different model ceilings, and gateways
                # can cap a model below its native card (for example OpenRouter Gemma free).
                "input_context_limit": int(source["input_context_limit"]),
                "output_context_limit": int(source["output_context_limit"]),
            }
        )
        if provider_cfg.get("rpm") is not None:
            route["provider_rpm"] = provider_cfg["rpm"]
        if provider_cfg.get("tpm") is not None:
            route["provider_tpm"] = provider_cfg["tpm"]
        if provider_cfg.get("concurrency") is not None:
            route["provider_concurrency"] = provider_cfg["concurrency"]
        routes.append(route)
    return {
        "_metadata": compiled["_metadata"],
        "model_aliases": compiled.get("model_aliases", {}),
        "model_routing": compiled.get("model_routing", {}),
        "routes": routes,
    }


_WORKER_ROUTE_FIELDS = (
    "route_id",
    # The route's PRIMARY logical model. A route listed in several pools via `also_serves`
    # appears in several model_routes_map entries, so the Worker must not infer its identity from
    # whichever pool it happens to find first (routes.js modelForRouteId).
    "model",
    "provider",
    "upstream_model",
    "input_context_limit",
    "output_context_limit",
    "hard_input_ceiling",
    "input_token_ratio",
    "account_id",
    "rpm",
    "rpd",
    "tpm",
    "concurrency",
    "request_start_margin_seconds",
    "free",
    "input_per_token",
    "output_per_token",
    "pricing",
    "reset_timezone",
    # Rate probe characterization measurements (PR-5 / Initiative 20).
    "observed_on",
    "observed_rpm",
    "observed_burst",
    "observed_input_ceiling",
    "observed_recovery_seconds",
    "retry_after_trustworthy",
    "upstream_429_default",
    # Read by the Worker's upstreamRequestForRoute (gateway.js) to shape a schema-only structured
    # job for this route (review/48 R10). Each entry lands under its own field name in the
    # compiled catalog, so order here does not matter.
    "structured_output_method",
    "structured_output_response_format",
    "structured_output_include_schema_in_prompt",
    "structured_output_schema_strip_keys",
    # Merged into the provider request by gateway.js's upstreamRequestForRoute.
    "request_params",
    # How this route expresses a reasoning level a lane asks for (gateway.js applies it).
    "reasoning_controls",
)

_WORKER_PROVIDER_FIELDS = (
    "api_base",
    "ai_gateway_slug",
    "ai_gateway_chat_path",
    "chat_path",
    "ai_gateway_max_attempts",
    "rpm",
    "tpm",
    "concurrency",
    "reset_timezone",
    "accounts",
)


def _worker_legacy_model_map(routes: list[dict[str, Any]]) -> dict[str, str]:
    """Replace the Worker-only legacy route scan with an indexed lookup.

    The old Worker scanned the duplicated ``routes`` array only when a request used a legacy
    upstream model selector. Preserve that first-route-wins behavior while compiling the two
    selectors it tested into a compact map.
    """
    result: dict[str, str] = {}
    for route in routes:
        canonical_model = route["model"]
        upstream_model = route.get("upstream_model")
        provider = route.get("provider")
        for selector in (upstream_model, f"{provider}/{upstream_model}"):
            if selector and selector not in result:
                result[selector] = canonical_model
    return result


def _worker_catalog(compiled: dict[str, Any]) -> dict[str, Any]:
    """Build the minimal route catalog imported by the Worker at startup.

    Python receives the richer catalog from ``_python_routes``. The Worker only needs physical
    route selection, provider endpoint/credential data, and materialized pricing/limits. In
    particular, do not ship the duplicate route list or direct structured-output metadata: the
    Worker does not use either.

    ``routes_by_id`` is keyed by route ID and ``model_routes_map`` holds route-ID strings, matching
    ``compiled``'s own shape -- not the positional-array/integer-index encoding an earlier revision
    used to shave startup parse time. That parse cost was measured and ruled out as a scheduled-
    dispatch hotspot (review/43); the array encoding remained only as an unverified holdover, and
    it cost a real bug (structured_output_schema_strip_keys silently missing because
    _WORKER_ROUTE_FIELDS and its JS twin drifted out of sync -- CHANGELOG, 2026-08-15) plus a latent
    worse one: model_routes_map's indices would silently resolve to the *wrong* route, not merely a
    missing field, if routes were ever reordered. A named lookup can go missing; it cannot
    misresolve.
    """
    routes = compiled.get("routes", [])
    worker_routes = {
        route["route_id"]: {key: route.get(key) for key in _WORKER_ROUTE_FIELDS} for route in routes
    }
    providers = {
        provider: {key: config[key] for key in _WORKER_PROVIDER_FIELDS if key in config}
        for provider, config in compiled.get("providers", {}).items()
    }
    worker_aliases = dict(compiled.get("model_aliases", {}))
    for selector, canonical_model in _worker_legacy_model_map(routes).items():
        worker_aliases.setdefault(selector, canonical_model)
    return {
        "_metadata": compiled["_metadata"],
        "providers": providers,
        "routes_by_id": worker_routes,
        "model_routes_map": {
            model: list(route_ids)
            for model, route_ids in compiled.get("model_routes_map", {}).items()
        },
        "model_aliases": worker_aliases,
        "model_routing": {
            model: list(targets) for model, targets in compiled.get("model_routing", {}).items()
        },
    }


def fetch_openrouter_models(provider_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Best-effort auto-discovery of OpenRouter models and pricing.

    ``provider_cfg`` is this provider's own block from ``provider_limits.yml`` (reads its
    ``discovery.endpoint`` rather than hardcoding the URL, so the YAML stays the single source of
    truth for it). Never called unless the maintainer passes ``--discover openrouter`` (or bare
    ``--discover``, which covers every provider with a ``discovery`` block).
    """
    endpoint = (provider_cfg.get("discovery") or {}).get("endpoint")
    if not endpoint:
        raise ValueError("openrouter has no discovery.endpoint configured")
    # `endpoint` comes from committed YAML, not user input, so this isn't the SSRF gate
    # (`validate_source_url`) applies to -- but `urlopen` honors `file://`/`http://` just as
    # readily as `https://`, so a one-line scheme check closes that class of surprise for free
    # (CodeRabbit, review/41).
    if not str(endpoint).startswith("https://"):
        raise ValueError(f"openrouter discovery.endpoint must be https://, got {endpoint!r}")
    req = Request(endpoint, headers={"User-Agent": "citypods-limits-compiler/1.0"})
    try:
        with urlopen(req, timeout=5) as resp:  # noqa: S310 -- fixed maintainer-configured endpoint
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("data", [])
    except Exception as exc:
        print(f"Notice: openrouter model auto-discovery skipped ({exc})", file=sys.stderr)
    return []


def _openrouter_routes(
    discovered: list[dict[str, Any]], existing_route_ids: set[str]
) -> list[dict[str, Any]]:
    """Transform OpenRouter's discovery response into this repo's route dict shape, skipping any
    route_id already present -- including one *this same call* just added, so two discovered model
    IDs that normalize to the same route_id (e.g. ``a/b:free`` and ``a/b_free``) can't both be
    appended and silently overcount ``routes_count``/duplicate ``model_routes_map`` entries."""
    new_routes: list[dict[str, Any]] = []
    for item in discovered:
        m_id = item.get("id")
        if not m_id:
            continue
        route_id = f"openrouter_{m_id.replace('/', '_').replace(':', '_')}"
        if route_id in existing_route_ids:
            continue
        existing_route_ids.add(route_id)

        pricing = item.get("pricing", {})
        try:
            inp_price = float(pricing.get("prompt", 0) or 0)
            out_price = float(pricing.get("completion", 0) or 0)
        except (TypeError, ValueError):
            inp_price, out_price = 0.0, 0.0

        new_routes.append(
            {
                "route_id": route_id,
                "model": f"openrouter/{m_id}",
                "provider": "openrouter",
                "upstream_model": m_id,
                "account_id": "primary",
                "free": inp_price == 0.0 and out_price == 0.0,
                "input_per_token": inp_price,
                "output_per_token": out_price,
                "auto_discovered": True,
                # OpenRouter publishes both the model-family context and the effective top
                # provider ceiling.  Discovery-created routes must be just as explicit as the
                # curated routes; otherwise a future discovery run would reintroduce a provider
                # default into the compiled catalog.
                "input_context_limit": int(item.get("context_length") or 1),
                "output_context_limit": int(
                    (item.get("top_provider") or {}).get("max_completion_tokens")
                    or item.get("context_length")
                    or 1
                ),
            }
        )
    return new_routes


# One entry per provider that has a real discovery endpoint. A future provider that gains one
# (Mistral/Gemini/DeepSeek `GET /models`-style endpoints) plugs in the same way: a fetcher here
# reading its own `discovery.endpoint` from the YAML, plus a `_<provider>_routes()` transform --
# both gated identically behind `--discover`, never called from the default/deploy path.
DISCOVERY_FETCHERS: dict[str, Callable[[dict[str, Any]], list[dict[str, Any]]]] = {
    "openrouter": fetch_openrouter_models,
}
DISCOVERY_TRANSFORMS: dict[
    str, Callable[[list[dict[str, Any]], set[str]], list[dict[str, Any]]]
] = {
    "openrouter": _openrouter_routes,
}


def run_discovery(raw: dict[str, Any], providers_requested: list[str]) -> bool:
    """Mutate ``raw["routes"]`` in place with newly discovered routes for each requested provider.

    ``providers_requested`` empty means "every provider with a ``discovery`` block declared."
    Returns whether anything was actually discovered (so the caller only rewrites the YAML file
    when there's a real change). Unknown/undiscoverable provider names are a hard error -- a typo
    in a maintainer-run flag must not silently no-op.
    """
    providers = raw.get("providers", {})
    routes = raw.setdefault("routes", [])
    targets = providers_requested or [
        name for name, cfg in providers.items() if isinstance(cfg, dict) and cfg.get("discovery")
    ]
    changed = False
    for name in targets:
        provider_cfg = providers.get(name)
        if not isinstance(provider_cfg, dict) or not provider_cfg.get("discovery"):
            raise ValueError(f"provider {name!r} has no discovery.endpoint configured")
        fetcher = DISCOVERY_FETCHERS.get(name)
        transform = DISCOVERY_TRANSFORMS.get(name)
        if fetcher is None or transform is None:
            raise ValueError(f"provider {name!r} has no registered discovery fetcher")
        discovered = fetcher(provider_cfg)
        existing_route_ids = {r.get("route_id") for r in routes}
        new_routes = transform(discovered, existing_route_ids)
        if new_routes:
            routes.extend(new_routes)
            changed = True
        print(f"discovery: {name} -> {len(new_routes)} new route(s)", file=sys.stderr)
    return changed


def _validated_routes(
    routes: list[Any],
) -> tuple[
    list[dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, list[str]],
    dict[str, str],
]:
    """Normalize and pre-index routes by logical model / route_id for O(1) Worker lookup.

    ``model`` is the backwards-compatible selector written by a provider route author.
    ``model_key`` optionally assigns that route to a shared logical model family. The compiled
    route list stores the shared key in ``model`` and ``model_aliases`` preserves the old selector,
    so existing callers keep working while the scheduler can pool equivalent provider routes.

    Raises a clear ``ValueError`` naming the offending route index for a hand-authored YAML route
    missing either required key, instead of letting an opaque ``KeyError`` surface right before
    ``wrangler deploy``.
    """
    normalized_routes: list[dict[str, Any]] = []
    routes_by_id: dict[str, dict[str, Any]] = {}
    model_routes_map: dict[str, list[str]] = {}
    model_aliases: dict[str, str] = {}
    physical_routes: dict[tuple[str, str, str], tuple[str, dict[str, Any]]] = {}

    def register_alias(source_model: str, canonical_model: str, index: int) -> None:
        if source_model == canonical_model:
            return
        prior = model_aliases.get(source_model)
        if prior is not None and prior != canonical_model:
            raise ValueError(
                f"route #{index} assigns model alias {source_model!r} to both "
                f"{prior!r} and {canonical_model!r}"
            )
        model_aliases[source_model] = canonical_model

    for index, route in enumerate(routes):
        try:
            r_id = route["route_id"]
            source_model = route["model"]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"route #{index} is missing 'route_id' or 'model': {route!r}") from exc
        if not isinstance(r_id, str) or not r_id.strip():
            raise ValueError(f"route #{index} has an invalid route_id: {r_id!r}")
        if not isinstance(source_model, str) or not source_model.strip():
            raise ValueError(f"route #{index} has an invalid model: {source_model!r}")
        c_model = route.get("model_key", source_model)
        if not isinstance(c_model, str) or not c_model.strip():
            raise ValueError(f"route #{index} has an invalid model_key: {c_model!r}")
        if r_id in routes_by_id:
            # Same class of bug already fixed for discovery (`_openrouter_routes`'s
            # `existing_route_ids` dedup): two hand-authored routes sharing a route_id would
            # otherwise silently collapse in `routes_by_id` while both stay in `routes` and
            # `model_routes_map`, overcounting `_metadata.routes_count` and making one route
            # unreachable (CodeRabbit, review/41).
            raise ValueError(f"route #{index} redeclares route_id {r_id!r}")
        normalized = dict(route)
        normalized["model"] = c_model
        normalized.pop("model_key", None)
        # A provider/account/upstream tuple is one physical quota bucket. A second YAML entry
        # that differs only by its selector is an alias, not another capacity pool; compiling it
        # as a route would let the Worker reserve the same credential twice. Require complete
        # physical identity and identical limits before coalescing, so a real quota/configuration
        # conflict fails loudly instead of silently choosing one entry.
        physical_identity = tuple(
            str(normalized.get(field, "")) for field in ("provider", "account_id", "upstream_model")
        )
        physical_settings = {
            key: value for key, value in normalized.items() if key not in {"route_id", "model"}
        }
        if all(physical_identity):
            prior_physical = physical_routes.get(physical_identity)
            if prior_physical is not None:
                prior_model, prior_settings = prior_physical
                if c_model != prior_model:
                    raise ValueError(
                        f"route #{index} reuses physical provider/account/upstream tuple "
                        f"{physical_identity!r} with conflicting model_key {c_model!r}; "
                        f"existing key is {prior_model!r}"
                    )
                if physical_settings != prior_settings:
                    raise ValueError(
                        f"route #{index} reuses physical provider/account/upstream tuple "
                        f"{physical_identity!r} with conflicting limits or settings"
                    )
                register_alias(source_model, prior_model, index)
                continue
            physical_routes[physical_identity] = (c_model, physical_settings)

        normalized_routes.append(normalized)
        routes_by_id[r_id] = normalized
        model_routes_map.setdefault(c_model, []).append(r_id)
        register_alias(source_model, c_model, index)

    # `also_serves`: one physical route may belong to more than one logical model pool. The route
    # keeps a single route_id -- so a single set of rpm/rpd/tpm counters, matching the single
    # upstream quota -- and is appended to each listed pool after its primary `model` pool. Used
    # when a provider serves one upstream model under several logical names (NVIDIA's
    # deepseek-v4.1-flash replacing both retired v4-flash-0731 and v4-pro-0813) without making
    # those names aliases of each other, which would also pull every other route of the primary
    # pool into them.
    for route in normalized_routes:
        extra = route.get("also_serves")
        if extra is None:
            continue
        if not isinstance(extra, list) or not all(
            isinstance(model, str) and model.strip() for model in extra
        ):
            raise ValueError(
                f"route {route['route_id']!r} has an invalid also_serves: {extra!r} "
                "(expected a list of model names)"
            )
        if len(set(extra)) != len(extra) or route["model"] in extra:
            raise ValueError(
                f"route {route['route_id']!r} also_serves must list distinct models other than "
                f"its own model {route['model']!r}"
            )
        for model in extra:
            if model in model_aliases:
                raise ValueError(
                    f"route {route['route_id']!r} also_serves {model!r}, which is an alias of "
                    f"{model_aliases[model]!r}; name the canonical pool instead"
                )
            model_routes_map.setdefault(model, []).append(route["route_id"])
        route["also_serves"] = list(extra)

    canonical_models = set(model_routes_map)
    conflicts = sorted(alias for alias in model_aliases if alias in canonical_models)
    if conflicts:
        alias = conflicts[0]
        raise ValueError(
            f"model alias {alias!r} is also a canonical model; use one model_key consistently"
        )
    return normalized_routes, routes_by_id, model_routes_map, model_aliases


def _validated_model_routing(
    raw_model_routing: Any,
    *,
    model_aliases: dict[str, str],
    canonical_models: set[str],
) -> dict[str, list[str]]:
    """Normalize and validate the optional cross-model overflow map (``model_routing`` in the YAML).

    Each key names a model whose callers may also be satisfied by any of its listed target models
    once its own routes are exhausted/unavailable -- extra capacity for a job pinned to one model,
    without editing the job itself. Keys and values may use either a canonical model or a legacy
    alias; both resolve through ``model_aliases`` the same way a route's own ``model`` selector
    does. An unknown model on either side is a hard compile error, so a typo can't silently produce
    a routing entry nothing ever reaches.
    """

    def resolve(selector: Any, *, where: str) -> str:
        if not isinstance(selector, str) or not selector.strip():
            raise ValueError(f"model_routing {where} has an invalid model selector: {selector!r}")
        canonical = model_aliases.get(selector, selector)
        if canonical not in canonical_models:
            raise ValueError(f"model_routing {where} references unknown model {selector!r}")
        return canonical

    if raw_model_routing is None:
        return {}
    if not isinstance(raw_model_routing, dict):
        raise ValueError(
            f"model_routing must be a mapping of source models to target lists, "
            f"got {raw_model_routing!r}"
        )

    result: dict[str, list[str]] = {}
    for source, targets in raw_model_routing.items():
        canonical_source = resolve(source, where=f"key {source!r}")
        if not isinstance(targets, list) or not targets:
            raise ValueError(f"model_routing[{source!r}] must be a non-empty list of target models")
        resolved = result.setdefault(canonical_source, [])
        for target in targets:
            canonical_target = resolve(target, where=f"target for {source!r}")
            if canonical_target == canonical_source:
                raise ValueError(f"model_routing[{source!r}] cannot route a model to itself")
            if canonical_target not in resolved:
                resolved.append(canonical_target)
    return result


def _validate_pricing_windows(route: dict[str, Any]) -> None:
    """Reject a rate window that can never reach a lower price.

    Equal start and end times denote a full local day. A surcharge over that whole day makes
    flexible jobs defer forever, because there is no cheaper recurring instant to wake for.
    """
    pricing = route.get("pricing") or {}
    periods = pricing.get("periods", ())
    root_windows = pricing.get("windows", ())
    for windows in [root_windows, *(period.get("windows", ()) for period in periods)]:
        for window in windows:
            try:
                start = str(window["start"])
                end = str(window["end"])
                multiplier = float(window["multiplier"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"route {route.get('route_id', route.get('model'))!r} "
                    "has invalid pricing window"
                ) from exc
            if not math.isfinite(multiplier):
                raise ValueError(
                    f"route {route.get('route_id', route.get('model'))!r} "
                    "has non-finite pricing multiplier"
                )
            if start == end and multiplier > 1:
                raise ValueError(
                    f"route {route.get('route_id', route.get('model'))!r} "
                    "has a full-day pricing surcharge"
                )


def compile_limits(*, discover: list[str] | None = None) -> dict[str, Any]:
    if not INPUT_YAML.exists():
        raise FileNotFoundError(f"Input configuration missing: {INPUT_YAML}")

    with INPUT_YAML.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    if discover is not None and run_discovery(raw, discover):
        with INPUT_YAML.open("w", encoding="utf-8") as f:
            yaml.safe_dump(raw, f, sort_keys=False, default_flow_style=False)
        print(
            f"discovery: rewrote {INPUT_YAML.relative_to(REPO_ROOT)} -- review the diff "
            "(including comment loss from the YAML round-trip) before committing",
            file=sys.stderr,
        )

    raw_buffer = raw.get("token_estimate_buffer")
    if raw_buffer is None:
        raw_buffer = raw.get("token_usage_buffer")
    token_estimate_buffer = _validate_token_buffer(raw_buffer)

    raw_split_cap = raw.get("split_cap_multiplier")
    if raw_split_cap is None:
        raw_split_cap = raw.get("split_cap")
    split_cap_multiplier = _validate_split_cap(raw_split_cap)

    providers = raw.get("providers", {})
    for provider_name, provider_cfg in providers.items():
        if isinstance(provider_cfg, dict):
            if "ai_gateway_max_attempts" in provider_cfg:
                provider_cfg["ai_gateway_max_attempts"] = _validate_ai_gateway_max_attempts(
                    provider_cfg["ai_gateway_max_attempts"], provider_name
                )
            if "tpm" in provider_cfg and provider_cfg["tpm"] is not None:
                tpm_val = provider_cfg["tpm"]
                if (
                    isinstance(tpm_val, bool)
                    or not isinstance(tpm_val, (int, float))
                    or tpm_val <= 0
                    or not math.isfinite(tpm_val)
                ):
                    raise ValueError(
                        f"provider {provider_name} has invalid non-positive tpm: {tpm_val!r}"
                    )
    if token_estimate_buffer != 1.0 or split_cap_multiplier != 1.0:
        for provider_cfg in providers.values():
            if isinstance(provider_cfg, dict):
                if provider_cfg.get("rpm") is not None and split_cap_multiplier != 1.0:
                    provider_cfg["rpm"] = _scale_rate_limit(
                        provider_cfg["rpm"], split_cap_multiplier
                    )
                if provider_cfg.get("monthly_tpm") is not None:
                    provider_cfg["monthly_tpm"] = _scale_rate_limit(
                        provider_cfg["monthly_tpm"],
                        token_estimate_buffer * split_cap_multiplier,
                    )
                if provider_cfg.get("tpm") is not None:
                    provider_cfg["tpm"] = _scale_rate_limit(
                        provider_cfg["tpm"],
                        token_estimate_buffer * split_cap_multiplier,
                    )

    routes = raw.get("routes", [])
    structured_output_methods = _normalize_structured_output_methods(
        raw.get("structured_output_methods")
    )
    normalized_routes, routes_by_id, model_routes_map, model_aliases = _validated_routes(routes)
    model_routing = _validated_model_routing(
        raw.get("model_routing"),
        model_aliases=model_aliases,
        canonical_models=set(model_routes_map),
    )
    resolved_methods = _resolve_structured_output_methods(normalized_routes, providers)
    for route in normalized_routes:
        _validate_pricing_windows(route)
        if split_cap_multiplier != 1.0:
            if route.get("rpm") is not None:
                route["rpm"] = _scale_rate_limit(route["rpm"], split_cap_multiplier)
            if route.get("rpd") is not None:
                route["rpd"] = _scale_rate_limit(route["rpd"], split_cap_multiplier)
        if route.get("tpm") is not None and (
            token_estimate_buffer != 1.0 or split_cap_multiplier != 1.0
        ):
            route["tpm"] = _scale_rate_limit(
                route["tpm"], token_estimate_buffer * split_cap_multiplier
            )
        provider_cfg = providers.get(route.get("provider"), {})
        for limit_name in ("input_context_limit", "output_context_limit"):
            value = route.get(limit_name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 1:
                raise ValueError(
                    f"route {route.get('route_id', route.get('model'))!r} must declare a positive "
                    f"integer {limit_name}; provider defaults are not supported"
                )
        # Optional, opt-in only -- most providers tolerate a request above their configured `tpm`
        # (confirmed live for NVIDIA), so this must never be inferred/defaulted from `tpm` here.
        # Left unscaled by `token_estimate_buffer`/`split_cap_multiplier`: unlike `tpm`, this is a
        # hard fact about what a single request can get through, not our own dispatcher-coexistence
        # bookkeeping, and is already authored conservatively below the observed live boundary.
        hard_ceiling = route.get("hard_input_ceiling")
        if hard_ceiling is not None:
            if (
                isinstance(hard_ceiling, bool)
                or not isinstance(hard_ceiling, (int, float))
                or (hard_ceiling < 1)
            ):
                raise ValueError(
                    f"route {route.get('route_id', route.get('model'))!r} has an invalid "
                    f"hard_input_ceiling: {hard_ceiling!r}"
                )
            if hard_ceiling > route["input_context_limit"]:
                raise ValueError(
                    f"route {route.get('route_id', route.get('model'))!r} has "
                    f"hard_input_ceiling ({hard_ceiling}) above its own input_context_limit "
                    f"({route['input_context_limit']}); it can never bind and should be removed"
                )
            route["hard_input_ceiling"] = int(hard_ceiling)
        # Provider tokens per estimate unit (`estimate_tokens`' chars/4). Our estimate is one
        # tokenizer-agnostic heuristic; each model family's real tokenizer diverges from it by a
        # measured, model-specific ratio (2026-09-23, 1,727 paired B2 payload/result samples:
        # Gemma p95 1.16, Gemini 3.5 Flash Lite p95 2.15). Every size and pacing comparison
        # against this route's real limits multiplies the raw estimate by this ratio first, in
        # both the Python producers and the Worker. Absent means 1.0, today's behavior.
        ratio = route.get("input_token_ratio")
        if ratio is not None:
            if (
                isinstance(ratio, bool)
                or not isinstance(ratio, (int, float))
                or not math.isfinite(ratio)
                or not 0.25 <= ratio <= 8.0
            ):
                raise ValueError(
                    f"route {route.get('route_id', route.get('model'))!r} has an invalid "
                    f"input_token_ratio: {ratio!r} (expected a number from 0.25 to 8.0)"
                )
            route["input_token_ratio"] = float(ratio)
        # Rate probe characterization measurements (PR-5 / Initiative 20).
        obs_ceil = route.get("observed_input_ceiling")
        if obs_ceil is not None:
            if (
                isinstance(obs_ceil, bool)
                or not isinstance(obs_ceil, (int, float))
                or (obs_ceil < 1)
            ):
                raise ValueError(
                    f"route {route.get('route_id', route.get('model'))!r} has an invalid "
                    f"observed_input_ceiling: {obs_ceil!r}"
                )
            if obs_ceil > route["input_context_limit"]:
                raise ValueError(
                    f"route {route.get('route_id', route.get('model'))!r} has "
                    f"observed_input_ceiling ({obs_ceil}) above its own input_context_limit "
                    f"({route['input_context_limit']})"
                )
            route["observed_input_ceiling"] = int(obs_ceil)
            # Deliberately NOT promoted to `hard_input_ceiling`. An observation is evidence; a
            # hard ceiling is enforcement that makes a route permanently unserviceable for any
            # larger job (pacing.js's earliestSafeStart returns null, not "not yet"). Auto-promoting
            # the two meant a single bad probe run silently blocked five routes on 2026-09-09 --
            # including moonshotai/kimi-k3, the overflow route added specifically for jobs too
            # large for Gemini, which live-tested fine at 17,864 tokens against a recorded ceiling
            # of 1,000. review/45 §20.8 is explicit that observed values reach enforcement only
            # through a human-reviewed PR; promote by authoring `hard_input_ceiling` yourself.

        obs_rpm = route.get("observed_rpm")
        if obs_rpm is not None:
            if isinstance(obs_rpm, bool) or not isinstance(obs_rpm, (int, float)) or obs_rpm <= 0:
                raise ValueError(
                    f"route {route.get('route_id', route.get('model'))!r} has an invalid "
                    f"observed_rpm: {obs_rpm!r}"
                )
            route["observed_rpm"] = float(obs_rpm)
            # Consumed in ONE direction only: it may lower the effective `rpm`, never raise it.
            #
            # This is deliberately asymmetric, and the asymmetry is the whole safety argument.
            # Clamping DOWN means "the provider throttles us harder than we configured" -- acting
            # on it prevents 429s, and its worst case is a route that runs slower than it could.
            # Raising would mean betting a route can absorb more than its authored limit on the
            # strength of one probe run, whose worst case is a sustained overdrive into throttling
            # or a ban. Given a bad probe run had already turned `observed_input_ceiling` into a
            # total route block on 2026-09-09, an observation gets to make things safer on its own
            # and must go through a human to make them faster.
            #
            # No `max(1.0, ...)` floor (CodeRabbit, 2026-09-13; an earlier version of this
            # comment justified one as "no measurement may ever drive a limit to 0, because 0 is
            # this repository's paused convention"): the validation above already rejects
            # `obs_rpm <= 0` outright, so `route["observed_rpm"]` here is always strictly
            # positive -- there is no path through which this assignment could produce a 0. A
            # floor of 1.0 instead silently RAISED a genuinely fractional observation below 1.0
            # (e.g. declared 0.5, observed 0.2 -- both legitimate; both schedulers pace
            # fractional rpm correctly) back up by 5x, which is exactly the direction this
            # one-way clamp exists to forbid.
            declared_rpm = route.get("rpm")
            if declared_rpm is not None and route["observed_rpm"] < declared_rpm:
                route["rpm"] = route["observed_rpm"]

        obs_burst = route.get("observed_burst")
        if obs_burst is not None:
            if isinstance(obs_burst, bool) or not isinstance(obs_burst, int) or obs_burst < 0:
                raise ValueError(
                    f"route {route.get('route_id', route.get('model'))!r} has an invalid "
                    f"observed_burst: {obs_burst!r}"
                )
            route["observed_burst"] = int(obs_burst)

        obs_rec = route.get("observed_recovery_seconds")
        if obs_rec is not None:
            if isinstance(obs_rec, bool) or not isinstance(obs_rec, (int, float)) or obs_rec <= 0:
                raise ValueError(
                    f"route {route.get('route_id', route.get('model'))!r} has an invalid "
                    f"observed_recovery_seconds: {obs_rec!r}"
                )
            route["observed_recovery_seconds"] = float(obs_rec)

        ra_trust = route.get("retry_after_trustworthy")
        if ra_trust is not None:
            if not isinstance(ra_trust, bool):
                raise ValueError(
                    f"route {route.get('route_id', route.get('model'))!r} has an invalid "
                    f"retry_after_trustworthy: {ra_trust!r}"
                )
            route["retry_after_trustworthy"] = bool(ra_trust)

        up_default = route.get("upstream_429_default")
        if up_default is not None:
            if up_default not in (
                "upstream_capacity",
                "gateway_limit",
                "own_rpm",
                "own_tpm",
                "own_rpd",
                "unknown_429",
            ):
                raise ValueError(
                    f"route {route.get('route_id', route.get('model'))!r} has unknown "
                    f"upstream_429_default: {up_default!r}"
                )
            route["upstream_429_default"] = str(up_default)

        obs_on = route.get("observed_on")
        if obs_on is not None:
            route["observed_on"] = str(obs_on)
        _validate_request_params(route)
        _validate_reasoning_controls(route)
        method_name, method_source, verified_on = resolved_methods[route["route_id"]]
        method = structured_output_methods[method_name]
        route.update(
            {
                "structured_output_method": method_name,
                "structured_output_method_source": method_source,
                "structured_output_verified_on": verified_on,
                "structured_output_response_format": method["response_format"],
                "structured_output_include_schema_in_prompt": method["include_schema_in_prompt"],
                "structured_output_schema_strip_keys": method["strip_schema_keys"],
            }
        )
        if provider_cfg.get("ai_gateway_max_attempts") is not None:
            route["ai_gateway_max_attempts"] = provider_cfg["ai_gateway_max_attempts"]
        route["input_context_limit"] = int(route["input_context_limit"])
        route["output_context_limit"] = int(route["output_context_limit"])

    compiled = {
        "_metadata": {
            "source": str(INPUT_YAML.relative_to(REPO_ROOT)),
            "routes_count": len(normalized_routes),
            "providers_count": len(providers),
            "token_estimate_buffer": token_estimate_buffer,
            "split_cap_multiplier": split_cap_multiplier,
        },
        "providers": providers,
        "structured_output_methods": structured_output_methods,
        "routes": normalized_routes,
        "routes_by_id": routes_by_id,
        "model_routes_map": model_routes_map,
        "model_aliases": model_aliases,
        "model_routing": model_routing,
    }
    return compiled


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--discover",
        nargs="*",
        default=None,
        metavar="PROVIDER",
        help=(
            "Opt-in, maintainer-run only: refresh config/provider_limits.yml with each named "
            "provider's live discovery endpoint before compiling (bare --discover covers every "
            "provider with a discovery block). Never pass this in the deploy workflow -- see the "
            "module docstring."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    compiled = compile_limits(discover=args.discover)
    worker_catalog = _worker_catalog(compiled)
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_JSON.open("w", encoding="utf-8") as f:
        json.dump(worker_catalog, f, indent=2, default=_json_default)
    V2_OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    with V2_OUTPUT_JSON.open("w", encoding="utf-8") as f:
        json.dump(worker_catalog, f, indent=2, default=_json_default)
    with PYTHON_OUTPUT_JSON.open("w", encoding="utf-8") as f:
        json.dump(_python_routes(compiled), f, indent=2, default=_json_default)
    rel_out = OUTPUT_JSON.relative_to(REPO_ROOT)
    rel_v2_out = V2_OUTPUT_JSON.relative_to(REPO_ROOT)
    print(
        f"Successfully compiled {compiled['_metadata']['routes_count']} routes "
        f"across {compiled['_metadata']['providers_count']} providers to {rel_out}, "
        f"{rel_v2_out}, and {PYTHON_OUTPUT_JSON.relative_to(REPO_ROOT)}"
    )


if __name__ == "__main__":
    main()
