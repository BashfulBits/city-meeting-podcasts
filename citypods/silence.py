"""Silence-trim TimelinePlanner (#111).

Runs ``ffmpeg silencedetect`` on the source URL, builds a ``Timeline`` that keeps only the
non-silent spans, and returns it for ``TimelineStage`` to persist.  Downstream ``AudioStage``
renders the trimmed EDL; ``RemapStage`` re-aligns any provider chapters.

Pure functions (``parse_silences``, ``build_silence_timeline``) are I/O-free and fully
testable offline.  ``detect_silences`` owns the subprocess call.  ``SilencePlanner`` is the
``TimelinePlanner`` plugin that wires them together inside the stage pipeline.

Re-encoding the catalog
-----------------------
To force a full catalog re-trim after changing detection parameters, bump
``SilencePlanner.version``.  ``TimelineStage`` detects the stale signature → re-plans all
episodes → new EDL digests → ``AudioStage`` re-encodes.  Do NOT use ``rebuild-audio`` for
this: it re-encodes using the *existing* timeline, which would still be the identity EDL.
"""

from __future__ import annotations

import re
import shutil
import subprocess

from citypods.availability import is_effectively_silent
from citypods.http import StopRequested
from citypods.timeline import Segment, Timeline, identity_timeline

# ---------------------------------------------------------------------------
# Pure helpers (no I/O)
# ---------------------------------------------------------------------------


def parse_silences(stderr: str) -> list[tuple[float, float]]:
    """Parse ``ffmpeg silencedetect`` stderr into ``[(start, end), ...]``.

    Matches lines like::

        [silencedetect @ ...] silence_start: 0
        [silencedetect @ ...] silence_end: 2.347 | silence_duration: 2.347
    """
    starts: list[float] = []
    pairs: list[tuple[float, float]] = []
    for line in stderr.splitlines():
        m = re.search(r"silence_start:\s*([\d.]+)", line)
        if m:
            starts.append(float(m.group(1)))
            continue
        m = re.search(r"silence_end:\s*([\d.]+)", line)
        if m and starts:
            pairs.append((starts.pop(), float(m.group(1))))
    return pairs


def _parse_ffmpeg_duration(stderr: str) -> float | None:
    """Parse ``Duration: HH:MM:SS.ss`` from ffmpeg's probe header."""
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", stderr)
    if not m:
        return None
    h, mn, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return h * 3600 + mn * 60 + s


def build_silence_timeline(
    source_id: str,
    source_duration: float,
    silences: list[tuple[float, float]],
    *,
    lead_trail_min: float = 1.0,
    mid_min: float = 10.0,
) -> Timeline | None:
    """Build a trimmed Timeline from detected silence spans.

    Removes:
    - Leading silence (before first audio) if ≥ ``lead_trail_min`` seconds.
    - Trailing silence (after last audio) if ≥ ``lead_trail_min`` seconds.
    - Mid-meeting gaps if ≥ ``mid_min`` seconds.

    Returns ``None`` when nothing qualifies for removal (caller should stamp an identity
    timeline so the episode is not re-examined next run).

    The returned ``Timeline`` has ``version="identity"`` set as a placeholder; the caller
    (``TimelinePlanner.plan``) must not rely on that value — ``TimelineStage`` overwrites
    ``version`` with the planner-set signature before persisting.
    """
    if not silences and source_duration <= 0:
        return None

    # Determine which silences to cut.
    cuts: list[tuple[float, float]] = []
    for start, end in silences:
        duration = end - start
        is_leading = start <= 0.001
        is_trailing = end >= source_duration - 0.001
        if is_leading or is_trailing:
            if duration >= lead_trail_min:
                cuts.append((start, end))
        else:
            if duration >= mid_min:
                cuts.append((start, end))

    if not cuts:
        return None

    # Build keep-spans by inverting the cuts.
    keep: list[tuple[float, float]] = []
    cursor = 0.0
    for cut_start, cut_end in sorted(cuts):
        if cut_start > cursor + 0.001:
            keep.append((cursor, cut_start))
        cursor = cut_end
    if cursor < source_duration - 0.001:
        keep.append((cursor, source_duration))

    if not keep:
        return None

    # Assign served-time coordinates (left-packed, no gaps).
    segments: list[Segment] = []
    served_cursor = 0.0
    for src_start, src_end in keep:
        span = src_end - src_start
        segments.append(
            Segment(
                served_start=served_cursor,
                served_end=served_cursor + span,
                kind="source",
                source_id=source_id,
                source_start=src_start,
                source_end=src_end,
            )
        )
        served_cursor += span

    return Timeline(version="identity", segments=tuple(segments))


def is_degenerate_served_duration(
    tl: Timeline,
    source_duration: float,
    *,
    min_seconds: float = 5.0,
    min_fraction: float = 0.02,
) -> bool:
    """True when a trimmed Timeline's kept (served) span is implausibly short.

    Guards against hosting a near-empty recording (observed: 0.005s/0.010s outputs) when
    ``source_duration`` itself is garbage — e.g. ``detect_silences`` read a truncated/throttled
    fetch and flagged nearly the whole thing as silent. The floor is the larger of an absolute
    minimum and a fraction of the (claimed) source duration, so a real short meeting near the
    absolute floor is not rejected outright while a near-total wipeout is.
    """
    served_total = tl.segments[-1].served_end if tl.segments else 0.0
    floor = max(min_seconds, source_duration * min_fraction)
    return served_total < floor


