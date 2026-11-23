"""Semi-realtime queued calls on the interactive lane (#2249)."""

from __future__ import annotations

import pytest

from citypods.compute.base import InferenceJob, JobHandle, JobResult
from citypods.compute.interactive import (
    INTERACTIVE_PURPOSE,
    InteractiveTimeout,
    interactive_policy,
    submit_and_wait,
)
from citypods.compute.llm import LLMBackendError
from citypods.compute.llm_lanes import NON_DISPATCHING_PURPOSES, lane_for, load_lanes
from citypods.discovery.config import discovery_allowed_models

JOB = InferenceJob(task="tag", inputs={}, recipe_hash="r")
HANDLE = JobHandle(task="tag", recipe_hash="r", backend="litellm", ref="ref-1")
RESULT = JobResult(task="tag", recipe_hash="r", output={"ok": True})


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeBackend:
    def __init__(self, enqueued, polls=()):
        self.enqueued = enqueued
        self.polls = list(polls)
        self.poll_count = 0

    def enqueue_batch(self, jobs):
        return [self.enqueued]

    def poll_batch(self, handles):
        self.poll_count += 1
        value = self.polls.pop(0) if self.polls else None
        return {handles[0].ref: value}


def run(backend, **kw):
    clock = Clock()
    return submit_and_wait(backend, JOB, sleep=clock.sleep, clock=clock, **kw)


def test_policy_is_queued_priority_zero_and_free():
    policy = interactive_policy(("gemini/gemini-3.6-flash",))
    assert policy.purpose == INTERACTIVE_PURPOSE
    assert policy.queue_only and policy.priority == 0
    assert not policy.require_direct and not policy.allow_paid


def test_a_stored_result_is_returned_without_polling():
    backend = FakeBackend(RESULT)
    assert run(backend) is RESULT
    assert backend.poll_count == 0


def test_polls_until_the_job_completes():
    backend = FakeBackend(HANDLE, polls=[None, None, RESULT])
    assert run(backend) is RESULT
    assert backend.poll_count == 3


def test_a_wait_past_the_budget_times_out_and_leaves_the_job_queued():
    backend = FakeBackend(HANDLE)
    with pytest.raises(InteractiveTimeout):
        run(backend, timeout_seconds=10, poll_seconds=3)
    assert backend.poll_count == 4


def test_a_rejected_submission_raises():
    with pytest.raises(LLMBackendError, match="refused"):
        run(FakeBackend(LLMBackendError("refused")))


def test_a_failed_job_raises():
    with pytest.raises(LLMBackendError, match="failed"):
        run(FakeBackend(HANDLE, polls=[LLMBackendError("failed")]))


def test_the_lane_covers_both_callers_routes():
    from citypods.audit_remedy import REMEDY_MODELS

    assert INTERACTIVE_PURPOSE in load_lanes()
    assert INTERACTIVE_PURPOSE not in NON_DISPATCHING_PURPOSES
    models = set(lane_for(INTERACTIVE_PURPOSE).models)
    assert set(REMEDY_MODELS) <= models
    assert set(discovery_allowed_models("gemini/gemini-3.7-flash")) <= models
