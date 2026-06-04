"""Swagit provider (Granicus-owned).

Swagit has no public API/RSS, but its archive "view" page is server-rendered HTML: a
table of rows, each ``<a href="/videos/{id}">{body name}</a>`` + a date. One view lists
every body's meetings, so a city YAML selects one body via ``body:`` (substring match) —
e.g. "City Council Agenda Meetings". Use ``scripts/discover_swagit.py`` to list the bodies.

Media is a progressive MP4 behind an expiring (~1h) presigned S3 URL via
``/videos/{id}/download``, so episodes are ``media_kind="hls"`` (resolved lazily and
re-hosted as audio by the materialization pipeline, like CivicPlus).

Older meetings 302 to a *keyless* presigned URL (signs only the bucket prefix, no object), so
``/download`` is unusable; for those we fall back to the direct ``dfile`` MP4(s) embedded in the
``/videos/{id}`` page (issue #120). Single-segment meetings resolve directly; multi-segment ones
are deferred (audio concat is a roadmap item) and the pipeline backs off re-trying them.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

import requests

from citypods.http import DEFAULT_TIMEOUT, make_session
from citypods.models import ChangeToken, Episode
from citypods.providers.base import MEDIA_DEAD, MEDIA_DEFERRED, MediaUnavailable, ProviderError

# <a ... href="/videos/123">Body Name</a> </td> <td nowrap> May 26, 2026 </td>
ROW_RE = re.compile(
    r'<a[^>]*href="/videos/(\d+)"[^>]*>([^<]+)</a>\s*</td>\s*<td[^>]*nowrap[^>]*>\s*([^<]+?)\s*</td>',
    re.IGNORECASE,
)

# Agenda-item links on a video page double as chapter markers:
#   <a class="playerControl" data-ts="51" data-end-ts="491" data-title="..." href="/play/ID/51">
CHAPTER_ANCHOR_RE = re.compile(r'<a\b[^>]*class="[^"]*playerControl[^"]*"[^>]*>', re.IGNORECASE)

# Legacy (older-meeting) video pages embed each agenda segment's media as inline JSON, e.g.
#   {"id":...,"seq":2,"title":"Call to Order","dfile":"https://.../01212014-616.h264.mp4",...}
# ``dfile`` is a direct, reachable MP4 — the fallback when ``/download`` is keyless (issue #120).
_SEGMENT_OBJ_RE = re.compile(r'\{[^{}]*"dfile"[^{}]*\}')
_DFILE_RE = re.compile(r'"dfile":"([^"]+)"')
_SEQ_RE = re.compile(r'"seq":(\d+)')
_TITLE_RE = re.compile(r'"title":"([^"]+)"')


def _attr(tag: str, name: str) -> str | None:
    m = re.search(rf'\b{name}="([^"]*)"', tag)
    return m.group(1) if m else None


def parse_chapters(content: bytes) -> list[dict]:
    """Parse a Swagit video page's timestamped agenda items into chapter markers.

    Each ``playerControl`` anchor carries ``data-ts`` (start seconds), ``data-end-ts`` (end),
    and ``data-title``. Returns ``[{"start": int, "end": int|None, "title": str}, ...]`` sorted
    by start with duplicate starts collapsed. Pure (no network)."""
    text = content.decode("utf-8", errors="replace")
    by_start: dict[int, dict] = {}
    for tag in CHAPTER_ANCHOR_RE.findall(text):
        ts = _attr(tag, "data-ts")
        if ts is None or not ts.isdigit():
            continue
        start = int(ts)
        title = html.unescape(_attr(tag, "data-title") or "").strip()
        title = re.sub(r"\s+", " ", title)
        if not title:
            continue
        end_raw = _attr(tag, "data-end-ts")
        end = int(end_raw) if end_raw and end_raw.isdigit() else None
        by_start.setdefault(start, {"start": start, "end": end, "title": title})
    return [by_start[s] for s in sorted(by_start)]


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _s3_object_key(url: str) -> str:
    """The object key of a presigned S3 URL — the last path segment, ignoring the query. Empty
    string when the URL signs only the bucket *prefix* (``.../dallastx/?X-Amz-...``), which is the
    keyless-redirect signature for older Swagit meetings whose ``/download`` is broken (#120)."""
    return urlsplit(url).path.rsplit("/", 1)[-1]


def parse_segment_objects(content: bytes) -> list[tuple[str, str]]:
    """Parse a Swagit video page's inline ``dfile`` segments, returning ``(url, title)`` ordered by ``seq``.

    Each JSON object on older meeting pages carries a ``dfile`` (direct MP4), a ``seq`` (playback
    order), and a ``title`` (the agenda-item name). Returns pairs sorted by ``seq``; ``title``
    is an empty string when the field is absent. Pure (no network)."""
    text = content.decode("utf-8", errors="replace")
    found: list[tuple[int, str, str]] = []
    for obj in _SEGMENT_OBJ_RE.findall(text):
        df = _DFILE_RE.search(obj)
        if not df:
            continue
        seq = _SEQ_RE.search(obj)
        title_m = _TITLE_RE.search(obj)
        title = html.unescape(title_m.group(1)).strip() if title_m else ""
        found.append((int(seq.group(1)) if seq else 0, df.group(1), title))
    found.sort(key=lambda s: s[0])
    return [(url, title) for _, url, title in found]


def parse_media_segments(content: bytes) -> list[str]:
    """Parse a Swagit video page's inline ``dfile`` media segments, ordered by ``seq``.

    Older meetings render each agenda segment as a JSON object carrying a direct, reachable MP4
    (``dfile``); newer meetings have none (they use ``/download``). Returns the segment MP4 URLs
    in playback order. Pure (no network)."""
    return [url for url, _ in parse_segment_objects(content)]


def parse_list(content: bytes, origin: str) -> list[Episode]:
    """Parse a Swagit view page into episodes (all bodies; ``Episode.body`` set).

    Body filtering is applied generically downstream via ``source.body``. Pure (no
    network); media URLs are resolved later by ``resolve_media_url``.
    """
    text = content.decode("utf-8", errors="replace")
    episodes: list[Episode] = []
    for vid, raw_body, raw_date in ROW_RE.findall(text):
        body_name = html.unescape(raw_body).strip()
        published = _parse_date(raw_date)
        if published is None:
            continue
        episodes.append(
            Episode(
                guid=vid,
                title=f"{body_name} – {raw_date.strip()}",
                published=published,
                video_url=f"{origin}/videos/{vid}/download",
                media_kind="hls",
                body=body_name,
                # video_url is the expiring /download redirect; the canonical human watch
                # page is /videos/{id}, so link there rather than the download endpoint.
                links={"canonical_video": f"{origin}/videos/{vid}"},
            )
        )
    return episodes


def _parse_date(raw: str) -> datetime | None:
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(raw.strip(), fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


class SwagitProvider:
    name = "swagit"
    # Swagit's player uses /play/{video_id}/{seconds} for time-anchored navigation.
    # Verified from live page chapter anchors: href="/play/389304/51" etc.
    capabilities: frozenset[str] = frozenset({"deeplink"})

    def video_deeplink(self, ref: str, t_seconds: float) -> str | None:
        """Build ``{origin}/play/{video_id}/{t}`` from a Swagit video watch-page URL.

        ``ref`` must be a ``/videos/{id}`` canonical URL set by :func:`parse_list`.
        Returns ``None`` when ``ref`` cannot be parsed (safety guard).

        Verified format: ``https://<tenant>.swagit.com/play/<video_id>/<t_seconds>``
        (chapter anchors on live Swagit pages use this exact pattern).
        """
        parts = urlsplit(ref)
        path_parts = parts.path.rstrip("/").rsplit("/", 1)
        if len(path_parts) < 2 or not path_parts[1].isdigit():
            return None
        video_id = path_parts[1]
        origin = f"{parts.scheme}://{parts.netloc}"
        return f"{origin}/play/{video_id}/{int(t_seconds)}"

    def validate(self, source: dict) -> None:
        if not source.get("list_url"):
            raise ValueError("swagit source requires 'list_url'")
        if not source.get("body"):
            raise ValueError("swagit source requires 'body' (meeting-body filter)")

    def detect_change(self, source: dict) -> ChangeToken | None:
        return None  # list page is one fetch; no cheap change probe

    def fetch_episodes(self, source: dict) -> list[Episode]:
        url = source["list_url"]
        with make_session() as session:
            try:
                resp = session.get(url, timeout=DEFAULT_TIMEOUT)
            except requests.RequestException as exc:
                raise ProviderError(f"GET {url} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(f"GET {url} returned {resp.status_code}")
        return parse_list(resp.content, _origin(url))

    def fetch_chapters(self, episode: Episode, source: dict) -> tuple[list[dict], str | None]:
        """Fetch a video page and extract (chapter markers, transcript URL).

        One network call per episode, so the chapters stage budgets/spreads these. Returns
        ``([], None)`` on a page with no agenda items. The transcript URL is only returned when
        the page actually links it (so we never emit a transcript link a meeting doesn't have).
        """
        origin = _origin(source["list_url"])
        url = f"{origin}/videos/{episode.guid}"
        with make_session() as session:
            try:
                resp = session.get(url, timeout=DEFAULT_TIMEOUT)
            except requests.RequestException as exc:
                raise ProviderError(f"GET {url} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(f"GET {url} returned {resp.status_code}")
        chapters = parse_chapters(resp.content)
        transcript_path = f"/videos/{episode.guid}/transcript"
        transcript = f"{origin}{transcript_path}" if transcript_path in resp.text else None
        return chapters, transcript

    def fetch_segment_objects(
        self, episode: Episode, source: dict
    ) -> list[tuple[str, str]] | None:
        """Return ``(url, title)`` pairs for keyless legacy meetings; ``None`` for modern ones.

        Called by :class:`~citypods.concat.SwagitConcatPlanner` during the timeline stage.

        * Returns ``None`` when ``/download`` resolves to a keyed presigned URL — the meeting
          is modern and ``resolve_media_url`` handles it normally.
        * Returns a list (possibly empty) for keyless meetings.  The caller checks the length:
          0 → dead (no usable media), 1 → single-segment (normal path), ≥2 → concat.

        Raises :class:`~citypods.providers.base.ProviderError` on unrecoverable network failure.
        """
        with make_session() as session:
            try:
                resp = session.get(
                    episode.video_url, timeout=DEFAULT_TIMEOUT, allow_redirects=False
                )
            except requests.RequestException as exc:
                raise ProviderError(f"GET {episode.video_url} failed: {exc}") from exc
            loc = resp.headers.get("Location")
            is_redirect = resp.status_code in (301, 302, 303, 307, 308) and bool(loc)
            if is_redirect and _s3_object_key(loc):
                return None  # modern: keyed presigned URL
            if not is_redirect and resp.status_code < 400:
                return None  # already a direct file (non-redirect path)
            if not is_redirect:
                raise ProviderError(
                    f"download resolve for {episode.guid} returned {resp.status_code}"
                )
            return self._page_segment_objects(episode, source, session)

    def resolve_media_url(self, episode: Episode, source: dict) -> str:
        """Resolve an episode's audio source URL.

        Newer meetings: follow ``/videos/{id}/download`` to its presigned, keyed MP4. For older
        meetings that redirect 302s to a *keyless* presigned URL (signs only the bucket prefix,
        so ffmpeg fails — issue #120), fall back to the direct ``dfile`` MP4(s) scraped from the
        ``/videos/{id}`` page:

          * one segment  -> return it directly;
          * many segments -> planned by :class:`~citypods.concat.SwagitConcatPlanner`; when
            ``ep.sources`` is already populated the fast-path below returns without a network call.
            If the planner hasn't run (e.g. not registered), raises :data:`MEDIA_DEFERRED` so the
            pipeline backs off rather than churning the budget;
          * no segments  -> truly un-materializable; raised as :data:`MEDIA_DEAD`.
        """
        # Fast path: already planned as multi-segment concat by SwagitConcatPlanner.
        # _sources_by_id will build the full source→URL map from ep.sources; this return
        # value is only used as the single-source fallback and is ignored for concat episodes.
        if len(episode.sources) > 1:
            return episode.sources[0].ref

        with make_session() as session:
            resp = session.get(episode.video_url, timeout=DEFAULT_TIMEOUT, allow_redirects=False)
            loc = resp.headers.get("Location")
            is_redirect = resp.status_code in (301, 302, 303, 307, 308) and bool(loc)
            if is_redirect and _s3_object_key(loc):
                return loc  # modern meeting: keyed presigned MP4, usable as-is
            if not is_redirect and resp.status_code < 400:
                return episode.video_url  # already the file
            if not is_redirect:
                raise ProviderError(
                    f"download resolve for {episode.guid} returned {resp.status_code}"
                )
            # Keyless redirect: fall back to the page-scraped direct media (issue #120).
            segments = self._page_segments(episode, source, session)
        if len(segments) == 1:
            return segments[0]
        if len(segments) > 1:
            raise MediaUnavailable(
                f"{episode.guid}: legacy multi-segment meeting ({len(segments)} parts) — "
                "SwagitConcatPlanner not registered or probe failed; backing off",
                code=MEDIA_DEFERRED,
            )
        raise MediaUnavailable(
            f"{episode.guid}: /download is keyless and the video page exposes no usable media",
            code=MEDIA_DEAD,
        )

    def _page_segments(self, episode: Episode, source: dict, session) -> list[str]:
        """Fetch the ``/videos/{id}`` page and return its inline ``dfile`` segment URLs."""
        return [url for url, _ in self._page_segment_objects(episode, source, session)]

    def _page_segment_objects(
        self, episode: Episode, source: dict, session
    ) -> list[tuple[str, str]]:
        """Fetch the ``/videos/{id}`` page and return its inline ``(dfile, title)`` segment pairs."""
        origin = _origin(source["list_url"])
        url = f"{origin}/videos/{episode.guid}"
        try:
            resp = session.get(url, timeout=DEFAULT_TIMEOUT)
        except requests.RequestException as exc:
            raise ProviderError(f"GET {url} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(f"GET {url} returned {resp.status_code}")
        return parse_segment_objects(resp.content)
