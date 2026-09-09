from __future__ import annotations

import json
from unittest.mock import MagicMock

from citypods import llm_rate_probe
from citypods.llm_rate_probe import RateProbeRunner, main, run_phase_0, run_phase_4, run_probes


def test_dry_run_issues_no_requests(monkeypatch, tmp_path):
    """Without --apply, the probe prints the plan and issues zero live HTTP calls."""

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("HTTP request made during dry-run!")

    monkeypatch.setattr(llm_rate_probe.requests.Session, "post", fail_if_called)

    out_file = tmp_path / "report.json"
    ret = main(["--phase", "0", "--out", str(out_file)])
    assert ret == 0
    assert out_file.exists()

    report = json.loads(out_file.read_text(encoding="utf-8"))
    summary = report["summary"]
    assert summary["apply"] is False
    assert summary["total_requests"] == 0


def test_respects_max_requests_total(monkeypatch):
    """Total requests ceiling halts further probes once reached."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"x-ratelimit-remaining-requests": "5"}
    mock_resp.json.return_value = {"choices": [{"message": {"content": "pong"}}]}

    session = MagicMock()
    session.post.return_value = mock_resp

    runner = RateProbeRunner(
        apply=True,
        max_requests_total=3,
        session=session,
    )

    routes = [
        {"route_id": f"route_{i}", "provider": "test", "api_key_env": "TEST_KEY"} for i in range(10)
    ]
    monkeypatch.setenv("TEST_KEY", "dummy-secret")

    report = run_probes(routes, ["0"], runner)

    assert runner.total_requests == 3
    assert report["summary"]["total_requests"] == 3
    # Verify budget interruption was recorded
    interrupted = [r for r in report["routes"] if "budget_interrupted" in r]
    assert len(interrupted) == 1


def test_skips_routes_with_missing_api_key_env(monkeypatch):
    """In live apply mode, routes without configured env secrets are reported as skipped."""
    monkeypatch.delenv("MISSING_KEY_VAR", raising=False)

    runner = RateProbeRunner(apply=True)
    routes = [
        {
            "route_id": "test_route",
            "provider": "gemini",
            "api_key_env": "MISSING_KEY_VAR",
        }
    ]

    report = run_probes(routes, ["0"], runner)
    assert len(report["routes"]) == 1
    skipped = report["routes"][0]
    assert skipped["status"] == "skipped"
    assert "Missing MISSING_KEY_VAR" in skipped["reason"]
    assert runner.total_requests == 0


def test_paid_routes_excluded_by_default(tmp_path):
    """Only free routes are probed by default; --include-paid opts into paid routes."""
    out_file = tmp_path / "report_free.json"
    main(["--phase", "0", "--out", str(out_file)])
    report_free = json.loads(out_file.read_text(encoding="utf-8"))
    free_count = report_free["summary"]["routes_probed"]

    out_file_paid = tmp_path / "report_paid.json"
    main(["--phase", "0", "--include-paid", "--out", str(out_file_paid)])
    report_paid = json.loads(out_file_paid.read_text(encoding="utf-8"))
    paid_count = report_paid["summary"]["routes_probed"]

    assert paid_count > free_count, "Expected --include-paid to include additional paid routes"


def test_report_records_header_names_and_values_but_no_api_key(monkeypatch):
    """Headers matching limit metadata regex are recorded; credentials are excluded."""
    secret_key = "sk-super-secret-api-key-12345"
    mock_resp = MagicMock()
    mock_resp.status_code = 429
    mock_resp.headers = {
        "x-ratelimit-remaining-requests": "0",
        "retry-after": "45",
        "cf-ray": "89abcdef1234",
        "authorization": f"Bearer {secret_key}",
        "set-cookie": f"session={secret_key}",
    }
    mock_resp.json.return_value = {
        "error": {
            "message": "The server is overloaded. Please try again later.",
            "code": "rate_limit_exceeded",
        }
    }

    session = MagicMock()
    session.post.return_value = mock_resp

    runner = RateProbeRunner(apply=True, session=session)
    route = {
        "route_id": "test_nvidia",
        "provider": "nvidia",
        "api_key_env": "TEST_KEY",
        "api_base": "https://api.test.com",
    }
    monkeypatch.setenv("TEST_KEY", secret_key)

    p0 = run_phase_0(runner, route)

    headers = p0["headers"]
    assert headers["x-ratelimit-remaining-requests"] == "0"
    assert headers["retry-after"] == "45"
    assert headers["cf-ray"] == "89abcdef1234"
    assert "authorization" not in headers
    assert "set-cookie" not in headers

    report_str = json.dumps(p0)
    assert secret_key not in report_str


def test_phase_4_ground_truth_agreement():
    """Phase 4 labels cold-first 429s as upstream_capacity and checks classifier agreement."""
    # Agrees with upstream_capacity
    p0_agreed = {
        "status": 429,
        "classification": {
            "failure_class": "upstream_capacity",
            "rule_id": "overloaded",
        },
    }
    res_agreed = run_phase_4(p0_agreed)
    assert res_agreed is not None
    assert res_agreed["ground_truth"] == "upstream_capacity"
    assert res_agreed["agreed"] is True

    # Disagrees (classifier thought it was own_rpm)
    p0_disagreed = {
        "status": 429,
        "classification": {
            "failure_class": "own_rpm",
            "rule_id": "openai-shaped-rate-limit",
        },
    }
    res_disagreed = run_phase_4(p0_disagreed)
    assert res_disagreed is not None
    assert res_disagreed["agreed"] is False

    # Status 200 produces no Phase 4 evaluation
    assert run_phase_4({"status": 200}) is None


def _route(**over):
    base = {
        "route_id": "r1",
        "provider": "groq",
        "upstream_model": "m",
        "api_key_env": "K",
        "api_base": "https://example.invalid",
        "chat_path": "/v1/chat/completions",
        "input_context_limit": 131072,
        "free": True,
    }
    base.update(over)
    return base


class _ScriptedRunner:
    """A RateProbeRunner stand-in returning a canned response for every probe."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def send_request(self, route, prompt, *, max_tokens=1):
        self.calls += 1
        return self._responses[min(self.calls - 1, len(self._responses) - 1)]


