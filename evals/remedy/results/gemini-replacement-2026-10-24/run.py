"""Frozen Gemini replacement evaluation; candidate probes under approved provider pause."""

import argparse
import fcntl
import hashlib
import json
import os
import signal
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import requests

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[3]
sys.path.insert(0, str(ROOT / "evals/remedy/results/body-selection-2026-10-13"))
from body_selection import load_manifest, validate_answer  # noqa: E402

from citypods.compute.llm_dispatch_pause import DispatchPauseClient, Selection  # noqa: E402
from citypods.compute.llm_policy import ROUTE_REGISTRY  # noqa: E402
from citypods.compute.structured import parse_structured_json  # noqa: E402
from citypods.compute.structured_shaping import shape_for_route  # noqa: E402
from citypods.security import validate_source_url  # noqa: E402

REASON = "review51-gemini-replacement-2026-10-24"
MODEL = "gemini-3.8-flash-free"
CANDIDATE_ROUTE_ID = "beatapi_gemini_3_8_flash_free"
BASE_ROUTE_ID = "beatapi_gpt_6_1_sol_free"
PROVIDER_OVERRIDE = None
API_BASE_OVERRIDE = None
PARTITION = None
SPACING_SECONDS = 65
STREAMING = False
OUTPUT_CAP = 16384


def stamp():
    return datetime.now(UTC).isoformat()


