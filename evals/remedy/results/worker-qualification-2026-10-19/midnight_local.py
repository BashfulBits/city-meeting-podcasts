"""One authorized UTC-boundary local observation per pinned route; no implicit retries."""

import argparse
import fcntl
import hashlib
import json
import os
import signal
import time
from datetime import UTC, datetime, timedelta

import requests
from chat_observe import load_contract, manifest
from observe import OUT, ROOT, stamp, write

from citypods.compute.llm_dispatch_pause import DispatchPauseClient, Selection
from citypods.compute.llm_policy import ROUTE_REGISTRY, route_request_params
from citypods.compute.structured import parse_structured_json
from citypods.compute.structured_shaping import shape_for_route
from citypods.security import validate_source_url

WINDOW = OUT / "midnight-local-2026-10-24"
PAUSE_AT = datetime(2026, 10, 23, 23, 50, tzinfo=UTC)
START_AT = datetime(2026, 10, 24, 0, 1, tzinfo=UTC)
REASON = "review51-midnight-local-2026-10-24"


def now():
    return datetime.now(UTC)


def save(name, value):
    WINDOW.mkdir(exist_ok=True)
    write(WINDOW / name, value)


def checked(value):
    if value.get("ok") is not True:
        raise RuntimeError("Worker control did not acknowledge operation")
    return value


