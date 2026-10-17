"""Durable 540-job prompt comparison with immutable calls, bounded retries and Git checkpoints."""

import argparse
import fcntl
import importlib.util
import json
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from variants import BODY, MODELS, ROOT, VARIANTS, request_messages

sys.path.insert(0, str(BODY))
spec = importlib.util.spec_from_file_location("comparison_body_runner", BODY / "run.py")
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)

from body_selection import instant, load_manifest  # noqa: E402

from citypods.compute.structured import register_response_model  # noqa: E402
from citypods.remedy_evaluation import canonical_hash  # noqa: E402

OUT = Path(__file__).resolve().parent


def observations(directory, binding):
    rows = [json.loads(p.read_text()) for p in (directory / "batches").glob("*.json")]
    resolved = {r.get("attempt_id") for r in rows}
    for path in (directory / "intents").glob("*.json"):
        row = json.loads(path.read_text())
        if row["attempt_id"] not in resolved:
            rows.append(row | {"status": "pending", "unknown_outcome": True})
    for row in rows:
        if row["manifest_hash"] != binding or row["candidate"] not in MODELS:
            raise ValueError("foreign prompt comparison result")
        prefix, variant = row["mode"].split("/", 1)
        if prefix != "prompt_comparison" or variant not in VARIANTS:
            raise ValueError("foreign prompt recipe")
        row["variant"] = variant
    return rows


def jobs(cases):
    ordered = sorted(cases, key=lambda c: canonical_hash(c.case_id))
    result = []
    variants = list(VARIANTS)
    for index, case in enumerate(ordered):
        for offset in range(3):
            variant = variants[(index + offset) % 3]
            for model_offset in range(3):
                result.append((MODELS[(index + model_offset) % 3], variant, case))
    return result


def pair_state(rows, model, variant, case_id):
    own = [
        r for r in rows if (r["candidate"], r["variant"], r["case_id"]) == (model, variant, case_id)
    ]
    if any(r["status"] == "completed" for r in own):
        return "complete", own
    if any(r.get("unknown_outcome") for r in own):
        return "unknown_outcome", own
    if sum(r["provider_attempts"] for r in own) >= 4:
        return "exhausted", own
    if any(r.get("failure_category") == "access_error" for r in own):
        return "access_error", own
    return "ready", own


def binding_for(frozen, bypass):
    return canonical_hash(
        {
            "manifest": frozen,
            "prompts": VARIANTS,
            "models": MODELS,
            "schema": adapter.BodySelectionAnswer.model_json_schema(),
            "attempts_per_job": 4,
            "coordination_bypass": bypass,
        }
    )


