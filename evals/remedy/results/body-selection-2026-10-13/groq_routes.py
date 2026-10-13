"""Evaluation-only Groq capability copies and conservative shared-token headroom."""

import json
import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from citypods.compute.llm_policy import ROUTE_CANDIDATES, ROUTE_REGISTRY
from citypods.compute.llm_policy import ROUTES as LOGICAL_ROUTES

CANDIDATES = {
    "gptoss": ("groq_gpt_oss_120b_primary", "high"),
    "qwen38": ("groq_qwen_3_8_27b_primary", "high"),
}
CONTROLS_SOURCE = "https://console.groq.com/docs/api-reference"
LIMITS_SOURCE = "https://console.groq.com/docs/rate-limits"


def route_for(candidate):
    original = ROUTE_REGISTRY[CANDIDATES[candidate][0]]
    params = json.loads(original.request_params_json or "{}")
    params["reasoning_format"] = "parsed"
    controls = json.loads(original.reasoning_controls_json or "{}")
    controls["high"] = {"reasoning_effort": "high"}
    route = replace(
        original,
        quota=replace(original.quota, tpd=min(original.quota.tpd or 180000, 180000)),
        output_context_limit=min(original.output_context_limit, 16384),
        request_params_json=json.dumps(params, sort_keys=True),
        reasoning_controls_json=json.dumps(controls, sort_keys=True),
    )
    ROUTE_REGISTRY[route.route_id] = route
    ROUTE_CANDIDATES[route.model] = (route,)
    LOGICAL_ROUTES[route.model] = route
    return route


def token_headroom(transport, client, storage, route, request_tokens):
    stats = client._request("GET", "v2/stats?detail=1&limit=100")
    now = datetime.now(UTC)
    observed = stats.get("now")
    if type(observed) is not int or not 0 <= now.timestamp() * 1000 - observed <= 60000:
        raise ValueError("Groq Worker token usage is stale or invalid")
    inventory = stats.get("routes", {})
    rows = inventory.get("all")
    if (
        not isinstance(rows, list)
        or type(inventory.get("total")) is not int
        or len(rows) != inventory["total"]
    ):
        raise ValueError("Groq Worker token inventory is incomplete")
    matched = [r for r in rows if r.get("route_id") == route.route_id]
    if len(matched) != 1:
        raise ValueError("Groq Worker token usage is missing or duplicated")
    worker_used = matched[0].get("tpd_used")
    if (
        isinstance(worker_used, bool)
        or not isinstance(worker_used, (int, float))
        or not math.isfinite(worker_used)
        or worker_used < 0
    ):
        raise ValueError("Groq Worker token usage is invalid")
    budget, _ = transport.load_llm_budget_cas(storage)
    local = budget.routes.get(route.route_id) or budget.routes.get(route.model)
    local_used = 0
    if local:
        local_used = local.tokens_day
        if not math.isfinite(local_used) or local_used < 0:
            raise ValueError("Groq local token usage is invalid")
        if local.tokens_day_updated_at:
            updated = datetime.fromisoformat(local.tokens_day_updated_at)
            if updated.tzinfo is None or updated > now:
                raise ValueError("Groq local token usage timestamp is invalid")
            elapsed = (now - updated).total_seconds()
            local_used = max(0, local_used - elapsed * route.quota.tpd / 86400)
    # Stores may overlap; summing errs toward deferral instead of missing direct-call usage.
    used = worker_used + local_used
    reserve = math.ceil(route.quota.tpd * 0.2)
    remaining = route.quota.tpd - used
    enough = remaining >= request_tokens + reserve
    result = {
        "kind": "available" if enough else "daily_quota",
        "worker_tokens_used": worker_used,
        "local_tokens_used": local_used,
        "conservative_tokens_used": used,
        "remaining_tokens": max(0, remaining),
        "request_token_reservation": request_tokens,
        "production_token_reserve": reserve,
    }
    if not enough:
        # Recheck advisory rolling debt; shared accounting still gates the actual request.
        result["next_eligible_at"] = (now + timedelta(minutes=15)).isoformat()
    return enough, result
