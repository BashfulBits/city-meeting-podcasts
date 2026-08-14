"""Probe configured OpenAI-compatible LLM routes with a fixed, non-sensitive prompt.

This is an operational compatibility check, not a benchmark. It never reads queue objects,
meeting records, or prompts from storage. Each configured physical route gets one plain request
and one structured JSON request when its API-key environment variable is available.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
ROUTES_JSON = REPO_ROOT / "citypods" / "compute" / "llm_routes.json"
PROMPT = 'Reply with exactly one JSON object containing a single key "ok" with boolean true.'
SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


def _error(
    response: requests.Response | None = None, exc: BaseException | None = None
) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False}
    if response is not None:
        result["status"] = response.status_code
        try:
            body = response.json()
        except ValueError:
            body = {}
        body_map = body if isinstance(body, dict) else {}
        error = body_map.get("error", {})
        if isinstance(error, dict):
            result["provider_code"] = error.get("code") or error.get("type")
            message = error.get("message") or body_map.get("message") or ""
            result["provider_message"] = (
                json.dumps(message, sort_keys=True)
                if isinstance(message, (dict, list))
                else str(message)
            )[:300]
        elif isinstance(error, list):
            result["provider_message"] = json.dumps(error, sort_keys=True)[:300]
        elif isinstance(error, str):
            result["provider_message"] = error[:300]
        else:
            result["provider_message"] = str(body_map.get("message") or "")[:300]
    if exc is not None:
        result["exception_type"] = type(exc).__name__
        result["exception_message"] = str(exc)[:300]
    return result


def _request(route: dict[str, Any], *, structured: bool) -> dict[str, Any]:
    api_key_env = str(route.get("api_key_env") or "")
    api_key = os.environ.get(api_key_env)
    if not api_key:
        return {"skipped": True, "reason": f"missing {api_key_env or 'api key env'}"}

    payload: dict[str, Any] = {
        "model": route["upstream_model"],
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": 64,
        "temperature": 0,
    }
    if structured:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "probe", "strict": True, "schema": SCHEMA},
        }
    else:
        payload["response_format"] = {"type": "json_object"}

    url = f"{str(route['api_base']).rstrip('/')}/{str(route['chat_path']).lstrip('/')}"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if route.get("provider") == "openrouter":
        headers["HTTP-Referer"] = "https://citypodcasts.org"
        headers["X-Title"] = "Citypods route compatibility probe"
    started = time.monotonic()
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
    except requests.RequestException as exc:
        return _error(exc=exc)
    result: dict[str, Any] = {
        "ok": response.ok,
        "status": response.status_code,
        "latency_ms": round((time.monotonic() - started) * 1000),
    }
    if not response.ok:
        result.update(_error(response=response))
    return result


def _probe_route(route: dict[str, Any]) -> dict[str, Any]:
    return {
        "route_id": route["route_id"],
        "provider": route["provider"],
        "model": route["upstream_model"],
        "input_context_limit": route["input_context_limit"],
        "output_context_limit": route["output_context_limit"],
        "plain_json_object": _request(route, structured=False),
        "json_schema": _request(route, structured=True),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider",
        action="append",
        dest="providers",
        help=(
            "Provider to probe; repeat for multiple providers (default: all configured providers)."
        ),
    )
    parser.add_argument(
        "--route-id",
        action="append",
        dest="route_ids",
        help="Route ID to probe; repeat for targeted retries (default: all routes).",
    )
    parser.add_argument("--max-workers", type=int, default=4)
    args = parser.parse_args(argv)
    providers = set(args.providers or ())
    route_ids = set(args.route_ids or ())
    with ROUTES_JSON.open(encoding="utf-8") as handle:
        catalog = json.load(handle)
    routes = [
        route
        for route in catalog["routes"]
        if (not providers or route["provider"] in providers)
        and (not route_ids or route["route_id"] in route_ids)
    ]
    if not routes:
        parser.error("no configured routes matched --provider/--route-id")
    with ThreadPoolExecutor(max_workers=max(1, args.max_workers)) as executor:
        futures = {executor.submit(_probe_route, route): route for route in routes}
        results = [future.result() for future in as_completed(futures)]
    for result in sorted(results, key=lambda item: item["route_id"]):
        print("llm-route-probe: " + json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