def checkpoint(directory, remote_branch):
    paths = [str((directory / name).relative_to(ROOT)) for name in ("batches", "intents")]
    files = [
        str(p.relative_to(ROOT))
        for name in ("batches", "intents")
        for p in sorted((directory / name).glob("*.json"))
    ]
    if files:
        subprocess.run(["git", "add", "--", *files], cwd=ROOT, check=True, capture_output=True)
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    if any(not any(p.startswith(prefix + "/") for prefix in paths) for p in staged):
        raise RuntimeError("Unrelated staged changes; no automatic commit")
    if staged:
        subprocess.run(
            ["git", "commit", "-m", "data: checkpoint prompt comparison observations"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
    subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "push", "origin", f"HEAD:{remote_branch}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        timeout=90,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--coordination-bypass", action="store_true")
    parser.add_argument("--remote-branch", default="chore/2413-prompt-observations")
    args = parser.parse_args()
    frozen, cases = load_manifest()
    binding = binding_for(frozen, args.coordination_bypass)
    work = jobs(cases)
    if not args.execute:
        print(json.dumps({"jobs": len(work), "models": MODELS, "prompts": list(VARIANTS)}))
        return
    if str(OUT).startswith(("/tmp/", "/private/tmp/")):
        raise SystemExit("Durable results directory required")
    for name in ("intents", "batches"):
        (OUT / name).mkdir(exist_ok=True)
    with (OUT / "controller.lock").open("a") as controller:
        try:
            fcntl.flock(controller, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Comparison already running") from None
        status = OUT / "controller-status.json"
        state = (
            json.loads(status.read_text())
            if status.exists()
            else {
                "binding": binding,
                "started_at": datetime.now(UTC).isoformat(),
                "deadline_at": (datetime.now(UTC) + timedelta(days=7)).isoformat(),
                "next_index": 0,
                "cooldowns": {},
                "waits": {},
            }
        )
        if state["binding"] != binding:
            raise ValueError("Changed prompt/model/accounting context on resume")
        site = adapter.transport.load_site_config(ROOT / "config/site_config.yml")
        storage = adapter.transport.make_storage(site, site.get("base_url", ""), ROOT / "docs")
        if storage is None or not storage.cas_capable:
            raise SystemExit("Shared CAS storage required")
        client = None if args.coordination_bypass else adapter.transport.DispatchPauseClient()
        register_response_model("body-selection-eval-v1", adapter.BodySelectionAnswer)
        since_checkpoint = 0
        with (OUT / "shared-call.lock").open("a") as shared:
            try:
                while True:
                    now = datetime.now(UTC)
                    rows = observations(OUT, binding)
                    plans = {model: {} for model in MODELS}
                    ready = []
                    for index, (model, variant, case) in enumerate(work):
                        kind, own = pair_state(rows, model, variant, case.case_id)
                        counts = plans[model].setdefault(variant, {})
                        counts[kind] = counts.get(kind, 0) + 1
                        wait = state["waits"].get(f"{model}/{variant}/{case.case_id}")
                        cooldown = state["cooldowns"].get(model)
                        if kind == "ready" and all(
                            not when or instant(when) <= now for when in (wait, cooldown)
                        ):
                            ready.append(index)
                    state.update(
                        updated_at=now.isoformat(),
                        target=540,
                        answered=sum(r["status"] == "completed" for r in rows),
                        submitted=sum(r["provider_attempts"] for r in rows),
                        plans=plans,
                        stop_reason=None,
                    )
                    unfinished = sum(v.get("ready", 0) for p in plans.values() for v in p.values())
                    if not unfinished:
                        state["stop_reason"] = "complete_or_held"
                    elif now + timedelta(seconds=1050) >= instant(state["deadline_at"]):
                        state["stop_reason"] = "horizon_expired"
                    adapter.recovery.atomic_status(status, state)
                    if state["stop_reason"]:
                        checkpoint(OUT, args.remote_branch)
                        return
                    if not ready:
                        if since_checkpoint:
                            checkpoint(OUT, args.remote_branch)
                            since_checkpoint = 0
                        time.sleep(30)
                        continue
                    index = min(ready, key=lambda i: (i - state["next_index"]) % len(work))
                    model, variant, case = work[index]
                    key = f"{model}/{variant}/{case.case_id}"
                    route = adapter.route_for(model)
                    try:
                        enough, capacity = adapter.headroom(
                            client,
                            storage,
                            route,
                            case,
                            coordination_bypass=args.coordination_bypass,
                            request_messages=request_messages(case, variant),
                        )
                    except Exception as exc:
                        enough, capacity = False, {"reason": adapter.recovery.sanitize(exc)}
                    if not enough:
                        state["waits"][key] = (
                            capacity.get("next_eligible_at")
                            or (now + timedelta(minutes=5)).isoformat()
                        )
                        state["cooldowns"][model] = state["waits"][key]
                        state.setdefault("capacity_by_model", {})[model] = capacity
                        state["last_capacity"] = capacity
                        state["next_index"] = (index + 1) % len(work)
                        continue
                    fcntl.flock(shared, fcntl.LOCK_EX)
                    try:
                        state["active_job"] = {
                            "model": model,
                            "variant": variant,
                            "case_id": case.case_id,
                        }
                        adapter.recovery.atomic_status(status, state)
                        _, own = pair_state(rows, model, variant, case.case_id)
                        used = sum(r["provider_attempts"] for r in own)
                        row = adapter.attempt(
                            OUT,
                            model,
                            case,
                            used,
                            binding,
                            storage,
                            client,
                            coordination_bypass=args.coordination_bypass,
                            request_messages=request_messages(case, variant),
                            experiment_mode=f"prompt_comparison/{variant}",
                        )
                    finally:
                        fcntl.flock(shared, fcntl.LOCK_UN)
                    state.pop("active_job", None)
                    state["next_index"] = (index + 1) % len(work)
                    if row["status"] != "completed":
                        if row["provider_attempts"]:
                            state["cooldowns"][model] = (
                                datetime.now(UTC) + timedelta(minutes=30)
                            ).isoformat()
                        else:
                            state["waits"][key] = (
                                datetime.now(UTC) + timedelta(minutes=5)
                            ).isoformat()
                    since_checkpoint += 1
                    # Persist remotely before another model request can be submitted.
                    checkpoint(OUT, args.remote_branch)
                    since_checkpoint = 0
            except BaseException as exc:
                state.update(
                    stop_reason="interrupted_or_error",
                    error=adapter.recovery.sanitize(exc),
                    updated_at=datetime.now(UTC).isoformat(),
                )
                adapter.recovery.atomic_status(status, state)
                raise


if __name__ == "__main__":
    main()
