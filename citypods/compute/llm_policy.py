"""Provider-neutral model policy and route capabilities for the LLM scheduler."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time
from pathlib import Path
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
    # Durable Worker-queue admission. The caller submits work to the dispatch Worker and lets
    # that Worker's ledger decide when it reaches a provider. Enqueueing must not consume the
    # runner's provider RPD/RPM/TPM reservation.
    queue_only: bool = False
    # Worker dispatch lane.  ``fast`` drains short requests before a scheduled invocation risks
    # starting work it cannot finish; ``long`` opts into the bounded long-context timeout lane.
    timeout_class: Literal["fast", "long"] = "long"


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
    output_token_budget: int = DEFAULT_OUTPUT_TOKEN_MARGIN


@dataclass(frozen=True)
class PeakWindow:
    tz: str
    start: time
    end: time
    multiplier: float


@dataclass(frozen=True)
class PricingPeriod:
    """One effective-dated rate card for a physical LLM route.

    ``input_per_token`` is the input rate for compatibility with the original route table.
    Windows multiply the period's rates; a period can therefore
    describe both ordinary/off-peak pricing and peak surcharges without provider-specific code.
    """

    effective_at: datetime
    input_per_token: float | None = None
    output_per_token: float | None = None
    windows: tuple[PeakWindow, ...] = ()


@dataclass(frozen=True)
class PricingPolicy:
    input_per_token: float = 0.0
    output_per_token: float = 0.0
    windows: tuple[PeakWindow, ...] = ()
    periods: tuple[PricingPeriod, ...] = ()
    cost_cap: float | None = None
    # A hard calendar-day spending ceiling. Unlike ``cost_cap`` (the existing billing-cycle
    # guard), this is for cautiously enabling a paid route in a recurring experiment.
    daily_cost_cap: float | None = None

    def rates_at(self, now: datetime | None = None) -> tuple[float, float, tuple[PeakWindow, ...]]:
        """Return ``(input, output, windows)`` at ``now``.

        A missing or invalid historical rate card falls back to the route's legacy fields.  This
        keeps pricing advisory: a bad pricing update cannot make a route unusable.
        """
        instant = now or datetime.now(UTC)
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=UTC)
        instant = instant.astimezone(UTC)
        active = [
            period for period in self.periods if period.effective_at.astimezone(UTC) <= instant
        ]
        period = max(active, key=lambda candidate: candidate.effective_at) if active else None
        if period is None:
            return (
                self.input_per_token,
                self.output_per_token,
                self.windows,
            )
        return (
            self.input_per_token if period.input_per_token is None else period.input_per_token,
            self.output_per_token if period.output_per_token is None else period.output_per_token,
            period.windows or self.windows,
        )


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
    # Physical route identity and direct LiteLLM adapter metadata.  Empty defaults preserve the
    # small hand-built routes used by unit tests and old callers; generated routes always fill all
    # fields and use ``route_id`` as their shared-ledger key.
    route_id: str = ""
    provider: str = ""
    provider_rpm: int | None = None
    upstream_model: str = ""
    direct_model: str = ""
    api_base: str = ""
    chat_path: str = "/v1/chat/completions"
    api_key_env: str = ""
    account_id: str = ""
    # Conservative defaults for hand-authored/test routes. Generated route catalogs materialize
    # provider- or route-specific values for every physical route.
    input_context_limit: int = 32768
    output_context_limit: int = 1024

    def __post_init__(self) -> None:
        # Hand-built fallback and test routes intentionally omit provider adapter metadata.  They
        # still need to be valid direct LiteLLM routes, so preserve their canonical model rather
        # than making every consumer remember an empty-string fallback.
        if not self.direct_model:
            object.__setattr__(self, "direct_model", self.model)


_DEEPSEEK_WINDOW = PeakWindow("UTC", time(16, 30), time(0, 30), 0.5)


def _load_generated_catalog() -> tuple[list[LLMRoute], dict[str, str]]:
    path = Path(__file__).with_name("llm_routes.json")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Invalid generated LLM route catalog at {path}; rerun scripts/compile_llm_limits.py"
        ) from exc

    def parse_time(value: Any) -> time:
        if isinstance(value, time):
            return value
        return time.fromisoformat(str(value))

    def parse_datetime(value: Any) -> datetime:
        parsed = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        )
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def parse_windows(raw_windows: Any) -> tuple[PeakWindow, ...]:
        windows = []
        for raw_window in raw_windows or ():
            windows.append(
                PeakWindow(
                    tz=str(raw_window.get("tz", "UTC")),
                    start=parse_time(raw_window["start"]),
                    end=parse_time(raw_window["end"]),
                    multiplier=float(raw_window.get("multiplier", 1.0)),
                )
            )
        return tuple(windows)

    result = []
    for item in raw.get("routes", []):
        provider = str(item.get("provider", ""))
        raw_pricing = item.get("pricing") or {}
        periods = []
        for raw_period in raw_pricing.get("periods", ()):
            periods.append(
                PricingPeriod(
                    effective_at=parse_datetime(raw_period["effective_at"]),
                    input_per_token=(
                        float(raw_period["input_per_token"])
                        if raw_period.get("input_per_token") is not None
                        else None
                    ),
                    output_per_token=(
                        float(raw_period["output_per_token"])
                        if raw_period.get("output_per_token") is not None
                        else None
                    ),
                    windows=parse_windows(raw_period.get("windows")),
                )
            )
        result.append(
            LLMRoute(
                model=str(item["model"]),
                transport="direct",
                transports=tuple(item.get("transports", ("direct",))),
                free=bool(item.get("free", False)),
                quota=QuotaPolicy(
                    rpm=item.get("rpm"),
                    rpd=item.get("rpd"),
                    tpm=item.get("tpm"),
                    concurrency=item.get("concurrency"),
                    reset_timezone=str(item.get("reset_timezone", "UTC")),
                ),
                pricing=PricingPolicy(
                    input_per_token=float(item.get("input_per_token", 0) or 0),
                    output_per_token=float(item.get("output_per_token", 0) or 0),
                    windows=(_DEEPSEEK_WINDOW,) if provider == "deepseek" else (),
                    periods=tuple(periods),
                ),
                max_provider_attempts=None,
                route_id=str(item.get("route_id", item["model"])),
                provider=provider,
                provider_rpm=item.get("provider_rpm"),
                upstream_model=str(item.get("upstream_model", "")),
                direct_model=str(item.get("direct_model", "")),
                api_base=str(item.get("api_base", "")),
                chat_path=str(item.get("chat_path", "/v1/chat/completions")),
                api_key_env=str(item.get("api_key_env", "")),
                account_id=str(item.get("account_id", "")),
                input_context_limit=max(1, int(item.get("input_context_limit", 32768) or 32768)),
                output_context_limit=max(1, int(item.get("output_context_limit", 1024) or 1024)),
            )
        )
    aliases = {
        str(source): str(target)
        for source, target in (raw.get("model_aliases") or {}).items()
        if str(source) and str(target)
    }
    return result, aliases


_GENERATED_ROUTES, MODEL_ALIASES = _load_generated_catalog()


def canonical_model(model: str) -> str:
    """Return the shared logical model key for a legacy/provider-qualified selector."""
    current = model
    seen: set[str] = set()
    while current in MODEL_ALIASES and current not in seen:
        seen.add(current)
        current = MODEL_ALIASES[current]
    return current


# Physical route registry used for selection and the shared ledger.  ``ROUTES`` remains the
# stable logical-model compatibility view: one deterministic primary route per model for callers
# that only need to inspect capabilities.  ``ROUTE_CANDIDATES`` retains every provider/account
# route, so a logical model can spill across providers and accounts without a second config table.
ROUTE_REGISTRY: dict[str, LLMRoute] = {
    route.route_id or route.model: route for route in _GENERATED_ROUTES
}
ROUTE_CANDIDATES: dict[str, tuple[LLMRoute, ...]] = {}
for _route in _GENERATED_ROUTES:
    ROUTE_CANDIDATES.setdefault(_route.model, tuple())
    ROUTE_CANDIDATES[_route.model] += (_route,)

ROUTES: dict[str, LLMRoute] = {
    model: candidates[0] for model, candidates in ROUTE_CANDIDATES.items()
}

# Source fallback for a checkout that has not run the compiler yet.  This is intentionally kept
# below the generated catalog and only supplies the original 14 routes during local development;
# CI and packaging always compile and commit ``llm_routes.json``.
if not _GENERATED_ROUTES:
    ROUTES = {
        "gemini/gemini-3-flash-preview": LLMRoute(
            model="gemini/gemini-3-flash-preview",
            transport="direct",
            transports=("direct", "llm-dispatch"),
            free=True,
            quota=QuotaPolicy(
                rpm=5,
                rpd=20,
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
            # Independent free-tier pool from 3.1-flash-lite (separate model = separate provider
            # quota), so the tag lane can spill onto it once 3.1's per-minute/day window fills --
            # ~1000 tags/day
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
            quota=QuotaPolicy(rpm=4, tpm=250_000),
            pricing=PricingPolicy(),
            max_provider_attempts=1,
        ),
        "mistral/mistral-large-3": LLMRoute(
            model="mistral/mistral-large-3",
            transport="llm-dispatch",
            transports=("llm-dispatch",),
            free=True,
            quota=QuotaPolicy(rpm=4, tpm=250_000),
            pricing=PricingPolicy(),
            max_provider_attempts=1,
        ),
        "mistral/mistral-medium-2508": LLMRoute(
            model="mistral/mistral-medium-2508",
            # Production agenda extraction is submitted through the shared deferred Worker so a
            # GitHub runner never holds an Mistral pacing sleep and the same job registry can
            # retry or finalize it later.
            transport="llm-dispatch",
            transports=("llm-dispatch",),
            free=True,
            quota=QuotaPolicy(rpm=22, tpm=356_250),
            pricing=PricingPolicy(),
            max_provider_attempts=1,
        ),
        "kilo/stepfun/step-3.7-flash:free": LLMRoute(
            model="kilo/stepfun/step-3.7-flash:free",
            transport="llm-dispatch",
            transports=("llm-dispatch",),
            free=True,
            quota=QuotaPolicy(rpm=20, rpd=200, tpm=100_000),
            pricing=PricingPolicy(),
            max_provider_attempts=1,
        ),
        "kilo/nvidia/nemotron-3-ultra-550b-a55b:free": LLMRoute(
            model="kilo/nvidia/nemotron-3-ultra-550b-a55b:free",
            transport="llm-dispatch",
            transports=("llm-dispatch",),
            free=True,
            quota=QuotaPolicy(rpm=20, rpd=200, tpm=100_000),
            pricing=PricingPolicy(),
            max_provider_attempts=1,
        ),
        "opencode/deepseek-v4-flash-free": LLMRoute(
            model="opencode/deepseek-v4-flash-free",
            transport="llm-dispatch",
            transports=("llm-dispatch",),
            free=True,
            quota=QuotaPolicy(rpm=30, rpd=500, tpm=250_000),
            pricing=PricingPolicy(),
            max_provider_attempts=1,
        ),
        "opencode/mimo-v2.5-free": LLMRoute(
            model="opencode/mimo-v2.5-free",
            transport="llm-dispatch",
            transports=("llm-dispatch",),
            free=True,
            quota=QuotaPolicy(rpm=30, rpd=500, tpm=100_000),
            pricing=PricingPolicy(),
            max_provider_attempts=1,
        ),
        "opencode/longcat-2.0-free": LLMRoute(
            model="opencode/longcat-2.0-free",
            transport="llm-dispatch",
            transports=("llm-dispatch",),
            free=True,
            quota=QuotaPolicy(rpm=20, rpd=200, tpm=100_000),
            pricing=PricingPolicy(),
            max_provider_attempts=1,
        ),
        "opencode/nemotron-3-ultra-free": LLMRoute(
            model="opencode/nemotron-3-ultra-free",
            transport="llm-dispatch",
            transports=("llm-dispatch",),
            free=True,
            quota=QuotaPolicy(rpm=20, rpd=200, tpm=100_000),
            pricing=PricingPolicy(),
            max_provider_attempts=1,
        ),
    }
    ROUTE_REGISTRY = {route.model: route for route in ROUTES.values()}
    ROUTE_CANDIDATES = {model: (route,) for model, route in ROUTES.items()}


def estimate_tokens(messages: list[Mapping[str, Any]]) -> int:
    """Estimate input tokens conservatively from message content."""
    characters = sum(len(str(message.get("content", ""))) for message in messages)
    return math.ceil(characters / 4)


__all__ = [
    "DEFAULT_OUTPUT_TOKEN_MARGIN",
    "DeferredLLMRequest",
    "LLMRequestPolicy",
    "LLMRoute",
    "MODEL_ALIASES",
    "PeakWindow",
    "PricingPeriod",
    "PricingPolicy",
    "QuotaPolicy",
    "ROUTES",
    "ROUTE_CANDIDATES",
    "ROUTE_REGISTRY",
    "canonical_model",
    "estimate_tokens",
]
