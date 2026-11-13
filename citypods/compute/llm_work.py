"""Purpose-bound, run-local LLM work census and job telemetry (review/52).

Only aggregates enter durable run events. Work identities, recipes and payloads stay in memory.
A backend response is not consumed work: the producer must finalize its own result first.
"""

from __future__ import annotations

import threading
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from citypods.compute.base import JobHandle, JobResult
from citypods.compute.llm_lanes import load_lanes

SCHEMA_VERSION = 1
WORKING = frozenset({"ready", "queued", "ingress_limited", "stopped"})
HELD = frozenset({"blocked", "policy_held", "errored"})
_STATES = WORKING | HELD | {"consumed", "reused"}
# Ingress dispositions kept apart from the latest per-recipe status: the flush that follows an
# admission re-reports the same job as ``queued``, which would otherwise erase admitted vs replayed.
_ADMISSION = frozenset(
    {
        "fresh_admitted",
        "replayed",
        "deferred",
        "client_daily_cap",
        "deferred_retry",
        "rejected",
        "prior_pending",
        "cached_completed",
    }
)
# What a Worker poll said about a handle. Producers normally enqueue and exit without polling, so
# most queued work has no poll observation; those items are reported as ``unobserved``.
_POLLED = frozenset({"waiting", "completed", "failed"})
_BINDING = "_llm_work"
_ACTIVE: ContextVar[tuple[Any, str] | None] = ContextVar("llm_work_producer", default=None)


@dataclass
class _Item:
    state: str = "ready"
    consumed: bool = False
    jobs: dict[str, str] = field(default_factory=dict)
    first_seen: str = ""
    admission: dict[str, str] = field(default_factory=dict)
    polled: dict[str, str] = field(default_factory=dict)
    # A producer-defined label for why the item ended where it did (review/58 §12.3), counted per
    # purpose in the snapshot. One label per item, so re-visits in the same run never double-count.
    outcome: str = ""


