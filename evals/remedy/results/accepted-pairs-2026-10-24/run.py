"""Evaluate both accepted pairs with explicitly approved independent adjudicators."""

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import signal
import sys
import time
from dataclasses import replace
from pathlib import Path

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[3]
sys.path.insert(0, str(ROOT / "evals/remedy/results/worker-qualification-2026-10-19"))
import chat_observe  # noqa: E402
import midnight_local as local  # noqa: E402
import observe  # noqa: E402

from citypods.compute.llm_dispatch_pause import DispatchPauseClient, Selection  # noqa: E402
from citypods.compute.llm_policy import ROUTE_REGISTRY  # noqa: E402
from citypods.compute.structured import parse_structured_json  # noqa: E402
from citypods.judging.backends import JevBackend  # noqa: E402
from citypods.remedy_evaluation import canonical_hash  # noqa: E402
from citypods.security import validate_source_url  # noqa: E402

DIRECTORY = OUT / "adjudication"
REASON = "review51-accepted-pair-adjudication-2026-10-24"


def save(name, value):
    observe.write(DIRECTORY / name, value)


def jobs():
    approval = json.loads((OUT / "adjudication-approval.json").read_text())
    spec = importlib.util.spec_from_file_location(
        "previous_adjudication", OUT.parent / "gemini-replacement-2026-10-24/adjudicate.py"
    )
    previous = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(previous)
    for pair, judges in approval["judge_mapping"].items():
        frozen = approval["packets"][pair]
        path = ROOT / frozen["path"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != frozen["sha256"]:
            raise RuntimeError("Frozen packet changed")
        previous.DIRECTORY = path.parent
        for role, cid, route, payload, packet, entry in previous.jobs():
            if role == "deepseek" and "gemini" in judges:
                role = "gemini"
                route = replace(
                    ROUTE_REGISTRY["beatapi_gpt_6_1_sol_free"],
                    route_id="beatapi_gemini_3_8_flash_free",
                    upstream_model="gemini-3.8-flash-free",
                    structured_output_method="prompt_only",
                )
                payload = dict(payload, model=route.upstream_model)
                payload.pop("response_format", None)
            yield pair, role, cid, route, payload, packet, entry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    prepared = list(jobs())
    if len(prepared) != 24:
        raise RuntimeError("Expected exactly24 approved jobs")
    if not args.execute:
        print("24 frozen jobs validated; no provider calls")
        return
    DIRECTORY.mkdir(exist_ok=True)
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
            for pair, role, cid, route, payload, packet, entry in prepared:
                name = pair + "--" + role + "-" + cid
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
                    name = pair + "--" + role + "-" + cid + f"-retry{attempt:04d}"
                if (DIRECTORY / f"{name}-result.json").exists():
                    continue
                if (DIRECTORY / f"{name}-intent.json").exists():
                    raise RuntimeError("Unresolved intent; no duplicate call")
                save(
                    f"pause-{time.time_ns()}.json", local.checked(c.pause(selection, 3600, REASON))
                )
                deadline = time.monotonic() + 900
                renewed_at = time.monotonic()
                while local.checked(c.status(selection))["in_flight"]:
                    save(f"drain-{time.time_ns()}.json", local.checked(c.status(selection)))
                    if time.monotonic() - renewed_at >= 240:
                        status = local.checked(c.status(selection))
                        if any(
                            p["scope"] == "provider:beatapi" and p.get("reason") != REASON
                            for p in status["pauses"]
                        ):
                            raise RuntimeError("Foreign pause replaced owned lease")
                        save(
                            f"renew-{time.time_ns()}.json",
                            local.checked(c.pause(selection, 3600, REASON)),
                        )
                        renewed_at = time.monotonic()
                    if time.monotonic() > deadline:
                        raise RuntimeError("Provider drain timeout")
                    time.sleep(10)
                time.sleep(65)
                if role != "gemini":
                    save(f"{name}-reservation.json", local.checked(c.reserve(route.route_id, 1)))
                else:
                    save(
                        f"{name}-coordination.json",
                        {"candidate_unregistered": True, "coordination": "provider_pause"},
                    )
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
                        "pair": pair,
                        "route_id": route.route_id,
                        "case_id": cid,
                        "packet_hash": canonical_hash(entry),
                        "timeout": 720,
                    },
                )
                result = {"pair": pair, "role": role, "case_id": cid, "status": "transport_failure"}
                previous = signal.signal(signal.SIGALRM, local.timeout_handler)
                started = time.monotonic()
                response = None
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
                    result["exception_detail"] = str(exc).replace(key, "[REDACTED]")[:500]
                finally:
                    signal.setitimer(signal.ITIMER_REAL, 0)
                    if response is not None:
                        response.close()
                    signal.signal(signal.SIGALRM, previous)
                    result.update(
                        elapsed_seconds=time.monotonic() - started,
                        finished_epoch=time.time(),
                        finished_at=observe.stamp(),
                    )
                    save(f"{name}-result.json", result)
                print(pair + " " + role + " " + cid + ": " + result["status"], flush=True)
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
