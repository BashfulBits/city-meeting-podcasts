"""An episode's transcript, chapters and tag definitions as the evidence builders read them."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from citypods.judging.tasks import EpisodeTexts, transcript_identity


def build_episode_texts(ep: Any, raw: bytes | None, definitions: Mapping[str, str]) -> EpisodeTexts:
    """The evidence inputs for ``ep`` from its transcript bytes (``None`` or empty: no segments).

    Shared by the judge stage and the weekly audit, so a human is shown exactly the text a judge
    was shown."""
    from citypods.moments import parse_transcript_segments
    from citypods.tags import chapter_id

    segments = parse_transcript_segments(raw or b"", getattr(ep, "transcript_format", "") or "vtt")
    chapters = []
    served = [c for c in getattr(ep, "chapters", None) or [] if isinstance(c, dict)]
    for index, chapter in enumerate(served):
        start = float(chapter.get("start") or 0.0)
        end = float(served[index + 1].get("start") or 0.0) if index + 1 < len(served) else None
        chapters.append(
            (chapter_id(ep, chapter, index), str(chapter.get("title") or ""), start, end)
        )
    return EpisodeTexts(
        segments=segments,
        chapters=tuple(chapters),
        definitions=definitions,
        identity=transcript_identity(ep),
    )


def tag_definitions() -> dict[str, str]:
    """Tag id -> "label: description", from the taxonomy the tagger uses."""
    from citypods.tags import load_taxonomy

    return {
        tag_id: f"{tag.label}: {tag.description}".strip(": ")
        for tag_id, tag in load_taxonomy().by_id.items()
    }


ReadBytes = Callable[[str], "bytes | None"]
