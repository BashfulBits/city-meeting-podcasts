"""Payload-free telemetry for diagnosing LLM producer throughput.

The v2 Worker already exposes its own scheduler counters.  This module fills the other half of
the picture: what a GitHub producer actually considered, prepared, and asked ingress to accept.
It is deliberately opt-in through ``CITYPODS_LLM_SUBMISSION_TELEMETRY_FILE`` so local commands and
tests retain their usual quiet, side-effect-free behaviour.

Each event is one small JSON line.  It contains counts, route identifiers, and timing only -- never
prompts, results, recipe hashes, payload keys, or credentials.  The GitHub workflows take a start
and end scheduler snapshot and render the resulting file into ``GITHUB_STEP_SUMMARY``.
"""

from __future__ import annotations

import json
import os
import threading
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TELEMETRY_FILE_ENV = "CITYPODS_LLM_SUBMISSION_TELEMETRY_FILE"
_WRITE_LOCK = threading.Lock()


def enabled() -> bool:
    """Whether this process should emit the optional workflow telemetry."""
    return bool(os.environ.get(TELEMETRY_FILE_ENV, "").strip())


def record(event: str, /, **fields: Any) -> None:
    """Append one bounded, JSON-safe event when workflow telemetry is enabled."""
    destination = os.environ.get(TELEMETRY_FILE_ENV, "").strip()
    if not destination:
        return
    payload = {
        "event": event,
        "ts": datetime.now(UTC).isoformat(),
        "workflow": os.environ.get("GITHUB_WORKFLOW", ""),
        "run_id": os.environ.get("GITHUB_RUN_ID", ""),
        **fields,
    }
    try:
        line = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _WRITE_LOCK:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
    except (OSError, TypeError, ValueError):
        # Observability must never turn a durable job admission into a failed submission.
        return


def job_descriptor(job: Any) -> dict[str, str]:
    """Return the non-sensitive grouping dimensions for an inference job."""
    inputs = getattr(job, "inputs", {})
    policy = inputs.get("llm_policy") if isinstance(inputs, Mapping) else None
    allowed = getattr(policy, "allowed_models", ()) or ()
    return {
        "purpose": str(getattr(policy, "purpose", "") or "(unspecified)"),
        "task": str(getattr(job, "task", "(unknown)")),
        "primary_model": str(allowed[0]) if allowed else "(backend-default)",
    }


def record_stage_activity(
    *,
    lane: str | None,
    stage: str,
    ran: int,
    reused: int,
    backlog: int,
    errors: int,
    seconds: float,
    defer_reasons: Mapping[str, int],
) -> None:
    """Record final activity for an LLM-producing stage in an enrichment run."""
    record(
        "llm_submission_stage",
        lane=lane or "(none)",
        stage=stage,
        ran=ran,
        reused=reused,
        backlog=backlog,
        errors=errors,
        seconds=round(seconds, 3),
        defer_reasons={str(key): int(value) for key, value in sorted(defer_reasons.items())},
    )


def record_producer_observations(
    observations: Mapping[tuple[str, str, str], Mapping[str, int]],
) -> None:
    """Record what a run-scoped collector saw before it attempted ingress.

    This is intentionally separate from ``record_enqueue_outcomes``: a record that was already
    pending never reaches ingress in this run, which is the key distinction when a lane's apparent
    candidate count is much larger than its fresh-submission count.
    """
    for (purpose, task, primary_model), counts in sorted(observations.items()):
        record(
            "llm_submission_producer",
            purpose=purpose,
            task=task,
            primary_model=primary_model,
            **{str(key): int(value) for key, value in sorted(counts.items())},
        )