def requests_for_run():
    result = []
    for row in manifest()["jobs"]:
        path = ROOT / row["request_snapshot"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != row["request_snapshot_sha256"]:
            raise ValueError("Frozen request changed")
        request = json.loads(path.read_text())
        route = ROUTE_REGISTRY[row["route_id"]]
        if not route.free or route.upstream_model != row["upstream_model"]:
            raise ValueError("Pinned free identity changed")
        if route_request_params(route):
            raise ValueError("Evaluated empty route parameter map changed")
        messages, response_format = shape_for_route(
            request["messages"],
            name="BodySelectionAnswer",
            schema=request["response_schema"],
            route=route,
        )
        payload = {
            "model": route.upstream_model,
            "messages": messages,
            "max_tokens": 16384,
            "stream": False,
        }
        if response_format:
            payload["response_format"] = response_format
        result.append((row, route, payload))
    return result


def status(client, route):
    return checked(client.status(Selection("route", route.route_id)))


def active_pause(value, route):
    scope = "route:" + route.route_id
    return next((p for p in value["pauses"] if p["scope"] == scope), None)


def restore(client, states):
    errors = []
    for entry in reversed(states):
        if not entry.get("owned"):
            continue
        route = ROUTE_REGISTRY[entry["route_id"]]
        try:
            current = active_pause(status(client, route), route)
            if current and current.get("reason") == REASON:
                checked(client.resume(Selection("route", route.route_id)))
        except Exception as exc:
            errors.append({"route_id": route.route_id, "error_class": type(exc).__name__})
    save(f"restoration-{time.time_ns()}.json", {"observed_at": stamp(), "errors": errors})
    return errors


def pause_phase(client, jobs):
    if not PAUSE_AT <= now() < START_AT:
        raise ValueError("Outside one-shot pause window")
    states = []
    try:
        for _, route, _ in jobs:
            before = status(client, route)
            existing = active_pause(before, route)
            if existing:
                if existing["paused_until"] < int(
                    (PAUSE_AT + timedelta(hours=1)).timestamp() * 1000
                ):
                    raise RuntimeError("Foreign route pause expires before observation window")
                states.append({"route_id": route.route_id, "owned": False, "before": before})
            else:
                # Record ownership intent before the mutation, so ambiguous responses can recover.
                entry = {"route_id": route.route_id, "owned": True, "before": before}
                states.append(entry)
                save(f"pause-intent-{route.route_id}.json", entry)
                reply = checked(client.pause(Selection("route", route.route_id), 3600, REASON))
                entry["receipt"] = reply
            save(f"pause-{route.route_id}.json", states[-1])
        save("pause-state.json", {"observed_at": stamp(), "routes": states})
        print("Three route pauses acknowledged; no LLM calls", flush=True)
    except Exception:
        restore(client, states)
        raise


def timeout_handler(_signal, _frame):
    raise TimeoutError("Approved 720-second provider response deadline")


def run_phase(client, jobs):
    state_path = WINDOW / "pause-state.json"
    if not state_path.exists():
        raise RuntimeError("Pre-midnight pause phase not completed; no direct requests")
    states = json.loads(state_path.read_text())["routes"]
    summary = []
    try:
        if not START_AT <= now() < START_AT + timedelta(minutes=9):
            raise ValueError("Outside one-shot launch window")
        # Drain all requested routes, without changing foreign or provider-wide pauses.
        deadline = time.monotonic() + 180
        while True:
            snapshots = [status(client, route) for _, route, _ in jobs]
            if all(value["in_flight"] == 0 for value in snapshots):
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("Selected routes did not drain; no direct calls")
            time.sleep(10)
        previous_end = None
        for row, route, payload in jobs:
            role = row["role"]
            result_path = WINDOW / f"{role}-result.json"
            intent_path = WINDOW / f"{role}-intent.json"
            if intent_path.exists() or result_path.exists():
                summary.append({"role": role, "status": "prior_intent_not_repeated"})
                continue
            if previous_end:
                time.sleep(max(0, 60 - (time.monotonic() - previous_end)))
            entry = {"role": role, "route_id": route.route_id, "observed_at": stamp()}
            current = status(client, route)
            pause = active_pause(current, route)
            # Need time for the complete bounded request and local cleanup under this lease.
            if not pause or pause["paused_until"] < int(now().timestamp() * 1000) + 750000:
                entry["status"] = "pause_window_insufficient"
                save(result_path.name, entry)
                summary.append(entry)
                continue
            provider = checked(client.status(Selection("provider", route.provider)))
            if provider["in_flight"]:
                entry["status"] = "provider_busy_no_call"
                save(result_path.name, entry)
                summary.append(entry)
                continue
            # A provider-wide foreign maintenance pause is not ours to bypass.
            if any(
                p["scope"] in {"global", "provider:" + route.provider} for p in provider["pauses"]
            ):
                entry["status"] = "foreign_provider_pause_no_call"
                save(result_path.name, entry)
                summary.append(entry)
                continue
            quota = provider["routes"].get(route.route_id, {})
            if quota.get("rpd_remaining", 0) <= 0:
                entry["status"] = "daily_capacity_unavailable_no_call"
                save(result_path.name, entry)
                summary.append(entry)
                continue
            key = os.environ.get(route.api_key_env)
            if not key:
                raise RuntimeError("Pinned provider credential missing")
            url = route.api_base.rstrip("/") + "/" + route.chat_path.lstrip("/")
            validate_source_url(url)
            reservation = checked(client.reserve(route.route_id, 1))
            save(f"{role}-reservation.json", reservation)
            save(
                intent_path.name,
                {**entry, "request": payload, "timeout_seconds": 720, "client_retries": 0},
            )
            previous_handler = signal.signal(signal.SIGALRM, timeout_handler)
            started = time.monotonic()
            try:
                signal.setitimer(signal.ITIMER_REAL, 720)
                response = requests.post(
                    url,
                    headers={"Authorization": "Bearer " + key},
                    json=payload,
                    timeout=(20, 720),
                    allow_redirects=False,
                )
                entry["http_status"] = response.status_code
                entry["headers"] = {
                    k: response.headers[k]
                    for k in ("Retry-After", "X-Request-ID", "Date")
                    if k in response.headers
                }
                try:
                    body = response.json()
                except ValueError:
                    body = {"non_json_response": True}
                # Never persist a credential reflected by an upstream error or output.
                encoded = json.dumps(body)
                for _, other_route, _ in jobs:
                    secret = os.environ.get(other_route.api_key_env)
                    if secret:
                        encoded = encoded.replace(secret, "[REDACTED]")
                body = json.loads(encoded)
                entry["raw_response"] = body
                entry["status"] = "completed" if response.status_code == 200 else "http_failure"
                entry["returned_upstream"] = body.get("model") if isinstance(body, dict) else None
                entry["identity_matches"] = entry["returned_upstream"] == route.upstream_model
                entry["calibration_quality"] = None
                if response.status_code == 200:
                    try:
                        text = body["choices"][0]["message"]["content"]
                        contract = load_contract()
                        raw_answer = parse_structured_json(text)
                        packet = json.loads((ROOT / row["source_packet"]).read_text())
                        case = contract.SelectionInput.model_validate(
                            packet["cases"][row["packet_entry_index"]]["case"]
                        )
                        answer = contract.validate_answer(raw_answer, case)
                        entry["parsed_answer"] = answer.model_dump(mode="json")
                    except Exception as exc:
                        entry["schema_error_class"] = type(exc).__name__
            except Exception as exc:
                entry.update(
                    status="transport_failure",
                    exception_class=type(exc).__name__,
                    calibration_quality=None,
                )
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, previous_handler)
                previous_end = time.monotonic()
                entry["elapsed_seconds"] = round(previous_end - started, 3)
                entry["finished_at"] = stamp()
                save(result_path.name, entry)
                summary.append(entry)
            print(role + ": " + entry["status"], flush=True)
    finally:
        restoration = restore(client, states)
        save(
            f"summary-{time.time_ns()}.json",
            {"observed_at": stamp(), "results": summary, "restoration_errors": restoration},
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["prepare", "pause", "run"])
    args = parser.parse_args()
    jobs = requests_for_run()
    if args.phase == "prepare":
        print("Validated three direct request profiles; no credentials or network calls")
        return
    WINDOW.mkdir(exist_ok=True)
    with (WINDOW / "controller.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        client = DispatchPauseClient()
        if args.phase == "pause":
            pause_phase(client, jobs)
        else:
            run_phase(client, jobs)


if __name__ == "__main__":
    main()
