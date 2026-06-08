"""Enrichment-stage pipeline — the extensible backbone for per-episode features.

Each meeting passes through an ordered list of stages; adding a feature
(transcript, auto-summary, chapter markers, resource links) means adding a stage, not
rewiring the build. Stages mirror the provider registry: a small protocol + a default list.

Two invariants make this safe and incremental (see records.py):

  * **Ordering matters.** A stage whose output feeds the audio bytes (e.g. ``chapters``)
    must run *before* ``audio``, because the audio object is content-addressed by
    ``audio_spec_hash`` which includes chapters. A stage that only affects the feed (e.g.
    ``summary``) can run after. ``default_stages()`` encodes the order.

  * **Shared stop signal.** ASR/LLM/audio work is expensive, so each expensive stage checks the
    shared ``StageContext.stop`` predicate before starting new work — True once the run's
    wall-clock window is spent or a newer build is queued behind it. Anything not done this run is
    picked up on the next — a gradual backfill bounded by wall-clock time, not a count estimate.

A stage mutates episodes in place; downstream the split hashes pick up the change
automatically (``audio_spec_hash`` -> re-encode, ``feed_content_hash`` -> re-render).

--------------------------------------------------------------------------------------------
The ``stop`` convention (read this before adding an expensive stage)
--------------------------------------------------------------------------------------------
``ctx.stop()`` is the run's "wrap up and deploy now" signal. It is True once the wall-clock
window is spent OR a newer Build & Deploy run is queued behind this one (graceful yield, so a
push or the next cron takes over without hard-cancelling the in-flight Pages deploy). The
canonical shape for a stage that does expensive per-item work (encode, transcription,
translation, summarization, any multi-second ASR/LLM/network/CPU job) is::

    for ep in _materialize_set(episodes, city.max_episodes):
        if <already done for ep>:                 # cheap, idempotent — NOT gated by stop
            stats.reused += 1
            continue
        if ctx.stop is not None and ctx.stop():    # check immediately before the costly work
            stats.skipped += 1                     # deferred to a later run — not an error
            continue
        <do the expensive, restartable work for ep, persisting enough to resume next run>

Rules:
  1. **Gate only expensive, deferrable work.** Check ``stop()`` right before the costly operation
     for one item. Never gate cheap/idempotent bookkeeping (reuse checks, attaching an
     already-known URL, setting a default link, the audio *credit* path) — that must always finish
     so the run leaves consistent, deployable state.
  2. **Per item, not per stage.** Call it inside the loop so a run can complete N items and defer
     the rest mid-scan. It is cheap to call (throttled + latched), so per-item is fine.
  3. **Restartable or don't gate it.** Anything skipped on ``stop()`` must be safe to retry next
     run, and any item you *did* finish must be persisted (upload the artifact, record its
     key/URL) before moving on — so yielding never loses or half-writes progress. If a unit of
     work can't be made resumable, it doesn't belong behind ``stop()``.
  4. **Deferred is not failed.** Count a stopped item as skipped/deferred; reserve ``errors`` for
     real failures (which get the #120 backoff instead).
  5. **Don't sense supersession yourself.** ``stop()`` already encapsulates the wall-clock
     deadline and the throttled "newer run queued" check; just call it.
  6. **Cheap+uniform+numerous work fills the window by count, not cost.** ``stop()`` self-limits
     work whose per-item cost is large (an encode/transcription fills the window after a sane
     number of items). A *cheap* uniform op (a page scrape) does not — thousands fit — so a single
     run drains its whole backlog. That's fine in production (it backfills over a run or two), but
     too slow for short-lived contexts like the PR preview, so expose an *optional* per-run count
     cap they can set while leaving production bounded only by ``stop()`` — see ``ChaptersStage`` /
     ``StageContext.chapters_per_source`` (wired to ``citypods build --chapters-cap``).

Ordering caveat: a long stage that runs *before* others (e.g. transcription feeding chapters)
spends the same shared window, so everything downstream of it also defers when it yields. Place
the most valuable expensive stages earliest.
"""

from __future__ import annotations

import collections
import hashlib
import json
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol

from citypods import asr as asr_mod
from citypods.bodies import body_key, canonical_body
from citypods.media import MaterializeStats, SourceCache, _probe_duration_secs, materialize_audio
from citypods.models import City, Episode
from citypods.records import AUDIO_PIPELINE_VERSION
from citypods.timeline import Timeline, remap, timeline_digest


def _materialize_set(episodes: list[Episode], max_per_body: int) -> list[Episode]:
    """The subset worth processing: the most-recent ``max_per_body`` per body. Every
    per-board feed shows at most that many of its body, and the combined feed is a subset of
    the union, so this is exactly what some feed can display — never the deep archive."""
    by_body: dict[str, list[Episode]] = collections.defaultdict(list)
    for ep in episodes:
        by_body[body_key(canonical_body(ep.body or ""))].append(ep)
    out: list[Episode] = []
    for eps in by_body.values():
        eps.sort(key=lambda e: e.published, reverse=True)
        out.extend(eps[:max_per_body])
    return out


