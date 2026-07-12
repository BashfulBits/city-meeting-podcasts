"""Canonical chapter accessors."""

from __future__ import annotations

from citypods.models import Episode
from citypods.timeline import remap, timeline_digest


def episode_served_chapters(ep: Episode) -> list[dict]:
    """Served-time chapter view for an episode.

    When ``source_chapters`` is present, it is the canonical raw input and the served-time chapter
    list is derived from it plus the current timeline. Synthetic served-only chapter producers leave
    ``source_chapters`` empty and fall back to the stored ``ep.chapters`` list.
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

    return remap(
        ep.timeline,
        [dict(ch) for ch in ep.source_chapters],
        source_id=next(iter(source_ids)),
    )
