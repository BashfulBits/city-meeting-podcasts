"""Static presentation slides from meeting video, published as timed chapter images (review/61).

The pipeline has two ffmpeg passes per source and a set of pure helpers:

1. :func:`detect_command` streams the source video once, decoding keyframes only at 320 px gray,
   and prints ffmpeg ``freezedetect`` metadata. :func:`parse_freeze_intervals` turns that text
   into source-time :class:`Interval` values.
2. :func:`capture_command` seeks to one instant per kept interval and returns a single PNG frame.

Everything between those passes (edge trim, flat/low-detail checks, perceptual-hash recurrence and
duplicate merging, the per-episode cap, JPEG encoding) is pure and unit tested. The stage that
drives ffmpeg and storage lives in ``citypods/stages.py`` (``SlidesStage``).

Slides are stored in **source time** (``source_id`` + ``source_start``/``source_end``).
:func:`episode_served_slides` maps them onto the served clock with the same rules as
``citypods.chapters.episode_served_chapters`` at render time, so a timeline change never needs a
re-detection. :func:`chapters_with_slides` merges them into the Podcasting 2.0 chapters JSON.
Slides never reach the M4A chapter atoms, so ``audio_spec_hash`` is unaffected (review/61 D3).
"""

from __future__ import annotations

import hashlib
import io
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageFilter, ImageOps, ImageStat

from citypods.http import USER_AGENT
from citypods.models import Episode
from citypods.timeline import edl_duration, remap, source_to_served, timeline_digest

SLIDES_PIPELINE_VERSION = "1"
SLIDES_IMAGE_RECIPE = "jpeg-1280x720-pad-q82-v1"
SLIDES_RECIPE = f"{SLIDES_PIPELINE_VERSION}:{SLIDES_IMAGE_RECIPE}"

IMAGE_SIZE = (1280, 720)
_JPEG_START_QUALITY = 82
_JPEG_QUALITY_STEP = 8
_JPEG_MIN_QUALITY = 58
_ANALYSIS_WIDTH = 320
_DUPLICATE_HAMMING = 6
# Two near-identical consecutive frames merge into one slide only when the gap between them is at
# most this long (a cursor wiggle or a brief cut away). A slide shown again much later stays two
# slides, so the chapters keep the artwork reset for the video in between.
_MERGE_MAX_GAP_SECONDS = 30.0
# Conservative stand-ins when ffprobe reports no duration or bit rate, so an unknown source still
# counts against the per-run byte cap instead of reserving zero bytes.
_FALLBACK_DURATION_SECONDS = 4 * 3600
_FALLBACK_BIT_RATE = 8_000_000
_EDGE_PIXEL_THRESHOLD = 40
# ffmpeg protocols a remote video read may use. Same set as the audio lane minus ``file`` so a
# hostile HLS manifest cannot point a segment at a local path (SECURITY.md, ffmpeg whitelist).
PROTOCOL_WHITELIST = "crypto,data,http,https,tcp,tls"

# Every key the ``slides`` config block may carry (site defaults and per-city overrides).
DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "decode": "keyframes",
    "min_duration_seconds": 15.0,
    "noise_db": -50.0,
    "edge_trim_seconds": 120.0,
    "min_luma_stddev": 8.0,
    "min_edge_density": 0.004,
    "max_recurrence": 3,
    "max_slides_per_episode": 50,
    "max_image_kb": 150,
    "max_episodes_per_run": 20,
    "max_video_gb_per_run": 40.0,
    "exclude_dhashes": [],
}
_FLOAT_KEYS = frozenset(
    {
        "min_duration_seconds",
        "noise_db",
        "edge_trim_seconds",
        "min_luma_stddev",
        "min_edge_density",
        "max_video_gb_per_run",
    }
)
_INT_KEYS = frozenset(
    {"max_recurrence", "max_slides_per_episode", "max_image_kb", "max_episodes_per_run"}
)
_DECODE_MODES = frozenset({"keyframes", "fps1"})
_HEX64 = re.compile(r"^[0-9a-f]{16}$")


