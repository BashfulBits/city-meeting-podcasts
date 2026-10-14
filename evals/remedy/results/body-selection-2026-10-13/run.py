"""Bounded eight-model body-selection runner; never reads scoring answers."""

import argparse
import fcntl
import importlib.util
import json
import sys
import time
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from body_selection import (
    MAX_ATTEMPTS,
    MODELS,
    PROMPT,
    ROOT,
    BodySelectionAnswer,
    attempt_position,
    expand_model_set,
    history,
    instant,
    load_manifest,
    messages,
    plan,
    validate_answer,
    write_exclusive,
)
from call_priority import bind_priority, inspect_priority, read_status
from groq_routes import CANDIDATES as GROQ_CANDIDATES
from groq_routes import CONTROLS_SOURCE, LIMITS_SOURCE, token_headroom
from groq_routes import route_for as groq_route_for

from citypods.compute.base import InferenceJob
from citypods.compute.llm import LLMBackendConfig, _messages_with_schema
from citypods.compute.llm_policy import LLMRequestPolicy, estimate_tokens
from citypods.compute.structured import parse_structured_json, register_response_model
from citypods.remedy_evaluation import canonical_hash

# Reuse the exact approved physical routes, CAS headroom and provider coordination.
VARIED = ROOT / "evals/remedy/results/varied-2026-10-11"
sys.path.insert(0, str(VARIED))
spec = importlib.util.spec_from_file_location("body_selection_transport", VARIED / "run.py")
transport = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transport)
recovery = sys.modules["recovery"]
CANDIDATES = transport.CANDIDATES | GROQ_CANDIDATES


def route_for(candidate):
    return (
        groq_route_for(candidate)
        if candidate in GROQ_CANDIDATES
        else transport.route_for(candidate)
    )


def shaped_messages(case, route):
    payload = messages(case)
    if getattr(route, "structured_output_include_schema_in_prompt", False):
        return _messages_with_schema(payload, BodySelectionAnswer)
    return payload


def headroom(client, storage, route, case, coordination_bypass=False):
    enough, capacity = (
        transport.headroom(client, storage, route, coordination_bypass=True)
        if coordination_bypass
        else transport.headroom(client, storage, route)
    )
    if enough and route.provider == "groq":
        request_tokens = estimate_tokens(shaped_messages(case, route)) + 16384
        enough, token_capacity = (
            token_headroom(
                transport, client, storage, route, request_tokens, coordination_bypass=True
            )
            if coordination_bypass
            else token_headroom(transport, client, storage, route, request_tokens)
        )
        capacity.update(token_capacity)
    return enough, capacity


def snapshot(rows, cases, now):
    plans = {name: plan(rows, name, cases, now) for name in MODELS}
    return {
        "updated_at": now.isoformat(),
        "models": plans,
        "answered": sum(p["answered"] for p in plans.values()),
        "target": len(cases) * len(MODELS),
        "provider_attempts": sum(r["provider_attempts"] for r in rows),
    }


