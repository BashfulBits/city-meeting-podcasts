"""One plain-language answer per new route: is it ready to add, and what happens next.

The request-cap tables in the rolling issue are evidence; this module turns the same evidence
into a single state and next step so a maintainer never has to read the tables to know what to
do. It only reads what `/apply` itself reads (`_verified_input_cap`, `_verified_output_cap`,
provider/LiteLLM output claims), so "ready" here means `/apply` will not defer for limits.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# Used only after the output search has finished without a measured value and neither the
# provider catalog nor LiteLLM publishes one. Mirrors the 4,096-token completion allowance the
# structured-output checks already use (review/48); the PR marks it provisional for review.
PROVISIONAL_OUTPUT_CONTEXT_LIMIT = 4_096

READY = "ready"
WAITING = "waiting"
ACTION = "action"

_NOT_RUN = {
    None,
    "",
    "not_selected",
    # Interrupted before a usable result: a later run must retry, not fall back to a default.
    "outage",
    "quota_deferred",
    "access_deferred",
    "pause_lost",
    "budget_exhausted",
    "deadline_or_reported_usage_overrun",
}
# Stubs stored when automatic discovery deliberately skips a candidate: nothing will measure it.
_NOT_MEASURED = {"below_automatic_quality_floor", "automatic_discovery_already_recorded"}
_SERVER_ERROR_REASONS = {"outage"}
_TIME_LIMITED = {"input_time_limited_lower_bound", "minimum_probe_too_slow"}


@dataclass(frozen=True)
class Readiness:
    state: str  # ready | waiting | action
    headline: str
    next_step: str
    input_cap: int | None = None
    output_cap: int | None = None
    output_source: str | None = None  # measured | provider | litellm | provisional


def _axis(discovery: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    return next(
        (
            row
            for row in discovery.get("axes") or []
            if isinstance(row, Mapping) and row.get("axis") == name
        ),
        {},
    )


def output_search_finished(discovery: Mapping[str, Any] | None) -> bool:
    """The output axis was actually searched (any outcome), not merely left unselected."""
    if not isinstance(discovery, Mapping):
        return False
    return _axis(discovery, "output").get("reason") not in _NOT_RUN


def output_cap_for(discovery, provider_claim=None):
    """(value, source) for the route's output_context_limit, or (None, None) if none exists yet.

    Preference order matches `/apply`: a verified measurement, the provider catalog claim, then
    LiteLLM. A provisional default applies only once the output search has run and found nothing.
    """
    from citypods.provider_catalog.apply import _verified_output_cap

    measured = _verified_output_cap(discovery)
    if measured:
        return measured, "measured"
    if type(provider_claim) is int and provider_claim > 0:
        return provider_claim, "provider"
    litellm = discovery.get("litellm") if isinstance(discovery, Mapping) else None
    litellm_output = litellm.get("output") if isinstance(litellm, Mapping) else None
    if type(litellm_output) is int and litellm_output > 0:
        return litellm_output, "litellm"
    if output_search_finished(discovery):
        return PROVISIONAL_OUTPUT_CONTEXT_LIMIT, "provisional"
    return None, None


def _tokens(value: int) -> str:
    return f"{value:,}"


def candidate_readiness(candidate) -> Readiness:
    discovery = candidate.discovery
    if not isinstance(discovery, Mapping) or not discovery:
        if candidate.below_floor:
            return Readiness(
                ACTION,
                "Limits not tested: below the quality bar, so it is not measured automatically.",
                "Tick `ignore`, or tick `add route only` if you want it anyway; `/apply` will "
                "then say what evidence is missing.",
            )
        return Readiness(
            WAITING,
            "Limits not tested yet.",
            "Nothing to do. The next daily run tests it; this line updates when it has.",
        )

    from citypods.provider_catalog.apply import _verified_input_cap

    cap = _verified_input_cap(discovery)
    input_row = _axis(discovery, "input")
    if cap:
        admitted = cap["admitted_input"]
        evidence = candidate.evidence or {}
        output, source = output_cap_for(discovery, evidence.get("output_context_limit"))
        speed = (
            " (limited by speed: larger inputs would not finish inside the 600 s budget that "
            "keeps calls under the Worker's 720 s ceiling)"
            if input_row.get("reason") in _TIME_LIMITED
            else ""
        )
        if output:
            how = {
                "measured": "measured",
                "provider": "from the provider catalog",
                "litellm": "from LiteLLM",
                "provisional": "provisional default; the output search found no value",
            }[source]
            return Readiness(
                READY,
                f"**Ready to add.** Tested input cap {_tokens(admitted)} tokens{speed}; "
                f"output cap {_tokens(output)} ({how}).",
                "Tick `add route only` (or a lane), then comment `/apply`. The PR you review "
                "carries exactly these values.",
                admitted,
                output,
                source,
            )
        return Readiness(
            WAITING,
            f"Input cap tested: {_tokens(admitted)} tokens{speed}. Output limit not tested yet.",
            "Tick `add route only` now. The next daily run measures the output limit; "
            "then comment `/apply` again (a deferral never loses your tick).",
            admitted,
        )

    reason = input_row.get("reason") or discovery.get("reason")
    if reason in _SERVER_ERROR_REASONS:
        return Readiness(
            WAITING,
            "The provider returned server errors on the input test (retried automatically).",
            "Nothing to do. The next daily run tries again.",
        )
    if reason in _TIME_LIMITED:
        return Readiness(
            ACTION,
            "Too slow: even the smallest probe did not finish inside the 600 s time budget, "
            "so no cap can keep calls under the Worker's 720 s ceiling.",
            "Tick `ignore` unless you expect the provider to speed up; `test again` re-checks.",
        )
    if reason in _NOT_MEASURED:
        return Readiness(
            ACTION,
            "Limits not tested: the daily run does not measure this model automatically.",
            "Tick `ignore`, or tick `add route only` if you want it anyway; `/apply` will "
            "then say what evidence is missing.",
        )
    if discovery.get("retryable") is True:
        return Readiness(
            ACTION,
            "Input limit not found yet (partial result).",
            "Tick `test again` to queue another bounded run, then comment `/apply`.",
        )
    return Readiness(
        WAITING,
        "Input limit not tested yet.",
        "Nothing to do. The next daily run measures found candidates.",
    )
