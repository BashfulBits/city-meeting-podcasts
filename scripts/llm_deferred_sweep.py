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
import signal
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from citypods.compute.llm import LiteLLMBackend, LLMBackendConfig
from citypods.compute.llm_deferred import list_pending_deferred, prune_expired_deferred
from citypods.compute.llm_policy import DeferredLLMRequest
from citypods.config import load_site_config
from citypods.storage import make_storage


class _StopRequested(Exception):
    """Raised by signal handlers so the sweep can summarize and exit before a hard kill."""


def _install_signal_handlers() -> None:
    def _request_stop(_signum, _frame):
        raise _StopRequested

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)


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
    return replace(
        handle,
        deferred_request=DeferredLLMRequest(
            messages=deferred.messages,
            policy=replace(deferred.policy, deadline_at=deadline_at),
        ),
    )


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
        from citypods.tags import ensure_llm_contract

        ensure_llm_contract()
    except ImportError:
        pass
    try:
        import citypods.discovery.classify  # noqa: F401 -- registers on import, side effect only
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
    backend = LiteLLMBackend(LLMBackendConfig.from_env(), storage=storage)
    _register_known_contracts()

    _install_signal_handlers()
    deadline_at = datetime.now(UTC) + timedelta(minutes=args.run_time_budget_minutes)
    completed = 0
    still_pending = 0
    failed = 0
    seen_pending: set[str] = set()
    completed_hashes: set[str] = set()
    stop_requested = False

    while datetime.now(UTC) < deadline_at:
        pending = list_pending_deferred(storage)
        if not pending:
            break
        pass_completed = 0
        for handle in pending:
            if datetime.now(UTC) >= deadline_at:
                stop_requested = True
                break
            if handle.recipe_hash in completed_hashes:
                continue
            seen_pending.add(handle.recipe_hash)
            try:
                result = backend.reconcile(_with_sweep_deadline(handle, deadline_at))
            except _StopRequested:
                stop_requested = True
                break
            except Exception as exc:  # noqa: BLE001 -- one bad record must not abort the sweep
                failed += 1
                print(f"llm-deferred-sweep: {handle.recipe_hash} failed: {exc}", file=sys.stderr)
                continue
            if result is None:
                still_pending += 1
            else:
                completed += 1
                pass_completed += 1
                completed_hashes.add(handle.recipe_hash)

        if stop_requested:
            break
        # Each handle in the snapshot has now either completed, remained genuinely pending, failed
        # independently, or paced until no allowed route could fit before the sweep deadline. A
        # second immediate pass would only re-poll/re-log the same remaining handles; the next
        # scheduled run gets a fresh registry snapshot.
        break

    pruned = prune_expired_deferred(storage)
    remaining = len(list_pending_deferred(storage))
    print(
        f"llm-deferred-sweep: {len(seen_pending)} pending seen, {completed} completed, "
        f"{still_pending} still pending observations, {failed} failed, {pruned} pruned, "
        f"{remaining} remaining"
    )
    if stop_requested:
        print("llm-deferred-sweep: graceful stop requested; finished work has been persisted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