def attempt(
    directory, candidate, case, previous, manifest_hash, storage, client, coordination_bypass=False
):
    route = route_for(candidate)
    timeout = 120 if route.provider == "gemini" else 900
    tokens = 4096 if route.provider == "gemini" else 16384
    effort = CANDIDATES[candidate][1]
    payload = messages(case)
    estimate = estimate_tokens(shaped_messages(case, route))
    if (
        estimate + tokens > route.input_context_limit
        or (route.hard_input_ceiling is not None and estimate > route.hard_input_ceiling)
        or (route.output_context_limit is not None and tokens > route.output_context_limit)
    ):
        raise ValueError("frozen request exceeds route context/output capacity")
    row = {
        "candidate": candidate,
        "case_id": case.case_id,
        "mode": "blind_body_selection",
        "manifest_hash": manifest_hash,
        "prompt_hash": canonical_hash(PROMPT),
        "schema_hash": canonical_hash(BodySelectionAnswer.model_json_schema()),
        "case_input_hash": canonical_hash(case.model_dump()),
        "catalog_hash": canonical_hash(
            json.loads((ROOT / "citypods/compute/llm_routes.json").read_text())
        ),
        "route_id": route.route_id,
        "upstream_model_requested": route.upstream_model,
        "reasoning": effort or "provider-default",
        "request_timeout": timeout,
        "output_token_cap": tokens,
        "provider_attempts": 0,
        "observed_at": datetime.now(UTC).isoformat(),
        "qualified": False,
        "coordination_bypass": coordination_bypass,
        "production_drained": False,
        "coordination_approval_ref": (
            "review/51 temporary coordination bypass; maintainer chat2026-10-13"
            if coordination_bypass
            else None
        ),
        **attempt_position(previous),
    }
    if candidate in GROQ_CANDIDATES:
        row.update(
            controls_source=CONTROLS_SOURCE,
            limits_source=LIMITS_SOURCE,
            controls_verified_on="2026-10-13",
            estimated_shaped_input_tokens=estimate,
        )
    row["route_settings_hash"] = canonical_hash(
        {
            "route": route.route_id,
            "model": route.model,
            "effort": effort,
            "controls": route.reasoning_controls_json,
            "params": route.request_params_json,
            "coordination_bypass": coordination_bypass,
            "timeout": timeout,
            "tokens": tokens,
        }
    )
    started = time.monotonic()
    diagnostics = []
    try:
        policy = LLMRequestPolicy(
            allowed_models=(route.model,),
            allowed_route_ids=(route.route_id,),
            purpose="audit-remedy",
            require_direct=True,
            deadline_at=datetime.now(UTC) + timedelta(seconds=150 if timeout == 120 else 950),
        )
        job = InferenceJob(
            task="tag",
            recipe_hash=canonical_hash(row),
            inputs={
                "messages": payload,
                "structured_output": "body-selection-eval-v1",
                "llm_policy": policy,
                "reasoning_level": effort,
                "max_tokens": tokens,
                "timeout": timeout,
                "num_retries": 0,
                "max_provider_attempts": 1,
            },
        )
        backend = transport.LiteLLMBackend(LLMBackendConfig(model=route.model), storage=storage)
        completion = backend._completion_fn()

        def observed(**kwargs):
            if candidate in GROQ_CANDIDATES:
                controls = kwargs.get("extra_body") or {}
                if (
                    controls.get("reasoning_effort") != "high"
                    or controls.get("reasoning_format") != "parsed"
                ):
                    raise ValueError("Groq high-reasoning controls missing; no request submitted")
                row["submitted_reasoning_controls"] = {
                    k: controls[k] for k in ("reasoning_effort", "reasoning_format")
                }
            row["attempt_id"] = uuid4().hex
            write_exclusive(
                directory / "intents" / (row["attempt_id"] + ".json"),
                row | {"provider_attempts": 1},
            )
            row["provider_attempts"] = 1
            stamp = time.monotonic()
            try:
                return completion(**kwargs)
            except Exception as exc:
                row["provider_error"] = transport.provider_error(exc, datetime.now(UTC))
                raise
            finally:
                diagnostics.append({"duration_seconds": time.monotonic() - stamp})

        backend._completion = observed
        window_context = (
            nullcontext(None)
            if coordination_bypass
            else transport.paused(
                transport.Selection("provider", route.provider),
                seconds=timeout + 100,
                drain_timeout=30,
                reason="Approved independent body-selection evaluation",
                client=client,
            )
        )
        with window_context as window:
            if not coordination_bypass and not window.drained:
                raise RuntimeError("Production did not drain; no call submitted")
            row["production_drained"] = not coordination_bypass
            if coordination_bypass:
                enough, row["capacity"] = transport.headroom(
                    client, storage, route, coordination_bypass=True
                )
                if not enough:
                    raise RuntimeError("Local quota headroom unavailable; no call submitted")
            if candidate in GROQ_CANDIDATES:
                enough, row["capacity"] = token_headroom(
                    transport,
                    client,
                    storage,
                    route,
                    estimate + tokens,
                    **({"coordination_bypass": True} if coordination_bypass else {}),
                )
                if not enough:
                    raise RuntimeError(
                        "Groq daily tokens reserved for production; no call submitted"
                    )
            if candidate != "glm_full" and not coordination_bypass:
                client.reserve(route.route_id, 1)
            result = backend.run_immediate(job)
        row.update(raw_response=result.output, upstream_model=result.upstream_model)
        if result.output.get("usage") is not None:
            row["provider_usage"] = result.output["usage"]
        row["identity_held"] = result.output.get("model") not in {
            route.upstream_model,
            route.direct_model,
        }
        row["serving_family_held"] = route.provider == "beatapi"
        answer = parse_structured_json(result.output["choices"][0]["message"]["content"])
        row.update(status="completed", answer=validate_answer(answer, case).model_dump())
    except Exception as exc:
        details = row.get("provider_error") or transport.provider_error(exc, datetime.now(UTC))
        row.update(
            status="failed" if row["provider_attempts"] else "deferred",
            reason=details["message"],
            failure_category=details["category"],
            provider_error=details,
        )
    row.update(
        end_to_end_seconds=time.monotonic() - started,
        diagnostics=diagnostics,
        finished_at=datetime.now(UTC).isoformat(),
    )
    write_exclusive(directory / "batches" / (uuid4().hex + ".json"), row)
    print(candidate, case.case_id, row["status"], row["round"], row["attempt_in_round"], flush=True)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--results-dir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--shared-lock", type=Path)
    parser.add_argument("--priority-results-dir", type=Path)
    parser.add_argument("--coordination-bypass", action="store_true")
    args = parser.parse_args()
    frozen, cases = load_manifest()
    mh = canonical_hash(frozen)
    ids = {c.case_id for c in cases}
    directory = args.results_dir.resolve()
    if not args.execute:
        print(json.dumps(snapshot(history(directory, mh, ids), cases, datetime.now(UTC)), indent=2))
        return
    if args.shared_lock is None:
        parser.error("execution requires explicit shared provider-call lock")
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "controller.lock").open("a") as controller_lock:
        try:
            fcntl.flock(controller_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Body-selection controller already running") from None
        status_file = directory / "controller-status.json"
        if args.coordination_bypass and not status_file.exists():
            parser.error("coordination bypass must resume the existing body-selection run")
        if status_file.exists():
            state = json.loads(status_file.read_text())
            if (
                state["manifest_hash"] != mh
                or state["prompt_hash"] != canonical_hash(PROMPT)
                or state["schema_hash"] != canonical_hash(BodySelectionAnswer.model_json_schema())
                or state["shared_lock"] != str(args.shared_lock.resolve())
            ):
                raise ValueError("cannot resume changed experiment")
        else:
            state = {
                "manifest_hash": mh,
                "prompt_hash": canonical_hash(PROMPT),
                "schema_hash": canonical_hash(BodySelectionAnswer.model_json_schema()),
                "started_at": datetime.now(UTC).isoformat(),
                "deadline_at": (datetime.now(UTC) + timedelta(days=7)).isoformat(),
                "max_provider_attempts": len(cases) * len(MODELS) * MAX_ATTEMPTS,
                "shared_lock": str(args.shared_lock.resolve()),
                "next_candidate_index": 0,
            }
        expand_model_set(state, len(cases), datetime.now(UTC))
        if args.priority_results_dir:
            priority_directory = args.priority_results_dir.resolve()
            if args.shared_lock.resolve() != priority_directory / "run.lock":
                parser.error("priority results must use the same shared provider-call lock")
            if "call_priority" not in state:
                state["call_priority"] = bind_priority(
                    priority_directory, read_status(priority_directory), datetime.now(UTC)
                )
            elif state["call_priority"]["older_results_dir"] != str(priority_directory):
                raise ValueError("cannot resume changed priority results directory")
        elif "call_priority" in state:
            parser.error("resume requires the saved priority results directory")
        if state.get("coordination_bypass", False) != args.coordination_bypass:
            for current in state.get("models", {}).values():
                if current.get("state") in {"capacity_unknown", "provider_busy"}:
                    current["next_eligible_at"] = None
            state.setdefault("coordination_mode_history", []).append(
                {
                    "bypass": args.coordination_bypass,
                    "observed_at": datetime.now(UTC).isoformat(),
                    "approval_ref": (
                        "review/51 temporary coordination bypass; maintainer chat2026-10-13"
                    ),
                }
            )
        state["coordination_bypass"] = args.coordination_bypass
        if args.coordination_bypass:
            qwen = state.get("models", {}).get("qwen38", {})
            if str(qwen.get("capacity", {}).get("reason", "")).startswith("Qwen held: bypass"):
                qwen["next_eligible_at"] = None
            if not state.get("qwen_bypass_enabled", False):
                state["next_candidate_index"] = MODELS.index("qwen38")
            state["qwen_bypass_enabled"] = True
            state["qwen_bypass_approval_ref"] = (
                "review/51 Qwen hold removal; maintainer chat2026-10-13"
            )
        recovery.atomic_status(status_file, state)
        site = transport.load_site_config(ROOT / "config/site_config.yml")
        storage = transport.make_storage(site, site.get("base_url", ""), ROOT / "docs")
        if storage is None or not storage.cas_capable:
            raise SystemExit("Shared CAS quota storage unavailable")
        client = None if args.coordination_bypass else transport.DispatchPauseClient()
        register_response_model("body-selection-eval-v1", BodySelectionAnswer)
        with args.shared_lock.open("a") as shared:
            try:
                while True:
                    now = datetime.now(UTC)
                    rows = history(directory, mh, ids)
                    prior = state.get("models", {})
                    state.update(snapshot(rows, cases, now))
                    for name, current in state["models"].items():
                        older = prior.get(name, {})
                        if (
                            current["state"] == "ready"
                            and older.get("state")
                            in {"daily_quota", "provider_busy", "capacity_unknown"}
                            and older.get("next_eligible_at")
                            and instant(older["next_eligible_at"]) > now
                        ):
                            state["models"][name] = older
                    reason = None
                    if state["answered"] == state["target"]:
                        reason = "complete"
                    elif now + timedelta(seconds=1050) >= instant(state["deadline_at"]):
                        reason = "horizon_expired"
                    elif state["provider_attempts"] >= state["max_provider_attempts"]:
                        reason = "attempt_budget_exhausted"
                    elif all(
                        p["state"] in {"complete", "held_or_exhausted", "access_error"}
                        for p in state["models"].values()
                    ):
                        reason = "incomplete_held_or_exhausted"
                    state["stop_reason"] = reason
                    state["next_action_at"] = None
                    recovery.atomic_status(status_file, state)
                    if reason:
                        print(reason, state["answered"], state["target"], flush=True)
                        return
                    if "call_priority" in state:
                        state["call_allocation"] = inspect_priority(state["call_priority"], now)
                        if not state["call_allocation"]["allow_new"]:
                            state["next_action_at"] = (now + timedelta(seconds=10)).isoformat()
                            recovery.atomic_status(status_file, state)
                            time.sleep(10)
                            continue
                    selected = None
                    for offset in range(len(MODELS)):
                        index = (state["next_candidate_index"] + offset) % len(MODELS)
                        name = MODELS[index]
                        current = state["models"][name]
                        if current["state"] != "ready":
                            continue
                        try:
                            case_id = current["eligible_case_ids"][0]
                            case = next(c for c in cases if c.case_id == case_id)
                            route = route_for(name)
                            enough, capacity = (
                                headroom(client, storage, route, case, coordination_bypass=True)
                                if args.coordination_bypass
                                else headroom(client, storage, route, case)
                            )
                        except Exception as exc:
                            enough = False
                            capacity = {
                                "kind": "capacity_unknown",
                                "reason": recovery.sanitize(exc),
                                "next_eligible_at": (now + timedelta(minutes=5)).isoformat(),
                            }
                        if not enough:
                            current.update(
                                state=capacity["kind"],
                                next_eligible_at=capacity["next_eligible_at"],
                                capacity=capacity,
                            )
                            continue
                        selected = name
                        state["next_candidate_index"] = (index + 1) % len(MODELS)
                        break
                    if selected is None:
                        waits = [
                            instant(p["next_eligible_at"])
                            for p in state["models"].values()
                            if p.get("next_eligible_at")
                        ]
                        state["next_action_at"] = min(waits).isoformat() if waits else None
                        recovery.atomic_status(status_file, state)
                        time.sleep(60)
                        continue
                    try:
                        fcntl.flock(shared, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        state["waiting_for_shared_lock"] = True
                        state["next_action_at"] = (now + timedelta(minutes=1)).isoformat()
                        recovery.atomic_status(status_file, state)
                        time.sleep(60)
                        continue
                    try:
                        priority_slot = None
                        if "call_priority" in state:
                            state["call_allocation"] = inspect_priority(state["call_priority"])
                            if not state["call_allocation"]["allow_new"]:
                                recovery.atomic_status(status_file, state)
                                continue
                            if state["call_allocation"]["contested"]:
                                # Persist first; an interrupted outcome consumes a slot.
                                priority_slot = uuid4().hex
                                state["call_priority"]["new_contested_slots"].append(priority_slot)
                        case_id = state["models"][selected]["eligible_case_ids"][0]
                        case = next(c for c in cases if c.case_id == case_id)
                        used = sum(
                            r["provider_attempts"]
                            for r in rows
                            if r["candidate"] == selected and r["case_id"] == case_id
                        )
                        state["waiting_for_shared_lock"] = False
                        state["models"][selected]["state"] = "running"
                        recovery.atomic_status(status_file, state)
                        row = attempt(
                            directory,
                            selected,
                            case,
                            used,
                            mh,
                            storage,
                            client,
                            **({"coordination_bypass": True} if args.coordination_bypass else {}),
                        )
                        if priority_slot and not row["provider_attempts"]:
                            state["call_priority"]["new_contested_slots"].remove(priority_slot)
                        recovery.atomic_status(status_file, state)
                        if row["status"] == "deferred":
                            capacity = row.get("capacity") or {}
                            state["models"][selected].update(
                                state=capacity.get("kind", "provider_busy"),
                                next_eligible_at=capacity.get("next_eligible_at")
                                or (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                                capacity=capacity,
                            )
                    finally:
                        fcntl.flock(shared, fcntl.LOCK_UN)
            except BaseException as exc:
                state.update(
                    stop_reason="interrupted" if isinstance(exc, KeyboardInterrupt) else "error",
                    error=recovery.sanitize(exc),
                    updated_at=datetime.now(UTC).isoformat(),
                )
                recovery.atomic_status(status_file, state)
                raise


if __name__ == "__main__":
    main()