def test_phase_2_reports_inconclusive_rather_than_the_search_floor():
    """Regression: the 2026-09-09 run recorded five routes at exactly 1000 -- the search floor --
    because `observed_ceiling` was seeded to it and only raised on a 200. That unverified number
    was then auto-promoted to an enforced hard ceiling and blocked the routes outright. A run
    where nothing is ever accepted must report None, never a number."""
    from citypods.llm_rate_probe import run_phase_2

    throttled = {"status": 429, "headers": {}, "body": {"error": {"message": "high demand"}}}
    result = run_phase_2(_ScriptedRunner([throttled]), _route(provider="sambanova"))

    assert result["observed_input_ceiling"] is None
    assert result["conclusive"] is False
    assert result["inconclusive_reason"]


def test_phase_2_does_not_treat_a_rate_limit_as_a_size_limit():
    """A 429 says nothing about request size -- narrowing the search on one is what produced the
    bogus 1000s. The search must abandon as inconclusive instead of shrinking toward the floor."""
    from citypods.llm_rate_probe import run_phase_2

    throttled = {"status": 429, "headers": {}, "body": {"error": {"message": "overloaded"}}}
    runner = _ScriptedRunner([throttled])
    result = run_phase_2(runner, _route(), max_probes=8)

    # One probe, then stop -- not eight probes bisecting down to the floor.
    assert runner.calls == 1
    assert result["observed_input_ceiling"] is None


def test_phase_2_records_only_a_size_that_actually_returned_200():
    from citypods.llm_rate_probe import run_phase_2

    ok = {
        "status": 200,
        "headers": {},
        "body": {"choices": [{"message": {"content": "x"}}], "usage": {"prompt_tokens": 1}},
    }
    result = run_phase_2(_ScriptedRunner([ok]), _route(input_context_limit=9000))

    assert result["conclusive"] is True
    # Converges upward to the largest size that actually returned 200, not to the floor.
    assert result["observed_input_ceiling"] == 8000