@dataclass
class StageContext:
    """Shared resources passed to every stage for one build.

    ``stop`` is a shared predicate (or None) that goes True once the run's wall-clock window is
    spent or a newer Build & Deploy run is queued behind this one; expensive stages check it
    before starting new work so the run wraps up and deploys instead of running indefinitely. See
    the module docstring's "stop convention" for exactly when (and when not) to gate work on it.

    ``chapters_per_source`` is an *optional* per-run count cap on chapter scraping for one source.
    Default is effectively unbounded: production lets chapters backfill fully, bounded only by the
    wall-clock ``stop``. It exists because chapter fetches are cheap+uniform+numerous (~0.3s, no
    encode), so the wall-clock alone lets *thousands* run — fine when draining a real backlog over
    runs, but too slow for the PR preview (no cached chapters → every episode un-scraped), which
    sets a small cap via ``citypods build --chapters-cap N`` to stay a fast sanity-check."""

    storage: object | None
    ffmpeg: object
    max_kbps: int
    dry_run: bool
    stop: Callable[[], bool] | None = None
    chapters_per_source: int = 10_000  # ~unbounded; build() lowers it only for the PR preview
    # EBU R128 loudness normalization (#151). Empty string = disabled.
    # e.g. "ebuR128:-16LUFS" normalises to -16 LUFS (Apple Podcasts / Spotify speech standard).
    loudness_profile: str = ""
    # Silence-trim planner config (#111). Config flows through ctx so SilencePlanner needs no
    # constructor args and enrich_stages() needs no site_config parameter.
    trim_silence: bool = False
    silence_noise_db: float = -40.0
    silence_lead_trail_min_s: float = 1.0
    silence_mid_min_s: float = 10.0
    # Parallel episode processing within one source. Workers are I/O-bound (rate-limited HLS
    # streaming), so this can safely exceed CPU count. Set via site_config max_encodes_per_source.
    max_encodes_per_source: int = 1
    # Per-run download cache shared across TimelineStage (SilencePlanner) and AudioStage so each
    # source is streamed at most once per episode, even when both stages need it.
    source_cache: SourceCache | None = None
    # Global semaphore that caps concurrent ASR inference calls across ALL sources in the run.
    # ASR is CPU-bound and uses all cpu_threads — running N sources' alignment/transcription
    # simultaneously divides effective CPU by N, making each job N× slower.  With max_workers=20
    # sources and one 4-hour meeting per source, 20 simultaneous calls would each take 20× longer,
    # blowing the 6-hour job ceiling.  Serialising to 1 (default) keeps total time predictable.
    # Set via site_config asr_workers (same field that drives cpu_threads per inference call).
    asr_semaphore: threading.Semaphore | None = None
    # ASR inference is native C++ work and can occasionally stop making visible progress. Bound
    # one item by wall-clock so the run can persist completed work instead of waiting for Actions
    # to SIGTERM the whole process. The timeout is base + per-audio-hour; <=0 disables it.
    asr_timeout_base_seconds: float = 15 * 60
    asr_timeout_per_hour_seconds: float = 30 * 60
    # Monotonic deadline for the whole enrich/all phase. ASR timeouts are capped to the remaining
    # budget before this deadline so a single item cannot outlive the planned wrap-up window.
    asr_deadline: float | None = None
    asr_timeout_budget_reserve_seconds: float = 60
    # Set after an ASR timeout so other source workers skip starting more ASR in this run. The
    # timed-out daemon thread may still be burning CPU until process exit, so don't pile on.
    asr_abort_event: threading.Event | None = None
    # Called only for a human/code-change supersession after the post-deploy enrich phase has
    # already abandoned in-flight ASR work. This is deliberately not used for scheduled-run
    # supersession or wall-clock budget stops: those should finish/persist as much completed work
    # as possible.
    fast_yield_exit: Callable[[], None] | None = None


@dataclass
class StageStats:
    name: str
    ran: int = 0  # newly produced this run
    reused: int = 0  # already up to date
    skipped: int = 0  # deferred to a later run (budget) — i.e. the remaining backlog for this stage
    errors: list[str] = field(default_factory=list)
    # Cost accounting (for the resource projection / run history). Additive; default 0.
    seconds: float = 0.0  # wall time spent in this stage this run (set by run_stages)
    bytes_written: int = 0  # object bytes uploaded by this stage this run
    # Audio-only breakdown of ``ran``: expensive encodes vs near-free storage re-credits. Lets the
    # build log show how much of ``ran`` actually consumed encode time (the rest is metadata-only).
    encoded: int = 0
    credited: int = 0
    # Transcript-only breakdown of ``ran``: forced-alignment (Path A) vs fresh transcription
    # (Path B). Shown in the status dashboard as ``Naln·Nasr`` alongside the stage row.
    aligned: int = 0  # Path A: stable-ts forced alignment from source text
    transcribed: int = 0  # Path B: fresh faster-whisper transcription

    def note(self) -> str:
        if not (self.ran or self.reused or self.skipped or self.errors):
            return ""
        parts = [f"{self.name} {self.ran}+{self.reused}"]
        if self.skipped:
            parts.append(f"{self.skipped} queued")
        if self.errors:
            parts.append(f"{len(self.errors)} errors")
        return " ".join(parts) if len(parts) == 1 else parts[0] + " (" + ", ".join(parts[1:]) + ")"


class EnrichmentStage(Protocol):
    name: str
    version: str

    def process(
        self, provider, city: City, episodes: list[Episode], ctx: StageContext
    ) -> StageStats:
        """Enrich the ``_materialize_set`` of ``episodes`` in place, within budget."""
        ...


def _requests_fast_yield_exit(stop: Callable[[], bool] | None) -> bool:
    return bool(stop is not None and getattr(stop, "should_exit_immediately", lambda: False)())


def _asr_timeout_seconds(ctx: StageContext, duration_hours: float) -> float | None:
    configured = ctx.asr_timeout_base_seconds + max(0.0, duration_hours) * (
        ctx.asr_timeout_per_hour_seconds
    )
    timeout = configured if configured > 0 else None
    if ctx.asr_deadline is None:
        return timeout
    remaining = ctx.asr_deadline - time.monotonic() - ctx.asr_timeout_budget_reserve_seconds
    if remaining <= 0:
        return 0.0
    return min(timeout, remaining) if timeout is not None else remaining


def _episode_duration_hours(ep: Episode) -> tuple[float, str]:
    if ep.audio_duration_served is not None and ep.audio_duration_served > 0:
        return ep.audio_duration_served / 3600, "served"
    if ep.duration is not None and ep.duration > 0:
        return ep.duration / 3600, "source"
    return 0.0, "unknown"


class AudioStage:
    """Re-host audio, content-addressed by spec. Wraps the existing materialize pipeline."""

    name = "audio"
    version = AUDIO_PIPELINE_VERSION

    def process(
        self, provider, city: City, episodes: list[Episode], ctx: StageContext
    ) -> StageStats:
        if ctx.dry_run or ctx.storage is None:
            return StageStats(self.name)
        ms: MaterializeStats = materialize_audio(
            city,
            _materialize_set(episodes, city.max_episodes),
            storage=ctx.storage,
            ffmpeg=ctx.ffmpeg,
            max_kbps=ctx.max_kbps,
            loudness_profile=ctx.loudness_profile,
            resolve_media_url=lambda ep: provider.resolve_media_url(ep, city.source),
            stop=ctx.stop,
            source_cache=ctx.source_cache,
            max_workers=ctx.max_encodes_per_source,
        )
        return StageStats(
            self.name,
            ms.hosted,
            ms.reused,
            ms.skipped_budget + ms.skipped_backoff,
            ms.errors,
            bytes_written=ms.bytes_written,
            encoded=ms.encoded,
            credited=ms.credited,
        )


