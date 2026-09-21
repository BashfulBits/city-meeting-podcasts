#!/usr/bin/env python3
"""Classify and optionally retire stale chapter-agenda deferred jobs.

The normal deferred sweep intentionally lists only route partitions with current capacity. That is
the right optimization for reconciliation, but it hides precisely the legacy jobs pinned to an
exhausted or paused model. This maintenance command reads the canonical deferred registry, finds
chapter-agenda handles that use a retired model or have waited past the supplied age threshold,
and, with ``--apply``, batch-cancels submitted v2 jobs before discarding their client records.

It never scans or mutates arbitrary Durable Object rows. The deferred registry is the client-owned
set of jobs that can be safely superseded: the next chapter-agenda run sees the missing handle and
builds a fresh recipe under the current Nemotron/Gemini policy. A v2 handle reported as in-flight
is left in place because the coordinator intentionally fences leased work to normal completion.

Examples::

    PYTHONPATH=. python scripts/reconcile_stuck_chapter_agenda.py --dry-run
    PYTHONPATH=. python scripts/reconcile_stuck_chapter_agenda.py --apply --older-than-hours 24
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
from collections import Counter
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from citypods.chapter_titles import AGENDA_BACKUP_MODELS, AGENDA_PRODUCTION_MODELS
from citypods.compute.base import JobHandle
from citypods.compute.llm import _WORKER_BATCH_LIMIT, LiteLLMBackend, LLMBackendConfig
from citypods.compute.llm_deferred import (
    discard_deferred,
    load_deferred_snapshot,
)
from citypods.compute.llm_policy import DeferredLLMRequest
from citypods.config import load_site_config
from citypods.storage import StorageReadUnavailable, make_storage

CURRENT_MODELS = frozenset((*AGENDA_PRODUCTION_MODELS, *AGENDA_BACKUP_MODELS))
DEFAULT_MAX_ROW_WRITES = 25_000
# A queued cancellation deletes one or more job-model rows and updates the job row plus the
# state/updated-at indexes. Cloudflare bills affected index rows too, so keep a safety margin over
# the seven rows a one-model job currently touches. This is a conservative budget estimate, not a
# replacement for the platform's eventual usage meter.
ESTIMATED_CANCEL_ROW_WRITES_PER_JOB = 8
# Each discard is an independent CAS lease-acquire + read + delete + index-cleanup against its own
# recipe_hash key (see citypods/compute/llm_deferred.py's _deferred_record_lock) -- genuinely
# independent B2/R2 objects, so this mirrors the read-side concurrency convention already used for
# the same registry (SNAPSHOT_READ_WORKERS in llm_deferred.py, _STATE_SYNC_MAX_WORKERS in
# statesync.py). A production run with ~4,000 discard candidates took over 1.5 hours fully
# sequential and still didn't finish inside the workflow's 110-minute step timeout.
DISCARD_WORKERS = 16
DEFAULT_RUN_TIME_BUDGET_MINUTES = 100.0
# Mirrors repair_deferred_index's own progress_interval default so a long discard pass is
# observable in the workflow's live log, not just from a final report a killed process never
# reaches.
DISCARD_PROGRESS_INTERVAL = 1000


class _StopState:
    """Signal-safe stop flag checked between discard batches (mirrors llm_deferred_sweep.py)."""

    requested = False


def _install_signal_handlers() -> _StopState:
    stop_state = _StopState()

    def _request_stop(_signum, _frame):
        stop_state.requested = True

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)
    return stop_state


def _created_at(data: Mapping[str, Any] | None) -> datetime | None:
    """Parse a persisted registry timestamp, returning ``None`` for malformed data."""
    value = data.get("created_at") if isinstance(data, Mapping) else None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _purpose(handle: JobHandle) -> str | None:
    """Return the deferred policy purpose, if this handle carries a synthetic request."""
    deferred = handle.deferred_request
    if not isinstance(deferred, DeferredLLMRequest):
        return None
    return deferred.policy.purpose


def _classify_entry(entry, *, now: datetime, older_than_hours: float) -> dict[str, Any] | None:
    """Classify one snapshot entry when it is an old or legacy chapter-agenda handle."""
    handle = entry.decoded
    if not isinstance(handle, JobHandle) or handle.task != "agenda-item-extract":
        return None

    purpose = _purpose(handle)
    if purpose not in {None, "chapter-agenda"}:
        return None

    created = _created_at(entry.data)
    age_hours = ((now - created).total_seconds() / 3600) if created else None
    reasons: list[str] = []
    if handle.model and handle.model not in CURRENT_MODELS:
        reasons.append("legacy-model")
    if age_hours is not None and age_hours >= older_than_hours:
        reasons.append("age")
    if not reasons:
        return None

    remote = (
        handle.backend == "llm-dispatch-v2"
        and handle.deferred_request is None
        and bool(handle.ref)
        and not handle.ref.startswith("deferred")
    )
    synthetic = isinstance(handle.deferred_request, DeferredLLMRequest)
    return {
        "recipe_hash": handle.recipe_hash,
        "ref": handle.ref,
        "backend": handle.backend,
        "model": handle.model,
        "purpose": purpose,
        "created_at": created.isoformat() if created else None,
        "age_hours": round(age_hours, 2) if age_hours is not None else None,
        "reasons": reasons,
        "synthetic": synthetic,
        "remote_v2": remote,
    }


def _summary(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize candidate counts for the human- and machine-readable report."""
    return {
        "candidate_count": len(candidates),
        "by_reason": dict(Counter(reason for item in candidates for reason in item["reasons"])),
        "by_model": dict(Counter(item["model"] or "<unresolved>" for item in candidates)),
        "remote_v2_count": sum(item["remote_v2"] for item in candidates),
        "non_remote_count": sum(not item["remote_v2"] for item in candidates),
    }


