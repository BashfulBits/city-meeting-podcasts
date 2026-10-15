"""Bounded offline-first request-cap discovery; no configuration or provider-name branches."""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from citypods.provider_catalog.evidence import digest

AXES = ("input", "output", "total_input_heavy", "total_output_heavy")
MAX_INPUT, MAX_OUTPUT = 1_000_000, 131_072
INPUT_OUTPUT_COUNTERPART = 16_384
DEFAULT_INPUT_SEED = 500_000
DEFAULT_OUTPUT_SEED = INPUT_OUTPUT_COUNTERPART
DEFAULT_COMBINED_SEED = INPUT_OUTPUT_COUNTERPART + 2_048
MIN_OUTPUT_INPUT = 256
MIN_INPUT_PROBE = 2_000
MAX_SESSION_INPUT = 8_388_608
MAX_RUN_INPUT = 16_777_216


def output_discovery_control(min_tokens: bool = False) -> str:
    """The prompt and minimum-generation setting form one comparable output experiment."""
    if type(min_tokens) is not bool:
        raise ValueError("invalid minimum output capability")
    return "output:v4:min_tokens" if min_tokens else "output:v4"


def default_discovery_seed(axis: str) -> int:
    if axis == "input":
        return DEFAULT_INPUT_SEED
    if axis == "output":
        return DEFAULT_OUTPUT_SEED
    if axis in {"total_input_heavy", "total_output_heavy"}:
        return DEFAULT_COMBINED_SEED
    raise ValueError(f"unsupported discovery axis: {axis}")


def discovery_failure_reason(exc: Exception) -> str:
    """Retain control failure category without URLs, credentials or response bodies."""
    from citypods.compute.llm_dispatch_pause import DispatchPauseError

    reason = f"measurement_stopped:{type(exc).__name__}"
    if not isinstance(exc, DispatchPauseError):
        return reason
    match = re.match(
        r"(v2/dispatch:(?:pause|resume|status)) (?:request failed: "
        r"([A-Za-z][A-Za-z0-9_]{0,63})|returned HTTP ([0-9]{3})(?::|$))",
        str(exc),
    )
    if match:
        reason += f":{match[1]}:{match[2] or 'HTTP_' + match[3]}"
    cause = exc.__cause__
    if cause is not None:
        reason += f":cause={type(cause).__name__}"
    return reason


MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_PREVIEW_CHARS = 1_024
MAX_NO_RESPONSE_CALLS = 5
INPUT_SIZE_GUIDANCE = {"computed_input_budget_exhausted", "input_size_refused_without_exact_counts"}
MAX_RESPONSE_TIMEOUT_SECONDS = 720
MAX_SESSION_SECONDS = 3_600
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
    response_status: int | None = None
    response_media_type: str = "unknown"
    response_bytes: int | None = None
    message_content_state: str = "unknown"
    finish_reason_state: str = "unknown"
    usage_state: str = "unknown"
    provider_request_id: str | None = None
    response_id: str | None = None
    response_preview: str = ""
    sentinel_verification: str = "unknown"
    stream_bytes: int | None = None
    stream_events: int | None = None

    def __post_init__(self):
        if (
            datetime.fromisoformat(self.observed_at).tzinfo is None
            or self.outcome not in OUTCOMES
            or type(self.verified) is not bool
        ):
            raise ValueError("invalid discovery observation")
        if self.response_status is not None and (
            type(self.response_status) is not int or not 100 <= self.response_status <= 599
        ):
            raise ValueError("invalid discovery response status")
        if self.response_media_type not in {
            "unknown",
            "application/json",
            "text/event-stream",
            "other",
        }:
            raise ValueError("invalid discovery response media type")
        for name, bound in (("stream_bytes", 64 * 1024 * 1024), ("stream_events", 1_000_000)):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or not 0 <= value <= bound):
                raise ValueError("invalid discovery stream diagnostic")
        if not isinstance(self.sentinel_verification, str) or self.sentinel_verification not in {
            "unknown",
            "passed",
            "failed",
            "not_checked",
        }:
            raise ValueError("invalid discovery sentinel verification")
        if self.response_bytes is not None and (
            type(self.response_bytes) is not int or not 0 <= self.response_bytes <= 4 * 1024 * 1024
        ):
            raise ValueError("invalid discovery response byte count")
        if self.message_content_state not in {
            "unknown",
            "not_applicable",
            "missing",
            "null",
            "empty",
            "text",
            "other",
        }:
            raise ValueError("invalid discovery message content state")
        if self.finish_reason_state not in {
            "unknown",
            "not_applicable",
            "missing",
            "null",
            "stop",
            "length",
            "other",
        }:
            raise ValueError("invalid discovery finish reason state")
        if self.usage_state not in {
            "unknown",
            "not_applicable",
            "missing",
            "not_object",
            "prompt_missing",
            "prompt_invalid",
            "completion_missing",
            "completion_invalid",
            "total_invalid",
            "total_missing",
            "valid",
        }:
            raise ValueError("invalid discovery usage state")
        for value in (self.provider_request_id, self.response_id):
            if value is not None and (
                not isinstance(value, str)
                or not 1 <= len(value) <= 128
                or any(
                    char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-"
                    for char in value
                )
            ):
                raise ValueError("invalid discovery correlation ID")
        if not isinstance(self.response_preview, str) or len(self.response_preview) > (
            MAX_RESPONSE_PREVIEW_CHARS
        ):
            raise ValueError("invalid bounded discovery response preview")
        try:
            preview_bytes = len(self.response_preview.encode("utf-8"))
        except UnicodeError as exc:
            raise ValueError("invalid bounded discovery response preview") from exc
        if preview_bytes > MAX_RESPONSE_PREVIEW_CHARS:
            raise ValueError("invalid bounded discovery response preview")
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
    max_input: int = MAX_SESSION_INPUT
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
    selected_axes=None,
    input_priority=False,
    output_min_tokens=False,
    measure,
    renew,
    journal,
    seeds=None,
    counterparts=None,
    history=(),
    budget=None,
    shared_budget=None,
    clock=time.monotonic,
    sleep=time.sleep,
    deadline=None,
):
    """Inject transport, pause renewal and time; no provider calls without a charged receipt."""
    if type(input_priority) is not bool:
        raise ValueError("invalid discovery input priority")
    output_control = output_discovery_control(output_min_tokens)
    budget = budget or DiscoveryBudget()
    if selected_axes is not None and (
        not selected_axes or any(axis not in AXES for axis in selected_axes)
    ):
        raise ValueError("invalid selected discovery axes")
    for selected, multiplier in ((budget, 1), (shared_budget, 2)):
        if selected and (
            selected.max_calls > 64 * multiplier
            or selected.max_input > MAX_SESSION_INPUT * multiplier
            or selected.max_output > 524_288 * multiplier
        ):
            raise ValueError("discovery envelope exceeds reviewed ceiling")
    started = datetime.fromisoformat(journal.data["started_at"])
    if started.tzinfo is None or started > datetime.now(UTC):
        raise ValueError("invalid discovery session start")
    elapsed = (datetime.now(UTC) - started).total_seconds()
    deadline = min(
        deadline if deadline is not None else float("inf"),
        clock() + max(0, MAX_SESSION_SECONDS - elapsed),
    )
    session_deadline = deadline
    shared_output_cost = None
    no_response_calls = 0
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
    for axis_index, axis in enumerate(AXES):
        deadline = session_deadline
        bracket = Bracket(axis, int(seeds.get(axis, default_discovery_seed(axis))))
        brackets.append(bracket)
        supporting = input_priority and axis in {"output", "total_input_heavy"}
        if input_priority and axis == "total_output_heavy":
            bracket.reason = "optional_output_heavy_not_searched"
            continue
        if selected_axes is not None and axis not in selected_axes:
            bracket.reason = "not_selected"
            continue
        if axis not in supported_axes:
            bracket.reason = "unsupported_endpoint_axis"
            continue
        remaining_axes = [
            item
            for item in AXES[axis_index:]
            if item in supported_axes
            and (selected_axes is None or item in selected_axes)
            and not (input_priority and item == "total_output_heavy")
        ]
        axis_started = clock()
        if len(remaining_axes) > 1 and axis_started < session_deadline:
            weights = {
                item: 8 if input_priority and item == "input" else 1 for item in remaining_axes
            }
            deadline = axis_started + (session_deadline - axis_started) * (
                weights[axis] / sum(weights.values())
            )
        compatible_history = [
            item
            for item in history
            if item.request.identity == identity
            and item.request.axis == axis
            and item.request.parser_version == parser_version
            and item.request.fixture_version == "discovery-v1"
        ]
        if supporting:
            # A prior maximum search may have reduced its counterpart to a tiny allowance.
            # The supporting example must use this run's practical output target instead.
            compatible_history = []
        previous = [
            item
            for item in compatible_history
            if item.outcome in {"admitted", "refused"} and item.verified
        ]
        previous.sort(key=lambda item: datetime.fromisoformat(item.observed_at))
        counterpart = INPUT_OUTPUT_COUNTERPART if axis in {"input", "total_input_heavy"} else 2_048
        control_version = (
            "v3" if axis == "output" else "v2" if axis in {"input", "total_input_heavy"} else "v1"
        )
        base_control = (
            output_control
            if axis == "output"
            else f"{axis}:reservation-v2"
            if axis.startswith("total_")
            else f"{axis}:{control_version}"
        )
        hinted_counterpart = (counterparts or {}).get(axis, counterpart)
        if (
            not positive(hinted_counterpart)
            or hinted_counterpart > counterpart
            or (axis in {"output", "total_output_heavy"} and hinted_counterpart < MIN_OUTPUT_INPUT)
        ):
            raise ValueError("invalid discovery fixed-counterpart hint")
        if hinted_counterpart != counterpart:
            counterpart = hinted_counterpart
            control = f"{base_control}:opposite={counterpart}"
        else:
            control = base_control
        compatible = [
            item
            for item in previous + observations
            if item.request.axis == axis
            and (
                item.request.control == base_control
                or item.request.control.startswith(base_control + ":opposite=")
            )
        ]
        if compatible:
            latest = sorted(compatible, key=lambda item: datetime.fromisoformat(item.observed_at))[
                -1
            ]
            prefix = base_control + ":opposite="
            if latest.request.control.startswith(prefix):
                value = latest.request.control.removeprefix(prefix)
                minimum = MIN_OUTPUT_INPUT if axis in {"output", "total_output_heavy"} else 1
                if value.isascii() and value.isdigit() and minimum <= int(value) <= counterpart:
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
        required_fresh = 1 if axis == "output" else 2
        upper_recheck = None
        qualitative_upper = min(
            (
                item.request.target
                for item in compatible_history
                if item.request.control == control
                and item.diagnostic in INPUT_SIZE_GUIDANCE | {"size_refused_without_exact_counts"}
                and item.request.target > (pending.lower or 0)
            ),
            default=None,
        )
        if supporting:
            # Supporting checks are fresh examples, not independent maximum searches.
            previous = []
            pending = Bracket(axis, bracket.seed)
            qualitative_upper = None
            if axis == "total_input_heavy":
                primary = next((b for b in brackets if b.axis == "input"), None)
                if primary is None or primary.lower is None:
                    bracket.reason = "supporting_input_not_verified"
                    continue
                generated = [
                    item.reported_output
                    for item in observations
                    if item.request.axis == "output"
                    and positive(item.reported_output)
                    and item.response_status == 200
                    and item.usage_state == "valid"
                ]
                if generated:
                    counterpart = min(counterpart, max(generated))
                    control = f"{base_control}:opposite={counterpart}"
                bracket.seed = primary.lower + counterpart
        qualitative_rechecked = False
        axis_calls = 0
        recent_output_cost = None
        clipped_count = None
        clipped_recheck = None
        early_stop_retries = 0
        pending_transport = []
        while not bracket.converged and bracket.nonprogress < 3:
            if supporting and axis_calls:
                bracket.reason = "supporting_check_inconclusive"
                break
            if clock() >= deadline or budget.overrun or (shared_budget and shared_budget.overrun):
                if (
                    clock() >= deadline
                    and deadline < session_deadline
                    and not budget.overrun
                    and not (shared_budget and shared_budget.overrun)
                ):
                    bracket.reason = "axis_time_budget_exhausted"
                else:
                    reason = "deadline_or_reported_usage_overrun"
                break
            request_ceiling = (
                MAX_INPUT if axis == "input" else MAX_OUTPUT if axis == "output" else None
            )
            if (
                request_ceiling is not None
                and bracket.lower is not None
                and bracket.lower >= request_ceiling
            ):
                bracket.reason = "hard_ceiling_lower_bound"
                break
            if (
                axis == "input"
                and bracket.lower is None
                and bracket.upper is not None
                and bracket.upper <= MIN_INPUT_PROBE
            ):
                bracket.reason = "minimum_probe_refused"
                break
            if request_ceiling is not None and any(
                item.request.axis == axis
                and item.request.target >= request_ceiling
                and item.outcome == "admitted"
                for item in observations
            ):
                bracket.reason = "hard_ceiling_lower_bound"
                break
            target = (
                pending.lower
                if previous and fresh_successes < required_fresh and pending.lower
                else upper_recheck
                if upper_recheck is not None
                else bracket.target()
            )
            if clipped_recheck is not None:
                target = clipped_recheck
            explicit_recheck = (
                clipped_recheck is not None
                or upper_recheck is not None
                or bool(previous and fresh_successes < required_fresh and pending.lower)
            )
            if (
                axis == "input"
                and qualitative_upper is not None
                and qualitative_upper > (bracket.lower or 0)
                and previous
                and fresh_successes >= required_fresh
                and not qualitative_rechecked
            ):
                # An old non-numerical refusal guides one fresh test, not a permanent ceiling.
                target = qualitative_upper
                explicit_recheck = True
                qualitative_rechecked = True
            if qualitative_upper is not None and not explicit_recheck:
                if bracket.lower is not None:
                    if qualitative_upper <= bracket.lower:
                        qualitative_upper = None
                    elif qualitative_upper - bracket.lower <= max(
                        1, math.ceil(bracket.lower / 200)
                    ):
                        bracket.reason = "size_refusal_unmeasured_upper"
                        break
                    else:
                        target = bracket.lower + (qualitative_upper - bracket.lower) // 2
                else:
                    target = max(1, qualitative_upper // 2)
                if axis == "input":
                    target = max(MIN_INPUT_PROBE, target)
            checking_upper = upper_recheck is not None and target == upper_recheck
            if (
                axis == "output"
                and bracket.lower is not None
                and target > bracket.lower
                and not checking_upper
            ):
                # Failed targets guide cheaper retry sizes; they are never refused bounds.
                failed = [
                    item.request.target
                    for item in compatible_history + observations
                    if item.request.axis == axis
                    and item.outcome in {"inconclusive", "outage"}
                    and item.request.control == control
                    and item.request.target > bracket.lower
                    and timedelta(0)
                    <= datetime.now(UTC) - datetime.fromisoformat(item.observed_at)
                    <= timedelta(days=1)
                    and (
                        item.diagnostic in {"worker_response_timeout", "transport_timeout"}
                        or item.outcome == "outage"
                        or (
                            item.diagnostic == "provider_sse_error"
                            and item.response_preview == '"Internal server error"'
                        )
                    )
                ]
                if failed:
                    hint = min(failed)
                    if hint - bracket.lower <= max(1, math.ceil(bracket.lower / 200)):
                        bracket.reason = "output_time_limited_lower_bound"
                        break
                    target = min(target, bracket.lower + (hint - bracket.lower) // 2)
            if axis == "input" and bracket.lower is None and bracket.upper is not None:
                target = max(target, MIN_INPUT_PROBE)
            if request_ceiling is not None and target > request_ceiling:
                target = request_ceiling
            if axis == "input":
                input_tokens, output_tokens = target, counterpart
            elif axis == "output":
                input_tokens, output_tokens = counterpart, target
            elif axis == "total_input_heavy":
                input_tokens, output_tokens = (
                    max(MIN_OUTPUT_INPUT, target - counterpart),
                    counterpart,
                )
            else:
                input_tokens = min(counterpart, max(MIN_OUTPUT_INPUT, target // 4))
                output_tokens = max(1, target - input_tokens)
            matching = [
                item
                for item in compatible_history + observations
                if item.request.axis == axis
                and item.reported_input
                and item.request.control == control
                and item.verified
                and item.outcome in {"admitted", "refused"}
            ]
            if supporting and axis == "total_input_heavy":
                matching = [
                    item
                    for item in observations
                    if item.request.axis == "input"
                    and item.request.control == primary.control
                    and item.verified
                    and item.outcome == "admitted"
                    and positive(item.reported_input)
                ]
            if matching and axis != "output":
                last = max(
                    enumerate(matching),
                    key=lambda pair: (datetime.fromisoformat(pair[1].observed_at), pair[0]),
                )[1]
                floor = 1 if axis == "input" else MIN_OUTPUT_INPUT
                input_tokens = max(
                    floor, math.ceil(input_tokens * last.request.input_tokens / last.reported_input)
                )
            if axis == "input":
                # MAX_INPUT caps the reservation; it must not erase provider-count mapping
                # when the desired provider-counted target reaches that same ceiling.
                input_tokens = min(input_tokens, MAX_INPUT)
            if input_tokens > MAX_INPUT or output_tokens > MAX_OUTPUT:
                bracket.reason = "per_call_ceiling"
                break
            generation_cost = recent_output_cost or shared_output_cost
            if axis != "input" and generation_cost is not None:
                expected = min(
                    MAX_RESPONSE_TIMEOUT_SECONDS, math.ceil(output_tokens * generation_cost)
                )
                if deadline - clock() < expected:
                    bracket.reason = (
                        "insufficient_output_probe_window"
                        if axis == "output"
                        else "insufficient_generation_probe_window"
                    )
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

            def before_call(request=request, deadline=deadline):
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

            call_started = clock()
            try:
                renew()
                observation = measure(
                    request,
                    before_call,
                    min(MAX_RESPONSE_TIMEOUT_SECONDS, deadline - clock()),
                )
                if not charged or observation.request != request:
                    raise ValueError("discovery_transport_admission_or_identity_mismatch")
                journal.complete(observation)
            except Exception as exc:
                if isinstance(exc, ValueError) and str(exc) == "discovery_budget_exhausted":
                    reason = bracket.reason = "budget_exhausted"
                    break
                reason = discovery_failure_reason(exc)
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
            axis_calls += 1
            if (
                axis == "output"
                and observation.outcome == "admitted"
                and observation.verified
                and positive(observation.coordinate)
                and clock() > call_started
            ):
                recent_output_cost = (clock() - call_started) / observation.coordinate
                shared_output_cost = recent_output_cost
            if (
                axis.startswith("total_")
                and observation.verified
                and observation.finish_reason == "length"
                and positive(observation.reported_output)
                and clock() > call_started
            ):
                shared_output_cost = (clock() - call_started) / observation.reported_output
            if checking_upper and observation.outcome != "quota":
                upper_recheck = None
            budget.settle(observation)
            if shared_budget:
                shared_budget.settle(observation)
            if observation.diagnostic in NO_RESPONSE_DIAGNOSTICS:
                no_response_calls += 1
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
                if no_response_calls < MAX_NO_RESPONSE_CALLS:
                    continue
                transport_retries.append(tuple(pending_transport))
                pending_transport = []
                reason = bracket.reason = "uncertain_no_response_after_five_calls"
                break
            no_response_calls = 0
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
            if observation.response_status in {401, 402, 403}:
                reason = bracket.reason = "access_deferred"
                break
            if observation.outcome in {"outage", "daily"}:
                reason = observation.outcome
                bracket.reason = reason
                break
            if supporting:
                if observation.outcome == "quota":
                    reason = bracket.reason = "quota_deferred"
                else:
                    bracket.observe(observation)
                    bracket.reason = (
                        "supporting_check_admitted"
                        if bracket.lower is not None
                        else "supporting_check_inconclusive"
                    )
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
            early_stop = observation.diagnostic == "output_stopped_early" or (
                axis.startswith("total_")
                and observation.diagnostic.startswith("combined_output_stopped_early:")
            )
            if (
                early_stop
                and observation.finish_reason == "stop"
                and positive(observation.reported_output)
            ):
                if early_stop_retries >= 3 or request.output_tokens <= 1:
                    bracket.reason = "early_stopping_after_smaller_probes"
                    break
                early_stop_retries += 1
                smaller = max(1, min(request.output_tokens // 2, observation.reported_output))
                if axis == "output":
                    if bracket.lower is not None:
                        if request.output_tokens - bracket.lower <= max(
                            1, math.ceil(bracket.lower / 200)
                        ):
                            bracket.reason = "output_early_stop_limited_lower_bound"
                            break
                        smaller = bracket.lower + (request.output_tokens - bracket.lower) // 2
                    clipped_recheck = smaller
                elif axis == "total_input_heavy":
                    next_target = max(1, target - counterpart) + smaller
                    counterpart = smaller
                    control = f"{base_control}:opposite={counterpart}"
                    bracket = Bracket(axis, next_target, reason="fixed_counterpart_reduced")
                    clipped_count = None
                    brackets[-1] = bracket
                    previous = []
                    fresh_successes = 0
                    qualitative_upper = None
                else:
                    next_counterpart = max(MIN_OUTPUT_INPUT, min(counterpart, smaller // 3))
                    if next_counterpart != counterpart:
                        counterpart = next_counterpart
                        control = f"{base_control}:opposite={counterpart}"
                        bracket = Bracket(axis, target, reason="fixed_counterpart_reduced")
                        brackets[-1] = bracket
                        previous = []
                        fresh_successes = 0
                        qualitative_upper = None
                    clipped_recheck = counterpart + smaller
                continue
            if (
                axis == "output"
                and observation.outcome == "admitted"
                and observation.verified
                and observation.finish_reason == "length"
            ):
                if clipped_recheck is not None and target == clipped_recheck:
                    clipped_recheck = None
                if observation.coordinate < request.output_tokens:
                    bracket.observe(observation)
                    previous = []
                    upper_recheck = None
                    if clipped_count == observation.coordinate:
                        bracket.reason = "output_clipped_lower_bound"
                        break
                    clipped_count = observation.coordinate
                    clipped_recheck = clipped_count
                    continue
            if (
                axis.startswith("total_")
                and observation.diagnostic.startswith("combined_output_clipped")
                and positive(observation.reported_output)
            ):
                if axis == "total_input_heavy" and counterpart > observation.reported_output:
                    counterpart = observation.reported_output
                    control = f"{base_control}:opposite={counterpart}"
                    bracket = Bracket(axis, target, reason="fixed_counterpart_reduced")
                    brackets[-1] = bracket
                    previous = []
                    fresh_successes = 0
                    qualitative_upper = None
                elif axis == "total_output_heavy":
                    if clipped_count == observation.reported_output:
                        bracket.reason = "combined_output_clipped_lower_bound"
                        break
                    clipped_count = observation.reported_output
                    next_counterpart = max(MIN_OUTPUT_INPUT, min(counterpart, clipped_count // 3))
                    if next_counterpart != counterpart:
                        counterpart = next_counterpart
                        control = f"{base_control}:opposite={counterpart}"
                        bracket = Bracket(axis, target, reason="fixed_counterpart_reduced")
                        brackets[-1] = bracket
                        previous = []
                        fresh_successes = 0
                        qualitative_upper = None
                    clipped_recheck = counterpart + clipped_count
                else:
                    bracket.reason = "combined_clipped_inconclusive"
                    break
                continue
            if axis.startswith("total_") and target == clipped_recheck:
                clipped_recheck = None
            if (
                observation.diagnostic in INPUT_SIZE_GUIDANCE
                and axis in {"output", "total_output_heavy"}
                and counterpart > MIN_OUTPUT_INPUT
            ):
                counterpart = max(MIN_OUTPUT_INPUT, counterpart // 2)
                control = f"{base_control}:opposite={counterpart}"
                bracket = Bracket(axis, target, reason="fixed_counterpart_reduced")
                brackets[-1] = bracket
                previous = []
                fresh_successes = 0
                upper_recheck = None
                qualitative_upper = None
                continue
            if observation.diagnostic in INPUT_SIZE_GUIDANCE and axis in {
                "output",
                "total_output_heavy",
            }:
                bracket.reason = "minimum_input_fixture_refused"
                break
            if observation.diagnostic in INPUT_SIZE_GUIDANCE | {
                "size_refused_without_exact_counts"
            }:
                if (
                    axis in {"input", "total_input_heavy"}
                    and (
                        (axis == "input" and target <= MIN_INPUT_PROBE)
                        or (axis == "total_input_heavy" and target <= counterpart)
                    )
                    and counterpart > 1
                ):
                    counterpart = max(1, counterpart // 2)
                    control = f"{base_control}:opposite={counterpart}"
                    bracket = Bracket(axis, target, reason="fixed_counterpart_reduced")
                    brackets[-1] = bracket
                    previous = []
                    fresh_successes = 0
                    qualitative_upper = None
                else:
                    qualitative_upper = min(qualitative_upper or target, target)
                continue
            blocking = (
                {"output", "requested_output"}
                if axis in {"input", "total_input_heavy"}
                else {"input", "prompt_tokens"}
            )
            if observation.outcome == "refused" and observation.count_basis in blocking:
                floor = MIN_OUTPUT_INPUT if axis in {"output", "total_output_heavy"} else 1
                if counterpart <= floor:
                    bracket.reason = "minimum_fixed_counterpart_refused"
                    break
                # A typed failure of the fixed counterpart supplies no boundary for this axis.
                # Reduce that counterpart and establish a new, independent control identity.
                counterpart = max(floor, counterpart // 2)
                control = f"{base_control}:opposite={counterpart}"
                bracket = Bracket(axis, bracket.seed, reason="fixed_counterpart_reduced")
                brackets[-1] = bracket
                previous = []
                fresh_successes = 0
                upper_recheck = None
                recent_output_cost = None
                continue
            bracket.observe(observation)
            if (
                request_ceiling is not None
                and request.target >= request_ceiling
                and observation.outcome == "admitted"
            ):
                bracket.reason = "hard_ceiling_lower_bound"
                break
            if previous and fresh_successes < required_fresh:
                compatible = (
                    observation.outcome == "admitted"
                    and (pending.basis is None or observation.count_basis == pending.basis)
                    and (pending.control is None or observation.request.control == pending.control)
                    and observation.coordinate >= (pending.lower or 0)
                )
                if compatible:
                    fresh_successes += 1
                    if axis == "output":
                        # The fresh lower proves itself. Re-test an old refusal rather than
                        # importing it as current proof or spending a second identical success.
                        upper_recheck = (
                            pending.upper
                            if pending.upper is not None and pending.upper > (bracket.lower or 0)
                            else None
                        )
                    elif fresh_successes == 2:
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
        if pending_transport:
            transport_retries.append(tuple(pending_transport))
            pending_transport = []
            if reason == "deadline_or_reported_usage_overrun":
                reason = bracket.reason = "uncertain_no_response_before_session_deadline"
        if bracket.nonprogress >= 3:
            bracket.reason = bracket.reason or "stalled"
        if reason != "complete":
            break
    if reason == "complete" and not all(bracket.converged for bracket in brackets):
        reason = "partial"
    if input_priority and reason in {"complete", "partial"}:
        primary = next((b for b in brackets if b.axis == "input"), None)
        if primary and primary.converged:
            reason = "input_characterized"
        elif primary and primary.lower and primary.reason == "size_refusal_unmeasured_upper":
            reason = "input_boundary_estimated"
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
