"""Separate fourteen-call diagnostic; respects shared locks and leaves original pauses intact."""

import argparse
import fcntl
import importlib.util
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
BODY = ROOT / "evals/remedy/results/body-selection-2026-10-13"
sys.path.insert(0, str(BODY))
spec = importlib.util.spec_from_file_location("feed_context_body_runner", BODY / "run.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

from body_selection import SelectionInput, instant, write_exclusive  # noqa: E402

from citypods.compute.structured import register_response_model  # noqa: E402
from citypods.remedy_evaluation import canonical_hash  # noqa: E402

PACKET = ROOT / "evals/remedy/holdout/feed-context-2026-10-14/packet.json"


def observations(directory):
    rows = [json.loads(p.read_text()) for p in (directory / "batches").glob("*.json")]
    resolved = {r.get("attempt_id") for r in rows}
    for path in (directory / "intents").glob("*.json"):
        row = json.loads(path.read_text())
        if row["attempt_id"] not in resolved:
            rows.append(row | {"status": "unknown_outcome"})
    for row in rows:
        if "variant" not in row:
            row["variant"] = row["mode"].split("/", 1)[1]
    return rows


def job_plan(packet, rows, waits=None, cooldowns=None):
    jobs = []
    variants = list(packet["variants"])
    for index, model in enumerate(packet["models"]):
        order = variants if index % 2 == 0 else variants[::-1]
        for variant in order:
            own = [r for r in rows if r["candidate"] == model and r.get("variant") == variant]
            submitted = [r for r in own if r["provider_attempts"]]
            if submitted:
                assert len(submitted) == 1, "duplicate diagnostic submission"
                continue
            last = own[-1] if own else None
            if last and last["status"] == "input_capacity_hold":
                continue
            wait = (last or {}).get("capacity", {}).get("next_eligible_at")
            wait = (waits or {}).get(f"{model}/{variant}", {}).get("next_eligible_at") or wait
            cooldown = (cooldowns or {}).get(model)
            if cooldown and (not wait or instant(cooldown) > instant(wait)):
                wait = cooldown
            jobs.append({"candidate": model, "variant": variant, "next_eligible_at": wait})
    return jobs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--packet", type=Path, default=PACKET)
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--shared-lock", type=Path, required=True)
    args = parser.parse_args()
    packet = json.loads(args.packet.read_text())
    binding = canonical_hash(packet)
    case = SelectionInput.model_validate(packet["case"])
    if not args.execute:
        print(json.dumps(job_plan(packet, []), indent=2))
        return
    directory = args.results_dir
    for name in ("intents", "batches"):
        (directory / name).mkdir(parents=True, exist_ok=True)
    with (directory / "controller.lock").open("a") as controller:
        try:
            fcntl.flock(controller, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Diagnostic controller already running") from None
        status_path = directory / "controller-status.json"
        if status_path.exists():
            state = json.loads(status_path.read_text())
            if state["packet_hash"] != binding:
                raise ValueError("changed diagnostic packet")
        else:
            state = {
                "packet_hash": binding,
                "started_at": datetime.now(UTC).isoformat(),
                "deadline_at": (datetime.now(UTC) + timedelta(days=7)).isoformat(),
                "target": 14,
            }
        site = runner.transport.load_site_config(ROOT / "config/site_config.yml")
        storage = runner.transport.make_storage(site, site.get("base_url", ""), ROOT / "docs")
        if storage is None or not storage.cas_capable:
            raise SystemExit("Shared CAS storage required")
        register_response_model("body-selection-eval-v1", runner.BodySelectionAnswer)
        with args.shared_lock.open("a") as shared:
            while True:
                now = datetime.now(UTC)
                rows = observations(directory)
                if any(r["manifest_hash"] != binding for r in rows):
                    raise ValueError("foreign diagnostic result")
                jobs = job_plan(packet, rows, state.get("waiting"), state.get("model_cooldowns"))
                state.update(
                    updated_at=now.isoformat(),
                    submitted=sum(r["provider_attempts"] for r in rows),
                    answered=sum(r["status"] == "completed" for r in rows),
                    jobs=jobs,
                    stop_reason=None,
                )
                if not jobs:
                    state["stop_reason"] = "all_fourteen_jobs_observed_or_held"
                elif now + timedelta(seconds=1050) >= instant(state["deadline_at"]):
                    state["stop_reason"] = "horizon_expired"
                runner.recovery.atomic_status(status_path, state)
                if state["stop_reason"]:
                    print(state["stop_reason"], flush=True)
                    return
                fcntl.flock(shared, fcntl.LOCK_EX)
                try:
                    selected = None
                    for job in jobs:
                        if job["next_eligible_at"] and instant(job["next_eligible_at"]) > now:
                            continue
                        model, variant = job["candidate"], job["variant"]
                        route = runner.route_for(model)
                        payload = packet["variants"][variant]
                        estimate = runner.estimate_tokens(
                            runner.shaped_messages(case, route, payload)
                        )
                        output_cap = 4096 if route.provider == "gemini" else 16384
                        if (
                            estimate + output_cap > route.input_context_limit
                            or route.hard_input_ceiling
                            and estimate > route.hard_input_ceiling
                        ):
                            row = {
                                "candidate": model,
                                "variant": variant,
                                "case_id": case.case_id,
                                "manifest_hash": binding,
                                "provider_attempts": 0,
                                "status": "input_capacity_hold",
                                "observed_at": now.isoformat(),
                                "capacity": {"next_eligible_at": state["deadline_at"]},
                            }
                            write_exclusive(
                                directory / "batches" / f"input-{model}-{variant}.json", row
                            )
                            continue
                        enough, capacity = runner.headroom(
                            None,
                            storage,
                            route,
                            case,
                            coordination_bypass=True,
                            request_messages=payload,
                        )
                        if enough:
                            selected = job
                            break
                        state.setdefault("waiting", {})[f"{model}/{variant}"] = capacity
                    if selected is None:
                        runner.recovery.atomic_status(status_path, state)
                    else:
                        model, variant = selected["candidate"], selected["variant"]
                        state["active_job"] = selected
                        runner.recovery.atomic_status(status_path, state)
                        # Variant is embedded in separate experiment provenance before submission.
                        context_case = case.model_copy(deep=True)
                        row = runner.attempt(
                            directory,
                            model,
                            context_case,
                            0,
                            binding,
                            storage,
                            None,
                            coordination_bypass=True,
                            request_messages=packet["variants"][variant],
                            experiment_mode=f"feed_context/{variant}",
                        )
                        # Derive variant from immutable mode; never rewrite an attempt or intent.
                        state.pop("active_job", None)
                        if row["provider_attempts"] == 0:
                            state.setdefault("waiting", {})[f"{model}/{variant}"] = {
                                "kind": "local_preflight_deferral",
                                "next_eligible_at": (
                                    datetime.now(UTC) + timedelta(minutes=5)
                                ).isoformat(),
                            }
                        else:
                            state.get("waiting", {}).pop(f"{model}/{variant}", None)
                            if row["status"] != "completed":
                                state.setdefault("model_cooldowns", {})[model] = (
                                    datetime.now(UTC) + timedelta(minutes=30)
                                ).isoformat()
                        runner.recovery.atomic_status(status_path, state)
                finally:
                    fcntl.flock(shared, fcntl.LOCK_UN)
                if selected is None:
                    time.sleep(30)


if __name__ == "__main__":
    main()
