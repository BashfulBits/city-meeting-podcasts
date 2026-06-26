"""CivicClerk provider.

CivicClerk exposes a public OData JSON API at ``<tenant>.api.civicclerk.com/v1/Events``.
Each event carries metadata plus ``mediaSourcePathMp4`` — for recorded meetings this is an
absolute progressive MP4 on CivicPlus's CDN (``cpmedia.azureedge.net``), so it's used as a
direct enclosure (like Granicus). Non-meeting items (e.g. press conferences) carry only a
relative streaming path and are skipped.

Source config:
    provider: civicclerk
    source:
      api_base: https://traviscotx.api.civicclerk.com
      category_id: 26          # optional: restrict to one meeting category
      max_fetch: 100           # optional: how many recent events to request
"""

from __future__ import annotations

import json
from datetime import datetime
from urllib.parse import urlsplit

import requests

from citypods.http import DEFAULT_TIMEOUT, make_session
from citypods.models import ChangeToken, Episode
from citypods.providers.base import ProviderError


def _parse_dt(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _event_duration(e: dict) -> int | None:
    live_start = _parse_dt(e.get("liveStartTime", ""))
    live_end = _parse_dt(e.get("liveEndTime", ""))
    if (
        live_start is not None
        and live_end is not None
        and live_start.year > 1900
        and live_end > live_start
    ):
        return round((live_end - live_start).total_seconds())

    try:
        hours = int(e.get("durationHrs") or 0)
        minutes = int(e.get("durationMin") or 0)
    except (TypeError, ValueError):
        return None
    duration = hours * 3600 + minutes * 60
    return duration if duration > 0 else None


# CivicClerk "published file" types -> our normalized link keys. Each published file has a
# stable ``fileId`` served by the OData file-stream endpoint (verified live), so we can link
# to the agenda/packet/minutes/transcript PDFs that back every meeting.
_FILE_TYPE_LINKS = {
    "Agenda": "agenda",
    "Agenda Packet": "agenda_packet",
    "Minutes": "minutes",
    "Transcript": "transcript",
}


def _file_stream_url(api_base: str, file_id: int) -> str:
    base = api_base.rstrip("/")
    return f"{base}/v1/Meetings/GetMeetingFileStream(fileId={file_id},plainText=false)"


def _published_links(event: dict, api_base: str) -> dict:
    """Map a CivicClerk event's ``publishedFiles`` to resource links (agenda, packet,
    minutes, transcript). Needs ``api_base`` to build absolute file-stream URLs."""
    if not api_base:
        return {}
    links: dict[str, str] = {}
    for f in event.get("publishedFiles") or []:
        key = _FILE_TYPE_LINKS.get((f.get("type") or "").strip())
        fid = f.get("fileId")
        if key and fid and key not in links:  # first of each type wins (handles "REVISED" dupes)
            links[key] = _file_stream_url(api_base, fid)
    return links


def parse_events(
    content: bytes, api_base: str = "", category_id: int | None = None
) -> list[Episode]:
    """Parse a CivicClerk OData Events payload into direct-MP4 episodes.

    Includes only published events whose ``mediaSourcePathMp4`` is an absolute URL
    (recorded meetings); relative/streaming-only items are skipped. ``api_base`` (when given)
    is used to build absolute agenda/minutes/transcript file links. Pure (no network).
    """
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"invalid CivicClerk JSON: {exc}") from exc

    episodes: list[Episode] = []
    for e in data.get("value", []):
        if category_id is not None and e.get("categoryId") != category_id:
            continue
        if not e.get("hasMedia"):
            continue
        mp4 = (e.get("mediaSourcePathMp4") or "").strip()
        mp4_parts = urlsplit(mp4)
        if mp4_parts.scheme != "https" or not mp4_parts.netloc:
            continue  # relative streaming path (e.g. press conferences) — not a meeting MP4
        published = _parse_dt(e.get("startDateTime", ""))
        if published is None:
            continue
        episodes.append(
            Episode(
                guid=str(e.get("id")),
                title=e.get("eventName") or "Untitled meeting",
                published=published,
                video_url=mp4,
                description=e.get("eventDescription") or "",
                media_kind="direct",
                duration=_event_duration(e),
                body=(e.get("categoryName") or e.get("meetingTypeName") or "").strip() or None,
                links=_published_links(e, api_base),
            )
        )
    return episodes


