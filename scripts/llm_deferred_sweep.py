"""Complete pending deferred/dispatched LLM requests (R13).

Reconciles everything the deferred-request registry (``citypods.compute.llm_deferred``) has
pending -- a deferred-direct request whose cost/timing preference or quota exhaustion has since
cleared, or a genuine Mistral dispatch handle that finished at the Worker. Each invocation is a
complete, idempotent sweep of whatever is currently pending; there is no per-item state to track
between runs beyond what the registry itself already holds. Also prunes registry records past
their TTL, so a caller whose own identity changes run to run (e.g. city discovery's recipe_hash,
which depends on that run's search results) doesn't leave orphaned records behind forever.

Scheduled every six hours, with one run inside DeepSeek's off-peak discount window (see the workflow
this script backs). A normal run is an observation and bounded-retry pass, not a multi-hour drain:
durable v2 submissions and reads are batched, while direct retries stop cleanly at the configured
deadline so the next cadence can make a fresh capacity decision.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from citypods.compute.base import InferenceJob, JobHandle, JobResult
from citypods.compute.llm import (
    LiteLLMBackend,
    LLMBackendConfig,
    LLMDispatchTerminalError,
    LLMStructuredOutputError,
)
from citypods.compute.llm_deferred import (
    MAX_TERMINAL_FAILURE_RETRIES,
    discard_terminal_failure,
    load_deferred_snapshot,
    load_v2_terminal_cursor,
    look_up_v2_deferred_ref,
    prune_expired_deferred_snapshot,
    prune_expired_failure_markers,
    record_schema_correction,
    repair_deferred_index,
    schema_correction_attempted,
    snapshot_deferred_handles,
    write_deferred,
    write_v2_terminal_cursor,
)
from citypods.compute.llm_policy import ROUTES, DeferredLLMRequest
from citypods.config import load_site_config
from citypods.storage import make_storage


class _StopState:
    """Signal-safe stop flag checked between reconciliation attempts."""

    requested = False


def _install_signal_handlers() -> _StopState:
    stop_state = _StopState()

    def _request_stop(_signum, _frame):
        stop_state.requested = True

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)
    return stop_state


def _with_sweep_deadline(handle, deadline_at: datetime):
    """Let deferred-direct retries pace until this sweep's remaining wall-clock budget.

    The original caller's deadline was usually the short `tag.yml` lane budget that produced the
    deferral in the first place. Reusing that stale deadline in the daily sweep makes the retry give
    up immediately instead of waiting for the provider's next minute window. Genuine dispatch
    handles have no `deferred_request` and are left untouched: they only poll already-submitted
    work.
    """
    deferred = handle.deferred_request
    if not isinstance(deferred, DeferredLLMRequest):
        return handle
    # These producers persist a recipe and can finalize on a later scheduled pass. A record that
    # predates queue-only mode must therefore be submitted to the Worker now, not retried through
    # the runner's direct LiteLLM transport or discarded because its producer deadline elapsed.
    # City onboarding is intentionally absent: it consumes the answer synchronously during the
    # discovery pass and remains direct by design.
    durable_purposes = (
        "topic-tags",
        "chapter-agenda",
        "chapter-locator",
        "tournament:",
        "r5-benchmark:",
    )
    if deferred.policy.purpose.startswith(durable_purposes):
        return replace(
            handle,
            deferred_request=DeferredLLMRequest(
                messages=deferred.messages,
                policy=replace(deferred.policy, deadline_at=None, queue_only=True),
                output_token_budget=deferred.output_token_budget,
            ),
        )
    return replace(
        handle,
        deferred_request=DeferredLLMRequest(
            messages=deferred.messages,
            policy=replace(deferred.policy, deadline_at=deadline_at),
            output_token_budget=deferred.output_token_budget,
        ),
    )


def _capacity_signature(handle) -> tuple | None:
    """The resolved route pool a record draws capacity from, or ``None`` for a real dispatch poll
    (nothing to skip ahead on -- it's not re-running ``select_route`` at all, just checking on
    work already submitted to a Worker).

    Keyed on *what ``select_route`` actually evaluates* -- the candidate model set and the paid
    gate -- not on which feature/purpose produced the record. Two different callers (e.g. two
    structured-output features) sharing the exact same ``allowed_models`` draw on the exact same
    underlying provider quota pools, so once one has proven that pool exhausted for the rest of
    this sweep, the other's backlog can skip ahead too instead of independently re-discovering the
    identical answer. ``allowed_models=None`` means "every configured route", so it's normalized
    to the full set rather than left as a distinct one-off signature.
    """
    deferred = handle.deferred_request
    if not isinstance(deferred, DeferredLLMRequest):
        return None
    policy = deferred.policy
    models = (
        frozenset(policy.allowed_models) if policy.allowed_models is not None else frozenset(ROUTES)
    )
    return (models, policy.allow_paid)


def _job_from_deferred_handle(handle: JobHandle) -> InferenceJob:
    """Rebuild a queue-only policy job without issuing its legacy singleton reconcile call."""
    deferred = handle.deferred_request
    assert isinstance(deferred, DeferredLLMRequest)
    inputs: dict[str, object] = {
        "messages": [dict(message) for message in deferred.messages],
        "max_tokens": deferred.output_token_budget,
        "llm_policy": deferred.policy,
    }
    if handle.structured_output:
        inputs["structured_output"] = handle.structured_output
    return InferenceJob(task=handle.task, inputs=inputs, recipe_hash=handle.recipe_hash)


def _pending_breakdown(handles, *, legacy_backend: str) -> dict[str, int]:
    """Count the client-owned outstanding handles without scanning either Worker's queue."""
    counts = {
        "v1_dispatched": 0,
        "v2_dispatched": 0,
        "v2_deferred": 0,
        "direct_deferred": 0,
        "other": 0,
    }
    for handle in handles:
        deferred = handle.deferred_request
        if handle.backend == "llm-dispatch-v2":
            counts["v2_deferred" if deferred is not None else "v2_dispatched"] += 1
        elif handle.backend == legacy_backend and deferred is None:
            counts["v1_dispatched"] += 1
        elif isinstance(deferred, DeferredLLMRequest):
            counts["direct_deferred"] += 1
        else:
            counts["other"] += 1
    return counts