class SlidesConfigError(ValueError):
    """A ``slides`` config block has an unknown key or a value of the wrong type or range."""


def _check_block(block: Mapping[str, Any], where: str, *, allow_excludes: bool) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in block.items():
        if key not in DEFAULTS:
            raise SlidesConfigError(f"{where}: unknown slides key {key!r}")
        if key == "enabled":
            if not isinstance(value, bool):
                raise SlidesConfigError(f"{where}: slides.enabled must be true or false")
            out[key] = value
        elif key == "decode":
            if value not in _DECODE_MODES:
                raise SlidesConfigError(f"{where}: slides.decode must be one of {_DECODE_MODES}")
            out[key] = value
        elif key == "exclude_dhashes":
            if not allow_excludes:
                raise SlidesConfigError(f"{where}: exclude_dhashes is a per-city setting")
            if not isinstance(value, list) or not all(
                isinstance(item, str) and _HEX64.match(item) for item in value
            ):
                raise SlidesConfigError(
                    f"{where}: slides.exclude_dhashes must be a list of 16-hex-digit strings"
                )
            out[key] = list(value)
        elif value is None and key == "min_edge_density":
            out[key] = 0.0
        else:
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise SlidesConfigError(f"{where}: slides.{key} must be a number")
            if key in _INT_KEYS:
                if int(value) != value or value < 1:
                    raise SlidesConfigError(f"{where}: slides.{key} must be a positive integer")
                out[key] = int(value)
            else:
                if key != "noise_db" and value < 0:
                    raise SlidesConfigError(f"{where}: slides.{key} must not be negative")
                out[key] = float(value)
    return out


