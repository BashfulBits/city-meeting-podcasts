"""Run approved frozen challenge cases once, with immutable observations and shared capacity."""

import argparse
import fcntl
import json
import math
import os
import time
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from recovery import (
    MODELS,
    candidate_plan,
    cooldown_until,
    logical_candidate,
    next_reset,
    provider_error,
    read_history,
    timestamp,
)

from citypods.compute.base import InferenceJob
from citypods.compute.llm import LiteLLMBackend, LLMBackendConfig
from citypods.compute.llm_budget import daily_reset_key, load_llm_budget_cas
from citypods.compute.llm_dispatch_pause import DispatchPauseClient, Selection, paused
from citypods.compute.llm_policy import (
    ROUTE_CANDIDATES,
    ROUTE_REGISTRY,
    LLMRequestPolicy,
    estimate_tokens,
)
from citypods.compute.llm_policy import ROUTES as LOGICAL_ROUTES
from citypods.compute.structured import parse_structured_json, register_response_model
from citypods.config import load_site_config
from citypods.remedy_evaluation import (
    Manifest,
    RemedyEvalAnswer,
    canonical_hash,
    case_messages,
    prompt_for_mode,
    validate_answer,
)
from citypods.storage import make_storage

ROOT = Path(__file__).resolve().parents[4]
OUT = Path(__file__).resolve().parent
FROZEN = ROOT / "evals/remedy/holdout/backlog-varied-2026-10-11/approved-v2"
CANDIDATES = {
    "flashlite": ("gemini_3_5_flash_lite_primary", None),
    "gemini37": ("gemini_3_7_flash_primary", "high"),
    "gemini37_secondary": ("gemini_3_7_flash_secondary", "high"),
    "gemini38": ("gemini_3_8_flash_primary", "high"),
    "gemini38_secondary": ("gemini_3_8_flash_secondary", "high"),
    "kimi": ("nvidia_kimi_k3_free", "max"),
    "glm_full": ("nvidia_glm_5_3_eval", None),
    "glm_flash": ("orcarouter_zai_glm_5_3_flash_free", None),
    "astra": ("beatapi_gpt_6_astra_free", None),
    "sol": ("beatapi_gpt_6_1_sol_free", None),
    "deepseek": ("beatapi_deepseek_v4_1_flash_free", None),
}


def route_for(candidate):
    rid, _ = CANDIDATES[candidate]
    if candidate == "glm_full":
        template = ROUTE_REGISTRY["nvidia_kimi_k3_free"]
        route = replace(
            template,
            model="nvidia/z-ai/glm-5.3",
            route_id=rid,
            upstream_model="z-ai/glm-5.3",
            direct_model="openai/z-ai/glm-5.3",
            quota=replace(template.quota, rpm=1, rpd=None, tpm=None, concurrency=1),
            input_context_limit=32768,
            output_context_limit=16384,
            hard_input_ceiling=None,
            reasoning_controls_json="",
            request_params_json="",
            structured_output_method="prompt_only",
            structured_output_response_format="none",
            structured_output_include_schema_in_prompt=True,
        )
        from citypods.compute import llm

        llm.SUPPORTED_MODELS = llm.SUPPORTED_MODELS | {route.model}
    else:
        route = ROUTE_REGISTRY[rid]
    if route.provider == "beatapi":
        route = replace(route, direct_model="openai/" + route.upstream_model)
    ROUTE_REGISTRY[rid] = route
    ROUTE_CANDIDATES[route.model] = (route,)
    LOGICAL_ROUTES[route.model] = route
    return route


def used_today(budget, route):
    key = route.route_id if route.route_id in budget.routes else route.model
    entry = budget.routes.get(key)
    used = 0
    if entry and entry.requests_day_key == daily_reset_key(
        datetime.now(UTC), route.quota.reset_timezone
    ):
        used = entry.requests_day
    return key, used


