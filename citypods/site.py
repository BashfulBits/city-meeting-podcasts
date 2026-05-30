"""Render the directory index (grouped by city) and per-feed pages."""

from __future__ import annotations

from citypods.feeds import enclosure_url
from citypods.models import City, Episode
from citypods.render import get_env


def _feed_label(city: City) -> str:
    body = city.source.get("body")
    if body:
        return body
    title = city.podcast_title
    return title.split("—", 1)[-1].strip() if "—" in title else title


def _duration(seconds: int | None) -> str:
    if not seconds:
        return ""
    h, m = divmod(seconds // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def render_index(cities, site_config, base_url, feed_info=None):
    """Group feeds by city (podcast_author) into a searchable accordion."""
    site = base_url.rstrip("/")
    feed_info = feed_info or {}
    groups: dict[str, dict] = {}
    for c in sorted(cities, key=lambda c: (c.podcast_author or "", _feed_label(c).lower())):
        key = c.podcast_author or c.state or "Other"
        info = feed_info.get(c.slug, {})
        label = _feed_label(c)
        feed = {
            "url": f"{site}/{c.slug}/",
            "label": label,
            "has_audio": info.get("has_audio", True),
            "has_video": info.get("has_video", False),
            "search": f"{key} {label} {c.slug} {c.state or ''}".lower(),
        }
        g = groups.setdefault(key, {"label": key, "feeds": [], "search_parts": [key]})
        g["feeds"].append(feed)
        g["search_parts"].append(label)
    rows = []
    for key in sorted(groups):
        g = groups[key]
        g["search"] = " ".join(g["search_parts"]).lower()
        rows.append(g)

    template = get_env().get_template("index.html.j2")
    return template.render(groups=rows, config=site_config, site=site)


def render_city_page(
    city: City,
    base_url: str,
    episodes: list[Episode],
    *,
    site_config: dict | None = None,
    has_audio: bool = True,
    has_video: bool = True,
) -> str:
    site = base_url.rstrip("/")
    city_url = f"{site}/{city.slug}/"
    audio_url = f"{city_url}audio_feed.xml"
    episode_view = [
        {
            "title": e.title,
            "duration": _duration(e.duration),
            "audio": enclosure_url(e, "audio"),
        }
        for e in episodes
        if enclosure_url(e, "audio")
    ]
    template = get_env().get_template("city.html.j2")
    return template.render(
        city=city,
        site=site,
        config=site_config or {},
        audio_url=audio_url,
        audio_noscheme=audio_url.split("://", 1)[-1],
        video_url=f"{city_url}video_feed.xml",
        episodes=episode_view,
        has_audio=has_audio,
        has_video=has_video,
    )
