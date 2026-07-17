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
    deadline_at: datetime | None = None
    purpose: str = ""


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
    transport: Literal["direct", "mistral-dispatch"]
    free: bool
    quota: QuotaPolicy
    pricing: PricingPolicy


_DEEPSEEK_WINDOW = PeakWindow("UTC", time(16, 30), time(0, 30), 0.5)

ROUTES: dict[str, LLMRoute] = {
    "gemini/gemini-3-flash-preview": LLMRoute(
        model="gemini/gemini-3-flash-preview",
        transport="direct",
        free=True,
        quota=QuotaPolicy(
            rpm=10,
            rpd=1500,
            tpm=250_000,
            reset_timezone="America/Los_Angeles",
        ),
        pricing=PricingPolicy(),
    ),
    "deepseek/deepseek-v4-flash": LLMRoute(
        model="deepseek/deepseek-v4-flash",
        transport="direct",
        free=False,
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
        free=False,
        quota=QuotaPolicy(),
        pricing=PricingPolicy(
            input_per_token=0.435e-6,
            output_per_token=0.87e-6,
            windows=(_DEEPSEEK_WINDOW,),
        ),
    ),
    "mistral/mistral-large-latest": LLMRoute(
        model="mistral/mistral-large-latest",
        transport="mistral-dispatch",
        free=True,
        quota=QuotaPolicy(rpm=2),
        pricing=PricingPolicy(),
    ),
    "mistral/mistral-large-3": LLMRoute(
        model="mistral/mistral-large-3",
        transport="mistral-dispatch",
        free=True,
        quota=QuotaPolicy(rpm=2),
        pricing=PricingPolicy(),
    ),
}


def estimate_tokens(messages: list[Mapping[str, Any]]) -> int:
    """Estimate input tokens conservatively from message content."""
    characters = sum(len(str(message.get("content", ""))) for message in messages)
    return math.ceil(characters / 4)


__all__ = [
    "DEFAULT_OUTPUT_TOKEN_MARGIN",
    "LLMRequestPolicy",
    "LLMRoute",
    "PeakWindow",
    "PricingPolicy",
    "QuotaPolicy",
    "ROUTES",
    "estimate_tokens",
]
