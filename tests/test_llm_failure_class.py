import re
from pathlib import Path

from citypods.compute.llm_failure_class import (
    FAILURE_SIGNATURES,
    classify_provider_failure,
)


def test_rule_ids_match_worker_table():
    """Drift guard: assert JS and Python rule tables match in exact order and rule_id strings."""
    worker_classify_path = (
        Path(__file__).resolve().parent.parent / "workers/llm-dispatch-v2/src/classify.js"
    )
    assert worker_classify_path.exists(), f"Missing {worker_classify_path}"

    content = worker_classify_path.read_text(encoding="utf-8")
    # Match rule_id inside FAILURE_SIGNATURES block (closing bracket is at the start of a line)
    match = re.search(r"export const FAILURE_SIGNATURES\s*=\s*\[([\s\S]*?)\n\];", content)
    assert match is not None, "Could not find FAILURE_SIGNATURES array in classify.js"

    array_body = match.group(1)
    js_rule_ids = re.findall(r'rule_id:\s*["\']([^"\']+)["\']', array_body)

    python_rule_ids = [rule["rule_id"] for rule in FAILURE_SIGNATURES]

    assert js_rule_ids == python_rule_ids, (
        f"Drift detected between classify.js and llm_failure_class.py!\n"
        f"JS:     {js_rule_ids}\n"
        f"Python: {python_rule_ids}"
    )


def test_classify_http_402():
    res = classify_provider_failure(status=402, body={"error": "payment needed"})
    assert res.failure_class == "payment_required"
    assert res.rule_id == "http-402"
    assert res.scope == "route"


def test_classify_http_5xx():
    res = classify_provider_failure(status=502, retry_after_seconds=15)
    assert res.failure_class == "server_error"
    assert res.rule_id == "http-5xx"
    assert res.retry_after_seconds == 15
    assert res.scope == "route"


def test_classify_upstream_400():
    body = {
        "error": {
            "type": "server_error",
            "message": "Upstream request failed: Model is unavailable.",
        }
    }
    res = classify_provider_failure(status=400, body=body)
    assert res.failure_class == "upstream_capacity"
    assert res.rule_id == "upstream-400-body"
    assert res.scope == "route"


def test_classify_cf_aig_error():
    res1 = classify_provider_failure(status=429, headers={"cf-aig-error": "rate_limit"})
    assert res1.failure_class == "gateway_limit"
    assert res1.rule_id == "cf-aig"
    assert res1.scope == "provider"

    res2 = classify_provider_failure(status=429, body="Gateway rate limit reached", headers={})
    assert res2.failure_class == "gateway_limit"
    assert res2.rule_id == "cf-aig"
    assert res2.scope == "provider"


def test_classify_gemini_signatures():
    # gemini-rpd
    rpd = classify_provider_failure(
        status=429,
        body={"error": {"message": "GenerateRequestsPerDayPerProjectPerModel exceeded"}},
        route={"provider": "gemini"},
    )
    assert rpd.failure_class == "own_rpd"
    assert rpd.rule_id == "gemini-rpd"

    # gemini-tpm
    tpm = classify_provider_failure(
        status=429,
        body={"error": {"message": "InputTokensPerMinute limit exceeded"}},
        route={"provider": "gemini"},
    )
    assert tpm.failure_class == "own_tpm"
    assert tpm.rule_id == "gemini-tpm"

    # gemini-rpm
    rpm = classify_provider_failure(
        status=429,
        body={"error": {"message": "Requests per minute limit exceeded"}},
        route={"provider": "gemini"},
    )
    assert rpm.failure_class == "own_rpm"
    assert rpm.rule_id == "gemini-rpm"

    # gemini-resource-exhausted fallback
    fallback = classify_provider_failure(
        status=429,
        body={"error": {"status": "RESOURCE_EXHAUSTED", "message": "Unknown"}},
        route={"provider": "gemini"},
    )
    assert fallback.failure_class == "own_rpm"
    assert fallback.rule_id == "gemini-resource-exhausted"

    # ordering precedence: gemini-rpd beats gemini-resource-exhausted
    both = classify_provider_failure(
        status=429,
        body={"error": {"status": "RESOURCE_EXHAUSTED", "message": "requests per day exceeded"}},
        route={"provider": "gemini"},
    )
    assert both.failure_class == "own_rpd"
    assert both.rule_id == "gemini-rpd"