def parse_bookmarks(media: dict) -> tuple[list[dict], str | None]:
    """Extract ``(chapters, transcript_url)`` from a CivicClerk ``EventMediaApiModel``.

    ``eventBookmarks`` -> ``[{"start": secs, "title": str}, ...]`` (markers at second 0 are
    section labels with no real video position and are dropped). Transcript prefers
    ``transcriptionUrl``, then the closed-caption track. Pure (no network)."""
    by_start: dict[int, dict] = {}
    for b in media.get("eventBookmarks") or []:
        try:
            start = int(b.get("markerTimeStart"))
        except (TypeError, ValueError):
            continue
        title = (b.get("markerTitle") or b.get("markerName") or "").strip()
        if start > 0 and title:
            by_start.setdefault(start, {"start": start, "title": title})
    chapters = [by_start[s] for s in sorted(by_start)]
    transcript = (media.get("transcriptionUrl") or media.get("closedCaptionUrl") or "").strip()
    return chapters, (transcript or None)


class CivicClerkProvider:
    name = "civicclerk"
    # CivicClerk episodes are direct CDN MP4s; no confirmed time-anchored player URL.
    # Revisit when a public portal deep-link format is established.
    capabilities: frozenset[str] = frozenset()

    def video_deeplink(self, ref: str, t_seconds: float) -> str | None:
        return None

    def validate(self, source: dict) -> None:
        if not source.get("api_base"):
            raise ValueError("civicclerk source requires 'api_base'")

    def detect_change(self, source: dict) -> ChangeToken | None:
        # The Events list is one cheap API call, so there's no separate change probe;
        # always fetch (matches other providers without usable validators).
        return None

    def fetch_episodes(self, source: dict) -> list[Episode]:
        base = source["api_base"].rstrip("/")
        top = int(source.get("max_fetch", 100))
        params = {
            "$filter": "hasMedia eq true",
            "$orderby": "startDateTime desc",
            "$top": str(top),
        }
        url = f"{base}/v1/Events"
        with make_session() as session:
            try:
                resp = session.get(url, params=params, timeout=DEFAULT_TIMEOUT)
            except requests.RequestException as exc:
                raise ProviderError(f"GET {url} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(f"GET {url} returned {resp.status_code}")
        return parse_events(resp.content, api_base=base, category_id=source.get("category_id"))

    def fetch_chapters(self, episode: Episode, source: dict) -> tuple[list[dict], str | None]:
        """Fetch chapter markers + a transcript/caption link from the ``EventsMedia/{id}``
        endpoint (one call). ``eventBookmarks`` carry ``markerTimeStart`` (seconds) and
        ``markerTitle``; section headers with ``markerTimeStart == 0`` (no video position) are
        dropped. The transcript link prefers ``transcriptionUrl``, falling back to the closed-
        caption ``.srt``. Returns ``([], None)`` when the event has no bookmarks."""
        base = source["api_base"].rstrip("/")
        url = f"{base}/v1/EventsMedia/{episode.guid}"
        with make_session() as session:
            try:
                resp = session.get(url, timeout=DEFAULT_TIMEOUT)
            except requests.RequestException as exc:
                raise ProviderError(f"GET {url} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(f"GET {url} returned {resp.status_code}")
        try:
            data = json.loads(resp.content)
        except json.JSONDecodeError as exc:
            raise ProviderError(f"invalid EventsMedia JSON: {exc}") from exc
        return parse_bookmarks(data)

    def resolve_media_url(self, episode: Episode, source: dict) -> str:
        return episode.video_url  # direct MP4
