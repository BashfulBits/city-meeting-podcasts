"""Provider plugins classify real recorded responses as the evidence established (review/48).

The fixture was recorded 2026-09-24 under provider-scoped v2 dispatch pauses (Slice 1 step 0):
statuses, rate-limit headers and <=160-char error snippets. Every non-2xx row is pinned to the
verdict that evidence supports, so a plugin edit that changes a documented meaning fails here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from citypods.provider_catalog.classify import classify
from citypods.provider_catalog.probe import _first_event_error
from citypods.provider_catalog.registry import all_rules
from citypods.provider_catalog.rules import Response

FIXTURE = Path(__file__).parent / "fixtures" / "provider_catalog" / "evidence_2026_09_24.json"
EVIDENCE = json.loads(FIXTURE.read_text(encoding="utf-8"))["providers"]

# (provider, model) -> verdict, for every failed canary in the recording.
EXPECTED = {
    ("airforce", "codestral-latest"): "inconclusive",  # 503 every channel unavailable
    ("airforce", "kimi-k2.7-code"): "not_entitled",  # 402 subscription required
    ("airforce", "ministral-14b-2512"): "inconclusive",
    ("airforce", "ministral-14b-latest"): "quota_exhausted",  # 429 global 1 rps
    ("airforce", "nano-banana-2"): "not_entitled",
    ("airforce", "kimi-k3"): "not_entitled",
    ("gemini", "gemini-3.8-flash"): "inconclusive",  # 503 high demand
    ("gemini", "gemini-2.5-pro"): "retired",  # 404 no longer available
    ("gemini", "gemini-2.5-flash"): "retired",
    ("gemini", "gemini-pro-latest"): "not_entitled",  # free-tier quotas with no value
    ("gemini", "gemini-3.1-pro-preview"): "not_entitled",
    ("gemini", "gemini-flash-latest"): "inconclusive",
    ("groq", "qwen/qwen3.6-27b"): "retired",  # 404 does not exist (and gone from /models)
    ("kilo", "z-ai/glm-5.2:free"): "inconclusive",  # 429 upstream rate-limited
    ("mistral", "devstral-2512"): "account_blocked",  # 429 + limit-req-minute 0
    ("mistral", "mistral-small-2603"): "account_blocked",
    ("mistral", "mistral-medium-latest"): "account_blocked",
    ("mistral", "mistral-medium"): "account_blocked",
    ("mistral", "mistral-vibe-cli-fast"): "account_blocked",
    ("mistral", "mistral-large-2512"): "not_entitled",  # 403 tier_not_allowed
    ("mistral", "labs-leanstral-1-5-1"): "account_blocked",  # 403 Labs toggle
    ("nvidia", "deepseek-ai/deepseek-v4.1-flash"): "inconclusive",  # 504 after 302 s
    ("nvidia", "nvidia/llama-3.1-nemoguard-8b-topic-control"): "inconclusive",  # 502
    ("nvidia", "nvidia/llama-nemotron-embed-vl-1b-v2"): "inconclusive",  # bare 404 page
    ("nvidia", "nvidia/nemotron-parse"): "inconclusive",  # 400 no text input
    ("openrouter", "thinkingmachines/inkling:free"): "not_entitled",  # agentic-only
    ("openrouter", "poolside/laguna-xs-2.1:free"): "inconclusive",
    ("sambanova", "DeepSeek-V3.2"): "inconclusive",  # 429 high demand
    ("sambanova", "gpt-oss-120b"): "inconclusive",
    ("zai", "glm-4.7-flash"): "inconclusive",  # 1305 overloaded
    ("zai", "glm-4.5"): "not_entitled",  # 1113 insufficient balance
    ("zai", "glm-4.5-air"): "not_entitled",
    ("zai", "glm-4.6"): "not_entitled",
}


def _response(row: dict) -> Response:
    body = row.get("body", "")
    if row.get("quota_violations"):
        # The snippet is truncated; rebuild the structured part Gemini's classifier reads.
        body = json.dumps(
            [
                {
                    "error": {
                        "code": row["status"],
                        "details": [{"violations": row["quota_violations"]}],
                    }
                }
            ]
        )
    return Response(status=row["status"], headers=row.get("headers") or {}, body=body)


def _rows():
    for provider, record in sorted(EVIDENCE.items()):
        for row in record.get("canaries") or []:
            if "status" in row:
                yield provider, row


@pytest.mark.parametrize(
    ("provider", "row"), list(_rows()), ids=lambda v: v if isinstance(v, str) else v["model"]
)
def test_recorded_response_classifies_as_the_evidence_established(provider, row):
    rules = all_rules()[provider]
    result = classify(_response(row), rules)
    if row["status"] is not None and 200 <= row["status"] < 300 and not row["first_event_error"]:
        assert result.verdict == "proven"
    elif row["status"] is not None and row["status"] >= 300:
        expected = "not_served" if row["group"] == "no_card" else EXPECTED[(provider, row["model"])]
        assert result.verdict == expected, row["body"]


def test_every_nvidia_model_without_a_build_card_is_not_served():
    rows = [r for r in EVIDENCE["nvidia"]["canaries"] if r["group"] == "no_card"]
    assert len(rows) == 8
    for row in rows:
        assert classify(_response(row), all_rules()["nvidia"]).verdict == "not_served"


def test_every_failed_recorded_canary_has_a_pinned_verdict():
    failed = {(p, r["model"]) for p, r in _rows() if r["status"] and r["status"] >= 300}
    nvidia_no_card = {
        ("nvidia", r["model"]) for r in EVIDENCE["nvidia"]["canaries"] if r["group"] == "no_card"
    }
    assert failed - nvidia_no_card == set(EXPECTED)


def test_first_event_parsing_is_structural_not_substring():
    # A 200 stream whose first event is an error is not a completion (NVIDIA, 2026-09-24).
    overloaded = 'data: {"error":{"message":"Service temporarily overloaded","code":503}}\n\n'
    assert _first_event_error(overloaded)
    # An ordinary chunk may carry `"error": null`; keep-alive comments are skipped.
    chunk = ': OPENROUTER PROCESSING\n\ndata: {"id":"x","choices":[{"delta":{}}],"error":null}\n\n'
    assert not _first_event_error(chunk)
    assert not _first_event_error("data: [DONE]\n\n")


def test_timeouts_and_transport_errors_are_never_evidence():
    for rules in all_rules().values():
        assert classify(Response(status=None, timed_out=True), rules).verdict == "inconclusive"
        assert (
            classify(Response(status=None, transport_error="ConnectionError"), rules).verdict
            == "inconclusive"
        )


def test_a_200_whose_first_event_is_an_error_is_not_proven():
    response = Response(status=200, body="data: {}", first_event_error=True)
    for rules in all_rules().values():
        assert classify(response, rules).verdict != "proven"
