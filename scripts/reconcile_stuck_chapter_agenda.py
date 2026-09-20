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
import sys
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
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
) -> dict[str, Any]:
    """Cancel confirmed remote jobs and discard only handles safe to supersede."""
    if max_row_writes < 0:
        raise ValueError("max_row_writes must be non-negative")

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

    discarded = 0
    retained = 0
    dispositions: Counter[str] = Counter()
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
        try:
            discarded_record = discard_deferred(storage, item["recipe_hash"], expected_ref=ref)
        except StorageReadUnavailable as exc:
            # A transiently unreadable record must remain available for the next maintenance run;
            # one B2/R2 read failure must not abort the rest of an apply pass.
            errors.append(f"discard failed for {item['recipe_hash']}: {type(exc).__name__}: {exc}")
            dispositions["record_unavailable_retained"] += 1
            retained += 1
            continue
        if discarded_record:
            discarded += 1
            dispositions["superseded"] += 1
        else:
            dispositions["record_changed_or_missing"] += 1

    return {
        "cancelled_count": len(cancelled),
        "in_flight_count": len(in_flight),
        "not_found_count": len(not_found),
        "discarded_count": discarded,
        "retained_count": retained,
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
) -> int:
    """Run the dry-run classification or guarded cancellation/supersession flow."""
    if older_than_hours <= 0:
        raise ValueError("--older-than-hours must be greater than zero")
    if max_row_writes < 0:
        raise ValueError("--max-row-writes must be non-negative")

    site_config = load_site_config(site_config_path)
    storage = make_storage(site_config, "", Path(output_dir))
    if storage is None or not getattr(storage, "cas_capable", False):
        raise RuntimeError("a CAS-capable B2/R2 storage backend is required")

    now = datetime.now(UTC)
    print(
        "stuck chapter-agenda reconciliation: loading full deferred registry",
        file=sys.stderr,
        flush=True,
    )
    snapshot = load_deferred_snapshot(storage, now=now, include_ineligible=True)
    print(
        f"stuck chapter-agenda reconciliation: loaded {len(snapshot.entries)} of "
        f"{snapshot.listed_count} registry records",
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
        "snapshot": {
            "listed": snapshot.listed_count,
            "loaded": len(snapshot.entries),
            "unavailable": len(snapshot.unavailable_reads),
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
        )

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if not report.get("action", {}).get("errors") else 1


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
        type=int,
        default=DEFAULT_MAX_ROW_WRITES,
        help=(
            "Conservative billed row-write budget for v2 cancellation (default: "
            f"{DEFAULT_MAX_ROW_WRITES})."
        ),
    )
    args = parser.parse_args(argv)
    try:
        return run(
            apply=args.apply,
            site_config_path=args.site_config,
            output_dir=args.output_dir,
            older_than_hours=args.older_than_hours,
            max_row_writes=args.max_row_writes,
        )
    except Exception as exc:  # noqa: BLE001 -- CLI emits one actionable failure and exits nonzero
        print(f"stuck chapter-agenda reconciliation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
