"""Offline tests for the v2 dispatch pause helper (no network)."""

from __future__ import annotations

import pytest

from citypods.compute.llm_dispatch_pause import (
    DispatchPauseClient,
    DispatchPauseError,
    Selection,
    paused,
)


class FakeResponse:
    def __init__(self, status: int, body: dict) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> dict:
        return self._body


class FakeSession:
    """Scripted Worker: records calls and reports a draining in-flight count."""

    def __init__(self, in_flight: list[int], *, fail_resume: bool = False) -> None:
        self.in_flight = list(in_flight)
        self.fail_resume = fail_resume
        self.calls: list[tuple[str, str, dict | None, dict]] = []

    def request(self, method, url, headers=None, json=None, timeout=None):
        self.calls.append((method, url, json, headers or {}))
        if "pause-status" in url:
            count = self.in_flight.pop(0) if len(self.in_flight) > 1 else self.in_flight[0]
            return FakeResponse(200, {"ok": True, "in_flight": count})
        if url.endswith("dispatch:resume") and self.fail_resume:
            return FakeResponse(500, {"error": "coordinator_error", "detail": "boom"})
        return FakeResponse(200, {"ok": True})


def _client(session: FakeSession) -> DispatchPauseClient:
    return DispatchPauseClient("https://worker.test", "tok", session=session)


def _paths(session: FakeSession) -> list[str]:
    return [url.split("worker.test/")[1].split("?")[0] for _, url, _, _ in session.calls]


