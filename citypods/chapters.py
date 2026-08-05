"""Canonical chapter accessors."""

from __future__ import annotations

from citypods.models import Episode
from citypods.timeline import remap, timeline_digest


def episode_served_chapters(ep: Episode, *, with_source_index: bool = False) -> list[dict]:
    """Served-time chapter view for an episode.

    When ``source_chapters`` is present, it is the canonical raw input and the served-time chapter
    list is derived from it plus the current timeline. Synthetic served-only chapter producers leave
    ``source_chapters`` empty and fall back to the stored ``ep.chapters`` list.

    Provider chapter starts that fall in a removed source span are snapped to the next kept served
    boundary. The served list's position therefore no longer necessarily lines up with its source
    position. Passing ``with_source_index=True`` stamps each surviving chapter with its true source
    position under ``source_index``, for callers (``chapter_id()``) that must look up the untouched
    source chapter rather than assume position == position. Off by default so unrelated consumers
    keep their existing dict shape.
    """
    if not ep.source_chapters:
        return [dict(ch) for ch in ep.chapters]

    if ep.timeline is None or timeline_digest(ep.timeline, ep.sources) == "":
        return [dict(ch) for ch in ep.source_chapters]

    source_ids = {
        seg.source_id
        for seg in ep.timeline.segments
        if seg.kind == "source" and seg.source_id is not None
    }
    if len(source_ids) != 1:
        return [dict(ch) for ch in ep.chapters]

    items = (
        [dict(ch, source_index=i) for i, ch in enumerate(ep.source_chapters)]
        if with_source_index
        else [dict(ch) for ch in ep.source_chapters]
    )
    return remap(
        ep.timeline,
        items,
        source_id=next(iter(source_ids)),
        snap_cut_starts=True,
    )
