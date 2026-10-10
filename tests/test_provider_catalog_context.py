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


class ContextFakeResponse:
    def __init__(self, body, counter=None, *, delay=0, oversized=False):
        self.status_code = 200
        self.headers = {"Content-Type": "application/json"}
        self.body, self.counter, self.delay, self.oversized = body, counter, delay, oversized
        self.closed = False

    def iter_content(self, chunk_size):
        import time

        time.sleep(self.delay)
        yield b"x" * (4 * 1024 * 1024 + 1) if self.oversized else self.body.encode()

    def close(self):
        self.closed = True


class ContextFakeSession:
    def __init__(self, response, calls):
        self.response, self.calls = response, calls
        self.adapters = {}

    def post(self, url, **kwargs):
        assert kwargs["allow_redirects"] is False and kwargs["stream"] is True
        self.calls.value += 1
        return self.response

    def close(self):
        pass


def test_collector_enforces_bytes_closes_response_and_rejects_truncated_sse():
    import time
    from types import SimpleNamespace

    from citypods.provider_catalog.probe import _collect_context, _context_envelope

    req = request()
    response = ContextFakeResponse("", oversized=True)
    observed = _collect_context(
        ContextFakeSession(response, SimpleNamespace(value=0)), req, {}, 120, time.monotonic
    )
    assert observed.timed_out and response.closed
    with pytest.raises(ValueError, match="incomplete"):
        _context_envelope('data: {"choices":[]}\n\n', "text/event-stream")
    frames = [
        {"choices": [{"delta": {"content": "1\n2\n3"}, "finish_reason": None}]},
        {
            "choices": [{"delta": {}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 100, "total_tokens": 130},
        },
    ]
    raw = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames) + "data: [DONE]\n\n"
    normalized = _context_envelope(raw, "text/event-stream")
    assert parse(request("openrouter", "output", 100), json.loads(normalized)).outcome == "verified"
    with pytest.raises(ValueError, match="conflicting"):
        _context_envelope(
            raw.replace("data: [DONE]", f"data: {json.dumps(frames[1])}\n\ndata: [DONE]"),
            "text/event-stream",
        )


@pytest.mark.parametrize("mode", ["success", "timeout", "denied"])
def test_subprocess_requires_admission_and_cancels_slow_stream(monkeypatch, mode):
    import multiprocessing
    import time

    import citypods.security
    from citypods.compute.llm_dispatch_pause import ContextAdmission, DispatchPauseError
    from citypods.provider_catalog.probe import measure_context

    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("offline inherited fake transport requires fork")
    ctx = multiprocessing.get_context("fork")
    monkeypatch.setattr(multiprocessing, "get_context", lambda _name: ctx)
    monkeypatch.setattr(citypods.security, "validate_source_url", lambda _url: None)
    monkeypatch.setenv("CONTEXT_OFFLINE_TEST_KEY", "fake-offline-key")
    req = request()
    req = replace(req, shaped_body={**req.shaped_body, "api_key_env": "CONTEXT_OFFLINE_TEST_KEY"})
    calls = ctx.Value("i", 0)
    response = ContextFakeResponse(json.dumps(envelope(req)), delay=2 if mode == "timeout" else 0)
    started = time.monotonic()

    def before_call():
        assert calls.value == 0
        if mode == "denied":
            raise DispatchPauseError("lost admission response")
        return ContextAdmission("a" * 64)

    if mode == "denied":
        with pytest.raises(DispatchPauseError):
            measure_context(
                req,
                session=ContextFakeSession(response, calls),
                before_call=before_call,
                clock=time.monotonic,
                timeout=0.3,
            )
        assert calls.value == 0
    else:
        observed = measure_context(
            req,
            session=ContextFakeSession(response, calls),
            before_call=before_call,
            clock=time.monotonic,
            timeout=0.3,
        )
        assert calls.value == 1
        assert observed.outcome == ("verified" if mode == "success" else "transport")
    assert time.monotonic() - started < 1.5


def context_artifact_fixture():
    import io
    import zipfile
    from datetime import UTC, datetime

    from citypods.provider_catalog.evidence import (
        CONTEXT_ARTIFACT_FILE,
        RATE_WORKFLOW,
        context_artifact,
    )

    now = datetime(2026, 10, 9, 15, tzinfo=UTC)
    limits = {
        "routes": [dict(ROUTE)],
        "providers": {"groq": {**PROVIDER, "accounts": [{"id": "primary", "api_key_env": "KEY"}]}},
    }
    observed = replace(
        success(), observed_at=now.isoformat(), head_sha="a" * 40, attempt_id="b" * 64
    )
    envelope_value = context_artifact(
        [observed],
        limits,
        repository="owner/repo",
        run_id="1",
        head_sha="a" * 40,
        now=now,
        catalog_digest="c" * 64,
        attempted_routes=["r"],
    )
    run = {
        "id": 1,
        "status": "completed",
        "conclusion": "success",
        "event": "schedule",
        "workflow_id": 7,
        "head_branch": "main",
        "head_repository": {"full_name": "owner/repo"},
        "head_sha": "a" * 40,
    }
    artifact = {
        "id": 9,
        "name": "provider-catalog-context-evidence-1",
        "expired": False,
        "size_in_bytes": 2000,
    }

    def api(path):
        if path.endswith("/repo"):
            return {"full_name": "owner/repo", "default_branch": "main"}
        if "/workflows/" in path:
            return {"id": 7, "path": RATE_WORKFLOW}
        if "/artifacts?" in path:
            return {"artifacts": [artifact]}
        return run

    def download(_path):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr(CONTEXT_ARTIFACT_FILE, json.dumps(envelope_value))
        return stream.getvalue()

    reference = {"run_id": "1", "payload_digest": envelope_value["payload_digest"]}
    return now, limits, observed, envelope_value, run, artifact, reference, api, download


