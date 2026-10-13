"""Groq (free account: every chat model it lists is a free-route candidate).

Live evidence 2026-09-24: a model Groq withdrew returns 404 "does not exist or you do not have
access" and is gone from `/models` -- on this all-free account that is a retirement. Responses
carry `x-ratelimit-limit-requests` (per day) and `x-ratelimit-limit-tokens` (per minute).
"""

import re

from citypods.provider_catalog.evidence import parse_documented_token_rate_headers
from citypods.provider_catalog.rules import (
    MODEL_DOES_NOT_EXIST,
    ProviderRules,
    account_level,
    chat_context_observation,
    chat_discovery_observation,
    strict_context_json,
)


def context_rate_diagnostics(headers):
    """Groq documents these token-rate and Retry-After response headers."""
    return parse_documented_token_rate_headers(headers)


def context_rate_limit_request_tokens(response):
    """Parse Groq's explicit per-request TPM rejection, never generic error numbers."""
    if response.context_diagnostics.get("provider_error_class") != "rate_limited":
        return None
    try:
        payload = strict_context_json(response.body)
    except (TypeError, ValueError):
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return None
    if error.get("type") != "tokens" or error.get("code") != "rate_limit_exceeded":
        return None
    message = error.get("message")
    if not isinstance(message, str) or len(message) > 4096:
        return None
    number = r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)"
    match = re.search(
        rf"tokens per minute \(TPM\): Limit ({number}), Requested ({number})(?![0-9]|,[0-9])",
        message,
        re.IGNORECASE,
    )
    if not match:
        return None
    try:
        limit, requested = (int(value.replace(",", "")) for value in match.groups())
    except ValueError:
        return None
    header_limit = response.context_diagnostics.get("rate_limit_limit_tokens")
    if type(header_limit) is not int or header_limit != limit or not limit < requested <= 2**53 - 1:
        return None
    return requested


def context_observation(response, request):
    requested_tokens = context_rate_limit_request_tokens(response)
    return chat_context_observation(
        response,
        request,
        rate_limit_requested_tokens=requested_tokens,
        rate_limit_parser_version=(
            "groq-tpm-rejection-v1" if requested_tokens is not None else "not_recorded"
        ),
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


def discovery_observation(response, request, transient):
    return chat_discovery_observation(response, request, transient, reasoning_basis="unknown")


RULES = ProviderRules(
    discovery_axes=("input",),
    discovery_parser_version="groq-discovery-chat-v2",
    discovery_observation=discovery_observation,
    # https://console.groq.com/docs/api-reference (chat usage; reasoning remains unknown).
    context_observation=context_observation,
    context_rate_diagnostics=context_rate_diagnostics,
    name="groq",
    free_evidence=account_level(),
    signals=(MODEL_DOES_NOT_EXIST,),
    limit_observations=limit_observations,
)
