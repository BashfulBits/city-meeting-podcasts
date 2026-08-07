#!/usr/bin/env python3
"""Compile config/provider_limits.yml into workers/llm-dispatch-proxy/src/dispatch_limits.json.

Statically parses provider accounts, models, and rate limits, and dynamically queries API endpoints
(e.g., OpenRouter GET https://openrouter.ai/api/v1/models) to auto-discover models and pricing.
Emits pre-indexed lookup maps for sub-10ms Cloudflare Worker execution.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
INPUT_YAML = REPO_ROOT / "config" / "provider_limits.yml"
OUTPUT_JSON = REPO_ROOT / "workers" / "llm-dispatch-proxy" / "src" / "dispatch_limits.json"


def fetch_openrouter_models() -> list[dict[str, Any]]:
    """Best-effort auto-discovery of OpenRouter models and pricing."""
    url = "https://openrouter.ai/api/v1/models"
    req = Request(url, headers={"User-Agent": "citypods-limits-compiler/1.0"})
    try:
        with urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("data", [])
    except Exception as exc:
        print(f"Notice: OpenRouter model auto-discovery skipped ({exc})", file=sys.stderr)
    return []


def compile_limits() -> dict[str, Any]:
    if not INPUT_YAML.exists():
        raise FileNotFoundError(f"Input configuration missing: {INPUT_YAML}")

    with INPUT_YAML.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    providers = raw.get("providers", {})
    routes = raw.get("routes", [])

    # Process OpenRouter auto-discovered models if openrouter provider registered
    if "openrouter" in providers:
        discovered = fetch_openrouter_models()
        existing_route_ids = {r.get("route_id") for r in routes}
        for item in discovered:
            m_id = item.get("id")
            if not m_id:
                continue
            canonical_model = f"openrouter/{m_id}"
            route_id = f"openrouter_{m_id.replace('/', '_').replace(':', '_')}"
            if route_id in existing_route_ids:
                continue

            pricing = item.get("pricing", {})
            try:
                inp_price = float(pricing.get("prompt", 0) or 0)
                out_price = float(pricing.get("completion", 0) or 0)
            except (TypeError, ValueError):
                inp_price, out_price = 0.0, 0.0

            is_free = inp_price == 0.0 and out_price == 0.0

            routes.append(
                {
                    "route_id": route_id,
                    "model": canonical_model,
                    "provider": "openrouter",
                    "upstream_model": m_id,
                    "account_id": "primary",
                    "free": is_free,
                    "input_per_token": inp_price,
                    "output_per_token": out_price,
                    "auto_discovered": True,
                }
            )

    # Pre-index routes by canonical model for O(1) lookup in Worker
    model_routes_map: dict[str, list[str]] = {}
    routes_by_id: dict[str, dict[str, Any]] = {}

    for route in routes:
        r_id = route["route_id"]
        c_model = route["model"]
        routes_by_id[r_id] = route
        if c_model not in model_routes_map:
            model_routes_map[c_model] = []
        model_routes_map[c_model].append(r_id)

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


def main() -> None:
    compiled = compile_limits()
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_JSON.open("w", encoding="utf-8") as f:
        json.dump(compiled, f, indent=2)
    rel_out = OUTPUT_JSON.relative_to(REPO_ROOT)
    print(
        f"Successfully compiled {compiled['_metadata']['routes_count']} routes "
        f"across {compiled['_metadata']['providers_count']} providers to {rel_out}"
    )


if __name__ == "__main__":
    main()