def test_context_history_authenticates_and_survives_caps_but_not_gateway_edits():
    from citypods.provider_catalog.evidence import verified_context_history

    now, limits, observed, _env, _run, _artifact, reference, api, download = (
        context_artifact_fixture()
    )
    kwargs = dict(
        repository="owner/repo", now=now, api=api, download=download, ancestor=lambda _: True
    )
    values, accepted, deferred = verified_context_history([reference], limits, **kwargs)
    assert values == (observed,) and accepted and not deferred
    limits["routes"][0]["hard_input_ceiling"] = 90000
    limits["routes"][0]["rpm"] = 999
    assert verified_context_history([reference], limits, **kwargs)[0] == (observed,)
    limits["providers"]["groq"]["chat_path"] = "/changed"
    assert not verified_context_history([reference], limits, **kwargs)[0]


@pytest.mark.parametrize(
    "fault",
    [
        "expired",
        "tampered",
        "ancestor",
        "failed",
        "manual",
        "fork",
        "workflow",
        "observation_time",
        "oversized",
        "bad_count",
    ],
)
def test_context_history_tampering_and_untrusted_provenance_fail_closed(fault):
    from citypods.provider_catalog.evidence import verified_context_history

    now, limits, _o, env, run, artifact, reference, api, download = context_artifact_fixture()
    if fault == "expired":
        artifact["expired"] = True
    if fault == "tampered":
        env["payload"]["observations"][0]["reported_input"] = 1
    if fault == "failed":
        run["conclusion"] = "failure"
    if fault == "manual":
        run["event"] = "workflow_dispatch"
    if fault == "fork":
        run["head_branch"] = "fork"
    if fault == "workflow":
        run["workflow_id"] = 9
    if fault == "observation_time":
        env["payload"]["observations"][0]["observed_at"] = "2025-10-09T15:00:00+00:00"
    if fault == "oversized":
        artifact["size_in_bytes"] = 4 * 1024 * 1024 + 1
    if fault == "bad_count":
        env["payload"]["observations"][0]["reported_input"] = True
    values, accepted, deferred = verified_context_history(
        [reference],
        limits,
        repository="owner/repo",
        now=now,
        api=api,
        download=download,
        ancestor=lambda _: fault != "ancestor",
    )
    assert not values and not accepted and deferred


def test_context_caps_need_two_weekly_runs_and_expired_proof_is_not_carried():
    from datetime import UTC, datetime, timedelta

    from citypods.provider_catalog.limits import context_cap_changes

    now = datetime(2026, 10, 9, 15, tzinfo=UTC)
    history = [
        replace(success(9000), run_id="1", observed_at=(now - timedelta(days=8)).isoformat()),
        replace(success(9200), run_id="2", observed_at=now.isoformat()),
    ]
    limits = {"routes": [dict(ROUTE)], "providers": {"groq": PROVIDER}}
    assert not context_cap_changes(history[:1], limits, now=now)
    changes = context_cap_changes(history, limits, now=now)
    assert changes[0][:4] == ("r", "hard_input_ceiling", None, 8000)
    assert not context_cap_changes(history, limits, now=now + timedelta(days=100))
    limits["routes"][0]["rpd"] = 0
    assert not context_cap_changes(history, limits, now=now)


def test_explicit_context_editor_changes_only_one_scalar_and_preserves_comments():
    from datetime import UTC, datetime

    from citypods.provider_catalog.apply import SOURCE_PATHS, ApplyConfig, parse_context_choice
    from citypods.provider_catalog.config_edit import apply_config_edits, load_config
    from citypods.provider_catalog.limits import context_choice, context_edit_plan

    texts = {
        SOURCE_PATHS[0]: "providers: {}\nroutes:\n  - route_id: r # preserve\n    free: true\n"
        "    rpd: 100\n    input_context_limit: 10000\n    output_context_limit: 2000\n",
        SOURCE_PATHS[1]: "llm_lanes: {} # untouched\n",
        SOURCE_PATHS[2]: "ignored: []\n",
    }
    cfg = ApplyConfig(
        load_config(texts[SOURCE_PATHS[0]]), {}, texts, "base", datetime.now(UTC).date()
    )
    change = ("r", "hard_input_ceiling", None, 8000, "a" * 64)
    assert parse_context_choice(context_choice(change)) == change
    no_selection = context_edit_plan([change], cfg)
    assert apply_config_edits(texts, no_selection) == texts
    plan = context_edit_plan([change], cfg, selected=[context_choice(change)])
    output = apply_config_edits(texts, plan)
    assert output[SOURCE_PATHS[1]] == texts[SOURCE_PATHS[1]]
    assert "# preserve" in output[SOURCE_PATHS[0]]
    assert load_config(output[SOURCE_PATHS[0]])["routes"][0]["hard_input_ceiling"] == 8000
    changed_cfg = {**texts, SOURCE_PATHS[0]: texts[SOURCE_PATHS[0]].replace("rpd: 100", "rpd: 101")}
    with pytest.raises(ValueError, match="config changed"):
        apply_config_edits(changed_cfg, plan)


