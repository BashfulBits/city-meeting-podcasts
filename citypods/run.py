"""Build orchestration: source-level enrichment, per-feed rendering, and writing docs/.

Two phases per build:
  1. **Per source** (once, shared across a city's combined + per-board feeds): fetch episodes,
     assign stable uids, merge persisted records, (re-)host audio content-addressed by spec,
     and persist the record store. See citypods/records.py.
  2. **Per feed/slug**: filter the source's episodes to one body, cap, and — unless the
     feed_content_hash is unchanged and outputs already exist — render feeds/page/artwork.
"""

from __future__ import annotations

import collections
import faulthandler
import hashlib
import json
import os
import shutil
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext as _nullcontext
from dataclasses import dataclass
from pathlib import Path

from citypods.artwork import render_cover
from citypods.bodies import filter_by_body
from citypods.compute import DispatchCoordinator, make_compute
from citypods.config import (
    RESERVED_PUBLIC_DIRS as _RESERVED_DOC_NAMES,
)
from citypods.config import (
    filter_city_configs,
    load_backlog_policy,
    load_city_configs,
    load_site_config,
)
from citypods.feeds import build_rss, chapters_json, chapters_url, has_items
from citypods.h16_identity import H16IdentityTracker
from citypods.http import HOST_LIMITER
from citypods.media import (
    DEFAULT_SOURCE_MEDIA_MAX_BYTES,
    CommandFfmpeg,
    FfmpegRunner,
    HostedKeysCache,
    ProviderTransportTelemetry,
    SourceCache,
    probe_hosted_audio_duration_seconds,
)
from citypods.models import AgendaRecord, City, Episode
from citypods.ops.workqueue import (
    build_manifest,
    load_manifest,
    order_cities_by_policy,
    overlay_leases,
    save_manifest,
)
from citypods.progress import PROGRESS, format_snapshot
from citypods.provider_leases import DISTRIBUTED_PROVIDER_LEASES
from citypods.providers import get_provider
from citypods.providers.base import ProviderError
from citypods.records import (
    assign_uids,
    attach_auxiliary_agenda_links,
    episode_to_record,
    feed_content_hash,
    load_calendar_records,
    load_records,
    meeting_page_hash,
    merge_calendar_backfill,
    merge_calendar_records,
    merge_persisted,
    merge_records,
    migrate_legacy_manifests,
    protected_blocks_for_lane,
    prune_archive,
    reconcile_cross_feed_episodes,
    record_to_episode,
    save_calendar_records,
    save_records,
    source_key,
)
from citypods.resources import (
    MemoryReservation,
    NativeWorkGate,
    ResourceAdmission,
    current_snapshot,
    snapshot_string,
)
from citypods.search import build_search_index
from citypods.security import SecurityError
from citypods.seeds import merge_seed_episodes
from citypods.sharding import create_shard_plan, episodes_for_shard, load_shard_plan
from citypods.site import (
    render_city_archive_page,
    render_city_page,
    render_index,
    render_meeting_page,
    render_redirect_feed,
    render_redirect_page,
    render_search_page,
)
from citypods.stages import (
    ASR_PIPELINE_VERSION,
    LANE_STAGES,
    StageContext,
    _materialize_set,
    default_stages,
    enrich_stages,
    render_stages,
    run_stages,
)
from citypods.state import (
    build_fingerprint,
    load_etag_cache,
    resolve_state_dir,
    save_etag_cache,
)
from citypods.statesync import (
    pull_state,
    push_asr_runtime_log_merged,
    push_calendar_records_merged,
    push_records_merged,
    push_state,
    push_transcript_quality_log_merged,
    reconcile_state,
)
from citypods.storage import make_storage
from citypods.transcript_quality import load_quality_config, load_quality_routes

# Retention caps for the append-only archive (issue #109). Deliberately set arbitrarily high:
# nothing is pruned in normal operation, but the lever exists so retention can be ratcheted down
# later (the admin/usage report shows the storage cost of keeping old recordings around).
DEFAULT_MAX_ARCHIVE_ITEMS = 5000
DEFAULT_MAX_ARCHIVE_AGE_YEARS = 1000.0
_FAST_EXIT_RUN_EVENTS = {"push", "workflow_dispatch"}


