"""Approved existing-pilot JEV fallback; one direct call per frozen packet, no retries."""

import fcntl
import importlib.util
import json
import os
import time
from datetime import timedelta
from uuid import uuid4

import run

from citypods.compute.llm_budget import mutate_llm_budget, settle_route_reservation

spec = importlib.util.spec_from_file_location(
    "existing_jev_pilot", run.ROOT / "scripts/eval_judge.py"
)
pilot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pilot)


class CapacityWait(Exception):
    pass


def reserve_one(storage, route, owner, tokens):
    def change(budget, instant):
        if not budget.available(
            route.model, route=route, requests=1, tokens=tokens, cost=0, now=instant
        ):
            raise CapacityWait("Shared quota pacing")
        budget.reserve(
            owner, route.model, route=route, requests=1, tokens=tokens, cost=0, now=instant
        )

    mutate_llm_budget(storage, change)


def staging_only(row):
    reason = row.get("reason", "")
    return row.get("status") == "unknown_submission" and (
        "Connection was closed" in reason
        and "/payloads/" in reason
        and "/request.json" in reason
        and not row.get("handle")
    )


def one(entry, binding, storage, client, route, jev):
    packet, job, sizes = run.jev_job(entry, binding)
    body = {"model": pilot.JEV_MODEL, **job.inputs["systemone"]}
    pilot.check_jev_size(body)
    row = {
        "attempt_id": uuid4().hex,
        "candidate": "jev",
        "case_id": entry["case"]["case_id"],
        "binding": binding,
        "mode": "disagreement_adjudication",
        "transport": "existing_local_pilot",
        "approval_ref": "maintainer chat2026-10-14 local JEV pilot fallback",
        "observed_at": run.now().isoformat(),
        "provider_attempts": 0,
        "qualified": False,
        "sizes": sizes,
    }
    tokens = sizes["state"] + sum(sizes["questions"]) + 1024
    owner, reserved, submitted, usage = "eval-jev-" + row["attempt_id"], False, False, None
    started = time.monotonic()
    try:
        with run.adapter.transport.paused(
            run.adapter.transport.Selection("provider", route.provider),
            seconds=400,
            drain_timeout=30,
            reason="Approved eight-case local JEV diagnostic",
            client=client,
        ) as window:
            if not window.drained:
                raise CapacityWait("Production did not drain; no request")
            reserve_one(storage, route, owner, tokens)
            reserved = True
            client.reserve(route.route_id, 1)
            write = row | {"provider_attempts": 1, "request_hash": run.canonical_hash(body)}
            run.write_exclusive(run.CALLS / "intents" / (row["attempt_id"] + ".json"), write)
            submitted = True
            row["provider_attempts"] = 1
            stamp = time.monotonic()
            response = jev._http_post(body)
            row["provider_seconds"] = time.monotonic() - stamp
            row["http_status"] = response.status_code
            raw = pilot._json_or_text(response)
            row["raw_response"] = raw
            if response.status_code != 200:
                row.update(status="failed", reason=f"JEV HTTP {response.status_code}")
            else:
                usage = raw.get("usage")
                parsed = [value for _, value in run.JevBackend().parse(packet, raw)]
                row.update(status="completed" if len(parsed) == 3 else "malformed", parsed=parsed)
    except CapacityWait:
        raise
    except Exception as exc:
        row.update(
            status="failed" if submitted else "held", reason=run.adapter.recovery.sanitize(exc)
        )
    finally:
        if reserved and submitted:
            actual_tokens = None
            if isinstance(usage, dict):
                values = (usage.get("input_tokens"), usage.get("output_tokens"))
                if all(isinstance(v, int) for v in values):
                    actual_tokens = sum(values)
                elif isinstance(usage.get("total_tokens"), int):
                    actual_tokens = usage["total_tokens"]
            settle_route_reservation(
                storage,
                owner,
                route.model,
                route=route,
                actual_requests=1,
                actual_tokens=actual_tokens,
                actual_cost=0,
            )
        elif reserved:
            settle_route_reservation(
                storage,
                owner,
                route.model,
                route=route,
                actual_requests=0,
                actual_tokens=0,
                actual_cost=0,
            )
    row.update(
        finished_at=run.now().isoformat(),
        elapsed_seconds=time.monotonic() - started,
        end_to_end_seconds=time.monotonic() - started,
    )
    run.write_exclusive(run.CALLS / "batches" / (uuid4().hex + ".json"), row)
    return row


def main():
    frozen = json.loads(run.PACKET_FILE.read_text())
    binding = run.canonical_hash(frozen)
    status_path = run.OUT / "local-jev-status.json"
    with (run.OUT / "controller.lock").open("a") as lock:
        # Never overlap the original DeepSeek sequence or another fallback process.
        fcntl.flock(lock, fcntl.LOCK_EX)
        site = run.adapter.transport.load_site_config(run.ROOT / "config/site_config.yml")
        storage = run.adapter.transport.make_storage(
            site, site.get("base_url", ""), run.ROOT / "docs"
        )
        if storage is None or not storage.cas_capable:
            raise ValueError("Shared CAS required")
        client = run.adapter.transport.DispatchPauseClient()
        route = run.ROUTE_REGISTRY["beatapi_jev_1_13_free"]
        if not route.free or route.pricing.input_per_token or route.pricing.output_per_token:
            raise ValueError("Free route required")
        jev = pilot.JevClient(os.environ.get("BEATAPI_API_KEY", ""))
        state = (
            json.loads(status_path.read_text())
            if status_path.exists()
            else {
                "binding": binding,
                "started_at": run.now().isoformat(),
                "deadline_at": (run.now() + timedelta(hours=24)).isoformat(),
            }
        )
        if state["binding"] != binding:
            raise ValueError("Changed packet on local JEV resume")
        while True:
            rows, unknown = run.history()
            latest = {}
            for row in sorted(rows, key=lambda r: r["finished_at"]):
                latest[(row["candidate"], row["case_id"])] = row
            eligible = [
                e
                for e in frozen["cases"]
                if staging_only(latest.get(("jev", e["case"]["case_id"]), {}))
                and not any(
                    u.get("candidate") == "jev" and u.get("case_id") == e["case"]["case_id"]
                    for u in unknown
                )
            ]
            state.update(
                updated_at=run.now().isoformat(),
                active_job=None,
                completed=sum(
                    r["status"] == "completed" for (m, _), r in latest.items() if m == "jev"
                ),
                eligible=len(eligible),
                stop_reason=None,
            )
            if not eligible:
                state["stop_reason"] = "complete_or_held"
            elif run.now() >= run.datetime.fromisoformat(state["deadline_at"]):
                state["stop_reason"] = "deadline"
            run.adapter.recovery.atomic_status(status_path, state)
            if state["stop_reason"]:
                run.backup()
                return
            enough, capacity = run.adapter.transport.headroom(client, storage, route)
            if not enough:
                state["capacity"] = capacity
                run.adapter.recovery.atomic_status(status_path, state)
                time.sleep(30)
                continue
            entry = eligible[0]
            state["active_job"] = entry["case"]["case_id"]
            run.adapter.recovery.atomic_status(status_path, state)
            try:
                row = one(entry, binding, storage, client, route, jev)
            except CapacityWait as exc:
                state["capacity"] = str(exc)
                run.adapter.recovery.atomic_status(status_path, state)
                time.sleep(30)
                continue
            print(entry["case"]["case_id"], row["status"], flush=True)
            run.backup()


if __name__ == "__main__":
    main()
