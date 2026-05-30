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


def parse_events(content: bytes, category_id: int | None = None) -> list[Episode]:
    """Parse a CivicClerk OData Events payload into direct-MP4 episodes.

    Includes only published events whose ``mediaSourcePathMp4`` is an absolute URL
    (recorded meetings); relative/streaming-only items are skipped. Pure (no network).
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
        if not mp4.startswith("http"):
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
                body=(e.get("categoryName") or e.get("meetingTypeName") or "").strip() or None,
            )
        )
    return episodes


class CivicClerkProvider:
    name = "civicclerk"

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
        return parse_events(resp.content, category_id=source.get("category_id"))

    def resolve_media_url(self, episode: Episode, source: dict) -> str:
        return episode.video_url  # direct MP4
