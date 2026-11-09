"""Evaluate the actual replacement-pair disagreements on approved primary routes."""

import fcntl
import hashlib
import json
import os
import signal
import sys
import time
from pathlib import Path

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[3]
sys.path.insert(0, str(ROOT / "evals/remedy/results/worker-qualification-2026-10-19"))
import observe  # noqa: E402

from citypods.compute.llm_dispatch_pause import DispatchPauseClient, Selection  # noqa: E402
from citypods.compute.llm_policy import ROUTE_REGISTRY  # noqa: E402
from citypods.compute.structured import parse_structured_json  # noqa: E402
from citypods.compute.structured_shaping import shape_for_route  # noqa: E402
from citypods.judging.backends import JevBackend  # noqa: E402
from citypods.remedy_evaluation import canonical_hash  # noqa: E402
from citypods.security import validate_source_url  # noqa: E402
from evals.remedy import evaluation_support as chat_observe  # noqa: E402
from evals.remedy import evaluation_support as local  # noqa: E402

DIRECTORY = OUT / "adjudication"
REASON = "review51-gemini-adjudication-2026-10-24"


def save(name, value):
    observe.write(DIRECTORY / name, value)


def jobs():
    path = DIRECTORY / "packet.json"
    frozen = json.loads(path.read_text())
    observe.MANIFEST = {
        "source_packet": str(path.relative_to(ROOT)),
        "source_file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "route_id": "beatapi_jev_1_13_free",
    }
    contract = chat_observe.load_contract()
    # Reuse the exact approved adjudication instruction, not a gold-derived rewrite.
    import ast

    tree = ast.parse((ROOT / "evals/remedy/results/disagreement-2026-10-14/run.py").read_text())
    instruction = next(
        ast.literal_eval(n.value)
        for n in tree.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "CHOICE_INSTRUCTION" for t in n.targets)
    )
    suffix = """
Independently return the final correct body identity, publication action and ALL appropriate
existing feeds; you may reject both proposed feed sets and choose another supported set.
Use the supplied body-selection JSON schema, cite only evidence IDs in case.evidence_refs and
echo case.case_id. Cite the evidence that resolves the disagreement; if it remains unresolved,
name the missing proof. Do not silently treat a historical feed inclusion as authoritative."""
    for index, entry in enumerate(frozen["cases"]):
        cid = entry["case"]["case_id"]
        route = ROUTE_REGISTRY["beatapi_deepseek_v4_1_flash_free"]
        messages, fmt = shape_for_route(
            [
                {"role": "system", "content": instruction + suffix},
                {"role": "user", "content": json.dumps(entry, sort_keys=True)},
            ],
            name="BodySelectionAnswer",
            schema=contract.BodySelectionAnswer.model_json_schema(),
            route=route,
        )
        payload = {
            "model": route.upstream_model,
            "messages": messages,
            "max_tokens": 16384,
            "stream": False,
        }
        if fmt:
            payload["response_format"] = fmt
        yield "deepseek", cid, route, payload, None, entry
        encoded = json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        packet, job = observe.build(
            {
                "case_id": cid,
                "packet_entry_index": index,
                "packet_entry_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
            }
        )
        route = ROUTE_REGISTRY["beatapi_jev_1_13_free"]
        yield (
            "jev",
            cid,
            route,
            {"model": route.upstream_model, **job.inputs["systemone"]},
            packet,
            entry,
        )


