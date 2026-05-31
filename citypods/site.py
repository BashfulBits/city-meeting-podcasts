"""Render the directory index (grouped by city) and per-feed pages."""

from __future__ import annotations

from citypods.feeds import enclosure_url
from citypods.models import City, Episode
from citypods.render import get_env


def render_redirect_feed(title: str, new_feed_url: str, new_page_url: str) -> str:
    """A stub RSS that carries ``itunes:new-feed-url`` so clients migrate to the new feed."""
    template = get_env().get_template("redirect_feed.xml.j2")
    return template.render(title=title, new_feed_url=new_feed_url, new_page_url=new_page_url)


def render_redirect_page(new_page_url: str) -> str:
    """A minimal HTML page that redirects a moved feed's human page to its new home."""
    return (
        "<!doctype html><meta charset=utf-8>"
        f'<link rel=canonical href="{new_page_url}">'
        f'<meta http-equiv=refresh content="0; url={new_page_url}">'
        f'<title>Moved</title><p>This page has moved to <a href="{new_page_url}">'
        f"{new_page_url}</a>.</p>"
    )


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
        # Finalize the searchable string, then drop the builder-only key so it doesn't bloat
        # the JSON dataset embedded in the page.
        g["search"] = " ".join(g.pop("search_parts")).lower()
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
