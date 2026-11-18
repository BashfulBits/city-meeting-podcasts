"""Choose the cheapest GPU that fits a model and token volume, with a bounded timeout.

The estimate for one launch is ``startup + load + padded_tokens / tokens_per_second`` plus the
kernel-compile overhead of the chosen cache mode: ``none`` (compile and autotune), ``configs``
(fla prebaked configs only) or ``archive`` (configs plus a restored compiled-kernel archive).
Cost is that time at the GPU envelope's billed rate. A volume that would exceed the timeout
ceiling is split into sequential launches, each paying the fixed startup and load cost again, and
the reservation for one launch is the admission-ledger worst case ``max(60 s, timeout) * rate``.
Cost uses the median load time; the timeout and reservation use the p90 load time.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from citypods.gpu_launch.profiles import (
    CACHE_MODES,
    GPU_CLASSES,
    Calibration,
    GpuClass,
    ModelProfile,
)

TIMEOUT_STEP = 30
MAX_TIMEOUT = 3600  # the launcher's task-timeout override limit; lower it to cap worst-case spend
MIN_BILLED_SECONDS = 60
VRAM_HEADROOM = 0.97
# Characters-to-token estimate for planning only; the worker measures real tokens. Least-squares fit
# on the 158 frozen decisions (44,282 tokens, tokens = chars / 4.34 + 38, worst row 25% off).
CHARS_PER_TOKEN = 4.3
TEMPLATE_TOKENS = 38


@dataclass(frozen=True)
class Candidate:
    gpu: str
    feasible: bool
    reason: str | None
    launches: int = 0
    token_budget: int = 0
    seconds_per_launch: float = 0.0
    total_seconds: float = 0.0
    timeout_seconds: int = 0
    cost_microdollars: int = 0
    reservation_microdollars: int = 0  # per launch
    confidence: str = ""


@dataclass(frozen=True)
class LaunchPlan:
    model_id: str
    padded_tokens: int
    cache: str
    chosen: Candidate
    candidates: tuple[Candidate, ...]


class NoFeasibleGpu(ValueError):
    """No calibrated GPU class can run the model within the constraints."""


def estimate_padded_tokens(prompt_chars: Iterable[int], padding: float = 1.2) -> int:
    """Rough padded-token volume from prompt character counts (planning only)."""
    total = sum(math.ceil(c / CHARS_PER_TOKEN) + TEMPLATE_TOKENS for c in prompt_chars)
    return math.ceil(total * padding)


def _timeout(seconds: float, margin: float) -> int:
    return max(TIMEOUT_STEP, math.ceil(seconds * margin / TIMEOUT_STEP) * TIMEOUT_STEP)


def _candidate(
    profile: ModelProfile,
    gpu: GpuClass,
    cal: Calibration | None,
    padded_tokens: int,
    *,
    cache: str,
    ceiling: int,
    margin: float,
    allow_estimated: bool,
) -> Candidate:
    def no(reason: str) -> Candidate:
        return Candidate(gpu.name, False, reason)

    if cal is None:
        return no("no calibration for this model on this GPU")
    if cal.confidence == "estimated" and not allow_estimated:
        return no("calibration is an estimate, not a measurement")
    if cal.peak_gpu_gib > gpu.vram_gib * VRAM_HEADROOM:
        return no(f"peak {cal.peak_gpu_gib:g} GiB does not fit {gpu.vram_gib:g} GiB")
    budget = min(cal.token_budget, gpu.max_token_budget)
    cold = {
        "none": cal.cold_overhead_seconds,
        "configs": cal.config_overhead_seconds,
        "archive": 0.0,
    }[cache]
    fixed = cal.startup_seconds + cal.load_seconds + cold  # typical: prices the plan
    tail = cal.startup_seconds + cal.load_seconds_p90 + cold  # p90 load: sizes the timeout
    if tail * margin > ceiling:
        return no("fixed startup and load time exceed the timeout ceiling")
    per_launch_tokens = cal.tokens_per_second * (ceiling / margin - tail)
    launches = max(1, math.ceil(padded_tokens / per_launch_tokens))

    def sized(count: int) -> tuple[float, int]:
        run = padded_tokens / count / cal.tokens_per_second
        return fixed + run, _timeout(tail + run, margin)

    seconds, timeout = sized(launches)
    if timeout > ceiling:
        # Rounding to the timeout step pushed past the ceiling: split once more.
        launches += 1
        seconds, timeout = sized(launches)
    total = seconds * launches
    return Candidate(
        gpu=gpu.name,
        feasible=True,
        reason=None,
        launches=launches,
        token_budget=budget,
        seconds_per_launch=round(seconds, 1),
        total_seconds=round(total, 1),
        timeout_seconds=timeout,
        cost_microdollars=math.ceil(total * gpu.rate),
        reservation_microdollars=math.ceil(max(MIN_BILLED_SECONDS, timeout) * gpu.rate),
        confidence=cal.confidence,
    )


def plan_launch(
    profile: ModelProfile,
    padded_tokens: int,
    *,
    gpus: Sequence[str] | None = None,
    cache: str = "configs",
    ceiling: int = MAX_TIMEOUT,
    margin: float = 1.25,
    deadline_seconds: float | None = None,
    cost_tolerance: float = 0.10,
    allow_estimated: bool = False,
    classes: Mapping[str, GpuClass] = GPU_CLASSES,
) -> LaunchPlan:
    """Pick the cheapest feasible GPU; within ``cost_tolerance`` of the minimum, the fastest."""
    if cache not in CACHE_MODES:
        raise ValueError(f"cache must be one of {CACHE_MODES}")
    if padded_tokens <= 0 or ceiling < TIMEOUT_STEP or margin < 1:
        raise ValueError("need positive tokens, a ceiling of at least 30 s and margin >= 1")
    names = list(gpus) if gpus is not None else list(classes)
    unknown = set(names) - set(classes)
    if unknown:
        raise ValueError(f"unknown GPU classes: {sorted(unknown)}")
    candidates = []
    for name in names:
        candidate = _candidate(
            profile,
            classes[name],
            profile.calibration(name),
            padded_tokens,
            cache=cache,
            ceiling=ceiling,
            margin=margin,
            allow_estimated=allow_estimated,
        )
        if candidate.feasible and deadline_seconds and candidate.total_seconds > deadline_seconds:
            candidate = Candidate(name, False, "total time exceeds the deadline")
        candidates.append(candidate)
    feasible = [c for c in candidates if c.feasible]
    if not feasible:
        reasons = "; ".join(f"{c.gpu}: {c.reason}" for c in candidates)
        raise NoFeasibleGpu(f"{profile.model_id}: no feasible GPU ({reasons})")
    cheapest = min(c.cost_microdollars for c in feasible)
    near = [c for c in feasible if c.cost_microdollars <= cheapest * (1 + cost_tolerance)]
    chosen = min(near, key=lambda c: (c.total_seconds, c.cost_microdollars, c.gpu))
    return LaunchPlan(profile.model_id, padded_tokens, cache, chosen, tuple(candidates))
