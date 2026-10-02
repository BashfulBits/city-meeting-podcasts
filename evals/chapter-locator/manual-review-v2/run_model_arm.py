#!/usr/bin/env python3
"""Run one chapter-locator model/prompt arm on manually reviewed meetings.

Raw requests and provider responses are kept outside the repository under
``/private/tmp/chapter-locator-telemetry``. The script pauses the selected provider's v2 claims,
waits for them to drain, reserves every direct call against its route, and resumes on exit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import litellm

from citypods.chapter_locator import (
    LOCATOR_CONTRACT,
    DuplicateLocatorStartError,
    build_locator_request,
    build_locator_units,
    ensure_locator_contract,
    validate_locator_response,
)
from citypods.compute.base import InferenceJob
from citypods.compute.llm import LiteLLMBackend, LLMBackendConfig
from citypods.compute.llm_dispatch_pause import DispatchPauseClient, Selection, paused
from citypods.compute.llm_policy import ROUTE_CANDIDATES, canonical_model, estimate_tokens
from citypods.compute.structured_shaping import shape_for_route
from citypods.http import make_session
from scripts.research.agenda_chapters.build_locator_packets import _agenda_items
from scripts.research.agenda_chapters.train_transition_scorer import _artifact_bytes

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
PACKET_PATH = HERE / "packet.json"
TELEMETRY = Path("/private/tmp/chapter-locator-telemetry")
CACHE = TELEMETRY / "locator-manual-review-v2-transcript-cache"
MAX_ATTEMPTS = 2
TIMEOUT_SECONDS = 2400
MAX_OUTPUT_TOKENS = {
    "deepseek/deepseek-v4-flash": 16_384,
    "deepseek/deepseek-v4.1-flash": 16_384,
    "gemini/gemini-3.5-flash-lite": 8_192,
    "moonshotai/kimi-k3": 8_192,
    "zai/glm-5.3-flash": 65_536,
}
MIN_GAP_SECONDS = {
    "gemini": 80.0,
    "nvidia": 120.0,
    "orcarouter": 10.0,
}
TRANSITION_SWEEP = (
    "First scan the timed transcript from beginning to end and make a private inventory of "
    "every clear change into a new substantive topic, agenda discussion, hearing, motion, or "
    "announcement. Then map each transition to the single best agenda item using its title, "
    "locator cues, and spoken evidence. A speaker change or passing mention alone is not a "
    "transition. After the chronological pass, audit every agenda item for a clearly discussed "
    "start that the first pass missed. Return all distinct, well-supported starts; do not cap the "
    "number of anchors or omit a clear item because it is less prominent. If one item is "
    "revisited, use only its first substantive discussion. Omit uncertain matches. Keep exact "
    "unit IDs, one anchor per item, one use per unit, and the supplied JSON schema."
)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def response_dict(response: Any) -> dict[str, Any]:
    if hasattr(response, "model_dump"):
        return response.model_dump(exclude_none=True)
    if hasattr(response, "dict"):
        return response.dict(exclude_none=True)
    return json.loads(json.dumps(response, default=str))


def manifest_rows(uids: set[str]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for split in ("dataset-v3", "holdout-v1"):
        manifest = read_json(ROOT / "evals" / "chapter-locator" / split / "manifest.json")
        rows.update(
            {str(row["uid"]): row for row in manifest["episodes"] if str(row["uid"]) in uids}
        )
    missing = uids - rows.keys()
    if missing:
        raise ValueError(f"cohort UIDs missing from frozen manifests: {sorted(missing)}")
    return rows


def add_prompt(messages: list[dict[str, Any]], prompt: str) -> list[dict[str, Any]]:
    copied = [dict(message) for message in messages]
    if prompt == "baseline":
        return copied
    if prompt != "transition_sweep":
        raise ValueError(f"unsupported prompt arm: {prompt}")
    for message in copied:
        if message.get("role") == "system":
            message["content"] += "\n\n" + TRANSITION_SWEEP
            return copied
    copied.insert(0, {"role": "system", "content": TRANSITION_SWEEP})
    return copied


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, choices=sorted(MAX_OUTPUT_TOKENS))
    parser.add_argument("--prompt", required=True, choices=("baseline", "transition_sweep"))
    parser.add_argument("--uid", action="append", required=True)
    parser.add_argument("--route-id", help="Exact route ID; defaults to the first catalog route.")
    parser.add_argument(
        "--provider-pause", help="Provider name to pause; defaults to the route provider."
    )
    parser.add_argument("--min-gap-seconds", type=float)
    args = parser.parse_args()

    model = args.model
    routes = ROUTE_CANDIDATES[canonical_model(model)]
    route = (
        next((r for r in routes if r.route_id == args.route_id), None)
        if args.route_id
        else routes[0]
    )
    if route is None:
        raise ValueError(f"route {args.route_id!r} is not configured for {model}")
    provider = args.provider_pause or route.provider
    gap = args.min_gap_seconds if args.min_gap_seconds is not None else MIN_GAP_SECONDS[provider]
    uids = set(args.uid)
    packet = read_json(PACKET_PATH)
    cohort_cases = [case for case in packet["cases"] if case["source_episode_key"] in uids]
    found_uids = {case["source_episode_key"] for case in cohort_cases}
    if found_uids != uids:
        raise ValueError(f"requested UIDs missing from manual cohort: {sorted(uids - found_uids)}")
    rows = manifest_rows(uids)
    session = make_session()
    backend = LiteLLMBackend(
        LLMBackendConfig(model=model, mode="direct", direct_timeout_seconds=TIMEOUT_SECONDS)
    )
    response_model = ensure_locator_contract()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = TELEMETRY / f"locator-manual-v2-{model.replace('/', '-')}-{args.prompt}-{stamp}"
    attempts_dir = out_dir / "attempts"
    out_dir.mkdir(parents=True, exist_ok=False)
    metadata = {
        "started_utc": datetime.now(UTC).isoformat(),
        "dataset": "chapter-locator/manual-review-v2",
        "model": model,
        "route_id": route.route_id,
        "provider": route.provider,
        "provider_pause_scope": provider,
        "prompt": args.prompt,
        "prompt_text": TRANSITION_SWEEP if args.prompt == "transition_sweep" else None,
        "uids": sorted(uids),
        "max_attempts": MAX_ATTEMPTS,
        "max_output_tokens": MAX_OUTPUT_TOKENS[model],
        "timeout_seconds": TIMEOUT_SECONDS,
        "minimum_seconds_between_attempts": gap,
        "provider_labels_in_requests": False,
        "raw_responses_may_include_reasoning": True,
    }
    write_json(out_dir / "run-metadata.json", metadata)
    results: list[dict[str, Any]] = []
    attempt_index: list[dict[str, Any]] = []
    last_start = 0.0
    pause_client = DispatchPauseClient()
    with paused(
        Selection("provider", provider),
        seconds=3600,
        drain_timeout=1800,
        poll_interval=10,
        reason=f"chapter-locator manual review v2 {model} {args.prompt}",
        client=pause_client,
    ) as pause:
        write_json(
            out_dir / "pause.json",
            {
                "selection": pause.selection.body(),
                "drained": pause.drained,
                "in_flight_at_start": pause.in_flight_at_start,
                "waited_seconds": pause.waited_seconds,
            },
        )
        if not pause.drained:
            raise RuntimeError(f"provider {provider} did not drain; refusing model calls")
        for index, uid in enumerate(sorted(uids), start=1):
            row = rows[uid]
            agenda_model = next(iter(row["generated_agenda"]))
            agenda_items = _agenda_items(row, agenda_model)
            words, vtt, source, _ = _artifact_bytes(session, row, cache_dir=CACHE)
            units, unit_source = build_locator_units(words_data=words, vtt_data=vtt)
            request = build_locator_request(agenda_items, units)
            messages = add_prompt([dict(message) for message in request.messages], args.prompt)
            schema = response_model.model_json_schema()
            shaped_messages, response_format = shape_for_route(
                messages,
                name=response_model.__name__,
                schema=schema,
                route=route,
            )
            if not shaped_messages or any(
                not isinstance(message.get("content"), str) or not message["content"]
                for message in shaped_messages
            ):
                raise ValueError("refusing to send a request with an empty message")
            recipe = hashlib.sha256(
                json.dumps(
                    {
                        "uid": uid,
                        "model": model,
                        "prompt": args.prompt,
                        "messages": shaped_messages,
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
            answer: dict[str, Any] = {
                "uid": uid,
                "status": "invalid",
                "valid": False,
                "input_estimate_tokens": estimate_tokens(shaped_messages),
                "unit_source": unit_source or source,
                "unit_count": len(units),
                "agenda_item_count": len(agenda_items),
                "attempts": [],
            }
            working_messages = shaped_messages
            for attempt in range(1, MAX_ATTEMPTS + 1):
                wait_seconds = gap - (time.monotonic() - last_start) if last_start else 0
                if wait_seconds > 0:
                    time.sleep(wait_seconds)
                pause.renew()
                pause_client.reserve(route.route_id, 1)
                started_utc = datetime.now(UTC)
                started_mono = time.monotonic()
                last_start = started_mono
                inputs: dict[str, Any] = {
                    "messages": working_messages,
                    "structured_output": LOCATOR_CONTRACT,
                    "max_tokens": MAX_OUTPUT_TOKENS[model],
                    "timeout": TIMEOUT_SECONDS,
                }
                job = InferenceJob(task="agenda-chapter-locate", inputs=inputs, recipe_hash=recipe)
                options = backend._provider_options(
                    job,
                    route.direct_model,
                    route=route,
                    direct=True,
                    messages=working_messages,
                )
                options["max_tokens"] = MAX_OUTPUT_TOKENS[model]
                options["timeout"] = TIMEOUT_SECONDS
                options["num_retries"] = 0
                # DeepSeek V4.1's evaluated arm uses the provider default thinking mode.
                if model == "deepseek/deepseek-v4.1-flash":
                    options.pop("extra_body", None)
                if response_format is not None:
                    options["response_format"] = response_format
                request_log = {
                    "messages": working_messages,
                    **{
                        key: value
                        for key, value in options.items()
                        if key not in {"api_key", "headers", "extra_headers"}
                    },
                }
                attempt_dir = attempts_dir / uid / f"attempt-{attempt}"
                attempt_dir.mkdir(parents=True, exist_ok=True)
                write_json(attempt_dir / "request.json", request_log)
                record = {
                    "uid": uid,
                    "model": model,
                    "route_id": route.route_id,
                    "prompt": args.prompt,
                    "attempt": attempt,
                    "started_utc": started_utc.isoformat(),
                    "request_sha256": hashlib.sha256(
                        (attempt_dir / "request.json").read_bytes()
                    ).hexdigest(),
                    "request_path": str(attempt_dir / "request.json"),
                }
                try:
                    raw = litellm.completion(messages=working_messages, **options)
                    raw_map = response_dict(raw)
                    write_json(attempt_dir / "response.json", raw_map)
                    choice = (raw_map.get("choices") or [{}])[0]
                    message = choice.get("message") or {}
                    content = message.get("content")
                    record.update(
                        {
                            "response_path": str(attempt_dir / "response.json"),
                            "response_id": raw_map.get("id"),
                            "usage": raw_map.get("usage"),
                            "finish_reason": choice.get("finish_reason"),
                            "elapsed_seconds": round(time.monotonic() - started_mono, 3),
                            "content_bytes": len(content.encode())
                            if isinstance(content, str)
                            else None,
                            "reasoning_content_bytes": len(
                                (message.get("reasoning_content") or "").encode()
                            ),
                        }
                    )
                    if not isinstance(content, str) or not content.strip():
                        raise ValueError("empty response content")
                    anchors = validate_locator_response(
                        content, agenda_item_count=len(agenda_items), units=units
                    )
                    answer.update(
                        {
                            "status": "completed",
                            "valid": True,
                            "error": None,
                            "elapsed_seconds": sum(
                                float(entry.get("elapsed_seconds", 0))
                                for entry in answer["attempts"]
                            )
                            + float(record["elapsed_seconds"]),
                            "prompt_tokens": (raw_map.get("usage") or {}).get("prompt_tokens"),
                            "completion_tokens": (raw_map.get("usage") or {}).get(
                                "completion_tokens"
                            ),
                            "response_id": raw_map.get("id"),
                            "anchors": [
                                {
                                    "agenda_item_index": anchor.agenda_item_index,
                                    "start_seconds": anchor.unit.start,
                                    "unit_id": anchor.unit.id,
                                    "transition_quote": anchor.transition_quote,
                                }
                                for anchor in anchors
                            ],
                        }
                    )
                    record["validation"] = "valid"
                except ValueError as exc:
                    record["validation"] = "invalid"
                    record["validation_error"] = str(exc)
                    answer["error"] = f"ValueError: {exc}"
                    if attempt < MAX_ATTEMPTS and isinstance(locals().get("content"), str):
                        duplicate_unit = re.fullmatch(r"duplicate locator unit: (u\d{5})", str(exc))
                        repeated_start = isinstance(exc, DuplicateLocatorStartError)
                        if duplicate_unit or repeated_start:
                            collision = (
                                f" Transcript unit {duplicate_unit.group(1)} was assigned to "
                                "multiple agenda items."
                                if duplicate_unit
                                else " Multiple selected transcript units resolve to the same "
                                "start time."
                            )
                            correction = (
                                "The previous response failed because two chapter anchors have "
                                "the same location."
                                + collision
                                + " Recheck only the conflicting anchors against the transcript "
                                "and supplied unit times. If a distinct later start is supported, "
                                "use its supplied unit. If the items truly start together, or one "
                                "is only a broader section heading, keep the strongest, most "
                                "specific chapter and omit the redundant one. Do not invent a time "
                                "offset or reuse a unit. Preserve the other valid anchors and "
                                "return JSON only in the requested schema."
                            )
                        else:
                            correction = (
                                "Repair the previous locator response to satisfy the supplied "
                                "JSON schema. Use only supplied agenda indexes and transcript "
                                "unit IDs; ensure each agenda item and unit appears at most once. "
                                "Return JSON only."
                            )
                        working_messages = [
                            *working_messages,
                            {"role": "assistant", "content": content},
                            {"role": "user", "content": correction},
                        ]
                except Exception as exc:  # noqa: BLE001
                    record.update(
                        {
                            "elapsed_seconds": round(time.monotonic() - started_mono, 3),
                            "error": f"{type(exc).__name__}: {str(exc)[:1000]}",
                        }
                    )
                    answer["error"] = record["error"]
                    write_json(attempt_dir / "error.json", record)
                answer["attempts"].append(record)
                answer["elapsed_seconds"] = sum(
                    float(entry.get("elapsed_seconds", 0)) for entry in answer["attempts"]
                )
                attempt_index.append(record)
                write_json(out_dir / "attempt-index.json", attempt_index)
                write_json(
                    out_dir / "partial-results.json",
                    {
                        "completed": len(results),
                        "expected": len(uids),
                        "results": results + [answer],
                    },
                )
                if answer["valid"] or record.get("error"):
                    break
            results.append(answer)
            write_json(
                out_dir / "partial-results.json",
                {"completed": len(results), "expected": len(uids), "results": results},
            )
            print(
                json.dumps(
                    {
                        "index": index,
                        "total": len(uids),
                        "uid": uid,
                        "status": answer["status"],
                        "elapsed_seconds": answer.get("elapsed_seconds"),
                        "valid": answer["valid"],
                        "anchors": len(answer.get("anchors", [])),
                    }
                ),
                flush=True,
            )
        write_json(
            out_dir / "pause-outcome.json",
            {
                "drained": pause.drained,
                "in_flight_at_start": pause.in_flight_at_start,
                "waited_seconds": pause.waited_seconds,
                "pause_expired": pause.pause_expired,
            },
        )
    write_json(
        out_dir / "results.json",
        metadata
        | {
            "finished_utc": datetime.now(UTC).isoformat(),
            "results": results,
            "attempt_count": len(attempt_index),
            "raw_telemetry_dir": str(out_dir),
        },
    )
    print(json.dumps({"run_dir": str(out_dir), "completed": len(results), "expected": len(uids)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