class TimelinePlanner(Protocol):
    """Plugin interface for audio-manipulation planners.

    Planners compose: each receives the currently accumulated ``Timeline`` (or ``None``
    for identity) and may return a new/modified ``Timeline``.  Returning ``None`` leaves
    the current timeline unchanged.  The :class:`TimelineStage` calls planners in
    registration order; the final accumulated result is persisted onto the episode.

    **Planned implementations (separate feature PRs):**
      - Silence trimmer (#111) — cuts silent spans, returns a multi-segment source Timeline.
      - Concat planner (#122) — builds a multi-source Timeline from Swagit segments.
      - Intro/outro inserter (#25) — prepends/appends insert segments.
    """

    name: str
    # Bump to force re-planning. ``TimelineStage`` folds every registered planner's
    # ``(name, version)`` into the signature it stamps on ``Timeline.version``, so a later run
    # can distinguish an up-to-date EDL (skip — planning may be an expensive ffmpeg pass) from
    # one made by an older planner set (re-plan → new digest → re-encode only what changed).
    version: str

    def plan(
        self,
        provider,
        city: City,
        ep: Episode,
        ctx: StageContext,
        current: Timeline | None,
    ) -> Timeline | None:
        """Return a new or modified Timeline, or ``None`` to leave the current one unchanged.

        A planner whose work is expensive (e.g. silence detection runs an ffmpeg pass) and
        that *examined* the episode but found no edit should return an **identity** Timeline
        rather than ``None`` — that way the stage stamps it with the current signature and the
        detection is not re-run next time. Return ``None`` only to genuinely pass through
        (e.g. an inapplicable planner); if every planner returns ``None`` the episode stays
        identity and is re-examined on the next run.
        """
        ...


class TimelineStage:
    """Enrichment stage that builds the episode's Edit Decision List from registered planners.

    Runs **before** :class:`AudioStage` in both ``default_stages`` and ``enrich_stages``
    so the persisted EDL is in place when ``audio_spec_hash`` is computed.  With no
    planners registered (the current state until silence/concat/intro feature PRs land)
    the stage is a no-op: ``ep.timeline`` stays ``None``, which the encoder treats as
    identity (no manipulation, same bytes as always).

    Each planner is called in registration order with the accumulated timeline; it may
    return a new/modified Timeline or ``None`` to pass through.  The first planner to
    produce a non-None result establishes the base; later planners can augment it (e.g.
    an intro planner can prepend an insert segment to a silence-trimmed Timeline).

    **Plan once, persist, don't recompute** (design §4/§7). The stage stamps the produced
    EDL's ``version`` with a signature of the registered planner set+versions and, on later
    runs, *skips* episodes whose stored EDL already carries that signature — so an expensive
    planner (silence detection is an ffmpeg pass) runs once, not every build. A bumped planner
    version changes the signature → those episodes re-plan → new digest → they (and only they)
    re-encode. Planning is gated on ``ctx.stop()`` like an encode: deferred, not failed.
    """

    name = "timeline"
    version = "1"

    def __init__(self, planners: list[TimelinePlanner] | None = None):
        self.planners: list[TimelinePlanner] = planners or []

    def _signature(self) -> str:
        """Stable signature of the registered planner set, stamped onto ``Timeline.version``.
        Lets a later run skip episodes already planned by this exact set+versions and re-plan
        ones produced by an older set. ``"identity"`` when no planners are registered."""
        if not self.planners:
            return "identity"
        return "+".join(sorted(f"{p.name}:{getattr(p, 'version', '1')}" for p in self.planners))

    def process(
        self, provider, city: City, episodes: list[Episode], ctx: StageContext
    ) -> StageStats:
        stats = StageStats(self.name)
        sig = self._signature()
        all_eps = list(_materialize_set(episodes, city.max_episodes))

        if not self.planners:
            # No planners → identity path for all episodes; nothing to run in parallel.
            stats.reused = len(all_eps)
            return stats

        lock = threading.Lock()

        def _plan_one(ep: Episode) -> None:
            # Already planned by this exact planner set+versions → don't recompute. A stale
            # signature (older set) falls through and re-plans.
            if ep.timeline is not None and ep.timeline.version == sig:
                with lock:
                    stats.reused += 1
                return

            # Planning may be an expensive, restartable ffmpeg pass, so gate it on the shared
            # stop signal exactly like an encode — deferred to a later run, not an error.
            if ctx.stop is not None and ctx.stop():
                with lock:
                    stats.skipped += 1
                return

            current: Timeline | None = ep.timeline
            changed = False
            for planner in self.planners:
                result = planner.plan(provider, city, ep, ctx, current)
                if result is not None:
                    current = result
                    changed = True

            if changed and current is not None:
                # Stamp the planner-set signature so a future run can detect staleness.
                ep.timeline = replace(current, version=sig)
                with lock:
                    stats.ran += 1
            else:
                # No planner fired → identity path; ep.timeline stays None (== identity).
                with lock:
                    stats.reused += 1

        if ctx.max_encodes_per_source > 1:
            with ThreadPoolExecutor(max_workers=ctx.max_encodes_per_source) as pool:
                list(pool.map(_plan_one, all_eps))
        else:
            for ep in all_eps:
                _plan_one(ep)

        return stats


def is_timed_transcript(content: bytes) -> bool:
    """Heuristic: does this content look like a timed transcript (VTT or SRT)?

    Returns ``True`` for WebVTT files (``WEBVTT`` header) and SRT files (cues with
    ``HH:MM:SS,mmm --> HH:MM:SS,mmm`` timestamps).  Returns ``False`` for plain text,
    PDF, and other untimed formats.  Used to set ``transcript.synced`` on ingestion
    (INFRA-8) — untimed transcripts render as notes-only, never mis-aligned.
    """
    head = content[:1024].decode("utf-8", errors="replace").strip()
    if head.startswith("WEBVTT"):
        return True
    if re.search(r"\d{2}:\d{2}:\d{2}[,\.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}", head):
        return True
    return False


