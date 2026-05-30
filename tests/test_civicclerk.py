"""Unit tests for the CivicClerk adapter."""

from __future__ import annotations

import json

import pytest

from citypods.providers.civicclerk import CivicClerkProvider, parse_events
from tests.conftest import fixture_bytes

SAMPLE = json.dumps(
    {
        "value": [
            {
                "id": 100,
                "eventName": "Commissioners Court Voting Session",
                "startDateTime": "2026-05-19T12:20:00Z",
                "categoryId": 26,
                "hasMedia": True,
                "mediaSourcePathMp4": "https://cpmedia.azureedge.net/traviscotx/abc.mp4",
            },
            {
                "id": 101,
                "eventName": "Press Conference",
                "startDateTime": "2026-05-18T12:20:00Z",
                "categoryId": 26,
                "hasMedia": True,
                "mediaSourcePathMp4": "stream/TRAVISCOTX/relative.mp4",  # relative -> skipped
            },
            {
                "id": 102,
                "eventName": "Other Category",
                "startDateTime": "2026-05-17T12:20:00Z",
                "categoryId": 5,
                "hasMedia": True,
                "mediaSourcePathMp4": "https://cpmedia.azureedge.net/traviscotx/def.mp4",
            },
            {
                "id": 103,
                "eventName": "No media yet",
                "startDateTime": "2026-05-16T12:20:00Z",
                "categoryId": 26,
                "hasMedia": False,
                "mediaSourcePathMp4": "",
            },
        ]
    }
).encode()


def test_parse_includes_only_absolute_mp4():
    eps = parse_events(SAMPLE)
    titles = [e.title for e in eps]
    assert "Commissioners Court Voting Session" in titles
    assert "Press Conference" not in titles  # relative path skipped
    assert "No media yet" not in titles  # hasMedia False skipped
    ep = next(e for e in eps if e.guid == "100")
    assert ep.media_kind == "direct"
    assert ep.video_url == "https://cpmedia.azureedge.net/traviscotx/abc.mp4"
    assert ep.published.year == 2026


def test_category_filter():
    eps = parse_events(SAMPLE, category_id=26)
    assert all(e.guid != "102" for e in eps)  # different category excluded
    eps_all = parse_events(SAMPLE)
    assert any(e.guid == "102" for e in eps_all)


def test_invalid_json_raises():
    from citypods.providers.base import ProviderError

    with pytest.raises(ProviderError):
        parse_events(b"{not json")


def test_validate_and_resolve():
    p = CivicClerkProvider()
    with pytest.raises(ValueError):
        p.validate({})
    p.validate({"api_base": "https://x.api.civicclerk.com"})
    assert p.detect_change({"api_base": "x"}) is None
    eps = parse_events(SAMPLE)
    assert p.resolve_media_url(eps[0], {}) == eps[0].video_url


def test_recorded_fixture_parses():
    eps = parse_events(fixture_bytes("civicclerk", "travis-county-tx"), category_id=26)
    assert eps
    assert all(e.media_kind == "direct" and e.video_url.startswith("http") for e in eps)
