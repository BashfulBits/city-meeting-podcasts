#!/usr/bin/env python3
"""Time sequential queued jobs through the v2 Worker at priority 0 and priority 1.

Review/54 §8.15.G route A: interactive callers (city onboarding, interactive remedy, evaluators)
could submit to the Worker queue at priority 0 instead of calling a provider directly, which would
put every call on the one quota ledger with no new endpoint. Whether that works depends on the
round trip of one job, because onboarding's limit measurement is sequential (each probe waits for
the previous result). This submits tiny jobs one at a time, as onboarding would, alternating the
two priorities so both see the same Worker backlog, and reports submit time and total time to
result per priority. It spends a few free-tier provider calls on the ``city-onboarding`` lane.

Run it from the ``LLM queue latency probe`` workflow: it writes B2 payloads and the deferred
registry like any producer, which needs the workflow's storage credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

# `city-onboarding` itself cannot be queued: it is deliberately a non-dispatching purpose with no
# lane registered at the Worker, which refuses it as ``purpose_not_registered`` (llm_lanes.py). The
# round trip is a property of the queue, not of the lane, so the default is a registered lane
# whose models are free-tier chat models; the onboarding purpose can be asked for to see that.
PURPOSE = "topic-tags:tagger"
PROMPT = "Reply with the single word OK."


def make_job(priority: int, nonce: str, model: str, purpose: str = PURPOSE):
    from citypods.compute.base import InferenceJob
    from citypods.compute.llm_policy import LLMRequestPolicy

    return InferenceJob(
        task="classify-civic-platforms",
        inputs={
            "messages": [{"role": "user", "content": PROMPT}],
            "max_tokens": 16,
            "llm_policy": LLMRequestPolicy(
                allowed_models=(model,),
                allow_paid=False,
                purpose=purpose,
                queue_only=True,
                timeout_class="fast",
                priority=priority,  # type: ignore[arg-type]
            ),
        },
        # A fresh recipe per call: the registry would answer a repeat from a stored result.
        recipe_hash=hashlib.sha256(f"queue-latency:{nonce}:{priority}".encode()).hexdigest()[:40],
    )


def order(calls: int) -> list[int]:
    """Priorities in submission order: pairs, alternating which one goes first, so neither
    always sees the queue as the other left it."""
    out: list[int] = []
    for i in range(calls):
        out += [1, 0] if i % 2 == 0 else [0, 1]
    return out


def percentile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def summarize(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per priority: calls, completions, and submit/total seconds."""
    out: dict[str, dict[str, Any]] = {}
    for priority in sorted({row["priority"] for row in rows}):
        mine = [row for row in rows if row["priority"] == priority]
        done = [row for row in mine if row.get("outcome") == "completed"]
        entry: dict[str, Any] = {
            "calls": len(mine),
            "completed": len(done),
            "outcomes": {
                name: sum(1 for row in mine if row.get("outcome") == name)
                for name in sorted({str(row.get("outcome")) for row in mine})
            },
        }
        if done:
            totals = [row["total_seconds"] for row in done]
            submits = [row["submit_seconds"] for row in done]
            entry.update(
                submit_median=round(statistics.median(submits), 1),
                total_median=round(statistics.median(totals), 1),
                total_p90=round(percentile(totals, 0.9), 1),
                total_max=round(max(totals), 1),
                sequence_total=round(sum(totals), 1),
            )
        out[str(priority)] = entry
    return out


def markdown(summary: Mapping[str, Mapping[str, Any]], context: Mapping[str, Any]) -> str:
    lines = [
        "### Queue round trip per job (seconds, sequential, one job at a time)",
        "",
        "| Priority | Calls | Completed | Submit median | Total median | Total p90 | Total max |",
        "|---|---|---|---|---|---|---|",
    ]
    for priority, entry in summary.items():
        lines.append(
            f"| {priority} | {entry['calls']} | {entry['completed']} | "
            f"{entry.get('submit_median', '-')} | {entry.get('total_median', '-')} | "
            f"{entry.get('total_p90', '-')} | {entry.get('total_max', '-')} |"
        )
    lines += ["", f"Worker backlog at start: `{json.dumps(context.get('before'))}`"]
    lines += [f"Worker backlog at end: `{json.dumps(context.get('after'))}`", ""]
    return "\n".join(lines)


