"""Provider failure classification taxonomy and signature matching for LLM endpoints.

Mirrors workers/llm-dispatch-v2/src/classify.js for local scripts, probe harness,
and direct transport.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


def _error_dict(body: Any) -> dict[str, Any]:
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return err
    return {}


# Ordered rule table for HTTP 429 responses. First match wins.
# Each rule is a dict: rule_id, provider, failure_class, match.
# provider: None applies to all providers.
FAILURE_SIGNATURES: list[dict[str, Any]] = [
    {
        "rule_id": "gemini-rpd",
        "provider": "gemini",
        "failure_class": "own_rpd",
        "match": lambda ctx: (
            "perday" in ctx["msg"]
            or "requests per day" in ctx["msg"]
            or "generaterequestsperdayper" in ctx["msg"]
        ),
    },
    {
        "rule_id": "gemini-tpm",
        "provider": "gemini",
        "failure_class": "own_tpm",
        "match": lambda ctx: (
            "inputtokensperminute" in ctx["msg"] or "tokens per minute" in ctx["msg"]
        ),
    },
    {
        "rule_id": "gemini-rpm",
        "provider": "gemini",
        "failure_class": "own_rpm",
        "match": lambda ctx: (
            "requests per minute" in ctx["msg"] or "generaterequestsperminuteper" in ctx["msg"]
        ),
    },
    {
        "rule_id": "gemini-resource-exhausted",
        "provider": "gemini",
        "failure_class": "own_rpm",
        "match": lambda ctx: _error_dict(ctx.get("body")).get("status") == "RESOURCE_EXHAUSTED",
    },
    {
        "rule_id": "groq-tpd",
        "provider": "groq",
        "failure_class": "own_rpd",
        "match": lambda ctx: "tokens per day" in ctx["msg"] or "requests per day" in ctx["msg"],
    },
    {
        "rule_id": "groq-rate-limit",
        "provider": "groq",
        "failure_class": "own_rpm",
        "match": lambda ctx: _error_dict(ctx.get("body")).get("code") == "rate_limit_exceeded",
    },
    {
        "rule_id": "airforce-guaranteed-response",
        "provider": "airforce",
        "failure_class": "upstream_capacity",
        "match": lambda ctx: "guaranteed response" in ctx["msg"],
    },
    {
        "rule_id": "opencode-server-error",
        "provider": "opencode",
        "failure_class": "upstream_capacity",
        "match": lambda ctx: _error_dict(ctx.get("body")).get("type") == "server_error",
    },
    {
        "rule_id": "openrouter-upstream",
        "provider": "openrouter",
        "failure_class": "upstream_capacity",
        "match": lambda ctx: (
            "provider returned error" in ctx["msg"]
            or (
                isinstance(_error_dict(ctx.get("body")).get("metadata"), dict)
                and (
                    _error_dict(ctx.get("body"))["metadata"].get("limit_source")
                    == "upstream_provider_shared_pool"
                    or "upstream"
                    in str(_error_dict(ctx.get("body"))["metadata"].get("raw", "")).lower()
                )
            )
        ),
    },
    {
        "rule_id": "openai-shaped-rate-limit",
        "provider": None,
        "failure_class": "own_rpm",
        "match": lambda ctx: (
            _error_dict(ctx.get("body")).get("type") == "rate_limit_exceeded"
            or _error_dict(ctx.get("body")).get("code") == "rate_limit_exceeded"
        ),
    },
    {
        "rule_id": "remaining-zero-header",
        "provider": None,
        "failure_class": "own_rpm",
        "match": lambda ctx: (
            ctx.get("headers", {}).get("x-ratelimit-remaining-requests") == "0"
            or ctx.get("headers", {}).get("x-ratelimit-remaining-req-minute") == "0"
        ),
    },
    {
        "rule_id": "remaining-tokens-zero-header",
        "provider": None,
        "failure_class": "own_tpm",
        "match": lambda ctx: ctx.get("headers", {}).get("x-ratelimit-remaining-tokens") == "0",
    },
    {
        "rule_id": "overloaded",
        "provider": None,
        "failure_class": "upstream_capacity",
        "match": lambda ctx: any(
            p in ctx["msg"]
            for p in (
                "overloaded",
                "no capacity",
                "capacity",
                "try again later",
                "temporarily unavailable",
                "model is unavailable",
                "upstream",
                "server busy",
                "all providers",
                "no available provider",
                "service unavailable",
            )
        ),
    },
    {
        "rule_id": "concurrency",
        "provider": None,
        "failure_class": "upstream_capacity",
        "match": lambda ctx: (
            "concurrent" in ctx["msg"] or "too many concurrent requests" in ctx["msg"]
        ),
    },
]


@dataclass(frozen=True)
class FailureClassification:
    failure_class: str
    rule_id: str
    retry_after_seconds: int | None
    scope: str

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)


def _is_upstream_400(body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    error = body.get("error")
    if not isinstance(error, dict):
        return False
    if str(error.get("type", "")).lower() == "server_error":
        return True
    msg = str(error.get("message", "")).lower()
    return any(
        p in msg
        for p in (
            "upstream request failed",
            "model is unavailable",
            "no capacity",
            "temporarily unavailable",
        )
    )


def _has_rate_limit_header(headers: Mapping[str, str]) -> bool:
    for k in headers:
        k_lower = k.lower()
        if k_lower == "retry-after" or k_lower.startswith("x-ratelimit-"):
            return True
    return False


def classify_provider_failure(
    *,
    status: int,
    body: Any = None,
    headers: Mapping[str, str] | None = None,
    route: Any = None,
    retry_after_seconds: int | None = None,
) -> FailureClassification:
    """Classify an HTTP response into the 9-class provider failure taxonomy."""
    norm_headers = {k.lower(): str(v) for k, v in (headers or {}).items()}

    # 1. HTTP 402 -> payment_required
    if status == 402:
        return FailureClassification(
            failure_class="payment_required",
            rule_id="http-402",
            retry_after_seconds=retry_after_seconds,
            scope="route",
        )

    # 2. HTTP 5xx -> server_error
    if 500 <= status <= 599:
        return FailureClassification(
            failure_class="server_error",
            rule_id="http-5xx",
            retry_after_seconds=retry_after_seconds,
            scope="route",
        )

    # 3. HTTP 400 with upstream capacity error
    if status == 400 and _is_upstream_400(body):
        return FailureClassification(
            failure_class="upstream_capacity",
            rule_id="upstream-400-body",
            retry_after_seconds=retry_after_seconds,
            scope="route",
        )

    # 4. Cloudflare AI Gateway rate limit or rejection
    has_cf_aig_error = "cf-aig-error" in norm_headers
    is_aig_429 = (
        status == 429
        and not _has_rate_limit_header(norm_headers)
        and not (isinstance(body, dict) and "error" in body)
    )
    if has_cf_aig_error or is_aig_429:
        return FailureClassification(
            failure_class="gateway_limit",
            rule_id="cf-aig",
            retry_after_seconds=retry_after_seconds,
            scope="provider",
        )

    # 5. HTTP 429 -> walk FAILURE_SIGNATURES
    if status == 429:
        raw_msg = ""
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                raw_msg = str(err.get("message") or "")
            elif isinstance(err, str):
                raw_msg = err
            if not raw_msg:
                raw_msg = str(body.get("message") or body.get("detail") or "")
        elif isinstance(body, str):
            raw_msg = body

        msg = raw_msg.lower()
        provider = ""
        if isinstance(route, dict):
            provider = route.get("provider", "")
        elif hasattr(route, "provider"):
            provider = getattr(route, "provider", "")

        match_ctx = {"status": status, "body": body, "headers": norm_headers, "msg": msg}

        for rule in FAILURE_SIGNATURES:
            rule_provider = rule["provider"]
            if rule_provider is not None and rule_provider != provider:
                continue
            match_fn: Callable[[dict[str, Any]], bool] = rule["match"]
            if match_fn(match_ctx):
                return FailureClassification(
                    failure_class=rule["failure_class"],
                    rule_id=rule["rule_id"],
                    retry_after_seconds=retry_after_seconds,
                    scope="route",
                )

        # 6. HTTP 429 unmatched -> check upstream_429_default
        upstream_default = ""
        if isinstance(route, dict):
            upstream_default = route.get("upstream_429_default", "")
        elif hasattr(route, "upstream_429_default"):
            upstream_default = getattr(route, "upstream_429_default", "")

        if upstream_default == "upstream_capacity":
            return FailureClassification(
                failure_class="upstream_capacity",
                rule_id="route-default-upstream",
                retry_after_seconds=retry_after_seconds,
                scope="route",
            )

        return FailureClassification(
            failure_class="unknown_429",
            rule_id="unmatched-429",
            retry_after_seconds=retry_after_seconds,
            scope="route",
        )

    # 7. Any other status -> request_defect
    return FailureClassification(
        failure_class="request_defect",
        rule_id="http-4xx",
        retry_after_seconds=retry_after_seconds,
        scope="route",
    )
