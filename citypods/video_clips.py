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
from citypods.http import HOST_LIMITER, USER_AGENT, make_session
from citypods.models import Episode
from citypods.moments import MOMENTS_MAX_SECONDS, MOMENTS_MIN_SECONDS
from citypods.security import SecurityError, validate_source_url

VIDEO_PIPELINE_VERSION = "2"
CAPTION_RECIPE_VERSION = "1"
FRAMING_RECIPE_VERSION = "opencv-haar-mouth-motion-v1"
WATERMARK_VERSION = "1"
MIN_VISIBLE_PX = 720


def video_clip_key(
    episode_uid: str,
    start: float,
    end: float,
    timeline_version: str,
    *,
    source_identity: str,
    output_profile: str = "vertical-9x16-square-pane-v1",
) -> str:
    payload = {
        "uid": episode_uid,
        "start_ms": round(start * 1000),
        "end_ms": round(end * 1000),
        "timeline": timeline_version,
        "source": source_identity,
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


def _write_ass(
    segments: list[Mapping[str, Any]],
    start: float,
    end: float,
    path: Path,
    *,
    caption_override: str | None = None,
) -> None:
    """Write transcript-grounded captions, allowing only an exact maintainer wording override."""
    if caption_override:
        if _caption_norm(caption_override) != _caption_norm(caption_text(segments, start, end)):
            raise ValueError("caption override must match grounded transcript wording")
        segments = [{"start": start, "end": end, "text": caption_override}]
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


def _caption_norm(value: str) -> str:
    return " ".join(value.split()).casefold()


def _ass_time(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    seconds_value, centi = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{seconds_value:02d}.{centi:02d}"


def _safe_media_url(source_url: str) -> str | None:
    """Resolve redirects through the guarded HTTP client before an ffmpeg fetch.

    ffmpeg bypasses the Python SSRF adapter. Resolve each redirect with that adapter first, then
    disable redirects in ffmpeg. HLS stays text-only until every manifest segment has this guard.
    """
    try:
        validate_source_url(source_url, resolve=True)
        with make_session() as session:
            response = session.get(
                source_url,
                headers={"Range": "bytes=0-0"},
                timeout=30,
                allow_redirects=True,
                stream=True,
            )
            response.raise_for_status()
            resolved = response.url
            response.close()
        validate_source_url(resolved, resolve=True)
    except Exception:  # noqa: BLE001 - a failed media preflight is a safe text-only outcome.
        return None
    if resolved.split("?", 1)[0].lower().endswith(".m3u8"):
        return None
    return resolved


def _probe_size(probe_binary: str, source_url: str) -> tuple[int, int] | None:
    try:
        completed = subprocess.run(
            [
                probe_binary,
                "-user_agent",
                USER_AGENT,
                "-protocol_whitelist",
                "file,https,tls,tcp",
                "-follow_redirects",
                "0",
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
    probe_binary: str,
    captions_available: bool,
    withheld: bool = False,
) -> bool:
    """Check the non-negotiable media prerequisites before admission or rendering."""
    if withheld or not captions_available or not source_url:
        return False
    resolved = _safe_media_url(source_url)
    if not resolved:
        return False
    size = _probe_size(probe_binary, resolved)
    return size is not None and min(size) >= MIN_VISIBLE_PX


def render_video_clip(
    ep: Episode,
    candidate: Mapping[str, Any],
    *,
    source_url: str,
    binary: str,
    probe_binary: str,
    storage,
    segments: list[dict[str, Any]],
    timeline_version: str,
    source_identity: str,
    crop_anchor: Mapping[str, float] | None = None,
    caption_override: str | None = None,
    profile: str = "vertical-9x16-square-pane-v1",
) -> dict[str, Any]:
    """Render an admitted quote, or return a stable failure status without uploading anything."""
    start = float(candidate.get("start") or 0)
    end = float(candidate.get("end") or 0)
    if end <= start or not source_url or not segments:
        return {"status": "video-unavailable", "reason": "source-or-caption-gate"}
    resolved_source = _safe_media_url(source_url)
    if not resolved_source:
        return {"status": "video-unavailable", "reason": "unsafe-or-streaming-source"}
    size = _probe_size(probe_binary, resolved_source)
    if size is None or min(size) < MIN_VISIBLE_PX:
        return {"status": "video-unavailable", "reason": "source-resolution"}
    key = video_clip_key(
        ep.uid or ep.guid,
        start,
        end,
        timeline_version,
        source_identity=source_identity,
        output_profile=profile,
    )
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
    # A single source cut preserves duration; inserted served-time spans are excluded above.
    if duration < MOMENTS_MIN_SECONDS or duration > MOMENTS_MAX_SECONDS:
        return {"status": "video-unavailable", "reason": "duration-gate"}
    with tempfile.TemporaryDirectory() as temporary:
        temporary_path = Path(temporary)
        ass_path = temporary_path / "captions.ass"
        output_path = temporary_path / "clip.mp4"
        try:
            _write_ass(segments, start, end, ass_path, caption_override=caption_override)
        except ValueError:
            return {"status": "video-unavailable", "reason": "caption-override-gate"}
        square_output = profile.startswith("square")
        canvas_height = MIN_VISIBLE_PX if square_output else 1280
        pane_y = 0 if square_output else 280
        watermark_y = canvas_height - 40
        detected_anchor = crop_anchor or _speaker_anchor(
            binary, resolved_source, source_start, duration, temporary_path
        )
        x_expr = "(iw-720)/2"
        y_expr = "(ih-720)/2"
        if detected_anchor:
            x_expr = f"max(0,min(iw-720,{float(detected_anchor.get('x', 0.5))}*iw-360))"
            y_expr = f"max(0,min(ih-720,{float(detected_anchor.get('y', 0.5))}*ih-360))"
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
            "-nostdin",
            "-loglevel",
            "error",
            "-user_agent",
            USER_AGENT,
            "-protocol_whitelist",
            "file,https,tls,tcp",
            "-follow_redirects",
            "0",
            "-ss",
            f"{source_start:.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            resolved_source,
            "-vf",
            filter_graph,
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-map",
            "0:a?",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            str(output_path),
        ]
        try:
            with HOST_LIMITER.slot(resolved_source):
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
        "framing": (
            "manual-anchor"
            if crop_anchor
            else "active-speaker"
            if detected_anchor
            else "stable-group-fallback"
        ),
        "watermark": "citymeetings.fyi",
        "reused": False,
    }


def _ffmpeg_path(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace(":", r"\:").replace("'", r"\'")


def _speaker_anchor(
    binary: str, source_url: str, start: float, duration: float, temporary_path: Path
) -> dict[str, float] | None:
    """Find the visible face with the strongest tracked mouth-region motion.

    This is intentionally conservative: a low-motion or closely tied track produces no anchor,
    keeping the stable group composition. OpenCV's bundled Haar cascade is version-pinned with the
    renderer dependency and never receives an untrusted remote URL directly.
    """
    try:
        import cv2
        import numpy
    except ImportError:
        return None
    frames_dir = temporary_path / "frames"
    frames_dir.mkdir()
    command = [
        binary,
        "-y",
        "-nostdin",
        "-loglevel",
        "error",
        "-user_agent",
        USER_AGENT,
        "-protocol_whitelist",
        "file,https,tls,tcp",
        "-follow_redirects",
        "0",
        "-ss",
        f"{start:.3f}",
        "-t",
        f"{min(duration, 20.0):.3f}",
        "-i",
        source_url,
        "-vf",
        "fps=2,scale=640:-2",
        "-frames:v",
        "40",
        str(frames_dir / "frame-%03d.jpg"),
    ]
    try:
        with HOST_LIMITER.slot(source_url):
            subprocess.run(command, check=True, capture_output=True, timeout=90)
    except (OSError, subprocess.SubprocessError):
        return None
    detector = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    if detector.empty():
        return None
    tracks: list[dict[str, Any]] = []
    for frame_path in sorted(frames_dir.glob("*.jpg")):
        image = cv2.imread(str(frame_path))
        if image is None:
            continue
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        faces = detector.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(48, 48))
        used: set[int] = set()
        for x, y, width, height in faces:
            anchor = ((x + width / 2) / image.shape[1], (y + height / 2) / image.shape[0])
            index = min(
                range(len(tracks)),
                key=lambda i: (tracks[i]["x"] - anchor[0]) ** 2 + (tracks[i]["y"] - anchor[1]) ** 2,
                default=-1,
            )
            distance = (
                (tracks[index]["x"] - anchor[0]) ** 2 + (tracks[index]["y"] - anchor[1]) ** 2
                if index >= 0
                else 1.0
            )
            if index < 0 or distance > 0.04 or index in used:
                tracks.append({"x": anchor[0], "y": anchor[1], "motion": 0.0, "frames": 0})
                index = len(tracks) - 1
            used.add(index)
            mouth = gray[y + int(height * 0.55) : y + height, x : x + width]
            previous = tracks[index].get("mouth")
            if previous is not None and previous.shape == mouth.shape:
                tracks[index]["motion"] += float(
                    numpy.mean(numpy.abs(mouth.astype(float) - previous))
                )
            tracks[index].update(
                {
                    "x": anchor[0],
                    "y": anchor[1],
                    "mouth": mouth,
                    "frames": tracks[index]["frames"] + 1,
                }
            )
    ranked = sorted(
        (track for track in tracks if track["frames"] >= 3),
        key=lambda track: track["motion"],
        reverse=True,
    )
    if not ranked or ranked[0]["motion"] <= 2.0:
        return None
    if len(ranked) > 1 and ranked[0]["motion"] < ranked[1]["motion"] * 1.2:
        return None
    return {"x": float(ranked[0]["x"]), "y": float(ranked[0]["y"])}


__all__ = [
    "MIN_VISIBLE_PX",
    "render_video_clip",
    "technical_video_gate",
    "video_clip_key",
]