@pytest.mark.parametrize("family", ["council agenda", "人口预算会议", "a1,{b2};[c3]", "§✓ budget"])
def test_varied_text_feedback_keeps_provider_counts_separate_from_local_estimates(family):
    from citypods.compute.llm_policy import estimate_tokens

    req = request(target=10000)
    text = req.messages[0]["content"].replace(
        "north east south west water stone cloud tree", family
    )
    messages = ({"role": "user", "content": text},)
    estimated = estimate_tokens(messages)
    req = replace(req, messages=messages, local_input_estimate=estimated)
    observed = parse(req, envelope(req, prompt=9500))
    assert observed.outcome == "verified"
    updated = advance_context_state(ContextSearchState(req.identity_digest, "input"), observed)
    assert updated.success.reported_input == 9500
    assert context_input_ratio(updated, 1) == max(Fraction(1), Fraction(9500, estimated))


def test_combined_window_rejection_compares_full_reservation_not_generated_output():
    from citypods.provider_catalog.evidence import digest

    observed = replace(
        success(900),
        count_basis="combined_reserved",
        identity_digest=digest("combined-window-fixture"),
        reported_total=950,
    )
    current = ContextSearchState(observed.identity_digest, "input", success=observed)
    upper = replace(
        observed, outcome="inconclusive", evidence_kind="size_rejection", reported_ceiling=1200
    )
    updated = advance_context_state(current, upper)
    # Actual 900 input plus the full 256 output reservation leaves a 44-token interval.
    assert updated.status == "converged"
    assert (
        next_context_probe(
            updated, {"baseline": 1000, "opposite_reservation": 256}, BUDGET
        ).next_target
        == 1272
    )
    conflicting = replace(upper, count_basis="combined_generated")
    assert advance_context_state(current, conflicting).status == "uncertain"


def test_accounting_overshoot_updates_fixture_ratio_but_cannot_offer_a_cap():
    observed = replace(success(11000), reserved_input=10000, local_input_estimate=9000)
    updated = advance_context_state(state(), observed)
    assert updated.status == "uncertain" and updated.success is None
    assert context_input_ratio(updated, 1) == Fraction(11, 9)


def run_context_orchestration(
    monkeypatch, *, mode="success", provider="groq", quota=True, previous_requests=0, history=()
):
    import time
    from types import SimpleNamespace

    from citypods.compute.llm_dispatch_pause import ContextAdmission, DispatchPauseError
    from citypods.llm_rate_probe import RateProbeRunner
    from citypods.provider_catalog.reconcile import Report, _measure_context_routes

    calls, admissions = [], []
    route = {**ROUTE, "provider": provider, "hard_input_ceiling": 1000}
    context = {
        "run_id": "100",
        "head_sha": "a" * 40,
        "deadline_ms": int((time.time() + 3600) * 1000),
        "catalog_digest": "c" * 64,
        "history": history,
        "budget": dict(BUDGET),
        "readiness": {
            "r": {"enabled": True, "quota_scope": f"{provider}:primary:test" if quota else None}
        },
    }
    pause = SimpleNamespace(contended=False, renew=lambda: None)
    runner = RateProbeRunner(max_requests_total=3, max_requests_per_route=6)
    runner.total_requests = previous_requests

    def reserve(**fields):
        admissions.append(fields)
        if mode == "denied":
            raise DispatchPauseError("row-write limit / ambiguous response")
        return ContextAdmission(fields["attempt_id"])

    def measure(req, *, before_call, **_kwargs):
        if mode == "preflight":
            return ContextObservation.from_request(req, outcome="transport")
        assert isinstance(before_call(), ContextAdmission)
        calls.append(req)
        if mode == "transport":
            return ContextObservation.from_request(req, outcome="transport")
        if req.dimension == "input":
            count = req.reserved_input + 1 if mode == "overshoot" else req.reserved_input
            return parse(req, envelope(req, prompt=count))
        return parse(
            req,
            envelope(
                req, prompt=100, completion=req.requested_output, content="1\n2\n3", finish="length"
            ),
        )

    monkeypatch.setattr("citypods.provider_catalog.probe.measure_context", measure)
    report = Report()
    _measure_context_routes(
        report,
        context,
        [route],
        PROVIDER,
        rules_for(provider),
        runner,
        pause,
        SimpleNamespace(client=SimpleNamespace(reserve_context=reserve)),
        None,
        sleep=lambda _seconds: None,
        cooldown=0,
    )
    return report, context, runner, calls, admissions


def test_context_shared_request_allowance_is_not_a_new_pool(monkeypatch):
    report, context, runner, calls, admissions = run_context_orchestration(
        monkeypatch, previous_requests=2
    )
    assert len(calls) == len(admissions) == 1 and runner.total_requests == 3
    assert context["budget"]["remaining_requests"] == 23
    assert admissions[0]["output_tokens"] == 256
    assert report.context_observations[0].run_id == "100"


@pytest.mark.parametrize("mode", ["denied", "transport", "overshoot", "preflight"])
def test_context_admission_and_transport_failures_do_not_retry_or_start_output(monkeypatch, mode):
    report, context, _runner, calls, admissions = run_context_orchestration(monkeypatch, mode=mode)
    assert len(admissions) == (0 if mode == "preflight" else 1)
    assert len(calls) == (0 if mode in {"preflight", "denied"} else 1)
    assert all(req.dimension == "input" for req in calls)
    if mode == "denied":
        assert context["abandoned"] and not report.context_observations
    if mode == "preflight":
        assert not context.get("rotation") and not report.context_observations
    if mode == "overshoot":
        assert any("accounting overshoot" in note for note in report.observations)


