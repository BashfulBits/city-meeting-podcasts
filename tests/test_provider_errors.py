"""Unit tests for provider error classification and retryability."""

from __future__ import annotations

import pytest
import requests

from citypods.providers.base import ProviderError, is_transient_provider_error


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (500, True),
        (502, True),
        (503, True),
        (504, True),
        (520, True),
        (521, True),
        (522, True),
        (523, True),
        (524, True),
        (525, True),
        (526, True),
        (527, True),
        (408, True),
        (425, True),
        (429, True),
        (400, False),
        (401, False),
        (403, False),
        (404, False),
        (405, False),
        (410, False),
        (422, False),
    ],
)
def test_is_transient_provider_error_status_code_attribute(status_code: int, expected: bool):
    exc = ProviderError(f"GET https://example.com returned {status_code}", status_code=status_code)
    assert is_transient_provider_error(exc) is expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("GET https://fortworthgov.granicus.com/ViewPublisher.php?view_id=9 returned 500", True),
        ("GET https://example.com/feed returned 502", True),
        ("GET https://example.com/feed returned 503", True),
        ("GET https://example.com/feed returned 504", True),
        ("GET https://example.com/feed returned 429", True),
        ("GET https://example.com/feed returned 408", True),
        ("POST https://example.com/api returned 500", True),
        ("server returned HTTP 503 Service Unavailable", True),
        ("GET https://example.com returned 404", False),
        ("GET https://example.com returned 400", False),
        ("GET https://example.com returned 401", False),
        ("invalid CivicClerk JSON: Expecting value", False),
        ("no TikiLive embed URL found on https://example.com", False),
    ],
)
def test_is_transient_provider_error_message_parsing(message: str, expected: bool):
    exc = ProviderError(message)
    assert is_transient_provider_error(exc) is expected


def test_is_transient_provider_error_requests_transport_causes():
    conn_err = requests.ConnectionError("Connection reset by peer")
    exc_conn = ProviderError("GET https://example.com failed: Connection reset")
    exc_conn.__cause__ = conn_err
    assert is_transient_provider_error(exc_conn) is True

    timeout_err = requests.Timeout("Read timed out")
    exc_timeout = ProviderError("GET https://example.com failed: Timeout")
    exc_timeout.__cause__ = timeout_err
    assert is_transient_provider_error(exc_timeout) is True


def test_is_transient_provider_error_http_error_response():
    resp = requests.Response()
    resp.status_code = 503
    http_err = requests.HTTPError(response=resp)
    exc = ProviderError("upstream gateway failed")
    exc.__cause__ = http_err
    assert is_transient_provider_error(exc) is True

    resp_404 = requests.Response()
    resp_404.status_code = 404
    http_404 = requests.HTTPError(response=resp_404)
    exc_404 = ProviderError("resource not found")
    exc_404.__cause__ = http_404
    assert is_transient_provider_error(exc_404) is False


def test_is_transient_provider_error_cycle_guard():
    err1 = ProviderError("wrapper 1")
    err2 = ProviderError("wrapper 2")
    err1.__cause__ = err2
    err2.__cause__ = err1
    assert is_transient_provider_error(err1) is False
