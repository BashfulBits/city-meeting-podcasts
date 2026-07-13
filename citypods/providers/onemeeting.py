"""PrimeGov/OneMeeting public-portal provider.

PrimeGov exposes archived meetings through a small JSON endpoint.  The endpoint is useful as an
auxiliary agenda source even when the playable recording is supplied by Swagit: every meeting
row carries first-party compiled-document links for its agenda, packet, and minutes.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

import requests

from citypods.http import DEFAULT_TIMEOUT, make_session
from citypods.models import AgendaRecord, ChangeToken, Episode
from citypods.providers.base import ProviderError
from citypods.security import validate_source_url


def _parse_datetime(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _body(title: str) -> str:
    return re.sub(r"^\s*\d{1,2}/\d{1,2}(?:/\d{2,4})?\s+", "", title).strip() or title.strip()


def _document_url(base: str, template_id: object, output_type: object = 1) -> str | None:
    if template_id in (None, ""):
        return None
    encoded_id = quote(str(template_id), safe="")
    encoded_output = quote(str(output_type if output_type is not None else 1), safe="")
    return (
        f"{base.rstrip('/')}/Public/CompiledDocument?meetingTemplateId={encoded_id}"
        f"&compileOutputType={encoded_output}"
    )


def parse_archived_meetings(content: bytes, portal_url: str) -> list[AgendaRecord]:
    """Parse one ``ListArchivedMeetings`` response into official agenda records."""
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"invalid PrimeGov JSON: {exc}") from exc
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("data", payload.get("items", []))
    else:
        raise ProviderError("PrimeGov response did not contain a meeting list")
    if not isinstance(rows, list):
        raise ProviderError("PrimeGov response did not contain a meeting list")

    base = portal_url.rstrip("/")
    records: list[AgendaRecord] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        published = _parse_datetime(str(row.get("dateTime") or row.get("meetingDate") or ""))
        title = str(row.get("title") or row.get("meetingName") or "").strip()
        if published is None or not title:
            continue
        links: dict[str, str] = {}
        for document in row.get("documentList") or []:
            if not isinstance(document, dict):
                continue
            name = str(document.get("templateName") or "").lower()
            key = (
                "agenda_packet"
                if "packet" in name
                else "minutes"
                if "minute" in name
                else "agenda"
                if "agenda" in name
                else None
            )
            url = _document_url(base, document.get("templateId"), document.get("compileOutputType"))
            if key and url and key not in links:
                links[key] = url
        if links:
            records.append(
                AgendaRecord(body=_body(title), published=published, title=title, links=links)
            )
    return records


class OneMeetingProvider:
    name = "onemeeting"
    capabilities: frozenset[str] = frozenset()

    def validate(self, source: dict) -> None:
        portal = source.get("portal_url")
        if (
            not isinstance(portal, str)
            or urlsplit(portal).scheme != "https"
            or not urlsplit(portal).netloc
        ):
            raise ValueError("onemeeting source requires an https 'portal_url'")
        since = source.get("backfill_since")
        if since is not None and (not isinstance(since, int) or since < 2000):
            raise ValueError("onemeeting 'backfill_since' must be a year >= 2000")
        # Config validation must remain offline; make_session applies the resolving SSRF gate
        # when the endpoint is actually fetched.
        validate_source_url(portal, resolve=False)

    def detect_change(self, source: dict) -> ChangeToken | None:
        return None

    def fetch_episodes(self, source: dict) -> list[Episode]:
        # PrimeGov's videoUrl is an opaque external link and is not assumed to be a playable
        # enclosure. Swagit remains the media provider; this adapter is auxiliary-only.
        return []

    def _fetch_year(self, session: requests.Session, source: dict, year: int) -> bytes:
        portal = source["portal_url"].rstrip("/")
        url = f"{portal}/api/v2/PublicPortal/ListArchivedMeetings?year={year}"
        try:
            response = session.get(url, timeout=DEFAULT_TIMEOUT)
        except requests.RequestException as exc:
            raise ProviderError(f"GET {url} failed: {exc}") from exc
        if response.status_code == 404:
            return b"[]"
        if response.status_code >= 400:
            raise ProviderError(f"GET {url} returned {response.status_code}")
        return response.content

    def fetch_agenda_index(self, source: dict) -> list[AgendaRecord]:
        start = int(source.get("backfill_since", datetime.now(UTC).year))
        end = int(source.get("through_year", datetime.now(UTC).year))
        if end < start:
            raise ProviderError("onemeeting through_year precedes backfill_since")
        records: list[AgendaRecord] = []
        with make_session() as session:
            for year in range(start, end + 1):
                records.extend(
                    parse_archived_meetings(
                        self._fetch_year(session, source, year), source["portal_url"]
                    )
                )
        records.sort(key=lambda record: record.published, reverse=True)
        return records

    def resolve_media_url(self, episode: Episode, source: dict) -> str:
        return episode.video_url

    def video_deeplink(self, ref: str, t_seconds: float) -> str | None:
        return None