@pytest.mark.parametrize("provider,quota", [("groq", False), ("beatapi", True), ("gemini", True)])
def test_unknown_quota_and_unsupported_endpoint_never_admit(monkeypatch, provider, quota):
    report, context, _runner, calls, admissions = run_context_orchestration(
        monkeypatch, provider=provider, quota=quota
    )
    assert not calls and not admissions and not context.get("rotation")
    assert any("deferred" in note for note in report.observations)


def test_output_reserves_full_generation_after_input_budget_exhaustion(monkeypatch):
    from datetime import UTC, datetime

    # A scripted input success can exhaust its next desired target while output still fits.
    import citypods.provider_catalog.limits as limits_module
    from citypods.provider_catalog.evidence import digest

    original = limits_module.next_context_probe

    def plan(current, limits, budget):
        result = original(current, limits, budget)
        if current.dimension == "input" and current.success:
            return replace(result, status="budget_limited", next_target=524289)
        return result

    monkeypatch.setattr(limits_module, "next_context_probe", plan)
    report, _context, _runner, calls, admissions = run_context_orchestration(
        monkeypatch, provider="openrouter"
    )
    assert [r.dimension for r in calls] == ["input", "output", "output"]
    assert admissions[1]["input_tokens"] == 2048
    assert admissions[1]["output_tokens"] == 2000
    assert report.context_observations[1].reported_output == 2000
    assert all(a["request_digest"] != digest("") for a in admissions)
    assert all(
        datetime.fromisoformat(o.observed_at).tzinfo == UTC for o in report.context_observations
    )


def test_production_spawn_transport_rejects_private_url_before_admission():
    import time

    import requests

    from citypods.provider_catalog.probe import measure_context

    req = request()
    req = replace(req, shaped_body={**req.shaped_body, "url": "http://127.0.0.1/private"})

    def denied():
        pytest.fail("private URL must not reach admission")

    observed = measure_context(
        req, session=requests.Session(), before_call=denied, clock=time.monotonic, timeout=3
    )
    assert observed.outcome == "transport"


@pytest.mark.parametrize("dimension", ["input", "output"])
def test_all_configured_routes_have_offline_endpoint_and_eligibility_coverage(dimension):
    from citypods.provider_catalog.rules import context_parser_support

    limits = yaml.safe_load(Path("config/provider_limits.yml").read_text())
    eligible = {r["route_id"] for r in plan_context_scan(limits, {})}
    for route in limits["routes"]:
        speaks_chat = (route.get("api_shape") or "chat") == "chat"
        assert (route["route_id"] in eligible) == (
            route.get("free") is True and route.get("rpd") != 0 and speaks_chat
        )
        if not speaks_chat:
            # JEV's systemone route has no chat context to measure (review/53 PR2).
            continue
        req = build_context_request(
            route,
            limits["providers"][route["provider"]],
            rules_for(route["provider"]),
            dimension=dimension,
            target=1000,
            ratio=1,
            attempt_ordinal=1,
            nonce="a" * 64,
        )
        supported, reason = context_parser_support(rules_for(route["provider"]), req)
        expected = route["provider"] == "openrouter" or (
            route["provider"] == "groq" and dimension == "input"
        )
        assert supported == expected, route["route_id"]
        if not supported:
            assert "unsupported" in reason
        # A gateway's native/JEV fields never prove chat capacity in either dimension.
        native = {"usageMetadata": {"promptTokenCount": 1000}, "systemone": {"tokens": 1000}}
        assert parse(req, native).outcome != "verified"


def test_context_cap_publication_waits_for_different_open_reviewed_choices():
    from citypods.provider_catalog.apply import EditPlan
    from scripts.provider_catalog_commands import publish

    calls = []
    marker = "<!-- citypods:provider-catalog-context -->"

    def run(argv):
        calls.append(argv)
        if argv[:2] == ["git", "rev-parse"]:
            return "main-sha"
        if argv[:3] == ["gh", "pr", "list"]:
            return json.dumps(
                [
                    {
                        "number": 1,
                        "body": marker + "reviewed old choices",
                        "author": {"login": "github-actions[bot]"},
                        "baseRefName": "main",
                        "isCrossRepository": False,
                    }
                ]
            )
        return ""

    plan = EditPlan(
        "main-sha",
        (),
        proposal_kind="context",
        context_changes=(("r", "hard_input_ceiling", None, 900, "a" * 64),),
    )
    assert publish(plan, run_fn=run).startswith("Waiting:")
    assert not any(c[:2] in (["git", "push"], ["git", "commit"], ["git", "add"]) for c in calls)
    assert any("automation/provider-catalog-context" in c for c in calls)


