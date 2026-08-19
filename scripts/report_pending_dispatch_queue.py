#!/usr/bin/env python
"""Read-only report on the v1 LLM dispatch Worker's pending queue order and route eligibility.

Answers one operator question: is a job stuck only because every route its model can reach is
currently paused/blocked (e.g. Mistral's `rpd: 0` monthly-budget pause), or is it sitting behind
other jobs in R2's lexicographic `ready/<available_at>-<priority>-<created_at>-<id>.json` key
order for no route-availability reason at all? This mirrors (in Python, read-only) the exact
selection logic `workers/llm-dispatch-proxy/src/index.js` uses at dispatch time:
``readyKey``/``loadReadyHeads`` for queue order, ``canonicalModelName``/``model_routes_map`` for a
job's candidate routes, and ``routes_by_id``/``state/dispatch_budget.json`` for each route's
current capacity. It never writes to R2 -- no marker relocation, no requeue, no ledger mutation.

Required environment variables (same as ``reindex_llm_dispatch_queue.py``):
  CLOUDFLARE_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY

Optional:
  R2_ENDPOINT  jurisdiction-specific S3 endpoint override
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if __package__:
    from scripts.reindex_llm_dispatch_queue import _client
else:  # `python scripts/report_pending_dispatch_queue.py` from Actions.
    from reindex_llm_dispatch_queue import _client  # type: ignore[import-not-found]

READY_PREFIX = "ready/"
BUDGET_KEY = "state/dispatch_budget.json"
DEFAULT_BUCKET = "citypods-llm-dispatch"
DEFAULT_WORKERS = 8
# Matches the Worker's DEFAULT_READY_LOOKAHEAD -- a single scheduled tick only ever looks at this
# many markers, so a job past this position depends on earlier markers being dispatched/relocated
# first, not just on its own route's availability.
DEFAULT_READY_LOOKAHEAD = 16
DISPATCH_LIMITS_PATH = (
    Path(__file__).resolve().parent.parent
    / "workers"
    / "llm-dispatch-proxy"
    / "src"
    / "dispatch_limits.json"
)


def _load_dispatch_limits() -> dict[str, Any]:
    with DISPATCH_LIMITS_PATH.open() as fh:
        return json.load(fh)


def _canonical_model_name(model: str, dispatch_limits: dict[str, Any]) -> str:
    aliases = dispatch_limits.get("model_aliases") or {}
    seen: set[str] = set()
    current = model
    while isinstance(aliases.get(current), str) and current not in seen:
        seen.add(current)
        current = aliases[current]
    return current


def _candidate_route_ids(models: list[str], dispatch_limits: dict[str, Any]) -> list[str]:
    """Union of `model_routes_map` route IDs for every model in `models`, in order, deduped --
    mirrors `selectRoute`'s `[...new Set(models)]` iteration over `allowed_models` (or the single
    `model` field when a job has no `allowed_models`)."""
    route_map = dispatch_limits.get("model_routes_map") or {}
    seen: set[str] = set()
    ordered: list[str] = []
    for model in models:
        canonical = _canonical_model_name(model, dispatch_limits)
        for route_id in route_map.get(canonical, []):
            if route_id not in seen:
                seen.add(route_id)
                ordered.append(route_id)
    return ordered


def _route_status(
    route_id: str, dispatch_limits: dict[str, Any], budget: dict[str, Any], now: datetime
) -> dict[str, Any]:
    route = (dispatch_limits.get("routes_by_id") or {}).get(route_id) or {}
    entry = (budget.get("routes") or {}).get(route_id) or {}
    provider = route.get("provider", "unknown")
    rpd = route.get("rpd")

    blocked_until = entry.get("blocked_until")
    blocked_until_dt: datetime | None = None
    if isinstance(blocked_until, str) and blocked_until:
        try:
            blocked_until_dt = datetime.fromisoformat(blocked_until.replace("Z", "+00:00"))
        except ValueError:
            blocked_until_dt = None

    if rpd == 0:
        state = "paused_rpd0"
    elif blocked_until_dt is not None and blocked_until_dt > now:
        state = "blocked_until"
    else:
        # requests_day/requests_day_key comparison is UTC-approximate: routes may reset in a
        # provider-specific `reset_timezone`, which this report does not replicate. Treat this as
        # informational only, never as a definitive "still has capacity" claim.
        state = "reported_available"
        day_key = entry.get("requests_day_key")
        today_key_utc = now.strftime("%Y-%m-%d")
        if rpd is not None and day_key == today_key_utc and entry.get("requests_day", 0) >= rpd:
            state = "day_capped_utc_approx"

    return {
        "route_id": route_id,
        "provider": provider,
        "rpd": rpd,
        "requests_day": entry.get("requests_day"),
        "blocked_until": blocked_until,
        "state": state,
    }


def _list_ready_keys(client: Any, bucket: str, limit: int) -> list[str]:
    paginator = client.get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(
        Bucket=bucket, Prefix=READY_PREFIX, PaginationConfig={"MaxItems": limit}
    ):
        keys.extend(item["Key"] for item in page.get("Contents", []))
        if len(keys) >= limit:
            break
    return keys[:limit]


def _head_marker(client: Any, bucket: str, key: str) -> dict[str, Any] | None:
    try:
        head = client.head_object(Bucket=bucket, Key=key)
    except Exception as exc:  # noqa: BLE001 -- one unreadable marker must not sink the report
        print(f"warning: head_object failed for {key}: {exc}", file=sys.stderr)
        return None
    meta = head.get("Metadata") or {}
    if not meta.get("id") or not meta.get("model"):
        return None
    policy_raw = meta.get("policy") or "{}"
    try:
        policy = json.loads(policy_raw)
        if not isinstance(policy, dict):
            policy = {}
    except json.JSONDecodeError:
        policy = {}
    return {
        "key": key,
        "id": meta["id"],
        "model": meta["model"],
        "created_at": meta.get("created_at", ""),
        "available_at": meta.get("available_at", ""),
        "policy": policy,
    }


def build_report(
    client: Any,
    bucket: str,
    *,
    limit: int,
    workers: int,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    now = now or datetime.now(UTC)
    dispatch_limits = _load_dispatch_limits()

    try:
        budget_body = client.get_object(Bucket=bucket, Key=BUDGET_KEY)["Body"].read()
        budget = json.loads(budget_body)
    except client.exceptions.NoSuchKey:
        budget = {"routes": {}}

    keys = _list_ready_keys(client, bucket, limit)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        markers = list(executor.map(lambda k: _head_marker(client, bucket, k), keys))

    rows: list[dict[str, Any]] = []
    for position, marker in enumerate(markers, start=1):
        if marker is None:
            continue
        models = marker["policy"].get("allowed_models") or [marker["model"]]
        route_ids = _candidate_route_ids(models, dispatch_limits)
        candidates = [_route_status(rid, dispatch_limits, budget, now) for rid in route_ids]
        providers = {c["provider"] for c in candidates}
        any_open = any(c["state"] == "reported_available" for c in candidates)
        all_mistral = bool(providers) and providers == {"mistral"}
        rows.append(
            {
                "position": position,
                "within_lookahead": position <= DEFAULT_READY_LOOKAHEAD,
                "id": marker["id"],
                "model": marker["model"],
                "created_at": marker["created_at"],
                "available_at": marker["available_at"],
                "priority": _priority_label(marker["policy"]),
                "candidates": candidates,
                "mistral_only": all_mistral,
                "verdict": "ELIGIBLE" if any_open else "STUCK",
            }
        )
    return rows


def _priority_label(policy: dict[str, Any]) -> str:
    # Mirrors the Worker's own `readyPriority()` -- deriving the label from `policy` (already
    # parsed off the marker's metadata) rather than re-splitting the `ready/` key string, whose
    # `<priority>` segment ("0-urgent"/"1-fast"/"2-long") itself contains a dash and so can't be
    # isolated by position alone.
    if policy.get("submit_next"):
        return "0-urgent"
    return "2-long" if policy.get("timeout_class") == "long" else "1-fast"


def _print_table(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        candidate_summary = (
            ", ".join(f"{c['route_id']}[{c['provider']}:{c['state']}]" for c in row["candidates"])
            or "(no configured route)"
        )
        lookahead_flag = "" if row["within_lookahead"] else "  [beyond 16-marker lookahead]"
        print(
            f"{row['position']:>4}  {row['verdict']:<8} {row['priority']:<9} "
            f"{row['id']:<40} model={row['model']}{lookahead_flag}"
        )
        print(f"        routes: {candidate_summary}")


def _print_summary(rows: list[dict[str, Any]]) -> None:
    total = len(rows)
    stuck = [r for r in rows if r["verdict"] == "STUCK"]
    eligible = [r for r in rows if r["verdict"] == "ELIGIBLE"]
    mistral_only_stuck = [r for r in stuck if r["mistral_only"]]

    print("\n--- summary ---")
    print(f"listed: {total} pending ready markers")
    print(f"STUCK (every candidate route currently paused/blocked): {len(stuck)}")
    print(f"  of which mistral-only: {len(mistral_only_stuck)}")
    print(f"ELIGIBLE (at least one candidate route reports available): {len(eligible)}")

    # The direct answer to "is mistral blocking other jobs by queue position": find the longest
    # unbroken run of STUCK jobs starting at position 1, and report the first ELIGIBLE job's
    # position. Per the Worker's loadReadyHeads/dispatchBatch loop, a STUCK head does NOT stop the
    # scan within one lookahead window (16 markers) -- it's skipped in place (or relocated once
    # blocked past DEFER_IN_PLACE_SECONDS) and the loop advances to the next marker. So a STUCK run
    # only actually blocks dispatch if it fills the entire 16-marker lookahead window.
    head_run = 0
    for row in rows:
        if row["verdict"] != "STUCK":
            break
        head_run += 1
    first_eligible = next((r for r in rows if r["verdict"] == "ELIGIBLE"), None)

    print(f"\nconsecutive STUCK jobs at the head of the queue: {head_run}")
    if first_eligible:
        print(
            f"first ELIGIBLE job is at position {first_eligible['position']} "
            f"(id={first_eligible['id']}, model={first_eligible['model']})"
        )
        if first_eligible["position"] <= DEFAULT_READY_LOOKAHEAD:
            print(
                "-> within the Worker's 16-marker lookahead window: a scheduled tick reaches and "
                "can dispatch it even with STUCK jobs ahead of it in queue order."
            )
        else:
            print(
                f"-> BEYOND the Worker's {DEFAULT_READY_LOOKAHEAD}-marker lookahead window: this "
                "job is not examined at all until the STUCK jobs ahead of it are dispatched or "
                "relocated out of the head. This is queue-order blocking, not route exhaustion."
            )
    elif stuck:
        print("-> every listed pending job is STUCK; nothing in this window is dispatchable.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument(
        "--limit",
        type=int,
        default=500,
        help="max ready/ markers to inspect, in queue order (default: %(default)s)",
    )
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON instead of a table"
    )
    args = parser.parse_args()

    required = ("CLOUDFLARE_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        parser.error(f"missing required environment variables: {', '.join(missing)}")

    client = _client(workers=args.workers)
    rows = build_report(client, args.bucket, limit=args.limit, workers=args.workers)

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    _print_table(rows)
    _print_summary(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