class RemapStage:
    """Converts source-time chapters (and, later, transcript cues) to served-time
    using the episode's EDL.

    Runs after :class:`TimelineStage` (which produces the EDL) and **before**
    :class:`AudioStage` so the embedded M4A chapter markers are in served-time (the
    time a listener's app scrubs to).  No-op for identity timelines (source == served)
    or when chapters are already remapped.

    The ``chapters_basis`` field on the episode tracks which time-base chapters are in:
      - ``"source:s0"`` (default): provider-supplied, source-clock timestamps.
      - ``"served:<edl-version>"``: remapped to the served clock, tagged with the EDL
        version they were remapped against (so a later run can detect a stale remap).

    Cut-span chapters (whose start falls in a removed silence gap) are dropped by
    :func:`~citypods.timeline.remap` — they would scrub to nothing in the served file.
    A chapter whose ``end`` was cut keeps ``end=None``; the encoder's ``_ffmetadata`` derives
    its end from the next chapter's start (the INFRA-1 ``remap(clamp_to=…)`` primitive is for
    single-boundary consumers like the transcript/permalink renderers, not multi-chapter
    embedding — clamping to the served *duration* would wrongly overlap later chapters).
    """

    name = "remap"
    version = "1"

    def process(
        self, provider, city: City, episodes: list[Episode], ctx: StageContext
    ) -> StageStats:
        stats = StageStats(self.name)
        for ep in _materialize_set(episodes, city.max_episodes):
            if not _needs_chapter_remap(ep):
                stats.reused += 1
                continue
            source_id = ep.sources[0].id if ep.sources else "s0"
            # A cut chapter end stays None on purpose: the encoder derives it from the next
            # chapter's start, which is correct for a chapter truncated by a removed span
            # (clamping to the served duration would overlap later chapters).
            ep.chapters = remap(ep.timeline, ep.chapters, source_id=source_id)
            # Stamp the EDL version so a later run can tell these served-time chapters were
            # remapped against *this* timeline (staleness — see _needs_chapter_remap).
            ep.chapters_basis = f"served:{ep.timeline.version}"
            stats.ran += 1
        return stats


def _needs_chapter_remap(ep: Episode) -> bool:
    """True when ep.chapters need to be remapped from source-time to served-time."""
    if ep.chapters_basis.startswith("served"):
        # Already remapped (basis is "served" or "served:<edl-version>"). We do NOT re-remap
        # even if the EDL has since changed (a bumped planner version → ep.timeline.version no
        # longer matches the stamped one): remapping already-served values as if they were
        # source-time would corrupt them, and the source-time originals aren't retained
        # (served-time is canonical, design §10.2).
        # TODO(#111): when SilencePlanner bumps its version, the stored timeline changes but
        # chapters are already in served-time and can't be trivially re-remapped (the source-time
        # originals are not retained — design §10.2). Re-fetch source chapters from the provider
        # and re-run remap on EDL version mismatch. Inert until a planner version is bumped.
        return False
    if not ep.chapters:
        return False  # nothing to remap
    if ep.timeline is None:
        return False  # identity: source == served, no remap needed
    if timeline_digest(ep.timeline) == "":
        return False  # identity timeline: source == served
    return True


class ChaptersStage:
    """Fetch agenda-item chapter markers (and a transcript link) for providers that expose
    them. Audio-affecting, so it runs *before* ``audio``: chapters are part of
    ``audio_spec_hash``, so adding them changes the spec and the next audio pass re-encodes the
    file with embedded markers — spread over runs like the rest of the backfill.

    Each chapter fetch is one cheap network call, bounded per run by ``ctx.chapters_per_source``
    (a count — see StageContext) and the shared ``ctx.stop`` predicate (wall-clock/superseded);
    the rest defer to later runs. No-op for providers without a ``fetch_chapters`` method.
    """

    name = "chapters"
    version = "1"

    def process(
        self, provider, city: City, episodes: list[Episode], ctx: StageContext
    ) -> StageStats:
        stats = StageStats(self.name)
        fetch = getattr(provider, "fetch_chapters", None)
        if ctx.dry_run or fetch is None:
            return stats
        remaining = ctx.chapters_per_source
        for ep in _materialize_set(episodes, city.max_episodes):
            if ep.chapters:  # already captured; chapters don't change once set
                stats.reused += 1
                continue
            if remaining <= 0 or (ctx.stop is not None and ctx.stop()):
                stats.skipped += 1
                continue
            remaining -= 1
            try:
                chapters, transcript = fetch(ep, city.source)
            except Exception as exc:  # one bad page must not fail the whole source
                stats.errors.append(f"{ep.uid}: {exc}")
                continue
            if chapters:
                ep.chapters = chapters
                # Don't clobber a richer transcript already set (e.g. CivicClerk's published
                # transcript PDF beats a closed-caption .srt fallback).
                if transcript and "transcript" not in (ep.links or {}):
                    ep.links = {**(ep.links or {}), "transcript": transcript}
                stats.ran += 1
            else:
                stats.reused += 1  # no agenda items on this page; nothing to embed
        return stats


class LinksStage:
    """Attach resource links (canonical video, agenda, minutes, ...) to each episode.

    Feed-only (runs after ``audio``): links never affect the audio bytes, only the rendered
    show notes, so a change re-renders via ``feed_content_hash`` but never re-encodes. The
    link data is already in hand from the provider fetch, so this stage costs no network and
    needs no budget — it normalizes what providers set on ``ep.links``, fills a
    ``canonical_video`` default, and is the seam where future link enrichers (e.g. resolving
    agenda PDFs, which *would* cost a request) plug in with their own budget.
    """

    name = "links"
    version = "1"

    def process(
        self, provider, city: City, episodes: list[Episode], ctx: StageContext
    ) -> StageStats:
        stats = StageStats(self.name)
        episode_links = getattr(provider, "episode_links", None)
        for ep in _materialize_set(episodes, city.max_episodes):
            links = dict(ep.links or {})
            if episode_links is not None:
                try:
                    links.update(episode_links(ep, city.source))
                except Exception as exc:  # one bad episode must not fail the whole source
                    stats.errors.append(f"{ep.uid}: {exc}")
            # A canonical video reference is always available: the provider's watch-page URL,
            # or the direct media URL when that's all there is.
            links.setdefault("canonical_video", ep.video_url)
            # Feed-level link rendered on every episode: the city's canonical meetings/agenda
            # portal (or its general website), so listeners can reach the ground-truth source.
            meetings_url = city.meetings_url or city.city_website
            if meetings_url:
                links.setdefault("meetings", meetings_url)
            links = {k: v for k, v in links.items() if v}
            if links != (ep.links or {}):
                ep.links = links
                stats.ran += 1
            else:
                stats.reused += 1
        return stats


TRANSCRIPT_PIPELINE_VERSION = "1"
ASR_PIPELINE_VERSION = "1"

# MIME types used for the <podcast:transcript> tag and the stored object's content-type.
# Public (imported by citypods.feeds) so the feed tag and the stored object never disagree.
TRANSCRIPT_MIME = {
    "vtt": "text/vtt",
    "srt": "application/x-subrip",
    "json": "application/json",
    "txt": "text/plain",
}


