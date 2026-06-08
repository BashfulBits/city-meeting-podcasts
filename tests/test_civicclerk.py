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


def test_parse_duration_from_live_times_when_duration_fields_are_zero():
    payload = json.dumps(
        {
            "value": [
                {
                    "id": 200,
                    "eventName": "Voting Session",
                    "startDateTime": "2026-05-19T09:00:00Z",
                    "categoryId": 26,
                    "hasMedia": True,
                    "mediaSourcePathMp4": "https://cpmedia.azureedge.net/traviscotx/abc.mp4",
                    "durationHrs": 0,
                    "durationMin": 0,
                    "liveStartTime": "2026-05-19T08:45:02.107Z",
                    "liveEndTime": "2026-05-19T16:39:50.187Z",
                }
            ]
        }
    ).encode()

    eps = parse_events(payload)

    assert eps[0].duration == 28488


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


def test_published_files_become_agenda_links():
    base = "https://traviscotx.api.civicclerk.com"
    eps = parse_events(
        fixture_bytes("civicclerk", "travis-county-tx"), api_base=base, category_id=26
    )
    with_agenda = [e for e in eps if "agenda" in e.links]
    assert with_agenda, "expected at least one meeting with a published agenda"
    ep = with_agenda[0]
    assert ep.links["agenda"].startswith(f"{base}/v1/Meetings/GetMeetingFileStream(fileId=")
    # known kinds mapped from publishedFiles types
    assert set(ep.links) & {"agenda", "agenda_packet", "minutes", "transcript"}


def test_no_links_without_api_base():
    eps = parse_events(fixture_bytes("civicclerk", "travis-county-tx"), category_id=26)
    assert all(e.links == {} for e in eps)  # api_base omitted -> no file links built


def test_parse_bookmarks_drops_zero_time_and_picks_transcript():
    from citypods.providers.civicclerk import parse_bookmarks

    media = {
        "transcriptionUrl": None,
        "closedCaptionUrl": "https://cdn/x.srt",
        "eventBookmarks": [
            {"markerTimeStart": 0, "markerTitle": "Section Header"},
            {"markerTimeStart": 1231, "markerTitle": "Call to Order"},
            {"markerTimeStart": 1265, "markerName": "Public Communication"},
        ],
    }
    chapters, transcript = parse_bookmarks(media)
    assert chapters == [
        {"start": 1231, "title": "Call to Order"},
        {"start": 1265, "title": "Public Communication"},
    ]
    assert transcript == "https://cdn/x.srt"  # falls back to caption track


def test_parse_bookmarks_prefers_transcription_url():
    from citypods.providers.civicclerk import parse_bookmarks

    chapters, transcript = parse_bookmarks(
        {
            "transcriptionUrl": "https://t/full.pdf",
            "closedCaptionUrl": "https://c/x.srt",
            "eventBookmarks": [],
        }
    )
    assert chapters == [] and transcript == "https://t/full.pdf"