class SourcePipeline:
    """Fetch + enrich each distinct source once per build and share the result across all of
    its per-board feeds. Work is serialized per source key (so N board feeds don't re-fetch /
    re-enrich the same source concurrently); different sources still run in parallel.

    Enrichment runs an ordered list of stages (see citypods/stages.py); audio is the only
    built-in stage today, but transcript/summary/chapters/links slot in without touching this.
    """

    def __init__(
        self,
        *,
        state_dir: Path,
        stages,
        ctx: StageContext,
        max_archive_items: int = DEFAULT_MAX_ARCHIVE_ITEMS,
        max_archive_age_years: float = DEFAULT_MAX_ARCHIVE_AGE_YEARS,
        persist_records: bool = True,
    ):
        self.state_dir = state_dir
        self.stages = stages
        self.ctx = ctx
        self.max_archive_items = max_archive_items
        self.max_archive_age_years = max_archive_age_years
        # The render phase writes only docs/: it persists no records, so a stale render push can't
        # clobber audio/transcripts written by the separate enrich workflow (review/12 §H6/H11b).
        self.persist_records = persist_records
        self.h16_identity = H16IdentityTracker(
            storage=ctx.storage,
            max_kbps=ctx.max_kbps,
            loudness_profile=ctx.loudness_profile,
            processing_profile=ctx.audio_processing_profile,
            enabled=ctx.lane == "audio",
        )
        self._cache: dict[str, list[Episode]] = {}
        self._cache_cities: dict[str, City] = {}
        # A companion's no-video rows live beside, but never inside, episodes.json.
        self._calendar_cache: dict[str, list[AgendaRecord]] = {}
        self._calendar_store: dict[str, dict[str, AgendaRecord]] = {}
        self._aggregate_persist: dict[str, list[Episode]] = {}
        self._notes: dict[str, str] = {}
        self._locks: dict[str, threading.Lock] = collections.defaultdict(threading.Lock)
        self._guard = threading.Lock()
        # Per-stage cost totals across all sources this run (for run history / projection).
        self.stage_totals: dict[str, dict] = collections.defaultdict(
            lambda: {
                "ran": 0,
                "encoded": 0,
                "credited": 0,
                "aligned": 0,
                "transcribed": 0,
                "reused": 0,
                "backlog": 0,
                "seconds": 0.0,
                "bytes": 0,
                "errors": 0,
                "error_samples": [],
                "rate_limited": 0,
                "asr_migration_copied": 0,
                "asr_migration_already_present": 0,
                "asr_migration_missing": 0,
                "asr_migration_regenerated": 0,
                "defer_reasons": {},
                "defer_samples": [],
            }
        )

    def _hydrate_calendar(self, key: str) -> dict[str, AgendaRecord]:
        """Load and cache the durable calendar catalog for ``key``.

        Callers hold the corresponding source lock, so the store and list cache remain
        consistent with the on-disk calendar snapshot.
        """
        calendar = load_calendar_records(self.state_dir, key)
        self._calendar_store[key] = calendar
        self._calendar_cache[key] = list(calendar.values())
        return calendar

    def fetch_merge(self, city: City, key: str) -> tuple[object, list[Episode], dict, int]:
        """Fetch the source + merge persisted records + migrate legacy manifests. The cheap
        prepare step shared by ``enrich`` (per-source) and the global orchestrator (PR3).
        Returns ``(provider, episodes, persisted, seeded)``. ``ProviderError`` propagates."""
        provider = get_provider(city.provider)
        episodes = merge_seed_episodes(city, provider.fetch_episodes(city.source))
        fresh_calendar: list[AgendaRecord] = []
        if city.aux_provider and city.aux_source:
            try:
                aux_provider = get_provider(city.aux_provider)
                fetch_calendar_index = getattr(aux_provider, "fetch_calendar_index", None)
                if callable(fetch_calendar_index):
                    index = fetch_calendar_index(city.aux_source)
                    fresh_calendar = list(index.records)
                else:
                    fetch_agenda_index = getattr(aux_provider, "fetch_agenda_index", None)
                    source_records = (
                        fetch_agenda_index(city.aux_source)
                        if callable(fetch_agenda_index)
                        else aux_provider.fetch_episodes(city.aux_source)
                    )
                    fresh_calendar = [
                        record
                        if isinstance(record, AgendaRecord)
                        else AgendaRecord(
                            body=record.body or "",
                            title=record.title,
                            published=record.published,
                            links=dict(record.links or {}),
                        )
                        for record in source_records
                        if record.body
                    ]
                merge_calendar_backfill(episodes, fresh_calendar)
                print(
                    f"[enrich] companion fetched slug={city.slug} provider={city.aux_provider} "
                    f"source={key} records={len(fresh_calendar)} "
                    f"recorded={sum(bool(record.video_guid) for record in fresh_calendar)}",
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001 — a companion is strictly best-effort
                # A companion is additive: preserve its last-known calendar state and
                # never suppress primary archive discovery when it is temporarily down.
                print(
                    f"[enrich] companion failed slug={city.slug} provider={city.aux_provider} "
                    f"source={key} error={exc}",
                    flush=True,
                )
        print(
            f"[enrich] source fetched slug={city.slug} provider={city.provider} "
            f"source={key} episodes={len(episodes)}",
            flush=True,
        )
        assign_uids(city, episodes)
        persisted = load_records(self.state_dir, key)
        merge_persisted(episodes, persisted)
        self._cache_cities[key] = city
        if city.source.get("aggregate"):
            episodes, complete, reconciled = self.reconcile_aggregate(city, episodes)
            self._aggregate_persist[key] = complete
            if reconciled:
                print(
                    f"[enrich] cross-feed reconciled slug={city.slug} duplicates={reconciled}",
                    flush=True,
                )
        persisted_calendar = load_calendar_records(self.state_dir, key)
        assign_uids(city, fresh_calendar)
        calendar = merge_calendar_records(persisted_calendar, fresh_calendar)
        attach_auxiliary_agenda_links(episodes, list(calendar.values()))
        if city.aux_provider or calendar:
            self._calendar_store[key] = calendar
            self._calendar_cache[key] = list(calendar.values())
        seeded = migrate_legacy_manifests(self.state_dir, episodes)
        self.h16_identity.capture(city, episodes)
        return provider, episodes, persisted, seeded

    def reconcile_aggregate(
        self, city: City, episodes: list[Episode]
    ) -> tuple[list[Episode], list[Episode], int]:
        """Reconcile an aggregate source against already-prepared dedicated feeds for its city."""
        canonical_by_key = {
            key: archive
            for key, archive in self._cache.items()
            if key != source_key(city)
            and (peer := self._cache_cities.get(key)) is not None
            and peer.city_entity == city.city_entity
            and peer.provider == city.provider
            and not peer.source.get("aggregate")
        }
        unique, complete, reconciled = reconcile_cross_feed_episodes(
            canonical_by_key.values(), episodes
        )
        if reconciled and self.persist_records and not self.ctx.dry_run:
            # The aggregate may carry links absent from a dedicated view. Persist those link-only
            # enrichments back to the canonical source store without changing its UID or audio key.
            for key, archive in canonical_by_key.items():
                save_records(
                    self.state_dir,
                    key,
                    {episode.uid: episode_to_record(episode) for episode in archive if episode.uid},
                )
        return unique, complete, reconciled

    def fetch_merge_from_records(
        self, city: City, key: str, reason: Exception
    ) -> tuple[object, list[Episode], dict, int]:
        """Prepare a source from the persisted archive after a transient fetch failure.

        Transcript-only ASR lanes operate on already-hosted audio recorded in ``episodes.json``;
        they do not need fresh provider metadata to transcribe existing audio.  If the provider
        refresh fails, load the last-known archive and let the transcript pass continue.
        """
        provider = get_provider(city.provider)
        persisted = load_records(self.state_dir, key)
        if not persisted:
            raise ProviderError(str(reason)) from reason
        episodes = [record_to_episode(rec) for rec in persisted.values()]
        self._hydrate_calendar(key)
        self._notes[key] = f"stale provider fetch failed: {reason}"
        print(
            f"[enrich] source stale slug={city.slug} provider={city.provider} "
            f"source={key} archive={len(episodes)} reason={reason}",
            flush=True,
        )
        return provider, episodes, persisted, 0

    def accumulate_stats(self, stats: list) -> None:
        """Fold a ``run_stages`` result into the run-wide per-stage totals (thread-safe)."""
        with self._guard:
            for s in stats:
                t = self.stage_totals[s.name]
                t["ran"] += s.ran
                t["encoded"] += s.encoded
                t["credited"] += s.credited
                t["aligned"] += s.aligned
                t["transcribed"] += s.transcribed
                t["reused"] += s.reused
                t["backlog"] += s.skipped
                t["seconds"] += s.seconds
                t["bytes"] += s.bytes_written
                t["errors"] += len(s.errors)
                if s.errors and len(t["error_samples"]) < 3:
                    t["error_samples"].extend(s.errors[: 3 - len(t["error_samples"])])
                t["rate_limited"] += s.rate_limited
                t["asr_migration_copied"] += s.asr_migration_copied
                t["asr_migration_already_present"] += s.asr_migration_already_present
                t["asr_migration_missing"] += s.asr_migration_missing
                t["asr_migration_regenerated"] += s.asr_migration_regenerated
                for reason, count in s.defer_reasons.items():
                    t["defer_reasons"][reason] = t["defer_reasons"].get(reason, 0) + count
                if s.defer_samples and len(t["defer_samples"]) < 5:
                    t["defer_samples"].extend(s.defer_samples[: 5 - len(t["defer_samples"])])

    def h16_availability_summary(self) -> dict:
        """Census of durable media-availability verdicts across this run's known catalog (H16 PR3).

        Counted over the per-source archives the run loaded, so it reflects the current
        classification (persisted verdicts + this run's fresh ones), not just freshly-touched
        episodes. ``withheld`` is the count kept out of feeds; ``by_state`` breaks down the
        effective state (operator overrides included)."""
        by_state: dict[str, int] = collections.defaultdict(int)
        withheld = 0
        classified = 0
        seen: set[str] = set()
        with self._guard:
            archives = list(self._cache.values())
        for episodes in archives:
            for ep in episodes:
                av = ep.media_availability
                if av is None:
                    continue
                # One verdict per stable uid (a meeting may appear under several source views).
                marker = ep.uid or ep.guid
                if marker in seen:
                    continue
                seen.add(marker)
                classified += 1
                by_state[av.effective_state()] += 1
                if av.is_withheld():
                    withheld += 1
        return {
            "classified": classified,
            "withheld": withheld,
            "by_state": dict(sorted(by_state.items())),
        }

    def persist_source(
        self, key: str, episodes: list[Episode], persisted: dict, *, notes: list[str]
    ) -> list[Episode]:
        """Build + persist the append-only record store for one source, cache the rendered
        archive, and return it. Shared by ``enrich`` and the global orchestrator (PR3)."""
        # Copy notes so a repeat persist of the same source (GH#377 incremental persistence: once
        # after the audio pass, again after the transcript pass) doesn't append "{n} archived"
        # twice to the caller's list — keeps repeat persistence idempotent.
        notes = list(notes)
        self.h16_identity.verify(key, episodes)
        # Append-only archive (issue #109): merge this run's freshly-enriched records over
        # the persisted store (fresh wins on uid) instead of replacing it, so a meeting that
        # left the provider window keeps its record + audio. Bounded by the (high) retention
        # caps so it never grows truly unbounded.
        fresh = {ep.uid: episode_to_record(ep) for ep in episodes if ep.uid}
        combined = prune_archive(
            merge_records(persisted, fresh),
            max_items=self.max_archive_items,
            max_age_years=self.max_archive_age_years,
        )
        archived = len(combined) - len(fresh)
        if archived > 0:
            notes.append(f"{archived} archived")
        if not self.ctx.dry_run and self.persist_records:
            save_records(self.state_dir, key, combined)
            if key in self._calendar_store:
                save_calendar_records(self.state_dir, key, self._calendar_store[key])

        # Render from the full archive: prefer this run's in-memory enriched Episode for a
        # uid, else rehydrate a persisted-only one (dropped from the provider window).
        fetched_by_uid = {ep.uid: ep for ep in episodes if ep.uid}
        archive = [
            fetched_by_uid.get(uid) or record_to_episode(rec) for uid, rec in combined.items()
        ]
        self._cache[key] = archive
        self._notes[key] = ", ".join(notes)
        return archive

    def enrich(self, city: City) -> list[Episode]:
        key = source_key(city)
        with self._guard:
            lock = self._locks[key]
        with lock:
            if key in self._cache:
                print(
                    f"[enrich] source cache-hit slug={city.slug} "
                    f"provider={city.provider} source={key}",
                    flush=True,
                )
                return self._cache[key]
            t0 = time.perf_counter()
            thread_name = threading.current_thread().name
            print(
                f"[enrich] source start slug={city.slug} provider={city.provider} "
                f"source={key} thread={thread_name}",
                flush=True,
            )
            provider, episodes, persisted, seeded = self.fetch_merge(city, key)
            if self.ctx.lane != "audio":
                _normalize_episode_durations_for_dispatch(
                    key,
                    episodes,
                    storage=self.ctx.storage,
                    ffmpeg_binary=getattr(self.ctx.ffmpeg, "binary", "ffmpeg"),
                    allow_probe=not self.ctx.dry_run,
                    stop=self.ctx.stop,
                )
            stats = run_stages(provider, city, episodes, self.stages, self.ctx)
            notes = [s.note() for s in stats if s.note()]
            self.accumulate_stats(stats)
            if seeded:
                notes.append(f"{seeded} legacy")
            persist_episodes = self._aggregate_persist.pop(key, episodes)
            archive = self.persist_source(key, persist_episodes, persisted, notes=notes)
            if city.source.get("aggregate"):
                # The durable source archive retains suppressed observations, while callers
                # receive the aggregate's deduplicated public projection.
                self._cache[key] = episodes
                archive = episodes
            elapsed = time.perf_counter() - t0
            print(
                f"[enrich] source done slug={city.slug} provider={city.provider} source={key} "
                f"archive={len(archive)} seconds={elapsed:.1f}",
                flush=True,
            )
            return archive

    def note(self, city: City) -> str:
        return self._notes.get(source_key(city), "")

    def calendar_records(self, city: City) -> list[AgendaRecord]:
        """Return the source's durable non-podcast calendar catalog for this feed."""
        key = source_key(city)
        with self._guard:
            lock = self._locks[key]
        with lock:
            if key not in self._calendar_cache:
                self._hydrate_calendar(key)
            return self._calendar_cache[key]

    def render_from_records(self, city: City) -> list[Episode]:
        """Load the city's last-known archive from the record store with **no provider connection**
        — the ``--no-refresh`` render path (PR preview). Unlike :meth:`archive_from_records` (the
        transient-failure fallback, which errors on an empty store), an empty store here is fine and
        returns ``[]`` (renders an empty feed). No enrich stages, no persist."""
        key = source_key(city)
        with self._guard:
            lock = self._locks[key]
        with lock:
            if key in self._cache:
                return self._cache[key]
            records = load_records(self.state_dir, key)
            archive = [record_to_episode(rec) for rec in records.values()]
            if city.source.get("aggregate"):
                archive = [
                    episode
                    for episode in archive
                    if not (episode.integrity or {}).get("aggregate_suppressed")
                ]
            self._cache[key] = archive
            self._hydrate_calendar(key)
            return archive

    def archive_from_records(self, city: City, reason: Exception) -> list[Episode]:
        """Render fallback for transient provider fetch failures.

        The production render phase should prefer publishing the last known-good archive over
        failing the whole Pages deploy when one provider endpoint times out. Heavy enrich/all
        phases still fetch live data and report failures normally.
        """
        key = source_key(city)
        with self._guard:
            lock = self._locks[key]
        with lock:
            if key in self._cache:
                print(
                    f"[render] source cache-hit stale slug={city.slug} "
                    f"provider={city.provider} source={key}",
                    flush=True,
                )
                return self._cache[key]
            records = load_records(self.state_dir, key)
            if not records:
                raise ProviderError(str(reason)) from reason
            archive = [record_to_episode(rec) for rec in records.values()]
            if city.source.get("aggregate"):
                archive = [
                    episode
                    for episode in archive
                    if not (episode.integrity or {}).get("aggregate_suppressed")
                ]
            self._cache[key] = archive
            self._hydrate_calendar(key)
            self._notes[key] = f"stale provider fetch failed: {reason}"
            print(
                f"[render] source stale slug={city.slug} provider={city.provider} "
                f"source={key} archive={len(archive)} reason={reason}",
                flush=True,
            )
            return archive


@dataclass
class CityResult:
    slug: str
    status: str  # "built" | "skipped" | "error"
    episode_count: int = 0
    detail: str = ""
    has_audio: bool = True
    has_video: bool = False


@dataclass
class DurationNormalizationStats:
    normalized_from_probe: int = 0
    fallback_from_timeline: int = 0
    probe_failed: int = 0
    missing_after_normalization: int = 0


def _normalize_episode_durations_for_dispatch(
    source_key: str,
    episodes: list[Episode],
    *,
    storage,
    ffmpeg_binary: str,
    allow_probe: bool,
    stop: Callable[[], bool] | None = None,
    log: Callable[[str], None] | None = None,
) -> DurationNormalizationStats:
    """Heal missing served durations before transcript dispatch/work planning."""
    from citypods.durations import episode_served_duration_seconds, set_served_duration_seconds

    emit = log or (lambda msg: print(msg, flush=True))
    stats = DurationNormalizationStats()
    for ep in episodes:
        if episode_served_duration_seconds(ep) is not None:
            continue
        uid = ep.uid or ep.guid
        normalized = False
        probe_attempted = False
        if allow_probe and ep.hosted_audio_url and ep.audio_key:
            if stop is not None and stop():
                break
            probe_attempted = True
            try:
                probed, basis = probe_hosted_audio_duration_seconds(
                    storage,
                    ep.audio_key,
                    ffmpeg_binary=ffmpeg_binary,
                )
            except Exception:
                probed, basis = None, "probe-exception"
            if probed is not None and probed > 0:
                set_served_duration_seconds(ep, probed)
                stats.normalized_from_probe += 1
                emit(
                    f"duration_normalized_from_probe source={source_key} uid={uid} "
                    f"seconds={probed:.3f} basis={basis or 'probe'}"
                )
                normalized = True
            elif probe_attempted:
                stats.probe_failed += 1
                emit(
                    f"duration_probe_failed source={source_key} uid={uid} "
                    f"basis={basis or 'probe-failed'}"
                )
        if not normalized and episode_served_duration_seconds(ep) is None:
            stats.missing_after_normalization += 1
            emit(
                f"duration_missing_after_normalization source={source_key} uid={uid} "
                f"hosted={bool(ep.hosted_audio_url)} "
                f"timeline={bool(ep.timeline and ep.timeline.segments)}"
            )
    return stats


def _city_outputs_exist(output_dir: Path, slug: str) -> bool:
    # audio_feed.xml is the always-present feed (video may be absent for HLS providers).
    return (output_dir / slug / "audio_feed.xml").exists()


def _process_city(
    city: City,
    base_url: str,
    output_dir: Path,
    cache: dict,
    request_delay: float,
    dry_run: bool,
    pipeline: SourcePipeline,
    site_config: dict,
    fingerprint: str,
    render: bool = True,
    no_refresh: bool = False,
) -> tuple[CityResult, dict | None]:
    """Enrich the city (runs the pipeline's stages) and, when ``render`` is set, write its feeds/
    pages. In the enrich phase ``render`` is False: the expensive stages still run and persist via
    ``pipeline.enrich``, but ``docs/`` is left untouched. When ``no_refresh`` is set (render phase
    only), the city is rendered purely from the record store with no provider connection at all (PR
    preview). Returns the result and the new render-cache entry (or None to leave it unchanged)."""
    if request_delay and not no_refresh:
        time.sleep(request_delay)

    if no_refresh:
        # PR preview: render from the last-known archive, no provider fetch (immune to outages).
        episodes = pipeline.render_from_records(city)
    else:
        try:
            episodes = pipeline.enrich(city)
        except ProviderError as exc:
            if render:
                try:
                    episodes = pipeline.archive_from_records(city, exc)
                except ProviderError:
                    return CityResult(city.slug, "error", detail=str(exc)), None
            else:
                return CityResult(city.slug, "error", detail=str(exc)), None
        except SecurityError as exc:
            # SecurityError: a source/redirect URL was blocked by the SSRF gate (audit #S1) —
            # treat as a per-city error so one bad submission can't fail the whole build.
            return CityResult(city.slug, "error", detail=str(exc)), None

    # Filter the shared source archive to this feed's body, then cap to the most-recent
    # max_episodes (a feed never shows more; the archive itself retains far more — issue #109).
    archived = len(episodes)
    retained_eps = sorted(
        filter_by_body(episodes, city.source.get("body")),
        key=lambda e: e.published,
        reverse=True,
    )
    calendar_records = sorted(
        filter_by_body(pipeline.calendar_records(city), city.source.get("body")),
        key=lambda record: record.published,
        reverse=True,
    )
    feed_eps = retained_eps[: city.max_episodes]
    detail = f"{archived} archived"
    if archived > len(feed_eps):
        detail += f", {len(feed_eps)} after filter/cap"
    note = pipeline.note(city)
    if note:
        detail += f", {note}"

    has_audio = has_items(feed_eps, "audio")
    has_video = has_items(feed_eps, "video")

    # Enrich phase: the expensive stages already ran and persisted inside ``pipeline.enrich`` above;
    # we don't render or touch the render-cache. Return a result for logging / run history.
    if not render:
        return (
            CityResult(
                city.slug,
                "built",
                episode_count=len(feed_eps),
                detail=detail,
                has_audio=has_audio,
                has_video=has_video,
            ),
            None,
        )

    defaults = site_config.get("defaults", {}) if isinstance(site_config, dict) else {}
    meeting_pages_enabled = bool(defaults.get("meeting_pages", True))

    # feed_content_hash drives the re-render skip: hash the render-relevant fields + build
    # fingerprint. Unchanged + outputs present -> skip the (re-)render entirely. (Audio is a
    # separate concern, gated by audio_spec_hash inside materialize_audio.)
    content_hash = feed_content_hash(feed_eps, fingerprint)
    new_entry = {"content_hash": content_hash}
    cache_entry = cache.get(city.slug)
    feed_unchanged = (
        cache_entry is not None
        and cache_entry.get("content_hash") == content_hash
        and _city_outputs_exist(output_dir, city.slug)
    )
    meeting_outputs_changed = False

    if not dry_run:
        city_dir = output_dir / city.slug
        city_dir.mkdir(parents=True, exist_ok=True)
        if meeting_pages_enabled:
            page_cache = {}
            if isinstance(cache_entry, dict):
                page_cache = dict(cache_entry.get("meeting_pages") or {})
            rendered_page_cache = _write_meeting_pages(
                city_dir,
                city,
                retained_eps,
                base_url,
                site_config,
                page_cache,
            )
            new_entry["meeting_pages"] = rendered_page_cache
            meeting_outputs_changed = rendered_page_cache != page_cache
            archive_hash = _city_archive_hash(city, retained_eps, calendar_records, base_url)
            new_entry["archive_hash"] = archive_hash
            if cache_entry is None or cache_entry.get("archive_hash") != archive_hash:
                meeting_outputs_changed = True
                archive_dir = city_dir / "archive"
                archive_dir.mkdir(parents=True, exist_ok=True)
                (archive_dir / "index.html").write_text(
                    render_city_archive_page(
                        city,
                        base_url,
                        retained_eps,
                        calendar_records=calendar_records,
                        site_config=site_config,
                    )
                )
        if feed_unchanged and not meeting_outputs_changed:
            return (
                CityResult(
                    city.slug,
                    "skipped",
                    episode_count=len(feed_eps),
                    detail=detail + ", unchanged",
                    has_audio=has_audio,
                    has_video=has_video,
                ),
                new_entry,
            )

        _write_chapter_sidecars(city_dir, city, feed_eps, base_url)
        if has_audio:
            (city_dir / "audio_feed.xml").write_text(
                build_rss(
                    city,
                    feed_eps,
                    "audio",
                    base_url,
                    meeting_pages=meeting_pages_enabled,
                )
            )
        if has_video:
            (city_dir / "video_feed.xml").write_text(
                build_rss(
                    city,
                    feed_eps,
                    "video",
                    base_url,
                    meeting_pages=meeting_pages_enabled,
                )
            )
        else:
            (city_dir / "video_feed.xml").unlink(missing_ok=True)
        # Cover art: never overwrite a hand-committed artwork.jpg. Wordmark = the
        # deployment's domain (config-driven, so forks brand their own covers).
        artwork = city_dir / "artwork.jpg"
        if not artwork.exists():
            wordmark = (site_config.get("custom_domain") or "").removeprefix("www.")
            render_cover(city, artwork, wordmark=wordmark)
        (city_dir / "index.html").write_text(
            render_city_page(
                city,
                base_url,
                feed_eps,
                site_config=site_config,
                has_audio=has_audio,
                has_video=has_video,
                meeting_pages=meeting_pages_enabled,
            )
        )
        (city_dir / "meta.json").write_text(
            json.dumps(
                {
                    "slug": city.slug,
                    "episodes": len(feed_eps),
                    "has_audio": has_audio,
                    "has_video": has_video,
                },
                indent=2,
            )
            + "\n"
        )

    return (
        CityResult(
            city.slug,
            "built",
            episode_count=len(feed_eps),
            detail=detail,
            has_audio=has_audio,
            has_video=has_video,
        ),
        new_entry,
    )


def _try_preload_asr_model(defaults: dict, *, lane: str | None = None) -> None:
    """Pre-load the Whisper model into the process-level cache before workers start.

    Loading the float16 model peaks at ~5.5 GB RAM (float16 → float32 intermediate
    → int8 quantisation).  Doing this before the ``ThreadPoolExecutor`` opens means
    that peak resolves to the int8 steady-state (~800 MB) before any ffmpeg subprocesses
    are spawned — preventing OOM when high audio-encode concurrency coincides with model
    loading.

    ``lane`` selects which single model to warm so the two never co-load on a sharded runner
    (H6b): ``"align"`` pre-loads the stable-ts alignment model; ``"transcribe"`` / ``None`` (the
    combined enrich) pre-load the faster-whisper transcription model.
    """
    if not defaults.get("asr_enabled", True):
        return
    try:
        import math

        from citypods import asr as asr_mod

        model = str(defaults.get("asr_model", "large-v3-turbo"))
        compute_type = str(defaults.get("asr_compute_type", "int8"))
        asr_workers = int(defaults.get("asr_workers", 1))
        cpu_threads = max(1, math.ceil((os.cpu_count() or 4) / asr_workers))
        if lane == "align":
            print(f"[asr] pre-loading stable-ts align model {model} ({cpu_threads}t)…", flush=True)
            asr_mod._load_alignment_model(model, compute_type, cpu_threads)
        else:
            print(f"[asr] pre-loading {model} ({compute_type}, {cpu_threads} threads)…", flush=True)
            asr_mod.load_model(model, compute_type, cpu_threads)
        print("[asr] model ready — worker pool starting", flush=True)
    except Exception as exc:  # noqa: BLE001
        # Non-fatal: TranscriptStage will attempt load again per-source and record the error.
        print(f"[asr] pre-load failed (non-fatal): {exc}", flush=True)


def _resource_snapshot(root: Path) -> str:
    return snapshot_string(root)


def _heartbeat_interval_seconds() -> float:
    raw = os.environ.get("CITYPODS_HEARTBEAT_SECONDS")
    if raw is None:
        return 60.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 60.0


def _stall_dump_seconds() -> float:
    """No-progress threshold after which the heartbeat dumps thread stacks (0 disables)."""
    raw = os.environ.get("CITYPODS_STALL_DUMP_SECONDS")
    if raw is None:
        return 600.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 600.0


def _bool_default(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _resource_admission_from_defaults(defaults: dict, *, time_bounded: bool, dry_run: bool):
    enabled = _bool_default(
        defaults.get("resource_guard_enabled"),
        bool(os.environ.get("GITHUB_ACTIONS")),
    )
    if dry_run or not time_bounded or not enabled:
        return None
    min_available_mb = float(defaults.get("resource_guard_min_available_mb", 1536))
    max_load_per_cpu = float(defaults.get("resource_guard_max_load_per_cpu", 1.0))
    poll_seconds = float(defaults.get("resource_guard_poll_seconds", 10))
    return ResourceAdmission(
        min_available_bytes=int(min_available_mb * 1024 * 1024),
        max_load_per_cpu=max_load_per_cpu,
        poll_seconds=poll_seconds,
        log=lambda msg: print(msg, flush=True),
    )


class _ResourceHeartbeat:
    # Once a stack dump has fired, wait at least this long before dumping again — the same stall
    # would otherwise re-dump every tick and flood the log without adding information.
    _STALL_DUMP_COOLDOWN_SECONDS = 1800.0

    def __init__(
        self,
        *,
        enabled: bool,
        label: str,
        root: Path,
        interval_seconds: float,
        stall_dump_seconds: float = 0.0,
        native_work_gate: NativeWorkGate | None = None,
        resource_admission: ResourceAdmission | None = None,
    ):
        self.enabled = enabled and interval_seconds > 0
        self.label = label
        self.root = root
        self.interval_seconds = interval_seconds
        self.stall_dump_seconds = stall_dump_seconds
        self.native_work_gate = native_work_gate
        self.resource_admission = resource_admission
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.peak_load_per_cpu: float | None = None
        self.min_mem_avail_bytes: int | None = None
        self._last_stall_dump: float | None = None

    def __enter__(self) -> _ResourceHeartbeat:
        if not self.enabled:
            return self
        print(f"[{self.label}] heartbeat start {_resource_snapshot(self.root)}", flush=True)
        self._thread = threading.Thread(
            target=self._run,
            name="citypods-heartbeat",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        if not self.enabled:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        print(f"[{self.label}] heartbeat stop {_resource_snapshot(self.root)}", flush=True)
        self._sample()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self._tick()

    def _tick(self) -> None:
        """One heartbeat print cycle — separately callable (CR2-TS-10) so a test can exercise it
        deterministically instead of racing the background thread's own timing."""
        print(f"[{self.label}] heartbeat {_resource_snapshot(self.root)}", flush=True)
        print(f"[{self.label}] active work: {format_snapshot(PROGRESS.snapshot())}", flush=True)
        self._print_gate_and_lease_state()
        self._sample()
        self._maybe_dump_stalled_threads()

    def _print_gate_and_lease_state(self) -> None:
        """Surface *live* gate/lease occupancy each tick — ``total_wait_seconds`` and
        ``telemetry()`` are cumulative-for-the-run and can't show why the current tick is
        blocked (#376)."""
        if self.native_work_gate is not None:
            counts = self.native_work_gate.current_counts()
            if any(
                (
                    counts["audio_active"],
                    counts["audio_finalize_waiting"],
                    counts["asr_active"],
                    counts["asr_waiting"],
                )
            ):
                print(
                    f"[{self.label}] gate: "
                    f"audio_active={counts['audio_active']}/{counts['max_audio_active']} "
                    f"audio_finalize_waiting={counts['audio_finalize_waiting']} "
                    f"asr_active={counts['asr_active']} asr_waiting={counts['asr_waiting']} "
                    f"total_wait={self.native_work_gate.total_wait_seconds:.1f}s",
                    flush=True,
                )

        if self.resource_admission is not None:
            guard_waiting = self.resource_admission.current_waiting_counts()
            if guard_waiting:
                summary = " ".join(f"{kind}={n}" for kind, n in sorted(guard_waiting.items()))
                print(f"[{self.label}] resource guard: waiting {summary}", flush=True)

        waiting = DISTRIBUTED_PROVIDER_LEASES.current_waiting_counts()
        if waiting:
            telemetry = DISTRIBUTED_PROVIDER_LEASES.telemetry()
            for domain, waiting_count in sorted(waiting.items()):
                cum = telemetry.get(domain, {})
                print(
                    f"[{self.label}] leases: {domain} waiting={waiting_count} "
                    f"cum_wait={cum.get('lease_wait_seconds', 0.0):.1f}s "
                    f"renewals={cum.get('lease_renewals', 0)}",
                    flush=True,
                )

    def _maybe_dump_stalled_threads(self) -> None:
        if self.stall_dump_seconds <= 0:
            return
        if PROGRESS.longest_elapsed() < self.stall_dump_seconds:
            return
        now = time.monotonic()
        if (
            self._last_stall_dump is not None
            and now - self._last_stall_dump < self._STALL_DUMP_COOLDOWN_SECONDS
        ):
            return
        self._last_stall_dump = now
        print(
            f"[{self.label}] no progress for >={self.stall_dump_seconds:.0f}s on at least one "
            "tracked operation — dumping all thread stacks",
            flush=True,
        )
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)

    def _sample(self) -> None:
        try:
            snap = current_snapshot(self.root)
            cpus = snap.cpus or 1
            with self._lock:
                if snap.load1 is not None:
                    load_per_cpu = snap.load1 / cpus
                    if self.peak_load_per_cpu is None or load_per_cpu > self.peak_load_per_cpu:
                        self.peak_load_per_cpu = load_per_cpu
                if snap.mem_available_bytes is not None:
                    if (
                        self.min_mem_avail_bytes is None
                        or snap.mem_available_bytes < self.min_mem_avail_bytes
                    ):
                        self.min_mem_avail_bytes = snap.mem_available_bytes
        except Exception:
            pass


def _order_global_candidates(prepared: dict, policy) -> list[tuple[str, Episode]]:
    """Build the global candidate queue — each prepared source's materialized set — ordered
    across sources by the backlog policy. Pure + unit-testable. ``prepared`` maps each
    ``source_key`` to a dict with at least ``{"city": City, "episodes": list[Episode]}``."""
    from citypods.ops.workqueue import sort_key_for, workitem_from_episode

    candidates: list[tuple[str, Episode]] = []
    for key, st in prepared.items():
        city = st["city"]
        for ep in _materialize_set(
            st["episodes"], city.max_episodes, policy=policy, city_slug=city.slug
        ):
            candidates.append((key, ep))
    if policy is not None and policy.keys:
        keyfn = sort_key_for(policy)
        candidates.sort(
            key=lambda ke: keyfn(
                workitem_from_episode(ke[1], city_slug=prepared[ke[0]]["city"].slug)
            )
        )
    return candidates


def _run_enrich_global_queue(
    pipeline: SourcePipeline,
    cities: list[City],
    *,
    source_cache,
    max_workers: int,
    policy,
    owned_uids: dict[str, frozenset[str]] | None = None,
) -> list[CityResult]:
    """H5 PR3 — global two-pass enrich for the time-bounded heavy phase.

    Instead of the per-source pool (each city runs its full pipeline; expensive work serializes in
    thread-arrival order), this enumerates the whole backlog up front and processes it
    **newest-everywhere-first across all sources**:

    1. **Prepare** every unique source in parallel (fetch + merge records; no expensive stages).
    2. **Audio pass** — the on-runner work (``chapters→timeline→remap→audio``) over all candidate
       episodes in policy order, gated by the existing ``native_work_gate``/``stop``.
    3. **Transcript pass** — decoupled and *dispatch-not-await ready*: over episodes that now have
       hosted audio, in policy order. Runs faster-whisper on-runner today, but is structurally
       separate so an external (over-the-wall) backend replaces only this pass — the audio queue and
       feed rendering never assume in-run completion (H6b/H9).

    Records persist after the passes; content-addressed audio means a graceful ``stop`` (or a kill)
    never loses an encode — the object stays in storage and is re-credited next run.
    """
    ctx = pipeline.ctx
    # H6b lanes: the sharded workflows run one work class per job so the two ASR models never
    # co-load and audio encodes never share a runner with ASR. ``LANE_STAGES`` (the single source of
    # truth, also enforced inside ``run_stages``) decides which stages a lane runs: ``audio`` → the
    # audio chain only; ``transcribe``/``align`` → ``transcript`` only (the per-episode model choice
    # is gated inside TranscriptStage by ``ctx.lane``). ``None`` (combined enrich) runs both passes.
    # When a new lane lands (e.g. diarization — review/12 §H5) it gains an entry there, not here.
    allowed = LANE_STAGES.get(ctx.lane) if ctx.lane is not None else None
    # Document stages need a source-wide episode list: agenda-derived minutes attach to the
    # preceding same-body meeting, which a one-episode global-queue invocation cannot see.
    source_stage_names = {"links", "agenda_text", "minutes_text"}
    source_stages = [
        s
        for s in pipeline.stages
        if s.name in source_stage_names and (allowed is None or s.name in allowed)
    ]
    # Diarization consumes the minutes-derived roster as candidate vocabulary and the active
    # transcript, so it must run after the document stages *and* TranscriptStage's second pass.
    post_transcript = {"transcript", "diarize"}
    audio_stages = [
        s
        for s in pipeline.stages
        if s.name not in post_transcript | source_stage_names
        and (allowed is None or s.name in allowed)
    ]
    transcript_stages = [
        s
        for s in pipeline.stages
        if s.name in post_transcript and (allowed is None or s.name in allowed)
    ]

    # 1) Prepare every unique source (board feeds share a source_key → dedup so episodes don't
    #    double-count). ProviderError is captured per source and surfaces as that city's result.
    rep_city: dict[str, City] = {}
    for c in cities:
        rep_city.setdefault(source_key(c), c)
    prepared: dict[str, dict] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        fut_key = {
            pool.submit(pipeline.fetch_merge, city, key): key for key, city in rep_city.items()
        }
        for fut in fut_key:
            key = fut_key[fut]
            try:
                provider, episodes, persisted, seeded = fut.result()
            except (ProviderError, SecurityError) as exc:
                if ctx.lane in {"transcribe", "align"}:
                    try:
                        provider, episodes, persisted, seeded = pipeline.fetch_merge_from_records(
                            rep_city[key], key, exc
                        )
                    except ProviderError:
                        errors[key] = str(exc)
                        continue
                else:
                    errors[key] = str(exc)
                    continue
            notes: list[str] = [f"{seeded} legacy"] if seeded else []
            prepared[key] = {
                "city": rep_city[key],
                "provider": provider,
                "episodes": episodes,
                "persisted": persisted,
                "notes": notes,
                "persist_episodes": getattr(pipeline, "_aggregate_persist", {}).pop(key, episodes),
            }
    # Aggregate archive feeds are projections over the dedicated feeds. Reconcile them before
    # candidate selection so an overlapping recording is never materialized or persisted twice.
    for key, state in prepared.items():
        if not state["city"].source.get("aggregate"):
            continue
        canonical = [
            other["episodes"]
            for other_key, other in prepared.items()
            if other_key != key
            and other["city"].city_entity == state["city"].city_entity
            and other["city"].provider == state["city"].provider
            and not other["city"].source.get("aggregate")
        ]
        state["episodes"], state["persist_episodes"], reconciled = reconcile_cross_feed_episodes(
            canonical, state["episodes"]
        )
        if reconciled:
            print(
                f"[enrich] cross-feed reconciled slug={state['city'].slug} duplicates={reconciled}",
                flush=True,
            )
    if transcript_stages:

        def _normalize_prepared(item: tuple[str, dict]) -> None:
            key, st = item
            _normalize_episode_durations_for_dispatch(
                key,
                st["episodes"],
                storage=ctx.storage,
                ffmpeg_binary=getattr(ctx.ffmpeg, "binary", "ffmpeg"),
                allow_probe=not ctx.dry_run,
                stop=ctx.stop,
            )

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            list(pool.map(_normalize_prepared, prepared.items()))

    # 2) Global candidate queue: each source's materialized set, ordered across sources by policy.
    candidates = _order_global_candidates(prepared, policy)
    # Register every alias before worker submission so new duplicate artifacts choose one
    # deterministic source prefix regardless of which candidate thread reaches AudioStage first.
    for key, ep in candidates:
        ctx.audio_artifact_cache.register(prepared[key]["city"].provider, key, ep.uid or ep.guid)
    print(
        f"[enrich] global queue: {len(prepared)} source(s), {len(candidates)} candidate episode(s)"
        f"{f', {len(errors)} fetch error(s)' if errors else ''}",
        flush=True,
    )

    def _run_for(item: tuple[str, Episode], stages):
        key, ep = item
        st = prepared[key]
        try:
            stats = run_stages(st["provider"], st["city"], [ep], stages, ctx, quiet=True)
            pipeline.accumulate_stats(stats)
            return stats
        except Exception as exc:  # noqa: BLE001 — one bad episode must not abort the pass
            print(f"[enrich] global queue item failed source={key} uid={ep.uid}: {exc}", flush=True)
            return []

    # Links/documents are source-scoped rather than materialization-scoped.  In addition to
    # avoiding repeated document downloads for shared source views, this provides AgendaTextStage
    # the complete source archive it needs for conservative prior-meeting minutes inheritance.
    if source_stages:
        for st in prepared.values():
            stats = run_stages(
                st["provider"], st["city"], st["episodes"], source_stages, ctx, quiet=True
            )
            pipeline.accumulate_stats(stats)

    # 3) Passes. Submission order = global priority order; the native_work_gate still serializes
    #    the actual encodes (so the top-priority batch encodes first), and stages self-limit on
    #    ``stop`` (post-budget items count as backlog), so the maps drain fast once the window is
    #    spent. source_cache spans both passes (one download per episode).
    def _audio_task(item: tuple[str, Episode]):
        return _run_for(item, audio_stages)

    def _transcript_task(item: tuple[str, Episode]) -> None:
        _run_for(item, transcript_stages)

    def _persist_all() -> None:
        """Persist every prepared source's in-place-mutated episodes. Called after the audio pass
        (so audio-pass results survive a mid-run kill before the decoupled transcript pass even
        starts, GH#377) and again at the end (to capture transcripts). ``persist_source`` /
        ``merge_records`` are append-only and idempotent, so the repeat is safe."""
        for key, st in prepared.items():
            pipeline.persist_source(
                key,
                st.get("persist_episodes", st["episodes"]),
                st["persisted"],
                notes=st["notes"],
            )

    with source_cache or _nullcontext():
        if audio_stages:
            print(f"[enrich] audio pass: {len(candidates)} item(s) (newest-first)", flush=True)
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                list(pool.map(_audio_task, candidates))
            print("[enrich] audio pass done", flush=True)
            # Persist audio-pass results now, before the decoupled transcript pass — shrinks the
            # window in which a mid-run kill (SIGTERM/OOM/lost-comms) would lose every record update
            # for this run, not just killed ones (GH#377).
            _persist_all()
        if transcript_stages:
            # Decoupled from the audio pass: only episodes that now have hosted audio. Under a
            # per-episode transcribe plan (review/18 §3.3), also skip any uid this shard does not
            # own — its source is still loaded for render context, but a sibling shard handles it.
            tx = [
                item
                for item in candidates
                if item[1].hosted_audio_url
                and (owned_uids is None or item[1].uid in owned_uids.get(item[0], frozenset()))
            ]
            print(
                f"[enrich] transcript pass: {len(tx)} item(s) with audio (newest-first)", flush=True
            )
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                list(pool.map(_transcript_task, tx))
            print("[enrich] transcript pass done", flush=True)

    # 4) Persist each prepared source (episodes were mutated in place by the passes). Idempotent
    #    repeat of the post-audio-pass persist above; also captures the transcript-pass mutations.
    _persist_all()

    # 5) One CityResult per configured city, mirroring its source's outcome.  Transcript lanes are
    # best-effort backfill workers: a transient provider refresh failure for one shard means that
    # source simply has no ASR candidates this run.  Treat it as skipped/deferred so the worker can
    # persist sibling progress and the scheduled ASR lane completes cleanly instead of reddening on
    # an outage it cannot repair.  Audio/full enrich still report source-fetch errors as errors,
    # because those lanes own provider materialization work and should surface provider drift.
    transcript_only_lane = ctx.lane in {"transcribe", "align"}
    results: list[CityResult] = []
    for c in cities:
        key = source_key(c)
        if key in errors:
            if transcript_only_lane:
                results.append(
                    CityResult(c.slug, "skipped", detail=f"transcript lane deferred: {errors[key]}")
                )
            else:
                results.append(CityResult(c.slug, "error", detail=errors[key]))
        else:
            episodes = prepared.get(key, {}).get("episodes", [])
            results.append(CityResult(c.slug, "built", episode_count=len(episodes)))
    return results


def _build_impl(
    *,
    site_config_path: str | Path = "config/site_config.yml",
    config_dir: str | Path = "config",
    output_dir: str | Path = "docs",
    base_url: str | None = None,
    only_slug: str | None = None,
    dry_run: bool = False,
    ffmpeg: FfmpegRunner | None = None,
    chapters_cap: int | None = None,
    phase: str = "all",
    shard: tuple[int, int] | None = None,
    source: str | None = None,
    lane: str | None = None,
    no_refresh: bool = False,
    shard_plan_path: str | Path | None = None,
    state_snapshot_restored: bool = False,
    _compute_backend_holder: list[object] | None = None,
) -> list[CityResult]:
    """Build the site and/or backfill heavy enrichment, in one of three phases:

    * ``"all"`` (default) — run every stage and render: a one-shot build for local dev / the PR
      preview / tests. Time-bounded (graceful yield), as before.
    * ``"render"`` — the fast phase: cheap stages (links) + render ``docs/``, using audio/chapters
      already in the record store. NOT time-bounded (it always finishes in minutes); production
      deploys its output, then runs ``"enrich"``.
    * ``"enrich"`` — the heavy phase: expensive stages (chapters → audio, later transcripts) bounded
      by the wall-clock ``stop`` window. Does NOT render or touch ``docs/``; its output is picked up
      by the next run's ``"render"`` (graceful, like a not-yet-hosted episode).

    Splitting render from enrich is what lets a merge publish feeds/pages in ~minutes instead of
    waiting out the whole audio window (issue #63 follow-up).

    ``shard``/``source``/``lane`` drive the H6b sharded ``audio``/``asr`` workflows (enrich phase):
    ``shard=(k, n)`` keeps only the sources ``shard_assignment`` maps to shard ``k`` (a weighted,
    source-atomic partition over the sorted source_keys — disjoint, exhaustive, and never empty
    until ``#sources < n``); ``source`` keeps only that one ``source_key``; ``lane`` selects the
    work class (``audio`` / ``transcribe`` / ``align``). A sharded or source-scoped run pushes back
    **only** the records it owns (scoped ``push_state``) and does not reconcile (H11b hooks).
    ``shard_plan_path`` makes ownership come from one canonical pre-matrix snapshot instead of each
    shard recomputing it. ``state_snapshot_restored`` says that same snapshot is already present
    locally, so the shard skips the otherwise-redundant durable-state pull.

    ``no_refresh`` (render phase) renders each city purely from the record store and makes **no
    provider connections at all** — no ``detect_change``/``fetch_episodes``. Used by the PR preview:
    it verifies the render/build flow against the last-known state (restored from the build-state
    cache) without depending on live provider availability (which only timed out the preview, never
    added value — URL/contract validation lives in ``contracts.yml``). An empty store renders an
    empty feed, not an error."""
    if phase not in ("all", "render", "enrich"):
        raise ValueError(f"unknown build phase {phase!r}")
    if lane is not None and lane not in ("audio", "transcribe", "align"):
        raise ValueError(f"unknown lane {lane!r}")
    if shard_plan_path is not None and shard is None:
        raise ValueError("shard_plan_path requires shard=(K, N)")
    if state_snapshot_restored and shard_plan_path is None:
        raise ValueError("state_snapshot_restored requires a canonical shard plan")
    site_config = load_site_config(site_config_path)
    # Per-host concurrency caps (issue #39). Configure the process-global limiter once, before any
    # fetching, so both make_session and the ffmpeg fetch paths (media.py) honor it — bounding the
    # sharded burst that triggered the Granicus 403 / truncated-fetch storm.
    HOST_LIMITER.configure(site_config.get("provider_rate_limits", {}))
    cities = load_city_configs(config_dir, site_config.get("defaults", {}))
    if only_slug:
        cities = filter_city_configs(cities, only_slug)
        if not cities:
            raise ValueError(f"no feed or city entity with slug {only_slug!r}")
    # H6b source/shard selection (by source_key, so a city's combined + per-board feeds stay
    # together in one shard and one record store). ``scoped`` marks a partial run for statesync.
    scoped = bool(source or shard)
    if source:
        cities = [c for c in cities if source_key(c) == source]
        if not cities:
            raise ValueError(f"no source with key {source!r}")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    base_url = _resolve_base_url(base_url, site_config)

    state_dir = resolve_state_dir(site_config, output_dir)
    fingerprint = build_fingerprint(base_url)
    request_delay = float(site_config.get("request_delay_seconds", 0.1))
    max_workers = int(site_config.get("max_workers", 20))
    max_encodes_per_source = int(site_config.get("max_encodes_per_source", 1))
    defaults = site_config.get("defaults", {})
    native_audio_max_active = int(defaults.get("native_audio_max_active", 1))
    max_kbps = int(defaults.get("audio_max_kbps", 96))
    loudness_profile = str(defaults.get("audio_loudness_profile", ""))
    audio_processing_profile = str(defaults.get("audio_processing_profile", ""))

    # Per-episode transcribe ownership (review/18 §3.2): for an episode-unit plan this maps each
    # owned source to the subset of uids this shard transcribes/writes; None means source-atomic
    # (own every uid), preserving audio/align and unsharded behavior exactly.
    shard_owned_uids: dict[str, frozenset[str]] | None = None
    if shard is not None:
        k, n = shard
        if not (0 <= k < n):
            raise ValueError(f"shard {k}/{n} out of range (need 0 <= k < n)")
        plan_lane = lane or "audio"
        if shard_plan_path is not None:
            plan = load_shard_plan(shard_plan_path)
            plan_source_desc = f"loaded {shard_plan_path}"
        else:
            # Legacy/local path: compute once inside this process. Production ASR supplies the
            # immutable planner artifact so all four matrix jobs share one ownership decision.
            plan = create_shard_plan(
                cities,
                state_dir,
                lane=plan_lane,
                num_shards=n,
                defaults=defaults,
                asr_pipeline_version=ASR_PIPELINE_VERSION,
            )
            plan_source_desc = "computed in-process"
        owned, shard_owned_uids = episodes_for_shard(
            plan,
            lane=plan_lane,
            shard_index=k,
            num_shards=n,
            expected_sources={source_key(city) for city in cities},
        )
        uid_note = (
            f" uids={sum(len(u) for u in shard_owned_uids.values())}"
            if shard_owned_uids is not None
            else ""
        )
        print(
            f"shard plan: {plan_source_desc} lane={plan_lane} unit={plan.unit} "
            f"shard={k}/{n} sources={len(owned)}{uid_note}"
        )
        cities = [city for city in cities if source_key(city) in owned]
    # Backlog prioritization policy (H5). Built once per run so any windowed-recency horizon is
    # evaluated against a single ``now``. Empty/absent ⇒ behavior-preserving identity order.
    backlog_policy = load_backlog_policy(site_config)
    storage = make_storage(site_config, base_url, output_dir)
    DISTRIBUTED_PROVIDER_LEASES.configure(
        storage if not dry_run else None,
        site_config.get("provider_distributed_leases", {}),
        log=lambda msg: print(msg, flush=True),
        shard=f"{shard[0]}/{shard[1]}" if shard is not None else None,
    )
    # GPU/ASR execution backend (H13). ``local`` (in-process) by default; ``auto`` (H14a) returns a
    # DispatchCoordinator filling external free-tier GPU budgets first, overflowing to ``local``.
    # A dry run must never reach the live CAS budget ledger: pass storage=None so ``auto`` uses the
    # local-file budget path and a stray dispatch can't mutate the production R2 ledger.
    compute_backend = make_compute(
        site_config, state_dir=state_dir, storage=None if dry_run else storage
    )
    if _compute_backend_holder is not None:
        _compute_backend_holder.append(compute_backend)

    # Restore the durable state snapshot from the bucket (canonical) before loading any state,
    # so a missing/evicted actions/cache self-heals instead of losing derived artifacts.
    if not dry_run and not state_snapshot_restored:
        restored = pull_state(storage, state_dir)
        if restored:
            print(f"state: restored {restored} file(s) from durable storage")
    elif state_snapshot_restored:
        print("state: using canonical pre-matrix snapshot; durable restore already completed")
    # H14a: adopt the unexpired dispatch leases from the restored manifest, so a transcript a prior
    # run handed to an external worker (still in flight) isn't dispatched again this run.
    if isinstance(compute_backend, DispatchCoordinator):
        compute_backend.seed_leases(load_manifest(state_dir))
    cache = load_etag_cache(state_dir)

    # Phase wiring: which stages run, whether we render docs/, and whether the run is time-bounded.
    # Only the heavy phases ("enrich"/"all", which encode) carry the wall-clock budget + graceful
    # yield; "render" is cheap and must always finish so the deploy isn't gated on a budget.
    do_render = phase in ("all", "render")
    time_bounded = phase in ("all", "enrich")
    # H11b: render writes ONLY docs/ — it persists no records and never pushes/reconciles durable
    # state. The audio/ASR enrich workflows are the sole record writers, so a stale render push can
    # no longer silently erase a transcript/hosted-audio they wrote (the record-write race that the
    # render-only deploy split would otherwise open — review/12 §H6/H11b).
    persist_records = phase != "render"
    stages = {"render": render_stages, "enrich": enrich_stages, "all": default_stages}[phase]()

    # A time-bounded phase processes recordings (encode, chapter scrape) until a shared ``stop``
    # predicate goes True: the wall-clock window is spent, or a newer Build & Deploy run is queued
    # behind this one (graceful yield — let the next run take over without hard-cancelling the
    # in-flight deploy). No count budget: with variable per-recording cost, wall-clock is the bound.
    stop: StopSignal | None = None
    deadline: float | None = None
    asr_start_deadline: float | None = None
    asr_backstop_deadline: float | None = None
    if time_bounded:
        safety = float(defaults.get("budget_safety", 0.8))
        window_min = float(defaults.get("run_time_budget_minutes", 0))
        if lane in {"transcribe", "align"}:
            start_cutoff_min = float(defaults.get("asr_start_cutoff_minutes", 285))
            backstop_min = float(defaults.get("asr_backstop_minutes", 350))
            now = time.monotonic()
            asr_start_deadline = now + start_cutoff_min * 60 if start_cutoff_min > 0 else None
            asr_backstop_deadline = now + backstop_min * 60 if backstop_min > 0 else None
            deadline = asr_start_deadline
            stop = StopSignal(deadline=deadline, superseded=_newer_run_queued)
            print(
                f"budget: {lane} start cutoff {start_cutoff_min:.0f}m, "
                f"backstop {backstop_min:.0f}m (+ yield before new starts if superseded)"
            )
        else:
            deadline = (time.monotonic() + window_min * 60 * safety) if window_min > 0 else None
            stop = StopSignal(
                deadline=deadline,
                superseded=_newer_run_queued,
            )
            if window_min > 0:
                print(
                    f"budget: wall-clock window {window_min:.0f}m × {safety} "
                    "(+ yield if superseded)"
                )
        # Loud, once-per-run signal if graceful yield can't work (token dropped, e.g. via repo
        # settings rather than the workflow YAML the contract test guards). Without it the run is
        # bounded only by the wall-clock window — easy to miss, since the check fails open silently.
        if os.environ.get("GITHUB_ACTIONS") and not os.environ.get("GITHUB_TOKEN"):
            print(
                "warning: GITHUB_TOKEN unset — graceful yield disabled; run bounded only by window"
            )

    # Hard per-encode wall-clock cap: ffmpeg/ffprobe read the (remote) source directly, so a source
    # that stalls would otherwise hang a worker indefinitely — and ``stop()`` can't preempt a thread
    # parked in subprocess.run, so it would pin the whole build until GitHub's 6h cap. 0 = no cap.
    # Recalibrated 2026-07-02 (issue #497) from 45min against a very conservative 12h longest-
    # meeting ceiling (see ``DEFAULT_SOURCE_MEDIA_MAX_BYTES`` in citypods/media.py for how that
    # figure was derived) and a conservative 20 Mbps floor for a legitimately slow-but-not-stalled
    # transfer of the new 54 GB size cap: 54e9 bytes * 8 / 20 Mbps = 21,600s ≈ 360min. The
    # independent ``-rw_timeout``/``_STALL_TIMEOUT_US`` (120s zero-progress) stall detector is the
    # first line of defense for a truly hung connection; this is the belt-and-suspenders backstop
    # for a degraded-but-still-progressing one.
    encode_timeout_min = float(defaults.get("audio_encode_timeout_minutes", 360))
    # Media-specific size ceiling (issue #497) for direct ffmpeg source fetches — separate from any
    # HTTP response cap. 0/blank disables the preflight guard.
    media_max_bytes_raw = defaults.get("source_media_max_bytes", DEFAULT_SOURCE_MEDIA_MAX_BYTES)
    media_max_bytes = int(media_max_bytes_raw) if media_max_bytes_raw else None
    ffmpeg_threads_raw = defaults.get("audio_ffmpeg_threads")
    if ffmpeg_threads_raw is None:
        ffmpeg_threads = max(1, (os.cpu_count() or 1) // max(1, native_audio_max_active))
    else:
        ffmpeg_threads = max(1, int(ffmpeg_threads_raw))
    ffmpeg_memory_floor_mb = float(
        defaults.get(
            "audio_ffmpeg_memory_floor_mb",
            defaults.get("resource_guard_min_available_mb", 1536),
        )
    )
    ffmpeg_memory_floor_bytes = (
        int(ffmpeg_memory_floor_mb * 1024 * 1024) if ffmpeg_memory_floor_mb > 0 else None
    )
    _native_work_gate: NativeWorkGate | None = (
        NativeWorkGate(
            max_audio_active=native_audio_max_active,
            log=lambda msg: print(msg, flush=True),
        )
        if time_bounded and not dry_run
        else None
    )
    # Per-tenant Granicus transport telemetry (direct / Worker-fallback / truncation) for the H16
    # acceptance report. The rate-limit circuit breaker that previously lived here was removed in
    # GH#353 — it never tripped across audio runs #51–#56 (the runner 403s were egress IP
    # reputation, handled by the Worker), and the lease ceiling plus per-episode backoff bound load.
    _transport_telemetry: ProviderTransportTelemetry | None = ProviderTransportTelemetry(
        site_config.get("provider_transport_telemetry_domains", [])
    )
    if not _transport_telemetry.has_domains():
        _transport_telemetry = None
    owns_ffmpeg = ffmpeg is None
    ffmpeg = ffmpeg or CommandFfmpeg(
        max_kbps=max_kbps,
        timeout_seconds=(encode_timeout_min * 60) if encode_timeout_min > 0 else None,
        threads=ffmpeg_threads,
        memory_floor_bytes=ffmpeg_memory_floor_bytes,
        phase_gate=_native_work_gate,
        finalize_workers=int(defaults.get("audio_finalize_workers", 2)),
        transport_telemetry=_transport_telemetry,
        stop=stop,
        max_media_bytes=media_max_bytes,
    )
    source_cache = (
        SourceCache(
            ffmpeg_binary=getattr(ffmpeg, "binary", "ffmpeg"),
            timeout_seconds=getattr(ffmpeg, "timeout_seconds", None),
            memory_floor_bytes=getattr(ffmpeg, "memory_floor_bytes", None),
            transport_telemetry=_transport_telemetry,
            stop=stop,
            max_media_bytes=getattr(ffmpeg, "max_media_bytes", media_max_bytes),
        )
        if not dry_run
        else None
    )
    asr_abandoned_event = threading.Event() if time_bounded and not dry_run else None
    # Predicted-memory admission for audio encodes (H8): reserve each encode's estimated peak RSS
    # against a budget so a new encode begins only with real headroom — a leading signal the
    # instantaneous mem_available gate lacks (a long loudnorm encode grows for minutes before it
    # nears the floor). 0/blank budget disables it (falls back to the resource_admission gate).
    _memory_budget_mb = float(defaults.get("audio_memory_budget_mb", 0) or 0)
    _memory_reservation: MemoryReservation | None = (
        MemoryReservation(
            budget_bytes=int(_memory_budget_mb * 1024 * 1024),
            log=lambda msg: print(msg, flush=True),
        )
        if time_bounded and not dry_run and _memory_budget_mb > 0
        else None
    )
    transcript_quality_routes = load_quality_routes(
        site_config, state_dir, storage=None if dry_run else storage
    )
    ctx = StageContext(
        storage=storage,
        ffmpeg=ffmpeg,
        max_kbps=max_kbps,
        dry_run=dry_run,
        compute_backend=compute_backend,
        stop=stop,
        # Production leaves chapters bounded only by the wall-clock window (let the backlog
        # backfill fully over runs). ``--chapters-cap`` adds a small count bound *only* for the PR
        # preview, whose sanity-check should finish in seconds and starts with no cached chapters.
        chapters_per_source=chapters_cap if chapters_cap is not None else 10_000,
        # Loudness normalization config (#151).
        loudness_profile=loudness_profile,
        audio_processing_profile=audio_processing_profile,
        # Silence-trim planner config (#111).
        trim_silence=bool(defaults.get("trim_silence", False)),
        silence_noise_db=float(defaults.get("silence_noise_db", -40.0)),
        silence_lead_trail_min_s=float(defaults.get("silence_lead_trail_min_s", 1.0)),
        silence_mid_min_s=float(defaults.get("silence_mid_min_s", 10.0)),
        silence_min_served_seconds=float(defaults.get("silence_min_served_seconds", 5.0)),
        silence_min_served_fraction=float(defaults.get("silence_min_served_fraction", 0.02)),
        max_encodes_per_source=max_encodes_per_source,
        backlog_policy=backlog_policy,
        transcript_quality_routes=transcript_quality_routes,
        source_cache=source_cache,
        # One list_objects listing per source for this build, shared by every AudioStage call for
        # that source — the global queue (H5 PR3) invokes AudioStage once per episode, so without
        # this the same source's storage prefix would be re-listed once per episode (issue #344).
        hosted_keys_cache=HostedKeysCache(),
        resource_admission=_resource_admission_from_defaults(
            defaults,
            time_bounded=time_bounded,
            dry_run=dry_run,
        ),
        native_work_gate=_native_work_gate,
        memory_reservation=_memory_reservation,
        transport_telemetry=_transport_telemetry,
        # Global semaphore limiting concurrent ASR inference calls. ASR is CPU-bound;
        # N simultaneous inference calls each get 1/N of available CPU, making each
        # N× slower. asr_workers=1 (default) serialises all ASR, giving each call full
        # CPU and making runtime predictable. Raise only if you have spare memory for
        # multiple model instances (see asr_workers docs in site_config.yml).
        asr_semaphore=threading.Semaphore(int(defaults.get("asr_workers", 1)))
        if not dry_run and defaults.get("asr_enabled", True)
        else None,
        asr_timeout_base_seconds=float(defaults.get("asr_timeout_base_minutes", 15)) * 60,
        asr_timeout_per_hour_seconds=float(defaults.get("asr_timeout_per_audio_hour_minutes", 30))
        * 60,
        asr_timeout_safety_margin=float(defaults.get("asr_timeout_safety_margin", 1.2)),
        asr_start_deadline=asr_start_deadline if lane in {"transcribe", "align"} else deadline,
        asr_deadline=asr_backstop_deadline if lane in {"transcribe", "align"} else deadline,
        asr_timeout_budget_reserve_seconds=float(
            defaults.get("asr_timeout_budget_reserve_minutes", 0)
        )
        * 60,
        asr_runtime_log_path=state_dir / ASR_RUNTIME_LOG_NAME,
        transcript_quality_state_dir=None if dry_run else state_dir,
        transcript_quality_raw_log_cap=load_quality_config(site_config).raw_log_cap,
        asr_local_max_duration_hours=float(defaults.get("asr_local_max_duration_hours", 4)),
        asr_abort_event=threading.Event()
        if not dry_run and defaults.get("asr_enabled", True)
        else None,
        asr_abandoned_event=asr_abandoned_event,
        fast_yield_exit=(
            _fast_yield_exit
            if phase == "enrich"
            and lane not in {"transcribe", "align"}
            and os.environ.get("GITHUB_ACTIONS")
            else None
        ),
        lane=lane,
    )
    pipeline = SourcePipeline(
        state_dir=state_dir,
        stages=stages,
        ctx=ctx,
        max_archive_items=int(defaults.get("max_archive_items", DEFAULT_MAX_ARCHIVE_ITEMS)),
        max_archive_age_years=float(
            defaults.get("max_archive_age_years", DEFAULT_MAX_ARCHIVE_AGE_YEARS)
        ),
        persist_records=persist_records,
    )

    # Pre-load the Whisper model BEFORE the parallel worker pool starts.
    #
    # Problem: loading the 1617 MB float16 model peaks at ~5.5 GB RAM (float16 → float32
    # intermediate → int8 conversion).  With max_workers=20 sources × max_encodes_per_source=4
    # workers = up to 80 concurrent ffmpeg processes (~150 MB each = 12 GB), the model load
    # coinciding with peak audio-encode concurrency totals ~18+ GB — over the 16 GB runner
    # limit, causing the Linux OOM killer to send SIGKILL (exit 137).
    #
    # Fix: load the model here, before the pool opens, so its 5.5 GB peak is resolved to the
    # ~800 MB int8 steady state before any ffmpeg processes spawn.  The process-level cache in
    # asr.py means every TranscriptStage worker reuses this single loaded instance.
    results: list[CityResult] = []
    heartbeat_interval = _heartbeat_interval_seconds()
    heartbeat_requested = "CITYPODS_HEARTBEAT_SECONDS" in os.environ
    heartbeat_enabled = (
        time_bounded
        and not dry_run
        and (bool(os.environ.get("GITHUB_ACTIONS")) or heartbeat_requested)
    )
    build_start = time.monotonic()
    try:
        with _ResourceHeartbeat(
            enabled=heartbeat_enabled,
            label="enrich" if phase == "enrich" else phase,
            root=output_dir,
            interval_seconds=heartbeat_interval,
            stall_dump_seconds=_stall_dump_seconds() if heartbeat_enabled else 0.0,
            native_work_gate=_native_work_gate,
            resource_admission=ctx.resource_admission,
        ) as _hb:
            # Pre-load only the ASR model the active lane will use, so a transcribe shard never
            # pulls in stable-ts and an align shard never pulls in faster-whisper (H6b). The audio
            # lane loads nothing. ``lane=None`` (combined enrich) keeps the faster-whisper preload
            # as before.
            if (
                time_bounded
                and not dry_run
                and storage is not None
                and lane != "audio"
                and not getattr(compute_backend, "isolates_inference", False)
            ):
                _try_preload_asr_model(defaults, lane=lane)

            if phase == "enrich":
                # H5 PR3: the heavy production phase uses the global two-pass queue for true
                # newest-everywhere-first prioritization across all sources (the per-source pool
                # below can only order within a source). Renderless by definition.
                results = _run_enrich_global_queue(
                    pipeline,
                    cities,
                    source_cache=source_cache,
                    max_workers=max_workers,
                    policy=backlog_policy,
                    owned_uids=shard_owned_uids,
                )
            else:
                # all/render: the per-city pool. Light cross-source ordering (H5 PR2) submits
                # cities in policy order so high-priority cities start first (coarse — a started
                # city drains its own within-source-ordered backlog). Identity (no records load)
                # without a policy.
                if backlog_policy.keys:
                    _recs_by_slug: dict[str, dict] = {}
                    _recs_by_source: dict[str, dict] = {}
                    for c in cities:
                        sk = source_key(c)
                        if sk not in _recs_by_source:
                            _recs_by_source[sk] = load_records(state_dir, sk)
                        _recs_by_slug[c.slug] = _recs_by_source[sk]
                    cities = order_cities_by_policy(cities, _recs_by_slug, backlog_policy)

                # Aggregate sources run after dedicated sources so their cross-feed reconciliation
                # sees the canonical records and can omit overlap before persisting/rendering.
                ordered_city_groups = (
                    [c for c in cities if not c.source.get("aggregate")],
                    [c for c in cities if c.source.get("aggregate")],
                )
                result_by_slug: dict[str, CityResult] = {}
                with source_cache or _nullcontext():
                    for city_group in ordered_city_groups:
                        if not city_group:
                            continue
                        with ThreadPoolExecutor(max_workers=max_workers) as pool:
                            futures = [
                                pool.submit(
                                    _process_city,
                                    c,
                                    base_url,
                                    output_dir,
                                    cache,
                                    request_delay,
                                    dry_run,
                                    pipeline,
                                    site_config,
                                    fingerprint,
                                    do_render,
                                    no_refresh,
                                )
                                for c in city_group
                            ]
                            for fut in futures:
                                result, entry = fut.result()
                                result_by_slug[result.slug] = result
                                if entry is not None:
                                    cache[result.slug] = entry
                    # Aggregate reconciliation enriches canonical dedicated records in place.
                    # Re-render those canonical feeds from their persisted archives so links
                    # discovered only through the aggregate view are visible in this run.
                    if (
                        do_render
                        and not dry_run
                        and any(c.source.get("aggregate") for c in city_group)
                    ):
                        aggregate_entities = {
                            c.city_entity for c in city_group if c.source.get("aggregate")
                        }
                        canonical_cities = [
                            c
                            for c in cities
                            if c.city_entity in aggregate_entities and not c.source.get("aggregate")
                        ]
                        with ThreadPoolExecutor(max_workers=max_workers) as pool:
                            futures = [
                                pool.submit(
                                    _process_city,
                                    c,
                                    base_url,
                                    output_dir,
                                    cache,
                                    request_delay,
                                    dry_run,
                                    pipeline,
                                    site_config,
                                    fingerprint,
                                    True,
                                    True,
                                )
                                for c in canonical_cities
                            ]
                            for fut in futures:
                                result, entry = fut.result()
                                result_by_slug[result.slug] = result
                                if entry is not None:
                                    cache[result.slug] = entry
                results = [result_by_slug[c.slug] for c in cities]
    finally:
        # M9/CR2-CP-37: close the owned ffmpeg process pool even if the block above raises, so a
        # _process_city future's exception can't leak it.
        if owns_ffmpeg and isinstance(ffmpeg, CommandFfmpeg):
            ffmpeg.close()

    # Tally source-fetch failures by provider for run_history.jsonl.  Used by
    # check_provider_error_rates in audit.py to surface provider drift before it turns deploys red.
    provider_errors: dict[str, int] = {}
    for _city, _res in zip(cities, results, strict=False):
        if _res.status == "error":
            provider_errors[_city.provider] = provider_errors.get(_city.provider, 0) + 1

    provider_rate_limit_telemetry = DISTRIBUTED_PROVIDER_LEASES.telemetry()
    if _transport_telemetry is not None:
        for domain, values in _transport_telemetry.telemetry().items():
            provider_rate_limit_telemetry.setdefault(domain, {}).update(values)

    # Per-stage activity for the run, to stdout (build logs). Makes "did audio actually
    # materialize?" answerable without the step-summary report: ``ran`` is newly produced this
    # run, ``reused`` already up to date, ``queued`` deferred by budget (remaining backlog), plus
    # error count and a few sample messages so a re-host that triggers but fails downstream is
    # visible rather than hiding behind the feed-level "0 errors" line.
    for name, t in sorted(pipeline.stage_totals.items()):
        if not (t["ran"] or t["reused"] or t["backlog"] or t["errors"]):
            continue
        # Break ``ran`` into expensive encodes vs near-free storage re-credits when the stage
        # reports it (audio), so the per-episode time estimate's blend is visible at a glance.
        ran = f"{t['ran']} ran"
        if t["encoded"] or t["credited"]:
            ran += f" ({t['encoded']} encoded, {t['credited']} credited)"
        print(
            f"{name}: {ran}, {t['reused']} reused, {t['backlog']} queued, "
            f"{t['errors']} errors ({t['seconds']:.0f}s)"
        )
        migration_total = (
            t.get("asr_migration_copied", 0)
            + t.get("asr_migration_already_present", 0)
            + t.get("asr_migration_missing", 0)
            + t.get("asr_migration_regenerated", 0)
        )
        if migration_total:
            print(
                "    asr migration: "
                f"{t['asr_migration_copied']} copied, "
                f"{t['asr_migration_already_present']} already-present, "
                f"{t['asr_migration_missing']} missing, "
                f"{t['asr_migration_regenerated']} regenerated"
            )
        for msg in t["error_samples"]:
            print(f"    ! {msg}")
        if t.get("defer_reasons"):
            reasons = ", ".join(
                f"{reason}={count}"
                for reason, count in sorted(
                    t["defer_reasons"].items(), key=lambda item: (-item[1], item[0])
                )
            )
            print(f"    queued: {reasons}")
        for msg in t.get("defer_samples", []):
            print(f"    queued sample: {msg}")
        if t.get("rate_limited"):
            print(f"    ! throttle: {t['rate_limited']} 403/429 errors (GH#300)")
    for domain, values in sorted(provider_rate_limit_telemetry.items()):
        if values.get("lease_acquisitions"):
            print(
                f"provider {domain}: {values['lease_acquisitions']} lease acquisitions, "
                f"{values.get('lease_wait_seconds', 0):.1f}s wait "
                f"(max {values.get('lease_max_wait_seconds', 0):.1f}s), "
                f"{values.get('stale_leases_reaped', 0)} stale reaped, "
                f"{values.get('lease_renewals', 0)} renewals"
            )
        # Granicus Worker fallback usage (GH#337): direct-403 → authenticated Cloudflare egress.
        # These per-tenant counts are the post-activation evaluation signal — successes ≈ attempts
        # and failures ≈ 0 over the three acceptance runs means the fallback is recovering blocks.
        if values.get("worker_fallback_attempts"):
            print(
                f"provider {domain} granicus worker fallback: "
                f"{values.get('worker_fallback_attempts', 0)} attempts, "
                f"{values.get('worker_fallback_successes', 0)} ok, "
                f"{values.get('worker_fallback_failures', 0)} failed"
            )

    # Why the run ended (time-bounded phases only). "completed within the window" means all due
    # work finished; otherwise this names the trigger that wrapped it up (wall-clock vs a queued
    # build), so a regressed graceful yield is visible at a glance rather than silent (issue #63).
    if stop is not None:
        print(f"run end: {stop.fired_reason or 'completed within the window (no stop triggered)'}")

    if not dry_run:
        # Preserve the pre-search behavior: a later static-render error must not discard the
        # provider refresh validators already earned this run.  A second save below persists a
        # successful render's search cache.
        save_etag_cache(state_dir, cache)
        # Render-phase outputs (feeds/pages already written per-city above; here the site-wide
        # index/aliases/meta). The enrich phase skips all of this — it only backfills + persists.
        if do_render:
            all_cities = load_city_configs(config_dir, site_config.get("defaults", {}))
            feed_info = {
                r.slug: {"has_audio": r.has_audio, "has_video": r.has_video} for r in results
            }
            search_enabled = bool(site_config.get("defaults", {}).get("search", True))
            (output_dir / "index.html").write_text(
                render_index(
                    all_cities, site_config, base_url, feed_info, search_enabled=search_enabled
                )
            )
            _write_aliases(output_dir, base_url, all_cities, feed_info)
            _write_cname(output_dir, site_config)
            _prune_stale_dirs(output_dir, all_cities)
            if search_enabled:
                search_cache = cache.setdefault("_static_search", {})
                search_manifest = build_search_index(
                    state_dir,
                    all_cities,
                    output_dir,
                    base_url,
                    storage=storage,
                    cache=search_cache,
                )
                search_dir = output_dir / "search"
                search_dir.mkdir(parents=True, exist_ok=True)
                (search_dir / "index.html").write_text(render_search_page(site_config, base_url))
            else:
                # A config opt-out must not leave the previously deployed public search behind.
                shutil.rmtree(output_dir / "search", ignore_errors=True)
                shutil.rmtree(output_dir / "data" / "search", ignore_errors=True)
                (output_dir / "assets" / "minisearch-7.1.2.js").unlink(missing_ok=True)
                (output_dir / "assets" / "LICENSES" / "minisearch-7.1.2.txt").unlink(
                    missing_ok=True
                )
                search_manifest = {"shards": []}
            (output_dir / "meta.json").write_text(
                json.dumps(
                    {
                        "cities": len(all_cities),
                        "built": sum(r.status == "built" for r in results),
                        "search_shards": len(search_manifest.get("shards", [])),
                    },
                    indent=2,
                )
                + "\n"
            )
        # Search shard hashes are part of this same render cache.  Save after successful rendering
        # so a failed render cannot record a cache hit for files it did not finish publishing.
        save_etag_cache(state_dir, cache)
        # Run history calibrates the cost projection from real encode time, so record it only for
        # the phase that does that work (enrich/all) — a near-zero-cost render run would dilute it.
        if time_bounded:
            _elapsed = time.monotonic() - build_start
            _history_window_min = float(defaults.get("run_time_budget_minutes", 0))
            _history_safety = float(defaults.get("budget_safety", 0.8))
            if lane in {"transcribe", "align"}:
                _history_window_min = float(defaults.get("asr_backstop_minutes", 350))
                _history_safety = 1.0
            _window = _history_window_min * 60 * _history_safety
            _record_run_history(
                state_dir,
                results,
                pipeline.stage_totals,
                peak_load_per_cpu=_hb.peak_load_per_cpu,
                min_mem_avail_mb=(
                    round(_hb.min_mem_avail_bytes / (1024 * 1024), 1)
                    if _hb.min_mem_avail_bytes is not None
                    else None
                ),
                window_used_pct=(round(100.0 * _elapsed / _window, 1) if _window > 0 else None),
                gate_wait_seconds=(
                    round(_native_work_gate.total_wait_seconds, 1)
                    if _native_work_gate is not None
                    else None
                ),
                provider_errors=provider_errors or None,
                rate_limit_telemetry=provider_rate_limit_telemetry or None,
                h16_identity=pipeline.h16_identity.summary(),
                h16_availability=pipeline.h16_availability_summary(),
                scope={
                    "phase": phase,
                    "lane": lane,
                    "shard": f"{shard[0]}/{shard[1]}" if shard is not None else None,
                    "source": source,
                    "scoped": scoped,
                },
                interrupted=interrupt_requested(),
            )
            # Persist the derived work manifest (H5) for the status surface + as the H6b lease
            # substrate. Derived fresh from the just-updated records (hybrid model); ordered by
            # the configured policy. statesync picks up state/work.json automatically.
            if not dry_run:
                _city_by_source: dict[str, City] = {}
                for c in cities:
                    _city_by_source.setdefault(source_key(c), c)
                _manifest_sources = [
                    (sk, c, load_records(state_dir, sk)) for sk, c in _city_by_source.items()
                ]
                _manifest = build_manifest(
                    _manifest_sources,
                    policy=backlog_policy,
                    transcript_quality_routes=transcript_quality_routes,
                )
                # H14a: keep in-flight dispatch leases across the rebuild (build_manifest would
                # reset them to ``queued``), and flush the budget ledger as the run's final write.
                if isinstance(compute_backend, DispatchCoordinator):
                    _manifest = overlay_leases(_manifest, compute_backend.live_leases())
                    compute_backend.flush()
                save_manifest(state_dir, _manifest)
        # Persist the updated record store + cache back to durable storage. The bucket — not
        # actions/cache — is the source of truth for derived artifacts. Render is gated out
        # (persist_records is False): it produced no new records, and pushing its stale view would
        # clobber what the concurrent enrich workflow wrote (review/12 §H6/H11b).
        if persist_records:
            # H6b: a sharded/source-scoped run owns only a subset of sources. It pulled the whole
            # prefix for context but must push back ONLY its own ``sources/<key>/`` records, or it
            # would re-upload its now-stale copy of a sibling shard's record (cross-shard clobber).
            # It also must NOT reconcile — that reaps every remote object without a local
            # counterpart, which from a partial run means a sibling's records. The full unsharded
            # run does the sweep.
            if scoped:
                owned = sorted({source_key(c) for c in cities})
                # Foreign-block-preserving push: a scoped run owns only its lane's artifact block,
                # so it re-reads the freshest remote and preserves the blocks it does NOT own (audio
                # vs transcript) — closing the cross-LANE lost update that the cross-shard scope
                # alone does not (an ASR run finishing after an audio run would otherwise erase
                # freshly hosted audio — review/12 §H6). run_events are append-only per-run files
                # (no shared-field clobber), so a plain scoped push is correct for them.
                pushed = push_records_merged(
                    storage,
                    state_dir,
                    owned,
                    protected_blocks=protected_blocks_for_lane(lane),
                    owned_uids=shard_owned_uids,
                    log=lambda msg: print(msg, flush=True),
                )
                pushed += push_calendar_records_merged(
                    storage,
                    state_dir,
                    owned,
                    log=lambda msg: print(msg, flush=True),
                )
                pushed += push_state(
                    storage,
                    state_dir,
                    only_prefixes=[f"{RUN_EVENTS_DIR_NAME}/"],
                )
                pushed += push_asr_runtime_log_merged(
                    storage,
                    state_dir,
                    rel_path=ASR_RUNTIME_LOG_NAME,
                    log=lambda msg: print(msg, flush=True),
                )
                pushed += push_transcript_quality_log_merged(
                    storage,
                    state_dir,
                    max_events=load_quality_config(site_config).raw_log_cap,
                    log=lambda msg: print(msg, flush=True),
                )
            else:
                pushed = push_state(storage, state_dir)
            if pushed:
                print(f"state: pushed {pushed} file(s) to durable storage")
            # Reap remote state objects with no local counterpart (e.g. records orphaned by a
            # source edit that changed source_key) so they don't leak or pin orphaned audio.
            reclaimed = reconcile_state(storage, state_dir, full_run=not scoped)
            if reclaimed:
                print(f"state: reclaimed {reclaimed} stale remote file(s)")
        if (
            asr_abandoned_event is not None
            and asr_abandoned_event.is_set()
            and os.environ.get("GITHUB_ACTIONS")
        ):
            _abandoned_asr_exit()

    return results


def build(
    *,
    site_config_path: str | Path = "config/site_config.yml",
    config_dir: str | Path = "config",
    output_dir: str | Path = "docs",
    base_url: str | None = None,
    only_slug: str | None = None,
    dry_run: bool = False,
    ffmpeg: FfmpegRunner | None = None,
    chapters_cap: int | None = None,
    phase: str = "all",
    shard: tuple[int, int] | None = None,
    source: str | None = None,
    lane: str | None = None,
    no_refresh: bool = False,
    shard_plan_path: str | Path | None = None,
    state_snapshot_restored: bool = False,
) -> list[CityResult]:
    """Run a build and always close a subprocess-backed compute backend."""
    compute_backend_holder: list[object] = []
    try:
        return _build_impl(
            site_config_path=site_config_path,
            config_dir=config_dir,
            output_dir=output_dir,
            base_url=base_url,
            only_slug=only_slug,
            dry_run=dry_run,
            ffmpeg=ffmpeg,
            chapters_cap=chapters_cap,
            phase=phase,
            shard=shard,
            source=source,
            lane=lane,
            no_refresh=no_refresh,
            shard_plan_path=shard_plan_path,
            state_snapshot_restored=state_snapshot_restored,
            _compute_backend_holder=compute_backend_holder,
        )
    finally:
        for compute_backend in reversed(compute_backend_holder):
            close_compute = getattr(compute_backend, "close", None)
            if callable(close_compute):
                close_compute()


def _prune_stale_dirs(output_dir: Path, cities: list[City]) -> None:
    """Remove ``docs/<slug>`` directories left over from a deleted city or a renamed slug.
    ``docs/`` is restored from actions/cache between runs, so without this a removed feed (or
    the old slug after a rename) would keep serving forever. Only directories that look like a
    feed/alias slug and aren't in the current set are removed; reserved names (audio, etc.) and
    files are left alone."""
    import shutil

    live = {c.slug for c in cities}
    for c in cities:
        live.update(c.aliases)
    for child in output_dir.iterdir():
        if not child.is_dir() or child.name in _RESERVED_DOC_NAMES or child.name in live:
            continue
        # A feed/alias dir is identifiable by its audio_feed.xml or redirect index.html.
        if (child / "audio_feed.xml").exists() or (child / "index.html").exists():
            shutil.rmtree(child)


def _write_aliases(output_dir: Path, base_url: str, cities: list[City], feed_info: dict) -> None:
    """For every former slug (``aliases``), emit a permanent redirect: an itunes:new-feed-url
    stub feed (so podcast clients migrate the subscription) and an HTML redirect page. Also
    write ``redirects.json`` — a from->to map a CDN (Cloudflare) can turn into real 301s,
    since GitHub Pages can't redirect on its own. Slugs should rarely move; this is the safety
    net so subscribers never have to manually re-subscribe when they do."""
    site = base_url.rstrip("/")
    redirects: list[dict] = []
    for city in cities:
        if not city.aliases:
            continue
        info = feed_info.get(city.slug, {})
        new_page = f"{site}/{city.slug}/"
        kinds = ["audio"] + (["video"] if info.get("has_video") else [])
        for alias in city.aliases:
            adir = output_dir / alias
            adir.mkdir(parents=True, exist_ok=True)
            (adir / "index.html").write_text(render_redirect_page(new_page))
            redirects.append({"from": f"/{alias}/", "to": new_page})
            for kind in kinds:
                new_feed = f"{new_page}{kind}_feed.xml"
                (adir / f"{kind}_feed.xml").write_text(
                    render_redirect_feed(city.podcast_title, new_feed, new_page)
                )
                redirects.append({"from": f"/{alias}/{kind}_feed.xml", "to": new_feed})
    (output_dir / "redirects.json").write_text(
        json.dumps(sorted(redirects, key=lambda r: r["from"]), indent=2) + "\n"
    )


def _write_chapter_sidecars(
    city_dir: Path, city: City, episodes: list[Episode], base_url: str
) -> None:
    """Write each chaptered episode's Podcasting 2.0 chapters JSON to
    ``<slug>/chapters/<uid>.json`` (referenced by ``<podcast:chapters>``), and prune sidecars
    for episodes that no longer have chapters. Hosted from Pages, so chapters surface even for
    direct enclosures we don't re-host."""
    chap_dir = city_dir / "chapters"
    wanted: set[str] = set()
    for ep in episodes:
        if chapters_url(city, ep, base_url):
            wanted.add(f"{ep.uid}.json")
            chap_dir.mkdir(parents=True, exist_ok=True)
            (chap_dir / f"{ep.uid}.json").write_text(chapters_json(ep))
    if chap_dir.exists():
        for stale in chap_dir.glob("*.json"):
            if stale.name not in wanted:
                stale.unlink()


def _city_archive_hash(
    city: City, episodes: list[Episode], calendar_records: list[AgendaRecord], base_url: str
) -> str:
    """Hash the city archive page's render inputs for incremental publication."""
    payload = [
        city.slug,
        city.podcast_title,
        city.podcast_author,
        city.meetings_url,
        city.city_website,
        [
            [
                ep.uid,
                ep.title,
                ep.published.isoformat(),
                ep.media_availability.effective_state() if ep.media_availability else None,
            ]
            for ep in sorted(episodes, key=lambda e: (e.published, e.uid or ""), reverse=True)
        ],
        [
            [
                record.uid,
                record.title,
                record.body,
                record.published.isoformat(),
                sorted((record.links or {}).items()),
                record.video_guid,
            ]
            for record in sorted(
                calendar_records,
                key=lambda record: (record.published, record.uid or ""),
                reverse=True,
            )
        ],
        base_url.rstrip("/"),
    ]
    blob = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _write_meeting_pages(
    city_dir: Path,
    city: City,
    episodes: list[Episode],
    base_url: str,
    site_config: dict,
    page_cache: dict,
) -> dict:
    """Render retained meeting pages whose per-episode hashes or outputs changed."""
    pages_cache = dict(page_cache or {})
    wanted: set[str] = set()
    current_uids = {ep.uid for ep in episodes if ep.uid}
    for uid in list(pages_cache):
        if uid not in current_uids:
            pages_cache.pop(uid, None)
    for ep in episodes:
        if not ep.uid:
            continue
        wanted.add(ep.uid)
        # The page also carries city-level labels, provider deep-link behavior, and a configured
        # bug-report destination; include that small context so an operator edit re-renders it.
        page_context = [
            city.provider,
            city.podcast_title,
            city.podcast_author,
            city.meetings_url,
            city.city_website,
            base_url.rstrip("/"),
            site_config.get("github_repo"),
        ]
        page_hash = hashlib.sha256(
            json.dumps([meeting_page_hash(ep), page_context], separators=(",", ":")).encode()
        ).hexdigest()
        if pages_cache.get(ep.uid) == page_hash and (city_dir / ep.uid / "index.html").exists():
            continue
        page_dir = city_dir / ep.uid
        page_dir.mkdir(parents=True, exist_ok=True)
        (page_dir / "index.html").write_text(
            render_meeting_page(city, ep, base_url, site_config=site_config)
        )
        pages_cache[ep.uid] = page_hash
    for child in city_dir.iterdir():
        if not child.is_dir():
            continue
        if child.name in {"chapters", "archive"}:
            continue
        if child.name not in wanted and (child / "index.html").exists():
            import shutil

            shutil.rmtree(child)
    return pages_cache


RUN_HISTORY_NAME = "run_history.jsonl"
RUN_SUMMARY_NAME = "run_summary.json"
RUN_EVENTS_DIR_NAME = "run_events"
ASR_RUNTIME_LOG_NAME = "asr_runtime_log.json"
_RUN_HISTORY_KEEP = 1000  # cap the rolling log so the synced state stays small


def _record_run_history(
    state_dir: Path,
    results: list,
    stage_totals: dict,
    *,
    peak_load_per_cpu: float | None = None,
    min_mem_avail_mb: float | None = None,
    window_used_pct: float | None = None,
    gate_wait_seconds: float | None = None,
    provider_errors: dict[str, int] | None = None,
    rate_limit_telemetry: dict[str, dict[str, int | float]] | None = None,
    h16_identity: dict | None = None,
    h16_availability: dict | None = None,
    scope: dict | None = None,
    interrupted: bool = False,
) -> None:
    """Append one line to ``run_history.jsonl`` (rolling, capped) and write ``run_summary.json``
    (latest only). This is the data spine for the resource projection: it lets the model use a
    *measured* seconds/episode and per-stage backlog instead of defaults. Lives in the durable
    state (synced to the bucket), so trends survive cache eviction."""
    from datetime import UTC, datetime

    import citypods.records as _records  # avoid import cycle at module load

    scope = dict(scope or {})
    github_run_id = os.environ.get("GITHUB_RUN_ID")
    github_repository = os.environ.get("GITHUB_REPOSITORY")
    github_run_url = (
        f"https://github.com/{github_repository}/actions/runs/{github_run_id}"
        if github_run_id and github_repository
        else None
    )
    logical_run_id = (
        f"github:{github_run_id}:{scope.get('phase') or 'phase'}:{scope.get('lane') or 'lane'}"
        if github_run_id and scope.get("scoped")
        else None
    )

    stages = {
        name: {
            "ran": t["ran"],
            "encoded": t["encoded"],
            "credited": t["credited"],
            "aligned": t["aligned"],
            "transcribed": t["transcribed"],
            "reused": t["reused"],
            "backlog": t["backlog"],
            "seconds": round(t["seconds"], 1),
            "bytes": t["bytes"],
            "errors": t["errors"],
            "rate_limited": t.get("rate_limited", 0),
            "asr_migration": {
                "copied": t.get("asr_migration_copied", 0),
                "already_present": t.get("asr_migration_already_present", 0),
                "missing": t.get("asr_migration_missing", 0),
                "regenerated": t.get("asr_migration_regenerated", 0),
            },
            "defer_reasons": dict(sorted((t.get("defer_reasons") or {}).items())),
            "defer_samples": list(t.get("defer_samples") or [])[:5],
        }
        for name, t in stage_totals.items()
    }
    audio = stages.get("audio", {})
    # Provider media can first be touched by TimelineStage planners (notably SilencePlanner's
    # SourceCache prefetch). The legacy key name says "audio", but its value must reconcile every
    # direct media throttle/deferral in the audio work-class chain, not only AudioStage.
    media_rate_limited = sum(stage.get("rate_limited", 0) for stage in stages.values())
    summary = {
        "ts": datetime.now(UTC).isoformat(),
        "schema_version": _records.SCHEMA_VERSION,
        "phase": scope.get("phase"),
        "lane": scope.get("lane"),
        "shard": scope.get("shard"),
        "source": scope.get("source"),
        "scoped": bool(scope.get("scoped", False)),
        # GH#377: was this run cut short by a termination signal (SIGTERM/cancel)? Lets the
        # projection/audit tell a deliberately-completed run from one that was killed mid-queue
        # rather than inferring it from a missing trailing entry.
        "interrupted": bool(interrupted),
        "outcome": "interrupted" if interrupted else "completed",
        "cities": len(results),
        "built": sum(r.status == "built" for r in results),
        "skipped": sum(r.status == "skipped" for r in results),
        "errors": sum(r.status == "error" for r in results),
        # convenience keys the projection's measured_inputs() reads to calibrate sec/ep. Note
        # ``materialized`` still counts all hosts (encoded + credited); ``materialize_encoded`` is
        # the expensive-only count, exposed so sec/ep can later be based on encodes alone.
        "materialized": audio.get("ran", 0),
        "materialize_encoded": audio.get("encoded", 0),
        "materialize_seconds": audio.get("seconds", 0.0),
        "stages": stages,
        "github_run_id": github_run_id,
        "github_run_url": github_run_url,
        "logical_run_id": logical_run_id,
        "peak_load_per_cpu": round(peak_load_per_cpu, 3) if peak_load_per_cpu is not None else None,
        "min_mem_avail_mb": min_mem_avail_mb,
        "window_used_pct": window_used_pct,
        "gate_wait_seconds": gate_wait_seconds,
        "provider_errors": provider_errors or {},
        # Per-provider throttle telemetry (GH#300). The legacy convenience key name says 403, but
        # the counter covers HTTP 403/429 media attempts that fell through to per-episode backoff.
        "audio_rate_limited_403s": media_rate_limited,
        "provider_rate_limit_telemetry": rate_limit_telemetry or {},
        "h16_identity": h16_identity or {},
        "h16_availability": h16_availability or {},
    }
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / RUN_SUMMARY_NAME).write_text(json.dumps(summary, indent=2) + "\n")

    path = state_dir / RUN_HISTORY_NAME
    lines = path.read_text().splitlines() if path.exists() else []
    lines.append(json.dumps(summary))
    path.write_text("\n".join(lines[-_RUN_HISTORY_KEEP:]) + "\n")

    if summary["scoped"]:
        event_dir = state_dir / RUN_EVENTS_DIR_NAME
        event_dir.mkdir(parents=True, exist_ok=True)
        ts_token = (
            summary["ts"].replace("-", "").replace(":", "").replace(".", "").replace("+", "p")
        )
        scope_parts = [
            str(summary.get("phase") or "phase"),
            str(summary.get("lane") or "lane"),
            str(summary.get("shard") or summary.get("source") or "scope"),
        ]
        scope_token = "-".join(
            "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in part)
            for part in scope_parts
        )
        run_token = "".join(
            ch if ch.isalnum() or ch in "._-" else "_" for ch in str(github_run_id or "local")
        )
        (event_dir / f"{ts_token}-{run_token}-{scope_token}.json").write_text(
            json.dumps(summary, indent=2) + "\n"
        )


# Process-wide interrupt latch (GH#377). Set by the SIGTERM handler installed at the CLI entry
# point (``install_signal_handlers``); read by ``StopSignal`` so in-flight workers start deferring
# immediately instead of waiting for the next wall-clock poll, and by ``_build_impl`` to mark the
# run cut-short in ``run_history.jsonl``. Module-level (not per-``StopSignal``) so the async signal
# handler has a stable target regardless of which ``StopSignal`` instance — if any — is live.
_INTERRUPT = threading.Event()


def request_stop() -> None:
    """Latch the process-wide interrupt (idempotent). Safe to call from a signal handler."""
    _INTERRUPT.set()


def interrupt_requested() -> bool:
    """True once a termination signal has flipped the interrupt latch this process."""
    return _INTERRUPT.is_set()


def _signal_stop_handler(signum, _frame) -> None:
    """SIGTERM handler: latch the interrupt and log once. Deliberately minimal — the graceful path
    (in-flight workers deferring, then per-source persist + run-history) runs on the normal threads
    once they observe the flipped ``stop`` predicate, not from inside this async handler."""
    request_stop()
    try:
        print(
            f"stop: signal {signum} received — finishing in-flight work, then persisting",
            flush=True,
        )
    except Exception:  # noqa: BLE001 - a handler must never raise back into arbitrary code
        pass


def install_signal_handlers() -> None:
    """Register SIGTERM → graceful stop. Call once from the CLI entry point; never from library
    code that tests import, so the test process's signal disposition is left untouched. Fails open
    (e.g. when not on the main thread, where ``signal.signal`` raises)."""
    import signal

    try:
        signal.signal(signal.SIGTERM, _signal_stop_handler)
    except (ValueError, OSError):
        pass


class StopSignal:
    """Shared, thread-safe predicate telling expensive stages when to stop starting new work.

    Two triggers, both meaning "wrap up this run and deploy":
      * **wall-clock** — ``deadline`` (a ``time.monotonic()`` value) has passed; the run has used
        its time window.
      * **superseded** — a newer Build & Deploy run is queued behind this one. We yield gracefully
        so it can take over, rather than relying on a hard ``cancel-in-progress`` that could abort
        the in-flight Pages deploy.

    Callable so stages just do ``if ctx.stop and ctx.stop(): ...``. The (network) superseded check
    is polled at most every ``poll_interval`` seconds and latches once true, so calling it per
    episode across parallel sources stays cheap."""

    def __init__(
        self,
        *,
        deadline: float | None = None,
        superseded: Callable[[], str | None] | None = None,
        poll_interval: float = 60.0,
    ):
        self._deadline = deadline
        self._superseded = superseded
        self._poll_interval = poll_interval
        self._last_poll = 0.0
        self._latched = False
        self._latched_event: str | None = None
        self._lock = threading.Lock()
        # Which trigger first fired (None until then). Drives the once-only announce below and the
        # end-of-run summary in build(), so a log reader can tell "ran the full window" from
        # "yielded to a queued build" — the visibility this feature lacked (issue #63).
        self.fired_reason: str | None = None

    def __call__(self) -> bool:
        if _INTERRUPT.is_set():
            self._announce("termination signal received")
            return True
        if self._deadline is not None and time.monotonic() >= self._deadline:
            self._announce("wall-clock window spent")
            return True
        if self._superseded is None:
            return False
        with self._lock:
            if not self._latched:
                now = time.monotonic()
                if now - self._last_poll >= self._poll_interval:
                    self._last_poll = now
                    try:
                        superseded = self._superseded()
                        self._latched = bool(superseded)
                        self._latched_event = str(superseded) if superseded else None
                    except Exception:
                        self._latched = False  # never let a flaky API check stop a run
            latched = self._latched
        if latched:
            self._announce("newer build queued behind this run")
        return latched

    def _announce(self, reason: str) -> None:
        """Record + print the stop trigger exactly once (first thread to fire wins)."""
        with self._lock:
            if self.fired_reason is not None:
                return
            self.fired_reason = reason
        print(f"stop: {reason} — finishing in-flight work, then deploying", flush=True)

    def should_exit_immediately(self) -> bool:
        """True only for code/human-triggered supersession, not cron or wall-clock stops."""
        return self.fired_reason == "newer build queued behind this run" and (
            self._latched_event in _FAST_EXIT_RUN_EVENTS
        )


def _fast_yield_exit() -> None:
    """End post-deploy enrich promptly after a push/manual supersession.

    The ASR inference thread is daemonized, but native CTranslate2/BLAS work and executor
    shutdown can still keep the process alive long enough to hold the workflow concurrency lock.
    For a code-change build waiting behind us, the correct product behavior is to discard this
    partial enrich attempt and let the newer run render/deploy immediately.
    """
    import os as _os

    print("fast-yield: exiting enrich immediately for queued code-change build", flush=True)
    _os._exit(0)


def _abandoned_asr_exit() -> None:
    """Exit cleanly after state persistence when abandoned native ASR is still running."""
    import os as _os

    print(
        "asr-yield: exiting after state push to avoid native ASR teardown while inference "
        "continues in background",
        flush=True,
    )
    _os._exit(0)


def _newer_run_queued() -> str | None:
    """Event name if a newer run is queued behind this run (GitHub Actions).

    Reads the standard ``GITHUB_*`` env + ``GITHUB_TOKEN``; a run waiting on the ``pages``
    concurrency group shows up with status ``queued``/``waiting``/``pending``. Any newer run should
    make expensive work stop starting new items so GitHub does not externally terminate a long ASR
    job while the next run waits. Only push/manual runs trigger the immediate fast-exit path; cron
    runs use normal graceful stop/persist behavior. Returns None outside CI or on any error
    (fail-open — never block a run on this)."""
    import os
    import urllib.request

    repo = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")
    run_id = os.environ.get("GITHUB_RUN_ID")
    run_number = os.environ.get("GITHUB_RUN_NUMBER")
    workflow_ref = os.environ.get("GITHUB_WORKFLOW_REF", "")  # .../.github/workflows/deploy.yml@ref
    if not (repo and token and run_id and run_number):
        return None
    # GITHUB_WORKFLOW_REF is ``owner/repo/.github/workflows/deploy.yml@refs/heads/main``. The
    # ``@ref`` suffix contains slashes, so the filename is the basename *after dropping the ref* —
    # split on ``@`` BEFORE ``/`` (the reverse silently yields the branch name, "main", which the
    # API rejects with a 404 → graceful yield never fires; this was issue #63's actual cause).
    workflow_file = workflow_ref.split("@")[0].split("/")[-1] or "deploy.yml"
    # Don't filter by ``status=queued`` in the query: a run held by the ``pages`` concurrency group
    # surfaces as queued/pending/waiting depending on timing, so fetch the recent runs and treat
    # any newer, not-yet-completed run as "a build is waiting behind me".
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}/runs?per_page=30"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        # Fail open (never block a run on a flaky API check) — but say so ONCE. Silent fail-open is
        # exactly what hid the 404 above through three "fix the yield" attempts; a single visible
        # line turns the next regression into a grep instead of a multi-hour mystery.
        if not getattr(_newer_run_queued, "_warned", False):
            _newer_run_queued._warned = True  # type: ignore[attr-defined]
            print(
                f"warning: graceful-yield check failed ({exc!r}); "
                "run bounded by the wall-clock window only",
                flush=True,
            )
        return None
    me = int(run_number)
    for run in data.get("workflow_runs", []):
        event = str(run.get("event") or "")
        if (
            str(run.get("id")) != run_id
            and run.get("status") != "completed"
            and int(run.get("run_number", 0)) > me
        ):
            print(
                f"yield: {event} run #{run.get('run_number')} is queued behind this run (#{me}) — "
                "will wrap up and deploy",
                flush=True,
            )
            return event
    return None


def _resolve_base_url(base_url: str | None, site_config: dict) -> str:
    import os

    if base_url:
        return base_url
    env = os.environ.get("PAGES_BASE_URL")
    if env:
        return env
    domain = site_config.get("custom_domain")
    if domain:
        return f"https://{domain}"
    return "http://localhost:8000"


def _write_cname(output_dir: Path, site_config: dict) -> None:
    domain = site_config.get("custom_domain")
    cname = output_dir / "CNAME"
    if domain:
        cname.write_text(domain + "\n")
    elif cname.exists():
        cname.unlink()