def _transcript_spec_hash(content: bytes) -> str:
    """Hash of the inputs that determine a transcript's bytes: the fetched *content* + version.

    Keyed on the content, not the provider URL — mirroring how ``audio_spec_hash`` deliberately
    excludes the tokenized/expiring source URL. A rotated download token (Swagit/CivicClerk can
    hand back a signed URL) thus produces the *same* key for identical bytes, so a cold-cache or
    post-GC re-fetch reuses the object instead of orphaning a near-duplicate."""
    import hashlib

    digest = hashlib.sha1(content).hexdigest()
    spec = {"v": TRANSCRIPT_PIPELINE_VERSION, "content": digest}
    blob = json.dumps(spec, separators=(",", ":"), sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def _transcript_object_key(src_key: str, uid: str, spec: str, fmt: str) -> str:
    return f"transcripts/{src_key}/{uid}-{spec}.{fmt}"


def _asr_object_key(src_key: str, uid: str, recipe: str) -> str:
    """ASR keys use an ``asr-`` infix to distinguish them from provider content-hash keys."""
    return f"transcripts/{src_key}/{uid}-asr-{recipe}.vtt"


def _preprocess_align_text(text: str) -> str:
    """Extract spoken dialogue from a meeting-minutes source transcript.

    Provider transcripts (CivicClerk, etc.) are *minutes documents*, not pure
    speech transcripts.  They include agenda headers, speaker attribution labels
    (``COUNCIL MEMBER SMITH:``), coarse timestamps, motion/vote boilerplate, and
    legal text that was never spoken aloud.  Passing that verbatim to stable-ts
    causes ~50 % alignment failures because half the words don't appear in the audio.

    Strategy:
    - Lines that are pure timestamps (``[00:15:23]`` / ``00:15:23``) → drop
    - Speaker-label prefix on a line (``ALL CAPS NAME:``) → strip the label, keep the rest
    - Lines that are entirely ALL-CAPS with no lower-case → likely headers, drop
    - Very short lines (≤ 2 words after stripping) → drop (vote tallies, "Aye", "No", etc.)
    - Strip inline timestamps embedded mid-sentence

    Returns the cleaned spoken-only text as a single space-joined string.  Returns
    the original text unchanged if the cleaned result is < 20 % of the original
    word count (safety valve: the transcript may not be minutes-style).
    """
    import re

    lines_out = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        # Drop pure-timestamp lines: [HH:MM:SS], HH:MM:SS, HH:MM, etc.
        if re.fullmatch(r"\[?\d{1,2}:\d{2}(:\d{2})?\]?", s):
            continue
        # Drop ALL-CAPS-only lines (agenda headers, section dividers)
        if s == s.upper() and re.search(r"[A-Z]", s):
            continue
        # Strip inline timestamps
        s = re.sub(r"\[?\d{1,2}:\d{2}(:\d{2})?\]?", "", s).strip()
        # Strip leading speaker-attribution label: "FIRSTNAME LASTNAME:" or "DR. NAME:"
        s = re.sub(r"^[A-Z][A-Z\s\.\-]+:\s*", "", s).strip()
        # Skip very short residuals (single words, vote tallies)
        if len(s.split()) <= 2:
            continue
        lines_out.append(s)

    cleaned = " ".join(lines_out)
    orig_words = len(text.split())
    clean_words = len(cleaned.split())
    # Safety valve: if we stripped > 80 % of the words the heuristics were too aggressive —
    # return the original so alignment at least gets something to work with.
    if orig_words > 0 and clean_words < orig_words * 0.20:
        return text
    return cleaned


def _detect_format(content: bytes) -> str:
    head = content[:512].decode("utf-8", errors="replace").strip()
    if head.startswith("WEBVTT"):
        return "vtt"
    if re.search(r"\d{2}:\d{2}:\d{2}[,\.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}", head):
        return "srt"
    return "txt"


@contextmanager
def _download_audio(url: str):
    """Download the hosted audio to a temp file, yield the Path, then clean up.

    Uses a plain ``requests`` session (not ``make_session``) because:
    - ``ep.hosted_audio_url`` is a URL *we* generated (our own CDN), not untrusted user input,
      so the SSRF guard in ``GuardedHTTPAdapter`` adds no value here.
    - Hosted M4A files routinely exceed the 64 MiB ``MAX_RESPONSE_BYTES`` cap that
      ``make_session`` enforces on Content-Length, which would reject valid audio.
    """
    import tempfile as _tmp2

    with _tmp2.TemporaryDirectory() as t:
        dest = Path(t) / "audio.m4a"
        _download_audio_file(url, dest)
        yield dest


def _download_audio_file(url: str, dest: Path) -> None:
    import requests as _req

    with _req.Session() as sess:
        sess.headers["User-Agent"] = "citypods/0.1 (+https://github.com/)"
        r = sess.get(url, timeout=300, stream=True)
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)


def _refresh_served_duration_from_audio(ep: Episode, audio_path: Path, ffmpeg_binary: str) -> str:
    probed = _probe_duration_secs(audio_path, ffmpeg_binary)
    if probed is None or probed <= 0:
        return "unknown"
    if ep.audio_duration_served is None or abs(ep.audio_duration_served - probed) > 1.0:
        ep.audio_duration_served = probed
        return "hosted"
    return "served"


