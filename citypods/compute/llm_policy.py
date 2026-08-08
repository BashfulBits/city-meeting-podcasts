"""Provider-neutral model policy and route capabilities for the LLM scheduler."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any, Literal

DEFAULT_OUTPUT_TOKEN_MARGIN = 1024


@dataclass(frozen=True)
class LLMRequestPolicy:
    allowed_models: tuple[str, ...] | None = None
    allow_paid: bool = False
    # Research-only routes are never selected by ordinary pipeline work, even when their
    # transport credentials happen to be present on a runner.
    allow_experimental: bool = False
    deadline_at: datetime | None = None
    purpose: str = ""
    require_direct: bool = False
    submit_next: bool = False
    # Inert pending a real provider batch-submission API (review/33 §9 -- no route in ROUTES has
    # a confirmed server-side batch endpoint as of this field's addition). Plumbed through the
    # dispatch payload for forward compatibility only; nothing currently reads it to change
    # routing behavior.
    allow_batch: bool = True
    # Explicit, caller-opted-in permission to dispatch a route that also offers `direct` (today
    # only Gemini) over the Worker's `llm-dispatch` transport instead of calling the provider
    # directly. Default False: a route that can be reached directly always is, matching
    # review/33 §7's decision that Gemini's own free tier needs no Worker ("only build a
    # dedicated Gemini Worker later, and only if real usage shows it's needed"). This flag is the
    # sanctioned way for a future caller to reach *additional* capacity a direct call can't see --
    # concretely, a second configured account (`GEMINI_API_KEY_SECONDARY`) the Worker's per-route
    # ledger rotates onto once the direct route's own ledger entry is exhausted. See
    # review/41 for the incident (city discovery was silently routed through the Worker, breaking
    # its documented same-run-completion design) that motivated making this opt-in rather than
    # "dispatch whenever a Worker happens to be configured."
    allow_dispatch_overflow: bool = False


@dataclass(frozen=True)
class DeferredLLMRequest:
    """The portable capsule stored on a deferred `JobHandle`: everything `LiteLLMBackend.
    reconcile()` needs to retry the request without the caller reconstructing it.

    Every policy-bearing call that can't complete synchronously returns a `JobHandle` -- uniformly,
    whether that's because nothing is eligible right now (this capsule; re-runs the same gates
    fresh on each `reconcile()`) or because a route is genuinely in flight at an external Worker
    (no capsule; `JobHandle.deferred_request is None` for that case). The caller never needs to
    know which. `messages` mirrors what `InferenceJob.inputs["messages"]` would have held; `policy`
    is the exact policy the request was originally submitted with.
    """

    messages: tuple[Mapping[str, Any], ...]
    policy: LLMRequestPolicy


@dataclass(frozen=True)
class PeakWindow:
    tz: str
    start: time
    end: time
    multiplier: float


@dataclass(frozen=True)
class PricingPolicy:
    input_per_token: float = 0.0
    output_per_token: float = 0.0
    windows: tuple[PeakWindow, ...] = ()
    cost_cap: float | None = None
    # A hard calendar-day spending ceiling. Unlike ``cost_cap`` (the existing billing-cycle
    # guard), this is for cautiously enabling a paid route in a recurring experiment.
    daily_cost_cap: float | None = None


@dataclass(frozen=True)
class QuotaPolicy:
    rpm: int | None = None
    rpd: int | None = None
    tpm: int | None = None
    concurrency: int | None = None
    reset_timezone: str = "UTC"


@dataclass(frozen=True)
class LLMRoute:
    model: str
    transport: Literal["direct", "mistral-dispatch", "llm-dispatch"]
    free: bool
    quota: QuotaPolicy
    pricing: PricingPolicy
    experimental: bool = False
    # Direct structured output may make one corrective retry. A queued dispatch Worker submits
    # exactly one upstream request and must reserve only that one, even when the caller's
    # structured-output contract is present.
    max_provider_attempts: int | None = None
    transports: tuple[Literal["direct", "mistral-dispatch", "llm-dispatch"], ...] = ("direct",)


_DEEPSEEK_WINDOW = PeakWindow("UTC", time(16, 30), time(0, 30), 0.5)

ROUTES: dict[str, LLMRoute] = {
    "gemini/gemini-3-flash-preview": LLMRoute(
        model="gemini/gemini-3-flash-preview",
        transport="direct",
        transports=("direct", "llm-dispatch"),
        free=True,
        quota=QuotaPolicy(
            rpm=10,
            rpd=1500,
            tpm=250_000,
            reset_timezone="America/Los_Angeles",
        ),
        pricing=PricingPolicy(),
    ),
    "gemini/gemini-3.1-flash-lite": LLMRoute(
        model="gemini/gemini-3.1-flash-lite",
        transport="direct",
        transports=("direct", "llm-dispatch"),
        free=True,
        # Real free-tier allowance for this route (raised from the initial rpd=20 safety ceiling
        # now that the tag lane paces within its per-minute budget rather than bursting and
        # stopping). Paired with `gemini-3.5-flash-lite` below, which has its own independent
        # free-tier pool, so tagging can draw on ~2x these numbers across the two routes.
        quota=QuotaPolicy(rpm=15, rpd=500, tpm=250_000, reset_timezone="America/Los_Angeles"),
        pricing=PricingPolicy(),
    ),
    "gemini/gemini-3.5-flash-lite": LLMRoute(
        model="gemini/gemini-3.5-flash-lite",
        transport="direct",
        transports=("direct", "llm-dispatch"),
        free=True,
        # Independent free-tier pool from 3.1-flash-lite (separate model = separate provider quota),
        # so the tag lane can spill onto it once 3.1's per-minute/day window fills -- ~1000 tags/day
        # combined at 30 rpm across the two.
        quota=QuotaPolicy(rpm=15, rpd=500, tpm=250_000, reset_timezone="America/Los_Angeles"),
        pricing=PricingPolicy(),
    ),
    "deepseek/deepseek-v4-flash": LLMRoute(
        model="deepseek/deepseek-v4-flash",
        transport="direct",
        transports=("direct",),
        free=False,
        # Paid route: the maintainer confirmed there is no provider daily request allowance.
        # Cost telemetry remains active, but a speculative calendar-day ceiling must not stall
        # bounded research or later explicitly authorized paid work.
        quota=QuotaPolicy(),
        pricing=PricingPolicy(
            input_per_token=0.14e-6,
            output_per_token=0.28e-6,
            windows=(_DEEPSEEK_WINDOW,),
        ),
    ),
    "deepseek/deepseek-v4-pro": LLMRoute(
        model="deepseek/deepseek-v4-pro",
        transport="direct",
        transports=("direct",),
        free=False,
        quota=QuotaPolicy(),
        pricing=PricingPolicy(
            input_per_token=0.435e-6,
            output_per_token=0.87e-6,
            windows=(_DEEPSEEK_WINDOW,),
        ),
    ),
    "mistral/mistral-large-2512": LLMRoute(
        model="mistral/mistral-large-2512",
        transport="llm-dispatch",
        transports=("llm-dispatch",),
        free=True,
        # The account's Mistral Large alias resolves to ``mistral-large-2512`` (0.07 RPS), but
        # production does not call that API directly: the deployed one-model dispatch Worker
        # claims exactly one request each minute.  The local ledger must represent that stricter
        # end-to-end ceiling, not the upstream's theoretical four requests/minute.
        quota=QuotaPolicy(rpm=1),
        pricing=PricingPolicy(),
        max_provider_attempts=1,
    ),
    "mistral/mistral-large-3": LLMRoute(
        model="mistral/mistral-large-3",
        transport="llm-dispatch",
        transports=("llm-dispatch",),
        free=True,
        quota=QuotaPolicy(rpm=1),
        pricing=PricingPolicy(),
        max_provider_attempts=1,
    ),
    "mistral/mistral-medium-2508": LLMRoute(
        model="mistral/mistral-medium-2508",
        # Production agenda extraction is submitted through the shared deferred Worker so a
        # GitHub runner never holds an Mistral pacing sleep and the same job registry can retry or
        # finalize it later.
        transport="llm-dispatch",
        transports=("llm-dispatch",),
        free=True,
        quota=QuotaPolicy(rpm=22, tpm=356_250),
        pricing=PricingPolicy(),
        max_provider_attempts=1,
    ),
}


def estimate_tokens(messages: list[Mapping[str, Any]]) -> int:
    """Estimate input tokens conservatively from message content."""
    characters = sum(len(str(message.get("content", ""))) for message in messages)
    return math.ceil(characters / 4)


__all__ = [
    "DEFAULT_OUTPUT_TOKEN_MARGIN",
    "DeferredLLMRequest",
    "LLMRequestPolicy",
    "LLMRoute",
    "PeakWindow",
    "PricingPolicy",
    "QuotaPolicy",
    "ROUTES",
    "estimate_tokens",
]