def test_context_writer_ignores_issue_proof_and_reauthenticates_fresh_main(monkeypatch, tmp_path):
    from datetime import UTC, datetime, timedelta

    from citypods.provider_catalog import evidence
    from citypods.provider_catalog.apply import SOURCE_PATHS
    from citypods.provider_catalog.config_edit import load_config
    from citypods.provider_catalog.issue import render_body
    from citypods.provider_catalog.limits import context_cap_changes
    from citypods.provider_catalog.reconcile import Report
    from scripts import provider_catalog_commands as commands

    config = {
        "providers": {"groq": {**PROVIDER, "accounts": [{"id": "primary"}]}},
        "routes": [dict(ROUTE)],
    }
    texts = {
        SOURCE_PATHS[0]: yaml.safe_dump(config, sort_keys=False),
        SOURCE_PATHS[1]: Path(SOURCE_PATHS[1]).read_text(),
        SOURCE_PATHS[2]: "version: 1\n",
    }
    for path, text in texts.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    monkeypatch.setattr(commands, "ROOT", tmp_path)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    now = datetime.now(UTC)
    history = (
        replace(success(800), observed_at=(now - timedelta(days=15)).isoformat(), run_id="1"),
        replace(success(850), observed_at=(now - timedelta(days=8)).isoformat(), run_id="2"),
        replace(success(900), observed_at=(now - timedelta(days=1)).isoformat(), run_id="3"),
    )
    # The latest success is this run's observation, not part of pre-run trusted history.
    prior, current = history[:-1], history[-1:]
    changes = context_cap_changes((*prior, *current), config, now=now)
    assert context_cap_changes(prior, config, now=now) != changes
    report = Report(
        context_changes=list(changes), context_observations=list(current), state={"last_full": {}}
    )
    body = render_body(report, run_date=now.date().isoformat()).replace("- [ ]", "- [x]")
    monkeypatch.setattr(evidence, "discover_context_references", lambda **_kwargs: ("real",))

    def verified(refs, *_args, **_kwargs):
        assert refs == ("real",)
        return history, (), ()

    monkeypatch.setattr(evidence, "verified_context_history", verified)
    plan = commands.prepare(body, "fresh-main", proposal_kind="context")
    assert plan.base_commit == "fresh-main" and plan.context_changes == changes
    assert (
        load_config((tmp_path / SOURCE_PATHS[0]).read_text())["routes"][0]["hard_input_ceiling"]
        == 900
    )
    # Reset the fresh sources. An edited checkbox is a selection, never authority for a larger cap.
    for path, text in texts.items():
        (tmp_path / path).write_text(text)
    forged = body.replace("to 900", "to 9999")
    denied = commands.prepare(forged, "new-main", proposal_kind="context")
    assert not denied.context_changes and denied.deferred
    assert (tmp_path / SOURCE_PATHS[0]).read_text() == texts[SOURCE_PATHS[0]]
    monkeypatch.setattr(
        evidence,
        "verified_context_history",
        lambda *_args, **_kwargs: ((), (), ("missing authenticated history",)),
    )
    with pytest.raises(ValueError, match="history unavailable"):
        commands.prepare(body, "new-main", proposal_kind="context")


@pytest.mark.parametrize(
    "event,schedule,ref",
    [
        ("workflow_dispatch", "17 10 * * 1", "refs/heads/main"),
        ("schedule", "37 9 * * *", "refs/heads/main"),
        ("schedule", "17 10 * * 1", "refs/heads/feature"),
    ],
)
def test_context_cli_rejects_manual_daily_and_non_main_runs(
    monkeypatch, tmp_path, event, schedule, ref
):
    from scripts import reconcile_provider_routes as script

    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.setenv("GITHUB_EVENT_SCHEDULE", schedule)
    monkeypatch.setenv("GITHUB_REF", ref)
    monkeypatch.setattr(script, "_control", lambda _no_pause: None)
    with pytest.raises(SystemExit) as exc:
        script.main(["--context-evidence", str(tmp_path / "evidence.json")])
    assert exc.value.code == 2


@pytest.mark.parametrize("enabled", [False, True])
def test_weekly_disabled_or_stale_catalog_does_not_start_history_admission_or_calls(
    monkeypatch, tmp_path, enabled
):
    from types import SimpleNamespace

    from citypods.provider_catalog import evidence
    from citypods.provider_catalog.reconcile import Report
    from scripts import reconcile_provider_routes as script

    for key, value in {
        "GITHUB_EVENT_NAME": "schedule",
        "GITHUB_EVENT_SCHEDULE": "17 10 * * 1",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_RUN_ID": "100",
        "GITHUB_SHA": "a" * 40,
    }.items():
        monkeypatch.setenv(key, value)

    def forbidden(*_args, **_kwargs):
        pytest.fail("disabled context must not consume history or admission")

    control = script.WorkerDispatchControl(
        SimpleNamespace(
            context_status=lambda _selection: {"enabled": enabled, "catalog_digest": "c" * 64},
            start_context=forbidden,
            reserve_context=forbidden,
        )
    )
    monkeypatch.setattr(script, "_control", lambda _no_pause: control)
    monkeypatch.setattr(evidence, "discover_context_references", forbidden)
    monkeypatch.setattr(script, "fetch_quality_index", lambda _session: None)

    def reconcile(*_args, **kwargs):
        assert kwargs["context_run"] is None
        return Report(state={"last_full": {}})

    monkeypatch.setattr(script, "reconcile", reconcile)
    monkeypatch.setattr(script, "backtest_discovery", lambda *_args, **_kwargs: [])
    target = tmp_path / "evidence.json"
    assert script.main(["--context-evidence", str(target)]) == 0
    assert json.loads(target.read_text())["payload"]["observations"] == []