def test_phase_2_retries_through_a_throttle_before_giving_up():
    """Endurance semantics: a throttle is waited out and the SAME size re-probed, so a busy route
    still yields a real measurement instead of being written off."""
    from citypods.llm_rate_probe import run_phase_2

    throttled = {"status": 429, "headers": {}, "body": {"error": {"message": "overloaded"}}}
    ok = {"status": 200, "headers": {}, "body": {"choices": [{"message": {"content": "x"}}]}}
    runner = _ScriptedRunner([throttled, ok])
    result = run_phase_2(
        runner, _route(input_context_limit=9000), throttle_retries=2, throttle_wait_seconds=0
    )

    assert runner.calls >= 2
    assert result["observed_input_ceiling"] == 8000


def test_provider_reported_token_limit_prefers_header_then_message():
    from citypods.llm_rate_probe import provider_reported_token_limit as f

    assert f({"headers": {"x-ratelimit-limit-tokens": "8000"}, "body": None}) == 8000
    itpm = {"headers": {}, "body": {"error": {"message": "(ITPM): Limit 7000, Requested 1"}}}
    assert f(itpm) == 7000
    assert f({"headers": {}, "body": {"error": {"message": "TPM: Limit 250,000"}}}) == 250000
    assert f({"headers": {}, "body": {"error": {"message": "overloaded"}}}) is None


def test_phase_2_clamps_the_search_at_the_provider_reported_token_budget():
    """A request can never exceed the per-minute token budget it would have to spend, so the
    search must be bounded by what the provider states -- not by the advertised context window and
    not by our own configured `tpm`, which is a pacing input rather than a measurement. Groq
    advertises a 131,072-token context on an account whose real ITPM is 7,000."""
    from citypods.llm_rate_probe import run_phase_2

    ok = {
        "status": 200,
        "headers": {"x-ratelimit-limit-tokens": "7000"},
        "body": {"choices": [{"message": {"content": "x"}}]},
    }

    # The endurance path learns the budget while polling and hands it in, so the search is bounded
    # from the very first probe rather than after an overshoot.
    result = run_phase_2(
        _ScriptedRunner([ok]), _route(input_context_limit=131072), provider_reported_tpm=7000
    )
    assert result["search_upper_bound"] <= 7000
    assert result["observed_input_ceiling"] <= 7000

    # A budget first revealed mid-search still pulls the upper bound down for later probes.
    unbounded = run_phase_2(_ScriptedRunner([ok]), _route(input_context_limit=131072))
    assert unbounded["provider_reported_tpm"] == 7000
    assert unbounded["search_upper_bound"] <= 7000


def test_provider_reported_token_limit_tolerates_every_error_body_shape():
    """Regression: a live endurance run crashed on the first provider returning a bare-string
    `error`, because the parser assumed a dict. Response bodies are untrusted third-party shapes;
    every access has to be shape-checked."""
    from citypods.llm_rate_probe import provider_reported_token_limit as f

    assert f({"headers": {}, "body": {"error": "Limit 500 exceeded"}}) == 500
    assert f({"headers": {}, "body": {"error": None}}) is None
    assert f({"headers": {}, "body": {"detail": "TPM: Limit 900"}}) == 900
    assert f({"headers": {}, "body": "Limit 42"}) == 42
    assert f({"headers": {}, "body": []}) is None
    assert f({"headers": {}, "body": None}) is None
    assert f({}) is None


def test_phase_2_reopens_the_search_when_a_rejection_was_only_contention():
    """This account's Gemini/NVIDIA/Airforce routes carry live production traffic, so a probe can
    be refused for budget another process just spent. Accepting that first refusal as the ceiling
    understates it. Re-testing the boundary must reopen the search when it later succeeds."""
    from citypods.llm_rate_probe import run_phase_2

    reject = {"status": 413, "headers": {}, "body": {"error": {"message": "too large"}}}
    ok = {"status": 200, "headers": {}, "body": {"choices": [{"message": {"content": "x"}}]}}

    class _Seq:
        def __init__(self, seq):
            self.seq, self.calls = list(seq), 0

        def send_request(self, route, prompt, *, max_tokens=1):
            self.calls += 1
            return self.seq[min(self.calls - 1, len(self.seq) - 1)]

    # First probe rejected (contention), every later probe fine.
    result = run_phase_2(
        _Seq([reject, ok]),
        _route(input_context_limit=9000),
        confirm_rounds=2,
        confirm_wait_seconds=0,
    )
    assert result["contention_detected"] is True
    assert result["observed_input_ceiling"] is not None
    assert result["observed_input_ceiling"] >= 5000


