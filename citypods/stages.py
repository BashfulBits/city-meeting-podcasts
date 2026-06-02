"""Enrichment-stage pipeline — the extensible backbone for per-episode features.

Each meeting passes through an ordered list of stages; adding a feature
(transcript, auto-summary, chapter markers, resource links) means adding a stage, not
rewiring the build. Stages mirror the provider registry: a small protocol + a default list.

Two invariants make this safe and incremental (see records.py):

  * **Ordering matters.** A stage whose output feeds the audio bytes (e.g. ``chapters``)
    must run *before* ``audio``, because the audio object is content-addressed by
    ``audio_spec_hash`` which includes chapters. A stage that only affects the feed (e.g.
    ``summary``) can run after. ``default_stages()`` encodes the order.

  * **Own budget per stage.** ASR/LLM/audio work is expensive and rate-limited, so each
    stage draws from its own per-run ``GlobalBudget`` (via ``StageContext.budgets``) and a
    per-source cap. Anything not done this run is picked up on the next — the same gradual
    backfill the audio pipeline already uses, now for every feature.

A stage mutates episodes in place; downstream the split hashes pick up the change
automatically (``audio_spec_hash`` -> re-encode, ``feed_content_hash`` -> re-render).
"""

from __future__ import annotations

import collections
import time
from dataclasses import dataclass, field
from typing import Protocol

from citypods.bodies import body_key, canonical_body
from citypods.media import GlobalBudget, MaterializeStats, materialize_audio
from citypods.models import City, Episode
from citypods.records import AUDIO_PIPELINE_VERSION


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
    """Shared resources + budgets passed to every stage for one build."""

    storage: object | None
    ffmpeg: object
    max_kbps: int
    per_source_budget: int
    dry_run: bool
    budgets: dict[str, GlobalBudget | None] = field(default_factory=dict)


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
            budget=ctx.per_source_budget,
            max_kbps=ctx.max_kbps,
            resolve_media_url=lambda ep: provider.resolve_media_url(ep, city.source),
            global_budget=ctx.budgets.get(self.name),
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


class ChaptersStage:
    """Fetch agenda-item chapter markers (and a transcript link) for providers that expose
    them. Audio-affecting, so it runs *before* ``audio``: chapters are part of
    ``audio_spec_hash``, so adding them changes the spec and the next audio pass re-encodes the
    file with embedded markers — spread over runs by the audio budget, exactly like backfill.

    Each chapter fetch is one network call, so the stage draws from its own ``"chapters"``
    budget (global + per-source) and defers the rest to later runs. No-op for providers without
    a ``fetch_chapters`` method.
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
        budget = ctx.budgets.get(self.name)
        remaining = ctx.per_source_budget
        for ep in _materialize_set(episodes, city.max_episodes):
            if ep.chapters:  # already captured; chapters don't change once set
                stats.reused += 1
                continue
            if remaining <= 0 or (budget is not None and not budget.take()):
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
            links = {k: v for k, v in links.items() if v}
            if links != (ep.links or {}):
                ep.links = links
                stats.ran += 1
            else:
                stats.reused += 1
        return stats


def default_stages() -> list[EnrichmentStage]:
    """Ordered: audio-affecting stages (``chapters``) must precede ``audio`` so a chapter
    change re-encodes; feed-only stages (``summary``, ``links``) follow it."""
    return [ChaptersStage(), AudioStage(), LinksStage()]


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