# ---------------------------------------------------------------------------
# Durable media-availability detection (H16 PR3) — rides the existing decode pass
# ---------------------------------------------------------------------------


def _availability_profile(ctx) -> str:
    """The detector profile string folded into the availability fingerprint. Changing any silence
    threshold re-fingerprints, which re-opens stored verdicts (review/12 PR3)."""
    return (
        f"noise={ctx.silence_noise_db}dB;"
        f"min_s={ctx.silence_min_served_seconds};"
        f"min_f={ctx.silence_min_served_fraction}"
    )


def _stamp_availability(ep, obs_kind: str, profile: str, *, reason: str = "") -> None:
    """Fold one media observation into the episode's durable availability verdict (H16 PR3).

    ``classify`` returns ``None`` only for a transport failure with no prior verdict; in that case
    the episode is left unclassified so the normal backoff/circuit machinery keeps owning retries.
    """
    from citypods import availability

    fingerprint = availability.source_fingerprint(ep, profile)
    obs = availability.Observation(
        kind=obs_kind, fingerprint=fingerprint, profile=profile, reason=reason
    )
    verdict = availability.classify(ep.media_availability, obs)
    if verdict is not None:
        ep.media_availability = verdict


# ---------------------------------------------------------------------------
# I/O: run ffmpeg silencedetect
# ---------------------------------------------------------------------------


def detect_silences(
    url: str,
    ffmpeg_binary: str = "ffmpeg",
    noise_db: float = -40.0,
    min_duration_s: float = 1.0,
    timeout: float | None = None,
    threads: int | None = None,
) -> tuple[list[tuple[float, float]], float | None]:
    """Run ``ffmpeg silencedetect`` on ``url``; return ``(silences, source_duration)``.

    ``min_duration_s`` is passed to the filter as the minimum silence length to detect.
    Use a short value (e.g. 1.0) to capture all candidates; ``build_silence_timeline``
    applies the per-category thresholds (leading/trailing vs mid-meeting) when filtering.

    ``source_duration`` is parsed from the same stderr output — no extra ffprobe call.
    Returns ``([], None)`` on any subprocess or parse error.

    ``threads``, if given, pins ffmpeg's decode/filter thread count the same way
    ``CommandFfmpeg`` pins its encode passes — ffmpeg otherwise defaults to "all cores",
    and an unpinned silencedetect pass running alongside ``AudioStage``'s
    ``NativeWorkGate``-budgeted encodes would oversubscribe the CPU the gate exists to bound.
    """
    cmd = [
        ffmpeg_binary,
        "-y",
        "-loglevel",
        "info",
        "-protocol_whitelist",
        "file,crypto,data,http,https,tcp,tls",
        *(["-threads", str(threads)] if threads is not None else []),
        "-i",
        url,
        "-vn",
        "-af",
        f"silencedetect=noise={noise_db}dB:d={min_duration_s}",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            return [], None
        # ffmpeg writes both its probe header and filter output to stderr.
        stderr = result.stderr
        silences = parse_silences(stderr)
        duration = _parse_ffmpeg_duration(stderr)
        return silences, duration
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, OSError):
        return [], None


# ---------------------------------------------------------------------------
# TimelinePlanner plugin
# ---------------------------------------------------------------------------


