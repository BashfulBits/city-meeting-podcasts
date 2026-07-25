from __future__ import annotations

import requests

from citypods.provider_request import DENIED_ACCESS_STATUSES, TRANSIENT_TRANSPORT_STATUSES, get


class Response:
    def __init__(self, status_code: int):
        self.status_code = status_code


class Session:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _configure_swagit(monkeypatch):
    monkeypatch.setenv("SWAGIT_PROXY_BASE_URL", "worker.example")
    monkeypatch.setenv("SWAGIT_PROXY_TOKEN", "secret")
    monkeypatch.delenv("GRANICUS_PROXY_BASE_URL", raising=False)
    monkeypatch.delenv("GRANICUS_PROXY_TOKEN", raising=False)
    monkeypatch.setattr("citypods.provider_request.validate_source_url", lambda *a, **k: None)
    monkeypatch.setattr("citypods.swagit_proxy.validate_source_url", lambda *a, **k: None)


def test_every_denied_access_status_retries_through_worker(monkeypatch):
    _configure_swagit(monkeypatch)
    url = "https://austintx.new.swagit.com/videos/123"
    for denied in DENIED_ACCESS_STATUSES:
        session = Session([Response(denied), Response(200)])
        assert get(session, url).status_code == 200
        assert len(session.calls) == 2
        assert "/v1/swagit/" in session.calls[1][0]


def test_exhausted_direct_transport_error_retries_through_worker(monkeypatch):
    _configure_swagit(monkeypatch)
    session = Session([requests.ConnectionError("denied by edge"), Response(200)])
    result = get(session, "https://austintx.new.swagit.com/views/1/council")
    assert result.status_code == 200
    assert len(session.calls) == 2


def test_granicus_metadata_denial_uses_same_api(monkeypatch):
    monkeypatch.delenv("SWAGIT_PROXY_BASE_URL", raising=False)
    monkeypatch.delenv("SWAGIT_PROXY_TOKEN", raising=False)
    monkeypatch.setenv("GRANICUS_PROXY_BASE_URL", "worker.example")
    monkeypatch.setenv("GRANICUS_PROXY_TOKEN", "secret")
    monkeypatch.setattr("citypods.provider_request.validate_source_url", lambda *a, **k: None)
    monkeypatch.setattr("citypods.granicus_proxy.validate_source_url", lambda *a, **k: None)
    session = Session([Response(403), Response(200)])
    result = get(session, "https://arlingtontx.granicus.com/JSON.php?clip_id=123")
    assert result.status_code == 200
    assert "/v1/granicus/arlingtontx.granicus.com/JSON.php?clip_id=123" in session.calls[1][0]


def test_transient_http_failures_also_retry_through_worker(monkeypatch):
    _configure_swagit(monkeypatch)
    for status in TRANSIENT_TRANSPORT_STATUSES:
        session = Session([Response(status), Response(200)])
        assert get(session, "https://austintx.new.swagit.com/videos/123").status_code == 200
        assert len(session.calls) == 2


def test_non_recoverable_response_does_not_add_a_worker_request(monkeypatch):
    _configure_swagit(monkeypatch)
    session = Session([Response(422)])
    assert get(session, "https://austintx.new.swagit.com/videos/123").status_code == 422
    assert len(session.calls) == 1
