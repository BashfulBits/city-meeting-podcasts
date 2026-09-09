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