class FakeClock:
    """Monotonic clock that advances only when the code under test sleeps (or a test says so)."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_paused_waits_for_the_selection_to_drain_then_resumes():
    session = FakeSession([3, 1, 0])
    clock = FakeClock()
    selection = Selection("provider", "nvidia")
    with paused(selection, client=_client(session), sleep=clock.sleep, clock=clock) as outcome:
        assert outcome.drained
        assert outcome.in_flight_at_start == 3
        assert outcome.waited_seconds == 20
    assert not outcome.contended
    assert _paths(session) == [
        "v2/dispatch:pause",
        "v2/dispatch:pause-status",
        "v2/dispatch:pause-status",
        "v2/dispatch:pause-status",
        "v2/dispatch:pause",  # re-armed so the probe starts with a full window
        "v2/dispatch:resume",
    ]
    assert session.calls[0][2] == {
        "scope": "provider",
        "target": "nvidia",
        "seconds": 900,
        "reason": "provider probe",
    }
    assert session.calls[0][3]["authorization"] == "Bearer tok"
    assert "scope=provider&target=nvidia" in session.calls[1][1]


def test_paused_marks_results_contended_when_the_drain_times_out():
    session = FakeSession([2])
    clock = FakeClock()
    with paused(
        Selection("route", "r1"),
        client=_client(session),
        drain_timeout=300,
        sleep=clock.sleep,
        clock=clock,
    ) as outcome:
        pass
    assert not outcome.drained and outcome.contended
    assert _paths(session)[-1] == "v2/dispatch:resume"


def test_paused_renews_a_short_pause_while_draining():
    # A 15 s pause with a 10 s poll would lapse mid-drain; it must be re-armed before each gap.
    session = FakeSession([4, 3, 2, 1, 0])
    clock = FakeClock()
    with paused(
        Selection("provider", "gemini"),
        seconds=15,
        client=_client(session),
        sleep=clock.sleep,
        clock=clock,
    ) as outcome:
        pass
    assert outcome.drained and not outcome.contended
    pauses = [c for c in session.calls if c[1].endswith("dispatch:pause")]
    assert len(pauses) >= 4  # initial + renewals during the 40 s drain + the pre-probe re-arm


def test_a_probe_that_outlives_its_pause_is_contended_unless_renewed():
    session = FakeSession([0])
    clock = FakeClock()
    with paused(
        Selection("global"), seconds=60, client=_client(session), sleep=clock.sleep, clock=clock
    ) as outcome:
        clock.now += 61
    assert outcome.pause_expired and outcome.contended

    clock = FakeClock()
    with paused(
        Selection("global"), seconds=60, client=_client(session), sleep=clock.sleep, clock=clock
    ) as renewed:
        clock.now += 50
        renewed.renew()
        clock.now += 50
    assert not renewed.pause_expired and not renewed.contended


def test_a_status_without_a_valid_in_flight_count_is_an_error_not_a_drain():
    for bad in (None, "0", -1, 1.5):

        class BadStatus(FakeSession):
            def request(self, method, url, headers=None, json=None, timeout=None, _bad=bad):
                if "pause-status" in url:
                    self.calls.append((method, url, json, headers or {}))
                    return FakeResponse(200, {"ok": True, "in_flight": _bad})
                return super().request(method, url, headers, json, timeout)

        session = BadStatus([0])
        with pytest.raises(DispatchPauseError, match="in_flight"):
            with paused(Selection("global"), client=_client(session), sleep=lambda _: None):
                pass
        assert _paths(session)[-1] == "v2/dispatch:resume"


def test_a_token_is_never_sent_over_plain_http(monkeypatch):
    for name in ("CITYPODS_LLM_DISPATCH_V2_AUTH_TOKEN", "LLM_DISPATCH_V2_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(DispatchPauseError, match="https"):
        DispatchPauseClient("http://worker.test", "tok", session=FakeSession([0]))
    # Tokenless plain HTTP stays allowed for a local `wrangler dev`.
    DispatchPauseClient("http://localhost:8787", None, session=FakeSession([0]))


def test_paused_resumes_even_when_the_probe_raises():
    session = FakeSession([0])
    with pytest.raises(RuntimeError, match="probe failed"):
        with paused(Selection("global"), client=_client(session), sleep=lambda _: None):
            raise RuntimeError("probe failed")
    assert _paths(session)[-1] == "v2/dispatch:resume"
    assert session.calls[0][2] == {"scope": "global", "seconds": 900, "reason": "provider probe"}


def test_a_failed_resume_does_not_mask_the_probe_result(capsys):
    session = FakeSession([0], fail_resume=True)
    with paused(Selection("global"), client=_client(session), sleep=lambda _: None) as outcome:
        assert outcome.drained
    assert "expire by itself" in capsys.readouterr().err


def test_pause_rejects_out_of_range_seconds_before_calling_the_worker():
    session = FakeSession([0])
    with pytest.raises(DispatchPauseError):
        _client(session).pause(Selection("global"), 3601)
    assert session.calls == []


def test_client_requires_a_worker_url(monkeypatch):
    monkeypatch.delenv("LLM_DISPATCH_V2_URL", raising=False)
    monkeypatch.delenv("CITYPODS_LLM_DISPATCH_V2_URL", raising=False)
    with pytest.raises(DispatchPauseError):
        DispatchPauseClient()


@pytest.mark.parametrize("fault", [None, "legacy", "truncated", "invalid_count", "bad_day"])
def test_rate_failures_requires_complete_typed_counter_snapshot(monkeypatch, fault):
    client = _client(FakeSession([0]))
    data = {
        "kind": "rate_failures",
        "utc_day": "2026-10-09",
        "truncated": False,
        "route_failures": [
            {"utc_day": "2026-10-09", "route_id": "route", "failure_class": "own_rpm", "count": 3}
        ],
    }
    if fault == "legacy":
        data = {"utc_day": "2026-10-09"}
    elif fault == "truncated":
        data["truncated"] = True
    elif fault == "invalid_count":
        data["route_failures"][0]["count"] = True
    elif fault == "bad_day":
        data["utc_day"] = "bad"
    calls = []
    monkeypatch.setattr(client, "_request", lambda *args: calls.append(args) or data)
    if fault:
        with pytest.raises(DispatchPauseError):
            client.rate_failures()
    else:
        assert client.rate_failures() == data
    assert calls == [("GET", "v2/stats?rate_failures=1")]


class ContextResponseSession:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return FakeResponse(self.status, self.body)


def test_context_start_and_status_validate_server_owned_allowance():
    body = {
        "ok": True,
        "week_start": "2026-10-05",
        "run_id": "123",
        "deadline_ms": 1000,
        "remaining_input": 2_097_152,
        "remaining_output": 131_072,
        "remaining_requests": 24,
    }
    session = ContextResponseSession(body)
    client = DispatchPauseClient("https://worker.test", "tok", session=session)
    opened = client.start_context("123", "a" * 64)
    assert opened.remaining_requests == 24
    assert session.calls[0][2]["json"]["operation"] == "context_start"
    session.body = {
        "ok": True,
        "context": {
            "enabled": False,
            "catalog_digest": "a" * 64,
            "session": body,
            "routes": {},
        },
    }
    assert client.context_status(Selection("provider", "groq"))["enabled"] is False
    assert "context=1" in session.calls[-1][1]
    session.body["context"]["session"]["remaining_requests"] = True
    with pytest.raises(DispatchPauseError, match="invalid context session"):
        client.context_status(Selection("provider", "groq"))


@pytest.mark.parametrize(
    "response",
    [
        {"ok": True},
        {"ok": True, "disposition": "replay", "attempt_id": "a" * 64},
        {"ok": True, "disposition": "new", "attempt_id": "b" * 64},
        {"ok": False, "error": "already_consumed"},
    ],
)
def test_context_client_never_accepts_an_ambiguous_or_replayed_permission(response):
    session = ContextResponseSession(response)
    client = DispatchPauseClient("https://worker.test", "tok", session=session)
    with pytest.raises(DispatchPauseError):
        client.reserve_context(
            run_id="123",
            catalog_digest="c" * 64,
            route_id="groq_route",
            dimension="input",
            attempt_id="a" * 64,
            request_digest="d" * 64,
            input_tokens=1000,
            output_tokens=256,
        )
    assert len(session.calls) == 1


def test_context_client_accepts_exact_new_attempt_and_does_not_retry_transport_failure():
    session = ContextResponseSession({"ok": True, "disposition": "new", "attempt_id": "a" * 64})
    client = DispatchPauseClient("https://worker.test", "tok", session=session)
    args = dict(
        run_id="123",
        catalog_digest="c" * 64,
        route_id="groq_route",
        dimension="input",
        attempt_id="a" * 64,
        request_digest="d" * 64,
        input_tokens=1000,
        output_tokens=256,
    )
    assert client.reserve_context(**args).attempt_id == "a" * 64
    session.status = 500
    with pytest.raises(DispatchPauseError):
        client.reserve_context(**args)
    assert len(session.calls) == 2
