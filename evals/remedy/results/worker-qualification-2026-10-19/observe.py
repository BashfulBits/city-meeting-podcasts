"""Approved four-job Worker observation; one submission or poll per invocation."""

import ast
import hashlib
import json
import os
import sys
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin
from uuid import uuid4

import requests

from citypods.compute.llm import (
    LiteLLMBackend,
    LLMBackendConfig,
    _dispatch_policy_json_payload,
    _dispatch_request_digest,
    _messages,
    estimate_tokens,
)
from citypods.judging.backends import Item, JevBackend, Packet
from citypods.judging.tasks import QuestionSpec, Subject, make_evidence
from citypods.remedy_evaluation import canonical_hash
from citypods.storage.s3 import b2_from_env

ROOT = Path(__file__).resolve().parents[4]
OUT = Path(__file__).resolve().parent


MANIFEST = None  # Optional explicit packet binding supplied by adjudication job builders.


def manifest():
    """Load this run's manifest only when its workflow is actually invoked."""
    if MANIFEST is not None:
        return MANIFEST
    return json.loads((OUT / "proposed-jobs.json").read_text())


def write(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write("\n")


def stamp():
    return datetime.now(UTC).isoformat()


def build(row):
    raw = (ROOT / manifest()["source_packet"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == manifest()["source_file_sha256"]
    entry = json.loads(raw)["cases"][row["packet_entry_index"]]
    assert entry["case"]["case_id"] == row["case_id"]
    assert (
        hashlib.sha256(
            json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        == row["packet_entry_sha256"]
    )
    tree = ast.parse((OUT.parent / "disagreement-2026-10-14/run.py").read_text())
    instruction = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "CHOICE_INSTRUCTION" for t in node.targets)
    )
    cid = row["case_id"]
    subject = Subject("remedy-adjudication-eval", cid, "", cid, None, {})
    evidence = make_evidence("T3", "Use the shared context.")
    items = [
        Item(
            QuestionSpec(
                "choice",
                "choose",
                instruction + "\nA = proposal A exactly; B = proposal B exactly; "
                "C = neither complete proposal is supported; "
                "D = evidence insufficient to decide between the proposals.",
            ),
            "T3",
            evidence,
            (subject,),
            options=tuple((v, v) for v in "ABCD"),
        )
    ]
    for letter in "AB":
        items.append(
            Item(
                QuestionSpec(
                    "supported_" + letter,
                    "validate",
                    instruction + f"\nIs proposal {letter}'s ENTIRE publication action "
                    "and feed set supported?",
                ),
                "T3",
                evidence,
                (subject,),
            )
        )
    packet = Packet("remedy:p3", "adjudicator", "typesafe/jev-1.13", tuple(items))
    job = JevBackend().build(packet)
    policy = replace(job.inputs["llm_policy"], allowed_route_ids=(manifest()["route_id"],))
    inputs = job.inputs | {
        "systemone": job.inputs["systemone"] | {"state": {"context": entry}},
        "llm_policy": policy,
        "num_retries": 0,
    }
    recipe = canonical_hash(
        [
            "remedy-worker-qualification-2026-10-19",
            inputs | {"llm_policy": asdict(policy)},
        ]
    )
    return packet, replace(job, inputs=inputs, recipe_hash=recipe)


class Receipts(requests.Session):
    def request(self, method, url, **kwargs):
        response = super().request(method, url, **kwargs)
        try:
            body = response.json()
        except ValueError:
            body = {"non_json_response": True}
        write(
            OUT / f"receipt-{time.time_ns()}-{uuid4().hex}.json",
            {
                "observed_at": stamp(),
                "method": method,
                "operation": url.split("/v2/")[-1].split("?")[0],
                "status_code": response.status_code,
                "body": body,
            },
        )
        return response


def main():
    jobs = [(row, *build(row)) for row in manifest()["jobs"]]
    if "--prepare" in sys.argv:
        for row, _, job in jobs:
            path = OUT / f"job-{row['job']}-request.json"
            if not path.exists():
                write(path, {"case_id": row["case_id"], **asdict(job)})
        print("Prepared four pinned JevBackend requests; no dispatch")
        return
    os.environ["B2_KEY_ID"] = os.environ["B2_DEV_KEY_ID"]
    os.environ["B2_APP_KEY"] = os.environ["B2_DEV_APP_KEY"]
    storage = b2_from_env()
    assert storage is not None
    config = replace(
        LLMBackendConfig.from_env(),
        model="typesafe/jev-1.13",
        mode="dispatch",
        dispatch_v2_client_retire=False,
    )
    backend = LiteLLMBackend(config)
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

    for row, packet, job in jobs:
        n = row["job"]
        done = OUT / f"job-{n}-result.json"
        if done.exists():
            previous = json.loads(done.read_text())
            if previous["status"] != "completed":
                print(f"Stopped at terminal job {n}")
                return
            continue
        if n > 1:
            previous = json.loads((OUT / f"job-{n - 1}-result.json").read_text())
            remaining = (
                60
                - (
                    datetime.now(UTC) - datetime.fromisoformat(previous["collected_at"])
                ).total_seconds()
            )
            if remaining > 0:
                print(f"Pacing: wait {remaining:.0f} seconds")
                return
        intent = OUT / f"job-{n}-intent.json"
        handle_path = OUT / f"job-{n}-handle.json"
        try:
            if handle_path.exists():
                ref = json.loads(handle_path.read_text())["ref"]
                status = api("jobs:poll-batch", {"ids": [ref]})
                st = next((v for v in status["statuses"] if v["id"] == ref), None)
                if not st or st["state"] not in {"completed", "failed"}:
                    print(f"Job {n} pending")
                    return
                if st["state"] == "failed":
                    write(done, {"status": "failed", "collected_at": stamp(), "receipt": st})
                    print(f"Job {n} failed; no manual retry")
                    return
                raw = storage.get_bytes(st["result_key"])
                if not raw:
                    print(f"Job {n} result not readable yet")
                    return
                output = json.loads(raw[0])
                raw_path = OUT / f"job-{n}-worker-result.json"
                if not raw_path.exists():
                    write(raw_path, output)
                identity_ok = (
                    st.get("route_id") == manifest()["route_id"]
                    and st.get("model") == "typesafe/jev-1.13"
                    and output.get("model") == manifest()["upstream_model"]
                )
                parsed = [v for _, v in JevBackend().parse(packet, output)]
                write(
                    done,
                    {
                        "status": "completed" if identity_ok else "identity_hold",
                        "collected_at": stamp(),
                        "case_id": row["case_id"],
                        "identity_ok": identity_ok,
                        "parsed": parsed,
                        "worker_status": st,
                        "raw_response": output,
                    },
                )
                print(f"Job {n} collected: identity_ok={identity_ok}, answers={len(parsed)}")
                return
            if intent.exists():
                print(f"Unknown submission for job {n}; stopped without resubmission")
                return
            policy = job.inputs["llm_policy"]
            payload = backend._payload(
                job,
                None,
                resolved_model="typesafe/jev-1.13",
                schema_only=True,
            )
            ref = str(uuid4())
            payload_key = f"payloads/{ref}/request.json"
            envelope = {
                "id": ref,
                "idempotency_key": f"{job.recipe_hash}:durable-queue-v2",
                "request_digest": _dispatch_request_digest(payload, policy),
                "prompt_family": job.task,
                "input_token_estimate": estimate_tokens(_messages(job)),
                "max_output_token_estimate": job.inputs["max_tokens"],
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
                print(f"Job {n} not accepted; inspect receipt without resubmission")
                return
            write(handle_path, {"ref": accepted["id"], "recipe_hash": job.recipe_hash})
            print(f"Job {n} queued: {accepted['id']}")
        except Exception as exc:
            write(
                OUT / f"job-{n}-error-{time.time_ns()}.json",
                {
                    "observed_at": stamp(),
                    "exception_class": type(exc).__name__,
                    "terminal_reason": getattr(exc, "terminal_reason", None),
                },
            )
            print(f"Job {n} stopped: {type(exc).__name__}; no manual retry")
        return
    print("All four jobs collected")


if __name__ == "__main__":
    main()
