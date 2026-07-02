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

import json
import re
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path
from urllib.parse import urlparse

from citypods.availability import is_effectively_silent
from citypods.http import USER_AGENT, StopRequested
from citypods.integrity import REPAIR_TIMELINE_REPLAN, needs_timeline_audio_repair
from citypods.timeline import Segment, SourceMedia, Timeline, identity_timeline

DEFER_CACHE_UNAVAILABLE = "deferred_cache_unavailable"
DEFER_CACHE_STOP = "deferred_cache_stop"
DEFER_DECODE_UNAVAILABLE = "deferred_decode_unavailable"
DEFER_DEGENERATE_TIMELINE = "deferred_degenerate_timeline"
DEFER_NATIVE_GATE = "deferred_native_gate"


def _defer_timeline_plan(ep, reason: str, *, failure_code: str | None = None) -> None:
    ep.timeline_defer_reason = reason
    if failure_code:
        from citypods.media import record_materialize_failure

        record_materialize_failure(ep, failure_code)


def _describe_media_locator(locator: str) -> str:
    """Stable, log-safe locator summary without query tokens."""
    if not locator:
        return "unknown"
    if locator.startswith(("http://", "https://")):
        parsed = urlparse(locator)
        tail = Path(parsed.path).name or parsed.path or "/"
        return f"{parsed.scheme}://{parsed.netloc}/{tail.lstrip('/')}"
    path = Path(locator)
    return str(path)


def _local_file_snapshot(path: str) -> str:
    try:
        stat = Path(path).stat()
    except OSError:
        return f"path={Path(path).name} bytes=unknown"
    return f"path={Path(path).name} bytes={stat.st_size}"


def _silence_summary(silences: list[tuple[float, float]], *, limit: int = 3) -> str:
    if not silences:
        return "count=0 total=0.000s longest=0.000s spans=none"
    total = sum(max(0.0, end - start) for start, end in silences)
    longest = max(max(0.0, end - start) for start, end in silences)
    spans = ", ".join(f"{start:.3f}-{end:.3f}" for start, end in silences[:limit])
    if len(silences) > limit:
        spans += ", ..."
    return f"count={len(silences)} total={total:.3f}s longest={longest:.3f}s spans={spans}"


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
    """Parse ``Duration: HH:MM:SS.ss`` from ffmpeg's probe header.

    This is the **container/format** ``Duration`` header, which can overstate the playable audio
    stream (HLS manifests, or a direct MP4 whose video stream outlasts its audio). It is retained
    as diagnostic telemetry; SilencePlanner must not use it as the EDL source clock. See GH#702.
    """
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", stderr)
    if not m:
        return None
    h, mn, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return h * 3600 + mn * 60 + s


def _parse_ffmpeg_decoded_end(stderr: str) -> float | None:
    """Parse the final processed timestamp (``time=HH:MM:SS.ss``) from ffmpeg's stats stream.

    ``detect_silences`` runs a full ``-vn -f null -`` decode of the audio, so ffmpeg's progress
    ``time=`` reports the last frame's timestamp from that decode. **This is a PTS clock, not a
    decoded-sample-count clock** — without correction it carries forward any discontinuity in the
    source's presentation timestamps (a stream splice, an ad-insertion boundary, a dropped-segment
    gap) as if the gap were real elapsed audio, so it can overstate the true decodable content by
    exactly the gap size. Confirmed by direct reproduction (GH#702): a 10s two-segment file with a
    deliberate 2s forward PTS jump reports ``time=12.0x``, identical to the container `Duration`
    header — both clocks are PTS-based and both overstate. The render path
    (``_build_streaming_single_source_filter``, ``media.py``) resets timestamps to a contiguous
    sample-index clock via ``asetpts=N/SR/TB`` and so naturally compacts the gap away, producing a
    *shorter* file than either PTS-based measurement predicts — exactly the
    `rendered-duration-mismatch` survivors an earlier basis-tier fix (this function, originally)
    could not close. ``detect_silences`` now prepends the identical ``asetpts=N/SR/TB`` reset ahead
    of ``silencedetect`` in its filter chain, so this function's ``time=`` reading is on the same
    gap-compacted clock the render will actually produce (reproduced: 10.06s vs. the render's
    measured 10.069s) — a pure per-frame timestamp rewrite, not a resample, so it costs nothing
    extra and is a no-op on a source with no PTS discontinuity.

    Returns the largest **positive** ``time=`` seen (decode is monotonic, so the final stats line
    is the end), or ``None`` when none is parseable. A stats stream that never advances (only
    ``time=00:00:00.00``) yields ``None`` rather than a zero-length clock the planner would
    otherwise stamp. The leading ``-`` of ffmpeg's occasional negative-wrapped warm-up timestamps
    is deliberately not matched, so they are skipped too.
    """
    best: float | None = None
    for h, mn, s in re.findall(r"time=\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", stderr):
        secs = int(h) * 3600 + int(mn) * 60 + float(s)
        if secs <= 0:
            continue
        if best is None or secs > best:
            best = secs
    return best


