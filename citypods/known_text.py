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
_BRACKET_TIMESTAMP = re.compile(r"\[(\d{1,2}:\d{2}(?::\d{2})?)\]")
_BARE_TIMESTAMP_LINE = re.compile(r"(?m)^[ \t]*(\d{1,2}:\d{2}(?::\d{2})?)[ \t]*$")
_INLINE_TIMESTAMP = re.compile(r"\[?\d{1,2}:\d{2}(?::\d{2})?\]?")
_BRACKET_ANNOTATION = re.compile(r"\[[^\]\r\n]*\]")
_SPEAKER_PREFIX = re.compile(r"^[A-Z][A-Z .\-']{1,60}:\s*")
_MINUTES_HEADINGS = frozenset(
    {
        "AGENDA",
        "THE AGENDA",
        "CALL TO ORDER",
        "ROLL CALL",
        "CONSENT AGENDA",
        "ADJOURNMENT",
    }
)


def _seconds(value: str) -> float:
    parts = [float(part) for part in value.split(":")]
    if len(parts) == 2:
        minutes, seconds = parts
        return minutes * 60 + seconds
    hours, minutes, seconds = parts
    return hours * 3600 + minutes * 60 + seconds


def clean_provider_text(text: str) -> str:
    """Remove bracketed annotations and common minutes-only line noise."""
    lines: list[str] = []
    for line in text.splitlines():
        # Bracketed annotations are metadata, but never cross a line boundary: a malformed
        # provider document must not let one unmatched ``[`` delete later spoken paragraphs.
        value = _BRACKET_ANNOTATION.sub(" ", line)
        value = _INLINE_TIMESTAMP.sub(" ", value).strip()
        if not value:
            continue
        value = _SPEAKER_PREFIX.sub("", value).strip()
        # Only discard known agenda headings. Arbitrary all-caps text can be a provider's
        # transcription convention (for example, ``SO MOVED``) and belongs in the transcript.
        if value in _MINUTES_HEADINGS:
            continue
        lines.append(value)
    if lines:
        return " ".join(lines)
    # A plain transcript (or an unfamiliar provider layout) should not lose its content merely
    # because none of the narrow metadata heuristics recognized a spoken line.
    fallback = " ".join(_BRACKET_ANNOTATION.sub(" ", line) for line in text.splitlines())
    return re.sub(r"\s+", " ", _INLINE_TIMESTAMP.sub(" ", fallback)).strip()


def provider_align_ineligible(registry: object) -> bool:
    """Whether a provider candidate failed this version of known-text alignment."""
    if not isinstance(registry, dict):
        return False
    marker = f"provider-align:{PROVIDER_ALIGN_PIPELINE_VERSION}"
    return any(
        isinstance(artifact, dict) and artifact.get("align_ineligible_pipeline_version") == marker
        for artifact in (registry.get("candidate"), registry.get("known_good"))
    )


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
    source_text = text.replace("\r\n", "\n")
    # Swagit markers normally occupy their own line, but some exports begin a paragraph on the
    # same line as ``[HH:MM:SS]``. Preserve both forms; only bracketed inline timestamps count
    # as section markers so ordinary prose times are never treated as cue boundaries.
    markers: list[tuple[int, int, float]] = [
        (match.start(), match.end(), _seconds(match.group(1)))
        for match in _BRACKET_TIMESTAMP.finditer(source_text)
    ]
    markers.extend(
        (match.start(), match.end(), _seconds(match.group(1)))
        for match in _BARE_TIMESTAMP_LINE.finditer(source_text)
    )
    markers.sort()

    raw_sections: list[dict] = []
    if markers:
        for index, (_marker_start, marker_end, start) in enumerate(markers):
            next_start = markers[index + 1][2] if index + 1 < len(markers) else source_end
            if next_start is not None and next_start <= start:
                continue
            body_end = markers[index + 1][0] if index + 1 < len(markers) else len(source_text)
            body = clean_provider_text(source_text[marker_end:body_end])
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