def main():
    with (DIRECTORY / "controller.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        c = DispatchPauseClient()
        selection = Selection("provider", "beatapi")
        before = local.checked(c.status(selection))
        if any(
            p["scope"] in {"global", "provider:beatapi"} and p.get("reason") != REASON
            for p in before["pauses"]
        ):
            raise RuntimeError("Foreign provider pause; no requests")
        try:
            for role, cid, route, payload, packet, entry in jobs():
                name = role + "-" + cid
                attempt = 0
                while (DIRECTORY / f"{name}-result.json").exists():
                    prior = json.loads((DIRECTORY / f"{name}-result.json").read_text())
                    if prior["status"] == "completed":
                        break
                    if not (
                        prior.get("http_status", 0) >= 500
                        or prior.get("http_status") == 429
                        or prior["status"] == "transport_failure"
                    ):
                        raise RuntimeError("Nonretryable adjudication hold")
                    if time.time() - prior["finished_epoch"] < 300:
                        break
                    attempt += 1
                    name = role + "-" + cid + f"-retry{attempt:04d}"
                if (DIRECTORY / f"{name}-result.json").exists():
                    continue
                if (DIRECTORY / f"{name}-intent.json").exists():
                    raise RuntimeError("Unresolved intent; no duplicate call")
                save(
                    f"pause-{time.time_ns()}.json", local.checked(c.pause(selection, 3600, REASON))
                )
                deadline = time.monotonic() + 900
                while local.checked(c.status(selection))["in_flight"]:
                    if time.monotonic() > deadline:
                        raise RuntimeError("Provider drain timeout")
                    time.sleep(10)
                time.sleep(65)
                save(f"{name}-reservation.json", local.checked(c.reserve(route.route_id, 1)))
                key = os.environ[route.api_key_env]
                url = route.api_base.rstrip("/") + (
                    "/systemone" if role == "jev" else "/" + route.chat_path.lstrip("/")
                )
                validate_source_url(url)
                save(
                    f"{name}-intent.json",
                    {
                        "payload": payload,
                        "role": role,
                        "case_id": cid,
                        "packet_hash": canonical_hash(entry),
                        "timeout": 720,
                    },
                )
                result = {"role": role, "case_id": cid, "status": "transport_failure"}
                previous = signal.signal(signal.SIGALRM, local.timeout_handler)
                started = time.monotonic()
                try:
                    signal.setitimer(signal.ITIMER_REAL, 720)
                    import requests

                    response = requests.post(
                        url,
                        headers={"Authorization": "Bearer " + key},
                        json=payload,
                        timeout=(20, 720),
                        allow_redirects=False,
                    )
                    result["http_status"] = response.status_code
                    try:
                        raw = response.json()
                    except ValueError:
                        raw = {"non_json_response": True}
                    raw = json.loads(json.dumps(raw).replace(key, "[REDACTED]"))
                    result.update(
                        raw_response=raw,
                        returned_model=raw.get("model"),
                        identity_matches=raw.get("model") == route.upstream_model,
                        status="completed" if response.status_code == 200 else "http_failure",
                    )
                    if response.status_code == 200:
                        try:
                            if role == "jev":
                                result["parsed"] = [v for _, v in JevBackend().parse(packet, raw)]
                            else:
                                contract = chat_observe.load_contract()
                                answer = parse_structured_json(
                                    raw["choices"][0]["message"]["content"]
                                )
                                result["answer"] = contract.validate_answer(
                                    answer, contract.SelectionInput.model_validate(entry["case"])
                                ).model_dump(mode="json")
                        except Exception as exc:
                            result["schema_error"] = type(exc).__name__
                except Exception as exc:
                    result["exception_class"] = type(exc).__name__
                finally:
                    signal.setitimer(signal.ITIMER_REAL, 0)
                    signal.signal(signal.SIGALRM, previous)
                    result.update(
                        elapsed_seconds=time.monotonic() - started,
                        finished_epoch=time.time(),
                        finished_at=observe.stamp(),
                    )
                    save(f"{name}-result.json", result)
                print(role + " " + cid + ": " + result["status"], flush=True)
                if result.get("http_status") in {401, 402, 403, 404, 429}:
                    raise RuntimeError("Provider access/capacity hold")
        finally:
            current = local.checked(c.status(selection))
            if any(
                p["scope"] == "provider:beatapi" and p.get("reason") == REASON
                for p in current["pauses"]
            ):
                save(f"restore-{time.time_ns()}.json", local.checked(c.resume(selection)))


if __name__ == "__main__":
    main()
