"""Build iTunes-compatible audio and video RSS feeds from normalized episodes."""

from __future__ import annotations

import json
from html import escape

from citypods.durations import episode_served_duration_seconds, episode_source_duration_seconds
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
    "provider_transcript_original": "Original city-provided transcript",
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


def _provider_known_good(ep: Episode) -> dict | None:
    known_good = (ep.provider_transcript or {}).get("known_good")
    return known_good if isinstance(known_good, dict) else None


def _provider_transcript_url(ep: Episode) -> str | None:
    known_good = _provider_known_good(ep)
    if not known_good:
        return None
    return known_good.get("hosted_url") or known_good.get("url")


def episode_resource_links(ep: Episode) -> list[tuple[str, str]]:
    """Episode resource links, including the retained provider-source transcript document."""
    links = dict(ep.links or {})
    if url := _provider_transcript_url(ep):
        links["provider_transcript_original"] = url
    return ordered_links(links)


def podcast_transcript(ep: Episode) -> tuple[str, str] | None:
    """Feed-facing transcript URL + MIME.

    ASR/provider-aligned served-time transcripts own the Podcasting 2.0 tag once present.
    Until then, a synced known-good provider document may fill the slot while still appearing
    as a separate original-document download link.
    """
    if ep.transcript_synced and ep.transcript_hosted_url:
        return (
            ep.transcript_hosted_url,
            TRANSCRIPT_MIME.get(ep.transcript_format or "", "text/plain"),
        )

    known_good = _provider_known_good(ep)
    if not known_good or not known_good.get("synced"):
        return None
    fmt = known_good.get("format") or ""
    mime = TRANSCRIPT_MIME.get(fmt)
    if not mime:
        return None
    if url := _provider_transcript_url(ep):
        return (url, mime)
    return None


def _partial_source_disclaimer_html(ep: Episode) -> str:
    """Listener-facing disclaimer for a confirmed-partial episode (GH#851): the recording is
    real and published, just reproducibly shorter than the source claims. Factual, no
    unverifiable "request it" process claim, linked to the source watch page when known."""
    video_url = (ep.links or {}).get("canonical_video")
    note = "This recording appears incomplete — the audio is shorter than the meeting agenda."
    if video_url:
        note += (
            " A complete recording may be available from the city's "
            f'<a href="{escape(video_url)}">video page</a>.'
        )
    else:
        note += " A complete recording may be available from the city's video page."
    return f"<p><strong>Note:</strong> {note}</p>"


def episode_notes_html(ep: Episode) -> str:
    """Rich show-notes HTML (summary/description + a resource-link list) for
    ``content:encoded``. Returns "" when there's nothing richer than the plain ``<description>``
    (no summary, no links, and not a confirmed-partial disclaimer), so those feeds stay clean.

    Body text: our own ``summary`` is plain text and is HTML-escaped; a provider ``description``
    is often already HTML (e.g. Granicus emits ``<p>...<a>``), so it's emitted raw. Everything
    lives inside a CDATA section in the template, so the only sequence we must neutralize is the
    CDATA terminator ``]]>``."""
    pairs = episode_resource_links(ep)
    is_partial = bool(ep.media_availability and ep.media_availability.is_confirmed_partial())
    if not ep.summary and not pairs and not is_partial:
        return ""
    parts: list[str] = []
    if is_partial:
        parts.append(_partial_source_disclaimer_html(ep))
    if ep.summary:
        parts.append(f"<p>{escape(ep.summary)}</p>")
    elif ep.description:
        parts.append(ep.description)  # provider HTML, rendered as-is inside CDATA
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


def enclosure_duration(ep: Episode, kind: str) -> int | None:
    """Whole-second duration to advertise as ``<itunes:duration>`` for *kind*'s enclosure.

    For the **audio** feed this is the duration of the actual hosted object —
    ``served_duration_seconds`` (the canonical served/hosted clock, falling back to
    ``audio_duration_served``) — so a trimmed episode advertises its real played length, not the
    longer source duration. Falls back to ``source_duration_seconds`` (then ``ep.duration``) when
    no served duration is recorded (e.g. identity/direct audio that was never re-hosted). The
    **video** feed always uses the source duration. ``None`` when unknown."""
    if kind == "audio":
        served = episode_served_duration_seconds(ep)
        if served is not None:
            return int(round(served))
    source = episode_source_duration_seconds(ep)
    return int(round(source)) if source is not None else None


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
        transcript = podcast_transcript(ep)
        items.append(
            {
                "ep": ep,
                "enclosure_url": url,
                "duration": enclosure_duration(ep, kind),
                "notes_html": episode_notes_html(ep),
                "chapters_url": chapters_url(city, ep, base_url),
                "transcript_url": transcript[0] if transcript else None,
                "transcript_mime": transcript[1] if transcript else None,
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
