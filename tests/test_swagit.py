"""Unit tests for the Swagit adapter."""

from __future__ import annotations

import pytest

from citypods.bodies import filter_by_body
from citypods.providers.base import ProviderError
from citypods.providers.swagit import SwagitProvider, parse_list
from tests.conftest import fixture_bytes

ORIGIN = "https://dallastx.new.swagit.com"

SAMPLE = b"""
<table>
<tr><td><a target="_blank" href="/videos/100">City Council Agenda Meetings</a></td>
    <td nowrap> May 27, 2026 </td><td>02h</td></tr>
<tr><td><a target="_blank" href="/videos/101">Board of Adjustments: Panel A</a></td>
    <td nowrap> May 26, 2026 </td><td>01h</td></tr>
<tr><td><a target="_blank" href="/videos/102">City Council Agenda Meetings</a></td>
    <td nowrap> Apr 22, 2026 </td><td>03h</td></tr>
</table>
"""


def test_parse_sets_body_and_media():
    eps = parse_list(SAMPLE, ORIGIN)
    assert [e.guid for e in eps] == ["100", "101", "102"]  # all bodies returned
    ep = eps[0]
    assert ep.body == "City Council Agenda Meetings"
    assert ep.media_kind == "hls"
    assert ep.video_url == f"{ORIGIN}/videos/100/download"
    # canonical_video points at the human watch page, not the expiring download endpoint
    assert ep.links == {"canonical_video": f"{ORIGIN}/videos/100"}
    assert ep.published.year == 2026 and ep.published.month == 5


def test_generic_body_filter():
    eps = parse_list(SAMPLE, ORIGIN)
    assert [e.guid for e in filter_by_body(eps, "City Council Agenda Meetings")] == ["100", "102"]
    assert [e.guid for e in filter_by_body(eps, "Board of Adjustments")] == ["101"]


def test_validate_requires_list_url_and_body():
    p = SwagitProvider()
    with pytest.raises(ValueError):
        p.validate({"list_url": "x"})
    with pytest.raises(ValueError):
        p.validate({"body": "x"})
    p.validate({"list_url": "x", "body": "y"})


class _Resp:
    def __init__(self, status, location=None):
        self.status_code = status
        self.headers = {"Location": location} if location else {}


class _Session:
    def __init__(self, resp):
        self._resp = resp

    def get(self, url, timeout=None, allow_redirects=True):
        return self._resp

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_resolve_follows_download_redirect(monkeypatch):
    presigned = "https://granicus-aasmp-swagit-video.s3.amazonaws.com/dallastx/abc.mp4?X-Amz=1"
    monkeypatch.setattr(
        "citypods.providers.swagit.make_session", lambda: _Session(_Resp(302, presigned))
    )
    eps = parse_list(SAMPLE, ORIGIN)
    assert SwagitProvider().resolve_media_url(eps[0], {}) == presigned


def test_resolve_errors_on_failure(monkeypatch):
    monkeypatch.setattr("citypods.providers.swagit.make_session", lambda: _Session(_Resp(404)))
    eps = parse_list(SAMPLE, ORIGIN)
    with pytest.raises(ProviderError):
        SwagitProvider().resolve_media_url(eps[0], {})


def test_recorded_fixture_parses():
    eps = parse_list(fixture_bytes("swagit", "dallas-tx-city-council"), ORIGIN)
    assert eps
    assert all(e.media_kind == "hls" and e.video_url.endswith("/download") for e in eps)