def _probe_stream_sample_duration(
    url: str,
    ffprobe_binary: str = "ffprobe",
    timeout: float | None = None,
) -> float | None:
    """ffprobe the first audio stream's sample-clock duration (``duration_ts * time_base``).

    Falls back to the stream-level ``duration`` field. Deliberately does **not** fall back to
    ``format.duration`` (the container header) — that is the clock GH#702 distrusts. Returns
    ``None`` when no stream-level clock is exposed or on any subprocess/parse failure.

    The silence EDL's trailing-silence test and final keep-span must anchor on the source's real
    **audio-stream** end, not the container/format ``Duration`` header. When a source's container
    overstates its audio, the renderer (``atrim``/``aselect``) hits EOF early and the rendered file
    comes out shorter than the planned EDL — the single-file ``rendered-duration-mismatch`` class
    (GH#702). This helper is kept as a low-level diagnostic, but SilencePlanner now relies on the
    local-file decoded duration from ``detect_silences`` and defers when that measurement is
    unavailable. A browser ``user_agent`` is sent for remote inputs (Granicus CDN blocks others);
    a cached local file needs none.
    """
    is_remote = url.startswith(("http://", "https://"))
    cmd = [
        ffprobe_binary,
        "-v",
        "error",
        *(["-user_agent", USER_AGENT] if is_remote else []),
        "-select_streams",
        "a:0",
        "-show_entries",
        "format=duration:stream=duration_ts,time_base,duration",
        "-of",
        "json",
        url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            return None
        out = (result.stdout or "").strip()
        if not out:
            return None
        # Tests historically patch ffprobe with a plain duration line; accept that too.
        if not out.startswith("{"):
            value = float(out)
            return value if value > 0 else None
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
                value = float(stream_duration)
                return value if value > 0 else None
        # Deliberately NO ``format.duration`` fallback: that is the container header — the very
        # clock GH#702 says overstates the audio. Returning it would let the caller mislabel a
        # container value as ``stream-sample``. When no stream-level clock is exposed, return None
        # so callers can decide whether to defer rather than stamp a weak clock.
        return None
    except (
        subprocess.TimeoutExpired,
        subprocess.CalledProcessError,
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
        ZeroDivisionError,
    ):
        return None


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
) -> tuple[list[tuple[float, float]], float | None, float | None]:
    """Run ``ffmpeg silencedetect`` on ``url``; return ``(silences, container_duration,
    decoded_duration)``.

    ``min_duration_s`` is passed to the filter as the minimum silence length to detect.
    Use a short value (e.g. 1.0) to capture all candidates; ``build_silence_timeline``
    applies the per-category thresholds (leading/trailing vs mid-meeting) when filtering.

    Both durations are parsed from the same stderr output — no extra ffprobe call.
    ``container_duration`` is the ``Duration`` header (which can overstate the audio stream);
    ``decoded_duration`` is the decoded audio-stream end (the final ``time=`` stats timestamp from
    this ``-vn`` decode pass), used by ``SilencePlanner`` as the only accepted GH#702 source
    clock. Returns ``([], None, None)`` on any subprocess or parse error.

    The filter chain prepends ``asetpts=N/SR/TB`` ahead of ``silencedetect`` (GH#702 PTS-gap fix):
    without it, both this pass's ``time=`` reading and ``silencedetect``'s own reported silence
    boundaries ride the source's raw presentation timestamps, so a PTS discontinuity (stream
    splice, ad-insertion boundary, dropped HLS segment) is counted as real elapsed audio. The reset
    is a per-frame timestamp rewrite at the native sample rate — no resampling, no second decode
    pass, a no-op when the source has no discontinuity — that puts both readings on the same
    contiguous sample-index clock the render path already uses, so they agree with what actually
    gets rendered instead of with each other's PTS-based overstatement. See
    :func:`_parse_ffmpeg_decoded_end` for the reproduced before/after numbers.

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
        f"asetpts=N/SR/TB,silencedetect=noise={noise_db}dB:d={min_duration_s}",
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
            return [], None, None
        # ffmpeg writes both its probe header and filter output to stderr.
        stderr = result.stderr
        silences = parse_silences(stderr)
        container_duration = _parse_ffmpeg_duration(stderr)
        decoded_duration = _parse_ffmpeg_decoded_end(stderr)
        return silences, container_duration, decoded_duration
    except (
        subprocess.TimeoutExpired,
        subprocess.CalledProcessError,
        OSError,
        ValueError,  # a malformed float() in parse_silences/_parse_ffmpeg_duration
        TypeError,
    ):
        return [], None, None


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

        # Download the source once and cache it locally so both planning and the subsequent
        # AudioStage encode read the same bytes.  Timeline planning must not fall back to probing
        # the remote URL directly: Granicus worker fallback lives in
        # SourceCache/_run_ffmpeg_guarded, and a failed cache/decode means this item should defer,
        # not stamp a weak EDL.
        uid = str(ep.uid or ep.guid or "")
        if ctx.source_cache is None or not uid:
            _defer_timeline_plan(ep, DEFER_CACHE_UNAVAILABLE, failure_code="timeline-cache")
            return None
        try:
            local = ctx.source_cache.get_or_fetch(uid, source_url)
        except StopRequested:
            # The run's wall-clock budget expired while queued behind another thread's fetch of the
            # same source — defer without recording a source failure (#120); not a real error.
            _defer_timeline_plan(ep, DEFER_CACHE_STOP)
            return None
        if local is None:
            _defer_timeline_plan(ep, DEFER_CACHE_UNAVAILABLE, failure_code="timeline-cache")
            return None
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
                _defer_timeline_plan(ep, DEFER_NATIVE_GATE)
                return None  # deferred: budget/queue pressure, not a real failure
        try:
            silences, _container_duration, decoded_duration = detect_silences(
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

        # Durable availability verdict (H16 PR3), judged off the decoded audio-stream end only.
        # A missing decoded end means the fetch/decode itself failed (throttle/truncation/parse
        # failure) — transport, not evidence about content — so it can never confirm silence or
        # clear a known-good episode, and it cannot author an audio-affecting EDL.
        probed_duration = decoded_duration
        if probed_duration is None:
            print(
                f"[enrich] silence planner decode unavailable uid={ep.uid or ep.guid} "
                f"source={_describe_media_locator(source_url)} "
                f"cache={_local_file_snapshot(detect_url)} "
                f"silences={_silence_summary(silences)}",
                flush=True,
            )
            _stamp_availability(ep, "transport_failed", profile)
            _defer_timeline_plan(ep, DEFER_DECODE_UNAVAILABLE, failure_code="timeline-decode")
            return None
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

        # The EDL source clock must be the decoded audio-stream end. Container/provider duration is
        # useful diagnostic metadata, but GH#702 proved it is unsafe as planning authority: a later
        # repair pass can otherwise recreate the over-long EDL that rendered audio cannot satisfy.
        source_duration = decoded_duration
        source_duration_basis = "decoded"

        existing_src = ep.sources[0] if ep.sources else None
        source_id = existing_src.id if existing_src else "s0"
        src = SourceMedia(
            id=source_id,
            provider=existing_src.provider if existing_src else city.provider,
            ref=existing_src.ref if existing_src else ep.video_url,
            media_kind=existing_src.media_kind if existing_src else ep.media_kind,
            duration=source_duration,
            watch_url=(
                existing_src.watch_url
                if existing_src and existing_src.watch_url is not None
                else (ep.links or {}).get("canonical_video")
            ),
            backup_key=existing_src.backup_key if existing_src else None,
            duration_basis=source_duration_basis,
        )
        ep.sources = [src]

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
            current_version = current.version if current is not None else "none"
            existing_basis = existing_src.duration_basis if existing_src is not None else "none"
            container_label = (
                f"{_container_duration:.3f}s" if _container_duration is not None else "None"
            )
            decoded_label = f"{decoded_duration:.3f}s" if decoded_duration is not None else "None"
            print(
                f"[enrich] silence planner rejected degenerate timeline uid={ep.uid or ep.guid} "
                f"served_total={served_total:.3f}s source_duration={source_duration:.1f}s "
                "— likely a bad/truncated source probe, not a real near-total silence wipeout",
                flush=True,
            )
            print(
                f"[enrich] silence planner degenerate detail uid={ep.uid or ep.guid} "
                f"source={_describe_media_locator(source_url)} "
                f"cache={_local_file_snapshot(detect_url)} "
                f"current_timeline={current_version} "
                f"existing_basis={existing_basis} "
                f"repair_selected={needs_timeline_audio_repair(ep, REPAIR_TIMELINE_REPLAN)}",
                flush=True,
            )
            print(
                f"[enrich] silence planner degenerate metrics uid={ep.uid or ep.guid} "
                f"container_duration={container_label} "
                f"decoded_duration={decoded_label} "
                f"kept_segments={len(tl.segments)} "
                f"silences={_silence_summary(silences)}",
                flush=True,
            )
            if current is not None:
                if needs_timeline_audio_repair(ep, REPAIR_TIMELINE_REPLAN):
                    # The canary repair path has already proven the prior EDL is bad. Do not keep
                    # serving it as "current" when the fresh decoded plan is withheld as degenerate;
                    # let media availability/deferred state own the episode until it recovers.
                    ep.timeline = None
                _defer_timeline_plan(
                    ep, DEFER_DEGENERATE_TIMELINE, failure_code="timeline-degenerate"
                )
                # Preserve ordinary prior timelines; repair-selected bad EDLs are cleared above.
                return None
            _defer_timeline_plan(ep, DEFER_DEGENERATE_TIMELINE, failure_code="timeline-degenerate")
            return None

        if tl is None:
            # Nothing to trim (or a degenerate result was rejected with no prior timeline to
            # keep): return an identity timeline so the episode is stamped and not re-examined
            # on the next run (per TimelinePlanner protocol).
            return identity_timeline(src, source_duration)

        return tl