def test_classify_groq_signatures():
    tpd = classify_provider_failure(
        status=429,
        body={"error": {"code": "rate_limit_exceeded", "message": "Tokens per day limit reached"}},
        route={"provider": "groq"},
    )
    assert tpd.failure_class == "own_rpd"
    assert tpd.rule_id == "groq-tpd"

    rpm = classify_provider_failure(
        status=429,
        body={"error": {"code": "rate_limit_exceeded", "message": "Rate limit exceeded"}},
        route={"provider": "groq"},
    )
    assert rpm.failure_class == "own_rpm"
    assert rpm.rule_id == "groq-rate-limit"


def test_classify_other_provider_signatures():
    airforce = classify_provider_failure(
        status=429,
        body={"error": {"message": "Guaranteed response in 30 seconds"}},
        route={"provider": "airforce"},
    )
    assert airforce.failure_class == "upstream_capacity"
    assert airforce.rule_id == "airforce-guaranteed-response"

    opencode = classify_provider_failure(
        status=429,
        body={"error": {"type": "server_error", "message": "Over capacity"}},
        route={"provider": "opencode"},
    )
    assert opencode.failure_class == "upstream_capacity"
    assert opencode.rule_id == "opencode-server-error"

    openrouter = classify_provider_failure(
        status=429,
        body={
            "error": {
                "message": "Provider returned error",
                "metadata": {
                    "limit_source": "upstream_provider_shared_pool",
                    "raw": "google/gemma is temporarily rate-limited upstream",
                },
            }
        },
        route={"provider": "openrouter"},
    )
    assert openrouter.failure_class == "upstream_capacity"
    assert openrouter.rule_id == "openrouter-upstream"


def test_classify_catchall_signatures():
    openai_rpm = classify_provider_failure(
        status=429,
        body={"error": {"type": "rate_limit_exceeded", "message": "Too fast"}},
        route={"provider": "mistral"},
    )
    assert openai_rpm.failure_class == "own_rpm"
    assert openai_rpm.rule_id == "openai-shaped-rate-limit"

    hdr_rpm = classify_provider_failure(
        status=429,
        body={"error": {"message": "Limit"}},
        headers={"x-ratelimit-remaining-requests": "0"},
        route={"provider": "cerebras"},
    )
    assert hdr_rpm.failure_class == "own_rpm"
    assert hdr_rpm.rule_id == "remaining-zero-header"

    hdr_min_rpm = classify_provider_failure(
        status=429,
        body={"error": {"message": "Limit"}},
        headers={"x-ratelimit-remaining-req-minute": "0"},
        route={"provider": "mistral"},
    )
    assert hdr_min_rpm.failure_class == "own_rpm"
    assert hdr_min_rpm.rule_id == "remaining-zero-header"

    hdr_tpm = classify_provider_failure(
        status=429,
        body={"error": {"message": "Limit"}},
        headers={"x-ratelimit-remaining-tokens": "0"},
        route={"provider": "cerebras"},
    )
    assert hdr_tpm.failure_class == "own_tpm"
    assert hdr_tpm.rule_id == "remaining-tokens-zero-header"

    overloaded = classify_provider_failure(
        status=429,
        body={"error": {"message": "Model is temporarily unavailable"}},
        route={"provider": "nvidia"},
    )
    assert overloaded.failure_class == "upstream_capacity"
    assert overloaded.rule_id == "overloaded"

    concurrency = classify_provider_failure(
        status=429,
        body={"error": {"message": "Too many concurrent requests"}},
        route={"provider": "kilo"},
    )
    assert concurrency.failure_class == "upstream_capacity"
    assert concurrency.rule_id == "concurrency"


def test_classify_unmatched_and_route_default():
    unmatched = classify_provider_failure(
        status=429,
        body={"error": {"message": "unrecognized"}},
        route={"provider": "unknown"},
    )
    assert unmatched.failure_class == "unknown_429"
    assert unmatched.rule_id == "unmatched-429"

    defaulted = classify_provider_failure(
        status=429,
        body={"error": {"message": "unrecognized"}},
        route={"provider": "unknown", "upstream_429_default": "upstream_capacity"},
    )
    assert defaulted.failure_class == "upstream_capacity"
    assert defaulted.rule_id == "route-default-upstream"


def test_classify_generic_4xx():
    res = classify_provider_failure(status=404, body={"error": "Not Found"})
    assert res.failure_class == "request_defect"
    assert res.rule_id == "http-4xx"
    assert res.scope == "route"


