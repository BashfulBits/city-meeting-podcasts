"""Complete pending deferred/dispatched LLM requests (R13).

Reconciles everything the deferred-request registry (``citypods.compute.llm_deferred``) has
pending -- a deferred-direct request whose cost/timing preference or quota exhaustion has since
cleared, or a genuine Mistral dispatch handle that finished at the Worker. Each invocation is a
complete, idempotent sweep of whatever is currently pending; there is no per-item state to track
between runs beyond what the registry itself already holds. Also prunes registry records past
their TTL, so a caller whose own identity changes run to run (e.g. city discovery's recipe_hash,
which depends on that run's search results) doesn't leave orphaned records behind forever.

Scheduled to run once daily, timed to land inside DeepSeek's off-peak discount window (see the
workflow this script backs) -- not on a tighter cron, since nothing configured today needs a
finer-grained wake-up than that, and GitHub Actions cron minutes are worth conserving. The sweep
uses a long internal wall-clock deadline (just under four hours in Actions) as the policy deadline
for deferred-direct retries, so it paces through provider minute windows and stops gracefully before
GitHub Actions' own hard kill can discard logs or interrupt cleanup.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from citypods.compute.base import JobHandle
from citypods.compute.llm import (
    BATCH_RETRY_ISOLATION_THRESHOLD,
    LiteLLMBackend,
    LLMBackendConfig,
    LLMDispatchTerminalError,
    LLMStructuredOutputError,
)
from citypods.compute.llm_deferred import (
    MAX_TERMINAL_FAILURE_RETRIES,
    discard_terminal_failure,
    load_deferred_snapshot,
    prune_expired_deferred_snapshot,
    prune_expired_failure_markers,
    record_schema_correction,
    repair_deferred_index,
    schema_correction_attempted,
    write_deferred,
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
            ),
        )
    return replace(
        handle,
        deferred_request=DeferredLLMRequest(
            messages=deferred.messages,
            policy=replace(deferred.policy, deadline_at=deadline_at),
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
        default=235.0,
        help="Internal graceful wall-clock budget; Actions wraps this with a 240m timeout.",
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
        repaired = repair_deferred_index(storage)
        print(f"llm-deferred-sweep: repaired {repaired} canonical deferred records")
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

    snapshot = load_deferred_snapshot(storage)
    prune_expired_failure_markers(storage)
    if datetime.now(UTC) < deadline_at:
        # Excludes a deferred_request-bearing handle (the client-side daily-cap/429 short-circuit
        # in enqueue_batch, tagged backend="llm-dispatch-v2" but never actually enqueued
        # server-side) -- reconcile() itself branches the same way (_reconcile_deferred vs.
        # poll_batch), and polling a ref the coordinator has no record of is pure waste.
        v2_handles = [
            h
            for h in snapshot.pending()
            if h.backend == "llm-dispatch-v2" and h.deferred_request is None
        ]
        # A successful batch poll observes every real v2 handle whose value is a JobResult or
        # None. Skip those handles below: otherwise a daily sweep with N pending jobs makes one
        # bounded poll plus N singleton polls. Only an item-level exception or an absent key is
        # retried individually by this loop; the client batches larger recovery sets itself.
        batch_observed_refs: set[str] = set()
        batch_fallback_count = 0
        v2_results = {}
        if v2_handles:
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
            if len(unresolved) > BATCH_RETRY_ISOLATION_THRESHOLD:
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
                if handle.ref not in v2_results or isinstance(v2_results[handle.ref], Exception):
                    batch_fallback_count += 1
                    continue
                result = v2_results[handle.ref]
                batch_observed_refs.add(handle.ref)
                seen_pending.add(handle.recipe_hash)
                if result is not None:
                    completed += 1
                    snapshot.mark_completed(handle.recipe_hash, result)
                else:
                    still_pending += 1
        if batch_fallback_count:
            print(
                json.dumps(
                    {
                        "event": "llm_dispatch_v2_batch",
                        "operation": "poll-recovery",
                        "batch_size": len(v2_handles),
                        "singleton_fallback": batch_fallback_count,
                        "request_count": batch_fallback_count,
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
        for handle in snapshot.pending():
            if stop_state.requested or datetime.now(UTC) >= deadline_at:
                break
            if handle.backend == "llm-dispatch-v2" and handle.ref in batch_observed_refs:
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
                failed += 1
                if schema_correction_attempted(storage, handle.recipe_hash):
                    marker_count = discard_terminal_failure(
                        storage, snapshot, handle, exc, backend=backend, exhausted=True
                    )
                    recovered_terminal_failures += 1
                    print(
                        f"llm-deferred-sweep: {handle.recipe_hash} malformed correction exhausted "
                        f"(attempt {marker_count}/{MAX_TERMINAL_FAILURE_RETRIES}): {exc}",
                        file=sys.stderr,
                    )
                    continue
                try:
                    corrected = backend.retry_malformed_dispatched(handle)
                    write_deferred(storage, handle.recipe_hash, corrected)
                    snapshot.replace_pending(handle.recipe_hash, corrected)
                    # Persist the one-correction guard before deleting the completed request.
                    # A marker-write failure must leave the original intact, not permit a second
                    # correction of the same malformed result on the next sweep.
                    record_schema_correction(storage, handle, exc)
                    # Both replacement persistence steps are durable before the malformed source
                    # is retired. If this cleanup races, it leaves only harmless retained audit
                    # history.
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
                continue
            except LLMDispatchTerminalError as exc:
                failed += 1
                marker_count = discard_terminal_failure(
                    storage, snapshot, handle, exc, backend=backend
                )
                recovered_terminal_failures += 1
                print(
                    f"llm-deferred-sweep: {handle.recipe_hash} terminal failure recovered "
                    f"(attempt {marker_count}/{MAX_TERMINAL_FAILURE_RETRIES}): {exc}",
                    file=sys.stderr,
                )
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
    print(
        f"llm-deferred-sweep: {len(seen_pending)} pending seen, {completed} completed, "
        f"{still_pending} still pending observations, {failed} failed "
        f"({recovered_terminal_failures} terminally recovered), {pruned} pruned, "
        f"{remaining} remaining"
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