def worker_backlog(url: str | None, token: str | None) -> dict[str, Any] | str:
    """Queue depth from ``/v2/stats`` for context; never fails the probe."""
    if not url:
        return "no Worker URL"
    import requests

    try:
        response = requests.get(
            urljoin(url.rstrip("/") + "/", "v2/stats"),
            headers={"authorization": f"Bearer {token}"} if token else {},
            timeout=20,
        )
        body = response.json()
        scheduler = body.get("scheduler") if isinstance(body, Mapping) else None
        counts = (scheduler or body).get("jobs") if isinstance(scheduler or body, Mapping) else None
        return counts if isinstance(counts, Mapping) else {"http": response.status_code}
    except Exception as exc:  # noqa: BLE001 - context only
        return f"unavailable ({type(exc).__name__})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calls", type=int, default=5, help="pairs of calls (one per priority)")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--timeout-seconds", type=float, default=1200.0, help="per call")
    parser.add_argument(
        "--purpose", default=PURPOSE, help="a lane purpose registered at the Worker"
    )
    parser.add_argument("--model", help="allowed model (default: the lane's first model)")
    parser.add_argument("--site-config", default="config/site_config.yml")
    parser.add_argument("--output-dir", default="public")
    parser.add_argument("--out", type=Path, default=Path("queue-latency.json"))
    args = parser.parse_args(argv)
    if not 1 <= args.calls <= 20:
        parser.error("--calls must be between 1 and 20")

    from citypods.compute.base import JobHandle, JobResult
    from citypods.compute.llm import LiteLLMBackend, LLMBackendConfig
    from citypods.compute.llm_lanes import load_lanes
    from citypods.config import load_site_config
    from citypods.storage import make_storage

    storage = make_storage(load_site_config(args.site_config), "", Path(args.output_dir))
    if storage is None or not getattr(storage, "cas_capable", False):
        raise SystemExit("a CAS-capable storage backend is required (queue submission)")
    config = LLMBackendConfig.from_env()
    if not config.dispatch_v2_url:
        raise SystemExit("LLM_DISPATCH_V2_URL is not set")
    backend = LiteLLMBackend(config, storage=storage)
    lane = load_lanes().get(args.purpose)
    model = args.model or (lane.models[0] if lane is not None and lane.models else None)
    if not model:
        raise SystemExit(f"no lane models for purpose {args.purpose!r}; pass --model")
    run = os.environ.get("GITHUB_RUN_ID", str(int(time.time())))
    context = {"before": worker_backlog(config.dispatch_v2_url, config.dispatch_v2_auth_token)}

    rows: list[dict[str, Any]] = []
    for index, priority in enumerate(order(args.calls)):
        job = make_job(priority, f"{run}:{index}", model, args.purpose)
        started = time.monotonic()
        row: dict[str, Any] = {"index": index, "priority": priority, "model": model}
        try:
            outcome = backend.enqueue_batch([job])[0]
            row["submit_seconds"] = round(time.monotonic() - started, 2)
            if isinstance(outcome, Exception):
                row.update(
                    outcome="submit_error",
                    error=f"{type(outcome).__name__}: {str(outcome)[:200]}",
                    reason=getattr(outcome, "reason", None),
                )
            elif isinstance(outcome, JobResult):
                row.update(outcome="completed", total_seconds=row["submit_seconds"])
            elif isinstance(outcome, JobHandle):
                while True:
                    elapsed = time.monotonic() - started
                    if elapsed > args.timeout_seconds:
                        row.update(outcome="timeout", total_seconds=round(elapsed, 2))
                        break
                    time.sleep(args.poll_seconds)
                    polled = backend.poll_batch([outcome]).get(outcome.ref)
                    if isinstance(polled, JobResult):
                        row.update(
                            outcome="completed",
                            total_seconds=round(time.monotonic() - started, 2),
                        )
                        break
                    if isinstance(polled, Exception):
                        row.update(
                            outcome="job_error",
                            error=type(polled).__name__,
                            total_seconds=round(time.monotonic() - started, 2),
                        )
                        break
            else:
                row.update(outcome="unexpected", error=type(outcome).__name__)
        except Exception as exc:  # noqa: BLE001 - record it and keep measuring
            row.update(outcome="exception", error=f"{type(exc).__name__}: {str(exc)[:160]}")
        rows.append(row)
        print(json.dumps(row, sort_keys=True), flush=True)

    context["after"] = worker_backlog(config.dispatch_v2_url, config.dispatch_v2_auth_token)
    summary = summarize(rows)
    result = {
        "model": model,
        "purpose": args.purpose,
        "summary": summary,
        "context": context,
        "rows": rows,
    }
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    text = markdown(summary, context)
    print(text)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
