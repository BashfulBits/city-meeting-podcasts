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
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Protocol

import re

from citypods.bodies import body_key, canonical_body
from citypods.media import MaterializeStats, materialize_audio
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
            resolve_media_url=lambda ep: provider.resolve_media_url(ep, city.source),
            stop=ctx.stop,
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
        for ep in _materialize_set(episodes, city.max_episodes):
            if not self.planners:
                # No planners → identity path; ep.timeline stays as-is (None == identity).
                stats.reused += 1
                continue

            # Already planned by this exact planner set+versions → don't recompute. A stale
            # signature (older set) falls through and re-plans.
            if ep.timeline is not None and ep.timeline.version == sig:
                stats.reused += 1
                continue

            # Planning may be an expensive, restartable ffmpeg pass, so gate it on the shared
            # stop signal exactly like an encode — deferred to a later run, not an error.
            if ctx.stop is not None and ctx.stop():
                stats.skipped += 1
                continue

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
                stats.ran += 1
            else:
                # No planner fired → identity path; ep.timeline stays None (== identity).
                stats.reused += 1

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
      - ``"served"``: remapped to the served enclosure clock.

    Cut-span chapters (whose start falls in a removed silence gap) are dropped by
    :func:`~citypods.timeline.remap` — they would scrub to nothing in the served file.
    Chapters whose ``end`` was cut get ``end=None``; renderers clamp to the next start.
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
            ep.chapters = remap(ep.timeline, ep.chapters, source_id=source_id)
            ep.chapters_basis = "served"
            stats.ran += 1
        return stats


def _needs_chapter_remap(ep: Episode) -> bool:
    """True when ep.chapters need to be remapped from source-time to served-time."""
    if ep.chapters_basis == "served":
        return False  # already done
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


def default_stages() -> list[EnrichmentStage]:
    """Ordered: audio-affecting stages must precede ``audio`` so a change re-encodes;
    feed-only stages (``links``) follow it. This is the full list — used by a one-shot
    ``citypods build`` (local dev / PR preview / tests). Production splits it across two
    phases via :func:`render_stages` + :func:`enrich_stages` (see build).

    Ordering invariant: ``chapters`` → ``timeline`` → ``remap`` → ``audio``.
    Chapters must arrive (source-time) before timeline plans the EDL; remap converts
    them to served-time before audio embeds them as M4A markers."""
    return [ChaptersStage(), TimelineStage(), RemapStage(), AudioStage(), LinksStage()]


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
    scraping (per-item network), timeline planning (once silence/concat planners land), and
    audio encoding (download+ffmpeg), bounded by the wall-clock ``stop`` window.

    Ordering: ``chapters`` → ``timeline`` → ``audio`` (each feeds the next's spec hash)."""
    return [ChaptersStage(), TimelineStage(), RemapStage(), AudioStage()]


def run_stages(
    provider,
    city: City,
    episodes: list[Episode],
    stages: list[EnrichmentStage],
    ctx: StageContext,
) -> list[StageStats]:
    out: list[StageStats] = []
    for stage in stages:
        t0 = time.perf_counter()
        stat = stage.process(provider, city, episodes, ctx)
        stat.seconds = time.perf_counter() - t0
        out.append(stat)
    return out