def record_enqueue_outcomes(
    entries: Sequence[tuple[Any, str, str | None]],
    *,
    elapsed_seconds: float,
    payload_stage_seconds: float,
    request_seconds: float,
    persist_seconds: float,
    transport_retries: int,
) -> None:
    """Aggregate one ingress attempt by purpose/task/model before writing telemetry.

    ``status`` is one of ``fresh_admitted``, ``replayed``, ``deferred``, ``rejected``,
    ``unknown``, ``cached_completed``, ``prior_pending``, ``deferred_retry``, or
    ``client_daily_cap``.  The optional third tuple item is a non-secret rejection reason.
    """
    grouped: dict[tuple[str, str, str], Counter[str]] = defaultdict(Counter)
    reasons: dict[tuple[str, str, str], Counter[str]] = defaultdict(Counter)
    for job, status, reason in entries:
        descriptor = job_descriptor(job)
        key = (descriptor["purpose"], descriptor["task"], descriptor["primary_model"])
        grouped[key][status] += 1
        if reason:
            reasons[key][reason] += 1
    for (purpose, task, primary_model), counts in sorted(grouped.items()):
        record(
            "llm_submission_ingress",
            purpose=purpose,
            task=task,
            primary_model=primary_model,
            **dict(sorted(counts.items())),
            rejection_reasons=dict(sorted(reasons[(purpose, task, primary_model)].items())),
            elapsed_seconds=round(elapsed_seconds, 3),
            payload_stage_seconds=round(payload_stage_seconds, 3),
            request_seconds=round(request_seconds, 3),
            persist_seconds=round(persist_seconds, 3),
            transport_retries=transport_retries,
        )


def _counter(events: Iterable[Mapping[str, Any]], key: str) -> Counter[str]:
    out: Counter[str] = Counter()
    for event in events:
        value = event.get(key)
        if isinstance(value, Mapping):
            out.update(
                {str(name): int(count) for name, count in value.items() if isinstance(count, int)}
            )
    return out