class TranscriptStage:
    """Stores and references provider transcripts as content-addressed objects.

    Implements the **reuse-first** provider transcript slot from design doc §5:
      1. If transcript already stored (``ep.transcript_key`` set + object present) → reuse.
      2. If a provider transcript URL is in ``ep.links["transcript"]`` and the episode
         has not yet been stored (``ep.transcript_key`` is None) → fetch, detect timing,
         store content-addressed, set ``transcript_synced`` and ``transcript_basis``.
         If the result is timed (VTT/SRT) the episode is complete; skip ASR.
         If untimed (txt), fall through to step 3 so ASR can upgrade it.
      3. ASR slot (issue #110): produce a timed VTT in *served time* from the hosted
         audio.  Two sub-paths:
           A. Forced alignment (stable-ts) — when a stored untimed source transcript
              is available.  Preserves official wording; faster than full transcription.
           B. Fresh transcription (faster-whisper) — when no source text exists.
              Uses episode title/body as ``initial_prompt`` to prime proper nouns.

    Step 1 only fast-paths when ``transcript_synced is True``.  Episodes with an untimed
    stored transcript (``transcript_key`` set, ``synced=False``) fall through to step 3
    so alignment can upgrade them to a fully timed VTT.

    The ``<podcast:transcript>`` tag is only emitted by :mod:`citypods.feeds` when
    ``ep.transcript_synced is True`` — untimed transcripts (plain text, PDFs) are
    never mis-aligned to the audio.

    ASR transcripts are keyed on inputs (recipe hash) not output content: changing the
    model or audio version triggers re-transcription without downloading the stored file.

    Note on remapping: for identity timelines, provider transcript timestamps are
    already in served time (source == served), so ``transcript_basis = "served"``.
    For non-identity timelines, a full VTT/SRT parser + remap is required (pending;
    see INFRA-5). Until then, non-identity episodes carry ``basis = "source:s0"`` and
    ``synced = False`` to prevent mis-alignment.  ASR-generated transcripts always
    use ``basis = "served"`` because ASR runs on the hosted (served) audio.
    """

    name = "transcript"
    version = TRANSCRIPT_PIPELINE_VERSION

    def process(
        self, provider, city: City, episodes: list[Episode], ctx: StageContext
    ) -> StageStats:
        import math
        import os
        import tempfile as _tmp

        from citypods.records import source_key as _src_key
        from citypods.timeline import timeline_digest as _td

        stats = StageStats(self.name)
        if ctx.dry_run or ctx.storage is None:
            return stats

        src_key = _src_key(city)
        hosted_keys = (
            {k for k, _ in ctx.storage.list_objects(f"transcripts/{src_key}/")}
            if hasattr(ctx.storage, "list_objects")
            else None
        )

        def _present(k: str) -> bool:
            return k in hosted_keys if hosted_keys is not None else ctx.storage.exists(k)

        cpu_threads = max(1, math.ceil((os.cpu_count() or 4) / city.asr_workers))

        # Load the Whisper model — uses the process-level cache so the actual load only
        # happens once per process (pre-loaded by run.py before workers start).  The log
        # line is suppressed here since run.py already printed it; any load failure is
        # still recorded as a single batch error rather than one per episode.
        _asr_model = None
        if city.asr_enabled:
            try:
                _asr_model = asr_mod.load_model(city.asr_model, city.asr_compute_type, cpu_threads)
            except Exception as exc:  # noqa: BLE001
                stats.errors.append(f"ASR model load failed ({city.asr_model}): {exc}")
                print(
                    f"[enrich] transcript model-load error slug={city.slug} "
                    f"provider={city.provider}: {exc}",
                    flush=True,
                )

        for ep in _materialize_set(episodes, city.max_episodes):
            label = ep.uid or ep.guid
            ep_ref = (
                f"slug={city.slug} provider={city.provider} source={src_key} "
                f"uid={label} guid={ep.guid}"
            )
            # 1. Already synced: re-attach URL and done.  Runs unconditionally (even after
            #    stop()) so a yielded run still references every already-synced transcript.
            if ep.transcript_key and ep.transcript_synced and _present(ep.transcript_key):
                ep.transcript_hosted_url = ctx.storage.public_url(ep.transcript_key)
                stats.reused += 1
                continue

            # 1b. Untimed stored transcript: re-attach URL so the feed still shows it as a
            #     text note, then fall through to the ASR slot to attempt an upgrade.
            if ep.transcript_key and not ep.transcript_synced and _present(ep.transcript_key):
                ep.transcript_hosted_url = ctx.storage.public_url(ep.transcript_key)
                # fall through to step 3

            # 2. Provider transcript slot: fetch + store if we don't have it yet.
            provider_url = (ep.links or {}).get("transcript")
            if provider_url and not ep.transcript_key:
                if ctx.stop is not None and ctx.stop():
                    stats.skipped += 1
                    continue

                try:
                    from citypods.http import make_session

                    print(
                        f"[enrich] transcript provider-fetch start {ep_ref}",
                        flush=True,
                    )
                    with make_session() as sess:
                        resp = sess.get(provider_url, timeout=30)
                    if resp.status_code >= 400:
                        stats.errors.append(f"{ep.uid}: HTTP {resp.status_code} for {provider_url}")
                        continue

                    content = resp.content
                    fmt = _detect_format(content)
                    timed = is_timed_transcript(content)
                    spec = _transcript_spec_hash(content)
                    key = _transcript_object_key(src_key, ep.uid or ep.guid, spec, fmt)

                    is_identity = ep.timeline is None or _td(ep.timeline) == ""
                    if timed and is_identity:
                        basis = "served"
                        synced = True
                    elif timed:
                        basis = "source:s0"
                        synced = False
                    else:
                        basis = "source:s0"
                        synced = False

                    with _tmp.TemporaryDirectory() as t:
                        dest = Path(t) / f"transcript.{fmt}"
                        dest.write_bytes(content)
                        mime = TRANSCRIPT_MIME.get(fmt, "text/plain")
                        url = ctx.storage.put_file(key, dest, mime)

                    ep.transcript_key = key
                    ep.transcript_hosted_url = url
                    ep.transcript_spec_hash = spec
                    ep.transcript_format = fmt
                    ep.transcript_basis = basis
                    ep.transcript_synced = synced
                    stats.ran += 1
                    print(
                        f"[enrich] transcript provider-fetch done {ep_ref} "
                        f"fmt={fmt} synced={synced}",
                        flush=True,
                    )

                    if synced:
                        continue  # timed VTT already stored — skip ASR
                except Exception as exc:  # noqa: BLE001
                    stats.errors.append(f"{ep.uid}: {exc}")
                    print(
                        f"[enrich] transcript provider-fetch error {ep_ref}: {exc}",
                        flush=True,
                    )
                    continue

            # 3. ASR slot (issue #110): produce a timed VTT from hosted audio.
            #    Guards: model must be loaded (skip gracefully if load failed), need hosted
            #    audio with a stable spec hash (implies ChaptersStage ran).
            if _asr_model is None:
                continue
            if not (ep.audio_key and ep.audio_spec_hash and ep.hosted_audio_url):
                continue
            if ep.transcript_synced:
                continue  # step 2 may have just set this in the same pass
            if ctx.asr_abort_event is not None and ctx.asr_abort_event.is_set():
                stats.skipped += 1
                print(
                    f"[enrich] transcript asr skipped {ep_ref} reason=prior-timeout",
                    flush=True,
                )
                continue

            # Determine alignment text from any stored untimed source transcript.
            # Strip non-spoken minutes content (headers, speaker labels, vote tallies)
            # before passing to stable-ts — provider "transcripts" are minutes documents
            # that include text never spoken in the audio, which causes ~50 % alignment
            # failures when passed verbatim.
            align_text: str | None = None
            if ep.transcript_hosted_url and ep.transcript_format == "txt":
                try:
                    from citypods.http import make_session

                    with make_session() as sess:
                        r = sess.get(ep.transcript_hosted_url, timeout=30)
                    if r.status_code == 200:
                        raw_text = r.content.decode("utf-8", errors="replace")
                        align_text = _preprocess_align_text(raw_text)
                except Exception:  # noqa: BLE001
                    pass  # alignment hint unavailable; fall back to fresh transcription

            align_hash = hashlib.sha1(align_text.encode()).hexdigest()[:12] if align_text else None
            recipe = asr_mod.asr_spec_hash(
                ep.audio_spec_hash, city.asr_model, align_hash, ASR_PIPELINE_VERSION
            )
            asr_key = _asr_object_key(src_key, ep.uid or ep.guid, recipe)

            if _present(asr_key):
                ep.transcript_key = asr_key
                ep.transcript_hosted_url = ctx.storage.public_url(asr_key)
                ep.transcript_synced = True
                ep.transcript_basis = "served"
                ep.transcript_format = "vtt"
                ep.transcript_spec_hash = recipe
                stats.reused += 1
                continue

            if ctx.stop is not None and ctx.stop():
                stats.skipped += 1
                continue

            # Acquire the global ASR semaphore before starting inference.
            # ASR is CPU-bound; running N concurrent inference calls divides effective CPU
            # by N, making each N× slower.  With max_workers=20 sources and one 4h meeting
            # per source, 20 simultaneous calls would each take 20× longer and blow the
            # 6-hour job ceiling.  The semaphore (default 1) serialises inference globally
            # while letting all other stages (chapters, audio) continue in parallel.
            # After waiting (potentially a long time), re-check stop() so we don't start
            # work that the budget no longer has room for.
            sem = ctx.asr_semaphore
            if sem is not None:
                print(
                    f"[enrich] transcript asr wait {ep_ref}",
                    flush=True,
                )
                sem.acquire()
                print(
                    f"[enrich] transcript asr acquired {ep_ref}",
                    flush=True,
                )
                if ctx.asr_abort_event is not None and ctx.asr_abort_event.is_set():
                    sem.release()
                    stats.skipped += 1
                    print(
                        f"[enrich] transcript asr skipped {ep_ref} reason=prior-timeout",
                        flush=True,
                    )
                    continue
                if ctx.stop is not None and ctx.stop():
                    sem.release()
                    stats.skipped += 1
                    continue

            audio_tmp = _tmp.TemporaryDirectory()
            audio_path = Path(audio_tmp.name) / "audio.m4a"
            try:
                _download_audio_file(ep.hosted_audio_url, audio_path)
            except Exception as exc:  # noqa: BLE001
                audio_tmp.cleanup()
                if sem is not None:
                    sem.release()
                    sem = None
                stats.errors.append(f"{ep.uid}: audio download: {exc}")
                print(
                    f"[enrich] transcript audio-download error {ep_ref}: {exc}",
                    flush=True,
                )
                continue

            probe_source = _refresh_served_duration_from_audio(
                ep,
                audio_path,
                getattr(ctx.ffmpeg, "binary", "ffmpeg"),
            )
            if probe_source == "hosted":
                print(
                    f"[enrich] transcript audio-probe {ep_ref} "
                    f"duration_s={ep.audio_duration_served:.1f}",
                    flush=True,
                )

            dur_h, duration_source = _episode_duration_hours(ep)
            if probe_source == "hosted":
                duration_source = "hosted"
            duration_label = f"{dur_h:.1f}" if duration_source != "unknown" else "unknown"
            mode = "align" if align_text else "transcribe"
            timeout_s = _asr_timeout_seconds(ctx, dur_h)
            if timeout_s is not None and timeout_s <= 0:
                audio_tmp.cleanup()
                if sem is not None:
                    sem.release()
                    sem = None
                stats.skipped += 1
                print(
                    f"[enrich] transcript asr skipped {ep_ref} reason=budget-exhausted",
                    flush=True,
                )
                continue

            timeout_label = f"{timeout_s / 60:.1f}" if timeout_s is not None else "disabled"
            print(
                f"[enrich] transcript asr start {ep_ref} mode={mode} duration_h={duration_label} "
                f"duration_source={duration_source} timeout_m={timeout_label}",
                flush=True,
            )

            # Run inference in a daemon thread so stop() can interrupt it immediately.
            # CTranslate2 inference runs in C++ with the GIL released — it cannot be
            # interrupted from Python.  The daemon thread approach lets the main thread
            # poll stop() every 2 s and bail out without waiting for inference to finish:
            #   • Semaphore is released immediately on stop() so cleanup is not blocked.
            #   • The daemon thread keeps running in the background but dies automatically
            #     when the process exits (daemon=True), so no zombie work survives the run.
            #   • Results produced after stop() fires are never stored — this prevents
            #     ``transcript done`` being reported for work not counted in this run's budget.
            _vtt: list[bytes] = []
            _err: list[Exception] = []
            _aligned: list[bool] = []

            # Bind per-iteration values as default args so the closure captures their
            # current values, not a reference that may change in future loop iterations
            # (ruff B023).  The thread calls _infer() with no positional args.
            def _infer(
                _ep=ep,
                _at=align_text,
                _ep_ref=ep_ref,
                _audio=audio_path,
                _audio_tmp=audio_tmp,
                _result=_vtt,
                _errors=_err,
                _was_aligned=_aligned,
            ) -> None:
                del _audio_tmp  # keep the tempdir alive for abandoned daemon inference
                try:

                    def _transcribe_fresh() -> bytes:
                        _prompt = ". ".join(
                            p for p in (city.podcast_title, _ep.body, _ep.title) if p
                        )
                        return asr_mod.transcribe(
                            _audio,
                            _asr_model,
                            city.asr_language or None,
                            city.asr_compute_type,
                            city.asr_beam_size,
                            _prompt,
                            cpu_threads,
                        )

                    if _at:
                        try:
                            _result.append(
                                asr_mod.align(
                                    _audio,
                                    _at,
                                    _asr_model,
                                    city.asr_language or None,
                                    cpu_threads,
                                )
                            )
                            _was_aligned.append(True)
                        except Exception as _align_exc:  # noqa: BLE001
                            _quality_error = getattr(asr_mod, "AlignmentQualityError", None)
                            if _quality_error is not None and isinstance(
                                _align_exc, _quality_error
                            ):
                                reason = "alignment-low-quality"
                            else:
                                reason = "alignment-error"
                            print(
                                f"[enrich] transcript {reason} {_ep_ref}, "
                                f"retrying as transcribe: {_align_exc}",
                                flush=True,
                            )
                            _result.append(_transcribe_fresh())
                            _was_aligned.append(False)
                    else:
                        _result.append(_transcribe_fresh())
                        _was_aligned.append(False)
                except Exception as _exc:  # noqa: BLE001
                    _errors.append(_exc)

            _t = threading.Thread(target=_infer, daemon=True, name=f"asr-{ep.uid[:8]}")
            _t.start()

            _abandoned = False
            _timeout_at = time.monotonic() + timeout_s if timeout_s else None
            while _t.is_alive():
                if ctx.stop is not None and ctx.stop():
                    _abandoned = True
                    print(
                        f"[enrich] transcript stop {ep_ref}"
                        f" (inference continues in background, result discarded)",
                        flush=True,
                    )
                    if sem is not None:
                        sem.release()
                        sem = None  # prevent double-release in the outer finally
                    stats.skipped += 1
                    break
                if _timeout_at is not None and time.monotonic() >= _timeout_at:
                    _abandoned = True
                    message = f"{label}: ASR timeout after {timeout_s / 60:.1f}m"
                    stats.errors.append(message)
                    stats.skipped += 1
                    if ctx.asr_abort_event is not None:
                        ctx.asr_abort_event.set()
                    print(
                        f"[enrich] transcript asr timeout {ep_ref} seconds={timeout_s:.0f} "
                        "(inference continues in background, result discarded; "
                        "remaining ASR skipped this run)",
                        flush=True,
                    )
                    if sem is not None:
                        sem.release()
                        sem = None
                    break
                time.sleep(2)

            if _abandoned:
                if ctx.fast_yield_exit is not None and _requests_fast_yield_exit(ctx.stop):
                    ctx.fast_yield_exit()
                continue

            # Normal completion — release semaphore and check for errors/results.
            if sem is not None:
                sem.release()
                sem = None
            audio_tmp.cleanup()

            if _err:
                stats.errors.append(f"{ep.uid}: ASR: {_err[0]}")
                print(
                    f"[enrich] transcript asr error {ep_ref}: {_err[0]}",
                    flush=True,
                )
                continue

            if not _vtt:
                stats.errors.append(f"{ep.uid}: ASR: inference produced no result")
                print(
                    f"[enrich] transcript asr error {ep_ref}: inference produced no result",
                    flush=True,
                )
                continue

            try:
                with _tmp.TemporaryDirectory() as t:
                    dest = Path(t) / "transcript.vtt"
                    dest.write_bytes(_vtt[0])
                    url = ctx.storage.put_file(asr_key, dest, TRANSCRIPT_MIME["vtt"])

                ep.transcript_key = asr_key
                ep.transcript_hosted_url = url
                ep.transcript_spec_hash = recipe
                ep.transcript_format = "vtt"
                ep.transcript_basis = "served"
                ep.transcript_synced = True
                if _aligned[0]:
                    stats.aligned += 1
                    outcome = "aligned"
                else:
                    stats.transcribed += 1
                    outcome = "transcribed"
                stats.ran += 1
                print(f"[enrich] transcript asr done {ep_ref} method={outcome}", flush=True)
            except Exception as exc:  # noqa: BLE001
                stats.errors.append(f"{ep.uid}: ASR store: {exc}")
                print(
                    f"[enrich] transcript store error {ep_ref}: {exc}",
                    flush=True,
                )
            finally:
                # Belt-and-suspenders: release semaphore if not already released.
                if sem is not None:
                    sem.release()
                    sem = None

        return stats


