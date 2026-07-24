from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from citypods.security import SecurityError

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "probe_swagit_transport.py"
SPEC = importlib.util.spec_from_file_location("probe_swagit_transport", SCRIPT)
assert SPEC and SPEC.loader
probe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = probe
SPEC.loader.exec_module(probe)


@pytest.mark.parametrize(
    ("status_code", "make_exc", "expected"),
    [
        (200, None, "success"),
        (302, None, "success"),
        (403, None, "http_403"),
        (429, None, "http_429"),
        (503, None, "http_503"),
        (504, None, "http_504"),
        (None, lambda requests: requests.Timeout("timed out"), "timeout"),
        (None, lambda requests: requests.ConnectionError("refused"), "connection_error"),
        (None, lambda requests: requests.RequestException("weird"), "error"),
    ],
)
def test_outcome_classification(status_code, make_exc, expected):
    import requests

    exc = make_exc(requests) if make_exc else None
    assert probe._outcome(status_code, exc) == expected


def test_default_tenant_urls_are_real_swagit_hosts():
    # Real config/feeds list_urls (not synthetic), so a probe result is directly comparable to
    # the Audio #257/#258 failures rather than testing an endpoint production never touches.
    assert set(probe.DEFAULT_TENANT_URLS) == {"addison", "austin", "dallas", "denton", "waco"}
    for url in probe.DEFAULT_TENANT_URLS.values():
        assert url.startswith("https://")
        assert (urlsplit(url).hostname or "").endswith(".swagit.com")
        assert "?" not in url


def test_default_tenant_urls_pass_the_swagit_host_allowlist():
    for url in probe.DEFAULT_TENANT_URLS.values():
        probe.validate_source_url(url, allowed_hosts=probe.ALLOWED_HOSTS, resolve=False)


def test_probe_rejects_unapproved_host():
    with pytest.raises(SecurityError):
        probe.validate_source_url(
            "https://example.com/videos/1", allowed_hosts=probe.ALLOWED_HOSTS, resolve=False
        )


@pytest.mark.parametrize(
    ("headers", "body_snippet", "expected"),
    [
        ({}, "ordinary swagit archive page content", False),
        ({"cf-mitigated": "challenge"}, "", True),
        ({}, "Sorry, you have been blocked", True),
        ({}, "Attention Required! | Cloudflare", True),
    ],
)
def test_waf_signature_detection(headers, body_snippet, expected):
    assert probe._waf_signature(headers, body_snippet) == expected