def _apply(
    storage,
    backend: LiteLLMBackend,
    candidates: list[dict[str, Any]],
    *,
    max_row_writes: int = DEFAULT_MAX_ROW_WRITES,
    discard_workers: int = DISCARD_WORKERS,
    deadline_at: datetime | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Cancel confirmed remote jobs and discard only handles safe to supersede."""
    if max_row_writes < 0:
        raise ValueError("max_row_writes must be non-negative")
    if discard_workers < 1:
        raise ValueError("discard_workers must be at least one")

    remote = [item for item in candidates if item["remote_v2"]]
    max_remote = max_row_writes // ESTIMATED_CANCEL_ROW_WRITES_PER_JOB
    selected_remote = remote[:max_remote]
    budget_skipped = remote[max_remote:]
    budget_skipped_refs = {item["ref"] for item in budget_skipped}
    synthetic = [item for item in candidates if item["synthetic"]]
    cancelled: set[str] = set()
    in_flight: set[str] = set()
    not_found: set[str] = set()
    errors: list[str] = []

    for start in range(0, len(selected_remote), _WORKER_BATCH_LIMIT):
        refs = [item["ref"] for item in selected_remote[start : start + _WORKER_BATCH_LIMIT]]
        try:
            result = backend.cancel_batch(refs)
        except Exception as exc:  # noqa: BLE001 -- retain every record on cancellation failure
            errors.append(f"cancel batch failed: {type(exc).__name__}: {exc}")
            continue
        cancelled.update(result.get("cancelled", ()))
        in_flight.update(result.get("in_flight", ()))
        not_found.update(result.get("not_found", ()))

    # Pass 1: cheap, in-memory eligibility check (no I/O) -- decide which candidates are actually
    # safe to discard vs. immediately retained for an already-known reason.
    discarded = 0
    retained = 0
    dispositions: Counter[str] = Counter()
    to_discard: list[dict[str, Any]] = []
    for item in candidates:
        ref = item["ref"]
        if item["remote_v2"]:
            if ref in budget_skipped_refs:
                dispositions["write_budget_retained"] += 1
                retained += 1
                continue
            if ref in in_flight:
                dispositions["in_flight_retained"] += 1
                retained += 1
                continue
            if ref not in cancelled and ref not in not_found:
                dispositions["cancel_unknown_retained"] += 1
                retained += 1
                continue
        elif not item["synthetic"]:
            dispositions["unsupported_remote_retained"] += 1
            retained += 1
            continue
        to_discard.append(item)

    # Pass 2: the actual network-bound work -- each discard_deferred call is an independent CAS
    # lease-acquire + read + delete + index-cleanup against its own recipe_hash key (see
    # citypods/compute/llm_deferred.py's _deferred_record_lock docstring: distinct keys, no shared
    # lock), so these are safe to run fully in parallel. A production run with ~4,000 of these
    # fully sequential took over 1.5 hours and still didn't finish inside the workflow's
    # 110-minute step timeout -- see DISCARD_WORKERS' own comment.
    def _discard_one(item: dict[str, Any]) -> tuple[str, str | None]:
        try:
            discarded_record = discard_deferred(
                storage, item["recipe_hash"], expected_ref=item["ref"]
            )
        except StorageReadUnavailable as exc:
            # A transiently unreadable record must remain available for the next maintenance run;
            # one B2/R2 read failure must not abort the rest of an apply pass.
            return "record_unavailable_retained", f"{type(exc).__name__}: {exc}"
        return ("superseded" if discarded_record else "record_changed_or_missing"), None

    deadline_reached = False
    if to_discard:
        total = len(to_discard)
        print(
            f"stuck chapter-agenda reconciliation: discarding {total} candidates",
            file=sys.stderr,
            flush=True,
        )
        started_at = datetime.now(UTC)
        executor = ThreadPoolExecutor(
            max_workers=min(discard_workers, len(to_discard)),
            thread_name_prefix="reconcile-discard",
        )
        stopped = False
        try:
            futures = {executor.submit(_discard_one, item): item for item in to_discard}
            for idx, future in enumerate(as_completed(futures), 1):
                item = futures[future]
                disposition, error = future.result()
                dispositions[disposition] += 1
                if disposition == "superseded":
                    discarded += 1
                elif disposition == "record_unavailable_retained":
                    retained += 1
                # "record_changed_or_missing" counts toward neither discarded nor retained,
                # matching the pre-parallelization behavior: the record was already gone or
                # superseded by something else under us, which is neither this run's doing nor
                # something for it to retry.
                if error is not None:
                    errors.append(f"discard failed for {item['recipe_hash']}: {error}")
                # Printed every DISCARD_PROGRESS_INTERVAL completions (mirroring
                # llm_deferred.py's repair_deferred_index) so a long apply pass is observable
                # while it runs -- not just from a final report a killed process never reaches.
                # This is exactly the visibility gap the timed-out production run exposed: its
                # own artifact came back completely empty, with no way to tell afterward how much
                # of its ~1h39m apply phase actually happened.
                if idx % DISCARD_PROGRESS_INTERVAL == 0 or idx == total:
                    elapsed = (datetime.now(UTC) - started_at).total_seconds()
                    rate = idx / elapsed if elapsed > 0 else 0.0
                    print(
                        f"stuck chapter-agenda reconciliation: discard progress {idx}/{total} "
                        f"({idx / total * 100:.1f}%) [{elapsed:.0f}s elapsed, {rate:.1f}/s, "
                        f"discarded={discarded}, retained={retained}]",
                        file=sys.stderr,
                        flush=True,
                    )
                if (should_stop is not None and should_stop()) or (
                    deadline_at is not None and datetime.now(UTC) >= deadline_at
                ):
                    stopped = True
                    deadline_reached = True
                    # Stop consuming further completions so the still-queued (not-yet-started)
                    # futures cancelled below actually stay uncancelled-into -- otherwise
                    # `as_completed` would just keep draining every already-submitted future
                    # regardless, making this check a no-op.
                    break
        finally:
            # A stopped run must not wait for the remaining queued (not-yet-started) discards --
            # they are safely left as-is for the next scheduled/manual run to pick up ("deferred,
            # not failed"). Already-running discards (at most `discard_workers` of them) finish in
            # the background; their outcome is simply not reflected in this run's report.
            executor.shutdown(wait=not stopped, cancel_futures=stopped)

    return {
        "cancelled_count": len(cancelled),
        "in_flight_count": len(in_flight),
        "not_found_count": len(not_found),
        "discarded_count": discarded,
        "retained_count": retained,
        "deadline_reached": deadline_reached,
        "dispositions": dict(dispositions),
        "errors": errors,
        "synthetic_count": len(synthetic),
        "max_row_writes": max_row_writes,
        "estimated_row_writes": len(selected_remote) * ESTIMATED_CANCEL_ROW_WRITES_PER_JOB,
        "write_budget_skipped_count": len(budget_skipped),
    }


def run(
    *,
    apply: bool,
    site_config_path: str,
    output_dir: str,
    older_than_hours: float,
    max_row_writes: int = DEFAULT_MAX_ROW_WRITES,
    run_time_budget_minutes: float = DEFAULT_RUN_TIME_BUDGET_MINUTES,
    stop_state: _StopState | None = None,
) -> int:
    """Run the dry-run classification or guarded cancellation/supersession flow.

    ``run_time_budget_minutes`` bounds both the registry load and the apply/discard phase to a
    wall-clock deadline (mirroring scripts/llm_deferred_sweep.py's own `--run-time-budget-minutes`
    convention), so an unexpectedly large registry or candidate set stops this run cleanly with a
    real, complete report -- what actually happened, and how much is left for the next run --
    instead of relying on the workflow's own step timeout to kill the process mid-operation with
    nothing captured at all.
    """
    if older_than_hours <= 0:
        raise ValueError("--older-than-hours must be greater than zero")
    if max_row_writes < 0:
        raise ValueError("--max-row-writes must be non-negative")
    if run_time_budget_minutes <= 0:
        raise ValueError("--run-time-budget-minutes must be greater than zero")

    site_config = load_site_config(site_config_path)
    storage = make_storage(site_config, "", Path(output_dir))
    if storage is None or not getattr(storage, "cas_capable", False):
        raise RuntimeError("a CAS-capable B2/R2 storage backend is required")

    now = datetime.now(UTC)
    deadline_at = now + timedelta(minutes=run_time_budget_minutes)
    should_stop = (lambda: stop_state.requested) if stop_state is not None else None
    print(
        "stuck chapter-agenda reconciliation: loading full deferred registry",
        file=sys.stderr,
        flush=True,
    )
    snapshot = load_deferred_snapshot(
        storage,
        now=now,
        include_ineligible=True,
        deadline_at=deadline_at,
        should_stop=should_stop,
    )
    print(
        f"stuck chapter-agenda reconciliation: loaded {len(snapshot.entries)} of "
        f"{snapshot.listed_count} registry records"
        + (" (stopped at deadline)" if snapshot.deadline_reached else ""),
        file=sys.stderr,
        flush=True,
    )
    candidates = [
        classified
        for entry in snapshot.entries
        if (classified := _classify_entry(entry, now=now, older_than_hours=older_than_hours))
    ]
    report: dict[str, Any] = {
        "event": "stuck_chapter_agenda_classification",
        "mode": "apply" if apply else "dry-run",
        "observed_at": now.isoformat(),
        "older_than_hours": older_than_hours,
        "run_time_budget_minutes": run_time_budget_minutes,
        "snapshot": {
            "listed": snapshot.listed_count,
            "loaded": len(snapshot.entries),
            "omitted": snapshot.omitted_count,
            "unavailable": len(snapshot.unavailable_reads),
            "deadline_reached": snapshot.deadline_reached,
        },
        "classification": _summary(candidates),
        "candidates": candidates,
    }

    if apply:
        backend = LiteLLMBackend(LLMBackendConfig.from_env(), storage=storage)
        report["action"] = _apply(
            storage,
            backend,
            candidates,
            max_row_writes=max_row_writes,
            deadline_at=deadline_at,
            should_stop=should_stop,
        )

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if not report.get("action", {}).get("errors") else 1


def _whole_int(value: str) -> int:
    """Parse a CLI arg as an integer, tolerating a decimal-formatted whole number.

    GitHub Actions renders a `workflow_dispatch` input declared `type: number` as a
    decimal-formatted string (e.g. "25000.0") even for a plain integer value or default --
    confirmed live: the reconcile-stuck-chapter-agenda workflow passes `max_row_writes` straight
    through as `env: MAX_ROW_WRITES: ${{ inputs.max_row_writes }}`, and a bare `type=int` here
    rejects that shape outright (`int("25000.0")` raises `ValueError`), failing the workflow
    before it does anything. `--older-than-hours` already used `type=float` and was unaffected.
    """
    parsed = float(value)
    if not parsed.is_integer():
        raise argparse.ArgumentTypeError(f"{value!r} is not a whole number")
    return int(parsed)


def main(argv: list[str] | None = None) -> int:
    """Parse CLI arguments and convert operational failures into a nonzero exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Classify only; do not mutate state.")
    mode.add_argument("--apply", action="store_true", help="Cancel and supersede stale jobs.")
    parser.add_argument("--site-config", default="config/site_config.yml")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument(
        "--older-than-hours",
        type=float,
        default=24.0,
        help="Treat a chapter-agenda handle this old as stuck (default: 24).",
    )
    parser.add_argument(
        "--max-row-writes",
        type=_whole_int,
        default=DEFAULT_MAX_ROW_WRITES,
        help=(
            "Conservative billed row-write budget for v2 cancellation (default: "
            f"{DEFAULT_MAX_ROW_WRITES})."
        ),
    )
    parser.add_argument(
        "--run-time-budget-minutes",
        type=float,
        default=DEFAULT_RUN_TIME_BUDGET_MINUTES,
        help=(
            "Internal graceful wall-clock budget for the registry load and apply/discard phase "
            f"(default: {DEFAULT_RUN_TIME_BUDGET_MINUTES:g}); the workflow wraps this with a "
            "110-minute step timeout, so this must stay comfortably under that."
        ),
    )
    args = parser.parse_args(argv)
    stop_state = _install_signal_handlers()
    try:
        return run(
            apply=args.apply,
            site_config_path=args.site_config,
            output_dir=args.output_dir,
            older_than_hours=args.older_than_hours,
            max_row_writes=args.max_row_writes,
            run_time_budget_minutes=args.run_time_budget_minutes,
            stop_state=stop_state,
        )
    except Exception as exc:  # noqa: BLE001 -- CLI emits one actionable failure and exits nonzero
        print(f"stuck chapter-agenda reconciliation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
