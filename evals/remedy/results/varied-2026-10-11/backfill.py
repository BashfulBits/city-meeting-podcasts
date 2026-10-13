"""Bounded, reset-aware controller for the approved six-model comparison.

Run through citypods-env. Default is a local plan only; --execute explicitly enables calls.
An interrupted run resumes its original horizon and call allowance. --new-run starts another
bounded allowance after an explicit operator decision. Existing results are never replaced.
"""

import argparse
import fcntl
import json
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import run as runner
from recovery import (
    MODELS,
    atomic_status,
    candidate_plan,
    load_retry_allowance,
    read_history,
    sanitize,
    timestamp,
)

from citypods.config import load_site_config
from citypods.remedy_evaluation import Manifest, canonical_hash
from citypods.storage import make_storage


def run_state(
    previous,
    manifest_hash,
    now,
    new_run=False,
    horizon_hours=24,
    max_calls=256,
    allowance_hash=None,
):
    if previous and not new_run:
        if previous.get("manifest_hash") != manifest_hash:
            raise ValueError("controller manifest mismatch")
        if previous.get("retry_allowance_hash") != allowance_hash:
            raise ValueError("controller retry allowance mismatch")
        timestamp(previous["started_at"])
        timestamp(previous["deadline_at"])
        if not 1 <= previous["max_calls"] <= 256:
            raise ValueError("invalid persisted controller call cap")
        return previous
    return {
        "run_id": uuid4().hex,
        "manifest_hash": manifest_hash,
        "started_at": now.isoformat(),
        "deadline_at": (now + timedelta(hours=horizon_hours)).isoformat(),
        "max_calls": max_calls,
        "next_candidate_index": 0,
        "retry_allowance_hash": allowance_hash,
    }


def snapshot(rows, state, case_ids, now, allowance=None):
    plans = {
        name: candidate_plan(rows, name, case_ids, now, timestamp(state["started_at"]), allowance)
        for name in MODELS
    }
    calls = sum(
        row.get("provider_attempts", 0)
        for row in rows
        if row["candidate"] in MODELS
        and timestamp(row["observed_at"]) >= timestamp(state["started_at"])
    )
    models = {
        name: {key: plans[name][key] for key in ("answered", "failed_pending", "never_called")}
        for name in MODELS
    }
    answered = sum(model["answered"] for model in models.values())
    target = len(case_ids) * len(MODELS)
    reason = None
    if answered == target:
        reason = "complete"
    elif now >= timestamp(state["deadline_at"]) - timedelta(seconds=1050):
        reason = "run_horizon"
    elif calls >= state["max_calls"]:
        reason = "call_budget"
    elif all(
        p["state"] in {"complete", "retry_exhausted", "access_error", "unknown_outcome"}
        for p in plans.values()
    ):
        reason = "manual_review_or_retry_exhausted"
    return state | {
        "updated_at": now.isoformat(),
        "answered": answered,
        "target": target,
        "models": models,
        "routes": plans,
        "provider_attempts_this_run": calls,
        "stop_reason": reason,
    }