def render_markdown(events: Sequence[Mapping[str, Any]]) -> str:
    """Render the JSONL event stream into a compact, decision-oriented Actions summary."""
    ingress = [event for event in events if event.get("event") == "llm_submission_ingress"]
    producer = [event for event in events if event.get("event") == "llm_submission_producer"]
    stages = [event for event in events if event.get("event") == "llm_submission_stage"]
    snapshots = [
        event for event in events if event.get("event") == "llm_submission_worker_snapshot"
    ]
    lines = ["## LLM submission telemetry", ""]
    if producer:
        lines.extend(
            [
                "### Producer candidate disposition",
                "",
                "| Purpose | Task | Model | New candidates | Existing pending | Deferred retry | "
                "Cached complete | In-run duplicate |",
                "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for event in producer:
            lines.append(
                "| {purpose} | {task} | {model} | {candidate} | {prior_pending} | "
                "{deferred_retry} | {cached_completed} | {duplicate_in_run} |".format(
                    purpose=event.get("purpose", "(unspecified)"),
                    task=event.get("task", "(unknown)"),
                    model=event.get("primary_model", "(backend-default)"),
                    candidate=int(event.get("candidate", 0) or 0),
                    prior_pending=int(event.get("prior_pending", 0) or 0),
                    deferred_retry=int(event.get("deferred_retry", 0) or 0),
                    cached_completed=int(event.get("cached_completed", 0) or 0),
                    duplicate_in_run=int(event.get("duplicate_in_run", 0) or 0),
                )
            )
        lines.append("")
    if ingress:
        lines.extend(
            [
                "### Producer → ingress",
                "",
                "| Purpose | Task | Model | Fresh | Replay | Deferred | Rejected | Cached | "
                "Pending | Client cap | Unknown | Stage s | Request s | Persist s |",
                "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | "
                "---: | ---: | ---: |",
            ]
        )
        totals: Counter[str] = Counter()
        for event in ingress:
            values = {
                key: int(event.get(key, 0) or 0)
                for key in (
                    "fresh_admitted",
                    "replayed",
                    "deferred",
                    "rejected",
                    "cached_completed",
                    "prior_pending",
                    "client_daily_cap",
                    "unknown",
                )
            }
            totals.update(values)
            lines.append(
                "| {purpose} | {task} | {model} | {fresh_admitted} | {replayed} | {deferred} | "
                "{rejected} | {cached_completed} | {prior_pending} | {client_daily_cap} | "
                "{unknown} | {payload_stage_seconds:.1f} | {request_seconds:.1f} | "
                "{persist_seconds:.1f} |".format(
                    purpose=event.get("purpose", "(unspecified)"),
                    task=event.get("task", "(unknown)"),
                    model=event.get("primary_model", "(backend-default)"),
                    payload_stage_seconds=float(event.get("payload_stage_seconds", 0) or 0),
                    request_seconds=float(event.get("request_seconds", 0) or 0),
                    persist_seconds=float(event.get("persist_seconds", 0) or 0),
                    **values,
                )
            )
        lines.append(
            "| **Total** |  |  | {fresh_admitted} | {replayed} | {deferred} | {rejected} | "
            "{cached_completed} | {prior_pending} | {client_daily_cap} | {unknown} | "
            " |  |  |".format(**{key: totals[key] for key in totals})
        )
        reasons = _counter(ingress, "rejection_reasons")
        if reasons:
            reason_text = ", ".join(f"`{key}`={value}" for key, value in reasons.most_common())
            lines.extend(["", "Ingress reasons: " + reason_text])
    else:
        lines.extend(["No v2 ingress attempts were observed in this workflow."])

    if stages:
        lines.extend(
            [
                "",
                "### Producer-stage gates",
                "",
                "| Lane | Stage | Ran | Reused | Deferred | Errors | Seconds |",
                "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for event in stages:
            lines.append(
                "| {lane} | {stage} | {ran} | {reused} | {backlog} | {errors} | "
                "{seconds:.1f} |".format(
                    lane=event.get("lane", "(none)"),
                    stage=event.get("stage", "(unknown)"),
                    ran=int(event.get("ran", 0) or 0),
                    reused=int(event.get("reused", 0) or 0),
                    backlog=int(event.get("backlog", 0) or 0),
                    errors=int(event.get("errors", 0) or 0),
                    seconds=float(event.get("seconds", 0) or 0),
                )
            )
            reasons = event.get("defer_reasons")
            if isinstance(reasons, Mapping) and reasons:
                lines.append(
                    "|  | ↳ "
                    + ", ".join(f"`{key}`={value}" for key, value in sorted(reasons.items()))
                    + " |  |  |  |  |  |"
                )

    if snapshots:
        lines.extend(["", "### Worker snapshots", ""])
        for event in snapshots:
            summary = event.get("summary")
            if isinstance(summary, Mapping):
                lines.append(
                    "- **{phase}:** active bundles `{active_bundles}`, "
                    "active calls `{active_calls}`, "
                    "queued `{queued}`, claimed today `{claimed}`, ingress today `{ingested}`; "
                    "last claim `{last_reason}`.".format(
                        phase=event.get("phase", "snapshot"),
                        active_bundles=summary.get("active_bundles", "?"),
                        active_calls=summary.get("active_calls", "?"),
                        queued=summary.get("queued", "?"),
                        claimed=summary.get("claimed", "?"),
                        ingested=summary.get("ingested", "?"),
                        last_reason=summary.get("last_reason", "?"),
                    )
                )
                reason_counts = summary.get("reason_counts")
                if isinstance(reason_counts, Mapping) and reason_counts:
                    reasons = ", ".join(
                        f"`{key}`={value}" for key, value in sorted(reason_counts.items())
                    )
                    lines.append(f"  - claim reasons: {reasons}")
                queued_by_model = summary.get("queued_by_model")
                if isinstance(queued_by_model, Mapping) and queued_by_model:
                    queued = ", ".join(
                        f"`{key}`={value}" for key, value in sorted(queued_by_model.items())
                    )
                    lines.append(f"  - queued models: {queued}")
            else:
                phase = event.get("phase", "snapshot")
                error = event.get("error", "unknown")
                lines.append(f"- **{phase}:** unavailable ({error}).")
    lines.append("")
    return "\n".join(lines)