class LLMWorkTracker:
    """A thread-safe census shared by all producers in one logical run.

    Register the producer's work identity before admission gates. Several recipe-keyed requests
    may belong to one work item. Re-visiting that item after batch flush replaces its disposition
    instead of adding a second backlog/completion. Each run emits a fresh snapshot, not deltas.
    """

    def __init__(self, lanes=None):
        self._lanes = lanes
        self._items: dict[str, dict[str, _Item]] = {}
        self._partial: set[str] = set()
        self._complete = False
        self._packets: dict[str, dict[str, int]] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @property
    def lanes(self):
        if self._lanes is None:
            self._lanes = load_lanes()
        return self._lanes

    def is_producer(self, producer: str) -> bool:
        return any(lane.telemetry_producer == producer for lane in self.lanes.values())

    @contextmanager
    def producer(self, producer: str):
        token = _ACTIVE.set((self, producer))
        try:
            yield
        except BaseException:
            self.partial(producer)
            raise
        finally:
            _ACTIVE.reset(token)

    def partial(self, producer: str) -> None:
        with self._lock:
            self._partial.add(producer)

    def finish(self) -> None:
        """Declare traversal/persistence complete; failed producers remain partial."""
        with self._lock:
            self._complete = True

    def activate(self, purpose: str, *, producer: str) -> None:
        lane = self.lanes.get(purpose)
        if lane is None or lane.telemetry_producer != producer:
            raise ValueError(f"Invalid LLM producer registration: {producer}/{purpose}")
        with self._lock:
            self._items.setdefault(purpose, {})

    def defer_remaining(self, purpose: str, state: str) -> None:
        if state not in WORKING | HELD:
            raise ValueError(f"Invalid LLM work deferral state: {state}")
        with self._lock:
            for item in self._items.get(purpose, {}).values():
                if item.state == "ready":
                    item.state = state
                    item.consumed = False

    def item(self, purpose: str, identity: str, *, producer: str, state: str = "ready") -> WorkItem:
        lane = self.lanes.get(purpose)
        if lane is None:
            raise ValueError(f"Unregistered LLM telemetry purpose: {purpose}")
        if lane.telemetry_producer != producer:
            raise ValueError(
                f"LLM purpose {purpose} belongs to {lane.telemetry_producer}, not {producer}"
            )
        if not identity:
            raise ValueError("LLM work requires a stable producer work identity")
        if state not in _STATES:
            raise ValueError(f"Invalid initial LLM work state: {state}")
        with self._lock:
            self._items.setdefault(purpose, {}).setdefault(
                str(identity), _Item(state=state, first_seen=self._now())
            )
        return WorkItem(self, purpose, str(identity))

    def packet_counts(self, purpose: str, counts: dict[str, int]) -> None:
        """Record packet-level counts a producer already computed (judge lanes, remedy:p3).

        Only non-negative integers are kept; the dict replaces any earlier report for the purpose.
        """
        if purpose not in self.lanes:
            raise ValueError(f"Unregistered LLM telemetry purpose: {purpose}")
        clean = {
            str(key): int(value)
            for key, value in counts.items()
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        }
        with self._lock:
            self._packets[purpose] = dict(sorted(clean.items()))

    @staticmethod
    def _queue_split(items) -> dict[str, int]:
        """Split queued items by what a Worker poll observed; no poll means ``unobserved``."""
        split = {"waiting_at_worker": 0, "completed_uncollected": 0, "failed_at_worker": 0}
        unobserved = 0
        for item in items:
            if item.state != "queued":
                continue
            seen = set(item.polled.values())
            if "completed" in seen:
                split["completed_uncollected"] += 1
            elif "failed" in seen:
                split["failed_at_worker"] += 1
            elif "waiting" in seen:
                split["waiting_at_worker"] += 1
            else:
                unobserved += 1
        split["unobserved"] = unobserved
        return split

    @staticmethod
    def _admission_counts(items) -> dict[str, int]:
        counts = Counter(status for item in items for status in item.admission.values())
        return {
            "admitted": counts["fresh_admitted"],
            "replayed": counts["replayed"],
            "deferred": counts["deferred"] + counts["client_daily_cap"] + counts["deferred_retry"],
            "rejected": counts["rejected"],
            "prior_pending": counts["prior_pending"],
            "cached_completed": counts["cached_completed"],
        }

    def snapshot(self) -> dict:
        with self._lock:
            purposes = {}
            for purpose, items in sorted(self._items.items()):
                lane = self.lanes[purpose]
                states = Counter(item.state for item in items.values())
                jobs = Counter(status for item in items.values() for status in item.jobs.values())
                purposes[purpose] = {
                    "producer": lane.telemetry_producer,
                    "unit": lane.telemetry_unit,
                    "completion": lane.telemetry_completion,
                    "coverage": (
                        "complete"
                        if self._complete and lane.telemetry_producer not in self._partial
                        else "partial"
                    ),
                    "scope": lane.telemetry_scope,
                    "observed": len(items),
                    "states": dict(sorted(states.items())),
                    "backlog": sum(states[state] for state in WORKING),
                    "consumed": sum(item.consumed for item in items.values()),
                    "job_outcomes": dict(sorted(jobs.items())),
                    # Additive (funnel telemetry, review/58 section 9). Run-local timestamps:
                    # nothing durable records when an item first became eligible, so readers
                    # extend these across runs rather than trusting one event.
                    "first_seen_at": min((i.first_seen for i in items.values()), default=None),
                    "oldest_eligible_at": min(
                        (i.first_seen for i in items.values() if i.state in WORKING),
                        default=None,
                    ),
                    "queue": self._queue_split(items.values()),
                    "admission": self._admission_counts(items.values()),
                    "jobs": sum(len(item.jobs) for item in items.values()),
                }
                if purpose in self._packets:
                    purposes[purpose]["packets"] = dict(self._packets[purpose])
                outcomes = Counter(item.outcome for item in items.values() if item.outcome)
                if outcomes:
                    purposes[purpose]["outcomes"] = dict(sorted(outcomes.items()))
            return {"schema_version": SCHEMA_VERSION, "purposes": purposes}


@dataclass(frozen=True)
class WorkItem:
    tracker: LLMWorkTracker
    purpose: str
    identity: str

    def _item(self) -> _Item:
        return self.tracker._items[self.purpose][self.identity]

    def has_prepared_jobs(self) -> bool:
        """Whether this item's batch collector prepared any new work in this pass."""
        with self.tracker._lock:
            return "prepared" in self._item().jobs.values()

    def defer(self, state: str) -> None:
        if state not in WORKING | HELD:
            raise ValueError(f"Invalid LLM work deferral state: {state}")
        with self.tracker._lock:
            item = self._item()
            if item.state in {"consumed", "reused"}:
                return
            item.state = state
            item.consumed = False

    def consumed(self, *, reused: bool = False) -> None:
        with self.tracker._lock:
            item = self._item()
            # A cached re-visit cannot erase consumption already completed in this run.
            item.consumed = item.consumed or not reused
            item.state = "consumed" if item.consumed else "reused"

    def outcome(self, label: str) -> None:
        """Label why this item ended where it did; the last label in a run wins."""
        if not label:
            raise ValueError("LLM work outcome label must be non-empty")
        with self.tracker._lock:
            self._item().outcome = label

    def job_outcome(self, recipe: str, status: str, reason: str | None = None) -> None:
        with self.tracker._lock:
            item = self._item()
            if recipe not in item.jobs:
                item.polled.clear()  # a new recipe supersedes earlier poll observations
            if status in _ADMISSION:
                item.admission[recipe] = status
            item.jobs[recipe] = status
            if item.state in {"consumed", "reused"}:
                return
            if status in {"fresh_admitted", "replayed", "prior_pending", "queued"}:
                item.state = "queued"
            elif status in {"deferred", "client_daily_cap", "deferred_retry"}:
                item.state = "ingress_limited"
            elif status in {"rejected", "errored"}:
                item.state = "errored"

    def poll_outcome(self, recipe: str, observed: str) -> None:
        """Record what a Worker poll said about one of this item's handles."""
        if observed not in _POLLED:
            raise ValueError(f"Invalid LLM poll observation: {observed}")
        with self.tracker._lock:
            self._item().polled[recipe] = observed

    def bind(self, job):
        policy = job.inputs.get("llm_policy")
        purpose = getattr(policy, "purpose", None)
        if purpose != self.purpose:
            raise ValueError(
                f"Job purpose {purpose!r} does not match work purpose {self.purpose!r}"
            )
        existing = job.inputs.get(_BINDING)
        if existing is not None and existing != self:
            raise ValueError("Job is already bound to a different LLM work item")
        return replace(job, inputs={**job.inputs, _BINDING: self})

    def backend(self, backend):
        return WorkBackend(backend, self)

    def result(self, job, result) -> None:
        if isinstance(result, Exception):
            self.job_outcome(job.recipe_hash, "errored")
        elif isinstance(result, JobHandle):
            if result.ref.startswith("batch-pending:"):
                self.job_outcome(job.recipe_hash, "prepared")
            else:
                self.job_outcome(
                    job.recipe_hash, "deferred" if result.deferred_request is not None else "queued"
                )
        elif isinstance(result, JobResult):
            self.job_outcome(job.recipe_hash, "returned")