def default_stages() -> list[EnrichmentStage]:
    """Ordered: audio-affecting stages must precede ``audio`` so a change re-encodes;
    feed-only stages (``links``) follow it. This is the full list — used by a one-shot
    ``citypods build`` (local dev / PR preview / tests). Production splits it across two
    phases via :func:`render_stages` + :func:`enrich_stages` (see build).

    Ordering invariant: ``chapters`` → ``timeline`` → ``remap`` → ``audio``.
    Chapters must arrive (source-time) before timeline plans the EDL; remap converts
    them to served-time before audio embeds them as M4A markers."""
    from citypods.concat import SwagitConcatPlanner
    from citypods.silence import SilencePlanner

    return [
        ChaptersStage(),
        TimelineStage(planners=[SwagitConcatPlanner(), SilencePlanner()]),
        RemapStage(),
        AudioStage(),
        TranscriptStage(),
        LinksStage(),
    ]


def render_stages() -> list[EnrichmentStage]:
    """The *cheap* stages that run in the fast render+deploy phase: no per-item network or encode,
    so the feeds/pages publish in ~minutes. ``links`` only normalizes data already in hand from the
    provider fetch (see LinksStage). Audio/chapters already produced by a prior run's enrich are
    carried onto the feed via the record store (merge_persisted), so the render still shows them —
    only *this* run's new encodes/chapters defer to the next deploy (graceful, like missing
    audio)."""
    return [LinksStage()]


