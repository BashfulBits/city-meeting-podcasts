"""Manual episode seeds for provider windows that are intentionally shallow.

Some providers expose a complete-ish archive page but only publish a tiny RSS/API window.
``seed_episodes`` lets a feed config name a few historical recordings once; after the first
enrich pass persists them, the normal append-only record store keeps them even if the live
provider continues to return only the newest item.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, date, datetime
from urllib.parse import urldefrag

from citypods.bodies import body_key, canonical_body
from citypods.models import City, Episode


def _parse_published(raw, *, label: str) -> datetime:
    if isinstance(raw, datetime):
        parsed = raw
    elif isinstance(raw, date):
        parsed = datetime(raw.year, raw.month, raw.day, tzinfo=UTC)
    elif isinstance(raw, str):
        value = raw.strip()
        if value.endswith("Z"):
            value = f"{value[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{label}: invalid published datetime {raw!r}") from exc
    else:
        raise ValueError(f"{label}: published must be an ISO datetime string")

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _url_key(url: str) -> str:
    return urldefrag(url.strip())[0].rstrip("/")


def _title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def _episode_key(ep: Episode) -> tuple[str, str, str]:
    return (
        body_key(canonical_body(ep.body or "")),
        ep.published.date().isoformat(),
        _title_key(ep.title),
    )


def seed_episodes(city: City) -> list[Episode]:
    """Build configured manual seed episodes for *city*.

    ``seed_episodes`` is intentionally a top-level feed key (available via ``city.extra``), not
    part of ``source``. That keeps ``source_key`` stable when operators add/remove one-time seeds.
    """
    raw_items = city.extra.get("seed_episodes") or []
    if not raw_items:
        return []
    if not isinstance(raw_items, list):
        raise ValueError(f"{city.slug}: seed_episodes must be a list")

    episodes: list[Episode] = []
    for index, raw in enumerate(raw_items, start=1):
        label = f"{city.slug}: seed_episodes[{index}]"
        if not isinstance(raw, dict):
            raise ValueError(f"{label}: entry must be a mapping")
        title = str(raw.get("title") or "").strip()
        video_url = str(raw.get("video_url") or "").strip()
        missing = [
            key
            for key, value in (
                ("title", title),
                ("published", raw.get("published")),
                ("video_url", video_url),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"{label}: missing required keys: {', '.join(missing)}")

        guid = str(raw.get("guid") or "").strip() or video_url
        episodes.append(
            Episode(
                guid=guid,
                title=title,
                published=_parse_published(raw["published"], label=label),
                video_url=video_url,
                description=str(raw.get("description") or ""),
                audio_url=raw.get("audio_url"),
                duration=int(raw["duration"]) if raw.get("duration") is not None else None,
                media_kind=str(raw.get("media_kind") or "direct"),
                body=str(raw.get("body") or city.source.get("body") or "").strip() or None,
                links=dict(raw.get("links") or {}),
            )
        )
    return episodes


def merge_seed_episodes(city: City, fetched: Iterable[Episode]) -> list[Episode]:
    """Append configured seed episodes, skipping obvious duplicates from the live provider.

    Prefer fetched provider rows when the same guid, URL, or body/date/title appears in both
    places. That keeps a one-time seed list harmless if the provider later expands its window.
    """
    episodes = list(fetched)
    seeds = seed_episodes(city)
    if not seeds:
        return episodes

    seen_guids = {ep.guid for ep in episodes if ep.guid}
    seen_urls = {_url_key(ep.video_url) for ep in episodes if ep.video_url}
    seen_episode_keys = {_episode_key(ep) for ep in episodes}
    for seed in seeds:
        url_key = _url_key(seed.video_url)
        episode_key = _episode_key(seed)
        if seed.guid in seen_guids or url_key in seen_urls or episode_key in seen_episode_keys:
            continue
        episodes.append(seed)
        seen_guids.add(seed.guid)
        seen_urls.add(url_key)
        seen_episode_keys.add(episode_key)
    return episodes
