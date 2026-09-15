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
            "inputtokensperminute" in ctx["msg"]
            or "tokens per minute" in ctx["msg"]
            # Real observed shape (2026-09-13): the human-readable message never spells out
            # "tokens per minute" -- it names the machine quota metric instead, e.g. "Quota
            # exceeded for metric: generativelanguage.googleapis.com/
            # generate_content_free_tier_input_token_count, limit: 16000, model: gemma-4-26b".
            # Ordered before gemini-resource-exhausted's generic RESOURCE_EXHAUSTED->own_rpm
            # fallback, which this would otherwise fall into.
            or "input_token_count" in ctx["msg"]
            or "output_token_count" in ctx["msg"]
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
        "match": lambda ctx: (
            str(_error_dict(ctx.get("body")).get("type", "")).lower()
            in ("server_error", "missingsessionid")
            or "free tier can only be used in opencode" in ctx["msg"]
            or "missing session id" in ctx["msg"]
        ),
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
        # A rate-limit header whose LIMIT (not "remaining") is literally 0 means the provider has
        # provisioned this account no allowance at all -- an account/billing state, not pacing. No
        # amount of backoff inside the window recovers it, so it belongs on the day -> week ->
        # month cooldown ladder rather than buying a 60-second buffer and retrying forever.
        # Mistral reports exactly this, with a message that gives nothing away:
        #   429 {"message":"Rate limit exceeded","type":"rate_limited","code":"1300"}
        #   x-ratelimit-limit-req-minute: 0
        # Confirmed live 2026-09-09 while /v1/models still returned 200. Ordered before
        # "openai-shaped-rate-limit" and "remaining-zero-header", which would otherwise read the
        # response as an ordinary exhausted minute and retry a route that can never serve.
        "rule_id": "zero-provisioned-limit",
        "provider": None,
        "failure_class": "payment_required",
        "match": lambda ctx: any(
            str(name).lower().startswith(("x-ratelimit-limit", "ratelimit-limit"))
            and str(value).strip().replace(".0", "").isdigit()
            and int(float(str(value).strip())) == 0
            for name, value in (ctx.get("headers") or {}).items()
        ),
    },
    {
        "rule_id": "openai-shaped-rate-limit",
        "provider": None,
        "failure_class": "own_rpm",
        "match": lambda ctx: (
            _error_dict(ctx.get("body")).get("type") == "rate_limit_exceeded"
            or _error_dict(ctx.get("body")).get("code") == "rate_limit_exceeded"
            or (isinstance(ctx.get("body"), dict) and ctx["body"].get("type") == "rate_limited")
            or (isinstance(ctx.get("body"), dict) and str(ctx["body"].get("code")) == "1300")
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
        # A monthly/prepaid allowance being exhausted is a BILLING signal, not a pacing one -- no
        # amount of backoff inside the window recovers it, so it must reach the payment-required
        # day -> week -> month cooldown ladder rather than buying a 60-second buffer. Ordered
        # before "overloaded" because real bodies mix the vocabularies ("capacity exceeded:
        # insufficient budget"), and the billing reading is the actionable one.
        #
        # NOT "quota exceeded" alone (CodeRabbit, 2026-09-13): that bare phrase is how many
        # providers word an ordinary RPM/RPD/TPM 429, not just a billing one -- Gemini's own RPD/
        # TPM messages say exactly that ("Resource exhausted: quota exceeded for
        # GenerateRequestsPerDayPerProjectPerModel"). Those are already caught by earlier,
        # provider-scoped rules (gemini-rpd/gemini-tpm), but a provider-agnostic match on the bare
        # phrase would catch an ordinary rate 429 from any OTHER provider too, routing it onto the
        # day/week/month payment_required cooldown ladder instead of the correct short-lived
        # own_rpm/own_rpd/own_tpm backoff. Require an explicit billing/credit/monthly signal.
        "rule_id": "insufficient-budget",
        "provider": None,
        "failure_class": "payment_required",
        "match": lambda ctx: any(
            token in ctx["msg"]
            for token in (
                "insufficient budget",
                "insufficient credit",
                "insufficient balance",
                "insufficient funds",
                "monthly limit",
                "monthly quota",
                "out of credits",
                "no credits",
                "billing",
            )
        ),
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
    if str(error.get("type", "")).lower() in ("server_error", "missingsessionid"):
        return True
    msg = str(error.get("message", "")).lower()
    return any(
        p in msg
        for p in (
            "upstream request failed",
            "model is unavailable",
            "no capacity",
            "temporarily unavailable",
            "free tier can only be used in opencode",
            "missing session id",
        )
    )


def _has_rate_limit_header(headers: Mapping[str, str]) -> bool:
    for k in headers:
        k_lower = k.lower()
        # Some providers emit the newer, unprefixed standard header (RateLimit-Limit) rather than
        # the older de facto X-RateLimit-* convention. Missing it here meant a 429 using the bare
        # form fell through to the generic AI-Gateway heuristic below as if it carried no rate-
        # limit information at all, misclassifying it gateway_limit before a more specific
        # provider signature (e.g. zero-provisioned-limit) ever got a chance to match.
        if k_lower == "retry-after" or k_lower.startswith(("x-ratelimit-", "ratelimit-")):
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
    # Gemini's OpenAI-compatible endpoint wraps its error body in a JSON ARRAY -- `[{"error":
    # {...}}]` -- not a bare object. Mirrors the same unwrap in classify.js: without it, a
    # completely genuine Gemini 429 quota exhaustion falls through every dict-shaped check below
    # and is misclassified.
    if isinstance(body, list) and len(body) == 1 and isinstance(body[0], dict):
        body = body[0]
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
        and not (
            isinstance(body, dict)
            and any(k in body for k in ("error", "message", "detail", "code"))
        )
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
    # A "too large" status whose body actually reports a RATE limit, not a size limit. Groq
    # returns 413 for a per-minute token throttle; classified as request_defect it failed the job
    # terminally even though the same model accepted ~3,600 tokens seconds earlier (confirmed live
    # 2026-09-09). A plain 413 with no rate-limit language is a genuine oversized request and
    # still falls through to request_defect.
    if status in (400, 413):
        msg = str(
            _error_dict(body).get("message")
            or (body.get("message") if isinstance(body, dict) else "")
            or (body if isinstance(body, str) else "")
        ).lower()
        if msg:
            if any(
                token in msg
                for token in ("tokens per minute", "token per minute", "(tpm)", "(itpm)")
            ):
                return FailureClassification(
                    failure_class="own_tpm",
                    rule_id="size-status-token-rate-limit",
                    retry_after_seconds=retry_after_seconds,
                    scope="route",
                )
            if "requests per minute" in msg or "(rpm)" in msg:
                return FailureClassification(
                    failure_class="own_rpm",
                    rule_id="size-status-request-rate-limit",
                    retry_after_seconds=retry_after_seconds,
                    scope="route",
                )
            # A daily quota resets on the provider's own calendar day, not a minute-scale bucket
            # -- own_tpm's pacing would retry every ~60s for the rest of the day for nothing.
            # Matches the existing groq-tpd rule's own_rpd classification for the same axis
            # (CodeRabbit, 2026-09-13: this branch originally lumped "tokens per day" in with the
            # per-minute cases above).
            if (
                "requests per day" in msg
                or "(rpd)" in msg
                or "tokens per day" in msg
                or "(tpd)" in msg
            ):
                return FailureClassification(
                    failure_class="own_rpd",
                    rule_id="size-status-daily-rate-limit",
                    retry_after_seconds=retry_after_seconds,
                    scope="route",
                )

    return FailureClassification(
        failure_class="request_defect",
        rule_id="http-4xx",
        retry_after_seconds=retry_after_seconds,
        scope="route",
    )
