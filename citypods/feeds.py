"""Build iTunes-compatible audio and video RSS feeds from normalized episodes."""

from __future__ import annotations

from citypods.models import City, Episode
from citypods.render import get_env

# enclosure length is intentionally 0: players re-check size at download time, and
# a HEAD per episode would be prohibitively expensive at scale. See PLAN.md.
ENCLOSURE_LENGTH = "0"


def _ordered(episodes: list[Episode], max_episodes: int) -> list[Episode]:
    ordered = sorted(episodes, key=lambda e: e.published, reverse=True)
    return ordered[:max_episodes]


def build_rss(city: City, episodes: list[Episode], kind: str, base_url: str) -> str:
    """Render an iTunes RSS feed.

    ``kind`` is "audio" or "video". The audio feed points at the same MP4 with an
    ``audio/mp4`` MIME type (players read the audio track); the video feed uses
    ``video/mp4``. Lossless audio extraction is a Phase 4 enhancement.
    """
    if kind not in ("audio", "video"):
        raise ValueError(f"kind must be 'audio' or 'video', got {kind!r}")

    mime = "audio/mp4" if kind == "audio" else "video/mp4"
    site = base_url.rstrip("/")
    city_url = f"{site}/{city.slug}/"
    artwork_url = f"{city_url}artwork.jpg"

    items = []
    for ep in _ordered(episodes, city.max_episodes):
        url = ep.resolved_audio_url() if kind == "audio" else ep.video_url
        items.append({"ep": ep, "enclosure_url": url})

    template = get_env().get_template("feed.xml.j2")
    return template.render(
        city=city,
        kind=kind,
        mime=mime,
        items=items,
        feed_url=f"{city_url}{kind}_feed.xml",
        city_url=city_url,
        artwork_url=artwork_url,
        enclosure_length=ENCLOSURE_LENGTH,
    )
