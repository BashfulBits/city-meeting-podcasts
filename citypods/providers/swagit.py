"""Swagit provider (Granicus-owned).

Swagit has no public API/RSS, but its archive "view" page is server-rendered HTML: a
table of rows, each ``<a href="/videos/{id}">{body name}</a>`` + a date. One view lists
every body's meetings, so a city YAML selects one body via ``body:`` (substring match) —
e.g. "City Council Agenda Meetings". Use ``scripts/discover_swagit.py`` to list the bodies.

Media is a progressive MP4 behind an expiring (~1h) presigned S3 URL via
``/videos/{id}/download``, so episodes are ``media_kind="hls"`` (resolved lazily and
re-hosted as audio by the materialization pipeline, like CivicPlus).
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

import requests

from citypods.http import DEFAULT_TIMEOUT, make_session
from citypods.models import ChangeToken, Episode
from citypods.providers.base import ProviderError

# <a ... href="/videos/123">Body Name</a> </td> <td nowrap> May 26, 2026 </td>
ROW_RE = re.compile(
    r'<a[^>]*href="/videos/(\d+)"[^>]*>([^<]+)</a>\s*</td>\s*<td[^>]*nowrap[^>]*>\s*([^<]+?)\s*</td>',
    re.IGNORECASE,
)


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def parse_list(content: bytes, origin: str) -> list[Episode]:
    """Parse a Swagit view page into episodes (all bodies; ``Episode.body`` set).

    Body filtering is applied generically downstream via ``source.body``. Pure (no
    network); media URLs are resolved later by ``resolve_media_url``.
    """
    text = content.decode("utf-8", errors="replace")
    episodes: list[Episode] = []
    for vid, raw_body, raw_date in ROW_RE.findall(text):
        body_name = html.unescape(raw_body).strip()
        published = _parse_date(raw_date)
        if published is None:
            continue
        episodes.append(
            Episode(
                guid=vid,
                title=f"{body_name} – {raw_date.strip()}",
                published=published,
                video_url=f"{origin}/videos/{vid}/download",
                media_kind="hls",
                body=body_name,
            )
        )
    return episodes


def _parse_date(raw: str) -> datetime | None:
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(raw.strip(), fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


class SwagitProvider:
    name = "swagit"

    def validate(self, source: dict) -> None:
        if not source.get("list_url"):
            raise ValueError("swagit source requires 'list_url'")
        if not source.get("body"):
            raise ValueError("swagit source requires 'body' (meeting-body filter)")

    def detect_change(self, source: dict) -> ChangeToken | None:
        return None  # list page is one fetch; no cheap change probe

    def fetch_episodes(self, source: dict) -> list[Episode]:
        url = source["list_url"]
        with make_session() as session:
            try:
                resp = session.get(url, timeout=DEFAULT_TIMEOUT)
            except requests.RequestException as exc:
                raise ProviderError(f"GET {url} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(f"GET {url} returned {resp.status_code}")
        return parse_list(resp.content, _origin(url))

    def resolve_media_url(self, episode: Episode, source: dict) -> str:
        """Follow /videos/{id}/download to its presigned MP4 URL."""
        with make_session() as session:
            resp = session.get(episode.video_url, timeout=DEFAULT_TIMEOUT, allow_redirects=False)
        loc = resp.headers.get("Location")
        if resp.status_code in (301, 302, 303, 307, 308) and loc:
            return loc
        if resp.status_code < 400:
            return episode.video_url  # already the file
        raise ProviderError(f"download resolve for {episode.guid} returned {resp.status_code}")
