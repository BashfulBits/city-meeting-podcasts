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
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
INPUT_YAML = REPO_ROOT / "config" / "provider_limits.yml"
OUTPUT_JSON = REPO_ROOT / "workers" / "llm-dispatch-proxy" / "src" / "dispatch_limits.json"
PYTHON_OUTPUT_JSON = REPO_ROOT / "citypods" / "compute" / "llm_routes.json"


def _direct_model(provider: str, upstream_model: str) -> str:
    """Return the LiteLLM model selector for a compiled provider route.

    Kilo and OpenCode expose OpenAI-compatible gateways rather than stable LiteLLM provider
    adapters.  Selecting the OpenAI adapter and supplying the compiled ``api_base`` keeps those
    routes usable directly without teaching the scheduler provider-specific URL logic.
    """
    if provider in {"kilo", "opencode"}:
        return f"openai/{upstream_model}"
    return f"{provider}/{upstream_model}"


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
        account = next(
            (
                account
                for account in provider_cfg.get("accounts", [])
                if account.get("id") == source.get("account_id")
            ),
            None,
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
                "chat_path": provider_cfg.get("chat_path", "/v1/chat/completions"),
                "api_key_env": (account or {}).get("api_key_env", ""),
                "reset_timezone": source.get(
                    "reset_timezone", provider_cfg.get("reset_timezone", "UTC")
                ),
            }
        )
        routes.append(route)
    return {"_metadata": compiled["_metadata"], "routes": routes}


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


def _validated_routes(routes: list[Any]) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]]]:
    """Pre-index routes by canonical model / route_id for O(1) Worker lookup.

    Raises a clear ``ValueError`` naming the offending route index for a hand-authored YAML route
    missing either required key, instead of letting an opaque ``KeyError`` surface right before
    ``wrangler deploy``.
    """
    routes_by_id: dict[str, dict[str, Any]] = {}
    model_routes_map: dict[str, list[str]] = {}
    for index, route in enumerate(routes):
        try:
            r_id = route["route_id"]
            c_model = route["model"]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"route #{index} is missing 'route_id' or 'model': {route!r}") from exc
        if r_id in routes_by_id:
            # Same class of bug already fixed for discovery (`_openrouter_routes`'s
            # `existing_route_ids` dedup): two hand-authored routes sharing a route_id would
            # otherwise silently collapse in `routes_by_id` while both stay in `routes` and
            # `model_routes_map`, overcounting `_metadata.routes_count` and making one route
            # unreachable (CodeRabbit, review/41).
            raise ValueError(f"route #{index} redeclares route_id {r_id!r}")
        routes_by_id[r_id] = route
        model_routes_map.setdefault(c_model, []).append(r_id)
    return routes_by_id, model_routes_map


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

    providers = raw.get("providers", {})
    routes = raw.get("routes", [])
    routes_by_id, model_routes_map = _validated_routes(routes)

    compiled = {
        "_metadata": {
            "source": str(INPUT_YAML.relative_to(REPO_ROOT)),
            "routes_count": len(routes),
            "providers_count": len(providers),
        },
        "providers": providers,
        "routes": routes,
        "routes_by_id": routes_by_id,
        "model_routes_map": model_routes_map,
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
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_JSON.open("w", encoding="utf-8") as f:
        json.dump(compiled, f, indent=2)
    with PYTHON_OUTPUT_JSON.open("w", encoding="utf-8") as f:
        json.dump(_python_routes(compiled), f, indent=2)
    rel_out = OUTPUT_JSON.relative_to(REPO_ROOT)
    print(
        f"Successfully compiled {compiled['_metadata']['routes_count']} routes "
        f"across {compiled['_metadata']['providers_count']} providers to {rel_out} and "
        f"{PYTHON_OUTPUT_JSON.relative_to(REPO_ROOT)}"
    )


if __name__ == "__main__":
    main()