def retain_wait(plan, prior, now):
    """Quota/busy waits survive a restart; observation-derived cooldowns remain authoritative."""
    if plan["state"] != "ready" or not prior:
        return plan
    if prior.get("state") not in {
        "daily_quota",
        "provider_busy",
        "capacity_unknown",
        "runner_busy",
    }:
        return plan
    if prior.get("next_eligible_at") and timestamp(prior["next_eligible_at"]) > now:
        return plan | {
            "state": prior["state"],
            "next_eligible_at": prior["next_eligible_at"],
            "capacity": prior.get("capacity"),
        }
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--new-run", action="store_true")
    parser.add_argument("--results-dir", type=Path, default=runner.OUT)
    parser.add_argument("--horizon-hours", type=float, default=24)
    parser.add_argument("--max-calls", type=int, default=256)
    parser.add_argument("--retry-allowance", type=Path)
    args = parser.parse_args()
    if not 0 < args.horizon_hours <= 168 or not 1 <= args.max_calls <= 256:
        parser.error("horizon must be 0–168 hours and call cap 1–256")
    directory = args.results_dir.resolve()
    manifest = Manifest.model_validate_json((runner.FROZEN / "manifest.json").read_text())
    manifest_hash = canonical_hash(manifest.model_dump())
    case_ids = [case.id for case in manifest.cases]
    allowance = load_retry_allowance(args.retry_allowance, manifest_hash, set(case_ids))
    allowance_hash = canonical_hash(allowance) if allowance else None
    if allowance and args.max_calls > 25:
        parser.error("scoped retry allowance permits at most 25 calls")
    status_path = directory / "controller-status.json"
    previous = json.loads(status_path.read_text()) if status_path.exists() else None
    state = run_state(
        previous,
        manifest_hash,
        datetime.now(UTC),
        args.new_run,
        args.horizon_hours,
        args.max_calls,
        allowance_hash,
    )
    if not args.execute:
        rows = read_history(directory / "batches", manifest_hash, set(case_ids))
        print(json.dumps(snapshot(rows, state, case_ids, datetime.now(UTC), allowance), indent=2))
        return
    if not (directory / "pilot-reviewed.json").exists():
        raise SystemExit("Full stage requires explicit reviewed pilot marker")
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "controller.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("A recovery controller is already running") from None
        # Read again under the controller lock in case another controller updated the status.
        previous = json.loads(status_path.read_text()) if status_path.exists() else None
        state = run_state(
            previous,
            manifest_hash,
            datetime.now(UTC),
            args.new_run,
            args.horizon_hours,
            args.max_calls,
            allowance_hash,
        )
        site = load_site_config(runner.ROOT / "config/site_config.yml")
        storage = make_storage(
            site, site.get("base_url", ""), runner.ROOT / site.get("output_dir", "docs")
        )
        if storage is None or not storage.cas_capable:
            raise SystemExit("Shared CAS quota storage unavailable")
        client = runner.DispatchPauseClient()
        try:
            while True:
                now = datetime.now(UTC)
                rows = read_history(directory / "batches", manifest_hash, set(case_ids))
                previous_routes = state.get("routes", {})
                state = snapshot(rows, state, case_ids, now, allowance)
                for name, plan in state["routes"].items():
                    state["routes"][name] = retain_wait(plan, previous_routes.get(name), now)
                atomic_status(status_path, state)
                if state["stop_reason"]:
                    print(json.dumps({k: state[k] for k in ("answered", "target", "stop_reason")}))
                    return
                selected = None
                for offset in range(len(MODELS)):
                    index = (state["next_candidate_index"] + offset) % len(MODELS)
                    name = MODELS[index]
                    plan = state["routes"][name]
                    if plan["state"] != "ready":
                        continue
                    try:
                        enough, capacity = runner.headroom(client, storage, runner.route_for(name))
                    except Exception as exc:
                        enough = False
                        capacity = {
                            "kind": "capacity_unknown",
                            "reason": sanitize(exc),
                            "next_eligible_at": (now + timedelta(minutes=5)).isoformat(),
                        }
                    plan["capacity"] = capacity
                    if not enough:
                        plan.update(
                            state=capacity["kind"], next_eligible_at=capacity["next_eligible_at"]
                        )
                        continue
                    selected = name
                    state["next_candidate_index"] = (index + 1) % len(MODELS)
                    break
                if selected is None:
                    waits = [
                        timestamp(p["next_eligible_at"])
                        for p in state["routes"].values()
                        if p["next_eligible_at"]
                    ]
                    state["next_action_at"] = min(waits).isoformat() if waits else None
                    atomic_status(status_path, state)
                    delay = (
                        max(1, (min(waits) - datetime.now(UTC)).total_seconds()) if waits else 60
                    )
                    time.sleep(min(60, delay))
                    continue
                state["routes"][selected]["state"] = "running"
                atomic_status(status_path, state)
                command = [
                    sys.executable,
                    str(Path(__file__).with_name("run.py")),
                    "--stage",
                    "full",
                    "--candidate",
                    selected,
                    "--retry-failed",
                    "--results-dir",
                    str(directory),
                    "--run-started-at",
                    state["started_at"],
                    "--stop-at",
                    state["deadline_at"],
                    "--max-calls",
                    str(min(16, state["max_calls"] - state["provider_attempts_this_run"])),
                ]
                if args.retry_allowance:
                    command.extend(["--retry-allowance", str(args.retry_allowance.resolve())])
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=max(
                        1, (timestamp(state["deadline_at"]) - datetime.now(UTC)).total_seconds()
                    ),
                )
                logs = directory / "controller-logs"
                logs.mkdir(exist_ok=True)
                log = logs / (datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + f"-{uuid4().hex}.log")
                with log.open("x") as output:
                    output.write("\n".join(sanitize(line) for line in result.stdout.splitlines()))
                    output.write("\n")
                    output.write("\n".join(sanitize(line) for line in result.stderr.splitlines()))
                if result.returncode:
                    if "already running" not in result.stderr + result.stdout:
                        raise RuntimeError(f"Evaluation subprocess failed; inspect {log.name}")
                    state["routes"][selected].update(
                        state="runner_busy",
                        next_eligible_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                    )
                    atomic_status(status_path, state)
        except BaseException as exc:
            state.update(
                stop_reason="interrupted"
                if isinstance(exc, KeyboardInterrupt)
                else "controller_error",
                reason=sanitize(exc),
                updated_at=datetime.now(UTC).isoformat(),
            )
            atomic_status(status_path, state)
            raise


if __name__ == "__main__":
    main()
