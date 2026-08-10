"""Render the directory index (grouped by city), per-feed pages, and meeting/archive pages."""

from __future__ import annotations

import dataclasses
import html
import json

from citypods.chapters import episode_public_chapters
from citypods.durations import episode_served_duration_seconds
from citypods.feeds import enclosure_url, episode_resource_links, meeting_page_url, ordered_links
from citypods.models import AgendaRecord, City, Episode
from citypods.providers import get_provider
from citypods.render import get_env
from citypods.tags import chapter_id
from citypods.timeline import served_to_source


def render_redirect_feed(title: str, new_feed_url: str, new_page_url: str) -> str:
    """A stub RSS that carries ``itunes:new-feed-url`` so clients migrate to the new feed."""
    template = get_env().get_template("redirect_feed.xml.j2")
    return template.render(title=title, new_feed_url=new_feed_url, new_page_url=new_page_url)


def render_redirect_page(new_page_url: str) -> str:
    """A minimal HTML page that redirects a moved feed's human page to its new home."""
    # CR2-CP-11: escape before interpolating into href/meta/anchor text. new_page_url traces to
    # base_url (site config) + city.slug (operator YAML), not adversarial web input, so
    # exploitability requires a malicious/typo'd config -- but the gap is real and cheap to close.
    safe_url = html.escape(new_page_url, quote=True)
    return (
        "<!doctype html><meta charset=utf-8>"
        f'<link rel=canonical href="{safe_url}">'
        f'<meta http-equiv=refresh content="0; url={safe_url}">'
        f'<title>Moved</title><p>This page has moved to <a href="{safe_url}">'
        f"{safe_url}</a>.</p>"
    )


def _feed_label(city: City) -> str:
    body = city.source.get("body")
    if body:
        return body
    title = city.podcast_title
    return title.split("—", 1)[-1].strip() if "—" in title else title


