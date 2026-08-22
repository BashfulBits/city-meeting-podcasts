"""Safe, content-addressed R6 social-video rendering.

The renderer is intentionally conservative. It renders one source cut at a time, preserves a
native-resolution square pane, and returns ``video-unavailable`` instead of upscaling a source or
publishing a clip whose captions cannot be grounded.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from citypods.clips import _clip_timeline
from citypods.models import Episode
from citypods.security import SecurityError, validate_source_url

VIDEO_PIPELINE_VERSION = "1"
CAPTION_RECIPE_VERSION = "1"
FRAMING_RECIPE_VERSION = "1"
WATERMARK_VERSION = "1"
MIN_VISIBLE_PX = 720


def video_clip_key(
    episode_uid: str,
    start: float,
    end: float,
    timeline_version: str,
    *,
    output_profile: str = "vertical-9x16-square-pane-v1",
) -> str:
    payload = {
        "uid": episode_uid,
        "start_ms": round(start * 1000),
        "end_ms": round(end * 1000),
        "timeline": timeline_version,
        "video_pipeline": VIDEO_PIPELINE_VERSION,
        "caption_recipe": CAPTION_RECIPE_VERSION,
        "framing_recipe": FRAMING_RECIPE_VERSION,
        "watermark": WATERMARK_VERSION,
        "profile": output_profile,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]
    return f"clips/{episode_uid}/r6-{digest}.mp4"


def caption_text(segments: list[Mapping[str, Any]], start: float, end: float) -> str:
    """Return only transcript text overlapping a quote range, safely joined for a caption file."""
    return " ".join(
        str(row.get("text") or "").replace("\n", " ").strip()
        for row in segments
        if float(row.get("end") or 0) > start and float(row.get("start") or 0) < end
    ).strip()


def _ass_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", " ")


def _write_ass(segments: list[Mapping[str, Any]], start: float, end: float, path: Path) -> None:
    rows = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: 720",
        "PlayResY: 720",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Default,DejaVu Sans,34,&H00FFFFFF,&H00FFFFFF,&H80000000,&H80000000,1,0,0,0,"
        "100,100,0,0,1,3,0,2,24,24,34,1",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for row in segments:
        cue_start = max(float(row.get("start") or 0), start) - start
        cue_end = min(float(row.get("end") or 0), end) - start
        text = _ass_escape(str(row.get("text") or "").strip())
        if cue_end <= cue_start or not text:
            continue
        rows.append(
            f"Dialogue: 0,{_ass_time(cue_start)},{_ass_time(cue_end)},Default,,0,0,34,,{text}"
        )
    path.write_text("\n".join(rows) + "\n")


def _ass_time(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    seconds_value, centi = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{seconds_value:02d}.{centi:02d}"


def _probe_size(binary: str, source_url: str) -> tuple[int, int] | None:
    try:
        completed = subprocess.run(
            [
                binary,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "json",
                source_url,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        stream = (json.loads(completed.stdout).get("streams") or [{}])[0]
        width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, json.JSONDecodeError):
        return None
    return (width, height) if width > 0 and height > 0 else None


def technical_video_gate(
    source_url: str,
    *,
    binary: str,
    captions_available: bool,
    withheld: bool = False,
) -> bool:
    """Check the non-negotiable media prerequisites before admission or rendering."""
    if withheld or not captions_available or not source_url:
        return False
    try:
        validate_source_url(source_url, resolve=False)
    except SecurityError:
        return False
    size = _probe_size(binary, source_url)
    return size is not None and min(size) >= MIN_VISIBLE_PX


def render_video_clip(
    ep: Episode,
    candidate: Mapping[str, Any],
    *,
    source_url: str,
    binary: str,
    storage,
    segments: list[dict[str, Any]],
    timeline_version: str,
    crop_anchor: Mapping[str, float] | None = None,
    profile: str = "vertical-9x16-square-pane-v1",
) -> dict[str, Any]:
    """Render an admitted quote, or return a stable failure status without uploading anything."""
    start = float(candidate.get("start") or 0)
    end = float(candidate.get("end") or 0)
    if end <= start or not source_url or not segments:
        return {"status": "video-unavailable", "reason": "source-or-caption-gate"}
    try:
        validate_source_url(source_url, resolve=False)
    except SecurityError:
        return {"status": "video-unavailable", "reason": "unsafe-source-url"}
    size = _probe_size(binary, source_url)
    if size is None or min(size) < MIN_VISIBLE_PX:
        return {"status": "video-unavailable", "reason": "source-resolution"}
    key = video_clip_key(ep.uid or ep.guid, start, end, timeline_version, output_profile=profile)
    if storage.exists(key):
        url = storage.public_url(key)
        try:
            validate_source_url(url, resolve=False)
        except SecurityError:
            return {"status": "video-unavailable", "reason": "unsafe-output-url"}
        return {"status": "ready", "key": key, "url": url, "reused": True}
    if ep.timeline is not None:
        _sub_timeline, cuts = _clip_timeline(ep.timeline, start, end)
        if len(cuts) != 1:
            return {"status": "video-unavailable", "reason": "multi-source-cut"}
        source_start, source_end = cuts[0][1], cuts[0][2]
    else:
        source_start, source_end = start, end
    duration = source_end - source_start
    if duration < 8.0 or duration > 90.0:
        return {"status": "video-unavailable", "reason": "duration-gate"}
    with tempfile.TemporaryDirectory() as temporary:
        temporary_path = Path(temporary)
        ass_path = temporary_path / "captions.ass"
        output_path = temporary_path / "clip.mp4"
        _write_ass(segments, start, end, ass_path)
        square_output = profile.startswith("square")
        canvas_height = MIN_VISIBLE_PX if square_output else 1280
        pane_y = 0 if square_output else 280
        watermark_y = canvas_height - 40
        # A supplied anchor is the only crop override.  Without one, preserve a stable group
        # square; an optional face analyzer can be added behind this versioned seam later.
        x_expr = "(iw-720)/2"
        y_expr = "(ih-720)/2"
        if crop_anchor:
            x_expr = f"max(0,min(iw-720,{float(crop_anchor.get('x', 0.5))}*iw-360))"
            y_expr = f"max(0,min(ih-720,{float(crop_anchor.get('y', 0.5))}*ih-360))"
        filter_graph = (
            f"scale={MIN_VISIBLE_PX}:{MIN_VISIBLE_PX}:force_original_aspect_ratio=increase,"
            f"crop={MIN_VISIBLE_PX}:{MIN_VISIBLE_PX}:{x_expr}:{y_expr},"
            f"pad=720:{canvas_height}:0:{pane_y}:color=black,"
            f"subtitles={_ffmpeg_path(ass_path)},"
            f"drawtext=text='citymeetings.fyi':fontcolor=white:fontsize=24:x=24:y={watermark_y}"
        )
        command = [
            binary,
            "-y",
            "-ss",
            f"{source_start:.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            source_url,
            "-vf",
            filter_graph,
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-an",
            str(output_path),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, timeout=duration * 6 + 60)
        except (OSError, subprocess.SubprocessError):
            return {"status": "video-unavailable", "reason": "render-failed"}
        url = storage.put_file(key, output_path, "video/mp4")
    try:
        validate_source_url(url, resolve=False)
    except SecurityError:
        return {"status": "video-unavailable", "reason": "unsafe-output-url"}
    return {
        "status": "ready",
        "key": key,
        "url": url,
        "profile": profile,
        "resolution": f"720x{canvas_height}",
        "framing": "manual-anchor" if crop_anchor else "stable-group-fallback",
        "watermark": "citymeetings.fyi",
        "reused": False,
    }


def _ffmpeg_path(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace(":", r"\:").replace("'", r"\'")


__all__ = [
    "MIN_VISIBLE_PX",
    "render_video_clip",
    "technical_video_gate",
    "video_clip_key",
]