def test_output_large_digit_strings_and_duplicate_sentinel_keys_fail_closed():
    req = request("openrouter", "output", 100)
    value = envelope(
        req, content="1\n" + "9" * 5000 + "\n3", finish="length", prompt=30, completion=100
    )
    assert parse(req, value).outcome == "inconclusive"
    req = request()
    value = envelope(req)
    content = value["choices"][0]["message"]["content"]
    value["choices"][0]["message"]["content"] = content.replace(
        '{"start":', '{"start":"bad","start":'
    )
    assert parse(req, value).outcome == "inconclusive"


def test_budget_ceiling_never_becomes_a_production_cap_reduction():
    from datetime import UTC, datetime, timedelta

    from citypods.provider_catalog.limits import context_cap_changes

    now = datetime.now(UTC)
    config = {
        "providers": {"groq": PROVIDER},
        "routes": [{**ROUTE, "input_context_limit": 1000000, "hard_input_ceiling": 500000}],
    }
    history = tuple(
        replace(
            success(count),
            reserved_input=524288,
            observed_at=(now - timedelta(days=days)).isoformat(),
            run_id=str(days),
        )
        for count, days in [(390000, 8), (400000, 1)]
    )
    assert not context_cap_changes(history, config, now=now)
    del config["routes"][0]["hard_input_ceiling"]
    # A missing cap can still offer a clearly stated verified lower bound for explicit review.
    assert context_cap_changes(history, config, now=now)[0][3] == 400000


def test_malformed_advisory_cap_state_cannot_break_reconciliation():
    from citypods.provider_catalog.reconcile import Report, _merge_into_last_full

    for value in [
        True,
        {"r": "forged"},
        [["wrong"], ["r", "rpm", 1, 2, "a" * 64]],
        [["r", "hard_input_ceiling", None, True, "a" * 64]],
    ]:
        report = Report()
        _merge_into_last_full(report, {"context_changes": value}, set())
        assert not report.context_changes


def test_uncertain_search_restarts_next_week_and_needs_two_fresh_weekly_successes():
    from datetime import UTC, datetime, timedelta

    from citypods.provider_catalog.limits import context_cap_changes, context_history_states

    now = datetime.now(UTC)
    history = tuple(
        replace(
            success(count), run_id=str(run), observed_at=(now - timedelta(days=days)).isoformat()
        )
        for count, run, days in [(800, 1, 24), (800, 2, 16), (700, 3, 8), (900, 4, 1)]
    )
    uncertain = context_history_states(history[:2], now=now)[(history[0].identity_digest, "input")]
    assert uncertain.status == "uncertain"
    baseline = next_context_probe(
        uncertain, {"baseline": 1000, "opposite_reservation": 256}, BUDGET
    )
    assert baseline.next_target == 1000 and baseline.success is None and baseline.rejection is None
    assert baseline.count_pairs == uncertain.count_pairs
    config = {"routes": [ROUTE], "providers": {"groq": PROVIDER}}
    assert not context_cap_changes(history[:3], config, now=now)
    assert context_cap_changes(history, config, now=now)[0][3] == 900
    renewed = context_history_states(history, now=now)[(history[0].identity_digest, "input")]
    assert renewed.status == "exploring" and renewed.success == history[-1]


def test_bounded_history_keeps_unexpired_anchor_without_trusting_summary():
    from citypods.provider_catalog.evidence import digest, verified_context_history
    from citypods.provider_catalog.limits import context_history_states

    now, limits, observed, env, _run, _artifact, reference, api, download = (
        context_artifact_fixture()
    )
    values = [replace(observed, attempt_id=f"{1:064x}")]
    values += [
        replace(
            observed,
            outcome="transport",
            count_basis="unknown",
            reported_input=None,
            reported_output=None,
            reported_total=None,
            attempt_id=f"{i:064x}",
        )
        for i in range(2, 22)
    ]
    env["payload"]["observations"] = [asdict(o) for o in values]
    env["payload"]["summaries"] = [{"success": {"reported_input": 1000000}}]
    env["payload_digest"] = reference["payload_digest"] = digest(env["payload"])
    history, _accepted, deferred = verified_context_history(
        [reference],
        limits,
        repository="owner/repo",
        now=now,
        api=api,
        download=download,
        ancestor=lambda _sha: True,
    )
    assert not deferred and len(history) == 16
    current = context_history_states(history, now=now)[(observed.identity_digest, "input")]
    assert current.success.reported_input == 900
    assert current.success.observed_at == observed.observed_at


def test_converged_bracket_waits_until_a_later_run_to_stretch_again(monkeypatch):
    from datetime import UTC, datetime, timedelta

    stamp = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    history = (
        replace(success(800), observed_at=stamp),
        replace(rejection(1000), observed_at=stamp),
    )
    report, _context, _runner, calls, _admissions = run_context_orchestration(
        monkeypatch, history=history
    )
    assert len(calls) == 1
    assert report.context_states[0].status == "converged"
    assert calls[0].reserved_input < 1000


@pytest.mark.parametrize("due_only", [False, True])
def test_non_context_scan_carries_only_advisory_cap_choices_without_context_calls(
    monkeypatch, due_only
):
    from datetime import UTC, datetime

    from citypods.provider_catalog.decisions import Decisions
    from citypods.provider_catalog.quality import QualityIndex
    from citypods.provider_catalog.reconcile import NoDispatchControl, reconcile

    def forbidden(*_args, **_kwargs):
        pytest.fail("manual/daily scan cannot call context transport")

    monkeypatch.setattr("citypods.provider_catalog.probe.measure_context", forbidden)
    change = ("r", "hard_input_ceiling", None, 900, "a" * 64)
    report = reconcile(
        {"providers": {}, "routes": []},
        {},
        Decisions(),
        QualityIndex(),
        {"last_full": {"context_changes": [change]}},
        session=None,
        control=NoDispatchControl(),
        today=datetime.now(UTC).date(),
        due_only=due_only,
    )
    assert report.context_changes == [change]
    assert report.state["last_full"]["context_changes"] == [change]
    assert not report.context_observations


