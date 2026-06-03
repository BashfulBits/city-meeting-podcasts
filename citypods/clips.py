"""Clip extraction service (INFRA-7, issue #148).

``extract_clip`` forward-maps a served-time range through the episode's EDL into
one or more source cuts, encodes them, and uploads a content-addressed clip object.

Used by:
  - Soundbites (roadmap #15): audio clips + ``<podcast:soundbite>`` tags in served time.
  - Future video-clip compilation: same call with ``kind="video"``.

Content addressing
------------------
Clip objects are keyed by ``(uid, served_range, timeline_version, kind)`` so:
  - The same clip range re-extracted from the same episode with the same timeline
    produces the same object key → storage reuse / no re-upload.
  - Changing the episode's timeline (e.g. a silence-trim update) changes the key →
    cache-bust + orphan-GC reclaim on the usual cycle.
  - The key is independent of the source URL (which may expire).

Deep-link attribution
---------------------
``ClipArtifact.source_cuts`` carries ``(source_id, src_start, src_end)`` tuples.
Callers can resolve a deep-link for each cut via
``provider.video_deeplink(sources[source_id].ref, src_start)`` (gated on
``"deeplink" in provider.capabilities``).

Multi-source clips
------------------
A clip range can span a concat boundary: the served range ``[600, 900]`` might come
from the end of source s0 AND the start of source s1.  The EDL forward-map produces
one source cut per overlapping segment; ``build_filter_complex`` (INFRA-3) then
concatenates them exactly as the timeline encoder does.
"""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from citypods.media import CONTENT_TYPE
from citypods.models import Episode
from citypods.storage.base import StorageBackend
from citypods.timeline import Segment, Timeline, timeline_digest

# ---------------------------------------------------------------------------
# ClipArtifact
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClipArtifact:
    """A content-addressed audio or video clip extracted from a served episode range.

    ``source_cuts`` is ordered by playback: each entry is ``(source_id, src_start, src_end)``
    in source-clock seconds.  Callers use these to attribute deep-links back to the
    city's own video at the matching moment.
    """

    key: str
    url: str
    uid: str
    served_start: float
    served_end: float
    timeline_version: str
    kind: str  # "audio" | "video"
    source_cuts: tuple[tuple[str, float, float], ...]


# ---------------------------------------------------------------------------
# Key / Timeline helpers
# ---------------------------------------------------------------------------


def clip_object_key(
    uid: str, served_start: float, served_end: float, timeline_version: str, kind: str
) -> str:
    """Content-addressed storage key for a clip.

    Stable across re-extractions with the same parameters; changes when the EDL or
    served range changes (giving a cache-bust URL and orphan-GC reclaim).
    """
    content = f"{uid}|{served_start}|{served_end}|{timeline_version}|{kind}"
    h = hashlib.sha1(content.encode()).hexdigest()[:16]
    ext = "m4a" if kind == "audio" else "mp4"
    return f"clips/{uid}/{h}.{ext}"


def _clip_timeline(
    ep_timeline: Timeline,
    clip_start: float,
    clip_end: float,
) -> tuple[Timeline, tuple[tuple[str, float, float], ...]]:
    """Build a sub-Timeline covering only the ``[clip_start, clip_end]`` served range.

    Returns ``(clip_timeline, source_cuts)`` where ``source_cuts`` is an ordered tuple of
    ``(source_id, src_start, src_end)`` for attribution / deep-link construction.

    A clip range that spans a concat boundary produces multiple source-segment entries in
    ``clip_timeline.segments`` and multiple entries in ``source_cuts``.
    """
    new_segs: list[Segment] = []
    cuts: list[tuple[str, float, float]] = []
    cur = 0.0  # running served position in the new clip timeline

    for seg in ep_timeline.segments:
        overlap_start = max(clip_start, seg.served_start)
        overlap_end = min(clip_end, seg.served_end)
        if overlap_start >= overlap_end:
            continue

        span = overlap_end - overlap_start

        if seg.kind == "source":
            # Constant-offset map: source_t = served_t + (source_start - served_start)
            offset = seg.source_start - seg.served_start  # type: ignore[operator]
            src_start = overlap_start + offset
            src_end = overlap_end + offset
            new_segs.append(
                Segment(
                    served_start=cur,
                    served_end=cur + span,
                    kind="source",
                    source_id=seg.source_id,
                    source_start=src_start,
                    source_end=src_end,
                )
            )
            cuts.append((seg.source_id, src_start, src_end))  # type: ignore[arg-type]

        elif seg.kind == "insert":
            new_segs.append(
                Segment(
                    served_start=cur,
                    served_end=cur + span,
                    kind="insert",
                    insert=seg.insert,
                    asset_id=seg.asset_id,
                    asset_version=seg.asset_version,
                )
            )

        cur += span

    clip_tl = Timeline(version=ep_timeline.version, segments=tuple(new_segs))
    return clip_tl, tuple(cuts)


