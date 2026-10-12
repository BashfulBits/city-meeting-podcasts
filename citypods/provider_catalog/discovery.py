"""Bounded offline-first request-cap discovery; no configuration or provider-name branches."""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from citypods.provider_catalog.evidence import digest

AXES = ("input", "output", "total_input_heavy", "total_output_heavy")
MAX_INPUT, MAX_OUTPUT = 524_288, 131_072
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_NO_RESPONSE_CALLS = 5
NO_RESPONSE_DIAGNOSTICS = {
    "transport_timeout",
    "transport_connection_error",
    "transport_request_error",
    "worker_response_timeout",
    "worker_transport_error",
}
OUTCOMES = {"admitted", "refused", "lower_only", "quota", "daily", "outage", "inconclusive"}


def positive(value):
    return type(value) is int and 0 < value <= 2**53 - 1


def discovery_identity(provider, account_id, upstream_model, endpoint):
    """Exact physical identity; parser/fixture versions are separate compatibility checks."""
    if not all(
        isinstance(value, str) and value
        for value in (
            provider,
            account_id,
            upstream_model,
            endpoint,
        )
    ):
        raise ValueError("invalid physical discovery identity")
    return digest([provider, account_id, upstream_model, endpoint])


@dataclass(frozen=True)
class DiscoveryRequest:
    session_id: str
    attempt_id: str
    identity: str
    axis: str
    target: int
    input_tokens: int
    output_tokens: int
    control: str
    parser_version: str
    fixture_version: str = "discovery-v1"

    def __post_init__(self):
        if (
            self.axis not in AXES
            or not all(positive(v) for v in (self.target, self.input_tokens, self.output_tokens))
            or self.input_tokens > MAX_INPUT
            or self.output_tokens > MAX_OUTPUT
            or not all(
                isinstance(v, str) and v
                for v in (
                    self.session_id,
                    self.attempt_id,
                    self.identity,
                    self.control,
                    self.parser_version,
                    self.fixture_version,
                )
            )
        ):
            raise ValueError("invalid discovery request")


@dataclass(frozen=True)
class DiscoveryAdmission:
    session_id: str
    attempt_id: str
    identity: str
    input_tokens: int
    output_tokens: int
    deadline: float

    def matches(self, request):
        return all(
            getattr(self, key) == getattr(request, key)
            for key in (
                "session_id",
                "attempt_id",
                "identity",
                "input_tokens",
                "output_tokens",
            )
        )


@dataclass(frozen=True)
class DiscoveryObservation:
    request: DiscoveryRequest
    observed_at: str
    outcome: str = "inconclusive"
    coordinate: int | None = None
    count_basis: str = "unknown"
    reported_input: int | None = None
    reported_output: int | None = None
    reasoning_tokens: int | None = None
    cached_tokens: int | None = None
    finish_reason: str | None = None
    verified: bool = False
    diagnostic: str = "unsupported_endpoint"
    retry_after_seconds: int | None = None

    def __post_init__(self):
        if (
            datetime.fromisoformat(self.observed_at).tzinfo is None
            or self.outcome not in OUTCOMES
            or type(self.verified) is not bool
        ):
            raise ValueError("invalid discovery observation")
        for value in (
            self.coordinate,
            self.reported_input,
            self.reported_output,
            self.reasoning_tokens,
            self.cached_tokens,
            self.retry_after_seconds,
        ):
            if value is not None and (type(value) is not int or not 0 <= value <= 2**53 - 1):
                raise ValueError("invalid discovery count")
        if self.outcome in {"admitted", "refused"} and (
            not self.verified or not positive(self.coordinate) or self.count_basis == "unknown"
        ):
            raise ValueError("boundary lacks verified comparable count")
        if (
            self.outcome == "admitted"
            and self.request.axis == "output"
            and (self.finish_reason != "length" or self.coordinate != self.reported_output)
        ):
            raise ValueError("output boundary lacks generated length-limited count")

    @classmethod
    def from_dict(cls, value):
        row = dict(value)
        row["request"] = DiscoveryRequest(**row["request"])
        return cls(**row)


