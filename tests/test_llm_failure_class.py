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