class SilencePlanner:
    """TimelinePlanner that trims silence from hosted-audio episodes.

    Registered in ``TimelineStage`` via ``enrich_stages()`` / ``default_stages()``.
    Config flows through ``StageContext`` (``trim_silence``, ``silence_noise_db``,
    ``silence_lead_trail_min_s``, ``silence_mid_min_s``).

    Per-feed opt-out: set ``extra: {trim_silence: false}`` in the feed YAML.

    To force a full catalog re-trim after changing detection parameters, bump ``version``
    here.  ``TimelineStage`` detects the stale signature and re-plans all episodes.

    ``version`` bumped 1->2 (audio workflow review, 2026-06) to re-examine episodes that may
    already carry a degenerate stamped timeline from before ``is_degenerate_served_duration``
    existed — a one-time full-catalog re-trim, bounded as always by the enrich wall-clock window.
    """

    name = "silence"
    version = "2"

    def plan(self, provider, city, ep, ctx, current: Timeline | None) -> Timeline | None:
        from citypods.providers.base import MEDIA_DEAD, MediaUnavailable, ProviderError

        # Gate: feature disabled globally or opted out per feed.
        if not city.extra.get("trim_silence", ctx.trim_silence):
            return None

        # Only plan for episodes whose audio we re-host (HLS always; direct only if extract_audio).
        if not (ep.media_kind == "hls" or city.extract_audio):
            return None

        # Multi-segment concat episodes are owned by SwagitConcatPlanner; silence-trimming
        # a single-source URL when the episode spans N sources would only trim the first segment.
        if ep.sources and len(ep.sources) > 1:
            return None

        ffmpeg_binary = getattr(ctx.ffmpeg, "binary", "ffmpeg")
        profile = _availability_profile(ctx)

        # Skip silently when ffmpeg isn't installed (e.g. PR preview CI). Avoid the expensive
        # resolve_media_url network call when we can't do anything with the result.
        if not shutil.which(ffmpeg_binary):
            return None

        # Resolve the source URL (may involve a network request for Swagit/CivicPlus).
        try:
            source_url = provider.resolve_media_url(ep, city.source)
        except MediaUnavailable as exc:
            # Permanently-dead media (no usable recording) is a durable availability signal; a
            # merely-deferred one (a pending feature like multi-segment concat) is not, so leave it
            # unclassified and let the existing materialize-backoff path own it.
            if getattr(exc, "code", None) == MEDIA_DEAD:
                _stamp_availability(
                    ep, "missing", profile, reason="provider reports no usable media"
                )
            return None
        except (ProviderError, Exception):  # noqa: BLE001
            return None
        timeout = getattr(ctx.ffmpeg, "timeout_seconds", None)

        # Download the source once and cache it locally so the subsequent AudioStage encode
        # pass can read from disk rather than re-streaming the rate-limited source.
        detect_url = source_url
        if ctx.source_cache is not None and ep.uid:
            try:
                local = ctx.source_cache.get_or_fetch(ep.uid, source_url)
            except StopRequested:
                # The run's wall-clock budget expired while queued behind another thread's fetch
                # of the same source — defer without recording a failure (#120); not a real error.
                return None
            if local is not None:
                detect_url = str(local)

        # silencedetect is a CPU-bound ffmpeg decode pass, same as an AudioStage encode — gate it
        # on the same NativeWorkGate so a TimelineStage planner pass for one source can't run
        # concurrently, unbounded, alongside the encodes the gate exists to budget (#111 follow-up,
        # audio workflow review 2026-06).
        gate = ctx.native_work_gate
        gate_acquired = False
        if gate is not None:
            gate_acquired = gate.acquire(kind="audio", label=str(ep.uid or ep.guid), stop=ctx.stop)
            if not gate_acquired:
                return None  # deferred: budget/queue pressure, not a real failure
        try:
            silences, source_duration = detect_silences(
                detect_url,
                ffmpeg_binary=ffmpeg_binary,
                noise_db=ctx.silence_noise_db,
                min_duration_s=1.0,  # detect all candidates; thresholds applied below
                timeout=timeout,
                threads=getattr(ctx.ffmpeg, "threads", None),
            )
        finally:
            if gate_acquired:
                gate.release(kind="audio")

        # Durable availability verdict (H16 PR3), judged off the *probed* duration only. A missing
        # probe header means the fetch/decode itself failed (throttle/truncation) — transport, not
        # evidence about content, so it can never confirm silence or clear a known-good episode.
        probed_duration = source_duration
        if probed_duration is None:
            _stamp_availability(ep, "transport_failed", profile)
        elif is_effectively_silent(
            silences,
            probed_duration,
            min_seconds=ctx.silence_min_served_seconds,
            min_fraction=ctx.silence_min_served_fraction,
        ):
            _stamp_availability(
                ep, "silent", profile, reason="successful decode is near-totally silent"
            )
        else:
            _stamp_availability(ep, "playable", profile)

        # Fall back to ep.duration when ffmpeg didn't emit a Duration header.
        if source_duration is None:
            if ep.duration is None:
                return None  # can't build a timeline without total duration
            source_duration = float(ep.duration)

        source_id = ep.sources[0].id if ep.sources else "s0"

        tl = build_silence_timeline(
            source_id,
            source_duration,
            silences,
            lead_trail_min=ctx.silence_lead_trail_min_s,
            mid_min=ctx.silence_mid_min_s,
        )

        if tl is not None and is_degenerate_served_duration(
            tl,
            source_duration,
            min_seconds=ctx.silence_min_served_seconds,
            min_fraction=ctx.silence_min_served_fraction,
        ):
            served_total = tl.segments[-1].served_end if tl.segments else 0.0
            print(
                f"[enrich] silence planner rejected degenerate timeline uid={ep.uid or ep.guid} "
                f"served_total={served_total:.3f}s source_duration={source_duration:.1f}s "
                "— likely a bad/truncated source probe, not a real near-total silence wipeout",
                flush=True,
            )
            if current is not None:
                return None  # preserve the prior (non-degenerate) timeline as-is
            tl = None  # no prior timeline to fall back to → treat as "nothing to trim" below

        if tl is None:
            # Nothing to trim (or a degenerate result was rejected with no prior timeline to
            # keep): return an identity timeline so the episode is stamped and not re-examined
            # on the next run (per TimelinePlanner protocol).
            from citypods.timeline import SourceMedia

            src = SourceMedia(
                id=source_id,
                provider=city.provider,
                ref=ep.video_url,
                media_kind=ep.media_kind,
                duration=source_duration,
                watch_url=(ep.links or {}).get("canonical_video"),
            )
            return identity_timeline(src, source_duration)

        return tl