def enrich_stages() -> list[EnrichmentStage]:
    """The *expensive*, deferrable stages that run in the heavy phase after the deploy: chapter
    scraping (per-item network), timeline planning (silence + concat planners), and
    audio encoding (download+ffmpeg), bounded by the wall-clock ``stop`` window.

    Ordering: ``chapters`` → ``timeline`` → ``audio`` (each feeds the next's spec hash)."""
    from citypods.concat import SwagitConcatPlanner
    from citypods.silence import SilencePlanner

    return [
        ChaptersStage(),
        TimelineStage(planners=[SwagitConcatPlanner(), SilencePlanner()]),
        RemapStage(),
        AudioStage(),
        TranscriptStage(),
    ]


def run_stages(
    provider,
    city: City,
    episodes: list[Episode],
    stages: list[EnrichmentStage],
    ctx: StageContext,
) -> list[StageStats]:
    out: list[StageStats] = []
    for stage in stages:
        print(
            f"[enrich] stage start slug={city.slug} provider={city.provider} "
            f"stage={stage.name} episodes={len(episodes)}",
            flush=True,
        )
        t0 = time.perf_counter()
        stat = stage.process(provider, city, episodes, ctx)
        stat.seconds = time.perf_counter() - t0
        print(
            f"[enrich] stage done slug={city.slug} provider={city.provider} stage={stage.name} "
            f"ran={stat.ran} reused={stat.reused} queued={stat.skipped} "
            f"errors={len(stat.errors)} seconds={stat.seconds:.1f}",
            flush=True,
        )
        out.append(stat)
    return out
