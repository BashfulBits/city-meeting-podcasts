"""Within-run rate pacing for policy-bearing LLM dispatch (throughput update).

Kept free of the ``pydantic``/``instructor`` LLM extras so it runs anywhere: it drives the pacing
loop and its helper through a backend subclass that stubs the single-attempt dispatch, the ledger
eligibility probe, and the clock/sleep, rather than reaching a real provider."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from citypods.compute.base import InferenceJob, JobHandle, JobResult
from citypods.compute.llm import (
    _PACING_POLL_CAP_SECONDS,
    LiteLLMBackend,
    LLMBackendConfig,
    _pacing_wait_seconds,
)
from citypods.compute.llm_policy import ROUTES, LLMRequestPolicy
from citypods.compute.llm_scheduler import SelectionResult

_T0 = datetime(2026, 7, 23, 0, 0, 30, tzinfo=UTC)  # 30s into a minute


def _job():
    return InferenceJob(task="tag", inputs={}, recipe_hash="r1")


def _result():
    return JobResult(task="tag", recipe_hash="r1", output={"choices": []}, model="m")


def _handle():
    return JobHandle(task="tag", recipe_hash="r1", backend="litellm", ref="deferred:r1")


class _PacingBackend(LiteLLMBackend):
    """Stubs everything the pacing loop calls so the loop logic is exercised deterministically."""

    def __init__(self, outcomes, eligibility):
        super().__init__(LLMBackendConfig(model="gemini/gemini-3.1-flash-lite"), storage=object())
        self._outcomes = list(outcomes)  # one JobResult|JobHandle per attempt
        self._eligibility = list(eligibility)  # one (available_now, retry_at) per deferral
        self.attempts = 0
        self.slept: list[float] = []
        self._clock = _T0

    def _run_policy_job(self, job, policy, structured, messages):
        self.attempts += 1
        return self._outcomes.pop(0)

    def _next_dispatch_eligibility(self, job, policy, structured, messages):
        available_now, retry_at = self._eligibility.pop(0)
        model = "gemini/gemini-3.1-flash-lite" if available_now else None
        return SelectionResult(model, None, "stub", (), retry_at=retry_at)

    def _sleep(self, seconds):
        self.slept.append(seconds)
        self._clock += timedelta(seconds=max(seconds, 0.0))

    def _now(self):
        return self._clock


# ---- _pacing_wait_seconds ------------------------------------------------------------------


def test_pacing_wait_none_when_nothing_frees_up():
    assert _pacing_wait_seconds(None, _T0 + timedelta(minutes=10), _T0) is None


def test_pacing_wait_none_when_reset_after_deadline():
    # A daily reset hours away, deadline ~10 min away -> give up (defer to sweep / next run).
    daily = _T0 + timedelta(hours=8)
    assert _pacing_wait_seconds(daily, _T0 + timedelta(minutes=10), _T0) is None


def test_pacing_wait_is_time_to_reset_when_before_deadline_and_within_cap():
    # Next window is 5s away (under the poll cap), deadline 10 min away -> wait exactly 5s.
    reset = _T0 + timedelta(seconds=5)
    assert _pacing_wait_seconds(reset, _T0 + timedelta(minutes=10), _T0) == 5.0


def test_pacing_wait_capped_for_responsiveness():
    reset = _T0 + timedelta(seconds=55)
    assert _pacing_wait_seconds(reset, None, _T0) == _PACING_POLL_CAP_SECONDS


def test_pacing_wait_zero_when_already_due():
    assert _pacing_wait_seconds(_T0 - timedelta(seconds=5), None, _T0) == 0.0


# ---- the paced loop ------------------------------------------------------------------------


def test_no_deadline_is_a_single_attempt_then_defer():
    """Without a deadline there's nothing to pace against: one attempt, return the deferred handle
    exactly like the pre-pacing path."""
    backend = _PacingBackend(outcomes=[_handle()], eligibility=[])
    policy = LLMRequestPolicy(allowed_models=("gemini/gemini-3.1-flash-lite",))  # deadline_at=None
    result = backend._run_policy_job_paced(_job(), policy, None, [{"role": "user", "content": "x"}])
    assert isinstance(result, JobHandle)
    assert backend.attempts == 1
    assert backend.slept == []


def test_waits_out_a_full_minute_window_then_resolves():
    """The core throughput behavior: an attempt deferred by a full per-minute window keeps waiting
    (in poll-capped steps) until the window rolls, then retries and resolves -- draining quota
    across minutes instead of stopping after the first burst."""
    reset = _T0 + timedelta(seconds=30)  # next minute boundary, 30s out

    class _ClockDriven(_PacingBackend):
        def _run_policy_job(self, job, policy, structured, messages):
            self.attempts += 1
            return _result() if self._clock >= reset else _handle()

        def _next_dispatch_eligibility(self, job, policy, structured, messages):
            available = self._clock >= reset
            model = "gemini/gemini-3.1-flash-lite" if available else None
            return SelectionResult(model, None, "stub", (), retry_at=None if available else reset)

    backend = _ClockDriven(outcomes=[], eligibility=[])
    policy = LLMRequestPolicy(
        allowed_models=("gemini/gemini-3.1-flash-lite",),
        deadline_at=_T0 + timedelta(minutes=10),
    )
    result = backend._run_policy_job_paced(_job(), policy, None, [{"role": "user", "content": "x"}])
    assert isinstance(result, JobResult)
    # Slept in 10s poll-capped steps across the 30s window (never one blind 30s sleep), then the
    # retry after the rollover resolved.
    assert sum(backend.slept) == 30.0
    assert all(s <= _PACING_POLL_CAP_SECONDS for s in backend.slept)
    assert backend.attempts >= 2


def test_gives_up_when_only_a_daily_reset_remains():
    """When both routes' per-minute AND daily windows are spent (next reset is tomorrow, past the
    run's deadline), the loop returns the deferred handle for the sweep / next run rather than
    sleeping for hours."""
    daily = _T0 + timedelta(hours=8)
    backend = _PacingBackend(outcomes=[_handle()], eligibility=[(False, daily)])
    policy = LLMRequestPolicy(
        allowed_models=("gemini/gemini-3.1-flash-lite",),
        deadline_at=_T0 + timedelta(minutes=10),
    )
    result = backend._run_policy_job_paced(_job(), policy, None, [{"role": "user", "content": "x"}])
    assert isinstance(result, JobHandle)
    assert backend.attempts == 1
    assert backend.slept == []  # never waited for a reset it can't reach in time


def test_retries_immediately_when_a_route_freed_between_checks():
    """If a route became available between the deferral and the eligibility probe (the minute
    rolled), retry right away with only the tiny anti-hot-spin floor."""
    backend = _PacingBackend(
        outcomes=[_handle(), _result()],
        eligibility=[(True, None)],  # available now
    )
    policy = LLMRequestPolicy(
        allowed_models=("gemini/gemini-3.1-flash-lite",),
        deadline_at=_T0 + timedelta(minutes=10),
    )
    result = backend._run_policy_job_paced(_job(), policy, None, [{"role": "user", "content": "x"}])
    assert isinstance(result, JobResult)
    assert backend.attempts == 2
    assert len(backend.slept) == 1 and backend.slept[0] > 0  # only the small floor


# ---- route table -----------------------------------------------------------------------------


def test_both_flash_lite_routes_present_with_real_free_tier_quotas():
    for model in ("gemini/gemini-3.1-flash-lite", "gemini/gemini-3.5-flash-lite"):
        route = ROUTES[model]
        assert route.free and route.transport == "direct"
        assert route.quota.rpm == 7
        assert route.quota.rpd == 250
        assert route.quota.tpm == 112_500
        assert route.quota.reset_timezone == "America/Los_Angeles"
