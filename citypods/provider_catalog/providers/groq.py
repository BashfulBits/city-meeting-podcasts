"""Groq (free account: every chat model it lists is a free-route candidate).

Live evidence 2026-09-24: a model Groq withdrew returns 404 "does not exist or you do not have
access" and is gone from `/models` -- on this all-free account that is a retirement. Responses
carry `x-ratelimit-limit-requests` (per day) and `x-ratelimit-limit-tokens` (per minute).
"""

from citypods.provider_catalog.rules import (
    MODEL_DOES_NOT_EXIST,
    ProviderRules,
    account_level,
    chat_context_observation,
)


def limit_observations(response):
    """Groq's documented header units; never interpret daily requests as RPM.

    These describe the requested model on an organization account. The caller must verify its
    configured physical model/account scope before binding a route; candidates remain advisory.
    https://console.groq.com/docs/rate-limits
    """
    if response.status != 200:
        return ()
    result = []
    for header, metric in (
        ("x-ratelimit-limit-requests", "rpd"),
        ("x-ratelimit-limit-tokens", "tpm"),
    ):
        value = response.header(header)
        if isinstance(value, str) and value.isascii() and value.isdigit():
            result.append((metric, int(value), "route"))
    return tuple(result)


RULES = ProviderRules(
    # https://console.groq.com/docs/api-reference (chat usage; reasoning remains unknown).
    context_observation=chat_context_observation,
    name="groq",
    free_evidence=account_level(),
    signals=(MODEL_DOES_NOT_EXIST,),
    limit_observations=limit_observations,
)
