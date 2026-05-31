"""Granicus provider.

Granicus exposes meeting archives as an RSS 2.0 feed via ``ViewPublisherRSS.php``.
A single ``view_id`` selects one meeting type; one city YAML maps to one feed.

Change detection uses a HEAD request and compares ETag / Last-Modified.
"""

from __future__ import annotations

from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

import requests

from citypods.bodies import granicus_body
from citypods.http import DEFAULT_TIMEOUT, make_session
from citypods.models import ChangeToken, Episode
from citypods.providers.base import ProviderError

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


class GranicusProvider:
    name = "granicus"

    def validate(self, source: dict) -> None:
        if not _feed_urls(source):
            raise ValueError("granicus source requires 'feed_url' or 'feed_urls'")

    def detect_change(self, source: dict) -> ChangeToken | None:
        url = _feed_urls(source)[0]  # probe the first view; Granicus HEAD lacks validators
        with make_session() as session:
            try:
                resp = session.head(url, timeout=DEFAULT_TIMEOUT, allow_redirects=True)
            except requests.RequestException as exc:
                raise ProviderError(f"HEAD failed for {url}: {exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(f"HEAD {url} returned {resp.status_code}")
        return ChangeToken(
            etag=resp.headers.get("ETag"),
            last_modified=resp.headers.get("Last-Modified"),
        )

    def fetch_episodes(self, source: dict) -> list[Episode]:
        episodes: list[Episode] = []
        seen: set[str] = set()
        with make_session() as session:
            for url in _feed_urls(source):
                resp = session.get(url, timeout=DEFAULT_TIMEOUT)
                if resp.status_code >= 400:
                    raise ProviderError(f"GET {url} returned {resp.status_code}")
                for ep in parse_feed(resp.content):
                    if ep.guid not in seen:  # dedup across views
                        seen.add(ep.guid)
                        episodes.append(ep)
        return episodes

    def resolve_media_url(self, episode: Episode, source: dict) -> str:
        # Granicus enclosures are already direct progressive MP4s.
        return episode.video_url

    def fetch_view_counts(self, source: dict) -> list[int]:
        """Per-view item counts, for the feed-health view-cap check: Granicus RSS is
        hard-capped at 100 items/view, so a view at the cap is probably truncated."""
        counts: list[int] = []
        with make_session() as session:
            for url in _feed_urls(source):
                resp = session.get(url, timeout=DEFAULT_TIMEOUT)
                if resp.status_code >= 400:
                    raise ProviderError(f"GET {url} returned {resp.status_code}")
                counts.append(len(parse_feed(resp.content)))
        return counts


def parse_feed(content: bytes) -> list[Episode]:
    """Parse Granicus RSS XML bytes into normalized episodes.

    Pure function (no network) so it is trivially unit-testable from fixtures.
    """
    try:
        root = ET.fromstring(content)
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
                links={"canonical_video": link} if link else {},
            )
        )
    return episodes


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
