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


def test_paused_waits_for_the_selection_to_drain_then_resumes():
    session = FakeSession([3, 1, 0])
    sleeps: list[float] = []
    selection = Selection("provider", "nvidia")
    with paused(
        selection, client=_client(session), sleep=sleeps.append, clock=lambda: 0.0
    ) as outcome:
        assert outcome.drained and not outcome.contended
        assert outcome.in_flight_at_start == 3
    assert _paths(session) == [
        "v2/dispatch:pause",
        "v2/dispatch:pause-status",
        "v2/dispatch:pause-status",
        "v2/dispatch:pause-status",
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
    assert len(sleeps) == 2


def test_paused_marks_results_contended_when_the_drain_times_out():
    session = FakeSession([2])
    ticks = iter([0.0, 5.0, 400.0, 400.0])
    with paused(
        Selection("route", "r1"),
        client=_client(session),
        drain_timeout=300,
        sleep=lambda _: None,
        clock=lambda: next(ticks),
    ) as outcome:
        assert outcome.contended
    assert _paths(session)[-1] == "v2/dispatch:resume"


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
