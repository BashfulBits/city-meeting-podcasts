"""Semi-realtime LLM calls through the Worker queue (review/54 section 8.15.G, #2249).

City onboarding's classifier and the unexpected-body remedy classifier used to call Gemini
directly, so the quota they spent was invisible to the Worker's ledger and could collide with the
queued lanes' use of the same routes. They now submit to one lane, ``interactive-classify``, at
priority 0 and wait for the answer in the same process.

The wait is a minute or two, not seconds: the Worker claims queued jobs on a one-minute cron, and
priority only orders jobs within a claim (measured: about 60 s at priority 0 and 1 alike). That
is fine for a caller that makes one or a few calls and may take 10 to 30 minutes, which is what
these two are. A caller that makes dozens of sequential calls must not use this.
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Callable

from citypods.compute.base import InferenceJob, JobHandle, JobResult
from citypods.compute.llm import LiteLLMBackend, LLMBackendError, LLMDispatchTerminalError
from citypods.compute.llm_policy import LLMRequestPolicy

INTERACTIVE_PURPOSE = "interactive-classify"
DEFAULT_POLL_SECONDS = 3.0
DEFAULT_WAIT_SECONDS = 600.0


class InteractiveTimeout(LLMBackendError):
    """The queued job did not finish within the wait; it stays queued and a retry can pick it up."""


def interactive_policy(
    allowed_models: tuple[str, ...],
    *,
    timeout_class: str = "fast",
) -> LLMRequestPolicy:
    """Policy for a queued, priority-0, free-route-only call on the interactive lane.

    There is deliberately no ``deadline_at``: it would enter the request digest, so a retry with
    the same recipe but a later deadline would look like a different request under the same
    idempotency key. The caller bounds its own wait with ``submit_and_wait(timeout_seconds=...)``.
    """
    return LLMRequestPolicy(
        allowed_models=allowed_models,
        allow_paid=False,
        purpose=INTERACTIVE_PURPOSE,
        queue_only=True,
        timeout_class=timeout_class,  # type: ignore[arg-type]
        priority=0,
    )


def submit_and_wait(
    backend: LiteLLMBackend,
    job: InferenceJob,
    *,
    timeout_seconds: float = DEFAULT_WAIT_SECONDS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    terminal_retries: int = 8,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> JobResult:
    """Enqueue one job and poll until it completes.

    Raises the submission or job error as is, and :class:`InteractiveTimeout` when the wait runs
    out. The job is not cancelled on timeout: its recipe hash is stable, so the next run finds the
    pending or completed record instead of paying for the call again.

    The Worker keeps a job that failed permanently under its recipe hash and replays that failure
    to every later submission of the same recipe. An interactive caller is a person re-running a
    command after a transient cause (a full route, a dead model), so a permanent failure moves on
    to the next retry hash (``r-retry-1``, ``r-retry-2``, ...) until one is not a replayed failure.
    Walking in order reuses a pending or completed attempt instead of paying for it again, and
    skips the failed ones at the cost of one cheap replay each, so a run after any number of
    earlier failures still reaches a fresh attempt. ``terminal_retries`` bounds one call's walk.
    """
    started = clock()
    attempt = 0
    while True:
        attempt_job = job
        if attempt:
            attempt_job = dataclasses.replace(job, recipe_hash=f"{job.recipe_hash}-retry-{attempt}")
        try:
            return _wait_for(
                backend, attempt_job, started, timeout_seconds, poll_seconds, sleep, clock
            )
        except LLMDispatchTerminalError:
            if attempt >= terminal_retries or not job.recipe_hash:
                raise
            attempt += 1


def _wait_for(
    backend: LiteLLMBackend,
    job: InferenceJob,
    started: float,
    timeout_seconds: float,
    poll_seconds: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> JobResult:
    outcome = backend.enqueue_batch([job])[0]
    if isinstance(outcome, Exception):
        raise outcome
    if isinstance(outcome, JobResult):
        return outcome
    if not isinstance(outcome, JobHandle):
        raise LLMBackendError("queued submission returned an unsupported result")
    while True:
        if clock() - started >= timeout_seconds:
            raise InteractiveTimeout(f"queued job not finished after {timeout_seconds:.0f}s")
        sleep(poll_seconds)
        polled = backend.poll_batch([outcome]).get(outcome.ref)
        if isinstance(polled, JobResult):
            return polled
        if isinstance(polled, Exception):
            raise polled
