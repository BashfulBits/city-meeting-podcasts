from __future__ import annotations

import json

import pytest

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


def test_validate():
    provider = OneMeetingProvider()
    provider.validate({"portal_url": "https://wacotexas.primegov.com", "backfill_since": 2020})
    with pytest.raises(ValueError, match="portal_url"):
        provider.validate({"portal_url": "http://wacotexas.primegov.com"})


def test_fetch_years_and_sort(monkeypatch):
    provider = OneMeetingProvider()
    payload = json.dumps(PAYLOAD).encode()
    monkeypatch.setattr(provider, "_fetch_year", lambda source, year: payload)
    records = provider.fetch_agenda_index(
        {
            "portal_url": "https://wacotexas.primegov.com",
            "backfill_since": 2025,
            "through_year": 2026,
        }
    )
    assert len(records) == 2
    assert records[0].published >= records[1].published
