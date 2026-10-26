"""Three approved pinned chat observations using DO queue and local receipts."""

import hashlib
import importlib.util
import json
import os
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from urllib.parse import urljoin
from uuid import uuid4

from observe import OUT, ROOT, Receipts, stamp, write

from citypods.compute.base import InferenceJob
from citypods.compute.llm import (
    LiteLLMBackend,
    LLMBackendConfig,
    _dispatch_policy_json_payload,
    _dispatch_request_digest,
    estimate_tokens,
)
from citypods.compute.llm_policy import ROUTE_REGISTRY, LLMRequestPolicy
from citypods.remedy_evaluation import canonical_hash
from citypods.storage.s3 import b2_from_env


def manifest():
    """Load this run's manifest only when its workflow is actually invoked."""
    return json.loads((OUT / "proposed-chat-jobs.json").read_text())


def load_contract():
    path = ROOT / "evals/remedy/results/body-selection-2026-10-13/body_selection.py"
    spec = importlib.util.spec_from_file_location("frozen_chat_contract", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def build(row, contract):
    path = ROOT / row["request_snapshot"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == row["request_snapshot_sha256"]
    request = json.loads(path.read_text())
    assert request["response_schema"] == contract.BodySelectionAnswer.model_json_schema()
    assert request["route_id"] == row["route_id"]
    route = ROUTE_REGISTRY[row["route_id"]]
    assert route.upstream_model == row["upstream_model"]
    policy = LLMRequestPolicy(
        purpose="remedy:p3",
        queue_only=True,
        allow_paid=False,
        timeout_class="long",
        allowed_models=(route.model,),
        allowed_route_ids=(route.route_id,),
    )
    job = InferenceJob(
        task="judge",
        recipe_hash=canonical_hash(
            [
                "remedy-chat-worker-identity-2026-10-19",
                row["request_snapshot_sha256"],
                row["route_id"],
                *([row["operational_retry"]] if row.get("operational_retry") else []),
            ]
        ),
        inputs={
            "messages": request["messages"],
            "max_tokens": 16384,
            "num_retries": 0,
            "llm_policy": policy,
        },
    )
    backend = LiteLLMBackend(LLMBackendConfig(model=route.model))
    payload = backend._payload(
        job,
        contract.BodySelectionAnswer,
        resolved_model=route.model,
        schema_only=True,
    )
    assert payload["max_tokens"] == 16384
    assert "temperature" not in payload and "reasoning_effort" not in payload
    return job, policy, payload, route.model


def main():
    assert manifest()["status"] == "authorized"
    contract = load_contract()
    jobs = [(row, *build(row, contract)) for row in manifest()["jobs"]]
    if "--prepare" in sys.argv:
        print("Three frozen snapshots validated; no dispatch")
        return
    os.environ["B2_KEY_ID"] = os.environ["B2_DEV_KEY_ID"]
    os.environ["B2_APP_KEY"] = os.environ["B2_DEV_APP_KEY"]
    storage = b2_from_env()
    assert storage is not None
    config = replace(LLMBackendConfig.from_env(), mode="dispatch")
    session = Receipts()

    def api(operation, body):
        response = session.post(
            urljoin(config.dispatch_v2_url.rstrip("/") + "/", "v2/" + operation),
            headers={"authorization": "Bearer " + config.dispatch_v2_auth_token},
            json=body,
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    previous = None
    for row, job, policy, payload, logical_model in jobs:
        role = row["role"]
        artifact_role = role + row.get("operational_retry", {}).get("artifact_suffix", "")
        done = OUT / f"chat-{artifact_role}-result.json"
        if done.exists():
            previous = json.loads(done.read_text())
            if previous["status"] != "completed":
                print(f"Stopped at {role} hold")
                return
            continue
        if previous:
            remaining = (
                60
                - (
                    datetime.now(UTC) - datetime.fromisoformat(previous["collected_at"])
                ).total_seconds()
            )
            if remaining > 0:
                print(f"Pacing: wait {remaining:.0f} seconds")
                return
        intent = OUT / f"chat-{artifact_role}-intent.json"
        handle_path = OUT / f"chat-{artifact_role}-handle.json"
        try:
            if handle_path.exists():
                ref = json.loads(handle_path.read_text())["ref"]
                status = api("jobs:poll-batch", {"ids": [ref]})
                st = next((v for v in status["statuses"] if v["id"] == ref), None)
                if not st or st["state"] not in {"completed", "failed"}:
                    print(f"{role} pending")
                    return
                if st["state"] == "failed":
                    write(done, {"status": "failed", "collected_at": stamp(), "receipt": st})
                    print(f"{role} failed; no manual retry")
                    return
                raw = storage.get_bytes(st["result_key"])
                if not raw:
                    print(f"{role} result not readable yet")
                    return
                output = json.loads(raw[0])
                write(OUT / f"chat-{artifact_role}-worker-result.json", output)
                identity_ok = (
                    st.get("route_id") == row["route_id"]
                    and st.get("model") == logical_model
                    and output.get("model") == row["upstream_model"]
                )
                validation = {"status": "unparsed"}
                try:
                    content = output["choices"][0]["message"]["content"]
                    parsed = json.loads(content) if isinstance(content, str) else content
                    case = json.loads((ROOT / row["source_packet"]).read_text())["cases"][
                        row["packet_entry_index"]
                    ]["case"]
                    answer = contract.validate_answer(
                        parsed,
                        contract.SelectionInput.model_validate(case),
                    )
                    validation = {"status": "valid", "answer": answer.model_dump(mode="json")}
                except Exception as exc:
                    validation = {"status": "invalid", "exception_class": type(exc).__name__}
                write(
                    done,
                    {
                        "status": "completed" if identity_ok else "identity_hold",
                        "collected_at": stamp(),
                        "case_id": row["case_id"],
                        "identity_ok": identity_ok,
                        "worker_status": st,
                        "validation": validation,
                        "raw_response": output,
                        "serving_family_attested": False,
                    },
                )
                print(f"{role} collected: identity_ok={identity_ok}, schema={validation['status']}")
                return
            if intent.exists():
                print(f"{role} unknown submission; stopped without resubmission")
                return
            ref = str(uuid4())
            payload_key = f"payloads/{ref}/request.json"
            envelope = {
                "id": ref,
                "idempotency_key": f"{job.recipe_hash}:durable-queue-v2",
                "request_digest": _dispatch_request_digest(payload, policy),
                "prompt_family": job.task,
                "input_token_estimate": estimate_tokens(payload["messages"])
                + (len(json.dumps(payload["structured_output"])) + 3) // 4,
                "max_output_token_estimate": 16384,
                "payload_key": payload_key,
                "priority": policy.priority,
                "policy_json": json.dumps(
                    _dispatch_policy_json_payload(
                        policy,
                        policy.allowed_models,
                    )
                ),
            }
            write(
                intent,
                {
                    "approved_at": "maintainer chat 2026-10-19",
                    "submitted_at": stamp(),
                    "recipe_hash": job.recipe_hash,
                    "envelope": envelope,
                    "payload": payload,
                },
            )
            storage.put_cas(
                payload_key, json.dumps(payload, sort_keys=True).encode(), "application/json"
            )
            result = api("jobs:enqueue-batch", {"jobs": [envelope]})
            accepted = next((v for v in result["accepted"] if v["submitted_id"] == ref), None)
            if not accepted:
                print(f"{role} not accepted; inspect receipt without resubmission")
                return
            write(handle_path, {"ref": accepted["id"], "recipe_hash": job.recipe_hash})
            print(f"{role} queued: {accepted['id']}")
        except Exception as exc:
            write(
                OUT / f"chat-{artifact_role}-error-{time.time_ns()}.json",
                {
                    "observed_at": stamp(),
                    "exception_class": type(exc).__name__,
                },
            )
            print(f"{role} stopped: {type(exc).__name__}; no manual retry")
        return
    print("All three chat observations collected")


if __name__ == "__main__":
    main()
