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


def record_producer_caps(ctx: Any) -> None:
    """Observe final runner slot accounting without reading or writing the coordinator.

    Slots describe producer work, not admitted DO rows. Ingress events separately distinguish
    fresh admissions from replays and rejected/deferred prepared work.
    """
    for purpose, prefix, exhausted_name in (
        ("topic-tags:tagger", "tag", "tag_llm_dispatch_exhausted"),
        ("topic-tags:prelabeler", "tag_prelabeler", "tag_prelabeler_dispatch_exhausted"),
        ("topic-tags:prelabeler-shadow", "tag_prelabeler_shadow", None),
        ("chapter-agenda", "chapter_agenda", "chapter_agenda_dispatch_exhausted"),
        ("chapter-locator", "chapter_locator", "chapter_locator_dispatch_exhausted"),
        ("r6-shared-moments-and-judge", "moment", None),
    ):
        cap = getattr(ctx, f"{prefix}_max_dispatches", None)
        used = getattr(ctx, f"{prefix}_dispatches_count", 0)
        if prefix == "moment":
            used = getattr(ctx, "moment_dispatches", 0)
        reserved = getattr(ctx, f"{prefix}_dispatches_reserved", 0)
        exhausted = getattr(ctx, exhausted_name, None) if exhausted_name else None
        closed = bool(exhausted and exhausted.is_set())
        anomalies = []
        if reserved:
            anomalies.append("unsettled_reservations")
        if cap is not None and used > cap:
            anomalies.append("producer_cap_exceeded")
        if cap is not None and used < cap and closed:
            # An explicit ingress closure can legitimately set this flag too. Surface it for
            # correlation with ingress reasons, rather than declaring every such gate a bug.
            anomalies.append("closed_below_cap_check_ingress_reason")
        record(
            "llm_producer_cap",
            lane=getattr(ctx, "lane", None),
            purpose=purpose,
            unit="producer slots",
            limit=cap,
            used=used,
            reserved=reserved,
            closed=closed,
            anomalies=anomalies,
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
    from citypods.compute.llm_work import validate_work_binding

    for job, status, reason in entries:
        binding = validate_work_binding(job)
        if binding is not None:
            binding.job_outcome(job.recipe_hash, status, reason)

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
    recovery = [event for event in events if event.get("event") == "llm_structural_recovery"]
    lines = ["## LLM submission telemetry", ""]
    schema_retries = [
        event
        for event in events
        if event.get("event") == "llm_dispatch_v2_batch"
        and event.get("operation") == "schema-retry"
    ]
    if schema_retries:
        observed = sum(int(event.get("schema_retry", 0) or 0) for event in schema_retries)
        lines.extend(
            [
                "### Recovery submissions",
                "",
                f"Schema-correction submission observations in this run: **{observed}**.",
                "These are recovery traffic, not new content work. A new correction job counts "
                "as ingress; a Worker retry/requeue reschedules the same job. Observations "
                "can include idempotent replays and are not an exact "
                "UTC-day count of new correction rows.",
                "",
            ]
        )
    _render_accounting(events, lines)
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
                "Reused includes no-op or out-of-scope stage visits; "
                "it does not count model outputs.",
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

    if recovery:
        audits = [event for event in recovery if event.get("operation") == "audit_persisted"]
        changed_counts = sum(
            event.get("failure_count_before") != event.get("failure_count_after")
            for event in audits
        )
        changed_schema = sum(
            event.get("schema_correction_before") != event.get("schema_correction_after")
            for event in audits
        )
        decisions = Counter(
            event.get("disposition")
            for event in recovery
            if event.get("operation") == "submission_decision"
        )
        lines.extend(
            [
                "",
                "### Structural recovery evidence",
                "",
                f"Persisted audits: `{len(audits)}`; retry-count changes: `{changed_counts}`; "
                f"schema-state changes: `{changed_schema}`.",
                "Submission decisions: "
                + (
                    ", ".join(f"`{key}`={value}" for key, value in sorted(decisions.items()))
                    or "none"
                ),
                "",
                "At most ten recipe fingerprints are shown; full events remain in the artifact. "
                "Missing observations do not establish canary success.",
                "",
                "| Recipe fingerprint | Operation | Result |",
                "| --- | --- | --- |",
            ]
        )
        sample: dict[str, list[Mapping[str, Any]]] = {}
        for event in recovery:
            fingerprint = str(event.get("recipe_fingerprint", "unknown"))
            if fingerprint not in sample and len(sample) >= 10:
                continue
            sample.setdefault(fingerprint, []).append(event)
        # Show one audit and the distinct decisions for each sampled recipe, not every replay.
        for fingerprint, events_for_recipe in sample.items():
            shown = set()
            for event in events_for_recipe:
                operation = event.get("operation")
                result = event.get("disposition", "audit write succeeded")
                key = (operation, result)
                if key in shown:
                    continue
                shown.add(key)
                lines.append(f"| `{fingerprint}` | {operation} | {result} |")
    else:
        lines.extend(
            [
                "",
                "### Structural recovery evidence",
                "",
                "No recovery observations were recorded. "
                "Missing observations do not establish canary success.",
            ]
        )

    if snapshots:
        lines.extend(["", "### Worker snapshots", ""])
        for event in snapshots:
            summary = event.get("summary")
            if isinstance(summary, Mapping):
                lines.append(
                    "- **{phase}:** active bundles `{active_bundles}`, "
                    "active calls `{active_calls}`, "
                    "queued `{queued}`, claimed today `{claimed}`, ingress today `{ingested}` "
                    "(UTC day `{utc_day}`); "
                    "last claim `{last_reason}`.".format(
                        phase=event.get("phase", "snapshot"),
                        active_bundles=summary.get("active_bundles", "?"),
                        active_calls=summary.get("active_calls", "?"),
                        queued=summary.get("queued", "?"),
                        claimed=summary.get("claimed", "?"),
                        ingested=summary.get("ingested", "?"),
                        utc_day=summary.get("utc_day", "?"),
                        last_reason=summary.get("last_reason", "?"),
                    )
                )
                reason_counts = summary.get("reason_counts")
                outcomes = {
                    key: summary.get(key) if summary.get(key) is not None else "?"
                    for key in ("completed", "failed", "retried")
                }
                lines.append(
                    "  - Worker results today: completed `{completed}`, failed `{failed}`, "
                    "Worker requeues `{retried}` (completion-time requeues, including late "
                    "deferrals). Completed means provider completion, not client "
                    "validation or content acceptance.".format(**outcomes)
                )
                budget = summary.get("row_budget")
                if isinstance(budget, Mapping):
                    lines.append(
                        f"  - DO rows written `{budget.get('rows_written_today')}` / safe stop "
                        f"`{budget.get('account_safe_stop')}`; "
                        f"enqueue open `{budget.get('enqueue_open')}`, "
                        f"claims open `{budget.get('claims_open')}`."
                    )
                for purpose in summary.get("ingress_by_purpose") or []:
                    if isinstance(purpose, Mapping):
                        lines.append(
                            f"  - `{purpose.get('purpose')}`: ingress today "
                            f"`{purpose.get('jobs_ingested')}` / `{purpose.get('daily_quota')}`; "
                            f"write units `{purpose.get('write_units')}`."
                        )
                rescue = summary.get("catalog_rescue")
                if isinstance(rescue, Mapping):
                    lines.append(
                        f"  - catalog rescue: digest `{rescue.get('digest')}`, "
                        f"complete `{rescue.get('complete')}`, "
                        f"committed cursor present `{bool(rescue.get('cursor'))}`."
                    )
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


def _render_accounting(events: Sequence[Mapping[str, Any]], lines: list[str]) -> None:
    """Keep gauges separate from observation counts; repeated polls are not distinct jobs."""
    caps = [event for event in events if event.get("event") == "llm_producer_cap"]
    judges = [event for event in events if event.get("event") == "llm_judge_backpressure"]
    audits = [event for event in events if event.get("event") == "llm_pending_audit"]
    sweeps = [event for event in events if event.get("event") == "llm_deferred_sweep_end"]
    polls = [
        event
        for event in events
        if event.get("event") == "llm_dispatch_v2_batch" and event.get("operation") == "poll-batch"
    ]
    task_polls = [event for event in events if event.get("event") == "llm_pending_poll"]
    if caps:
        lines.extend(
            [
                "### Runner accounting",
                "",
                "Producer slots are work prepared by the runner; they are not fresh DO rows.",
                "",
                "| Purpose | Slot cap | Used | Unsettled | Closed | Diagnostic |",
                "| --- | ---: | ---: | ---: | --- | --- |",
            ]
        )
        for event in caps:
            diagnostic = ", ".join(event.get("anomalies") or []) or "none"
            lines.append(
                f"| {event.get('purpose')} | {event.get('limit')} | {event.get('used')} | "
                f"{event.get('reserved')} | {event.get('closed')} | {diagnostic} |"
            )
        lines.append("")
    elif any(event.get("event") == "llm_submission_stage" for event in events):
        lines.extend(
            ["Runner cap accounting was not observed; check whether finalization ran.", ""]
        )
    if judges:
        lines.extend(
            [
                "### Judge outstanding gates",
                "",
                "Gauges at planning time, in packets. Each model has its own sibling limit.",
                "",
                "| Purpose | Model | Outstanding | Limit | Room | Remaining run cap |",
                "| --- | --- | ---: | ---: | ---: | ---: |",
            ]
        )
        for event in judges:
            lines.append(
                f"| {event.get('purpose')} | {event.get('model')} | "
                f"{event.get('outstanding')} | {event.get('limit')} | {event.get('room')} | "
                f"{event.get('run_cap')} |"
            )
        lines.append("")
    if audits or sweeps or polls or task_polls:
        lines.extend(["### Pending-handle reconciliation", ""])
        for event in audits:
            lines.append(
                f"- Indexed audit: {event.get('loaded')} loaded / {event.get('listed')} selected; "
                f"{event.get('unavailable')} unavailable; tasks "
                f"`{json.dumps(event.get('by_task', {}), sort_keys=True)}`."
            )
        if polls:
            totals = {
                key: sum(int(event.get(key, 0) or 0) for event in polls)
                for key in ("completed", "pending", "missing", "failed", "unknown")
            }
            lines.append(
                "- Batch-poll observations (retries can repeat jobs): "
                + ", ".join(f"{key}={value}" for key, value in totals.items())
                + "."
            )
        for event in sweeps:
            lines.append(
                f"- Sweep: missing handles cleared={event.get('missing_cleared', 0)}, "
                f"unobserved={event.get('v2_unobserved', 0)}, "
                f"unavailable={event.get('unavailable', 0)}."
            )
        if (
            any(event.get("v2_unobserved") or event.get("unavailable") for event in sweeps)
            or any(event.get("unknown") for event in polls)
            or any(event.get("unknown") for event in task_polls)
            or any(event.get("unavailable") for event in audits)
        ):
            lines.append(
                "- **Unresolved accounting:** some handles could not be observed; preserved "
                "pending state is not proof of a live queued job. Inspect the retained artifact."
            )
        if task_polls:
            lines.extend(
                [
                    "",
                    "Poll observations by task (including repeated observations):",
                    "",
                    "| Task | Completed | Pending | Missing | Failed | Unknown |",
                    "| --- | ---: | ---: | ---: | ---: | ---: |",
                ]
            )
            tasks: dict[str, Counter[str]] = defaultdict(Counter)
            for event in task_polls:
                for key in ("completed", "pending", "missing", "failed", "unknown"):
                    tasks[str(event.get("task"))][key] += int(event.get(key, 0) or 0)
            for task, counts in sorted(tasks.items()):
                lines.append(
                    f"| {task} | {counts['completed']} | {counts['pending']} | "
                    f"{counts['missing']} | {counts['failed']} | {counts['unknown']} |"
                )
        lines.append("")
