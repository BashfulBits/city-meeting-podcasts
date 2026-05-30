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


def enclosure_url(episode: Episode, kind: str) -> str | None:
    """The enclosure URL for this episode in a feed of ``kind``, or None to omit it.

    - audio: a hosted/materialized M4A if present, else the direct source (Granicus).
      HLS episodes with no hosted audio yet are omitted (picked up a later run).
    - video: only direct-MP4 sources; HLS sources are audio-only (not re-hosted as video).
    """
    if kind == "audio":
        if episode.hosted_audio_url:
            return episode.hosted_audio_url
        if episode.media_kind == "direct":
            return episode.resolved_audio_url()
        return None
    # video
    if episode.media_kind == "direct":
        return episode.video_url
    return None


def has_items(episodes: list[Episode], kind: str) -> bool:
    return any(enclosure_url(ep, kind) for ep in episodes)


def build_rss(city: City, episodes: list[Episode], kind: str, base_url: str) -> str:
    """Render an iTunes RSS feed.

    ``kind`` is "audio" or "video". Episodes with no enclosure for that kind (see
    :func:`enclosure_url`) are omitted.
    """
    if kind not in ("audio", "video"):
        raise ValueError(f"kind must be 'audio' or 'video', got {kind!r}")

    mime = "audio/mp4" if kind == "audio" else "video/mp4"
    site = base_url.rstrip("/")
    city_url = f"{site}/{city.slug}/"
    artwork_url = f"{city_url}artwork.jpg"

    items = []
    for ep in _ordered(episodes, city.max_episodes):
        url = enclosure_url(ep, kind)
        if url is None:
            continue
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
