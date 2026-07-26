"""Granicus provider.

``ViewPublisherRSS.php`` is capped at 100 items per view.  The corresponding
``ViewPublisher.php`` page is the native, uncapped archive and is therefore this
provider's recorded-meeting index.  RSS URLs remain in configuration solely as
stable view identifiers while the catalog moves archive-first without changing a
source key or episode identity.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

import defusedxml.ElementTree as DET
import requests

from citypods.bodies import granicus_body
from citypods.http import DEFAULT_TIMEOUT, make_session
from citypods.models import ChangeToken, Episode
from citypods.provider_request import get as provider_get
from citypods.providers.base import ProviderError
from citypods.security import SecurityError, validate_source_url

# RSS extension namespaces seen in Granicus feeds.
NS = {
    "media": "http://search.yahoo.com/mrss/",
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
}


def _text(item: ET.Element, tag: str) -> str:
    el = item.find(tag)
    return (el.text or "").strip() if el is not None and el.text else ""


def _parse_duration(raw: str) -> int | None:
    """Parse an itunes:duration of ``S``, ``M:S``, or ``H:M:S`` into seconds."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        parts = [int(p) for p in raw.split(":")]
    except ValueError:
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds


def _feed_urls(source: dict) -> list[str]:
    """A Granicus source may set ``feed_url`` (one view) or ``feed_urls`` (several views,
    merged) — useful because the RSS is capped at 100 items and busy cities split meeting
    bodies across views, so low-frequency bodies fall off a single view."""
    if source.get("feed_urls"):
        return list(source["feed_urls"])
    return [source["feed_url"]] if source.get("feed_url") else []


def _archive_url_from_feed_url(feed_url: str) -> str:
    """Turn a configured RSS view URL into its matching native archive URL.

    Only the ``view_id`` survives: ``mode=vpodcast`` is RSS-specific, and retaining
    it on the archive page is unnecessary surface area.  Keeping the RSS URL in
    config makes this migration source-key preserving for every existing feed.
    """
    parts = urlsplit(feed_url)
    if not parts.path.endswith("ViewPublisherRSS.php"):
        raise ValueError(
            "granicus archive-first discovery requires a ViewPublisherRSS.php feed URL "
            f"or an explicit archive_url, got {feed_url!r}"
        )
    view_id = (parse_qs(parts.query).get("view_id") or [None])[0]
    if not view_id:
        raise ValueError(f"granicus RSS URL has no view_id: {feed_url!r}")
    path = f"{parts.path[: -len('ViewPublisherRSS.php')]}ViewPublisher.php"
    return urlunsplit((parts.scheme, parts.netloc, path, urlencode({"view_id": view_id}), ""))


def _archive_urls(source: dict) -> list[str]:
    """Configured archive overrides, or the archive equivalent of every RSS view."""
    if source.get("archive_urls"):
        return list(source["archive_urls"])
    if source.get("archive_url"):
        return [source["archive_url"]]
    return [_archive_url_from_feed_url(url) for url in _feed_urls(source)]


@dataclass
class _ArchiveLink:
    attrs: dict[str, str]
    headers: str
    text: str = ""


@dataclass
class _ArchiveRow:
    cells: list[tuple[str, str]] = field(default_factory=list)
    links: list[_ArchiveLink] = field(default_factory=list)