def test_compiled_context_digest_matches_worker_canonical_json_numbers_and_utf8():
    from citypods.provider_catalog.evidence import _context_catalog_digest

    # Generated offline by protocol.js canonicalJson/sha256Hex; no production catalog golden.
    fixture = {
        "name": "人口",
        "number": 1.0,
        "tiny": 0.00000014,
        "small": 0.000001,
        "minus": -0.0,
        "parts": [True, None, 1.75],
    }
    assert _context_catalog_digest(fixture) == (
        "083a8eb03f27093ee1167196458185b5b51ac07095af02c24c4ce08f54506319"
    )
    for number in (float("nan"), 2**1024):
        with pytest.raises(ValueError, match="unsafe"):
            _context_catalog_digest({"tpm": number})


@pytest.mark.parametrize("input_cap,expected", [(8000, False), (6000, True)])
def test_selected_input_output_batch_preserves_final_total_window(input_cap, expected):
    from datetime import UTC, datetime

    from citypods.provider_catalog.apply import SOURCE_PATHS, ApplyConfig
    from citypods.provider_catalog.config_edit import apply_config_edits
    from citypods.provider_catalog.limits import context_choice, context_edit_plan

    texts = {
        SOURCE_PATHS[0]: yaml.safe_dump({"routes": [ROUTE]}, sort_keys=False),
        SOURCE_PATHS[1]: "llm_lanes: {}\n",
        SOURCE_PATHS[2]: "version: 1\n",
    }
    cfg = ApplyConfig({"routes": [ROUTE]}, {}, texts, "main", datetime.now(UTC).date())
    changes = (
        ("r", "hard_input_ceiling", None, input_cap, "a" * 64),
        ("r", "output_context_limit", 2000, 4000, "b" * 64),
    )
    plan = context_edit_plan(changes, cfg, selected=[context_choice(c) for c in changes])
    assert bool(plan.context_changes) == expected
    if expected:
        changed = yaml.safe_load(apply_config_edits(texts, plan)[SOURCE_PATHS[0]])["routes"][0]
        assert changed["hard_input_ceiling"] + changed["output_context_limit"] == 10000
    else:
        assert plan.deferred and apply_config_edits(texts, plan) == texts


@pytest.mark.parametrize("previous_requests", [3, 4])
def test_regular_probes_exhaust_shared_context_allowance(monkeypatch, previous_requests):
    report, context, runner, calls, admissions = run_context_orchestration(
        monkeypatch, previous_requests=previous_requests
    )
    assert not calls and not admissions and not report.context_observations
    assert runner.total_requests == previous_requests
    assert context["budget"]["remaining_requests"] == 24


@pytest.mark.parametrize("failed_build", [1, 2])
def test_context_fixture_failure_defers_without_admission(monkeypatch, failed_build):
    from citypods.provider_catalog import probe

    original = probe.build_context_request
    count = 0

    def build(*args, **kwargs):
        nonlocal count
        count += 1
        if count == failed_build:
            raise ValueError("fixture cannot fit")
        return original(*args, **kwargs)

    monkeypatch.setattr(probe, "build_context_request", build)
    report, _context, _runner, calls, admissions = run_context_orchestration(monkeypatch)
    assert not calls and not admissions
    assert any("r/input: context deferred (fixture cannot fit)" in x for x in report.observations)


def test_success_lower_bounds_cannot_reduce_existing_cap():
    from datetime import UTC, datetime, timedelta

    from citypods.provider_catalog.limits import context_cap_changes

    now = datetime.now(UTC)
    config = {
        "providers": {"groq": PROVIDER},
        "routes": [{**ROUTE, "hard_input_ceiling": 1000}],
    }
    history = tuple(
        replace(
            success(count), observed_at=(now - timedelta(days=days)).isoformat(), run_id=str(days)
        )
        for count, days in [(800, 8), (950, 1)]
    )
    assert not context_cap_changes(history, config, now=now)
    config["routes"][0]["hard_input_ceiling"] = 700
    assert context_cap_changes(history, config, now=now)[0][3] == 950


def test_the_context_scan_skips_non_chat_routes():
    chat = {"route_id": "chat", "free": True, "rpd": 10}
    jev = {"route_id": "jev", "free": True, "rpd": 10, "api_shape": "systemone"}
    assert [r["route_id"] for r in plan_context_scan({"routes": [jev, chat]}, {})] == ["chat"]


def test_manual_artifact_is_authenticated_but_cannot_become_scheduled_cap_history():
    from citypods.provider_catalog.evidence import digest, verified_context_history

    now, limits, observed, envelope, run, artifact, reference, api, download = (
        context_artifact_fixture()
    )
    run["event"] = "workflow_dispatch"
    artifact["name"] = "provider-catalog-manual-context-evidence-1"
    envelope["payload"]["kind"] = "manual_context"
    envelope["payload_digest"] = digest(envelope["payload"])
    reference["payload_digest"] = envelope["payload_digest"]
    history, accepted, gaps = verified_context_history(
        [reference],
        limits,
        repository="owner/repo",
        now=now,
        api=api,
        download=download,
        ancestor=lambda _sha: True,
        manual=True,
    )
    assert history == (observed,) and accepted and not gaps
    history, accepted, gaps = verified_context_history(
        [reference],
        limits,
        repository="owner/repo",
        now=now,
        api=api,
        download=download,
        ancestor=lambda _sha: True,
    )
    assert not history and not accepted and gaps