def effective_config(
    site_block: Mapping[str, Any] | None, city_block: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Merge site defaults and a city's override over :data:`DEFAULTS`.

    On by default (review/61 P4): the result is enabled unless the site kill switch or the city
    sets ``enabled: false``. Raises :class:`SlidesConfigError` for a malformed block."""
    if site_block is not None and not isinstance(site_block, Mapping):
        raise SlidesConfigError("site_config: slides must be a mapping")
    if city_block is not None and not isinstance(city_block, Mapping):
        raise SlidesConfigError("city config: slides must be a mapping")
    site = _check_block(site_block or {}, "site_config", allow_excludes=False)
    city = _check_block(city_block or {}, "city config", allow_excludes=True)
    merged = {**DEFAULTS, **site, **city}
    merged["enabled"] = bool(site.get("enabled", True)) and bool(city.get("enabled", True))
    return merged


def detection_fingerprint(config: Mapping[str, Any]) -> dict[str, Any]:
    """The config values that change detection output (excludes run caps and the switch)."""
    keys = (
        "decode",
        "min_duration_seconds",
        "noise_db",
        "edge_trim_seconds",
        "min_luma_stddev",
        "min_edge_density",
        "max_recurrence",
        "max_slides_per_episode",
        "max_image_kb",
        "exclude_dhashes",
    )
    return {key: config.get(key, DEFAULTS[key]) for key in keys}


# ---------------------------------------------------------------------------
# ffmpeg commands and parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Interval:
    """A static span of one source, in that source's seconds."""

    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def _remote_args(url: str) -> list[str]:
    if url.startswith(("http://", "https://")):
        return ["-user_agent", USER_AGENT, "-protocol_whitelist", PROTOCOL_WHITELIST]
    return []


def probe_command(probe_binary: str, url: str) -> list[str]:
    """ffprobe the container duration/bit rate and the first video stream's size."""
    return [
        probe_binary,
        *_remote_args(url),
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "format=duration,bit_rate:stream=codec_type,width,height",
        "-of",
        "json",
        url,
    ]


def detect_command(binary: str, url: str, config: Mapping[str, Any]) -> list[str]:
    """One streamed read of ``url`` that prints ``freezedetect`` metadata to stdout."""
    skip = ["-skip_frame", "nokey"] if config.get("decode", "keyframes") == "keyframes" else []
    fps = "fps=1," if config.get("decode") == "fps1" else ""
    noise = float(config.get("noise_db", DEFAULTS["noise_db"]))
    floor = float(config.get("min_duration_seconds", DEFAULTS["min_duration_seconds"]))
    return [
        binary,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        *_remote_args(url),
        *skip,
        "-i",
        url,
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-dn",
        "-vf",
        f"{fps}scale={_ANALYSIS_WIDTH}:-2,format=gray,"
        f"freezedetect=n={noise:g}dB:d={floor:g},metadata=print:file=-",
        "-f",
        "null",
        "-",
    ]


def capture_command(binary: str, url: str, at: float) -> list[str]:
    """Seek to ``at`` seconds and write one PNG frame to stdout."""
    return [
        binary,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        *_remote_args(url),
        "-ss",
        f"{max(0.0, at):.3f}",
        "-i",
        url,
        "-map",
        "0:v:0",
        "-frames:v",
        "1",
        "-f",
        "image2pipe",
        "-vcodec",
        "png",
        "-",
    ]


_FREEZE_RE = re.compile(r"lavfi\.freezedetect\.freeze_(start|end)=(-?[0-9.]+(?:e[-+]?\d+)?)")


def parse_freeze_intervals(text: str, duration: float | None = None) -> list[Interval]:
    """Parse ``freezedetect`` ``metadata=print`` output into ordered intervals.

    A trailing ``freeze_start`` with no ``freeze_end`` (the video ends while still frozen) closes
    at ``duration`` when known and is dropped otherwise. Malformed or reversed pairs are skipped.
    """
    intervals: list[Interval] = []
    open_start: float | None = None
    for kind, raw in _FREEZE_RE.findall(text or ""):
        value = float(raw)
        if kind == "start":
            open_start = value
        elif open_start is not None:
            if value > open_start:
                intervals.append(Interval(max(0.0, open_start), value))
            open_start = None
    if open_start is not None and duration is not None and duration > open_start:
        intervals.append(Interval(max(0.0, open_start), float(duration)))
    return intervals


def settle_time(interval: Interval) -> float:
    """The capture instant: a few seconds in, past slide-in animations and fades."""
    return interval.start + min(3.0, interval.duration / 3.0)


def edge_trim(
    intervals: Sequence[Interval],
    *,
    duration: float | None,
    edge_seconds: float,
    first_chapter_start: float | None,
) -> tuple[list[Interval], int]:
    """Drop holding/end slates: intervals that start in the leading edge before the first agenda
    chapter, or start inside the trailing edge. Returns ``(kept, rejected_count)``."""
    kept: list[Interval] = []
    rejected = 0
    for interval in intervals:
        leading = interval.start < edge_seconds and (
            first_chapter_start is None or interval.start < first_chapter_start
        )
        trailing = duration is not None and interval.start > duration - edge_seconds
        if leading or trailing:
            rejected += 1
        else:
            kept.append(interval)
    return kept, rejected


# ---------------------------------------------------------------------------
# Pixel checks and encoding
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FrameMetrics:
    luma_stddev: float
    edge_density: float
    dhash: int


def _analysis_image(image: Image.Image) -> Image.Image:
    gray = image.convert("L")
    width, height = gray.size
    if width > _ANALYSIS_WIDTH:
        gray = gray.resize((_ANALYSIS_WIDTH, max(1, round(height * _ANALYSIS_WIDTH / width))))
    return gray


def dhash(image: Image.Image) -> int:
    """64-bit difference hash (9x8 gray, horizontal gradient)."""
    small = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    pixels = list(small.tobytes())
    value = 0
    for row in range(8):
        for col in range(8):
            left = pixels[row * 9 + col]
            right = pixels[row * 9 + col + 1]
            value = (value << 1) | (1 if left > right else 0)
    return value


def dhash_hex(value: int) -> str:
    return f"{value:016x}"


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def frame_metrics(image: Image.Image) -> FrameMetrics:
    gray = _analysis_image(image)
    stddev = float(ImageStat.Stat(gray).stddev[0])
    edges = gray.filter(ImageFilter.FIND_EDGES)
    # FIND_EDGES leaves a 1 px frame of the source; ignore the border so a flat frame scores 0.
    width, height = edges.size
    if width > 2 and height > 2:
        edges = edges.crop((1, 1, width - 1, height - 1))
    histogram = edges.histogram()
    total = sum(histogram) or 1
    density = sum(histogram[_EDGE_PIXEL_THRESHOLD:]) / total
    return FrameMetrics(luma_stddev=stddev, edge_density=density, dhash=dhash(gray))


def pixel_rejection(metrics: FrameMetrics, config: Mapping[str, Any]) -> str | None:
    """The reason a captured frame is not a slide, or ``None`` to keep it."""
    if metrics.luma_stddev < float(config.get("min_luma_stddev", DEFAULTS["min_luma_stddev"])):
        return "flat"
    if metrics.edge_density < float(config.get("min_edge_density") or 0.0):
        return "low_detail"
    for excluded in config.get("exclude_dhashes") or []:
        if hamming(metrics.dhash, int(excluded, 16)) <= _DUPLICATE_HAMMING:
            return "excluded"
    return None


def encode_slide(image: Image.Image, *, max_kb: int = 150) -> bytes:
    """Pad to 1280x720 (no crop) and encode a progressive JPEG under ``max_kb`` when possible."""
    rgb = ImageOps.pad(image.convert("RGB"), IMAGE_SIZE, color=(0, 0, 0))
    quality = _JPEG_START_QUALITY
    while True:
        buffer = io.BytesIO()
        rgb.save(buffer, "JPEG", quality=quality, optimize=True, progressive=True)
        data = buffer.getvalue()
        if len(data) <= max_kb * 1024 or quality - _JPEG_QUALITY_STEP < _JPEG_MIN_QUALITY:
            return data
        quality -= _JPEG_QUALITY_STEP


def slide_key(uid: str, data: bytes) -> str:
    """Content-addressed storage key for one encoded slide."""
    return f"slides/{uid}/{hashlib.sha256(data).hexdigest()[:24]}.jpg"


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    """A captured static interval with its frame metrics, before selection.

    The stage keeps only the encoded JPEG (``data``, at most ``max_image_kb``), never the decoded
    native-resolution frame, so the candidates of a long meeting stay small in memory."""

    source_id: str
    interval: Interval
    metrics: FrameMetrics
    image: Image.Image | None = None
    data: bytes | None = None


def select_slides(
    candidates: Sequence[Candidate], *, max_recurrence: int, max_slides: int
) -> tuple[list[Candidate], dict[str, int], bool]:
    """Recurrence filter, consecutive-duplicate merge, then the per-episode cap.

    Returns ``(kept, rejected_by_reason, capped)``. A frame whose hash recurs in at least
    ``max_recurrence`` intervals is a slate or room shot, not a slide. Consecutive near-identical
    frames in the same source merge into one slide spanning both intervals when the gap between
    them is at most ``_MERGE_MAX_GAP_SECONDS``."""
    rejected: dict[str, int] = {}
    ordered = sorted(candidates, key=lambda c: (c.source_id, c.interval.start))
    recurring: set[int] = set()
    for index, candidate in enumerate(ordered):
        matches = sum(
            1
            for other in ordered
            if hamming(candidate.metrics.dhash, other.metrics.dhash) <= _DUPLICATE_HAMMING
        )
        if matches >= max_recurrence:
            recurring.add(index)
    survivors: list[Candidate] = []
    for index, candidate in enumerate(ordered):
        if index in recurring:
            rejected["recurrence"] = rejected.get("recurrence", 0) + 1
            continue
        previous = survivors[-1] if survivors else None
        if (
            previous is not None
            and previous.source_id == candidate.source_id
            and candidate.interval.start - previous.interval.end <= _MERGE_MAX_GAP_SECONDS
            and hamming(previous.metrics.dhash, candidate.metrics.dhash) <= _DUPLICATE_HAMMING
        ):
            previous.interval = Interval(previous.interval.start, candidate.interval.end)
            rejected["duplicate"] = rejected.get("duplicate", 0) + 1
            continue
        survivors.append(candidate)
    capped = len(survivors) > max_slides
    return survivors[:max_slides], rejected, capped


# ---------------------------------------------------------------------------
# Served clock and the chapters JSON merge
# ---------------------------------------------------------------------------


def ready_slide_items(ep: Episode) -> list[dict]:
    """The stored slide items of an episode whose slides block is ready, else ``[]``."""
    block = ep.slides if isinstance(ep.slides, dict) else {}
    if block.get("status") != "ready":
        return []
    items = block.get("items")
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            continue
        try:
            start = float(item["source_start"])
            end = float(item["source_end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end > start >= 0:
            out.append({**item, "source_start": start, "source_end": end})
    return out


def episode_served_slides(ep: Episode) -> list[dict]:
    """Served-time ``{"start", "end", "url"}`` slides, using the same clock as
    :func:`citypods.chapters.episode_served_chapters`: source time when the timeline is absent or
    identity, otherwise each slide is remapped through its own source's kept spans. Slides that
    fall in a removed span, or shrink below half the detection floor, are dropped."""
    items = ready_slide_items(ep)
    if not items:
        return []
    min_served = float(DEFAULTS["min_duration_seconds"]) / 2
    if ep.timeline is None or timeline_digest(ep.timeline, ep.sources) == "":
        served = [
            {"start": item["source_start"], "end": item["source_end"], "url": item["url"]}
            for item in items
        ]
    else:
        served = []
        by_source: dict[str, list[dict]] = {}
        for item in items:
            by_source.setdefault(str(item.get("source_id") or "s0"), []).append(item)
        for source_id, group in by_source.items():
            # A slide shown only while the served audio was cut away never reaches the listener.
            # Keep it when either end is on the served clock; remap snaps the cut side.
            visible = [
                item
                for item in group
                if source_to_served(ep.timeline, source_id, item["source_start"]) is not None
                or source_to_served(ep.timeline, source_id, item["source_end"]) is not None
            ]
            mapped = remap(
                ep.timeline,
                [
                    {"start": item["source_start"], "end": item["source_end"], "url": item["url"]}
                    for item in visible
                ],
                source_id=source_id,
                snap_cut_starts=True,
                clamp_to=edl_duration(ep.timeline),
            )
            served.extend(row for row in mapped if row.get("end") is not None)
    return sorted(
        (row for row in served if float(row["end"]) - float(row["start"]) >= min_served),
        key=lambda row: float(row["start"]),
    )


_AGENDA_MATCH_SECONDS = 2
_RESET_GAP_SECONDS = 5
_FALLBACK_TITLE = "Presentation"


def chapters_with_slides(
    chapters: Iterable[Mapping[str, Any]], slides: Iterable[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Podcasting 2.0 chapter entries for agenda ``chapters`` plus served ``slides``.

    - A slide starting within 2 s of an agenda chapter puts its ``img`` on that chapter.
    - Otherwise it adds a ``"toc": false`` entry titled after the enclosing agenda chapter.
    - When a slide ends more than 5 s before the next chapter or slide, an image-less
      ``"toc": false`` entry follows so apps drop back to the episode artwork.
    - Entries are whole seconds; a slide entry landing on an occupied second is dropped.
    """
    agenda = sorted(
        (
            {"startTime": round(float(c["start"])), "title": str(c.get("title") or "")}
            for c in chapters
        ),
        key=lambda entry: entry["startTime"],
    )
    slide_rows = sorted(
        (
            (round(float(s["start"])), round(float(s["end"])), str(s["url"]))
            for s in slides
            if s.get("url")
        ),
        key=lambda row: row[0],
    )
    entries: list[dict[str, Any]] = [dict(entry) for entry in agenda]
    occupied = {entry["startTime"] for entry in entries}

    def enclosing_title(at: int) -> str:
        title = _FALLBACK_TITLE
        for entry in agenda:
            if entry["startTime"] <= at + _AGENDA_MATCH_SECONDS:
                title = entry["title"] or _FALLBACK_TITLE
            else:
                break
        return title

    for index, (start, end, url) in enumerate(slide_rows):
        if end <= start:
            continue
        match = next(
            (
                entry
                for entry in entries
                if entry.get("toc", True)
                and "img" not in entry
                and abs(entry["startTime"] - start) <= _AGENDA_MATCH_SECONDS
            ),
            None,
        )
        if match is not None:
            match["img"] = url
        elif start in occupied:
            continue
        else:
            entries.append(
                {
                    "startTime": start,
                    "endTime": end,
                    "title": enclosing_title(start),
                    "img": url,
                    "toc": False,
                }
            )
            occupied.add(start)
        later_starts = [entry["startTime"] for entry in agenda if entry["startTime"] > start]
        later_starts += [row[0] for row in slide_rows[index + 1 :]]
        next_start = min(later_starts) if later_starts else None
        if (next_start is None or end + _RESET_GAP_SECONDS < next_start) and end not in occupied:
            entries.append({"startTime": end, "title": enclosing_title(end), "toc": False})
            occupied.add(end)
    return sorted(entries, key=lambda entry: entry["startTime"])


# ---------------------------------------------------------------------------
# Running ffmpeg (injectable for tests) and the per-run budget
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeResult:
    duration: float | None
    bit_rate: int | None
    has_video: bool

    @property
    def estimated_bytes(self) -> int:
        """Bytes a full read streams. A missing duration or bit rate uses a conservative
        fallback, never zero, so the per-run byte cap still applies."""
        duration = self.duration or _FALLBACK_DURATION_SECONDS
        bit_rate = self.bit_rate or _FALLBACK_BIT_RATE
        return int(duration * bit_rate / 8)


def parse_probe(stdout: str) -> ProbeResult:
    """Parse :func:`probe_command` JSON. Missing or malformed fields become ``None``."""
    import json

    try:
        data = json.loads(stdout or "{}")
    except ValueError:
        return ProbeResult(None, None, False)
    fmt = data.get("format") if isinstance(data, dict) else None
    fmt = fmt if isinstance(fmt, dict) else {}

    def number(value: Any) -> float | None:
        try:
            out = float(value)
        except (TypeError, ValueError):
            return None
        return out if out > 0 else None

    streams = data.get("streams") if isinstance(data, dict) else None
    has_video = any(
        isinstance(stream, dict) and stream.get("codec_type", "video") == "video"
        for stream in (streams or [])
    )
    rate = number(fmt.get("bit_rate"))
    return ProbeResult(number(fmt.get("duration")), int(rate) if rate else None, has_video)


class SlidesRunBudget:
    """Per-run caps shared by every worker thread: episodes started and video bytes streamed."""

    def __init__(self, *, max_episodes: int, max_bytes: int) -> None:
        import threading

        self.max_episodes = max_episodes
        self.max_bytes = max_bytes
        self.episodes = 0
        self.bytes = 0
        self._lock = threading.Lock()

    def admit_episode(self) -> bool:
        with self._lock:
            if self.episodes >= self.max_episodes:
                return False
            self.episodes += 1
            return True

    def admit_bytes(self, estimated: int) -> bool:
        """Reserve ``estimated`` streamed bytes (one episode's total). The first reservation of a
        run is always admitted so a single oversized meeting cannot block the lane forever."""
        with self._lock:
            if self.bytes and self.bytes + estimated > self.max_bytes:
                return False
            self.bytes += estimated
            return True

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> SlidesRunBudget:
        return cls(
            max_episodes=int(config.get("max_episodes_per_run", DEFAULTS["max_episodes_per_run"])),
            max_bytes=int(
                float(config.get("max_video_gb_per_run", DEFAULTS["max_video_gb_per_run"]))
                * 1024**3
            ),
        )


def decode_png(data: bytes) -> Image.Image | None:
    if not data:
        return None
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except (OSError, ValueError, Image.DecompressionBombError):
        return None
    return image
