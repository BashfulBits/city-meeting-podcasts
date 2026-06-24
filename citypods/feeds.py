"""Build iTunes-compatible audio and video RSS feeds from normalized episodes."""

from __future__ import annotations

import json
from html import escape

from citypods.models import City, Episode
from citypods.render import get_env
from citypods.stages import TRANSCRIPT_MIME  # single source for transcript MIME types

# Podcasting 2.0 chapters media type (a JSON sidecar referenced per item). This surfaces
# chapters to apps even for direct enclosures we don't re-host, where embedding into the
# audio bytes isn't possible.
CHAPTERS_TYPE = "application/json+chapters"

# enclosure length is intentionally 0: players re-check size at download time, and
# a HEAD per episode would be prohibitively expensive at scale. See PLAN.md.
ENCLOSURE_LENGTH = "0"

# Human labels + display order for resource links (see LinksStage). Keys not listed here
# still render, after these, with a title-cased fallback label.
LINK_LABELS: dict[str, str] = {
    "agenda": "Agenda",
    "agenda_packet": "Agenda packet",
    "minutes": "Minutes",
    "transcript": "Transcript",
    "documents": "Meeting documents",
    "canonical_video": "Watch the video",
    "meetings": "Official meetings page",
}


def link_label(key: str) -> str:
    return LINK_LABELS.get(key, key.replace("_", " ").title())


def ordered_links(links: dict | None) -> list[tuple[str, str]]:
    """``(label, url)`` pairs for a links dict, in display order (known kinds first, then
    alphabetical), with empties dropped. Shared by the feed notes and the city HTML page."""
    links = {k: v for k, v in (links or {}).items() if v}
    order = list(LINK_LABELS)
    keys = sorted(links, key=lambda k: (order.index(k) if k in order else len(order), k))
    return [(link_label(k), links[k]) for k in keys]


def episode_notes_html(ep: Episode) -> str:
    """Rich show-notes HTML (summary/description + a resource-link list) for
    ``content:encoded``. Returns "" when there's nothing richer than the plain ``<description>``
    (no summary and no links), so those feeds stay clean.

    Body text: our own ``summary`` is plain text and is HTML-escaped; a provider ``description``
    is often already HTML (e.g. Granicus emits ``<p>...<a>``), so it's emitted raw. Everything
    lives inside a CDATA section in the template, so the only sequence we must neutralize is the
    CDATA terminator ``]]>``."""
    if not ep.summary and not (ep.links or {}):
        return ""
    parts: list[str] = []
    if ep.summary:
        parts.append(f"<p>{escape(ep.summary)}</p>")
    elif ep.description:
        parts.append(ep.description)  # provider HTML, rendered as-is inside CDATA
    pairs = ordered_links(ep.links)
    if pairs:
        lis = "".join(
            f'<li><a href="{escape(url)}">{escape(label)}</a></li>' for label, url in pairs
        )
        parts.append(f"<p>Resources:</p><ul>{lis}</ul>")
    return "".join(parts).replace("]]>", "]]&gt;")


def chapters_json(ep: Episode) -> str:
    """Render an episode's chapters as a Podcasting 2.0 chapters document (the JSON a
    ``<podcast:chapters>`` URL points to)."""
    chapters = [
        {"startTime": int(c["start"]), "title": c.get("title", "")}
        for c in sorted(ep.chapters or [], key=lambda c: c["start"])
    ]
    return json.dumps({"version": "1.2.0", "chapters": chapters}, indent=2) + "\n"


def chapters_url(city: City, ep: Episode, base_url: str) -> str | None:
    """Public URL of an episode's chapters JSON sidecar, or None when it has no chapters.
    Hosted alongside the feed under ``docs/<slug>/chapters/<uid>.json`` (see run.py)."""
    if not ep.chapters or not ep.uid:
        return None
    return f"{base_url.rstrip('/')}/{city.slug}/chapters/{ep.uid}.json"


def _ordered(episodes: list[Episode], max_episodes: int) -> list[Episode]:
    ordered = sorted(episodes, key=lambda e: e.published, reverse=True)
    return ordered[:max_episodes]


def enclosure_url(episode: Episode, kind: str) -> str | None:
    """The enclosure URL for this episode in a feed of ``kind``, or None to omit it.

    - audio: a hosted/materialized M4A if present, else the direct source (Granicus).
      HLS episodes with no hosted audio yet are omitted (picked up a later run).
    - video: only direct-MP4 sources; HLS sources are audio-only (not re-hosted as video).

    A withheld media-availability verdict (suspected/confirmed empty, missing, invalid — H16 PR3)
    omits the episode from *both* feed kinds so a bad/empty enclosure is never published; its
    metadata (canonical watch page, agenda/minutes) still renders on the meeting page via ``links``.
    """
    av = episode.media_availability
    if av is not None and av.is_withheld():
        return None
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
        items.append(
            {
                "ep": ep,
                "enclosure_url": url,
                "notes_html": episode_notes_html(ep),
                "chapters_url": chapters_url(city, ep, base_url),
                # <podcast:transcript> emitted only when transcript is hosted and synced
                # (timed VTT/SRT); untimed transcripts render as notes-only.
                "transcript_url": ep.transcript_hosted_url if ep.transcript_synced else None,
                "transcript_mime": (
                    TRANSCRIPT_MIME.get(ep.transcript_format or "", "text/plain")
                    if ep.transcript_synced
                    else None
                ),
            }
        )

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
