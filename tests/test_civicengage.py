"""Offline tests for the CivicEngage Archive Center adapter."""

from __future__ import annotations

import pytest

from citypods.providers.civicengage import (
    CivicEngageProvider,
    parse_civicengage_archive,
)

ARCHIVE = b"""
<table>
  <tr><td><a href="Archive.aspx?ADID=100" target="_blank">
    <span>April 7, 2026 Agenda with Communications</span></a></td></tr>
  <tr><td><a href="Archive.aspx?ADID=101">
    <span>02-18-2025 Special Called Meeting Minutes</span></a></td></tr>
  <tr><td><a href="/Archive.aspx?ADID=102"><span>Not a dated document</span></a></td></tr>
</table>
"""


def test_parse_civicengage_archive_extracts_dates_and_detail_pdf_urls():
    records = parse_civicengage_archive(
        ARCHIVE,
        archive_url="https://www.example.gov/Archive.aspx?AMID=36",
        kind="agenda",
        body="City Council",
    )

    assert len(records) == 2
    by_date = {record.published.date().isoformat(): record for record in records}
    assert by_date["2025-02-18"].links["agenda"].endswith("ADID=101")
    assert by_date["2026-04-07"].links["agenda"].endswith("ADID=100")


def test_civicengage_provider_requires_both_archives_and_body(monkeypatch):
    provider = CivicEngageProvider()
    monkeypatch.setattr(
        "citypods.providers.civicengage.validate_source_url", lambda *_a, **_k: None
    )
    with pytest.raises(ValueError):
        provider.validate({"agenda_url": "https://example.gov/a"})
    provider.validate(
        {
            "agenda_url": "https://www.gainesville.tx.us/Archive.aspx?AMID=36",
            "minutes_url": "https://www.gainesville.tx.us/Archive.aspx?AMID=37",
            "body": "City Council",
        }
    )


def test_fetch_agenda_index_merges_agenda_and_minutes(monkeypatch):
    agenda = (
        b'<a href="Archive.aspx?ADID=200">April 7, 2026 Agenda</a>'
        b'<a href="Archive.aspx?ADID=202">April 7, 2026 Revised Agenda</a>'
    )
    minutes = b'<a href="Archive.aspx?ADID=201">April 7, 2026 Minutes</a>'

    class Response:
        status_code = 200

        def __init__(self, content):
            self.content = content

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, timeout=None):
            return Response(agenda if "AMID=36" in url else minutes)

    monkeypatch.setattr("citypods.providers.civicengage.make_session", lambda: Session())
    monkeypatch.setattr(
        "citypods.providers.civicengage.validate_source_url", lambda *_a, **_k: None
    )
    records = CivicEngageProvider().fetch_agenda_index(
        {
            "agenda_url": "https://example.gov/Archive.aspx?AMID=36",
            "minutes_url": "https://example.gov/Archive.aspx?AMID=37",
            "body": "City Council",
        }
    )
    assert len(records) == 1
    assert records[0].links["agenda"].endswith("ADID=200")
    assert records[0].links["agenda_2"].endswith("ADID=202")
    assert records[0].links["minutes"].endswith("ADID=201")
