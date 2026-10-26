"""Submit the three outstanding frozen jobs through the existing Worker DO queue."""

import json
import os
import time
from dataclasses import replace
from urllib.parse import urljoin
from uuid import uuid4

import chat_observe as chat
import midnight_local as local
from observe import Receipts, stamp, write

from citypods.compute.llm import (
    LLMBackendConfig,
    _dispatch_policy_json_payload,
    _dispatch_request_digest,
    estimate_tokens,
)
from citypods.compute.llm_dispatch_pause import DispatchPauseClient, Selection
from citypods.remedy_evaluation import canonical_hash
from citypods.storage.s3 import b2_from_env

OUT = chat.OUT / "queue-followup-2026-10-24"
REASON = "review51-queue-followup-2026-10-24"


def main():
    OUT.mkdir(exist_ok=True)
    local.WINDOW = OUT
    local.REASON = REASON
    client = DispatchPauseClient()
    states = []
    os.environ["B2_KEY_ID"] = os.environ["B2_DEV_KEY_ID"]
    os.environ["B2_APP_KEY"] = os.environ["B2_DEV_APP_KEY"]
    storage = b2_from_env()
    config = replace(LLMBackendConfig.from_env(), mode="dispatch")
    session = Receipts()
    contract = chat.load_contract()
    try:
        for row in chat.manifest()["jobs"]:
            route_id = row["route_id"]
            before = local.checked(client.status(Selection("route", route_id)))
            existing = next(
                (p for p in before["pauses"] if p["scope"] == "route:" + route_id), None
            )
            entry = {"route_id": route_id, "owned": existing is None, "before": before}
            states.append(entry)
            write(
                OUT / f"pause-state-{time.time_ns()}.json",
                {"routes": states, "observed_at": stamp()},
            )
            if not existing:
                entry["receipt"] = local.checked(
                    client.pause(Selection("route", route_id), 3600, REASON)
                )
            write(
                OUT / f"pause-state-{time.time_ns()}.json",
                {"routes": states, "observed_at": stamp()},
            )
        for row in chat.manifest()["jobs"]:
            role = row["role"]
            intent = OUT / f"{role}-intent.json"
            if intent.exists():
                raise RuntimeError("Prior intent exists; do not repeat submission")
            job, policy, payload, _ = chat.build(row, contract)
            recipe = canonical_hash([job.recipe_hash, "maintainer-followup-2026-10-24"])
            ref = str(uuid4())
            payload_key = f"payloads/{ref}/request.json"
            envelope = {
                "id": ref,
                "idempotency_key": f"{recipe}:durable-queue-v2",
                "request_digest": _dispatch_request_digest(payload, policy),
                "prompt_family": job.task,
                "input_token_estimate": estimate_tokens(payload["messages"])
                + (len(json.dumps(payload["structured_output"])) + 3) // 4,
                "max_output_token_estimate": 16384,
                "payload_key": payload_key,
                "priority": policy.priority,
                "policy_json": json.dumps(
                    _dispatch_policy_json_payload(policy, policy.allowed_models)
                ),
            }
            write(intent, {"submitted_at": stamp(), "envelope": envelope, "payload": payload})
            storage.put_cas(
                payload_key, json.dumps(payload, sort_keys=True).encode(), "application/json"
            )
            response = session.post(
                urljoin(config.dispatch_v2_url.rstrip("/") + "/", "v2/jobs:enqueue-batch"),
                headers={"authorization": "Bearer " + config.dispatch_v2_auth_token},
                json={"jobs": [envelope]},
                timeout=30,
            )
            response.raise_for_status()
            result = response.json()
            write(OUT / f"{role}-enqueue.json", result)
            accepted = next((v for v in result["accepted"] if v["submitted_id"] == ref), None)
            if accepted:
                write(OUT / f"{role}-handle.json", accepted)
                print(role + " queued: " + accepted["id"], flush=True)
            else:
                print(role + ": enqueue rejected; receipt saved", flush=True)
    finally:
        local.restore(client, states)


if __name__ == "__main__":
    main()
