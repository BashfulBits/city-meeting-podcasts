"""Legistar Calendar.aspx provider for historical Granicus meeting coverage.

Legistar's public calendar is a server-rendered ASP.NET/Telerik grid. It is an
index only: returned episodes still point to Granicus clips and reuse Granicus'
media-resolution and deep-link behavior.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from html import unescape
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

import requests

from citypods.http import DEFAULT_TIMEOUT, make_session
from citypods.models import AgendaRecord, CalendarIndex, ChangeToken, Episode
from citypods.providers.base import ProviderError
from citypods.providers.granicus import GranicusProvider, _resolve_download_url

_ROW_RE = re.compile(
    r'<tr\s+class="rg(?:Alt)?Row"[^>]*id="ctl00_ContentPlaceHolder1_gridCalendar_ctl00__\d+"[^>]*>(.*?)</tr>',
    re.IGNORECASE | re.DOTALL,
)
_HIDDEN_FIELD_RE = re.compile(
    r'<input[^>]+(?:name|id)="(?P<name>__VIEWSTATE|__VIEWSTATEGENERATOR|__EVENTVALIDATION)"[^>]+value="(?P<value>[^"]*)"',
    re.IGNORECASE,
)
_POSTBACK_RE = re.compile(
    r"NavigateToPage\([^,]+,\s*&#39;(?P<page>\d+)&#39;\).*?__doPostBack\(&#39;(?P<target>[^&#]+)&#39;",
    re.IGNORECASE | re.DOTALL,
)


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html))).strip()


def _source_url(source: dict, key: str) -> str:
    value = str(source.get(key) or "").strip()
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.netloc:
        raise ValueError(f"legistar source {key!r} must be an absolute https URL")
    return value


def _calendar_year_url(calendar_url: str, year: int) -> str:
    parts = urlsplit(calendar_url)
    query = parse_qs(parts.query, keep_blank_values=True)
    query["Mode"] = [str(year)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), ""))


def _extract_aspnet_fields(html: str) -> dict[str, str]:
    fields = {m.group("name"): unescape(m.group("value")) for m in _HIDDEN_FIELD_RE.finditer(html)}
    missing = {"__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"} - set(fields)
    if missing:
        missing_text = ", ".join(sorted(missing))
        raise ProviderError(f"Legistar calendar page missing ASP.NET fields: {missing_text}")
    return fields


def _next_page_target(html: str, current_page: int) -> str | None:
    for match in _POSTBACK_RE.finditer(html):
        if int(match.group("page")) > current_page:
            return unescape(match.group("target"))
    return None


def _iter_year_pages(session, calendar_url: str, year: int):
    """Yield all Legistar grid pages for one calendar year without redirects."""
    url = _calendar_year_url(calendar_url, year)
    try:
        response = session.get(url, timeout=DEFAULT_TIMEOUT, allow_redirects=False)
    except requests.RequestException as exc:
        raise ProviderError(f"GET {url} failed: {exc}") from exc
    if response.status_code >= 300:
        raise ProviderError(f"GET {url} returned {response.status_code}")

    current_page = 1
    html = response.text
    while True:
        yield html
        target = _next_page_target(html, current_page)
        if target is None:
            return
        fields = _extract_aspnet_fields(html)
        fields["__EVENTTARGET"] = target
        try:
            response = session.post(
                url, data=fields, timeout=DEFAULT_TIMEOUT, allow_redirects=False
            )
        except requests.RequestException as exc:
            raise ProviderError(f"POST {url} failed: {exc}") from exc
        if response.status_code >= 300:
            raise ProviderError(f"POST {url} returned {response.status_code}")
        html = response.text
        current_page += 1


def _row_value(row: str, suffix: str) -> str:
    match = re.search(rf'id="[^"]*_{suffix}"[^>]*>(.*?)</a>', row, re.IGNORECASE | re.DOTALL)
    return _text(match.group(1)) if match else ""


def _row_date(row: str) -> datetime | None:
    match = re.search(r'<td\s+class="rgSorted"[^>]*>(.*?)</td>', row, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    try:
        return datetime.strptime(_text(match.group(1)), "%m/%d/%Y").replace(tzinfo=UTC)
    except ValueError:
        return None


def _row_time(row: str) -> tuple[int, int]:
    match = re.search(r'id="[^"]*_lblTime"[^>]*>(.*?)</span>', row, re.IGNORECASE | re.DOTALL)
    if not match:
        return 0, 0
    try:
        parsed = datetime.strptime(_text(match.group(1)), "%I:%M %p")
    except ValueError:
        return 0, 0
    return parsed.hour, parsed.minute


def _row_video_id(row: str) -> str | None:
    match = re.search(r"(?:ID1|clip_id)=([0-9]+)", unescape(row), re.IGNORECASE)
    return match.group(1) if match else None


def _row_view_id(row: str) -> str | None:
    match = re.search(r"view_id=([0-9]+)", unescape(row), re.IGNORECASE)
    return match.group(1) if match else None


def _row_link_url(row: str, calendar_url: str, *suffixes: str) -> str | None:
    """Return a named Legistar grid link, if that column was populated for this row."""
    for suffix in suffixes:
        match = re.search(
            rf'id="[^"]*_{re.escape(suffix)}"[^>]+href="([^"]+)"',
            row,
            re.IGNORECASE,
        )
        if match:
            return urljoin(calendar_url, unescape(match.group(1)))
    return None


def _parse_calendar_records_page(html: str, source: dict) -> list[AgendaRecord]:
    """Parse every dated Legistar calendar row from one HTML grid page (no network)."""
    calendar_url = _source_url(source, "calendar_url")
    granicus_base = _source_url(source, "granicus_base").rstrip("/")
    configured_body = str(source.get("body") or "").strip()
    fallback_view = str(source.get("view_id") or "").strip()
    records: list[AgendaRecord] = []
    for row in _ROW_RE.findall(html):
        body = _row_value(row, "hypBody")
        if not body or (configured_body and body != configured_body):
            continue
        date = _row_date(row)
        if date is None:
            continue
        hour, minute = _row_time(row)
        links = {}
        for key, suffixes in {
            "agenda": ("hypAgenda",),
            "agenda_packet": ("hypAgendaPacket",),
            "minutes": ("hypMinutes",),
            "meeting_details": ("hypMeetingDetails", "hypMeetingDetail"),
        }.items():
            if url := _row_link_url(row, calendar_url, *suffixes):
                links[key] = url

        clip_id = _row_video_id(row)
        video_guid = None
        video_url = None
        if clip_id is not None:
            view_id = _row_view_id(row) or fallback_view
            if not view_id:
                raise ProviderError(
                    "Legistar video row has no view_id and source has no fallback view_id"
                )
            video_guid = f"{granicus_base}/MediaPlayer.php?view_id={view_id}&clip_id={clip_id}"
            video_url = f"{granicus_base}/DownloadFile.php?view_id={view_id}&clip_id={clip_id}"
            links["canonical_video"] = video_guid
            links.setdefault(
                "agenda_portal",
                f"{granicus_base}/AgendaViewer.php?view_id={view_id}&clip_id={clip_id}",
            )

        records.append(
            AgendaRecord(
                body=body,
                title=f"{body} Meeting",
                published=date.replace(hour=hour, minute=minute),
                links=links,
                video_guid=video_guid,
                video_url=video_url,
            )
        )
    return records


def _episode_from_calendar_record(record: AgendaRecord) -> Episode | None:
    """Create an Episode only for a calendar row with an explicit Granicus recording."""
    if not record.video_guid or not record.video_url:
        return None
    return Episode(
        guid=record.video_guid,
        title=record.title or f"{record.body} Meeting",
        published=record.published,
        video_url=record.video_url,
        body=record.body,
        links=dict(record.links),
    )


def _parse_calendar_page(html: str, source: dict) -> list[Episode]:
    """Parse the video-backed subset of one Legistar calendar grid page (no network)."""
    return [
        episode
        for record in _parse_calendar_records_page(html, source)
        if (episode := _episode_from_calendar_record(record)) is not None
    ]


class LegistarProvider:
    name = "legistar"
    capabilities: frozenset[str] = frozenset({"deeplink"})

    def validate(self, source: dict) -> None:
        for key in ("calendar_url", "granicus_base", "backfill_since"):
            if not source.get(key):
                raise ValueError(f"legistar source requires {key!r}")
        _source_url(source, "calendar_url")
        _source_url(source, "granicus_base")
        try:
            datetime.fromisoformat(str(source["backfill_since"]))
        except ValueError as exc:
            raise ValueError("legistar backfill_since must be an ISO date") from exc
        if source.get("view_id") is not None:
            try:
                int(source["view_id"])
            except (TypeError, ValueError) as exc:
                raise ValueError("legistar view_id must be an integer") from exc

    def detect_change(self, source: dict) -> ChangeToken | None:
        return None

    def fetch_calendar_index(self, source: dict) -> CalendarIndex:
        """Fetch the full calendar plus its explicitly linked Granicus recordings.

        Calendar.aspx is a companion, not a replacement primary: callers retain
        no-video rows as metadata and merge its video rows with the native
        Granicus archive using the stable MediaPlayer URL.
        """
        self.validate(source)
        since_year = datetime.fromisoformat(str(source["backfill_since"])).year
        records: list[AgendaRecord] = []
        record_seen: set[tuple] = set()
        episodes: list[Episode] = []
        episode_seen: set[str] = set()
        with make_session() as session:
            for year in range(since_year, datetime.now(UTC).year + 1):
                for html in _iter_year_pages(session, source["calendar_url"], year):
                    for record in _parse_calendar_records_page(html, source):
                        # Legistar's pager should not repeat a row, but use all
                        # source-visible fields as a defensive exact-duplicate
                        # guard without collapsing distinct same-day meetings.
                        marker = (
                            record.body,
                            record.published.isoformat(),
                            record.video_guid,
                            tuple(sorted(record.links.items())),
                        )
                        if marker in record_seen:
                            continue
                        record_seen.add(marker)
                        records.append(record)
                        episode = _episode_from_calendar_record(record)
                        if episode is not None and episode.guid not in episode_seen:
                            episode_seen.add(episode.guid)
                            episodes.append(episode)
        return CalendarIndex(episodes=episodes, records=records)

    def fetch_episodes(self, source: dict) -> list[Episode]:
        """Compatibility entry point for callers that need only recorded rows."""
        return self.fetch_calendar_index(source).episodes

    def fetch_agenda_index(self, source: dict) -> list[AgendaRecord]:
        """Compatibility entry point for agenda-only companion consumers."""
        return self.fetch_calendar_index(source).records

    def resolve_media_url(self, episode: Episode, source: dict) -> str:
        return _resolve_download_url(episode.video_url)

    def video_deeplink(self, ref: str, t_seconds: float) -> str | None:
        return GranicusProvider().video_deeplink(ref, t_seconds)

    def fetch_chapters(self, episode: Episode, source: dict) -> tuple[list[dict], str | None]:
        return GranicusProvider().fetch_chapters(episode, source)

    def fetch_view_counts(self, source: dict) -> list[int]:
        return []