def headroom(client, storage, route):
    now = datetime.now(UTC)
    budget, _ = load_llm_budget_cas(storage)
    # Gemini's primary and secondary credentials belong to separate configured projects.
    # Reserve and account against the selected account's route quota so the evaluator can use
    # both accounts without scoring them as separate models. Provider-wide in-flight protection
    # below still keeps this research work out of the way of active production calls.
    peers = [route]
    known = {}
    for peer in peers:
        key, local = used_today(budget, peer)
        if peer.route_id == "nvidia_glm_5_3_eval":
            # This candidate is deliberately ephemeral and is not in the Worker catalog.
            known[key] = max(known.get(key, 0), local)
        else:
            status = client.status(Selection("route", peer.route_id))
            cell = status.get("routes", {}).get(peer.route_id)
            if cell is None:
                raise RuntimeError("Worker capacity evidence missing")
            if type(cell.get("rpd_used")) is not int or cell["rpd_used"] < 0:
                raise RuntimeError("Worker daily quota evidence invalid")
            known[key] = max(known.get(key, 0), local, cell["rpd_used"])
    if route.quota.rpd is not None:
        remaining = route.quota.rpd - sum(known.values())
        reserve = max(2, math.ceil(route.quota.rpd * 0.2))
        if remaining <= reserve:
            resets = [next_reset(now, route.quota.reset_timezone)]
            worker_reset = cell.get("rpd_resets_at")
            if type(worker_reset) is not int or worker_reset <= now.timestamp() * 1000:
                raise RuntimeError("Worker daily reset evidence invalid")
            resets.append(datetime.fromtimestamp(worker_reset / 1000, UTC))
            return False, {
                "kind": "daily_quota",
                "remaining": remaining,
                "production_reserve": reserve,
                "next_eligible_at": max(resets).isoformat(),
            }
    provider = client.status(Selection("provider", route.provider))
    if type(provider.get("in_flight")) is not int or provider["in_flight"] < 0:
        raise RuntimeError("Worker provider capacity evidence invalid")
    if provider["in_flight"] != 0:
        return False, {
            "kind": "provider_busy",
            "reason": "production provider in flight",
            "next_eligible_at": (now + timedelta(minutes=5)).isoformat(),
        }
    return True, {"kind": "available", "observed_usage": known, "provider_in_flight": 0}


def observations(manifest_hash, retry_failed):
    finished = set()
    failed = set()
    for path in (OUT / "batches").glob("*/*.json"):
        item = json.loads(path.read_text())
        if item.get("manifest_hash") != manifest_hash:
            raise ValueError("Prior observation manifest mismatch")
        logical_key = (logical_candidate(item["candidate"]), item["case_id"])
        if item.get("status") == "completed":
            finished.add(logical_key)
        elif item.get("status") == "failed":
            # A secondary credential may retry a failure from the primary.
            failed.add((item["candidate"], item["case_id"]))
    return finished, failed if not retry_failed else set()