def _identity_clip_timeline(
    source_id: str,
    served_start: float,
    served_end: float,
) -> tuple[Timeline, tuple[tuple[str, float, float], ...]]:
    """Build a single-segment identity clip timeline (no EDL manipulation)."""
    seg = Segment(
        served_start=0.0,
        served_end=served_end - served_start,
        kind="source",
        source_id=source_id,
        source_start=served_start,
        source_end=served_end,
    )
    tl = Timeline(version="identity", segments=(seg,))
    cuts = ((source_id, served_start, served_end),)
    return tl, cuts


# ---------------------------------------------------------------------------
# extract_clip
# ---------------------------------------------------------------------------


def extract_clip(
    ep: Episode,
    served_start: float,
    served_end: float,
    *,
    kind: str = "audio",
    storage: StorageBackend,
    ffmpeg,  # FfmpegRunner — typed loosely to avoid circular import
    resolve_media_url: Callable[[Episode], str],
    max_kbps: int = 96,
    asset_resolver: Callable[[str, str | None], Path] | None = None,
) -> ClipArtifact:
    """Extract and upload a clip from the served episode range ``[served_start, served_end]``.

    The clip is content-addressed: if an object with the same key already exists in storage
    it is reused (the URL is re-attached without re-encoding).

    Args:
        ep: The episode to clip from. ``ep.timeline`` and ``ep.sources`` must be populated
            (or identity / empty) by the time this is called.
        served_start / served_end: Clip boundaries in served-enclosure seconds.
        kind: ``"audio"`` (default) or ``"video"`` (future).
        storage: Object storage backend for upload + URL generation.
        ffmpeg: FfmpegRunner implementation.
        resolve_media_url: Called to resolve the (possibly expiring) source URL just before
            the encode. For multi-source concat, the same resolver is called once per source
            (extension point — the concat planner feature will supply per-source resolution).
        max_kbps: AAC bitrate cap; ignored when ``kind="video"``.
        asset_resolver: Required when the clip spans an insert segment.

    Returns:
        A :class:`ClipArtifact` with the storage key, public URL, and source cuts for
        deep-link attribution.

    Raises:
        ValueError: When the clip range is empty or invalid.
    """
    if served_end <= served_start:
        raise ValueError(f"empty clip range: {served_start} >= {served_end}")

    # Determine timeline version for content-addressing
    tl_version = ep.timeline.version if ep.timeline is not None else "identity"
    key = clip_object_key(ep.uid or "", served_start, served_end, tl_version, kind)

    # Reuse if already in storage
    if storage.exists(key):
        return ClipArtifact(
            key=key,
            url=storage.public_url(key),
            uid=ep.uid or "",
            served_start=served_start,
            served_end=served_end,
            timeline_version=tl_version,
            kind=kind,
            source_cuts=_get_source_cuts(ep, served_start, served_end),
        )

    # Build the clip sub-timeline and source cuts
    source_id = ep.sources[0].id if ep.sources else "s0"
    if ep.timeline is None or timeline_digest(ep.timeline) == "":
        clip_tl, source_cuts = _identity_clip_timeline(source_id, served_start, served_end)
    else:
        clip_tl, source_cuts = _clip_timeline(ep.timeline, served_start, served_end)

    # Resolve source URLs (for now: single-resolver pattern; multi-source extension point)
    source_url = resolve_media_url(ep)
    sources_by_id: dict[str, str] = {}
    for seg in clip_tl.segments:
        if seg.kind == "source" and seg.source_id not in sources_by_id:
            sources_by_id[seg.source_id] = source_url  # type: ignore[index]

    # Encode and upload
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / f"clip.{'m4a' if kind == 'audio' else 'mp4'}"
        ffmpeg.extract_audio(
            clip_tl,
            sources_by_id,
            dest,
            None,  # no chapter markers on clips
            asset_resolver=asset_resolver,
        )
        content_type = CONTENT_TYPE if kind == "audio" else "video/mp4"
        url = storage.put_file(key, dest, content_type)

    return ClipArtifact(
        key=key,
        url=url,
        uid=ep.uid or "",
        served_start=served_start,
        served_end=served_end,
        timeline_version=tl_version,
        kind=kind,
        source_cuts=source_cuts,
    )


def _get_source_cuts(
    ep: Episode, served_start: float, served_end: float
) -> tuple[tuple[str, float, float], ...]:
    """Return source cuts for an episode (used on the reuse path)."""
    source_id = ep.sources[0].id if ep.sources else "s0"
    if ep.timeline is None or timeline_digest(ep.timeline) == "":
        return ((source_id, served_start, served_end),)
    _, cuts = _clip_timeline(ep.timeline, served_start, served_end)
    return cuts
