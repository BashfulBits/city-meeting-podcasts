from __future__ import annotations

import pytest

from citypods.swagit_proxy import SwagitWorkerFallback, get_with_worker_fallback

LIST_URL = "https://austintx.new.swagit.com/views/117/city-council-meetings"
LIST_URL_PAGED = f"{LIST_URL}?page=3"
WORKER_URL = (
    "https://worker.example/v1/swagit/austintx.new.swagit.com/views/117/city-council-meetings"
)


def _fallback(monkeypatch) -> SwagitWorkerFallback:
    monkeypatch.setenv("SWAGIT_PROXY_BASE_URL", "worker.example")
    monkeypatch.setenv("SWAGIT_PROXY_TOKEN", "secret-token")
    monkeypatch.setattr("citypods.swagit_proxy.validate_source_url", lambda *a, **kw: None)
    fallback = SwagitWorkerFallback.from_env()
    assert fallback is not None
    return fallback


def test_from_env_requires_both_values(monkeypatch):
    monkeypatch.setenv("SWAGIT_PROXY_BASE_URL", "worker.example")
    monkeypatch.delenv("SWAGIT_PROXY_TOKEN", raising=False)
    with pytest.raises(ValueError, match="configured together"):
        SwagitWorkerFallback.from_env()


def test_from_env_returns_none_when_unset(monkeypatch):
    monkeypatch.delenv("SWAGIT_PROXY_BASE_URL", raising=False)
    monkeypatch.delenv("SWAGIT_PROXY_TOKEN", raising=False)
    assert SwagitWorkerFallback.from_env() is None


def test_proxy_url_accepts_only_view_list_pages(monkeypatch):
    fallback = _fallback(monkeypatch)
    assert fallback.proxy_url(LIST_URL) == WORKER_URL
    assert fallback.proxy_url(LIST_URL_PAGED) == f"{WORKER_URL}?page=3"
    # Not a views/ list page (a video/download URL).
    assert fallback.proxy_url("https://austintx.new.swagit.com/videos/12345/download") is None
    # An unexpected extra query param -- not a shape production ever produces.
    assert fallback.proxy_url(f"{LIST_URL}?page=1&token=secret") is None
    # Out-of-range page.
    assert fallback.proxy_url(f"{LIST_URL}?page=0") is None
    assert fallback.proxy_url(f"{LIST_URL}?page=99999") is None


def test_headers_carry_the_bearer_token(monkeypatch):
    fallback = _fallback(monkeypatch)
    assert fallback.headers() == {"Authorization": "Bearer secret-token"}


class _Resp:
    def __init__(self, status_code: int, content: bytes = b"") -> None:
        self.status_code = status_code
        self.content = content


class _RecordingSession:
    def __init__(self, responses: list[_Resp]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, *, timeout, headers=None, allow_redirects=True):
        self.calls.append(
            (url, {"timeout": timeout, "headers": headers, "allow_redirects": allow_redirects})
        )
        return self._responses.pop(0)


def test_get_with_worker_fallback_passes_through_on_success(monkeypatch):
    monkeypatch.setattr("citypods.swagit_proxy.validate_source_url", lambda *a, **kw: None)
    session = _RecordingSession([_Resp(200, b"ok")])
    result = get_with_worker_fallback(session, LIST_URL, timeout=5)
    assert result.status_code == 200
    assert len(session.calls) == 1
    assert session.calls[0][1]["allow_redirects"] is False


def test_get_with_worker_fallback_is_noop_without_config(monkeypatch):
    monkeypatch.delenv("SWAGIT_PROXY_BASE_URL", raising=False)
    monkeypatch.delenv("SWAGIT_PROXY_TOKEN", raising=False)
    monkeypatch.setattr("citypods.swagit_proxy.validate_source_url", lambda *a, **kw: None)
    session = _RecordingSession([_Resp(403, b"forbidden")])
    result = get_with_worker_fallback(session, LIST_URL, timeout=5)
    assert result.status_code == 403
    assert len(session.calls) == 1  # no retry attempted


def test_get_with_worker_fallback_retries_once_through_worker_on_403(monkeypatch):
    monkeypatch.setenv("SWAGIT_PROXY_BASE_URL", "worker.example")
    monkeypatch.setenv("SWAGIT_PROXY_TOKEN", "secret-token")
    monkeypatch.setattr("citypods.swagit_proxy.validate_source_url", lambda *a, **kw: None)
    session = _RecordingSession([_Resp(403, b"forbidden"), _Resp(200, b"<html>ok</html>")])
    result = get_with_worker_fallback(session, LIST_URL, timeout=5)
    assert result.status_code == 200
    assert len(session.calls) == 2
    assert session.calls[0][0] == LIST_URL
    assert session.calls[0][1]["allow_redirects"] is False
    assert session.calls[1][0] == WORKER_URL
    assert session.calls[1][1]["headers"] == {"Authorization": "Bearer secret-token"}
    assert session.calls[1][1]["allow_redirects"] is False


def test_get_with_worker_fallback_degrades_on_misconfigured_env(monkeypatch, capsys):
    monkeypatch.setenv("SWAGIT_PROXY_BASE_URL", "worker.example")
    monkeypatch.delenv("SWAGIT_PROXY_TOKEN", raising=False)
    monkeypatch.setattr("citypods.swagit_proxy.validate_source_url", lambda *a, **kw: None)
    session = _RecordingSession([_Resp(403, b"forbidden")])
    result = get_with_worker_fallback(session, LIST_URL, timeout=5)
    assert result.status_code == 403
    assert len(session.calls) == 1  # no retry -- misconfig degrades to direct-only
    assert "misconfigured" in capsys.readouterr().out