def _register_known_contracts() -> None:
    """Register every feature's structured-output contract this process might need to validate.

    A pending record's ``structured_output`` name is only resolvable if something in *this*
    process registered it (``citypods.compute.structured.response_model`` is a plain in-memory
    registry, populated per-process) -- and reconciling a deferred/dispatched job is exactly what
    this sweep exists to do. Each feature's own call site (`llm_tag_suggestions`, `classify`)
    already registers its contract when the *feature* runs, but the sweep is a separate process
    that never calls either, so without this it can only ever fail to reconcile a structured
    "tag" or "classify-civic-platforms" record -- not crash (each failure is caught and logged
    per-record below), but never actually complete it either. Wrapped in try/except ImportError
    per feature so the sweep still runs for whichever features' optional extras (pydantic and
    friends) happen to be installed, rather than requiring every feature's dependencies at once.
    """
    try:
        from citypods.tags import ensure_llm_contract, ensure_prelabeler_contract

        ensure_llm_contract()
        ensure_prelabeler_contract()
    except ImportError:
        pass
    try:
        import citypods.discovery.classify  # noqa: F401 -- registers on import, side effect only
    except ImportError:
        pass
    try:
        from citypods.chapter_titles import ensure_agenda_item_extractor_contract

        ensure_agenda_item_extractor_contract()
    except ImportError:
        pass
    try:
        from citypods.chapter_locator import ensure_locator_contract

        ensure_locator_contract()
    except ImportError:
        pass
    try:
        from citypods.moments import ensure_moment_contract

        ensure_moment_contract()
    except ImportError:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-config", default="config/site_config.yml")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument(
        "--run-time-budget-minutes",
        type=float,
        default=30.0,
        help="Internal graceful wall-clock budget; Actions wraps this with a 40m timeout.",
    )
    parser.add_argument(
        "--repair-index",
        action="store_true",
        help="Rebuild the B2 pointer index from canonical records and finish dual-read migration.",
    )
    args = parser.parse_args(argv)

    site_config = load_site_config(args.site_config)
    storage = make_storage(site_config, "", Path(args.output_dir))
    if storage is None or not getattr(storage, "cas_capable", False):
        print(
            "llm-deferred-sweep: no CAS-capable storage configured, nothing to do",
            file=sys.stderr,
        )
        return 0

    # A backend able to reach every transport a pending record might need: `dispatch_url` (from
    # LLM_DISPATCH_URL/LLM_DISPATCH_AUTH_TOKEN) makes mistral-dispatch reachable alongside direct,
    # regardless of LLM_MODE -- this is exactly the caller `_available_transports()` was built for
    # (see citypods/compute/llm.py), since the sweep services a mixed bag of records regardless of
    # which route originally claimed them.
    # This workflow services a mix of direct and dispatch records.  Tag records are explicitly
    # upgraded to queue_only above; that path posts to LLM_DISPATCH_URL itself, while every other
    # deferred record preserves its original policy-selected behavior.
    backend = LiteLLMBackend(LLMBackendConfig.from_env(), storage=storage)
    _register_known_contracts()

    if args.repair_index:
        unavailable = []
        repaired = repair_deferred_index(storage, unavailable=unavailable)
        print(
            f"llm-deferred-sweep: repaired {repaired} canonical deferred records, "
            f"{len(unavailable)} unavailable"
        )
        for error in unavailable:
            print(
                f"llm-deferred-sweep: unavailable object skipped key={error.key} "
                f"reason={error.reason}",
                file=sys.stderr,
            )
        return 0

    stop_state = _install_signal_handlers()
    deadline_at = datetime.now(UTC) + timedelta(minutes=args.run_time_budget_minutes)
    completed = 0
    still_pending = 0
    failed = 0
    recovered_terminal_failures = 0
    seen_pending: set[str] = set()
    exhausted_capacity: set[tuple] = set()
    # Per-pool tally, purely for the end-of-run breakdown below -- visibility into which route
    # pool (if any) is the sweep's actual bottleneck, without persisting a separate index that
    # could drift out of sync with the registry itself.
    skipped_by_pool: dict[tuple, int] = {}
    v2_unobserved = 0

    snapshot_started_at = datetime.now(UTC)
    print(
        json.dumps(
            {
                "event": "llm_deferred_snapshot_load_started",
                "deadline_at": deadline_at.isoformat(),
            },
            sort_keys=True,
        ),
        flush=True,
    )
    snapshot = load_deferred_snapshot(
        storage,
        deadline_at=deadline_at,
        should_stop=lambda: stop_state.requested,
        reconcile_only=True,
    )
    snapshot_elapsed_seconds = round((datetime.now(UTC) - snapshot_started_at).total_seconds(), 3)
    pending_handles = list(snapshot.pending())
    legacy_backend = getattr(backend, "name", "litellm")
    start_summary: dict[str, object] = {
        "event": "llm_deferred_sweep_start",
        "pending": _pending_breakdown(pending_handles, legacy_backend=legacy_backend),
        "snapshot": {
            "deadline_reached": snapshot.deadline_reached,
            "elapsed_seconds": snapshot_elapsed_seconds,
            "listed": snapshot.listed_count,
            "loaded": len(snapshot.entries),
            "omitted": snapshot.omitted_count,
        },
    }
    stats_method = getattr(backend, "dispatch_v2_stats", None)
    has_v2_dispatch = getattr(getattr(backend, "config", None), "dispatch_v2_url", None)
    if callable(stats_method) and has_v2_dispatch:
        try:
            start_summary["v2_scheduler"] = stats_method()
        except Exception as exc:  # noqa: BLE001 -- observability must not block reaping
            start_summary["v2_scheduler_error"] = type(exc).__name__
    print(json.dumps(start_summary, sort_keys=True), flush=True)
    if snapshot.unavailable_reads:
        for error in snapshot.unavailable_reads:
            print(
                f"llm-deferred-sweep: unavailable object skipped key={error.key} "
                f"reason={error.reason}",
                file=sys.stderr,
            )
    if not stop_state.requested and datetime.now(UTC) < deadline_at:
        prune_expired_failure_markers(storage)
    else:
        print(
            json.dumps(
                {
                    "event": "llm_deferred_sweep_maintenance_skipped",
                    "reason": "deadline" if not stop_state.requested else "signal",
                },
                sort_keys=True,
            ),
            flush=True,
        )

    def recover_terminal(handle: JobHandle, exc: Exception, *, target_snapshot=snapshot) -> None:
        """Persist one terminal result without re-polling a batch-observed v2 job."""
        nonlocal failed, recovered_terminal_failures
        failed += 1
        if isinstance(exc, LLMStructuredOutputError):
            if schema_correction_attempted(storage, handle.recipe_hash):
                marker_count = discard_terminal_failure(
                    storage, target_snapshot, handle, exc, backend=backend, exhausted=True
                )
                recovered_terminal_failures += 1
                print(
                    f"llm-deferred-sweep: {handle.recipe_hash} malformed correction exhausted "
                    f"(attempt {marker_count}/{MAX_TERMINAL_FAILURE_RETRIES}): {exc}",
                    file=sys.stderr,
                )
                return
            try:
                corrected = backend.retry_malformed_dispatched(handle)
                write_deferred(storage, handle.recipe_hash, corrected)
                snapshot.replace_pending(handle.recipe_hash, corrected)
                # Persist the one-correction guard before deleting the completed request. A
                # marker-write failure must leave the original intact, not permit a second retry.
                record_schema_correction(storage, handle, exc)
                backend.ack_dispatched_ref(handle)
                backend.delete_dispatched_ref(handle.ref)
                print(
                    f"llm-deferred-sweep: {handle.recipe_hash} submitted one schema correction",
                    file=sys.stderr,
                )
            except Exception as retry_exc:  # noqa: BLE001 -- retain old handle for retry
                print(
                    f"llm-deferred-sweep: {handle.recipe_hash} schema correction failed: "
                    f"{retry_exc}",
                    file=sys.stderr,
                )
            return

        assert isinstance(exc, LLMDispatchTerminalError)
        marker_count = discard_terminal_failure(
            storage, target_snapshot, handle, exc, backend=backend
        )
        recovered_terminal_failures += 1
        print(
            f"llm-deferred-sweep: {handle.recipe_hash} terminal failure recovered "
            f"(attempt {marker_count}/{MAX_TERMINAL_FAILURE_RETRIES}): {exc}",
            file=sys.stderr,
        )

    # Submitted v2 work is consumed from the coordinator's bounded terminal feed.  This replaces
    # the old "load every pending record, then poll every id" loop once `--repair-index` has
    # seeded v2-ref pointers.  A missing pointer is harmless (the job was already consumed or
    # predates migration); advance the cursor so it never turns into a permanent hot row.
    v2_terminal_seen = 0
    if has_v2_dispatch and not stop_state.requested and datetime.now(UTC) < deadline_at:
        try:
            terminal_page = backend.terminal_feed(load_v2_terminal_cursor(storage))
            terminal_rows = terminal_page.get("terminals", [])
            terminal_handles = [
                handle
                for row in terminal_rows
                if isinstance(row, dict)
                and isinstance(row.get("id"), str)
                and (handle := look_up_v2_deferred_ref(storage, row["id"])) is not None
            ]
            v2_terminal_seen = len(terminal_rows)
            terminal_page_fully_consumed = True
            if terminal_handles:
                terminal_snapshot = snapshot_deferred_handles(storage, terminal_handles)
                terminal_results = backend.poll_batch(terminal_handles)
                for handle in terminal_handles:
                    result = terminal_results.get(handle.ref)
                    if isinstance(result, (LLMStructuredOutputError, LLMDispatchTerminalError)):
                        recover_terminal(handle, result, target_snapshot=terminal_snapshot)
                    elif isinstance(result, JobResult):
                        completed += 1
                    else:
                        v2_unobserved += 1
                        terminal_page_fully_consumed = False
            cursor = terminal_page.get("cursor")
            if terminal_page_fully_consumed and isinstance(cursor, dict):
                write_v2_terminal_cursor(storage, cursor)
        except Exception as exc:  # noqa: BLE001 -- leave cursor untouched for the next cadence
            print(
                f"llm-deferred-sweep: v2 terminal feed failed: {type(exc).__name__}",
                file=sys.stderr,
            )

    if not stop_state.requested and datetime.now(UTC) < deadline_at:
        handled_recipe_hashes: set[str] = set()
        # Queue-only records created before the normal call sites adopted run batching must not
        # turn the sweep into one ingress invocation per record. Their portable policy capsule is
        # sufficient to rebuild the original InferenceJob and submit one bounded v2 batch.
        v2_queue_only: list[tuple[JobHandle, JobHandle]] = []
        if has_v2_dispatch:
            for handle in pending_handles:
                if not isinstance(handle.deferred_request, DeferredLLMRequest):
                    continue
                upgraded_handle = _with_sweep_deadline(handle, deadline_at)
                upgraded_request = upgraded_handle.deferred_request
                if (
                    isinstance(upgraded_request, DeferredLLMRequest)
                    and upgraded_request.policy.queue_only
                ):
                    v2_queue_only.append((handle, upgraded_handle))
            if v2_queue_only and not stop_state.requested and datetime.now(UTC) < deadline_at:
                try:
                    queued_results = backend.enqueue_batch(
                        [_job_from_deferred_handle(upgraded) for _handle, upgraded in v2_queue_only]
                    )
                except Exception as exc:  # noqa: BLE001 -- leave every durable request pending
                    queued_results = [exc] * len(v2_queue_only)
                for (handle, _upgraded), result in zip(v2_queue_only, queued_results, strict=True):
                    handled_recipe_hashes.add(handle.recipe_hash)
                    seen_pending.add(handle.recipe_hash)
                    if isinstance(result, Exception):
                        failed += 1
                        print(
                            f"llm-deferred-sweep: {handle.recipe_hash} v2 enqueue failed: {result}",
                            file=sys.stderr,
                        )
                    elif isinstance(result, JobResult):
                        completed += 1
                        snapshot.mark_completed(handle.recipe_hash, result)
                    else:
                        still_pending += 1
                        snapshot.replace_pending(handle.recipe_hash, result)

        # Excludes a deferred_request-bearing handle (the client-side daily-cap/429 short-circuit
        # in enqueue_batch, tagged backend="llm-dispatch-v2" but never actually enqueued
        # server-side) -- reconcile() itself branches the same way (_reconcile_deferred vs.
        # poll_batch), and polling a ref the coordinator has no record of is pure waste.
        v2_handles = [
            h
            for h in snapshot.pending()
            if h.backend == "llm-dispatch-v2" and h.deferred_request is None
        ]
        # A batch response is the only v2 observation this sweep makes for a handle. Unknown
        # outcomes get one recovery *batch*; after that they remain pending for the next cadence.
        # Falling through to `reconcile()` would turn one failed batch into N singleton polls.
        v2_results = {}
        if v2_handles and not stop_state.requested and datetime.now(UTC) < deadline_at:
            try:
                v2_results = backend.poll_batch(v2_handles)
            except Exception as exc:  # noqa: BLE001
                print(
                    f"llm-deferred-sweep: batch poll for v2 handles failed: {type(exc).__name__}",
                    file=sys.stderr,
                )
            unresolved = [
                handle
                for handle in v2_handles
                if handle.ref not in v2_results or isinstance(v2_results[handle.ref], Exception)
            ]
            if unresolved:
                print(
                    json.dumps(
                        {
                            "event": "llm_dispatch_v2_batch",
                            "operation": "poll-batch-retry",
                            "batch_size": len(unresolved),
                            "batch_retry": 1,
                            "singleton_fallback": 0,
                            "request_count": 1,
                        },
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                )
                try:
                    v2_results.update(backend.poll_batch(unresolved))
                except Exception as exc:  # noqa: BLE001 -- isolate below after two batch attempts
                    print(
                        f"llm-deferred-sweep: v2 recovery batch poll failed: {type(exc).__name__}",
                        file=sys.stderr,
                    )
            for handle in v2_handles:
                handled_recipe_hashes.add(handle.recipe_hash)
                seen_pending.add(handle.recipe_hash)
                result = v2_results.get(handle.ref)
                if isinstance(result, (LLMStructuredOutputError, LLMDispatchTerminalError)):
                    recover_terminal(handle, result)
                elif isinstance(result, Exception) or handle.ref not in v2_results:
                    v2_unobserved += 1
                elif isinstance(result, JobResult):
                    completed += 1
                    snapshot.mark_completed(handle.recipe_hash, result)
                else:
                    still_pending += 1
        if v2_unobserved:
            print(
                json.dumps(
                    {
                        "event": "llm_dispatch_v2_batch",
                        "operation": "poll-unobserved",
                        "batch_size": len(v2_handles),
                        "unobserved": v2_unobserved,
                        "singleton_fallback": 0,
                        "request_count": 0,
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
        for handle in snapshot.pending():
            if stop_state.requested or datetime.now(UTC) >= deadline_at:
                break
            if handle.recipe_hash in handled_recipe_hashes:
                continue
            reconciled_handle = _with_sweep_deadline(handle, deadline_at)
            deferred = reconciled_handle.deferred_request
            # A queue-only tag handle remains pending immediately after a successful Worker
            # submission. That is durable acceptance, not evidence that runner-side provider
            # capacity is exhausted; never let it suppress the rest of the queued tag backlog.
            queue_only = isinstance(deferred, DeferredLLMRequest) and deferred.policy.queue_only
            signature = None if queue_only else _capacity_signature(reconciled_handle)
            if signature in exhausted_capacity:
                skipped_by_pool[signature] = skipped_by_pool.get(signature, 0) + 1
                continue
            seen_pending.add(handle.recipe_hash)
            try:
                result = backend.reconcile(reconciled_handle)
            except LLMStructuredOutputError as exc:
                recover_terminal(handle, exc)
                continue
            except LLMDispatchTerminalError as exc:
                recover_terminal(handle, exc)
                continue
            except Exception as exc:  # noqa: BLE001 -- one bad record must not abort the sweep
                failed += 1
                print(f"llm-deferred-sweep: {handle.recipe_hash} failed: {exc}", file=sys.stderr)
                continue
            if result is None:
                still_pending += 1
                if signature is not None:
                    exhausted_capacity.add(signature)
            else:
                completed += 1
                snapshot.mark_completed(handle.recipe_hash, result)
            if stop_state.requested:
                break

    # Each handle in the snapshot has now either completed, remained genuinely pending, failed
    # independently, or paced until no allowed route could fit before the sweep deadline. A second
    # immediate pass would only re-poll/re-log the same remaining handles; the next scheduled run
    # gets a fresh registry snapshot.

    pruned = prune_expired_deferred_snapshot(storage, snapshot, backend=backend)
    remaining = sum(
        1
        for entry in snapshot.entries
        if not entry.deleted and isinstance(entry.decoded, JobHandle)
    )
    end_summary = {
        "event": "llm_deferred_sweep_end",
        "completed": completed,
        "failed": failed,
        "pruned": pruned,
        "remaining": _pending_breakdown(snapshot.pending(), legacy_backend=legacy_backend),
        "snapshot": {
            "deadline_reached": snapshot.deadline_reached,
            "listed": snapshot.listed_count,
            "loaded": len(snapshot.entries),
            "omitted": snapshot.omitted_count,
        },
        "unavailable": len(snapshot.unavailable_reads),
        "v2_unobserved": v2_unobserved,
        "v2_terminal_seen": v2_terminal_seen,
    }
    print(json.dumps(end_summary, sort_keys=True), flush=True)
    print(
        f"llm-deferred-sweep: {len(seen_pending)} pending seen, {completed} completed, "
        f"{still_pending} still pending observations, {failed} failed "
        f"({recovered_terminal_failures} terminally recovered), {pruned} pruned, {remaining} "
        f"remaining, {v2_unobserved} v2 unobserved, {len(snapshot.unavailable_reads)} unavailable"
    )
    for signature, skipped in sorted(skipped_by_pool.items(), key=lambda item: -item[1]):
        models, allow_paid = signature
        pool = ",".join(sorted(models)) or "(none)"
        print(
            f"llm-deferred-sweep: pool [{pool}] (allow_paid={allow_paid}) exhausted -- "
            f"{skipped} further record(s) skipped without a reconcile attempt",
            file=sys.stderr,
        )
    if stop_state.requested:
        print("llm-deferred-sweep: graceful stop requested; finished work has been persisted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
