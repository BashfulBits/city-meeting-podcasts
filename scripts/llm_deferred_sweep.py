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
finer-grained wake-up than that, and GitHub Actions cron minutes are worth conserving.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from citypods.compute.llm import LiteLLMBackend, LLMBackendConfig
from citypods.compute.llm_deferred import list_pending_deferred, prune_expired_deferred
from citypods.config import load_site_config
from citypods.storage import make_storage


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

    pending = list_pending_deferred(storage)
    completed = 0
    still_pending = 0
    failed = 0
    for handle in pending:
        try:
            result = backend.reconcile(handle)
        except Exception as exc:  # noqa: BLE001 -- one bad record must not abort the sweep
            failed += 1
            print(f"llm-deferred-sweep: {handle.recipe_hash} failed: {exc}", file=sys.stderr)
            continue
        if result is None:
            still_pending += 1
        else:
            completed += 1

    pruned = prune_expired_deferred(storage)
    print(
        f"llm-deferred-sweep: {len(pending)} pending, {completed} completed, "
        f"{still_pending} still pending, {failed} failed, {pruned} pruned"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