class _ArchivePageParser(HTMLParser):
    """Capture the few table fields that form Granicus' public archive contract."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[_ArchiveRow] = []
        self._row: _ArchiveRow | None = None
        self._cell_headers = ""
        self._cell_text: list[str] | None = None
        self._link: _ArchiveLink | None = None
        self._link_text: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag == "tr" and self._row is None:
            self._row = _ArchiveRow()
        elif tag == "td" and self._row is not None:
            self._cell_headers = values.get("headers", "")
            self._cell_text = []
        elif tag in {"a", "option"} and self._row is not None and self._cell_text is not None:
            self._link = _ArchiveLink(values, self._cell_headers)
            self._link_text = []

    def handle_data(self, data: str) -> None:
        if self._cell_text is not None:
            self._cell_text.append(data)
        if self._link_text is not None:
            self._link_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"a", "option"} and self._link is not None:
            self._link.text = " ".join("".join(self._link_text or []).split())
            has_url = any(self._link.attrs.get(name) for name in ("href", "onclick", "value"))
            if has_url:
                self._row.links.append(self._link)
            self._link = None
            self._link_text = None
        elif tag == "td" and self._row is not None and self._cell_text is not None:
            self._row.cells.append((self._cell_headers, " ".join("".join(self._cell_text).split())))
            self._cell_headers = ""
            self._cell_text = None
        elif tag == "tr" and self._row is not None:
            if self._row.cells:
                self.rows.append(self._row)
            self._row = None
            self._cell_headers = ""
            self._cell_text = None
            self._link = None
            self._link_text = None


_PLAYER_RE = re.compile(r"MediaPlayer\.php\?view_id=(\d+)&clip_id=(\d+)", re.IGNORECASE)
_ARCHIVE_DATE_RE = re.compile(r"([A-Z][a-z]{2,8}\s+\d{1,2},?\s+\d{4}|\d{1,2}/\d{1,2}/\d{2,4})$")


def _archive_cell(row: _ArchiveRow, column: str) -> str:
    for headers, text in row.cells:
        if headers.lower().split(" ", 1)[0] == column.lower():
            return text
    return ""


def _archive_date(row: _ArchiveRow) -> datetime | None:
    match = _ARCHIVE_DATE_RE.search(_archive_cell(row, "Date"))
    if not match:
        return None
    raw = match.group(1)
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _archive_duration(row: _ArchiveRow) -> int | None:
    raw = _archive_cell(row, "Duration")
    match = re.search(r"(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?", raw, re.IGNORECASE)
    if not match or not any(match.groups()):
        return None
    hours, minutes, seconds = (int(value or 0) for value in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def _archive_link(row: _ArchiveRow, needle: str, archive_url: str) -> str | None:
    needle = needle.lower()
    for link in row.links:
        for value in link.attrs.values():
            if needle in value.lower():
                return urljoin(archive_url, value)
    return None


def parse_archive_page(content: bytes, archive_url: str) -> list[Episode]:
    """Normalize every recorded meeting in one native ``ViewPublisher.php`` page.

    The page's ``Documents...`` selector places Agenda/Minutes URLs in ``option``
    values, while the video player URL is an ``onclick`` attribute.  Parsing the
    rendered archive directly retains those official links and avoids the RSS cap.
    """
    parser = _ArchivePageParser()
    parser.feed(content.decode("utf-8", errors="replace"))
    origin = urlsplit(archive_url)
    base = f"{origin.scheme}://{origin.netloc}"
    episodes: list[Episode] = []
    for row in parser.rows:
        name = _archive_cell(row, "Name")
        published = _archive_date(row)
        player = _archive_link(row, "MediaPlayer.php", archive_url)
        match = _PLAYER_RE.search(player or "")
        if not name or published is None or match is None:
            continue
        view_id, clip_id = match.groups()
        canonical = f"{base}/MediaPlayer.php?view_id={view_id}&clip_id={clip_id}"
        links = {
            "canonical_video": canonical,
            "agenda": _archive_link(row, "AgendaViewer.php", archive_url)
            or f"{base}/AgendaViewer.php?view_id={view_id}&clip_id={clip_id}",
        }
        if minutes := _archive_link(row, "MinutesViewer.php", archive_url):
            links["minutes"] = minutes
        if packet := _archive_link(row, "AgendaPacket", archive_url):
            links["agenda_packet"] = packet
        episodes.append(
            Episode(
                guid=canonical,
                title=name,
                published=published,
                video_url=f"{base}/DownloadFile.php?view_id={view_id}&clip_id={clip_id}",
                duration=_archive_duration(row),
                body=granicus_body(name),
                links=links,
            )
        )
    return episodes


class GranicusProvider:
    name = "granicus"
    # Granicus MediaPlayer.php accepts &starttime=<seconds> for time-anchored deep-links.
    capabilities: frozenset[str] = frozenset({"deeplink"})

    def video_deeplink(self, ref: str, t_seconds: float) -> str | None:
        """Append ``&starttime=<t>`` to a Granicus MediaPlayer URL.

        ``ref`` must be a ``MediaPlayer.php?view_id=X&clip_id=Y`` URL — the
        canonical_video link set by :func:`_episode_links`. Returns ``None`` when
        ``ref`` doesn't look like a Granicus player page (safety guard).

        Verified format: ``https://<tenant>.granicus.com/MediaPlayer.php?view_id=N&clip_id=N&starttime=T``
        """
        if "MediaPlayer.php" not in ref:
            return None
        return f"{ref}&starttime={int(t_seconds)}"

    def validate(self, source: dict) -> None:
        if not _feed_urls(source):
            raise ValueError("granicus source requires 'feed_url' or 'feed_urls'")
        archive_urls = source.get("archive_urls")
        if archive_urls is not None and (
            not isinstance(archive_urls, list)
            or not archive_urls
            or not all(isinstance(url, str) and url for url in archive_urls)
        ):
            raise ValueError("granicus archive_urls must be a non-empty list of URLs")
        if source.get("archive_url") is not None and not isinstance(source["archive_url"], str):
            raise ValueError("granicus archive_url must be a URL string")

    def detect_change(self, source: dict) -> ChangeToken | None:
        # Native archive pages have no reliable validators.  Avoid touching the capped
        # RSS endpoint just to probe it: the archive itself is the source of truth.
        return None

    def fetch_episodes(self, source: dict) -> list[Episode]:
        episodes: list[Episode] = []
        seen: set[str] = set()
        with make_session() as session:
            for url in _archive_urls(source):
                try:
                    resp = provider_get(session, url, timeout=DEFAULT_TIMEOUT)
                except requests.RequestException as exc:
                    raise ProviderError(f"GET {url} failed: {exc}") from exc
                if resp.status_code >= 400:
                    raise ProviderError(f"GET {url} returned {resp.status_code}")
                for ep in parse_archive_page(resp.content, url):
                    if ep.guid not in seen:  # dedup across views
                        seen.add(ep.guid)
                        episodes.append(ep)
        return episodes

    def fetch_chapters(self, episode: Episode, source: dict) -> tuple[list[dict], str | None]:
        """Fetch agenda-level chapter markers from Granicus' ``JSON.php`` player-data endpoint
        (one call, no token). Granicus exposes no transcript, so the second element is None.
        Returns ``([], None)`` when the clip carries no index."""
        ids = _player_ids(episode.links.get("canonical_video", ""), episode.video_url)
        if not ids:
            return [], None
        origin, _view_id, clip_id = ids
        url = f"{origin}/JSON.php?clip_id={clip_id}"
        with make_session() as session:
            try:
                resp = provider_get(session, url, timeout=DEFAULT_TIMEOUT)
            except requests.RequestException as exc:
                raise ProviderError(f"GET {url} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(f"GET {url} returned {resp.status_code}")
        return parse_index_json(resp.content), None

    def resolve_media_url(self, episode: Episode, source: dict) -> str:
        return _resolve_download_url(episode.video_url)

    def fetch_view_counts(self, source: dict) -> list[int]:
        """Archive pages are uncapped, so the RSS view-cap audit no longer applies."""
        return []


def _resolve_download_url(url: str) -> str:
    """Follow a Granicus DownloadFile redirect to its signed media URL.

    Non-RSS Granicus indexes, such as Legistar, reuse this exact redirect,
    rate-limit, and SSRF behavior rather than carrying a second copy.
    """
    # DownloadFile.php 302-redirects to a signed archive-video.granicus.com URL. Pre-follow the
    # redirect here so ffmpeg receives the signed URL directly; the CDN returns 403 for unsigned
    # bare-path requests. The shared session handles 403 rate-limit retry/backoff.
    if "DownloadFile.php" not in url:
        return url
    try:
        with make_session() as session:
            resp = provider_get(session, url, timeout=DEFAULT_TIMEOUT, allow_redirects=False)
    except requests.RequestException:
        return url
    if resp.status_code in (301, 302, 303, 307, 308):
        location = resp.headers.get("Location", "")
        if location:
            location = urljoin(url, location)
            try:
                validate_source_url(location, resolve=True)
            except SecurityError:
                return url
            return location
    return url


def parse_feed(content: bytes) -> list[Episode]:
    """Parse Granicus RSS XML bytes into normalized episodes.

    Pure function (no network) so it is trivially unit-testable from fixtures.
    """
    try:
        root = DET.fromstring(content)
    except ET.ParseError as exc:
        raise ProviderError(f"invalid RSS XML: {exc}") from exc

    channel = root.find("channel")
    if channel is None:
        raise ProviderError("RSS feed has no <channel>")

    episodes: list[Episode] = []
    for item in channel.findall("item"):
        video_url = _enclosure_url(item)
        if not video_url:
            continue  # skip items without a media URL
        title = _text(item, "title") or "Untitled meeting"
        link = _text(item, "link")  # Granicus MediaPlayer watch page for this clip
        guid = _text(item, "guid") or link or video_url
        pub = _text(item, "pubDate")
        try:
            published = parsedate_to_datetime(pub) if pub else None
        except (TypeError, ValueError):
            published = None
        if published is None:
            continue  # an episode with no date can't be ordered reliably
        episodes.append(
            Episode(
                guid=guid,
                title=title,
                published=published,
                video_url=video_url,
                description=_text(item, "description"),
                duration=_parse_duration(_itunes_duration(item)),
                body=granicus_body(title),
                links=_episode_links(link, video_url),
            )
        )
    return episodes


def _player_ids(*urls: str) -> tuple[str, str, str] | None:
    """Pull ``(origin, view_id, clip_id)`` out of the first MediaPlayer/DownloadFile URL that
    carries them. These identifiers key both the agenda doc and the chapter index."""
    for url in urls:
        if not url:
            continue
        parts = urlsplit(url)
        q = parse_qs(parts.query)
        view_id = (q.get("view_id") or [None])[0]
        clip_id = (q.get("clip_id") or [None])[0]
        if parts.netloc and view_id and clip_id:
            return f"{parts.scheme}://{parts.netloc}", view_id, clip_id
    return None


def _episode_links(link: str, video_url: str) -> dict:
    """Resource links for a Granicus item, built (no network) from the (view_id, clip_id)
    pair carried in its MediaPlayer/DownloadFile URLs:

      * ``canonical_video`` — the MediaPlayer watch page (the RSS ``<link>``).
      * ``agenda`` — ``AgendaViewer.php?view_id&clip_id``, which Granicus 302-redirects to the
        archived agenda document for that clip (verified live). The RSS itself only links the
        watch page, so we synthesize this from the same identifiers.
    """
    links: dict[str, str] = {}
    if link:
        links["canonical_video"] = link
    ids = _player_ids(link, video_url)
    if ids:
        origin, view_id, clip_id = ids
        links["agenda"] = f"{origin}/AgendaViewer.php?view_id={view_id}&clip_id={clip_id}"
    return links


def parse_index_json(content: bytes) -> list[dict]:
    """Parse Granicus ``JSON.php`` player data into agenda-level chapter markers.

    The payload is a (sometimes nested) list of index points; each agenda heading has
    ``text`` like ``"Agenda:<id>"`` and a ``time`` in seconds. Roll-call/motion/vote points
    (``Rollcall:``/``Motion``/``Approve`` …) are excluded so chapters track the agenda outline
    rather than every sub-event. Titles may contain HTML (e.g. ``<br />``) — stripped. Pure."""
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    entries = data[0] if data and isinstance(data[0], list) else data
    by_start: dict[int, dict] = {}
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict) or not str(e.get("text", "")).startswith("Agenda:"):
            continue
        try:
            start = int(e.get("time"))
        except (TypeError, ValueError):
            continue
        title = unescape(re.sub(r"<[^>]+>", " ", e.get("title") or "")).strip()
        title = re.sub(r"\s+", " ", title)
        if title:
            by_start.setdefault(start, {"start": start, "title": title})
    return [by_start[s] for s in sorted(by_start)]


def _enclosure_url(item: ET.Element) -> str:
    """Granicus uses <enclosure>; some instances use <media:content>."""
    enc = item.find("enclosure")
    if enc is not None and enc.get("url"):
        return enc.get("url", "")
    media = item.find("media:content", NS)
    if media is not None and media.get("url"):
        return media.get("url", "")
    return ""


def _itunes_duration(item: ET.Element) -> str:
    el = item.find("itunes:duration", NS)
    return (el.text or "").strip() if el is not None and el.text else ""