@dataclass
class Bracket:
    axis: str
    seed: int
    lower: int | None = None
    upper: int | None = None
    basis: str | None = None
    control: str | None = None
    nonprogress: int = 0
    reason: str | None = None
    history: list = field(default_factory=list)

    @property
    def converged(self):
        return bool(
            self.lower is not None
            and self.upper is not None
            and 0 < self.upper - self.lower <= max(1, math.ceil(self.lower / 200))
        )

    def target(self):
        if self.lower is not None and self.upper is not None:
            return self.lower + (self.upper - self.lower) // 2
        if self.upper is not None:
            return max(1, self.upper // 2)
        return math.ceil(self.lower * 3 / 2) if self.lower else self.seed

    def observe(self, observation):
        if observation.request.axis != self.axis:
            raise ValueError("observation belongs to another axis")
        self.history.append(observation)
        if observation.outcome not in {"admitted", "refused"}:
            self.nonprogress += 1
            return
        allowed = (
            {"input", "prompt_tokens"}
            if self.axis == "input"
            else {"output", "completion_including_reasoning", "generated_output"}
            if self.axis == "output"
            else {"prompt_plus_requested_output", "prompt_plus_generated_output"}
        )
        if observation.count_basis not in allowed:
            self.nonprogress += 1
            self.reason = "conditional_or_unverified_count_basis"
            return
        key = (observation.count_basis, observation.request.control)
        if self.basis is not None and key != (self.basis, self.control):
            self.nonprogress += 1
            self.reason = "incomparable_count_or_control"
            return
        self.basis, self.control = key
        count = observation.coordinate
        before = (self.lower, self.upper)
        if (
            observation.outcome == "admitted" and self.upper is not None and count >= self.upper
        ) or (observation.outcome == "refused" and self.lower is not None and count <= self.lower):
            self.lower = self.upper = None
            self.reason = "contradictory_boundary_rebaseline"
        if observation.outcome == "admitted":
            self.lower = max(self.lower or 0, count)
        else:
            self.upper = min(self.upper or count, count)
        self.nonprogress = self.nonprogress + 1 if before == (self.lower, self.upper) else 0

    def summary(self):
        return {
            "axis": self.axis,
            "admitted": self.lower,
            "refused": self.upper,
            "count_basis": self.basis,
            "control": self.control,
            "target_width": max(1, math.ceil(self.lower / 200)) if self.lower else None,
            "status": "converged" if self.converged else "partial",
            "reason": self.reason,
        }


@dataclass
class DiscoveryBudget:
    max_calls: int = 64
    max_input: int = 2_097_152
    max_output: int = 524_288
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def charge(self, request):
        if (
            self.calls + 1 > self.max_calls
            or self.input_tokens + request.input_tokens > self.max_input
            or self.output_tokens + request.output_tokens > self.max_output
        ):
            raise ValueError("discovery_budget_exhausted")
        self.calls += 1
        self.input_tokens += request.input_tokens
        self.output_tokens += request.output_tokens

    def settle(self, observation):
        self.input_tokens += max(
            0, (observation.reported_input or 0) - observation.request.input_tokens
        )
        self.output_tokens += max(
            0, (observation.reported_output or 0) - observation.request.output_tokens
        )

    @property
    def overrun(self):
        return self.input_tokens > self.max_input or self.output_tokens > self.max_output


class DiscoveryJournal:
    """Atomic fsynced payload-free checkpoints, never refunding unknown charged attempts."""

    def __init__(self, path: Path, session: Mapping):
        self.path = path
        self.data = {
            "session": dict(session),
            "attempts": [],
            "reason": None,
            "started_at": datetime.now(UTC).isoformat(),
        }
        if path.exists():
            if path.stat().st_size > MAX_ARTIFACT_BYTES:
                raise ValueError("oversized discovery checkpoint")
            previous = json.loads(path.read_text())
            if previous["session"] != self.data["session"]:
                raise ValueError("checkpoint session identity mismatch")
            self.data = previous

    def save(self):
        encoded = json.dumps(self.data, sort_keys=True).encode()
        if len(encoded) > MAX_ARTIFACT_BYTES:
            raise ValueError("oversized discovery checkpoint")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            descriptor = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def charge(self, request):
        if any(row["request"]["attempt_id"] == request.attempt_id for row in self.data["attempts"]):
            raise ValueError("discovery_attempt_already_charged")
        self.data["attempts"].append({"request": asdict(request), "observation": None})
        self.save()

    def complete(self, observation):
        row = next(
            row
            for row in self.data["attempts"]
            if row["request"]["attempt_id"] == observation.request.attempt_id
        )
        if row["request"] != asdict(observation.request) or row["observation"] is not None:
            raise ValueError("discovery_result_identity_mismatch")
        row["observation"] = asdict(observation)
        self.save()


@dataclass(frozen=True)
class DiscoveryResult:
    session_id: str
    identity: str
    observations: tuple
    brackets: tuple
    budget: dict
    reason: str
    transport_retries: tuple = ()


def run_discovery(
    *,
    session_id,
    identity,
    parser_version,
    supported_axes,
    measure,
    renew,
    journal,
    seeds=None,
    history=(),
    budget=None,
    shared_budget=None,
    clock=time.monotonic,
    sleep=time.sleep,
    deadline=None,
):
    """Inject transport, pause renewal and time; no provider calls without a charged receipt."""
    budget = budget or DiscoveryBudget()
    for selected, multiplier in ((budget, 1), (shared_budget, 2)):
        if selected and (
            selected.max_calls > 64 * multiplier
            or selected.max_input > 2_097_152 * multiplier
            or selected.max_output > 524_288 * multiplier
        ):
            raise ValueError("discovery envelope exceeds reviewed ceiling")
    started = datetime.fromisoformat(journal.data["started_at"])
    if started.tzinfo is None or started > datetime.now(UTC):
        raise ValueError("invalid discovery session start")
    elapsed = (datetime.now(UTC) - started).total_seconds()
    deadline = min(
        deadline if deadline is not None else float("inf"), clock() + max(0, 1200 - elapsed)
    )
    observations, brackets = [], []
    transport_retries = []
    reason = "complete"
    seeds = seeds or {}
    restored = []
    for row in journal.data["attempts"]:
        request = DiscoveryRequest(**row["request"])
        if request.session_id != session_id or request.identity != identity:
            raise ValueError("checkpoint attempt identity mismatch")
        item = DiscoveryObservation.from_dict(row["observation"]) if row["observation"] else None
        if item is not None and item.request != request:
            raise ValueError("checkpoint observation identity mismatch")
        restored.append((request, item))
    for request, item in restored:
        budget.charge(request)
        if shared_budget:
            shared_budget.charge(request)
        if item is None:
            return DiscoveryResult(
                session_id, identity, (), (), asdict(budget), "indeterminate_attempt_new_receipt"
            )
        observations.append(item)
        budget.settle(item)
        if shared_budget:
            shared_budget.settle(item)
    for axis in AXES:
        bracket = Bracket(axis, int(seeds.get(axis, 256 if axis == "output" else 2048)))
        brackets.append(bracket)
        if axis not in supported_axes:
            bracket.reason = "unsupported_endpoint_axis"
            continue
        previous = [
            item
            for item in history
            if item.request.identity == identity
            and item.request.axis == axis
            and item.request.parser_version == parser_version
            and item.request.fixture_version == "discovery-v1"
            and item.outcome in {"admitted", "refused"}
            and item.verified
        ]
        previous.sort(key=lambda item: datetime.fromisoformat(item.observed_at))
        counterpart = 256 if axis in {"input", "total_input_heavy"} else 2048
        control = f"{axis}:v1"
        compatible = previous + [item for item in observations if item.request.axis == axis]
        if compatible:
            latest = sorted(compatible, key=lambda item: datetime.fromisoformat(item.observed_at))[
                -1
            ]
            prefix = f"{axis}:v1:opposite="
            if latest.request.control.startswith(prefix):
                value = latest.request.control.removeprefix(prefix)
                if value.isascii() and value.isdigit() and 0 < int(value) <= counterpart:
                    counterpart = int(value)
                    control = latest.request.control
        previous = [item for item in previous if item.request.control == control]
        pending = Bracket(axis, bracket.seed)
        for item in previous:
            pending.observe(item)
        for item in observations:
            if item.request.axis == axis and item.request.control == control:
                bracket.observe(item)
        fresh_successes, quota_retry = 0, False
        pending_transport = []
        while not bracket.converged and bracket.nonprogress < 3:
            if clock() >= deadline or budget.overrun or (shared_budget and shared_budget.overrun):
                reason = "deadline_or_reported_usage_overrun"
                break
            target = (
                pending.lower
                if previous and fresh_successes < 2 and pending.lower
                else bracket.target()
            )
            if axis == "input":
                input_tokens, output_tokens = target, counterpart
            elif axis == "output":
                input_tokens, output_tokens = counterpart, target
            elif axis == "total_input_heavy":
                input_tokens, output_tokens = max(1, target - counterpart), counterpart
            else:
                input_tokens = min(counterpart, max(1, target // 4))
                output_tokens = max(1, target - input_tokens)
            matching = [
                item for item in observations if item.request.axis == axis and item.reported_input
            ]
            if matching and axis != "output":
                last = matching[-1]
                input_tokens = max(
                    1, math.ceil(input_tokens * last.request.input_tokens / last.reported_input)
                )
            if input_tokens > MAX_INPUT or output_tokens > MAX_OUTPUT:
                bracket.reason = "per_call_ceiling"
                break
            ordinal = len(journal.data["attempts"]) + 1
            request = DiscoveryRequest(
                session_id,
                digest([session_id, ordinal]),
                identity,
                axis,
                target,
                input_tokens,
                output_tokens,
                control,
                parser_version,
            )
            charged = False

            def before_call(request=request):
                nonlocal charged
                if charged or clock() >= deadline:
                    raise ValueError("discovery_admission_expired_or_replayed")
                renew()
                if clock() >= deadline:
                    raise ValueError("discovery_admission_expired")
                for selected in (budget, shared_budget):
                    if selected:
                        DiscoveryBudget(**asdict(selected)).charge(request)
                budget.charge(request)
                if shared_budget:
                    shared_budget.charge(request)
                journal.charge(request)
                charged = True
                return DiscoveryAdmission(
                    session_id,
                    request.attempt_id,
                    identity,
                    request.input_tokens,
                    request.output_tokens,
                    deadline,
                )

            try:
                renew()
                observation = measure(request, before_call, min(120, deadline - clock()))
                if not charged or observation.request != request:
                    raise ValueError("discovery_transport_admission_or_identity_mismatch")
                journal.complete(observation)
            except Exception as exc:
                reason = f"measurement_stopped:{type(exc).__name__}"
                bracket.reason = reason
                journal.data["reason"] = reason
                journal.save()
                return DiscoveryResult(
                    session_id,
                    identity,
                    tuple(observations),
                    tuple(b.summary() for b in brackets),
                    asdict(budget),
                    reason,
                    tuple(transport_retries),
                )
            observations.append(observation)
            budget.settle(observation)
            if shared_budget:
                shared_budget.settle(observation)
            if observation.diagnostic in NO_RESPONSE_DIAGNOSTICS:
                pending_transport.append(
                    {
                        "attempt_id": request.attempt_id,
                        "axis": axis,
                        "target": target,
                        "reserved_input": input_tokens,
                        "requested_output": output_tokens,
                        "diagnostic": observation.diagnostic,
                        "outcome": observation.outcome,
                        "observed_at": observation.observed_at,
                    }
                )
                if len(pending_transport) < MAX_NO_RESPONSE_CALLS:
                    continue
                transport_retries.append(tuple(pending_transport))
                reason = bracket.reason = "uncertain_no_response_after_five_calls"
                break
            if pending_transport:
                pending_transport.append(
                    {
                        "attempt_id": request.attempt_id,
                        "axis": axis,
                        "target": target,
                        "reserved_input": input_tokens,
                        "requested_output": output_tokens,
                        "diagnostic": observation.diagnostic,
                        "outcome": observation.outcome,
                        "observed_at": observation.observed_at,
                    }
                )
                transport_retries.append(tuple(pending_transport))
                pending_transport = []
            if observation.outcome in {"outage", "daily"}:
                reason = observation.outcome
                bracket.reason = reason
                break
            if observation.outcome == "quota":
                wait = observation.retry_after_seconds
                if quota_retry or not positive(wait) or clock() + wait >= deadline:
                    reason = bracket.reason = "quota_deferred"
                    break
                quota_retry = True
                until = clock() + wait
                try:
                    while clock() < until:
                        renew()
                        sleep(min(30, until - clock()))
                except Exception:
                    reason = bracket.reason = "pause_lost"
                    break
                continue
            quota_retry = False
            blocking = (
                {"output", "requested_output"}
                if axis in {"input", "total_input_heavy"}
                else {"input", "prompt_tokens"}
            )
            if (
                observation.outcome == "refused"
                and observation.count_basis in blocking
                and counterpart > 1
            ):
                # A typed failure of the fixed counterpart supplies no boundary for this axis.
                # Reduce that counterpart and establish a new, independent control identity.
                counterpart = max(1, counterpart // 2)
                control = f"{axis}:v1:opposite={counterpart}"
                bracket = Bracket(axis, bracket.seed, reason="fixed_counterpart_reduced")
                brackets[-1] = bracket
                previous = []
                fresh_successes = 0
                continue
            bracket.observe(observation)
            if previous and fresh_successes < 2:
                compatible = (
                    observation.outcome == "admitted"
                    and (pending.basis is None or observation.count_basis == pending.basis)
                    and (pending.control is None or observation.request.control == pending.control)
                    and observation.coordinate >= (pending.lower or 0)
                )
                if compatible:
                    fresh_successes += 1
                    if fresh_successes == 2:
                        # Rebuild chronologically: each boundary is consumed exactly once,
                        # with current evidence taking precedence over contradicted old limits.
                        bracket = Bracket(axis, bracket.seed)
                        brackets[-1] = bracket
                        for item in previous:
                            bracket.observe(item)
                        for item in observations:
                            if item.request.axis == axis and item.request.control == control:
                                bracket.observe(item)
                elif observation.outcome == "refused":
                    previous = []
                    bracket.reason = "history_baseline_refused"
        if bracket.nonprogress >= 3:
            bracket.reason = bracket.reason or "stalled"
        if reason != "complete":
            break
    if reason == "complete" and not all(bracket.converged for bracket in brackets):
        reason = "partial"
    journal.data["reason"] = reason
    journal.save()
    return DiscoveryResult(
        session_id,
        identity,
        tuple(observations),
        tuple(b.summary() for b in brackets),
        asdict(budget),
        reason,
        tuple(transport_retries),
    )