class WorkBackend:
    """Bind every request from a producer work item, including recursively split requests."""

    def __init__(self, backend, work: WorkItem):
        self._backend = backend
        self.work = work

    def __getattr__(self, name):
        return getattr(self._backend, name)

    def run_inference(self, job):
        job = self.work.bind(job)
        try:
            result = self._backend.run_inference(job)
        except Exception:
            self.work.job_outcome(job.recipe_hash, "errored")
            raise
        self.work.result(job, result)
        return result

    def enqueue_batch(self, jobs):
        bound = [self.work.bind(job) for job in jobs]
        try:
            results = self._backend.enqueue_batch(bound)
        except Exception:
            for job in bound:
                self.work.job_outcome(job.recipe_hash, "errored")
            raise
        for job, result in zip(bound, results, strict=True):
            self.work.result(job, result)
        return results

    def poll_batch(self, handles):
        results = self._backend.poll_batch(handles)
        for handle in handles:
            if handle.ref not in results:
                continue  # the collector (or a partial response) made no observation
            value = results[handle.ref]
            if value is None:
                self.work.poll_outcome(handle.recipe_hash, "waiting")
            elif isinstance(value, JobResult):
                self.work.poll_outcome(handle.recipe_hash, "completed")
            elif isinstance(value, Exception) and _is_terminal_failure(value):
                self.work.poll_outcome(handle.recipe_hash, "failed")
        return results


def _is_terminal_failure(error: Exception) -> bool:
    """A Worker-recorded terminal failure; lost-handle and transport errors are not one."""
    from citypods.compute.llm import LLMDispatchMissingError, LLMDispatchTerminalError

    return isinstance(error, LLMDispatchTerminalError) and not isinstance(
        error, LLMDispatchMissingError
    )


def validate_work_binding(job) -> WorkItem | None:
    """Reject unbound job submission inside registered producer scopes, before network I/O."""
    binding = job.inputs.get(_BINDING)
    active = _ACTIVE.get()
    if binding is None:
        if active is not None:
            raise ValueError("LLM producer jobs must use a purpose-bound work backend")
        return None
    if not isinstance(binding, WorkItem):
        raise ValueError("Invalid LLM work binding")
    binding.bind(job)
    if active is not None:
        if binding.tracker is not active[0]:
            raise ValueError("LLM job binding belongs to a different run")
        if binding.tracker.lanes[binding.purpose].telemetry_producer != active[1]:
            raise ValueError("LLM job binding belongs to a different producer")
    return binding


def tracked_producer(producer: str):
    """Give research traversals the same enforced submission scope as production stages."""
    from functools import wraps

    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            tracker = kwargs.get("tracker") or LLMWorkTracker()
            kwargs["tracker"] = tracker
            with tracker.producer(producer):
                return function(*args, **kwargs)

        return wrapped

    return decorate


def write_run_event(tracker: LLMWorkTracker, state_dir, producer: str) -> str:
    """Append aggregates after the caller persisted its results; return the sync-relative path."""
    import json
    from datetime import UTC, datetime
    from pathlib import Path
    from uuid import uuid4

    tracker.finish()
    now = datetime.now(UTC)
    relative = f"run_events/{now.strftime('%Y%m%dT%H%M%S')}-{uuid4().hex}.json"
    target = Path(state_dir) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "ts": now.isoformat(),
                "lane": producer,
                "outcome": "completed",
                "stages": {},
                "llm_work": tracker.snapshot(),
            },
            sort_keys=True,
        )
        + "\n"
    )
    return relative
