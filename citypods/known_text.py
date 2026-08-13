"""Prepare provider-supplied meeting text for known-text alignment.

Swagit-style documents contain coarse source-time markers between paragraphs.  They are useful
alignment windows, but the markers and surrounding minutes metadata are not spoken audio.  This
module keeps that distinction explicit and returns the same segment-shaped input consumed by
WhisperX and the H15 provider-align candidate.
"""

from __future__ import annotations

import re

from citypods.timeline import Segment, Timeline, remap

# Shared by the work-queue classifier and stage artifact recipes. Bump when provider-align output
# must be regenerated for every stored provider document.
PROVIDER_ALIGN_PIPELINE_VERSION = "2"
_TIMESTAMP_LINE = re.compile(r"^\s*\[?(\d{1,2}:\d{2}(?::\d{2})?)\]?\s*$")
_INLINE_TIMESTAMP = re.compile(r"\[?\d{1,2}:\d{2}(?::\d{2})?\]?")


def _seconds(value: str) -> float:
    parts = [float(part) for part in value.split(":")]
    if len(parts) == 2:
        minutes, seconds = parts
        return minutes * 60 + seconds
    hours, minutes, seconds = parts
    return hours * 3600 + minutes * 60 + seconds


def clean_provider_text(text: str) -> str:
    """Remove bracketed annotations and common minutes-only line noise."""
    text = re.sub(r"\[[\s\S]*?\]", " ", text)
    lines: list[str] = []
    for line in text.splitlines():
        value = _INLINE_TIMESTAMP.sub(" ", line).strip()
        if not value or value == value.upper() and re.search(r"[A-Z]", value):
            continue
        value = re.sub(r"^[A-Z][A-Z\s.\-]+:\s*", "", value).strip()
        if len(value.split()) > 2:
            lines.append(value)
    cleaned = " ".join(lines)
    # A document without minutes-style metadata should not be destroyed by the heuristics.
    if len(cleaned.split()) < max(1, len(text.split()) // 5):
        return re.sub(r"\s+", " ", text).strip()
    return cleaned


def provider_sections(
    text: str,
    *,
    duration: float | None = None,
    source_duration: float | None = None,
    timeline: Timeline | dict | None = None,
    source_id: str | None = None,
) -> list[dict]:
    """Return clean text sections with served-time alignment windows.

    Markers are interpreted in source time.  When an EDL is present, each section boundary is
    remapped through it; cut boundaries are snapped to the next kept audio for starts and clamped
    to the preceding kept boundary for ends.  With no markers the whole document is one section.
    """
    if isinstance(timeline, dict):
        timeline = Timeline(
            version=str(timeline.get("version") or "persisted"),
            basis=str(timeline.get("basis") or "served"),
            segments=tuple(Segment(**segment) for segment in timeline.get("segments") or []),
        )
    if timeline is not None and source_duration is None:
        source_duration = max(
            (
                float(segment.source_end)
                for segment in timeline.segments
                if segment.kind == "source" and segment.source_end is not None
            ),
            default=0.0,
        )
    source_end = source_duration or duration
    lines = text.replace("\r\n", "\n").splitlines()
    markers: list[tuple[int, float]] = []
    for index, line in enumerate(lines):
        match = _TIMESTAMP_LINE.match(line)
        if match:
            markers.append((index, _seconds(match.group(1))))

    raw_sections: list[dict] = []
    if markers:
        for index, (line_no, start) in enumerate(markers):
            next_start = markers[index + 1][1] if index + 1 < len(markers) else source_end
            if next_start is None or next_start <= start:
                continue
            body_end = markers[index + 1][0] if index + 1 < len(markers) else len(lines)
            body = clean_provider_text("\n".join(lines[line_no + 1 : body_end]))
            if body:
                raw_sections.append({"start": start, "end": next_start, "text": body})
    else:
        body = clean_provider_text(text)
        if body:
            raw_sections.append({"start": 0.0, "end": source_end, "text": body})
    if timeline is None:
        return raw_sections
    mapped = remap(
        timeline,
        raw_sections,
        source_id=source_id,
        clamp_to=duration,
        snap_cut_starts=True,
    )
    return [
        section
        for section in mapped
        if section.get("end") is None or section["end"] > section["start"]
    ]