def save(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


def checked(value):
    if value.get("ok") is not True:
        raise RuntimeError("Worker control refused")
    return value


def timeout(_signal, _frame):
    raise TimeoutError("720-second response deadline")


def streamed_body(response):
    from citypods.provider_catalog.probe import _iter_sse_data_events

    events, content, reasoning = [], [], []
    model, finish, usage, done, count = None, None, None, False, 0
    for data, _, _, _ in _iter_sse_data_events(
        response.iter_lines(chunk_size=1), on_line=lambda: True
    ):
        count += len(data.encode())
        if count > max(4 * 1024 * 1024, OUTPUT_CAP * 1024):
            raise RuntimeError("Stream receipt exceeded output-profile byte bound")
        if data == "[DONE]":
            done = True
            break
        event = json.loads(data)
        if event.get("error"):
            failure = RuntimeError("Upstream stream error")
            failure.provider_error = event["error"]
            raise failure
        events.append(event)
        if event.get("model"):
            if model and event["model"] != model:
                raise RuntimeError("Conflicting streamed model identity")
            model = event["model"]
        if event.get("usage"):
            usage = event["usage"]
        for choice in event.get("choices", []):
            if choice.get("index", 0) != 0:
                raise RuntimeError("Unexpected streamed choice index")
            delta = choice.get("delta", {})
            content.append(delta.get("content") or "")
            reasoning.append(delta.get("reasoning_content") or "")
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
    if not done or not finish:
        raise RuntimeError("Incomplete upstream stream")
    return {
        "model": model,
        "choices": [
            {
                "message": {"content": "".join(content), "reasoning_content": "".join(reasoning)},
                "finish_reason": finish,
            }
        ],
        "usage": usage,
        "stream_events": events,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--account", choices=("primary", "secondary"), default="primary")
    parser.add_argument("--all-cases", action="store_true")
    args = parser.parse_args()
    if args.all_cases and (
        args.account != "primary" or not (OUT / "primary-completion-approval.json").exists()
    ):
        raise RuntimeError("All-case primary completion requires recorded authorization")
    manifest = json.loads((OUT / "manifest.json").read_text())
    for row in manifest["requests"]:
        if hashlib.sha256((ROOT / row["path"]).read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError("Frozen request changed")
    if not args.execute:
        print("60 frozen requests validated; no calls")
        return
    if not (OUT / "provider-pause-approval.json").exists():
        raise RuntimeError("Provider pause approval missing")
    for name in ("intents", "results", "receipts"):
        (OUT / name).mkdir(exist_ok=True)
    with (OUT / f"controller-{args.account}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        cases = {c.case_id: c for c in load_manifest()[1]}
        c = DispatchPauseClient()
        selection = Selection(
            "provider",
            PROVIDER_OVERRIDE or ("beatapi" if args.account == "primary" else "beatapi_secondary"),
        )
        before = checked(c.status(selection))
        save(OUT / "receipts" / f"before-{time.time_ns()}.json", before)
        foreign = [
            p
            for p in before["pauses"]
            if p["scope"] == "provider:" + selection.target and p.get("reason") != REASON
        ]
        if foreign or any(p["scope"] == "global" for p in before["pauses"]):
            raise RuntimeError("Foreign maintenance pause; no calls")
        base = ROUTE_REGISTRY[BASE_ROUTE_ID]
        route = replace(
            base,
            route_id=CANDIDATE_ROUTE_ID
            if args.account == "primary"
            else CANDIDATE_ROUTE_ID.replace("beatapi_", "beatapi_secondary_", 1),
            upstream_model=MODEL,
            api_base=API_BASE_OVERRIDE or base.api_base,
            provider=selection.target,
            account_id=args.account,
            structured_output_method="prompt_only",
        )
        key_env = route.api_key_env if args.account == "primary" else "BEATAPI_API_KEY_SECONDARY"
        key = os.environ.get(key_env, "")
        if not key.strip():
            raise RuntimeError("Selected account credential is empty; no calls")
        url = route.api_base.rstrip("/") + "/" + route.chat_path.lstrip("/")
        validate_source_url(url)
        previous = signal.signal(signal.SIGALRM, timeout)
        try:
            for index, row in enumerate(manifest["requests"]):
                if PARTITION is not None and index % 2 != PARTITION:
                    continue
                if not args.all_cases and index % 2 != (0 if args.account == "primary" else 1):
                    continue
                req = json.loads((ROOT / row["path"]).read_text())
                case_id = req["case_id"]
                done = OUT / "results" / f"{case_id}.json"
                intent = OUT / "intents" / f"{case_id}.json"
                attempt = 0
                while done.exists():
                    prior = json.loads(done.read_text())
                    retryable = prior.get("status") == "transport_failure" or (
                        prior.get("http_status", 0) >= 500 or prior.get("http_status") == 429
                    )
                    if (
                        args.all_cases
                        and prior.get("account") == "secondary"
                        and prior.get("http_status") == 403
                    ):
                        # Explicit renewed authorization: finish the packet with primary only.
                        # Keep the secondary denial intact; no secondary access is retried.
                        retryable = True
                    prior_request_path = OUT / "intents" / (done.stem + ".json")
                    prior_cap = json.loads(prior_request_path.read_text())["request"]["max_tokens"]
                    truncated = any(
                        c.get("finish_reason") == "length"
                        for c in prior.get("raw_response", {}).get("choices", [])
                    )
                    if (
                        "answer" not in prior
                        and truncated
                        and OUTPUT_CAP > prior_cap
                        and (OUT / "output-cap-approval.json").exists()
                    ):
                        retryable = True
                    if not retryable:
                        break
                    attempt += 1
                    done = OUT / "results" / f"{case_id}-retry{attempt:04d}.json"
                    intent = OUT / "intents" / f"{case_id}-retry{attempt:04d}.json"
                if done.exists():
                    continue
                if attempt:
                    # Explicit maintainer authorization: retry upstream failures until answered.
                    # Back off five minutes per case; never repeat a completed model answer.
                    from datetime import timedelta

                    ready = datetime.fromisoformat(prior["finished_at"]) + timedelta(minutes=5)
                    if datetime.now(UTC) < ready:
                        continue
                if intent.exists():
                    raise RuntimeError("Unresolved intent; never repeat unknown call")
                pause = checked(c.pause(selection, 3600, REASON))
                save(OUT / "receipts" / f"pause-{time.time_ns()}.json", pause)
                deadline = time.monotonic() + 900
                renewed_at = time.monotonic()
                while True:
                    if time.monotonic() - renewed_at >= 240:
                        current = checked(c.status(selection))
                        foreign = any(
                            p["scope"] == "provider:" + selection.target
                            and p.get("reason") != REASON
                            for p in current["pauses"]
                        )
                        if foreign:
                            raise RuntimeError("Foreign pause replaced evaluation lease")
                        save(
                            OUT / "receipts" / f"renew-drain-{time.time_ns()}.json",
                            checked(c.pause(selection, 3600, REASON)),
                        )
                        renewed_at = time.monotonic()
                    status = checked(c.status(selection))
                    save(OUT / "receipts" / f"drain-{time.time_ns()}.json", status)
                    if status["in_flight"] == 0:
                        break
                    if time.monotonic() > deadline:
                        raise RuntimeError("Provider did not drain in 15 minutes")
                    time.sleep(10)
                # Covers prior local calls, including across controller resumes.
                time.sleep(SPACING_SECONDS)
                messages, fmt = shape_for_route(
                    req["messages"],
                    name="BodySelectionAnswer",
                    schema=req["response_schema"],
                    route=route,
                )
                payload = {
                    "model": MODEL,
                    "messages": messages,
                    "max_tokens": OUTPUT_CAP,
                    "stream": STREAMING,
                }
                if fmt:
                    payload["response_format"] = fmt
                save(
                    intent,
                    {
                        "observed_at": stamp(),
                        "request": payload,
                        "account": args.account,
                        "request_url": url,
                        "candidate_unregistered": True,
                        "coordination": "provider_pause",
                        "timeout_seconds": 720,
                        "client_retries": 0,
                    },
                )
                result = {
                    "account": args.account,
                    "attempt": attempt + 1,
                    "case_id": case_id,
                    "started_at": stamp(),
                    "requested_model": MODEL,
                    "output_cap": OUTPUT_CAP,
                    "request_url": url,
                }
                started = time.monotonic()
                response = None
                try:
                    signal.setitimer(signal.ITIMER_REAL, 720)
                    response = requests.post(
                        url,
                        headers={"Authorization": "Bearer " + key},
                        json=payload,
                        timeout=(20, 720),
                        allow_redirects=False,
                        stream=STREAMING,
                    )
                    result["http_status"] = response.status_code
                    try:
                        body = (
                            streamed_body(response)
                            if STREAMING
                            and response.status_code == 200
                            and "text/event-stream" in response.headers.get("Content-Type", "")
                            else response.json()
                        )
                    except ValueError:
                        if STREAMING and response.status_code == 200:
                            raise RuntimeError("Malformed upstream stream") from None
                        body = {"non_json": True}
                    body = json.loads(json.dumps(body).replace(key, "[REDACTED]"))
                    result["raw_response"] = body
                    result["returned_model"] = body.get("model")
                    result["identity_matches"] = body.get("model") == MODEL
                    result["status"] = (
                        "completed" if response.status_code == 200 else "http_failure"
                    )
                    if response.status_code == 200:
                        try:
                            raw = parse_structured_json(body["choices"][0]["message"]["content"])
                            answer = validate_answer(raw, cases[case_id])
                            result["answer"] = answer.model_dump(mode="json")
                        except Exception as exc:
                            result["schema_error"] = type(exc).__name__
                except Exception as exc:
                    result.update(
                        status="transport_failure",
                        exception_class=type(exc).__name__,
                        exception_detail=str(exc).replace(key, "[REDACTED]")[:500],
                    )
                    if hasattr(exc, "provider_error"):
                        error = json.loads(
                            json.dumps(exc.provider_error).replace(key, "[REDACTED]")
                        )
                        result["provider_error"] = error
                        code = error.get("code") if isinstance(error, dict) else None
                        if code in {
                            "forbidden",
                            "invalid_api_key",
                            "missing_api_key",
                            "insufficient_credits",
                            "not_entitled",
                        }:
                            result.update(status="access_hold", provider_access_hold=True)
                finally:
                    signal.setitimer(signal.ITIMER_REAL, 0)
                    if response is not None:
                        response.close()
                    result.update(finished_at=stamp(), elapsed_seconds=time.monotonic() - started)
                    save(done, result)
                print(f"{index + 1}/60 {case_id}: {result['status']}", flush=True)
                if result.get("provider_access_hold") or result.get("http_status") in {
                    401,
                    402,
                    403,
                    404,
                    429,
                }:
                    raise RuntimeError("Provider access/capacity hold; no automatic retries")
        finally:
            signal.signal(signal.SIGALRM, previous)
            status = checked(c.status(selection))
            owned = any(
                p["scope"] == "provider:" + selection.target and p.get("reason") == REASON
                for p in status["pauses"]
            )
            if owned:
                save(
                    OUT / "receipts" / f"restore-{time.time_ns()}.json",
                    checked(c.resume(selection)),
                )


if __name__ == "__main__":
    main()