def main():
    global OUT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("pilot", "full"), default="pilot")
    parser.add_argument("--max-calls", type=int, default=8)
    parser.add_argument("--candidate", choices=tuple(CANDIDATES), action="append")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--max-calls-per-candidate", type=int)
    parser.add_argument("--results-dir", type=Path, default=OUT)
    parser.add_argument("--run-started-at", type=timestamp)
    parser.add_argument("--stop-at", type=timestamp)
    args = parser.parse_args()
    OUT = args.results_dir.resolve()
    OUT.mkdir(parents=True, exist_ok=True)
    if not 1 <= args.max_calls <= 16:
        raise SystemExit("Batch cap must be 1–16 provider calls")
    if args.stage == "full" and not (OUT / "pilot-reviewed.json").exists():
        raise SystemExit("Full stage requires explicit reviewed pilot marker")
    candidates = args.candidate or list(MODELS)
    if any(c not in MODELS for c in candidates):
        raise SystemExit("Discontinued model is excluded from the approved comparison")
    if len(candidates) != len(set(candidates)):
        raise SystemExit("Candidate may be listed only once")
    if args.stage == "pilot" and len(candidates) != 1:
        raise SystemExit("Pilot runs select exactly one candidate for a balanced comparison")
    if args.stage == "full" and len(candidates) != 1:
        raise SystemExit("Full runs select exactly one candidate for fair comparison")
    per_candidate_cap = args.max_calls_per_candidate
    if per_candidate_cap is None:
        per_candidate_cap = args.max_calls
    if per_candidate_cap < 1:
        raise SystemExit("Per-candidate cap must be positive")
    lock = (OUT / "run.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("An approved evaluation batch is already running") from None
    manifest = Manifest.model_validate_json((FROZEN / "manifest.json").read_text())
    manifest_hash = canonical_hash(manifest.model_dump())
    pilot = json.loads((FROZEN / "pilot.json").read_text())["case_ids"]
    selected = [c for c in manifest.cases if args.stage == "full" or c.id in pilot]
    if args.stage == "pilot":
        selected.sort(key=lambda c: pilot.index(c.id))
    site = load_site_config(ROOT / "config/site_config.yml")
    storage = make_storage(site, site.get("base_url", ""), ROOT / site.get("output_dir", "docs"))
    if storage is None or not storage.cas_capable:
        raise SystemExit("Shared CAS quota storage unavailable")
    client = DispatchPauseClient()
    register_response_model("remedy-eval-v2", RemedyEvalAnswer)
    done, failed = observations(manifest_hash, args.retry_failed)
    history = read_history(OUT / "batches", manifest_hash, {c.id for c in manifest.cases})
    started_at = args.run_started_at or datetime.now(UTC)
    plan = candidate_plan(
        history, candidates[0], [c.id for c in selected], datetime.now(UTC), started_at
    )
    eligible = set(plan["eligible_case_ids"])
    selected.sort(key=lambda c: plan["eligible_case_ids"].index(c.id) if c.id in eligible else 9999)
    batch = OUT / "batches" / (datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8])
    batch.mkdir(parents=True)
    calls = 0
    calls_by_candidate = Counter()
    held = set()
    # Interleave candidates by case; do not spend a whole daily pool on an easy case prefix.
    for case in selected:
        for candidate in candidates:
            # Credential-specific Gemini routes are one logical model.  The
            # primary key's completed cases must also suppress secondary calls.
            pair = (logical_candidate(candidate), case.id)
            route_pair = (candidate, case.id)
            if (
                pair in done
                or case.id not in eligible
                or route_pair in failed
                or candidate in held
                or calls >= args.max_calls
                or calls_by_candidate[candidate] >= per_candidate_cap
            ):
                continue
            route = route_for(candidate)
            effort = CANDIDATES[candidate][1]
            timeout = 120 if route.provider == "gemini" else 600
            tokens = 4096 if route.provider == "gemini" else 16384
            if args.stop_at and datetime.now(UTC) + timedelta(seconds=timeout + 150) > args.stop_at:
                print("Batch stopped before controller deadline; no further calls", flush=True)
                return
            messages = case_messages(case, "claim_support")
            row = {
                "candidate": candidate,
                "case_id": case.id,
                "mode": "claim_support",
                "manifest_hash": manifest_hash,
                "qualified": False,
                "route_id": route.route_id,
                "reasoning": effort or "provider-default",
                "request_timeout": timeout,
                "output_token_cap": tokens,
                "provider_attempts": 0,
                "observed_at": datetime.now(UTC).isoformat(),
            }
            row.update(
                catalog_hash=canonical_hash(
                    json.loads((ROOT / "citypods/compute/llm_routes.json").read_text())
                ),
                prompt_hash=canonical_hash(prompt_for_mode("claim_support")),
                schema_hash=canonical_hash(RemedyEvalAnswer.model_json_schema()),
                case_input_hash=canonical_hash(case.model_dump()),
                route_settings_hash=canonical_hash(
                    {
                        "route_id": route.route_id,
                        "model": route.model,
                        "upstream": route.upstream_model,
                        "direct": route.direct_model,
                        "controls": route.reasoning_controls_json,
                        "request_params": route.request_params_json,
                        "rpm": route.quota.rpm,
                        "rpd": route.quota.rpd,
                        "input_context": route.input_context_limit,
                        "output_context": route.output_context_limit,
                    }
                ),
            )
            diagnostics = []
            started = time.monotonic()
            try:
                enough, capacity = headroom(client, storage, route)
                row["capacity"] = capacity
                if not enough:
                    row.update(
                        status="deferred",
                        reason=capacity["kind"],
                        next_eligible_at=capacity["next_eligible_at"],
                    )
                    held.add(candidate)
                else:
                    estimate = estimate_tokens(messages)
                    if route.hard_input_ceiling is not None and estimate > route.hard_input_ceiling:
                        raise RuntimeError("Frozen prompt exceeds route input ceiling")
                    if estimate + tokens > route.input_context_limit:
                        raise RuntimeError("Frozen prompt and output cap exceed route context")
                    if (
                        route.output_context_limit is not None
                        and tokens > route.output_context_limit
                    ):
                        raise RuntimeError("Output cap exceeds route output limit")
                    policy = LLMRequestPolicy(
                        allowed_models=(route.model,),
                        allowed_route_ids=(route.route_id,),
                        purpose="audit-remedy",
                        require_direct=True,
                        deadline_at=datetime.now(UTC)
                        + timedelta(seconds=150 if route.provider == "gemini" else 650),
                    )
                    job = InferenceJob(
                        task="tag",
                        recipe_hash=canonical_hash(
                            {
                                "manifest": manifest_hash,
                                "candidate": candidate,
                                "case": case.id,
                                "reasoning": effort,
                                "tokens": tokens,
                            }
                        ),
                        inputs={
                            "messages": messages,
                            "structured_output": "remedy-eval-v2",
                            "llm_policy": policy,
                            "reasoning_level": effort,
                            "max_tokens": tokens,
                            "timeout": timeout,
                            "num_retries": 0,
                            "max_provider_attempts": 1,
                        },
                    )
                    backend = LiteLLMBackend(LLMBackendConfig(model=route.model), storage=storage)
                    completion = backend._completion_fn()

                    def observed(
                        _row=row, _completion=completion, _diagnostics=diagnostics, **kwargs
                    ):
                        stamp = time.monotonic()
                        _row["attempt_id"] = uuid4().hex
                        intents = OUT / "intents"
                        intents.mkdir(exist_ok=True)
                        with (intents / (_row["attempt_id"] + ".json")).open("x") as intent:
                            json.dump(_row | {"provider_attempts": 1}, intent, indent=2)
                            intent.flush()
                            os.fsync(intent.fileno())
                        _row["provider_attempts"] += 1
                        try:
                            return _completion(**kwargs)
                        except Exception as exc:
                            _row["provider_error"] = provider_error(exc, datetime.now(UTC))
                            raise
                        finally:
                            _diagnostics.append({"duration_seconds": time.monotonic() - stamp})

                    backend._completion = observed
                    # Provider scope prevents competition with the same shared account.
                    with paused(
                        Selection("provider", route.provider),
                        seconds=timeout + 100,
                        drain_timeout=30,
                        reason="Approved varied challenge pilot",
                        client=client,
                    ) as window:
                        if not window.drained:
                            raise RuntimeError("Production did not drain; no call submitted")
                        if candidate != "glm_full":
                            client.reserve(route.route_id, 1)
                        calls += 1
                        calls_by_candidate[candidate] += 1
                        result = backend.run_immediate(job)
                    row.update(raw_response=result.output, upstream_model=result.upstream_model)
                    identity = result.output.get("model")
                    row["identity_held"] = identity not in {
                        route.upstream_model,
                        route.direct_model,
                    }
                    row["serving_family_held"] = route.provider == "beatapi"
                    answer = parse_structured_json(
                        result.output["choices"][0]["message"]["content"]
                    )
                    row.update(
                        status="completed", answer=validate_answer(answer, case).model_dump()
                    )
                    done.add((logical_candidate(candidate), case.id))
            except Exception as exc:
                original = row.get("provider_error") or provider_error(exc, datetime.now(UTC))
                row.update(
                    status="failed" if row["provider_attempts"] else "deferred",
                    error_class=type(exc).__name__,
                    reason=original["message"],
                    failure_category=original["category"],
                    provider_error=original,
                )
                held.add(candidate)
            row.update(
                end_to_end_seconds=time.monotonic() - started,
                diagnostics=diagnostics,
                finished_at=datetime.now(UTC).isoformat(),
            )
            if row["status"] == "failed":
                until = cooldown_until(row)
                row["next_eligible_at"] = until.isoformat() if until else None
            with (batch / (candidate + "-" + canonical_hash(case.id)[:16] + ".json")).open(
                "x"
            ) as f:
                json.dump(row, f, indent=2)
            print(candidate, case.id, row["status"], "calls", calls, flush=True)
    print("Batch ended; provider calls", calls, flush=True)


if __name__ == "__main__":
    main()
