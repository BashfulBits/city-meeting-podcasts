"""Render the directory index and per-city landing pages (static HTML).

Phase 0 keeps these intentionally minimal; richer search/filter UI and the
add-city form land in Phase 3.
"""

from __future__ import annotations

from citypods.models import City
from citypods.render import get_env


def render_city_page(city: City, base_url: str, episode_count: int) -> str:
    site = base_url.rstrip("/")
    template = get_env().get_template("city.html.j2")
    return template.render(
        city=city,
        site=site,
        city_url=f"{site}/{city.slug}/",
        episode_count=episode_count,
    )


def render_index(cities: list[City], site_config: dict, base_url: str) -> str:
    site = base_url.rstrip("/")
    states = sorted({c.state for c in cities if c.state})
    rows = sorted(cities, key=lambda c: c.podcast_title.lower())
    template = get_env().get_template("index.html.j2")
    return template.render(
        cities=rows,
        states=states,
        site=site,
        config=site_config,
    )