def _duration(seconds: int | float | None) -> str:
    if not seconds:
        return ""
    seconds = round(seconds)
    h, m = divmod(seconds // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def render_index(cities, site_config, base_url, feed_info=None, *, search_enabled: bool = True):
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
    return template.render(
        groups=rows, config=site_config, site=site, search_enabled=search_enabled
    )


def render_search_page(site_config: dict, base_url: str) -> str:
    """Render the global static meeting-search shell.

    The shell is intentionally tiny: shards and the search engine are loaded only after a visitor
    searches, so the normal directory landing page never pays the transcript payload cost.
    """
    template = get_env().get_template("search.html.j2")
    return template.render(
        config=site_config,
        site=base_url.rstrip("/"),
        manifest_url=f"{base_url.rstrip('/')}/data/search/manifest.json",
        minisearch_url=f"{base_url.rstrip('/')}/assets/minisearch-7.1.2.js",
    )


def render_city_request_page(site_config: dict, base_url: str) -> str:
    """Render the public Formspark-backed city request form."""
    request_config = site_config.get("city_request_form") or {}
    template = get_env().get_template("request_city.html.j2")
    return template.render(
        config=site_config,
        site=base_url.rstrip("/"),
        formspark_action=request_config.get("formspark_action", ""),
        turnstile_site_key=request_config.get("turnstile_site_key", ""),
    )


def render_city_page(
    city: City,
    base_url: str,
    episodes: list[Episode],
    *,
    site_config: dict | None = None,
    has_audio: bool = True,
    has_video: bool = True,
    meeting_pages: bool = True,
) -> str:
    site = base_url.rstrip("/")
    city_url = f"{site}/{city.slug}/"
    audio_url = f"{city_url}audio_feed.xml"
    archive_url = f"{city_url}archive/"
    # CR2-CP-12: keep an episode when it has a usable enclosure of *either* kind the city
    # actually offers — filtering on audio only meant a video-only city (has_audio=False,
    # has_video=True) rendered a page with zero episodes despite a populated video feed.
    episode_view = [
        {
            "title": e.title,
            "duration": _duration(e.duration),
            "audio": enclosure_url(e, "audio"),
            "links": episode_resource_links(e),
            "page_url": meeting_page_url(city, e, base_url) if meeting_pages else None,
        }
        for e in episodes
        if (has_audio and enclosure_url(e, "audio")) or (has_video and enclosure_url(e, "video"))
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
        meeting_pages=meeting_pages,
        archive_url=archive_url,
    )


def _availability_view(ep: Episode) -> dict | None:
    """Project durable media availability into the labels exposed to the HTML templates."""
    av = ep.media_availability
    if av is None:
        return None
    state = av.effective_state()
    label = state.replace("_", " ")
    return {
        "state": state,
        "label": label.title(),
        "reason": av.operator_reason or av.reason,
        "last_check": av.last_check,
        "recovered_at": av.recovered_at,
        "withheld": av.is_withheld(),
    }


def _source_deeplink(city: City, ep: Episode, t_seconds: float) -> str | None:
    """Map a served-time offset to a provider-specific source video URL when supported."""
    provider = get_provider(ep.sources[0].provider if ep.sources else city.provider)
    if "deeplink" not in getattr(provider, "capabilities", frozenset()):
        return None
    ref = None
    source_time = t_seconds
    if ep.timeline is not None:
        mapping = served_to_source(ep.timeline, t_seconds)
        if mapping is None:
            return None
        source_id, source_time = mapping
        ref = next((src.ref for src in ep.sources if src.id == source_id), None)
    elif ep.sources:
        ref = ep.sources[0].ref
    else:
        ref = (ep.links or {}).get("canonical_video")
    if not ref:
        return None
    return provider.video_deeplink(ref, source_time)


def _transcript_client_config(city: City, ep: Episode, base_url: str) -> dict | None:
    """Build the JSON configuration used by the browser transcript synchronizer."""
    if not (
        ep.transcript_synced and ep.transcript_hosted_url and ep.transcript_format in {"vtt", "srt"}
    ):
        return None
    sources = [dataclasses.asdict(src) for src in ep.sources]
    if not sources:
        fallback_ref = (ep.links or {}).get("canonical_video")
        if fallback_ref:
            sources = [
                {
                    "id": "s0",
                    "provider": city.provider,
                    "ref": fallback_ref,
                    "media_kind": ep.media_kind,
                    "duration": episode_served_duration_seconds(ep),
                    "watch_url": fallback_ref,
                    "backup_key": None,
                    "duration_basis": None,
                }
            ]
    segments = [dataclasses.asdict(seg) for seg in ep.timeline.segments] if ep.timeline else []
    return {
        "transcriptUrl": ep.transcript_hosted_url,
        "transcriptFormat": ep.transcript_format,
        "pageUrl": meeting_page_url(city, ep, base_url),
        "sourceLinkBase": _source_deeplink(city, ep, 0.0),
        "sources": sources,
        "segments": segments,
        "cityLabel": city.podcast_author,
        "bodyLabel": ep.body or city.podcast_title,
        "dateLabel": ep.published.strftime("%B %-d, %Y"),
    }


def _script_json(value: dict) -> str:
    """Serialize data for an inline JSON script without allowing a closing script tag."""
    return json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def render_meeting_page(
    city: City,
    ep: Episode,
    base_url: str,
    *,
    site_config: dict | None = None,
    include_generated_chapters: bool = False,
) -> str:
    """Render one stable meeting permalink with media, metadata, chapters, and transcript UI."""
    site = base_url.rstrip("/")
    page_url = meeting_page_url(city, ep, base_url)
    transcript_client = _transcript_client_config(city, ep, base_url)
    official_links = episode_resource_links(ep)
    if city.meetings_url:
        official_links.append(("Official meetings page", city.meetings_url))
    elif city.city_website:
        official_links.append(("City website", city.city_website))
    availability = _availability_view(ep)
    player_url = enclosure_url(ep, "audio")
    chapters = [
        {
            "id": chapter_id(ep, ch, index),
            "start": round(float(ch.get("start", 0))),
            "title": str(ch.get("title") or ""),
            "tags": next(
                (
                    [tag.get("id") for tag in annotation.get("tags") or [] if tag.get("id")]
                    for annotation in ep.chapter_tags
                    if annotation.get("chapter_id") == chapter_id(ep, ch, index)
                ),
                [],
            ),
            "source_url": _source_deeplink(city, ep, float(ch.get("start", 0))),
        }
        for index, ch in enumerate(
            episode_public_chapters(
                ep,
                include_generated=include_generated_chapters,
                with_source_index=True,
            )
        )
    ]
    report_url = None
    github_repo = (site_config or {}).get("github_repo")
    if github_repo and ep.uid:
        body = (
            f"slug: {city.slug}\nuid: {ep.uid}\nmeeting page: {page_url}\n"
            f"canonical video: {(ep.links or {}).get('canonical_video', '')}\n"
        )
        from urllib.parse import quote

        report_url = (
            f"https://github.com/{github_repo}/issues/new?template=bug_report.yml"
            f"&title={quote(f'Meeting page issue: {city.slug} {ep.uid}')}"
            f"&body={quote(body)}"
        )
    template = get_env().get_template("meeting.html.j2")
    return template.render(
        city=city,
        ep=ep,
        site=site,
        config=site_config or {},
        page_url=page_url,
        city_url=f"{site}/{city.slug}/",
        player_url=player_url,
        duration=_duration(episode_served_duration_seconds(ep) or ep.duration),
        availability=availability,
        chapters=chapters,
        official_links=official_links,
        transcript_client_json=_script_json(transcript_client) if transcript_client else None,
        source_link_url=_source_deeplink(city, ep, 0.0),
        report_url=report_url,
    )


def render_city_archive_page(
    city: City,
    base_url: str,
    episodes: list[Episode],
    *,
    calendar_records: list[AgendaRecord] | None = None,
    site_config: dict | None = None,
) -> str:
    """Render retained recordings plus durable no-video calendar metadata for one feed."""
    site = base_url.rstrip("/")
    episode_uids = {episode.uid for episode in episodes if episode.uid}
    episode_guids = {episode.guid for episode in episodes if episode.guid}
    rows = [
        {
            "title": ep.title,
            "published": ep.published.strftime("%B %-d, %Y"),
            "page_url": meeting_page_url(city, ep, base_url),
            "availability": _availability_view(ep),
        }
        for ep in sorted(episodes, key=lambda e: e.published, reverse=True)
        if meeting_page_url(city, ep, base_url)
    ]
    calendar_rows = [
        {
            "title": record.title or f"{record.body} Meeting",
            "published": record.published.strftime("%B %-d, %Y"),
            "links": ordered_links(record.links),
        }
        for record in sorted(
            calendar_records or [], key=lambda record: record.published, reverse=True
        )
        # A row represented by an episode is already listed above.  Clip identity
        # handles source-specific body-name differences that prevent a UID match.
        if record.uid not in episode_uids and record.video_guid not in episode_guids
    ]
    template = get_env().get_template("city_archive.html.j2")
    return template.render(
        city=city,
        site=site,
        config=site_config or {},
        episodes=rows,
        calendar_records=calendar_rows,
        city_url=f"{site}/{city.slug}/",
    )