def test_manual_canary_rejects_local_or_non_main_before_provider_io(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from scripts import reconcile_provider_routes as script

    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/feature")
    control = script.WorkerDispatchControl(SimpleNamespace())
    monkeypatch.setattr(script, "_control", lambda _flag: control)
    with pytest.raises(ValueError, match="workflow_dispatch on main"):
        script.main(["--manual-context", "--context-evidence", str(tmp_path / "out.json")])


@pytest.mark.parametrize("fault", [None, "measurement", "cleanup", "both", "status"])
def test_manual_canary_is_context_only_and_finishes_session(monkeypatch, tmp_path, fault):
    from types import SimpleNamespace

    from citypods.compute.llm_dispatch_pause import ContextSession
    from citypods.provider_catalog import evidence, reconcile
    from scripts import reconcile_provider_routes as script

    run_id, sha = "100", "a" * 40
    for key, value in {
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_RUN_ID": run_id,
        "GITHUB_SHA": sha,
    }.items():
        monkeypatch.setenv(key, value)
    limits = {"routes": [dict(ROUTE)], "providers": {"groq": dict(PROVIDER)}}
    monkeypatch.setattr(script.yaml, "safe_load", lambda _text: limits)
    digest = evidence._context_catalog_digest(
        json.loads(
            (script.REPO_ROOT / "workers/llm-dispatch-v2/src/dispatch_limits.json").read_text()
        )
    )
    status = {
        "enabled": True,
        "catalog_digest": digest,
        "output_enabled": False,
        "routes": {"r": {"enabled": True, "quota_scope": "groq:primary:m"}},
        "manual_session": {"weekly_requests_used": 0},
    }
    status["routes"]["r"]["quota_scope"] = (
        f"{ROUTE['provider']}:{ROUTE['account_id']}:{ROUTE['upstream_model']}"
    )
    finished = []
    measurement_error = RuntimeError("original measurement failure")
    cleanup_error = ValueError("cleanup failure")
    status_error = OSError("status failure")
    reads = []

    def read_status(_selection):
        reads.append(True)
        if fault == "status" and len(reads) == 3:
            raise status_error
        return status

    def finish(*_args):
        finished.append(True)
        if fault in {"cleanup", "both"}:
            raise cleanup_error

    client = SimpleNamespace(
        context_status=read_status,
        start_manual_context=lambda *_args, **_kwargs: ContextSession(
            "2026-10-05", run_id, 9999999999999, 16384, 512, 2
        ),
        finish_manual_context=finish,
    )
    control = script.WorkerDispatchControl(client)
    from contextlib import contextmanager

    @contextmanager
    def pause(_provider):
        yield SimpleNamespace(contended=False)

    monkeypatch.setattr(control, "paused", pause)
    monkeypatch.setattr(script, "_control", lambda _flag: control)
    monkeypatch.setattr(
        evidence,
        "_gh_json",
        lambda path: (
            {"id": 7, "path": evidence.RATE_WORKFLOW}
            if "/workflows/" in path
            else {
                "id": 100,
                "workflow_id": 7,
                "event": "workflow_dispatch",
                "head_branch": "main",
                "head_sha": sha,
                "head_repository": {"full_name": "owner/repo"},
                "status": "in_progress",
            }
        ),
    )
    monkeypatch.setattr(evidence, "discover_context_references", lambda **_kwargs: [])
    monkeypatch.setattr(
        evidence, "verified_context_history", lambda *_args, **_kwargs: ((), (), ())
    )
    calls = []

    def measure(report, context, routes, *_args, **_kwargs):
        assert context["manual"] and context["dimensions"] == ["input"]
        assert [r["route_id"] for r in routes] == ["r"]
        calls.append(True)
        report.context_attempted_routes.add("r")
        if fault in {"measurement", "both"}:
            raise measurement_error

    monkeypatch.setattr(reconcile, "_measure_context_routes", measure)
    monkeypatch.setattr(script, "fetch_quality_index", lambda *_args: pytest.fail("catalog I/O"))
    target = tmp_path / "out.json"
    arguments = [
        "--manual-context",
        "--context-routes",
        "r",
        "--context-purpose",
        "#2221",
        "--context-evidence",
        str(target),
    ]
    if fault is None:
        assert script.main(arguments) == 0
    else:
        expected = (
            measurement_error
            if fault in {"measurement", "both"}
            else (cleanup_error if fault == "cleanup" else status_error)
        )
        with pytest.raises(type(expected)) as raised:
            script.main(arguments)
        assert raised.value is expected
    assert calls == [True] and finished == [True]
    payload = json.loads(target.read_text())["payload"]
    assert payload["kind"] == "manual_context" and payload["authority"]["max_requests"] == 2

    assert payload["run_status"] == ("failed" if fault else "success")
    assert payload["attempted_routes"] == ["r"]
    if fault:
        assert payload["errors"]
        assert all(set(error) == {"stage", "type"} for error in payload["errors"])
    if fault == "both":
        assert [error["stage"] for error in payload["errors"]] == ["measurement", "cleanup"]
