from __future__ import annotations

import json
from pathlib import Path

import pytest

from citypods.feeds import build_rss
from citypods.models import City
from citypods.providers.onemeeting import OneMeetingProvider, parse_archived_meetings

PAYLOAD = {
    "data": [
        {
            "id": 338,
            "title": "01/07 Building Standards Commission",
            "dateTime": "2026-01-07T15:00:00",
            "documentList": [
                {"templateId": 1668, "templateName": "Agenda", "compileOutputType": 1},
                {"templateId": 1669, "templateName": "Minutes", "compileOutputType": 1},
                {"templateId": 1670, "templateName": "Packet", "compileOutputType": 1},
            ],
        },
        {"title": "Bad row", "dateTime": "not-a-date", "documentList": []},
    ]
}


def test_parse_documents_and_body():
    records = parse_archived_meetings(
        json.dumps(PAYLOAD).encode(), "https://wacotexas.primegov.com"
    )
    assert len(records) == 1
    record = records[0]
    assert record.body == "Building Standards Commission"
    assert record.links["agenda"].endswith("meetingTemplateId=1668&compileOutputType=1")
    assert record.links["minutes"].endswith("meetingTemplateId=1669&compileOutputType=1")
    assert record.links["agenda_packet"].endswith("meetingTemplateId=1670&compileOutputType=1")


def test_parse_rejects_invalid_shape():
    with pytest.raises(Exception, match="meeting list"):
        parse_archived_meetings(b'{"data": {}}', "https://wacotexas.primegov.com")
    with pytest.raises(Exception, match="meeting list"):
        parse_archived_meetings(b'"error"', "https://wacotexas.primegov.com")


def test_document_url_encodes_values_and_preserves_zero():
    from citypods.providers.onemeeting import _document_url

    assert _document_url("https://example.primegov.com", "a&b", 0).endswith(
        "meetingTemplateId=a%26b&compileOutputType=0"
    )


def test_validate():
    provider = OneMeetingProvider()
    provider.validate({"portal_url": "https://wacotexas.primegov.com", "backfill_since": 2020})
    with pytest.raises(ValueError, match="portal_url"):
        provider.validate({"portal_url": "http://wacotexas.primegov.com"})


def test_fetch_years_and_sort(monkeypatch):
    provider = OneMeetingProvider()
    payload = json.dumps(PAYLOAD).encode()
    monkeypatch.setattr(provider, "_fetch_year", lambda session, source, year: payload)
    records = provider.fetch_agenda_index(
        {
            "portal_url": "https://wacotexas.primegov.com",
            "backfill_since": 2025,
            "through_year": 2026,
        }
    )
    assert len(records) == 2
    assert records[0].published >= records[1].published


def test_auxiliary_feed_snapshot(monkeypatch):
    provider = OneMeetingProvider()
    payload = json.dumps(PAYLOAD).encode()
    monkeypatch.setattr(provider, "_fetch_year", lambda session, source, year: payload)
    source = {
        "portal_url": "https://wacotexas.primegov.com",
        "backfill_since": 2026,
        "through_year": 2026,
    }
    records = provider.fetch_agenda_index(source)
    assert records
    city = City(
        slug="waco-tx-city-council",
        provider="swagit",
        source={"list_url": "https://wacotx.new.swagit.com/views/851"},
        podcast_title="Waco: City Council",
        podcast_author="City of Waco, TX",
        podcast_email="",
        podcast_description="City Council meetings for Waco.",
        state="TX",
    )
    generated = build_rss(
        city, provider.fetch_episodes(source), "audio", "https://podcasts.example.gov"
    )
    snapshot = Path(__file__).parent / "snapshots" / "waco-tx-city-council_audio.xml"
    assert generated == snapshot.read_text(encoding="utf-8")
