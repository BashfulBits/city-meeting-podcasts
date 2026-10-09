"""Offline review/48 context evidence: actual counts, strict bases and bounded search."""

import json
from dataclasses import asdict, replace
from fractions import Fraction
from pathlib import Path

import pytest
import yaml

from citypods.provider_catalog.evidence import ContextObservation, context_identity
from citypods.provider_catalog.limits import (
    ContextSearchState,
    advance_context_state,
    context_input_ratio,
    next_context_probe,
    plan_context_scan,
)
from citypods.provider_catalog.probe import build_context_request
from citypods.provider_catalog.registry import rules_for
from citypods.provider_catalog.rules import Response

ROUTE = {
    "route_id": "r",
    "provider": "groq",
    "account_id": "primary",
    "upstream_model": "test",
    "free": True,
    "rpd": 100,
    "input_context_limit": 10000,
    "output_context_limit": 2000,
}
PROVIDER = {"api_base": "https://api.example.test/v1", "chat_path": "/chat/completions"}
BUDGET = {"remaining_input": 2097152, "remaining_output": 131072, "remaining_requests": 24}


def request(provider="groq", dimension="input", target=1000, ratio=Fraction(1)):
    return build_context_request(
        {**ROUTE, "provider": provider},
        PROVIDER,
        rules_for(provider),
        dimension=dimension,
        target=target,
        ratio=ratio,
        attempt_ordinal=1,
        nonce="a" * 64,
    )


def envelope(req, *, content=None, finish="stop", prompt=900, completion=50):
    if content is None:
        import re

        content = json.dumps(
            dict(
                re.findall(
                    r"(start|middle|tail)_sentinel=([a-f0-9]{32})", req.messages[0]["content"]
                )
            )
        )
    return {
        "object": "chat.completion",
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        },
        "choices": [{"finish_reason": finish, "message": {"content": content}}],
    }


def parse(req, value):
    return rules_for(req.provider).context_observation(Response(200, body=json.dumps(value)), req)


def success(count=900, **changes):
    req = request(target=10000)
    return replace(
        ContextObservation.from_request(req),
        outcome="verified",
        count_basis="input",
        reported_input=count,
        reported_output=50,
        evidence_kind="processed_input",
        observed_at="2026-10-09T15:00:00+00:00",
        run_id="1",
        **changes,
    )


def rejection(count, **changes):
    return replace(
        success(),
        outcome="inconclusive",
        evidence_kind="size_rejection",
        reported_ceiling=count,
        **changes,
    )


def state(observation=None):
    return ContextSearchState(request().identity_digest, "input", success=observation)


def test_chat_success_uses_actual_counts_and_leaves_no_payload_in_observation():
    req = request()
    observed = parse(req, envelope(req))
    assert observed.outcome == "verified"
    assert observed.reported_input == 900
    assert observed.local_input_estimate != 900
    serialized = json.dumps(asdict(observed))
    assert "messages" not in serialized and "sentinel=" not in serialized
    assert req.messages[0]["content"] not in serialized


@pytest.mark.parametrize("count", [True, -1, 0.5, "900", 2**53, None])
def test_chat_rejects_non_integer_and_unsafe_counts(count):
    req = request()
    value = envelope(req)
    value["usage"]["prompt_tokens"] = count
    assert parse(req, value).outcome == "unsupported"


@pytest.mark.parametrize(
    "mutation",
    ["total", "native", "jev", "refusal", "tail", "finish", "reasoning", "multiple", "tool"],
)
def test_malformed_and_inconclusive_envelopes_do_not_prove_capacity(mutation):
    req = request()
    value = envelope(req)
    if mutation == "total":
        value["usage"]["total_tokens"] += 1
    if mutation == "native":
        value = {"usageMetadata": {"promptTokenCount": 1000}}
    if mutation == "jev":
        value = {"systemone": {"usage": {"prompt_tokens": 1000}}}
    if mutation == "refusal":
        value["choices"][0]["message"]["refusal"] = "refused"
    if mutation == "tail":
        value["choices"][0]["message"]["content"] = '{"start":"wrong"}'
    if mutation == "finish":
        value["choices"][0]["finish_reason"] = None
    if mutation == "reasoning":
        value["usage"]["completion_tokens_details"] = {"reasoning_tokens": 51}
    if mutation == "multiple":
        value["choices"] *= 2
    if mutation == "tool":
        value["choices"][0]["message"]["tool_calls"] = [{"name": "anything"}]
    assert parse(req, value).outcome != "verified"


def test_unknown_reasoning_permits_input_but_not_output_and_eos_is_inconclusive():
    req = request(dimension="output", target=100)
    value = envelope(req, content="1\n2\n3", finish="length", prompt=30, completion=100)
    assert parse(req, value).outcome == "inconclusive"
    req = request(provider="openrouter", dimension="output", target=100)
    assert parse(req, value).outcome == "verified"
    value["choices"][0]["finish_reason"] = "stop"
    assert parse(req, value).outcome == "inconclusive"
    value["choices"][0]["finish_reason"] = "length"
    value["choices"][0]["message"]["content"] = ""
    assert parse(req, value).outcome == "inconclusive"


def test_quota_and_undocumented_error_numbers_never_establish_rejection():
    req = request()
    for code in [400, 413, 429]:
        observed = rules_for("groq").context_observation(
            Response(code, body='{"error":{"message":"maximum 10000 tokens, received 11000"}}'), req
        )
        assert observed.reported_ceiling is None
        assert observed.outcome == ("quota" if code == 429 else "inconclusive")


