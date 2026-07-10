"""Concat planner for legacy multi-segment Swagit meetings (#122).

Older Swagit meetings whose ``/download`` redirect is keyless expose multiple direct-MP4 segment
URLs (``dfile`` fields in the video-page inline JSON).  Each segment is one agenda item; together
they form one full meeting.  :class:`SwagitConcatPlanner` detects these, probes each segment's
duration, and emits a multi-source :class:`~citypods.timeline.Timeline` that the generalized
encoder (INFRA-3) renders via its existing ``filter_complex`` concat path.

The planner also builds served-time chapter markers directly from the segment titles and
cumulative offsets, bypassing the per-segment timestamp ambiguity that would arise if
``RemapStage`` tried to remap source-time ``playerControl`` anchors across multiple sources.

Chapter limitation (TODO #122-v2): sub-chapter granularity within a segment (e.g. individual
motions timestamped inside a single agenda-item video) is not captured here.  The planner sets
one chapter per segment at its served-time start.  Richer per-segment chapter scraping requires
knowing which ``playerControl`` anchors belong to which source, which in turn requires inspecting
a live multi-segment Swagit page to determine whether their ``data-ts`` values are per-segment
or cumulative.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from fractions import Fraction

from citypods.http import HOST_LIMITER, USER_AGENT, StopRequested
from citypods.media import record_materialize_failure
from citypods.models import City, Episode
from citypods.provider_leases import DISTRIBUTED_PROVIDER_LEASES
from citypods.security import SecurityError, validate_source_url
from citypods.timeline import Segment, SourceMedia, Timeline


def _probe_duration_url(
    url: str,
    ffprobe: str = "ffprobe",
    timeout: float | None = None,
    *,
    stop: Callable[[], bool] | None = None,
) -> float | None:
    """Return a remote URL's stream-clock duration in seconds via ffprobe, or ``None`` on failure.

    Raises :class:`~citypods.http.StopRequested` (rather than returning ``None``) if the run's
    wall-clock budget expires while queued on the rate-limit/lease wait — the caller distinguishes
    that from a genuine probe failure so it doesn't record a materialize-failure backoff for it.
    """
    # This is the one ffprobe call in the tree that previously had no SSRF gate at all — url is
    # a page-scraped Swagit ``dfile`` URL, not implicitly trusted (MR-CP-01/C3). Treat a blocked
    # URL the same as any other probe failure (the caller records a materialize-failure and defers).
    try:
        validate_source_url(url, resolve=True)
    except SecurityError:
        return None
    # Keep the acquisition order identical to every other media subprocess path (#342). Acquiring
    # the distributed lease first can deadlock against a source-cache worker that already holds the
    # process-local slot while waiting for that lease.
    with HOST_LIMITER.slot(url, stop=stop), DISTRIBUTED_PROVIDER_LEASES.slots([url], stop=stop):
        try:
            out = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-user_agent",
                    USER_AGENT,
                    "-select_streams",
                    "a:0",
                    "-show_entries",
                    "format=duration:stream=duration_ts,time_base,duration",
                    "-of",
                    "json",
                    url,
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            ).stdout.strip()
            if not out:
                return None
            # Unit tests historically patched ffprobe with a plain duration line. Keep accepting
            # that while production uses JSON so stream duration_ts wins over format.duration.
            if not out.startswith("{"):
                return float(out)
            data = json.loads(out)
            streams = data.get("streams") or []
            if streams and isinstance(streams[0], dict):
                stream = streams[0]
                duration_ts = stream.get("duration_ts")
                time_base = stream.get("time_base")
                if duration_ts is not None and time_base:
                    duration = int(duration_ts) * Fraction(str(time_base))
                    if duration > 0:
                        return float(duration)
                stream_duration = stream.get("duration")
                if stream_duration:
                    duration = float(stream_duration)
                    return duration if duration > 0 else None
            raw = (data.get("format") or {}).get("duration")
            if raw:
                duration = float(raw)
                return duration if duration > 0 else None
            return None
        except (
            subprocess.TimeoutExpired,
            subprocess.CalledProcessError,
            OSError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ):
            return None


class SwagitConcatPlanner:
    """TimelinePlanner that materializes legacy multi-segment Swagit meetings (#122).

    Registered before :class:`~citypods.silence.SilencePlanner` in
    :class:`~citypods.stages.TimelineStage`.  ``SilencePlanner`` skips any episode with
    ``len(ep.sources) > 1`` so the two planners don't conflict.

    Detection is Swagit-specific (keyless ``/download`` redirect + ``dfile`` page scrape).  All
    downstream rendering — ``filter_complex``, :func:`~citypods.media.materialize_audio`, the clip
    service — is provider-agnostic and already handles multi-source timelines.
    """

    name = "swagit-concat"
    version = "1"

    def plan(
        self,
        provider,
        city: City,
        ep: Episode,
        ctx,
        current: Timeline | None,
    ) -> Timeline | None:
        """Return a concat Timeline for keyless multi-segment Swagit meetings, or ``None``.

        Returns ``None`` (pass-through) when:
          - not a Swagit source;
          - not an HLS episode (nothing to re-host);
          - ffmpeg/ffprobe not installed (e.g. PR-preview CI);
          - the meeting is modern (keyed ``/download``) or has ≤1 segment;
          - any duration probe fails (deferred; backoff handles retry).

        On success, mutates ``ep.sources`` and ``ep.chapters``/``ep.chapters_basis`` in place
        and returns the concat Timeline.  :class:`~citypods.stages.TimelineStage` stamps the
        planner-set version onto it before persisting.
        """
        if city.provider != "swagit":
            return None
        if ep.media_kind != "hls":
            return None

        ffmpeg_binary = getattr(ctx.ffmpeg, "binary", "ffmpeg")
        if not shutil.which(ffmpeg_binary):
            return None

        try:
            seg_objs = provider.fetch_segment_objects(ep, city.source)
        except Exception as exc:  # noqa: BLE001
            record_materialize_failure(ep, "concat-fetch")
            print(f"[enrich] concat planner fetch failed for {ep.uid or ep.guid}: {exc}")
            return None

        if seg_objs is None or len(seg_objs) <= 1:
            return None  # modern meeting or single-segment: normal path handles it

        ffprobe = (
            "ffprobe" if ffmpeg_binary == "ffmpeg" else ffmpeg_binary.replace("ffmpeg", "ffprobe")
        )
        timeout = getattr(ctx.ffmpeg, "timeout_seconds", None)

        durations: list[float] = []
        for i, (url, _) in enumerate(seg_objs):
            try:
                dur = _probe_duration_url(url, ffprobe, timeout=timeout, stop=ctx.stop)
            except StopRequested:
                # The run's wall-clock budget expired while queued, not a probe failure — defer
                # without recording a backoff so this isn't mistaken for a broken source (#120).
                return None
            if dur is None:
                # Record which segment failed so backoff/diagnostics target the actual failure
                # rather than a generic "concat deferred" — segment URLs/CDN behavior can differ
                # within one meeting (#370-style routing issues).
                record_materialize_failure(ep, f"concat-probe:s{i}")
                return None  # probe failed → defer; backoff handles retry
            durations.append(dur)

        sources = [
            SourceMedia(
                id=f"s{i}",
                provider="swagit",
                ref=url,
                media_kind="direct",
                duration=dur,
                watch_url=(ep.links or {}).get("canonical_video"),
                duration_basis="stream-sample",
            )
            for i, ((url, _), dur) in enumerate(zip(seg_objs, durations, strict=True))
        ]
        ep.sources = sources

        # Build served-time chapters: each segment is itself an agenda item whose title
        # starts at the cumulative offset where that segment's audio begins.
        chapters = []
        offset = 0.0
        for (_, title), dur in zip(seg_objs, durations, strict=True):
            if title:
                chapters.append({"start": offset, "end": offset + dur, "title": title})
            offset += dur
        if chapters:
            ep.chapters = chapters
            ep.chapters_basis = "served"  # already in served time; RemapStage skips

        # Build the concat Timeline: N consecutive full-source segments.
        segs: list[Segment] = []
        offset = 0.0
        for src, dur in zip(sources, durations, strict=True):
            segs.append(
                Segment(
                    served_start=offset,
                    served_end=offset + dur,
                    kind="source",
                    source_id=src.id,
                    source_start=0.0,
                    source_end=dur,
                )
            )
            offset += dur

        return Timeline(version="", segments=tuple(segs))