def test_zero_provisioned_limit_is_billing_not_pacing():
    """Mistral reports an account with no provisioned allowance as a plain 429 whose message says
    nothing ("Rate limit exceeded", type rate_limited, code 1300) -- the only tell is
    `x-ratelimit-limit-req-minute: 0`, the LIMIT rather than the remaining. Read as own_rpm it
    bought a 60s buffer and retried forever against a route that can never serve a request;
    21,287 jobs were queued behind exactly this on 2026-09-09 while /v1/models still returned 200.
    """
    result = classify_provider_failure(
        status=429,
        body={"message": "Rate limit exceeded", "type": "rate_limited", "code": "1300"},
        headers={"x-ratelimit-limit-req-minute": "0", "x-ratelimit-remaining-req-minute": "0"},
        route={"provider": "mistral"},
    )
    assert result.failure_class == "payment_required"
    assert result.rule_id == "zero-provisioned-limit"


def test_ordinary_exhaustion_is_still_pacing_not_billing():
    """The mirror case: a real limit that happens to be spent is our own pacing, and must keep the
    short buffer rather than the month-long billing ladder."""
    result = classify_provider_failure(
        status=429,
        body={"error": {"message": "slow down"}},
        headers={"x-ratelimit-limit-requests": "1000", "x-ratelimit-remaining-requests": "0"},
        route={"provider": "groq"},
    )
    assert result.failure_class == "own_rpm"
    assert result.rule_id == "remaining-zero-header"


def test_bare_ratelimit_limit_header_is_still_zero_provisioned_not_gateway_limit():
    """Some providers emit the newer, unprefixed standard header instead of the older de facto
    X-RateLimit-* convention. Before _has_rate_limit_header recognized it, this shape fell through
    to the generic AI-Gateway heuristic (no known rate-limit header + no body `error` key) and was
    misclassified gateway_limit -- which fans out a cooldown to every sibling route on the
    provider, not just the one whose own zero allowance was actually the problem."""
    result = classify_provider_failure(
        status=429,
        body={"message": "Rate limit exceeded"},
        headers={"ratelimit-limit": "0"},
        route={"provider": "mistral"},
    )
    assert result.failure_class == "payment_required"
    assert result.rule_id == "zero-provisioned-limit"


def test_bare_quota_exceeded_from_non_gemini_provider_stays_own_rpm_not_billing():
    """insufficient-budget used to match the bare phrase "quota exceeded" for any provider.
    Gemini's own RPD/TPM messages say exactly that ("Resource exhausted: quota exceeded for
    GenerateRequestsPerDayPerProjectPerModel"), and are correctly caught by earlier, gemini-scoped
    rules -- but a provider-agnostic match would have routed an ordinary rate 429 from any OTHER
    provider onto the day/week/month payment_required cooldown ladder instead of the correct
    short-lived own_rpm backoff."""
    result = classify_provider_failure(
        status=429,
        body={"error": {"message": "quota exceeded, please slow down"}},
        headers=None,
        route={"provider": "some-other-provider"},
    )
    assert result.failure_class != "payment_required"


def test_size_status_daily_token_quota_is_own_rpd_not_own_tpm():
    """ "tokens per day"/"(tpd)" used to be lumped into the same branch as the per-minute cases,
    applying own_tpm's ~60s bucket-wait pacing to a quota that only resets on the provider's
    calendar day -- matching the existing groq-tpd rule's own_rpd classification for the same
    axis elsewhere in this file."""
    result = classify_provider_failure(
        status=413,
        body={"error": {"message": "Request too large: exceeds tokens per day (TPD) limit"}},
        headers=None,
        route={"provider": "groq"},
    )
    assert result.failure_class == "own_rpd"
    assert result.rule_id == "size-status-daily-rate-limit"


def test_gemini_array_wrapped_body_is_unwrapped_before_classification():
    """Gemini's OpenAI-compatible endpoint wraps its error body in a JSON ARRAY
    (`[{"error": {...}}]`), not a bare object. Confirmed live 2026-09-13 during an endurance
    ceiling probe: this genuine, real quota exhaustion on gemma-4-26b/31b (a per-model,
    per-account token quota, nothing to do with Cloudflare's AI Gateway -- the probe calls
    Gemini directly, never touching the Gateway) was falling through every dict-shaped check and
    landing on the isAig429 fallback as gateway_limit. In production that misclassification would
    incorrectly cool down every OTHER Gemini route sharing the account, not just the one model
    whose own quota was exhausted."""
    body = [
        {
            "error": {
                "code": 429,
                "message": (
                    "You exceeded your current quota, please check your plan and billing "
                    "details. Quota exceeded for metric: generativelanguage.googleapis.com/"
                    "generate_content_free_tier_input_token_count, limit: 16000, "
                    "model: gemma-4-26b\nPlease retry in 31.44s."
                ),
                "status": "RESOURCE_EXHAUSTED",
            }
        }
    ]
    result = classify_provider_failure(
        status=429, body=body, headers={}, route={"provider": "gemini"}
    )
    assert result.failure_class == "own_tpm"
    assert result.rule_id == "gemini-tpm"
    # Never gateway_limit or the generic own_rpm fallback -- both would be wrong here.
    assert result.failure_class != "gateway_limit"


