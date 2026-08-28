"""CivicPlus / CivicMedia provider.

CivicMedia exposes a per-channel RSS feed (``/RSSFeed.aspx?ModID=92&CID=<channel>``)
listing meetings with a ``?VID=<n>`` watch-page link — but no media enclosure. The
playable media is tokenized, expiring TikiLive HLS, so episodes carry ``media_kind="hls"``
and the watch-page URL as a stable reference; the real ``.m3u8`` is resolved lazily by
:meth:`resolve_media_url` and handed to the audio materialization pipeline.

Resolution chain: watch page → ``tikiliveapi.com/embed?...videoId=<id>`` → ``.m3u8``.
"""

from __future__ import annotations

import re
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import defusedxml.ElementTree as DET
import requests

from citypods.http import DEFAULT_TIMEOUT, make_session
from citypods.models import ChangeToken, Episode
from citypods.providers.base import ProviderError
from citypods.security import validate_source_url

EMBED_RE = re.compile(r"https?://[\w.-]*tikiliveapi\.com/embed\?[^\"'\s]*videoId=\d+[^\"'\s]*")
M3U8_RE = re.compile(r"https?://[^\"'\s]+\.m3u8[^\"'\s]*")


def _text(item: ET.Element, tag: str) -> str:
    el = item.find(tag)
    return (el.text or "").strip() if el is not None and el.text else ""


def parse_civicmedia_feed(content: bytes) -> list[Episode]:
    """Parse a CivicMedia RSS channel into episodes (no media URLs resolved yet)."""
    try:
        root = DET.fromstring(content)
    except ET.ParseError as exc:
        raise ProviderError(f"invalid CivicMedia RSS: {exc}") from exc
    channel = root.find("channel")
    if channel is None:
        raise ProviderError("CivicMedia RSS has no <channel>")

    # The CivicMedia channel is itself the body; its title is like
    # "Gainesville, TX - Media Center - City Council" -> body "City Council".
    channel_title = _text(channel, "title")
    body = channel_title.split(" - ")[-1].strip() if channel_title else None

    episodes: list[Episode] = []
    for item in channel.findall("item"):
        link = _text(item, "link")
        if not link:
            continue  # without a watch page we can't resolve media
        title = _text(item, "title") or "Untitled meeting"
        pub = _text(item, "pubDate")
        try:
            source_published = parsedate_to_datetime(pub) if pub else None
        except (TypeError, ValueError):
            source_published = None
        if source_published is None:
            continue
        episodes.append(
            Episode(
                guid=_text(item, "guid") or link,
                title=title,
                # CivicMedia publishes upload time here.  A configured companion calendar may
                # later replace a conflicting date before UID assignment, but its own matching
                # date takes precedence when the two official sources already agree.
                published=source_published,
                video_url=link,  # stable watch-page reference; real HLS resolved lazily
                description=_text(item, "description"),
                media_kind="hls",
                body=body,
            )
        )
    return episodes


class CivicPlusProvider:
    name = "civicplus"
    # CivicPlus/CivicMedia serves tokenized HLS; no public player deep-link.
    capabilities: frozenset[str] = frozenset()

    def video_deeplink(self, ref: str, t_seconds: float) -> str | None:
        return None

    def validate(self, source: dict) -> None:
        if not source.get("feed_url"):
            raise ValueError("civicplus source requires 'feed_url' (a CivicMedia RSSFeed.aspx URL)")

    def detect_change(self, source: dict) -> ChangeToken | None:
        url = source["feed_url"]
        validate_source_url(url)
        with make_session() as session:
            try:
                resp = session.head(url, timeout=DEFAULT_TIMEOUT, allow_redirects=True)
            except requests.RequestException as exc:
                raise ProviderError(f"HEAD failed for {url}: {exc}") from exc
            if resp.status_code == 405:
                try:
                    resp = session.get(
                        url,
                        timeout=DEFAULT_TIMEOUT,
                        allow_redirects=True,
                        stream=True,
                    )
                except requests.RequestException as exc:
                    raise ProviderError(f"GET fallback failed for {url}: {exc}") from exc
                try:
                    if resp.status_code >= 400:
                        raise ProviderError(
                            f"GET fallback {url} returned {resp.status_code}",
                            status_code=resp.status_code,
                        )
                    return ChangeToken(
                        etag=resp.headers.get("ETag"),
                        last_modified=resp.headers.get("Last-Modified"),
                    )
                finally:
                    resp.close()
        if resp.status_code >= 400:
            raise ProviderError(
                f"HEAD {url} returned {resp.status_code}", status_code=resp.status_code
            )
        return ChangeToken(
            etag=resp.headers.get("ETag"),
            last_modified=resp.headers.get("Last-Modified"),
        )

    def fetch_episodes(self, source: dict) -> list[Episode]:
        url = source["feed_url"]
        with make_session() as session:
            try:
                resp = session.get(url, timeout=DEFAULT_TIMEOUT)
            except requests.RequestException as exc:
                raise ProviderError(f"GET {url} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(
                f"GET {url} returned {resp.status_code}", status_code=resp.status_code
            )
        return parse_civicmedia_feed(resp.content)

    def resolve_media_url(self, episode: Episode, source: dict) -> str:
        with make_session() as session:
            embed_url = self._find_embed_url(session, episode.video_url)
            return self._find_hls_url(session, embed_url)

    def _find_embed_url(self, session: requests.Session, page_url: str) -> str:
        try:
            resp = session.get(page_url, timeout=DEFAULT_TIMEOUT)
        except requests.RequestException as exc:
            raise ProviderError(f"GET watch page {page_url} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(
                f"GET watch page {page_url} returned {resp.status_code}",
                status_code=resp.status_code,
            )
        m = EMBED_RE.search(resp.text)
        if not m:
            raise ProviderError(f"no TikiLive embed URL found on {page_url}")
        embed_url = m.group(0).replace("&amp;", "&")
        host = (urlparse(embed_url).hostname or "").lower()
        if host != "tikiliveapi.com" and not host.endswith(".tikiliveapi.com"):
            raise ProviderError(f"unexpected embed host in URL: {embed_url}")
        return embed_url

    def _find_hls_url(self, session: requests.Session, embed_url: str) -> str:
        try:
            resp = session.get(embed_url, timeout=DEFAULT_TIMEOUT)
        except requests.RequestException as exc:
            raise ProviderError(f"GET embed {embed_url} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(
                f"GET embed {embed_url} returned {resp.status_code}",
                status_code=resp.status_code,
            )
        m = M3U8_RE.search(resp.text)
        if not m:
            raise ProviderError(f"no HLS (.m3u8) URL found in embed {embed_url}")
        hls_url = m.group(0).replace("&amp;", "&")
        # M3U8_RE (unlike the sibling EMBED_RE) has no host constraint; validate explicitly
        # before handing it to ffmpeg rather than relying on preflight_media_size's size-cap
        # side effect (CR2-CP-17/MR-CP-02).
        validate_source_url(hls_url, resolve=True)
        return hls_url