def test_phase_2_confirms_a_genuine_ceiling_without_flagging_contention():
    from citypods.llm_rate_probe import run_phase_2

    reject = {"status": 413, "headers": {}, "body": {"error": {"message": "too large"}}}
    result = run_phase_2(
        _ScriptedRunner([reject]),
        _route(input_context_limit=9000),
        confirm_rounds=2,
        confirm_wait_seconds=0,
    )
    assert result["contention_detected"] is False


def test_direct_chat_url_collapses_a_duplicated_segment():
    """`api_base` + `chat_path` are authored for AI Gateway's custom-provider path rewrite, so
    some providers repeat a segment already present in api_base. Concatenating naively 404'd every
    airforce request, making a reachable route look permanently dead in an endurance run whose
    whole purpose is telling "dead" apart from "busy"."""
    from citypods.llm_rate_probe import direct_chat_url

    airforce = {"api_base": "https://api.airforce/v1", "chat_path": "/v1/chat/completions"}
    assert direct_chat_url(airforce) == "https://api.airforce/v1/chat/completions"

    # Every other provider in the catalog is unaffected.
    assert (
        direct_chat_url(
            {"api_base": "https://api.groq.com/openai/v1", "chat_path": "/chat/completions"}
        )
        == "https://api.groq.com/openai/v1/chat/completions"
    )
    assert (
        direct_chat_url({"api_base": "https://api.mistral.ai", "chat_path": "/v1/chat/completions"})
        == "https://api.mistral.ai/v1/chat/completions"
    )
    assert (
        direct_chat_url(
            {"api_base": "https://api.z.ai/api/paas/v4", "chat_path": "/chat/completions"}
        )
        == "https://api.z.ai/api/paas/v4/chat/completions"
    )


def test_a_200_carrying_no_completion_is_not_a_success():
    """Airforce returns HTTP 200 with no `choices` and an error object whose own code says 503.
    Counting that as a success marks a route serving nothing as viable -- the one distinction an
    endurance run must not get wrong -- and in production it settled the job `completed` with a
    non-answer as its durable result."""
    from citypods.llm_rate_probe import is_usable_completion

    airforce = {
        "status": 200,
        "body": {
            "error": {
                "message": "No content was returned",
                "type": "upstream_unavailable",
                "code": "503",
            }
        },
    }
    assert is_usable_completion(airforce) is False
    assert is_usable_completion({"status": 200, "body": {"choices": []}}) is False
    assert is_usable_completion({"status": 200, "body": {}}) is False
    assert is_usable_completion({"status": 200, "body": "not json"}) is False
    assert is_usable_completion({"status": 429, "body": {"choices": [{"message": {}}]}}) is False
    assert (
        is_usable_completion({"status": 200, "body": {"choices": [{"message": {"content": "hi"}}]}})
        is True
    )


def test_endurance_uses_a_timeout_long_enough_for_slow_providers(monkeypatch):
    """NVIDIA has been observed taking >45s for a one-token request. At the 15s default those
    routes record as transport failures, and an endurance run would recommend disabling them for
    being slow rather than broken."""
    import citypods.llm_rate_probe as probe

    captured = {}

    class _Runner(probe.RateProbeRunner):
        def __init__(self, *a, **kw):
            captured["timeout"] = kw.get("request_timeout_seconds")
            super().__init__(*a, **kw)

    monkeypatch.setattr(probe, "RateProbeRunner", _Runner)
    probe.run_endurance([], apply=False, hours=0.001, interval_seconds=1)
    assert captured["timeout"] >= 60


def test_a_200_without_a_completion_labels_as_upstream_capacity_not_unknown():
    """Airforce logged 43 of these in one 3-hour run, all labelled "unknown" -- which hid the
    single most important fact about that route. A 2xx with no completion is the provider having
    nothing to serve."""
    from citypods.llm_rate_probe import _throttle_class

    empty_200 = {
        "status": 200,
        "headers": {},
        "body": {"error": {"message": "No content was returned", "code": "503"}},
    }
    assert _throttle_class({"provider": "airforce"}, empty_200) == "upstream_capacity"

    good = {"status": 200, "headers": {}, "body": {"choices": [{"message": {"content": "x"}}]}}
    assert _throttle_class({"provider": "airforce"}, good) is None