def test_gemini_input_token_count_quota_metric_is_own_tpm_not_generic_resource_exhausted():
    """The real message never spells out 'tokens per minute' -- it names the machine quota
    metric ('..._input_token_count') instead. Without this match, the message falls through to
    gemini-resource-exhausted's generic RESOURCE_EXHAUSTED -> own_rpm fallback, mislabeling a
    token quota as a request-count one."""
    body = {
        "error": {
            "message": (
                "Quota exceeded for metric: .../generate_content_free_tier_input_token_count, "
                "limit: 16000"
            ),
            "status": "RESOURCE_EXHAUSTED",
        }
    }
    result = classify_provider_failure(
        status=429, body=body, headers={}, route={"provider": "gemini"}
    )
    assert result.failure_class == "own_tpm"
    assert result.rule_id == "gemini-tpm"


def test_orcarouter_free_tier_prompt_cap_is_request_defect():
    """OrcaRouter free-tier returns 429 free_rate_limited without Retry-After when the prompt

    exceeds the tier cap. Retrying unchanged fails identically forever, so it is a request defect.
    """
    body = {"error": {"code": "free_rate_limited", "message": "Rate limit exceeded"}}
    res = classify_provider_failure(
        status=429, body=body, headers={}, route={"provider": "orcarouter"}
    )
    assert res.failure_class == "request_defect"
    assert res.rule_id == "orcarouter-prompt-cap"


def test_orcarouter_free_tier_rate_limits_with_retry_after():
    """OrcaRouter 429 with Retry-After maps to own_rpm (minute) or own_rpd (daily)."""
    body = {"error": {"code": "free_rate_limited", "message": "Rate limit exceeded"}}

    # Minute-scale window
    res_rpm = classify_provider_failure(
        status=429,
        body=body,
        headers={"retry-after": "45"},
        route={"provider": "orcarouter"},
    )
    assert res_rpm.failure_class == "own_rpm"
    assert res_rpm.rule_id == "orcarouter-minute-window"

    # Daily-scale window (> 120s to 00:00 UTC)
    res_rpd = classify_provider_failure(
        status=429,
        body=body,
        headers={"retry-after": "3600"},
        route={"provider": "orcarouter"},
    )
    assert res_rpd.failure_class == "own_rpd"
    assert res_rpd.rule_id == "orcarouter-daily-window"


def test_opencode_missing_session_id_is_upstream_capacity():
    """OpenCode free-tier 400 with MissingSessionID is classified as upstream_capacity."""
    body = {
        "error": {
            "type": "MissingSessionID",
            "message": (
                "Error from provider (Console): OpenCode's free tier can only be used in OpenCode"
            ),
        },
        "type": "error",
    }
    result = classify_provider_failure(
        status=400, body=body, headers={}, route={"provider": "opencode"}
    )
    assert result.failure_class == "upstream_capacity"
    assert result.rule_id == "upstream-400-body"


def test_opencode_bare_missing_session_id_is_request_defect():
    """OpenCode 400 with bare MissingSessionID without free-tier phrase is request_defect."""
    body = {
        "error": {
            "type": "MissingSessionID",
            "message": "Missing session ID in request headers",
        },
        "type": "error",
    }
    result = classify_provider_failure(
        status=400, body=body, headers={}, route={"provider": "opencode"}
    )
    assert result.failure_class == "request_defect"
    assert result.scope == "route"


def test_mistral_zero_provisioned_limit_precedence():
    """Mistral 429 with 0 req/min limit is payment_required rather than own_rpm."""
    body = {
        "message": "Rate limit exceeded",
        "type": "rate_limited",
        "code": "1300",
    }
    headers = {
        "x-ratelimit-limit-req-minute": "0",
        "x-ratelimit-remaining-req-minute": "0",
    }
    result = classify_provider_failure(
        status=429, body=body, headers=headers, route={"provider": "mistral"}
    )
    assert result.failure_class == "payment_required"
    assert result.rule_id == "zero-provisioned-limit"