def test_identity_survives_cap_and_rate_edits_but_not_gateway_or_opposite_changes():
    values = dict(
        dimension="input",
        count_basis="input",
        parser_version="chat-usage-v1",
        opposite_reservation=256,
    )
    first = context_identity(ROUTE, PROVIDER, **values)
    assert first == context_identity(
        {**ROUTE, "rpm": 999, "hard_input_ceiling": 90000, "output_context_limit": 9999},
        PROVIDER,
        **values,
    )
    assert first != context_identity(ROUTE, {**PROVIDER, "chat_path": "/changed"}, **values)
    assert first != context_identity(ROUTE, PROVIDER, **{**values, "opposite_reservation": 128})


def test_actual_counts_advance_search_and_observed_mapping_without_blanket_margin():
    observed = success()
    updated = advance_context_state(state(), observed)
    target = next_context_probe(updated, {"baseline": 1000, "opposite_reservation": 256}, BUDGET)
    assert target.next_target == 1350  # provider 900, not local estimate or reservation
    assert context_input_ratio(state(), 1) == 1
    assert context_input_ratio(replace(updated, count_pairs=((100, 123),)), 1) == Fraction(123, 100)
    assert context_input_ratio(replace(updated, count_pairs=((100, 90),)), 1.2) == Fraction(6, 5)


@pytest.mark.parametrize(
    "lower,width,expected",
    [
        (10000, 128, "converged"),
        (10000, 129, "refining"),
        (100000, 500, "converged"),
        (100000, 501, "refining"),
    ],
)
def test_refinement_uses_exact_half_percent_or_128(lower, width, expected):
    updated = advance_context_state(state(success(lower)), rejection(lower + width))
    assert updated.status == expected
    target = next_context_probe(updated, {"baseline": lower, "opposite_reservation": 256}, BUDGET)
    assert target.next_target == (
        lower * 11 // 10 if expected == "converged" else lower + width // 2
    )


def test_new_success_invalidates_historical_rejection_and_repeated_counts_are_uncertain():
    current = replace(state(success(900)), rejection=rejection(1000))
    updated = advance_context_state(current, success(1100))
    assert updated.rejection is None and updated.status == "exploring"
    assert advance_context_state(updated, success(1100)).status == "uncertain"
    assert advance_context_state(updated, rejection(1099)).status == "uncertain"
    with pytest.raises(ValueError, match="incompatible"):
        advance_context_state(current, replace(success(), identity_digest="f" * 64))


def test_budget_limited_retains_desired_target_and_does_not_fake_a_maximum():
    current = state(success(400000))
    desired = next_context_probe(current, {"baseline": 1000, "opposite_reservation": 256}, BUDGET)
    assert desired.status == "budget_limited" and desired.next_target == 600000
    assert desired.success.reported_input == 400000
    assert (
        next_context_probe(desired, {"baseline": 1000, "opposite_reservation": 256}, BUDGET)
        == desired
    )


def test_every_configured_free_route_has_supported_parser_or_explicit_unsupported():
    limits = yaml.safe_load(Path("config/provider_limits.yml").read_text())
    for route in limits["routes"]:
        if route.get("free") is not True:
            continue
        req = build_context_request(
            route,
            limits["providers"][route["provider"]],
            rules_for(route["provider"]),
            dimension="input",
            target=1000,
            ratio=Fraction(1),
            attempt_ordinal=1,
            nonce="a" * 64,
        )
        observed = parse(req, envelope(req))
        assert observed.outcome == (
            "verified" if route["provider"] in {"groq", "openrouter"} else "unsupported"
        ), route["route_id"]
        assert (
            rules_for(route["provider"])
            .context_observation(
                Response(200, body='{"usageMetadata":{"promptTokenCount":900}}'), req
            )
            .outcome
            == "unsupported"
        )
    eligible = plan_context_scan(limits, {})
    assert all(r.get("free") is True and r.get("rpd") != 0 for r in eligible)


def test_fixture_positions_ratio_and_run_nonce_are_bound_and_output_reserves_full_input():
    req = request(ratio=Fraction(123, 100))
    assert req.reserved_input <= 1000
    assert req.reserved_input == -(-(req.local_input_estimate * 123) // 100)
    content = req.messages[0]["content"]
    assert (
        content.index("start_sentinel=")
        < content.index("middle_sentinel=")
        < content.index("tail_sentinel=")
    )
    output = request(dimension="output", target=32768)
    assert output.reserved_input == 2048 and output.requested_output == 32768


def test_array_and_duplicate_json_usage_fields_fail_closed():
    req = request()
    assert parse(req, [envelope(req)]).outcome == "unsupported"
    raw = json.dumps(envelope(req)).replace(
        '"prompt_tokens": 900', '"prompt_tokens": 800, "prompt_tokens": 900'
    )
    assert (
        rules_for("groq").context_observation(Response(200, body=raw), req).outcome == "unsupported"
    )


def test_fixture_construction_cannot_allocate_above_the_reviewed_call_budget():
    with pytest.raises(ValueError, match="per-call ceiling"):
        request(target=524289)
    with pytest.raises(ValueError, match="positive mapping"):
        request(ratio=Fraction(1, 2))
