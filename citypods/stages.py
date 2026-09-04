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

    for ep in _materialize_set(episodes, city.full_artifact_episodes):
        if <already done for ep>:                 # cheap, idempotent — NOT gated by stop
            stats.reused += 1
            continue
        if ctx.stop is not None and ctx.stop():    # check immediately before the costly work
            stats.defer("wall-clock-budget")        # deferred to a later run — not an error
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
import dataclasses
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urljoin

from citypods import asr as asr_mod
from citypods.asr import asr_initial_prompt
from citypods.bodies import canonical_body, rank_by_body
from citypods.compute import DispatchCoordinator, InferenceJob
from citypods.compute.local import LocalBackend
from citypods.diarize import (
    DEFAULT_DIARIZE_MODEL,
    DEFAULT_EMBEDDING_MODEL,
    TIMED_WORDS_VALIDATION_VERSION,
    has_valid_timed_words,
)
from citypods.durations import (
    episode_duration_hours,
    episode_served_duration_seconds,
    episode_source_duration_seconds,
    set_served_duration_seconds,
)
from citypods.integrity import (
    REPAIR_AUDIO_REMATERIALIZE,
    REPAIR_TIMELINE_REPLAN,
    REPAIR_TRANSCRIPT_REGENERATE,
    ensure_timeline_audio_repair_token,
    needs_timeline_audio_repair,
    timeline_audio_repair_token,
)
from citypods.known_text import provider_align_ineligible, provider_sections
from citypods.media import (
    AudioArtifactCache,
    HostedKeysCache,
    MaterializeStats,
    ProviderTransportTelemetry,
    RateLimitedMediaFetchError,
    SourceCache,
    _probe_served_duration_secs,
    materialize_audio,
    record_materialize_failure,
)
from citypods.models import City, Episode
from citypods.ops.workqueue import BacklogPolicy, WorkItem, sort_key_for, workitem_from_episode
from citypods.progress import PROGRESS
from citypods.records import (
    AUDIO_PIPELINE_VERSION,
    _capped_exponential_backoff,
    _in_backoff,
    _parse_iso_utc,
    confirmed_dead_recheck_due,
    transcript_media_hash,
    transcript_timeout_backoff_until,
)
from citypods.resources import MemoryReservation, NativeWorkGate, ResourceAdmission
from citypods.security import MAX_REDIRECTS, validate_source_url
from citypods.speakers import IDENTITY_PIPELINE_VERSION
from citypods.timeline import Timeline, edl_duration, remap, timeline_digest
from citypods.transcript_quality import (
    TranscriptQualityRoute,
    accepted_recipe_allowed,
    quality_body_key,
    record_l1_sample,
)
from citypods.transcript_versions import PROVIDER_ALIGN_PIPELINE_VERSION


def _materialize_set(
    episodes: list[Episode],
    max_per_body: int,
    *,
    feed_visible_per_body: int | None = None,
    policy: BacklogPolicy | None = None,
    city_slug: str = "",
    work_class: str = "audio",
) -> list[Episode]:
    """The subset worth processing: the most-recent ``max_per_body`` per body. Every
    per-board feed shows at most that many of its body, and the combined feed is a subset of
    the union, so this is exactly what some feed can display — never the deep archive.

    Selection is unchanged; ``policy`` (H5) only reorders the selected set. With no policy the
    order is byte-identical to before (body-grouped, newest-first per body).

    ``work_class`` labels the transient ``WorkItem`` built for ordering only — it does not need to
    match an episode's *exact* transcript sub-lane (``transcript-asr`` vs ``transcript-align`` vs
    ``provider-transcript-align``, decided per-episode later by ``_transcript_class``), only
    whether this call is a transcript-producing stage (any value in
    ``workqueue.DURATION_AWARE_WORK_CLASSES``) so the ``long_first`` comparator can tell it apart
    from a non-transcript stage's call. Defaults to ``"audio"``: every caller except
    ``TranscriptStage`` / ``ProviderTranscriptDiarizeStage`` processes audio-adjacent work that
    duration-based external-GPU prioritization should never reorder."""
    out: list[Episode] = []
    ranked: list[tuple[Episode, str]] = []
    visible = max_per_body if feed_visible_per_body is None else feed_visible_per_body
    for eps in rank_by_body(episodes, body_of=lambda e: e.body, published_of=lambda e: e.published):
        selected = eps[:max_per_body]
        out.extend(selected)
        ranked.extend(
            (ep, "feed_visible" if index < visible else "recent_archive")
            for index, ep in enumerate(selected)
        )
    if policy is not None and policy.keys:
        key = sort_key_for(policy)
        buckets = {id(ep): bucket for ep, bucket in ranked}
        out.sort(
            key=lambda ep: key(
                workitem_from_episode(
                    ep,
                    city_slug=city_slug,
                    work_class=work_class,
                    priority_bucket=buckets[id(ep)],
                )
            )
        )
    return out


def _playable(episodes: list[Episode]) -> list[Episode]:
    """Drop episodes whose durable media-availability verdict is withheld (H16 PR3).

    Applied only by ``AudioStage`` — the stage that would otherwise encode/host a bad or empty
    recording. ``TimelineStage`` (which runs the silence-detection / availability pass) deliberately
    does NOT use this, so a withheld episode is still re-examined every run and can recover. A
    confirmed-unavailable episode is simply skipped here, keeping its prior record block untouched.
    """
    return [
        ep for ep in episodes if not (ep.media_availability and ep.media_availability.is_withheld())
    ]


def _timeline_ready(episodes: list[Episode]) -> list[Episode]:
    """Drop episodes whose required timeline planning explicitly deferred in this run."""
    return [ep for ep in episodes if not getattr(ep, "timeline_defer_reason", "")]


_TIMELINE_BACKOFF_ERRORS = frozenset(
    {
        "timeline-cache",
        "timeline-decode",
        "timeline-degenerate",
        "timeline-partial-source",
        "rate_limited",
        "timeline-plan-error",
    }
)


@dataclass(frozen=True)
class _CachedAsrArtifacts:
    artifacts: object
    aligned: bool


class AsrArtifactCache:
    """Thread-safe run-local reuse for identical stable-uid + ASR-recipe work.

    The durable transcript layout remains source-scoped, but one real-world meeting may appear in
    multiple source views. The planner co-locates those aliases on one shard; this cache lets the
    first completed inference supply bytes to every source-local object without re-running ASR.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._values: dict[tuple[str, str], _CachedAsrArtifacts] = {}
        self._inflight: set[tuple[str, str]] = set()

    def get(self, key: tuple[str, str]) -> _CachedAsrArtifacts | None:
        with self._condition:
            return self._values.get(key)

    def claim(self, key: tuple[str, str]) -> tuple[bool, _CachedAsrArtifacts | None]:
        """Wait for an in-flight leader, or reserve ``key`` for the caller.

        Returns ``(True, None)`` to the leader that must run inference, or
        ``(False, artifacts)`` to a follower after the leader completes.
        """
        with self._condition:
            while key in self._inflight:
                self._condition.wait()
            value = self._values.get(key)
            if value is not None:
                return False, value
            self._inflight.add(key)
            return True, None

    def complete(self, key: tuple[str, str], artifacts: object, *, aligned: bool) -> None:
        with self._condition:
            self._values[key] = _CachedAsrArtifacts(artifacts=artifacts, aligned=aligned)
            self._inflight.discard(key)
            self._condition.notify_all()

    def abort(self, key: tuple[str, str]) -> None:
        """Release a failed/deferred reservation so one follower may retry."""
        with self._condition:
            self._inflight.discard(key)
            self._condition.notify_all()


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
    # GPU/ASR execution backend (H13). The pluggable seam ``TranscriptStage`` routes inference
    # through — ``local`` (in-process faster-whisper/WhisperX) by default; H14 swaps in the
    # Modal/Beam dispatch adapters here with no stage change. None ⇒ build an in-process
    # ``LocalBackend`` on the stage's ``asr_mod`` (keeps the default path behavior-preserving).
    compute_backend: object | None = None
    # Optional R5 structured tag backend.  It is deliberately separate from the ASR backend so a
    # catalog build can run deterministic tags without installing or configuring the LLM extra.
    tag_backend: object | None = None
    # R6 uses a separate backend/policy so council routes can be restricted to the two configured
    # free Gemini pools without changing the established R5 tag route.
    moment_backend: object | None = None
    moment_evaluation_state_path: Path | None = None
    moment_evaluation_config: dict = field(default_factory=dict)
    moment_max_dispatches: int = 40
    moment_dispatches: int = 0
    moment_dispatch_lock: threading.Lock = field(default_factory=threading.Lock)
    # A source preflight can be shared by many quote candidates from one meeting.  Keep the
    # result run-local: signed URLs and availability can legitimately change between runs.
    moment_video_gate_cache: dict[str, bool] = field(default_factory=dict)
    moment_video_gate_lock: threading.Lock = field(default_factory=threading.Lock)
    # R7 keeps private registry/evaluation state separate from the public episode record.  The
    # registry may contain voice embeddings and is never copied into ``docs/`` artifacts.
    speaker_registry_path: Path | None = None
    speaker_evaluation_state_path: Path | None = None
    speaker_turn_evidence_path: Path | None = None
    diarize_runtime_log_path: Path | None = None
    speaker_config: dict = field(default_factory=dict)
    taxonomy_path: Path = Path("config/taxonomy.yml")
    llm_evaluation_state_path: Path | None = None
    llm_evaluation_config: dict = field(default_factory=dict)
    # Run-scoped cache for TagsStage's taxonomy + calibration state (populated lazily on its first
    # call). The global queue invokes TagsStage.process() once PER EPISODE, and both are read-only,
    # unchanged local-disk loads for the whole run -- yaml.safe_load on a real ~17KB taxonomy.yml
    # measured ~28ms/call; re-paid across a 13k-episode backlog that's ~6 minutes of pure YAML
    # parsing alone (before evaluation-state JSON parsing, Taxonomy construction, or the admission
    # policy hash), consuming the tag lane's entire wall-clock budget before most episodes ever
    # reached dispatch -- exactly what a real scheduled run showed (2 live LLM calls total). Same
    # object for the whole run since `ctx` itself is constructed once per build.
    tag_taxonomy_cache: dict[str, Any] = field(default_factory=dict)
    # Guards check-then-populate access to `tag_taxonomy_cache` across the global queue's worker
    # thread pool -- see the lock's use in `TagsStage.process()` for why an unguarded cache would
    # let one thread observe another's partially-written cache bundle.
    tag_taxonomy_cache_lock: threading.Lock = field(default_factory=threading.Lock)
    stop: Callable[[], bool] | None = None
    chapters_per_source: int = 10_000  # ~unbounded; build() lowers it only for the PR preview
    # Swagit transcript endpoint probes are bounded separately because the archive can contain
    # tens of thousands of old episodes, most of which have no transcript.
    provider_transcript_probes_per_source: int = 500
    provider_transcript_probe_remaining: dict[str, int] = field(default_factory=dict)
    provider_transcript_probe_lock: threading.Lock = field(default_factory=threading.Lock)
    # EBU R128 loudness normalization (#151). Empty string = disabled.
    # e.g. "ebuR128:-16LUFS" normalises to -16 LUFS (Apple Podcasts / Spotify speech standard).
    loudness_profile: str = ""
    # Named pre-mastering recipe included in audio_spec_hash. ``podcast-speech-v2`` performs
    # bounded-memory high-pass → dynamic leveling → compression before final linear loudnorm.
    audio_processing_profile: str = ""
    # Silence-trim planner config (#111). Config flows through ctx so SilencePlanner needs no
    # constructor args and enrich_stages() needs no site_config parameter.
    trim_silence: bool = False
    silence_noise_db: float = -40.0
    silence_lead_trail_min_s: float = 1.0
    silence_mid_min_s: float = 10.0
    # Sanity floor on the planner's *result* (audio workflow review, 2026-06): a garbage/short
    # ``source_duration`` (e.g. a throttled fetch silencedetect read as near-silent) must not
    # produce a near-empty served timeline that then gets encoded and hosted. Reject when the kept
    # served duration is below both the absolute floor and the source-duration fraction.
    silence_min_served_seconds: float = 5.0
    silence_min_served_fraction: float = 0.02
    # Parallel episode processing within one source. Workers are I/O-bound (rate-limited HLS
    # streaming), so this can safely exceed CPU count. Set via site_config max_encodes_per_source.
    max_encodes_per_source: int = 1
    # Number of additional audio items kept submitted beyond the active worker set. This is a
    # rolling queue window, not a source-media prefetch count; each audio item releases its raw
    # source cache before the next item is admitted.
    audio_queue_lookahead: int = 4
    # Backlog prioritization policy (H5). None (default) ⇒ behavior-preserving order. When set,
    # ``_materialize_set`` reorders the per-source set by the configured comparator keys.
    backlog_policy: BacklogPolicy | None = None
    # H15 transcript-routing decisions keyed by ``(source_key, body_key)``. Empty keeps the
    # legacy production behavior: align when source text exists, otherwise transcribe.
    transcript_quality_routes: dict[tuple[str, str], TranscriptQualityRoute] = field(
        default_factory=dict
    )
    # Per-run download cache shared across TimelineStage (SilencePlanner) and AudioStage so each
    # source is streamed at most once per episode, even when both stages need it.
    source_cache: SourceCache | None = None
    # Per-run cache of each source's hosted-object listing (issue #344). The global queue (H5 PR3)
    # calls AudioStage once per episode rather than once per source, so without this cache
    # materialize_audio() would re-list the same source's storage prefix once per episode instead
    # of once per source/pass.
    hosted_keys_cache: HostedKeysCache | None = None
    # Run-local audio coalescing for stable meetings exposed through distinct source views.
    audio_artifact_cache: AudioArtifactCache = field(default_factory=AudioArtifactCache)
    # Admission guard for expensive native work. It waits for memory/CPU headroom before
    # starting another ffmpeg encode or ASR inference, preventing runner-level kills.
    resource_admission: ResourceAdmission | None = None
    # Resource-class gate for native work. Audio encodes may overlap audio, but ASR is exclusive
    # and must not overlap ffmpeg encodes on the small GitHub-hosted runner.
    native_work_gate: NativeWorkGate | None = None
    # Predicted-memory admission for audio encodes: reserves each encode's estimated peak RSS so a
    # new encode begins only with real budget headroom (supersedes the instantaneous mem_available
    # gate for audio). See ``citypods/resources.py:MemoryReservation``.
    memory_reservation: MemoryReservation | None = None
    # Per-tenant Granicus transport telemetry (direct vs Worker-fallback vs truncation) for the H16
    # acceptance report. Observational only — it never defers or gates media work.
    transport_telemetry: ProviderTransportTelemetry | None = None
    # Global semaphore that caps concurrent ASR inference calls across ALL sources in the run.
    # ASR is CPU-bound and uses all cpu_threads — running N sources' alignment/transcription
    # simultaneously divides effective CPU by N, making each job N× slower.  With max_workers=20
    # sources and one 4-hour meeting per source, 20 simultaneous calls would each take 20× longer,
    # blowing the 6-hour job ceiling.  Serialising to 1 (default) keeps total time predictable.
    # Set via site_config asr_workers (same field that drives cpu_threads per inference call).
    asr_semaphore: threading.Semaphore | None = None
    # Identical stable-uid + recipe work can occur in multiple configured source views. The
    # canonical planner co-locates those aliases; this cache fans one inference result out to each
    # source-scoped transcript object. Run-local only: no durable identity or backfill change.
    asr_artifact_cache: AsrArtifactCache = field(default_factory=AsrArtifactCache)
    # ASR inference is native C++ work and can occasionally stop making visible progress. Bound
    # one item by wall-clock so the run can persist completed work instead of waiting for Actions
    # to SIGTERM the whole process. The timeout is (base + per-audio-hour) * safety margin;
    # <=0 base/per-hour disables it.
    asr_timeout_base_seconds: float = 15 * 60
    asr_timeout_per_hour_seconds: float = 30 * 60
    # Headroom over the assumed real-time ratio baked into the two fields above. Run #32 (review/12
    # §H6b) showed a real episode finishing at ratio=0.503 against a budget assuming ratio=0.5 —
    # only ~3% of margin — so a slightly-slower-than-average transcription gets killed mid-flight
    # even though it was never actually hung. 1.2 gives genuinely-progressing inference ~20% more
    # runway before the per-item timeout (not the hard backstop) fires.
    asr_timeout_safety_margin: float = 1.2
    # Monotonic deadline after which no new ASR item should start (285m in production). Active
    # inference is allowed to continue past this cutoff so completed work is not thrown away.
    asr_start_deadline: float | None = None
    # Monotonic hard backstop for one ASR item (350m in production). A recording that ran past this
    # can be abandoned; the next scheduled ASR run will resume from persisted state.
    asr_deadline: float | None = None
    asr_timeout_budget_reserve_seconds: float = 0
    # Rolling state-backed runtime log used to estimate whether a recording can fit before the start
    # cutoff. Stores the previous 100 successful ASR runtime/recording-duration ratios, seeded
    # with a conservative estimate until real samples replace it.
    asr_runtime_log_path: Path | None = None
    # R7 learns its own local pyannote runtime ratio.  It deliberately never borrows ASR
    # coefficients: the first pilot run records a sample, then later runs defer work that cannot
    # fit before the diarization lane cutoff.
    diarize_start_deadline: float | None = None
    diarize_start_reserve_seconds: float = 15 * 60
    # H15 Layer 1: state dir for the capped raw-evidence log. Every successful align()/
    # transcribe() call appends a near-zero-cost coverage + word-logprob sample here (see
    # record_l1_sample). None (e.g. dry-run/tests) skips L1 recording.
    transcript_quality_state_dir: Path | None = None
    transcript_quality_raw_log_cap: int = 200
    # Local/in-process faster-whisper memory-safety ceiling. External dispatch backends are not
    # subject to it. A non-positive value disables the guard; unknown durations remain eligible.
    asr_local_max_duration_hours: float = 0
    # Set after an ASR timeout so other source workers skip starting more ASR in this run. The
    # timed-out daemon thread may still be burning CPU until process exit, so don't pile on.
    asr_abort_event: threading.Event | None = None
    # Set whenever an ASR inference thread is abandoned because stop()/timeout fired. The build
    # epilogue uses this after state persistence to avoid Python interpreter teardown while native
    # CTranslate2/BLAS work is still alive, which has been observed to segfault in Actions.
    asr_abandoned_event: threading.Event | None = None
    # Called only for a human/code-change supersession after the post-deploy enrich phase has
    # already abandoned in-flight ASR work. This is deliberately not used for scheduled-run
    # supersession or wall-clock budget stops: those should finish/persist as much completed work
    # as possible.
    fast_yield_exit: Callable[[], None] | None = None
    # Work-class lane for the sharded H6b workflows. None ⇒ the combined behavior (audio pass +
    # auto align/transcribe per episode). ``"audio"`` runs only the audio pass; ``"transcribe"``
    # runs the transcript pass forcing fresh faster-whisper transcription (never loads WhisperX);
    # ``"align"`` runs the transcript pass align-only (WhisperX, episodes with a source transcript)
    # so the two ASR models never co-load in one runner. The pass selection lives in run.py; this
    # field tells TranscriptStage which ASR model path to take.
    lane: str | None = None
    # Run-scoped signal that the LLM tag backend has no dispatch capacity left this run (its
    # daily/per-minute provider quota is spent -- the first dispatch that comes back deferred sets
    # it). ``TagsStage`` reads it to stop RE-FETCHING agenda/transcript text for the large backlog
    # of episodes whose rules tags are already computed and cached and that only await a (now
    # unavailable) LLM tag: those defer in memory, untouched, and are retried on a later run once
    # quota frees. New/changed episodes still fetch, since they need the text for their rules tags.
    # One Event per build, shared across the global queue's per-episode ``process()`` calls; a
    # monotonic ``set()`` that is safe to race under the worker threads.
    tag_llm_dispatch_exhausted: threading.Event = field(default_factory=threading.Event)
    # Per-run cap on newly queued dispatches to avoid flooding worker queues on broad backfills.
    tag_max_dispatches: int | None = None
    tag_dispatches_count: int = 0
    tag_dispatches_reserved: int = 0
    tag_dispatches_lock: threading.Lock = field(default_factory=threading.Lock)
    # The evaluator is a separately budgeted ingress purpose. It must have its own producer cap:
    # sharing the tagger cap lets rule-candidate evaluation keep traversing the catalog after the
    # tagger quota has filled, which was enough to prevent the run-level batch flush from ever
    # reaching the Worker.
    tag_prelabeler_dispatch_exhausted: threading.Event = field(default_factory=threading.Event)
    tag_prelabeler_max_dispatches: int | None = None
    tag_prelabeler_dispatches_count: int = 0
    tag_prelabeler_dispatches_reserved: int = 0
    tag_prelabeler_dispatches_lock: threading.Lock = field(default_factory=threading.Lock)
    # UTC wall-clock deadline handed to the LLM tag scheduler as its dispatch ``deadline_at``.
    # Topic tags use asynchronous dispatch without an artificial deadline (None), avoiding
    # runner-side sleep pacing and preventing queued worker tasks from timing out.
    tag_llm_deadline: datetime | None = None

    def reserve_tag_dispatch(self) -> bool:
        """Atomically reserve one per-run tag dispatch slot before submitting work."""
        with self.tag_dispatches_lock:
            if self.tag_llm_dispatch_exhausted.is_set():
                return False
            if (
                self.tag_max_dispatches is not None
                and self.tag_dispatches_count + self.tag_dispatches_reserved
                >= self.tag_max_dispatches
            ):
                self.tag_llm_dispatch_exhausted.set()
                return False
            self.tag_dispatches_reserved += 1
            return True

    def settle_tag_dispatch(self, dispatched: bool) -> None:
        """Release a reservation and count it only when a deferred job was actually queued."""
        with self.tag_dispatches_lock:
            self.tag_dispatches_reserved = max(0, self.tag_dispatches_reserved - 1)
            if not dispatched:
                return
            self.tag_dispatches_count += 1
            if (
                self.tag_max_dispatches is not None
                and self.tag_dispatches_count >= self.tag_max_dispatches
            ):
                self.tag_llm_dispatch_exhausted.set()

    def reserve_tag_prelabeler_dispatch(self) -> bool:
        """Atomically reserve one per-run pre-labeler dispatch slot before submitting work."""
        with self.tag_prelabeler_dispatches_lock:
            if self.tag_prelabeler_dispatch_exhausted.is_set():
                return False
            if (
                self.tag_prelabeler_max_dispatches is not None
                and self.tag_prelabeler_dispatches_count + self.tag_prelabeler_dispatches_reserved
                >= self.tag_prelabeler_max_dispatches
            ):
                self.tag_prelabeler_dispatch_exhausted.set()
                return False
            self.tag_prelabeler_dispatches_reserved += 1
            return True

    def settle_tag_prelabeler_dispatch(self, dispatched: bool) -> None:
        """Release a pre-labeler reservation and count it only when work was queued."""
        with self.tag_prelabeler_dispatches_lock:
            self.tag_prelabeler_dispatches_reserved = max(
                0, self.tag_prelabeler_dispatches_reserved - 1
            )
            if not dispatched:
                return
            self.tag_prelabeler_dispatches_count += 1
            if (
                self.tag_prelabeler_max_dispatches is not None
                and self.tag_prelabeler_dispatches_count >= self.tag_prelabeler_max_dispatches
            ):
                self.tag_prelabeler_dispatch_exhausted.set()


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
    aligned: int = 0  # Path A: WhisperX known-text alignment from source text
    transcribed: int = 0  # Path B: fresh faster-whisper transcription
    dispatched: int = 0  # H14a: handed to an external GPU backend (off-runner); pending next render
    asr_migration_copied: int = 0
    asr_migration_already_present: int = 0
    asr_migration_missing: int = 0
    asr_migration_regenerated: int = 0
    rate_limited: int = 0  # audio encodes that hit HTTP 403 / provider throttle (GH#300)
    defer_reasons: dict[str, int] = field(default_factory=dict)
    defer_samples: list[str] = field(default_factory=list)
    quality_counts: dict[str, int] = field(default_factory=dict)

    def defer(self, reason: str, count: int = 1, *, sample: str | None = None) -> None:
        """Record restartable work left for a later run, grouped by a stable reason token."""
        self.skipped += count
        self.defer_reasons[reason] = self.defer_reasons.get(reason, 0) + count
        if sample and len(self.defer_samples) < 5:
            self.defer_samples.append(sample)

    def quality(self, outcome: str, count: int = 1) -> None:
        self.quality_counts[outcome] = self.quality_counts.get(outcome, 0) + count

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


_STAGE_EMPTY_OK = frozenset(
    {
        "chapters",
        "timeline",
        "remap",
        "links",
        "agenda_text",
        "minutes_text",
        "tags",
        "chapter_agenda",
        "chapter_locator",
        "generated_chapters",
        "moments",
        "moment-judge",
        "moment-admission",
        "video-clips",
    }
)


def stage_output_pointer(name: str, ep: Episode) -> str | None:
    """Return the durable output identity used to self-heal cached completion markers."""
    if name == "audio":
        return ep.audio_key or ep.hosted_audio_url
    if name == "transcript":
        return ep.transcript_key or ep.transcript_hosted_url
    if name == "diarize":
        return ep.speakers_key or ep.speakers_url or ep.speakers_error
    if name == "native_diarize":
        return ep.speakers_key or ep.speakers_url
    if name == "moments":
        return ep.moments_llm_recipe_hash or (ep.moment_pullquote_candidates and "moments")
    if name in {"moment-judge", "moment-admission"}:
        return hashlib.sha256(
            json.dumps(ep.moment_pullquote_candidates, sort_keys=True, default=str).encode()
        ).hexdigest()
    if name == "video-clips":
        return (ep.moment_video_clip or {}).get("key") or (ep.moment_video_clip or {}).get("status")
    if name == "chapter_agenda":
        artifact = ep.generated_agenda_candidates or {}
        return artifact.get("artifact_key") or artifact.get("recipe") or artifact.get("status")
    if name in {"chapter_locator", "generated_chapters"}:
        artifact = ep.generated_agenda_candidates or {}
        return artifact.get("boundary_artifact_key") or ep.generated_chapters_spec_hash
    return None


def stage_input_fingerprint(
    stage: EnrichmentStage | str,
    ep: Episode,
    city: City,
    *,
    speaker_config: Mapping[str, Any] | None = None,
) -> str:
    """Hash only the inputs that can invalidate one stage.

    This intentionally excludes derived output pointers: a new provider row with the same media
    and metadata therefore keeps its prior completion marker, while a changed URL/chapter/timeline
    or recipe version invalidates only the affected stage.
    """
    name = stage if isinstance(stage, str) else stage.name
    common = {"uid": ep.uid or ep.guid, "provider": city.provider, "stage": name}
    if name in {"links", "agenda_text", "minutes_text"}:
        payload = {**common, "links": ep.links or {}, "video": ep.video_url}
    elif name == "chapters":
        payload = {
            **common,
            "video": ep.video_url,
            "duration": ep.source_duration_seconds,
            "kind": ep.media_kind,
        }
    elif name == "timeline":
        payload = {
            **common,
            "video": ep.video_url,
            "duration": ep.source_duration_seconds,
            "chapters": ep.source_chapters or ep.chapters,
        }
    elif name == "remap":
        payload = {
            **common,
            "timeline": dataclasses.asdict(ep.timeline) if ep.timeline else None,
            "chapters": ep.source_chapters or ep.chapters,
        }
    elif name == "audio":
        payload = {
            **common,
            "video": ep.video_url,
            "duration": ep.source_duration_seconds,
            "chapters": ep.chapters,
            "timeline": dataclasses.asdict(ep.timeline) if ep.timeline else None,
            "audio_rebuild": ep.audio_rebuild,
            "audio_recipe": AUDIO_PIPELINE_VERSION,
        }
    elif name == "transcript":
        payload = {
            **common,
            "audio": ep.audio_key or ep.hosted_audio_url,
            "audio_spec": ep.audio_spec_hash,
            "timeline": timeline_digest(ep.timeline, ep.sources) if ep.timeline else "",
            "recipe": TRANSCRIPT_PIPELINE_VERSION,
            "word_validation": TIMED_WORDS_VALIDATION_VERSION,
        }
    elif name == "diarize":
        payload = {
            **common,
            "transcript": ep.transcript_key or ep.transcript_hosted_url,
            "recipe": PROVIDER_DIARIZE_PIPELINE_VERSION,
        }
    elif name == "native_diarize":
        config = speaker_config or {}
        from citypods.speakers import PILOT_SCOPE_VERSION

        payload = {
            **common,
            "audio": ep.audio_key or ep.hosted_audio_url,
            "transcript": ep.transcript_key or ep.transcript_hosted_url,
            "transcript_words": ep.transcript_words_key,
            "recipe": {
                "pipeline": DIARIZE_PIPELINE_VERSION,
                "pilot_scope": PILOT_SCOPE_VERSION,
                "word_validation": TIMED_WORDS_VALIDATION_VERSION,
                "model": config.get("model", DEFAULT_DIARIZE_MODEL),
                "embedding_model": config.get("embedding_model", DEFAULT_EMBEDDING_MODEL),
            },
        }
    elif name == "chapter_agenda":
        from citypods.chapter_jobs import AGENDA_PROMPT_VERSION
        from citypods.chapter_titles import AGENDA_PRODUCTION_MODEL

        recipe = (
            f"{AGENDA_PROMPT_VERSION}:{AGENDA_PRODUCTION_MODEL}:{CHAPTER_AGENDA_PIPELINE_VERSION}"
        )
        payload = {
            **common,
            "agenda_artifact": (ep.links or {}).get("agenda_text_artifact_key"),
            "recipe": recipe,
        }
    elif name in {"chapter_locator", "generated_chapters"}:
        from citypods.chapter_jobs import LOCATOR_MODEL, LOCATOR_PROMPT_VERSION

        recipe = f"{LOCATOR_PROMPT_VERSION}:{LOCATOR_MODEL}:{CHAPTER_LOCATOR_PIPELINE_VERSION}"
        payload = {
            **common,
            "agenda_recipe": (ep.generated_agenda_candidates or {}).get("recipe"),
            "agenda_artifact": (ep.generated_agenda_candidates or {}).get("artifact_key"),
            "transcript": ep.transcript_key,
            "transcript_words": ep.transcript_words_key,
            "recipe": recipe,
        }
    elif name == "tags":
        payload = {
            **common,
            "input": ep.tags_input_fingerprint,
            "transcript": ep.transcript_key,
            "agenda": ep.agenda_text_url,
            "chapters": ep.chapters,
        }
    elif name == "moments":
        from citypods.moments import (
            COUNCIL_MOMENT_MODELS,
            DEFAULT_MOMENT_MODELS,
            MOMENTS_PIPELINE_VERSION,
            MOMENTS_PROMPT_VERSION,
        )

        payload = {
            **common,
            "transcript": ep.transcript_key or ep.transcript_hosted_url,
            "transcript_words": ep.transcript_words_key,
            "chapters": ep.chapters,
            "video": ep.video_url,
            "meeting_family": city.extra.get("meeting_family", "default"),
            "agenda_artifact": (ep.links or {}).get("agenda_text_artifact_key"),
            "recipe": {
                "pipeline": MOMENTS_PIPELINE_VERSION,
                "prompt": MOMENTS_PROMPT_VERSION,
                "council_models": COUNCIL_MOMENT_MODELS,
                "default_models": DEFAULT_MOMENT_MODELS,
            },
        }
    elif name in {"moment-admission", "moment-judge"}:
        payload = {**common, "candidates": ep.moment_pullquote_candidates}
    elif name == "video-clips":
        payload = {
            **common,
            "moments": ep.moments_llm_recipe_hash,
            "candidates": ep.moment_pullquote_candidates,
            "video": ep.video_url,
            "timeline": dataclasses.asdict(ep.timeline) if ep.timeline else None,
        }
    else:
        payload = {
            **common,
            "video": ep.video_url,
            "title": ep.title,
            "published": ep.published.isoformat(),
            "description": ep.description,
        }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _legacy_stage_complete(name: str, ep: Episode) -> bool:
    """Infer terminal completion for pre-1013 records without forcing a backfill."""
    if name == "audio":
        return bool(ep.hosted_audio_url or (ep.media_kind == "direct" and ep.audio_spec_hash))
    if name == "timeline":
        return ep.timeline is not None
    if name == "remap":
        return not ep.chapters or ep.chapters_basis.startswith("served") or ep.timeline is None
    if name == "chapters":
        return bool(ep.source_chapters or ep.chapters)
    if name == "links":
        return bool(ep.links)
    if name == "agenda_text":
        return not (ep.links or {}).get("agenda") and not (ep.links or {}).get("agenda_portal")
    if name == "minutes_text":
        return not (ep.links or {}).get("minutes")
    if name == "transcript":
        return bool(ep.transcript_key or ep.transcript_hosted_url)
    if name == "diarize":
        return bool(ep.speakers_key or ep.speakers_url or ep.speakers_error)
    if name == "native_diarize":
        # Native failures can be transient audio/model/storage errors, so never synthesize a
        # completion marker from the error string alone.
        return bool(ep.speakers_key or ep.speakers_url)
    if name == "tags":
        return ep.tags_input_fingerprint is not None or bool(ep.tags or ep.chapter_tags)
    if name == "moments":
        return bool(ep.moments_llm_recipe_hash or ep.moment_pullquote_candidates)
    if name == "video-clips":
        return bool(ep.moment_video_clip)
    if name == "chapter_agenda":
        return (ep.generated_agenda_candidates or {}).get("status") in {
            "completed",
            "accepted",
            "not_found",
            "not_applicable",
            "rejected",
        }
    if name in {"chapter_locator", "generated_chapters"}:
        return bool(
            (ep.generated_agenda_candidates or {}).get("locator_status")
            in {"completed", "rejected", "not_found"}
            or ep.generated_chapters_spec_hash
        )
    return False


def stage_is_dirty(
    stage: EnrichmentStage,
    ep: Episode,
    city: City,
    *,
    speaker_config: Mapping[str, Any] | None = None,
) -> bool:
    # Admission state and asynchronous judge results are external to episode inputs. Both stages
    # are cheap projections, so always revisit them rather than making a human decision wait for a
    # transcript/media mutation before it can take effect.
    if stage.name in {"moment-admission", "moment-judge", "speaker_identity"}:
        return True
    # Provider chapter markers are canonical.  A one-time visit converts any historical
    # generated-chapter state into an explicit fallback exclusion (and, where possible, cancels
    # its queued job); after that, avoid re-running either LLM stage for the episode.
    if stage.name == "chapter_agenda" and ep.source_chapters:
        return (ep.generated_agenda_candidates or {}).get("status") != "not_applicable"
    if stage.name in {"chapter_locator", "generated_chapters"} and ep.source_chapters:
        return (ep.generated_agenda_candidates or {}).get("locator_status") != "not_applicable"
    marker = ep.stage_completion.get(stage.name) if isinstance(ep.stage_completion, dict) else None
    if stage.name == "native_diarize" and isinstance(marker, dict):
        # A prior R7 run could have marked an unselected or prerequisite-missing episode complete
        # with no speaker artifact.  Revisit those records after the pilot scope/readiness fix.
        if marker.get("state") == "complete" and not marker.get("output"):
            return True
    fingerprint = stage_input_fingerprint(stage, ep, city, speaker_config=speaker_config)
    if not isinstance(marker, dict):
        if not _legacy_stage_complete(stage.name, ep):
            return True
        ep.stage_completion[stage.name] = {
            "state": "complete-empty" if stage.name in _STAGE_EMPTY_OK else "complete",
            "version": stage.version,
            "input_fingerprint": fingerprint,
            "output": stage_output_pointer(stage.name, ep),
        }
        return False
    return not (
        marker.get("state") in {"complete", "complete-empty"}
        and marker.get("version") == stage.version
        and marker.get("input_fingerprint") == fingerprint
        and marker.get("output") == stage_output_pointer(stage.name, ep)
    )


def _mark_stage_complete(
    stage: EnrichmentStage,
    episodes: list[Episode],
    city: City,
    stat: StageStats,
    *,
    speaker_config: Mapping[str, Any] | None = None,
) -> None:
    if stat.errors or stat.skipped:
        return
    if stage.name == "native_diarize":
        from citypods.speakers import pilot_selected

        episodes = [
            ep
            for ep in episodes
            if pilot_selected(speaker_config or {}, city.slug, ep.body)
            and stage_output_pointer(stage.name, ep)
        ]
    state = "complete-empty" if stage.name in _STAGE_EMPTY_OK else "complete"
    for ep in episodes:
        ep.stage_completion[stage.name] = {
            "state": state,
            "version": stage.version,
            "input_fingerprint": stage_input_fingerprint(
                stage, ep, city, speaker_config=speaker_config
            ),
            "output": stage_output_pointer(stage.name, ep),
            "completed_at": datetime.now(UTC).isoformat(),
        }


class MomentsStage:
    """Extract grounded R6 moments and apply the manual/calibrated admission policy."""

    name = "moments"
    version = "2"

    def process(
        self, provider, city: City, episodes: list[Episode], ctx: StageContext
    ) -> StageStats:
        from citypods.chapters import episode_served_chapters
        from citypods.compute.base import InferenceJob, JobHandle, JobResult
        from citypods.compute.llm_policy import LLMRequestPolicy
        from citypods.moments import (
            COUNCIL_MOMENT_MODELS,
            DEFAULT_MOMENT_MODELS,
            MOMENTS_CONTRACT,
            candidate_matrix_key,
            ensure_moment_contract,
            normalize_decision_candidate,
            normalize_quote_candidate,
            parse_transcript_segments,
            recipe_hash,
            response_payload,
        )

        stats = StageStats(self.name)
        if ctx.moment_backend is None or ctx.storage is None:
            return stats
        backend_config = getattr(ctx.moment_backend, "config", None)
        configured = tuple(
            str(model)
            for model in (
                [getattr(backend_config, "model", "")]
                + list(getattr(backend_config, "additional_models", ()) or ())
            )
            if model
        )
        family = str(city.extra.get("meeting_family") or "default")
        rollout = (ctx.moment_evaluation_config or {}).get("rollout_meeting_families") or []
        rollout_families = {str(value) for value in rollout}
        if rollout_families and family not in rollout_families:
            return stats
        allowed_models = COUNCIL_MOMENT_MODELS if family == "council" else DEFAULT_MOMENT_MODELS
        allowed_models = (
            tuple(model for model in allowed_models if model in configured) or allowed_models
        )
        ensure_moment_contract()

        for ep in episodes:
            if ctx.stop and ctx.stop():
                stats.defer("stop")
                continue
            if not ep.transcript_key:
                stats.quality("shadow-no-transcript")
                continue
            raw = _read_storage_bytes(ctx.storage, ep.transcript_key)
            segments = parse_transcript_segments(raw or b"", ep.transcript_format or "vtt")
            if not segments:
                stats.quality("shadow-no-captions")
                continue
            chapters = episode_served_chapters(ep)
            chapter_rows = [
                {
                    "chapter_id": str(row.get("id") or row.get("chapter_id") or index),
                    "title": str(row.get("title") or ""),
                    "start": row.get("start"),
                    "end": row.get("end"),
                }
                for index, row in enumerate(chapters)
                if isinstance(row, dict)
            ]
            transcript_text = " ".join(str(row.get("text") or "") for row in segments)
            agenda_key = (ep.links or {}).get("agenda_text_artifact_key")
            agenda_data = _read_storage_bytes(ctx.storage, agenda_key or "")
            agenda_text = (agenda_data or b"").decode("utf-8", errors="replace")[:80_000]
            moments_recipe = recipe_hash(
                transcript_key=ep.transcript_key,
                transcript_words_key=ep.transcript_words_key,
                chapters=chapter_rows,
                agenda_text_key=agenda_key,
                route_models=allowed_models,
                meeting_family=family,
                evaluation_policy="candidate-generation-v1",
            )
            if ep.moments_llm_recipe_hash == moments_recipe:
                stats.reused += 1
                continue
            messages = [
                {
                    "role": "system",
                    "content": (
                        "You extract civic meeting moments. Quote only exact contiguous wording "
                        "from the transcript. Return one summary point per supplied chapter. "
                        "Do not invent "
                        "votes, decisions, names, times, or outcomes."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "scope": "moments",
                            "chapters": chapter_rows,
                            "transcript": transcript_text,
                            "agenda_text": agenda_text,
                        },
                        ensure_ascii=False,
                    ),
                },
            ]
            inputs: dict[str, Any] = {
                "messages": messages,
                "structured_output": MOMENTS_CONTRACT,
                "max_tokens": 4096,
            }
            inputs["llm_policy"] = LLMRequestPolicy(
                allowed_models=allowed_models,
                allow_paid=False,
                purpose="r6-moments",
                queue_only=True,
                timeout_class="long",
            )
            with ctx.moment_dispatch_lock:
                if ctx.moment_dispatches >= ctx.moment_max_dispatches:
                    stats.defer("rollout-dispatch-cap", sample=ep.uid or ep.guid)
                    continue
                ctx.moment_dispatches += 1
            try:
                outcome = ctx.moment_backend.run_inference(
                    InferenceJob(
                        task="moment-extraction",
                        inputs=inputs,
                        recipe_hash=moments_recipe,
                    )
                )
                if isinstance(outcome, JobHandle):
                    stats.defer("llm-capacity", sample=ep.uid or ep.guid)
                    continue
                if not isinstance(outcome, JobResult) or not isinstance(outcome.output, dict):
                    raise ValueError("moment backend returned an invalid result")
                payload = response_payload(outcome.output)
                model = str(outcome.model or allowed_models[0])
            except Exception as exc:  # noqa: BLE001 - one bad model response cannot stop a feed.
                stats.errors.append(f"{ep.uid or ep.guid}: moment extraction deferred ({exc})")
                stats.defer("llm-error", sample=ep.uid or ep.guid)
                continue

            summary_by_chapter = {
                str(row.get("chapter_id")): row
                for row in payload.get("summary_points", [])
                if isinstance(row, dict)
            }
            required_chapters = {item["chapter_id"] for item in chapter_rows}
            if required_chapters and set(summary_by_chapter) != required_chapters:
                ep.moment_summary_candidates = []
                ep.moment_pullquote_candidates = []
                ep.moment_decision_candidates = []
                ep.moments_llm_recipe_hash = moments_recipe
                ep.moments_llm_call_attempts.append(
                    {
                        "purpose": "r6-moments",
                        "status": "rejected",
                        "reason": "summary-chapter-coverage",
                        "provider_model": model,
                        "recipe_hash": moments_recipe,
                    }
                )
                stats.quality("summary-chapter-coverage")
                continue
            ep.moment_summary_candidates = [
                {
                    "chapter_id": row["chapter_id"],
                    "text": row["text"],
                    "confidence": row.get("confidence", 0.0),
                    "source_kind": "llm",
                    "provider_model": model,
                    "prompt_version": "1",
                }
                for row in summary_by_chapter.values()
                if row.get("chapter_id") in {item["chapter_id"] for item in chapter_rows}
            ]
            candidates: list[dict[str, Any]] = []
            for raw_candidate in payload.get("pull_quotes", []):
                if not isinstance(raw_candidate, dict):
                    continue
                candidate = normalize_quote_candidate(
                    raw_candidate,
                    episode_uid=ep.uid or ep.guid,
                    provider_model=model,
                    recipe=moments_recipe,
                    meeting_family=family,
                    transcript_segments=segments,
                )
                if candidate:
                    candidates.append(candidate)
            ep.moment_pullquote_candidates = candidates
            ep.moment_decision_candidates = [
                normalized
                for row in payload.get("decisions", [])
                if isinstance(row, dict)
                if (
                    normalized := normalize_decision_candidate(
                        row, provider_model=model, transcript_segments=segments
                    )
                )
                is not None
            ]
            ep.moments_llm_recipe_hash = moments_recipe
            ep.moments_llm_call_attempts.append(
                {
                    "purpose": "r6-moments",
                    "status": "completed",
                    "provider_model": model,
                    "recipe_hash": moments_recipe,
                    "candidate_count": len(candidates),
                    "calibration_cells": [candidate_matrix_key(row) for row in candidates],
                }
            )
            stats.ran += 1
        return stats


def _ffprobe_binary(ctx: StageContext) -> str:
    binary = str(getattr(ctx.ffmpeg, "binary", "ffmpeg"))
    path = Path(binary)
    return str(path.with_name("ffprobe")) if path.name == "ffmpeg" else "ffprobe"


def _moment_source(
    provider, city: City, ep: Episode, candidate: dict[str, Any]
) -> tuple[str, str] | None:
    """Resolve the exact source media used by a served quote, never the provider page URL."""
    from citypods.clips import _clip_timeline

    source_id = ep.sources[0].id if ep.sources else "s0"
    if ep.timeline is not None:
        _timeline, cuts = _clip_timeline(
            ep.timeline, float(candidate.get("start") or 0), float(candidate.get("end") or 0)
        )
        if len(cuts) != 1:
            return None
        source_id = cuts[0][0]
    if ep.sources and len(ep.sources) > 1:
        source = next((item for item in ep.sources if item.id == source_id), None)
        if source is None:
            return None
        return source.ref, f"{source.id}:{source.ref}"
    resolved = provider.resolve_media_url(ep, city.source)
    identity = ep.sources[0].ref if ep.sources else ep.video_url
    return resolved, f"{source_id}:{identity}"


class MomentJudgeStage:
    """Run independent, candidate-only judges in the background without candidate authority."""

    name = "moment-judge"
    version = "1"

    def process(
        self, provider, city: City, episodes: list[Episode], ctx: StageContext
    ) -> StageStats:
        from citypods.compute.base import JobHandle, JobResult
        from citypods.moment_judging import (
            JUDGE_CONTRACT,
            JUDGE_PROMPT_VERSION,
            JUDGE_SCHEMA_VERSION,
            ensure_judge_contract,
            judge_input,
            judge_models,
            judge_policy,
        )
        from citypods.moments import parse_transcript_segments

        stats = StageStats(self.name)
        config = (ctx.moment_evaluation_config or {}).get("judges") or {}
        if not config.get("enabled") or ctx.moment_backend is None or ctx.storage is None:
            return stats
        configured = tuple(str(model) for model in config.get("models") or ())
        models = judge_models(configured)
        if not models:
            return stats
        ensure_judge_contract()
        for ep in episodes:
            raw = _read_storage_bytes(ctx.storage, ep.transcript_key or "")
            segments = parse_transcript_segments(raw or b"", ep.transcript_format or "vtt")
            for candidate in ep.moment_pullquote_candidates:
                if not isinstance(candidate, dict):
                    continue
                existing = [
                    row for row in candidate.get("judge_assessments") or [] if isinstance(row, dict)
                ]
                evidence = " ".join(
                    str(row.get("text") or "")
                    for row in segments
                    if float(row.get("end") or 0) > float(candidate.get("start") or 0)
                    and float(row.get("start") or 0) < float(candidate.get("end") or 0)
                )
                for model in models:
                    if any(
                        row.get("provider_model") == model
                        and row.get("prompt_version") == JUDGE_PROMPT_VERSION
                        and row.get("schema_version") == JUDGE_SCHEMA_VERSION
                        for row in existing
                    ):
                        continue
                    if ctx.stop and ctx.stop():
                        stats.defer("stop")
                        break
                    with ctx.moment_dispatch_lock:
                        if ctx.moment_dispatches >= ctx.moment_max_dispatches:
                            stats.defer("rollout-dispatch-cap", sample=ep.uid or ep.guid)
                            return stats
                        ctx.moment_dispatches += 1
                    inputs: dict[str, Any] = {
                        "messages": [
                            {
                                "role": "system",
                                "content": (
                                    "You are an independent civic-publication judge. Score only "
                                    "the candidate and evidence. Never rewrite or create one."
                                ),
                            },
                            {
                                "role": "user",
                                "content": json.dumps(judge_input(candidate, evidence)),
                            },
                        ],
                        "structured_output": JUDGE_CONTRACT,
                        "llm_policy": judge_policy((model,)),
                        "max_tokens": 700,
                    }
                    outcome = ctx.moment_backend.run_inference(
                        InferenceJob(
                            task="moment-judge",
                            inputs=inputs,
                            recipe_hash=f"{candidate.get('candidate_id')}:{model}:{JUDGE_PROMPT_VERSION}",
                        )
                    )
                    if isinstance(outcome, JobHandle):
                        stats.defer("judge-capacity", sample=ep.uid or ep.guid)
                        continue
                    if not isinstance(outcome, JobResult) or not isinstance(outcome.output, dict):
                        stats.quality("judge-invalid-response")
                        continue
                    try:
                        content = outcome.output["choices"][0]["message"]["content"]
                        payload = ensure_judge_contract().model_validate_json(content).model_dump()
                    except (KeyError, IndexError, TypeError, ValueError):
                        stats.quality("judge-invalid-response")
                        continue
                    existing.append(
                        {
                            **payload,
                            "provider_model": str(outcome.model or model),
                            "prompt_version": JUDGE_PROMPT_VERSION,
                            "schema_version": JUDGE_SCHEMA_VERSION,
                        }
                    )
                    if ctx.moment_evaluation_state_path:
                        from citypods.moment_evaluation import append_judge_observation

                        append_judge_observation(
                            ctx.moment_evaluation_state_path, candidate, existing[-1]
                        )
                    stats.ran += 1
                candidate["judge_assessments"] = existing
        return stats


class MomentAdmissionStage:
    """Apply immutable human decisions and qualified model/judge policies to existing candidates."""

    name = "moment-admission"
    version = "1"

    def process(
        self, provider, city: City, episodes: list[Episode], ctx: StageContext
    ) -> StageStats:
        from citypods.moment_evaluation import apply_admission, load_state, refresh_policies
        from citypods.moments import parse_transcript_segments, quote_safety_gate
        from citypods.video_clips import technical_video_gate

        stats = StageStats(self.name)
        if ctx.storage is None:
            return stats
        state = (
            load_state(ctx.moment_evaluation_state_path) if ctx.moment_evaluation_state_path else {}
        )
        refresh_policies(state)
        mode = str((ctx.moment_evaluation_config or {}).get("mode") or "manual")
        for ep in episodes:
            if ctx.stop and ctx.stop():
                stats.defer("stop")
                continue
            raw = _read_storage_bytes(ctx.storage, ep.transcript_key or "")
            segments = parse_transcript_segments(raw or b"", ep.transcript_format or "vtt")
            updated: list[dict[str, Any]] = []
            for candidate in ep.moment_pullquote_candidates:
                if not isinstance(candidate, dict):
                    continue
                # Apply manual controls before resolving media: an explicit served range may map
                # to a different source cut, and must be what both the safety gate and renderer use.
                prepared = apply_admission(candidate, state, technical_gate=False, global_mode=mode)
                try:
                    source = _moment_source(provider, city, ep, prepared)
                except Exception:  # noqa: BLE001 - unresolved provider media is text-only.
                    source = None
                gate = False
                if source:
                    cache_key = source[1]
                    with ctx.moment_video_gate_lock:
                        cached = ctx.moment_video_gate_cache.get(cache_key)
                    if cached is None:
                        cached = technical_video_gate(
                            source[0],
                            probe_binary=_ffprobe_binary(ctx),
                            captions_available=bool(segments),
                            withheld=bool(
                                ep.media_availability and ep.media_availability.is_withheld()
                            ),
                        )
                        with ctx.moment_video_gate_lock:
                            ctx.moment_video_gate_cache[cache_key] = cached
                    gate = cached
                admitted = apply_admission(prepared, state, technical_gate=gate, global_mode=mode)
                if admitted.get("admission") == "admitted" and not quote_safety_gate(
                    admitted, segments
                ):
                    admitted.update(
                        {
                            "admission": "admitted_text_only",
                            "display": True,
                            "admission_reason": "timing-override-gate",
                        }
                    )
                updated.append(admitted)
            ep.moment_pullquote_candidates = updated
            if updated:
                stats.ran += 1
        return stats


class VideoClipsStage:
    """Materialize the highest-ranked admitted R6 quote as a social video."""

    name = "video-clips"
    version = "2"

    def process(
        self, provider, city: City, episodes: list[Episode], ctx: StageContext
    ) -> StageStats:
        from citypods.moments import parse_transcript_segments
        from citypods.video_clips import render_video_clip

        stats = StageStats(self.name)
        if ctx.storage is None or ctx.dry_run:
            return stats
        for ep in episodes:
            if ctx.stop and ctx.stop():
                stats.defer("stop")
                continue
            admitted = [
                candidate
                for candidate in ep.moment_pullquote_candidates
                if isinstance(candidate, dict) and candidate.get("admission") == "admitted"
            ]
            if not admitted:
                continue
            selected = max(
                admitted,
                key=lambda candidate: (
                    float(candidate.get("quality_score") or 0),
                    float(candidate.get("start") or 0),
                ),
            )
            raw = _read_storage_bytes(ctx.storage, ep.transcript_key or "")
            segments = parse_transcript_segments(raw or b"", ep.transcript_format or "vtt")
            try:
                source = _moment_source(provider, city, ep, selected)
            except Exception:  # noqa: BLE001 - provider resolution is a text-only failure.
                source = None
            if source is None:
                clip = {"status": "video-unavailable", "reason": "source-timeline"}
            else:
                clip = render_video_clip(
                    ep,
                    selected,
                    source_url=source[0],
                    source_identity=source[1],
                    binary=str(getattr(ctx.ffmpeg, "binary", "ffmpeg")),
                    probe_binary=_ffprobe_binary(ctx),
                    storage=ctx.storage,
                    segments=segments,
                    timeline_version=(
                        timeline_digest(ep.timeline, ep.sources) if ep.timeline else "identity"
                    ),
                    crop_anchor=selected.get("crop_anchor"),
                    caption_override=selected.get("caption"),
                    profile=str(selected.get("output_profile") or "vertical-9x16-square-pane-v1"),
                )
            if clip.get("status") == "ready":
                ep.moment_video_clip = {
                    **clip,
                    "candidate_id": selected.get("candidate_id"),
                    "served_start": selected.get("start"),
                    "served_end": selected.get("end"),
                }
            elif (ep.moment_video_clip or {}).get("key"):
                ep.moment_video_clip = {
                    **ep.moment_video_clip,
                    "last_error": clip.get("reason"),
                    "last_error_candidate_id": selected.get("candidate_id"),
                }
            else:
                ep.moment_video_clip = {
                    **clip,
                    "candidate_id": selected.get("candidate_id"),
                    "served_start": selected.get("start"),
                    "served_end": selected.get("end"),
                }
            if clip.get("status") == "ready":
                stats.ran += 1
            else:
                stats.quality(clip.get("reason") or "video-unavailable")
        return stats


class TagsStage:
    """Populate the versioned taxonomy from agenda titles/transcripts.

    Rules are cheap and always run. Instructor-backed LLM suggestions are dispatched and retained
    as shadow candidates; the generic calibration policy decides which candidates become visible.
    A pending dispatch or a stop signal leaves the deterministic result persisted for the next run.
    """

    name = "tags"
    version = "2"

    def process(
        self, provider, city: City, episodes: list[Episode], ctx: StageContext
    ) -> StageStats:
        import yaml

        from citypods.chapters import episode_served_chapters
        from citypods.llm_evaluation import (
            apply_admission,
            config_from_mapping,
            load_state,
            policy_fingerprint,
            visible_candidates,
        )
        from citypods.tags import (
            TAG_PROMPT_VERSION,
            TAGGER_VERSION,
            agenda_document_context,
            chapter_tag_inputs,
            decorate_llm_candidates,
            decorate_rule_candidates,
            episode_tag_inputs,
            llm_prelabel_candidates,
            llm_tag_suggestions,
            load_taxonomy,
            merge_tag_sources,
            rollup_tags,
            rule_phrase_audit,
            tag_episode,
            tag_input_fingerprint,
            tag_recipe_hash,
        )

        stats = StageStats(self.name)

        def remember_call_attempt(
            episode: Episode,
            *,
            purpose: str,
            recipe_hash: str,
            status: str,
            metadata: dict[str, Any] | None = None,
            model: str = "",
            reason: str = "",
        ) -> None:
            """Persist compact provenance for every actual tag/evaluator call.

            Candidate rows carry the same metadata when a call returns a candidate. This sidecar
            list covers empty, deferred, oversized, and failed calls without creating synthetic tag
            candidates that could enter calibration or public projection.
            """
            payload = dict(metadata or {})
            payload.update(
                {
                    "purpose": purpose,
                    "recipe_hash": recipe_hash,
                    "status": status,
                    "model": model or payload.get("prelabeler_model") or "",
                    "reason": reason,
                    "attempted_at": datetime.now(UTC).isoformat(),
                }
            )
            # Rule observations are deterministic for a recipe/scope and can be re-encountered
            # while an unrelated evaluator call is pending. Keep one such audit instead of
            # copying the same telemetry into the record on every scheduled run.
            if purpose == "topic-tags:rules" and any(
                prior.get("purpose") == purpose
                and prior.get("recipe_hash") == recipe_hash
                and prior.get("scope") == payload.get("scope")
                and prior.get("chapter_id") == payload.get("chapter_id")
                for prior in episode.tags_llm_call_attempts
                if isinstance(prior, dict)
            ):
                return
            # One model invocation is one durable event. Bound this sidecar because records are
            # restored wholesale; candidate history itself remains append-only for audit.
            payload["attempt_id"] = (
                "tag-attempt-"
                + hashlib.sha1(
                    json.dumps(payload, sort_keys=True, default=str).encode()
                ).hexdigest()[:20]
            )
            episode.tags_llm_call_attempts = [
                *episode.tags_llm_call_attempts,
                payload,
            ][-200:]

        # Load the taxonomy + calibration state at most ONCE for the whole run (cached on `ctx`,
        # which is the same object across every one of this lane's per-episode process() calls --
        # see `StageContext.tag_taxonomy_cache`), not once per episode. A prior failure is cached
        # too (as an error string) so a broken taxonomy/state file reports itself on every call
        # without re-attempting the same failing local-disk read thousands of times.
        #
        # The global queue runs this across a worker thread pool sharing one `ctx`, so the whole
        # check-then-populate sequence is guarded by `tag_taxonomy_cache_lock`: without it, one
        # thread's cache writes (three separate dict assignments, not atomic as a group) could be
        # interleaved with another thread's read of a still-incomplete cache -- e.g. a second
        # thread seeing `evaluation_state` already written but `admission_policy` not yet, skipping
        # (re-)population entirely, and then KeyError-ing on the read below. Contention only matters
        # for the first handful of calls (a warm cache read is a fast, lock-guarded dict lookup).
        with ctx.tag_taxonomy_cache_lock:
            cache = ctx.tag_taxonomy_cache
            if "taxonomy_error" in cache:
                stats.errors.append(cache["taxonomy_error"])
                return stats
            if "eval_error" in cache:
                stats.errors.append(cache["eval_error"])
                return stats
            if "taxonomy" not in cache:
                try:
                    cache["taxonomy"] = load_taxonomy(ctx.taxonomy_path)
                except (OSError, ValueError, KeyError, IndexError, yaml.YAMLError) as exc:
                    # yaml.YAMLError (parse/scan errors) is NOT a ValueError subclass, and PyYAML
                    # is documented to leak raw ValueError/KeyError/IndexError for some malformed
                    # explicit-tag scalars (e.g. `!!int nope`) instead of wrapping them in
                    # yaml.YAMLError -- both must be caught here for a genuinely corrupt
                    # taxonomy.yml to degrade gracefully (cached, reported once) rather than
                    # propagate uncaught out of every one of this run's per-episode calls.
                    cache["taxonomy_error"] = f"taxonomy unavailable: {exc}"
                    stats.errors.append(cache["taxonomy_error"])
                    return stats
            if "evaluation_state" not in cache:
                try:
                    evaluation_config = config_from_mapping(ctx.llm_evaluation_config)
                    evaluation_state = (
                        load_state(ctx.llm_evaluation_state_path)
                        if ctx.llm_evaluation_state_path is not None
                        else {"version": 1, "reviews": {}, "matrix": [], "trend": []}
                    )
                    cache["evaluation_config"] = evaluation_config
                    cache["evaluation_state"] = evaluation_state
                    cache["admission_policy"] = policy_fingerprint(
                        evaluation_config, evaluation_state
                    )
                except (ValueError, TypeError) as exc:
                    # load_state() fails closed on a corrupted (not merely missing) state file
                    # rather than silently resetting it -- that protects against this stage's
                    # caller later clobbering real review history via save_state(), but this stage
                    # itself only ever *reads* the file, so degrading tagging for this run (retried
                    # next run once the file is fixed) is the right response here, not crashing the
                    # whole city's enrich pass. config_from_mapping()/policy_fingerprint() are
                    # covered too: a malformed tagging.evaluation config (e.g. non-numeric
                    # minimum_reviews) must degrade the same way, not re-raise on every episode.
                    cache["eval_error"] = f"LLM evaluation state unavailable: {exc}"
                    stats.errors.append(cache["eval_error"])
                    return stats

            taxonomy = cache["taxonomy"]
            evaluation_config = cache["evaluation_config"]
            evaluation_state = cache["evaluation_state"]
            admission_policy = cache["admission_policy"]
            prelabeler_config = ctx.llm_evaluation_config.get("prelabeler") or {}
            prelabeler_enabled = bool(prelabeler_config.get("enabled", False)) and (
                ctx.tag_backend is not None
            )
            prelabeler_model = str(prelabeler_config.get("model") or "")
            prelabeler_prompt_version = str(prelabeler_config.get("prompt_version") or "1")
            prelabeler_llm_schema_version = str(prelabeler_config.get("llm_schema_version") or "1")

        for ep in _materialize_set(
            episodes,
            city.full_artifact_episodes,
            feed_visible_per_body=city.max_episodes,
            policy=ctx.backlog_policy,
            city_slug=city.slug,
        ):
            llm_route = (
                f"{getattr(ctx.tag_backend, 'name', 'litellm')}:"
                f"{getattr(getattr(ctx.tag_backend, 'config', None), 'model', '')}"
                if ctx.tag_backend is not None
                else ""
            )
            llm_enabled = ctx.tag_backend is not None
            from citypods.llm_evaluation import candidate_id

            persisted_candidates: list[dict[str, Any]] = []
            for raw_candidate in ep.llm_tag_candidates or []:
                if not isinstance(raw_candidate, dict):
                    continue
                candidate = dict(raw_candidate)
                candidate.setdefault("source_kind", "llm")
                candidate.setdefault("assessment_kind", "tagger-admission")
                candidate.setdefault(
                    "scope", "chapter" if candidate.get("chapter_id") else "episode"
                )
                candidate["candidate_id"] = str(
                    candidate.get("candidate_id") or candidate_id(candidate)
                )
                persisted_candidates.append(candidate)
            prelabeler_pending = prelabeler_enabled and any(
                candidate.get("candidate_state") != "historical"
                and (candidate.get("source_kind", "llm") == "rule" or candidate.get("chapter_id"))
                and (
                    candidate.get("prelabeler_model") != prelabeler_model
                    or candidate.get("prelabeler_prompt_version") != prelabeler_prompt_version
                    or candidate.get("prelabeler_llm_schema_version")
                    != prelabeler_llm_schema_version
                    or candidate.get("prelabeler_decision")
                    not in {
                        "likely_correct",
                        "needs_human_review",
                        "likely_incorrect",
                    }
                )
                for candidate in persisted_candidates
            )
            ledger_missing = bool(ep.tags or ep.chapter_tags) and not persisted_candidates
            cheap_fingerprint = tag_input_fingerprint(
                ep,
                taxonomy,
                llm_enabled=llm_enabled,
                llm_route=llm_route,
                prompt_version=TAG_PROMPT_VERSION,
                admission_policy=admission_policy,
            )
            # ---- In-memory triage, BEFORE any storage fetch -----------------------------------
            # Everything needed to decide what to do with this episode is already in the loaded
            # record + the storage-free `tag_input_fingerprint`. The two agenda/transcript fetches
            # below are the real cost of walking this lane's ~backlog-sized queue, so we classify
            # first and only fetch for episodes that will actually be tagged this run.
            has_chapters = bool(episode_served_chapters(ep))
            inputs_unchanged = (
                ep.tags_input_fingerprint is not None
                and ep.tags_input_fingerprint == cheap_fingerprint
                and ep.tags_spec_hash is not None
                and (not has_chapters or ep.chapter_tags)
            )
            # Whether this episode still wants a *new* LLM tag is decidable purely in memory: for
            # unchanged inputs a set `tags_llm_recipe_hash` means the LLM tag is already current;
            # `None` means it never resolved (dispatched-and-deferred, quota-parked, or errored).
            # Rules tags don't need the LLM, so an LLM-disabled run is never "pending".
            llm_pending = llm_enabled and ep.tags_llm_recipe_hash is None

            if (
                inputs_unchanged
                and not llm_pending
                and not prelabeler_pending
                and not ledger_missing
            ):
                # Fully resolved for the current inputs (or LLM disabled). Nothing to do -- skip
                # WITHOUT any storage fetch. This is the steady state for the whole catalog and has
                # to stay an in-memory O(1) check, never a storage round trip.
                stats.reused += 1
                continue

            if ctx.stop is not None and ctx.stop():
                # Wall-clock budget spent: defer everything still outstanding WITHOUT fetching
                # (untouched, retried next run) so the pass drains cheaply to its end-of-run
                # persist instead of grinding the backlog's fetches into GitHub's hard job timeout.
                stats.defer("tag-budget-stop", sample=ep.uid or ep.guid)
                continue

            # Once every still-needed LLM purpose has exhausted its own run allowance, this
            # unchanged record cannot make useful progress. Do not read its agenda/transcript
            # merely to rediscover that fact: the 2026-09-04 tag run continued doing exactly that
            # across more than 15k records after the tagger allowance had filled.
            tagger_unavailable = llm_pending and ctx.tag_llm_dispatch_exhausted.is_set()
            prelabeler_unavailable = (
                prelabeler_pending and ctx.tag_prelabeler_dispatch_exhausted.is_set()
            )
            if (
                inputs_unchanged
                and not ledger_missing
                and (tagger_unavailable or prelabeler_unavailable)
                and (not llm_pending or tagger_unavailable)
                and (not prelabeler_pending or prelabeler_unavailable)
            ):
                stats.defer(
                    "tag-llm-no-quota" if tagger_unavailable else "tag-prelabeler-no-quota",
                    sample=ep.uid or ep.guid,
                )
                continue

            titles, agenda_text, transcript_text = episode_tag_inputs(ep, ctx.storage)
            chapters = chapter_tag_inputs(ep, ctx.storage)
            chapter_fingerprint = [
                {
                    "chapter_id": item["chapter_id"],
                    "title": item["title"],
                    "start": item["start"],
                    "end": item["end"],
                    "agenda_text": item.get("agenda_text", ""),
                    "transcript_text": item.get("transcript_text", ""),
                    "transcript_segments": item.get("transcript_segments", []),
                }
                for item in chapters
            ]
            rules_hash = tag_recipe_hash(
                taxonomy,
                agenda_item_titles=titles,
                agenda_text=agenda_text,
                transcript_text=transcript_text,
                llm_enabled=False,
                chapter_inputs=chapter_fingerprint,
            )
            llm_recipe = tag_recipe_hash(
                taxonomy,
                # The initial production LLM contract is chapter-only. Chapter title, mapped
                # agenda evidence, transcript window, and timestamped segments are supplied below;
                # episode-wide/backup text remains deterministic-only context.
                agenda_item_titles="",
                agenda_text="",
                transcript_text="",
                llm_enabled=llm_enabled,
                chapter_inputs=chapter_fingerprint,
                llm_route=llm_route,
                prompt_version=TAG_PROMPT_VERSION,
            )
            projection_hash = tag_recipe_hash(
                taxonomy,
                agenda_item_titles=titles,
                agenda_text=agenda_text,
                transcript_text=transcript_text,
                llm_enabled=llm_enabled,
                chapter_inputs=chapter_fingerprint,
                llm_route=llm_route,
                prompt_version=TAG_PROMPT_VERSION,
                admission_policy=admission_policy,
            )
            if (
                ep.tags_spec_hash == projection_hash
                and (not chapters or ep.chapter_tags)
                and not prelabeler_pending
                and not ledger_missing
            ):
                # Backfill the cheap fingerprint so the pre-check above can short-circuit this
                # episode next run without the storage fetch just paid for above. Without this,
                # every episode that was already fully resolved *before* tags_input_fingerprint
                # existed (i.e. the entire pre-existing backlog on the first run after it was
                # added) hits this branch, not the pre-check, forever -- it's a terminal state
                # (tags_spec_hash already equals this run's full projection_hash) exactly like the
                # bottom-of-loop `fingerprint_after` case, just reached without a diff to persist.
                if ep.tags_input_fingerprint != cheap_fingerprint:
                    ep.tags_input_fingerprint = cheap_fingerprint
                    stats.ran += 1
                else:
                    stats.reused += 1
                continue

            def project_visible_tags(
                candidates: list[dict[str, Any]], annotations: list[dict[str, Any]]
            ) -> list[dict[str, Any]]:
                """Project the active ledger consistently after every state transition."""
                projectable = [
                    candidate
                    for candidate in candidates
                    if candidate.get("source_kind", "llm") == "rule" or candidate.get("chapter_id")
                ]
                visible = (
                    visible_candidates(
                        projectable, config=evaluation_config, state=evaluation_state
                    )
                    if projectable
                    else []
                )
                episode_tags = merge_tag_sources(
                    [], [tag for tag in visible if not tag.get("chapter_id")]
                )
                for annotation in annotations:
                    annotation["tags"] = merge_tag_sources(
                        [],
                        [
                            tag
                            for tag in visible
                            if tag.get("chapter_id") == annotation["chapter_id"]
                        ],
                    )
                return rollup_tags(episode_tags, annotations, taxonomy)

            def rule_audit_for(
                tags: list[dict[str, Any]], agenda: str, transcript: str
            ) -> list[dict[str, Any]]:
                # `tag_episode(..., include_rule_metadata=True)` already scanned includes to
                # produce rule evidence. Reuse those observations and scan only excludes here.
                includes = [
                    observation
                    for tag in tags
                    for observation in tag.get("rule_audit", [])
                    if isinstance(observation, dict)
                ]
                return includes + rule_phrase_audit(agenda, transcript, taxonomy, include=False)

            rule_tags = tag_episode(
                titles + "\n" + agenda_text,
                transcript_text,
                taxonomy,
                include_rule_metadata=True,
            )
            episode_rule_audit = rule_audit_for(
                rule_tags, titles + "\n" + agenda_text, transcript_text
            )
            if episode_rule_audit:
                remember_call_attempt(
                    ep,
                    purpose="topic-tags:rules",
                    recipe_hash=rules_hash,
                    status="resolved",
                    metadata={
                        "scope": "episode",
                        "rule_audit": episode_rule_audit,
                    },
                    model=f"rule:{TAGGER_VERSION}",
                )
            chapter_annotations = []
            for chapter in chapters:
                chapter_rules = tag_episode(
                    chapter["title"] + "\n" + chapter.get("agenda_text", ""),
                    chapter.get("transcript_text", ""),
                    taxonomy,
                    include_rule_metadata=True,
                )
                chapter_rule_audit = rule_audit_for(
                    chapter_rules,
                    chapter["title"] + "\n" + chapter.get("agenda_text", ""),
                    chapter.get("transcript_text", ""),
                )
                if chapter_rule_audit:
                    remember_call_attempt(
                        ep,
                        purpose="topic-tags:rules",
                        recipe_hash=rules_hash,
                        status="resolved",
                        metadata={
                            "scope": "chapter",
                            "chapter_id": chapter["chapter_id"],
                            "rule_audit": chapter_rule_audit,
                        },
                        model=f"rule:{TAGGER_VERSION}",
                    )
                for tag in chapter_rules:
                    for evidence in tag.get("evidence", []):
                        evidence["chapter_id"] = chapter["chapter_id"]
                        if evidence.get("where") == "transcript":
                            evidence["t"] = next(
                                (
                                    round(float(segment["start"]), 3)
                                    for segment in chapter.get("transcript_segments", [])
                                    if evidence["span"].casefold() in segment["text"].casefold()
                                ),
                                None,
                            )
                chapter_annotations.append(
                    {"chapter_id": chapter["chapter_id"], "tags": chapter_rules}
                )
            rule_candidates = decorate_rule_candidates(
                rule_tags,
                episode_uid=ep.uid,
                episode_title=ep.title,
                taxonomy=taxonomy,
                recipe_hash=rules_hash,
            )
            for annotation in chapter_annotations:
                rule_candidates.extend(
                    decorate_rule_candidates(
                        annotation["tags"],
                        episode_uid=ep.uid,
                        episode_title=ep.title,
                        taxonomy=taxonomy,
                        recipe_hash=rules_hash,
                        chapter_id_value=annotation["chapter_id"],
                    )
                )
            completed_llm_recipe = ep.tags_llm_recipe_hash
            persisted_rule_candidates = [
                dict(candidate)
                for candidate in persisted_candidates
                if candidate.get("source_kind") == "rule"
            ]
            persisted_llm_candidates = [
                dict(candidate)
                for candidate in persisted_candidates
                if candidate.get("source_kind", "llm") != "rule"
            ]
            prior_by_id = {
                str(candidate.get("candidate_id")): candidate
                for candidate in persisted_rule_candidates
                if candidate.get("candidate_id")
            }
            # A deterministic match with the same recipe identity keeps its evaluator result and
            # review provenance when the stage re-projects it. New rule candidates still receive
            # fresh source evidence, while old identities remain available for the historical lane.
            for current in rule_candidates:
                prior = prior_by_id.get(str(current.get("candidate_id")))
                if prior is not None:
                    for field in (
                        "prelabeler_model",
                        "prelabeler_prompt_version",
                        "prelabeler_llm_schema_version",
                        "prelabeler_decision",
                        "prelabeler_confidence",
                        "prelabeler_reason",
                        "prelabeler_evidence_supported",
                        "prelabeler_input_digest",
                        "prelabeler_batch_index",
                    ):
                        if field in prior:
                            current[field] = prior[field]
            active_candidate_ids: set[str] = set()
            if llm_enabled:
                active_cached = [
                    candidate
                    for candidate in persisted_llm_candidates
                    if completed_llm_recipe == llm_recipe
                    # R5's LLM projection is chapter-only. Keep legacy episode-level rows in the
                    # ledger for audit/backfill, but never feed them back into public projection.
                    and candidate.get("chapter_id")
                ]
                candidate_tags = active_cached
                active_candidate_ids = {
                    str(candidate.get("candidate_id") or "") for candidate in active_cached
                }
            elif ep.llm_tag_candidates:
                # llm disabled/unavailable this run (dry run, misconfigured LLM_MODEL, ...) must
                # still validate persisted candidates against current inputs, not republish them
                # unconditionally: a taxonomy/transcript/agenda change should invalidate them even
                # though there's no live backend this run to confirm that via llm_recipe (which is
                # a materially different hash shape when llm_enabled=False). Recompute the recipe
                # the candidates were actually generated under, using THEIR OWN recorded route/
                # prompt_version rather than this run's (there may be none) -- if every other input
                # is unchanged, this reproduces completed_llm_recipe exactly; if not, it correctly
                # diverges and the stale candidates are dropped.
                cached = next(
                    (
                        candidate
                        for candidate in ep.llm_tag_candidates
                        if candidate.get("source_kind", "llm") != "rule"
                    ),
                    None,
                )
                if cached is None:
                    candidate_tags = []
                else:
                    cached_recipe = tag_recipe_hash(
                        taxonomy,
                        agenda_item_titles="",
                        agenda_text="",
                        transcript_text="",
                        llm_enabled=True,
                        chapter_inputs=chapter_fingerprint,
                        llm_route=str(cached.get("provider_model") or ""),
                        prompt_version=str(cached.get("prompt_version") or TAG_PROMPT_VERSION),
                    )
                    candidate_tags = (
                        [
                            candidate
                            for candidate in persisted_llm_candidates
                            if completed_llm_recipe == cached_recipe and candidate.get("chapter_id")
                        ]
                        if completed_llm_recipe == cached_recipe
                        else []
                    )
                    active_candidate_ids = {
                        str(candidate.get("candidate_id") or "") for candidate in candidate_tags
                    }
            else:
                candidate_tags = []
            historical_candidates = [
                {
                    **candidate,
                    "candidate_state": "historical",
                    "display": False,
                }
                for candidate in [*persisted_rule_candidates, *persisted_llm_candidates]
                if str(candidate.get("candidate_id") or "") not in active_candidate_ids
                and str(candidate.get("candidate_id") or "")
                not in {str(item.get("candidate_id") or "") for item in rule_candidates}
            ]
            candidate_tags = rule_candidates + candidate_tags
            # Visibility is a pure projection of whatever candidates are already on hand, not of
            # whether *this* run can dispatch a new LLM call: gating it on ``llm_enabled`` would
            # strip already-admitted candidates from ``ep.tags``/``ep.chapter_tags`` the moment
            # tagging is disabled or a dry run has no live backend, even though candidate_tags
            # (preserved above) is untouched.
            final_tags = project_visible_tags(candidate_tags, chapter_annotations)
            final_hash = projection_hash if completed_llm_recipe == llm_recipe else rules_hash
            if llm_enabled and completed_llm_recipe != llm_recipe and chapters:
                if ctx.stop is not None and ctx.stop():
                    stats.defer("tag-llm-stop", sample=ep.uid or ep.guid)
                    candidate_tags = rule_candidates
                    completed_llm_recipe = None
                elif ctx.tag_llm_dispatch_exhausted.is_set():
                    stats.defer("tag-llm-no-quota", sample=ep.uid or ep.guid)
                    # The LLM request is the only unavailable part of this episode. Keep the
                    # deterministic rules and any applicable cached candidates in the ledger and
                    # projection; only the unresolved recipe remains unset for a later run.
                    completed_llm_recipe = None
                elif not ctx.reserve_tag_dispatch():
                    stats.defer("tag-llm-no-quota", sample=ep.uid or ep.guid)
                    completed_llm_recipe = None
                else:
                    dispatch_settled = False
                    try:
                        # The tag lane's heartbeat prints PROGRESS's snapshot on every tick
                        # ("active work: ..."), but until now nothing in this stage ever
                        # registered with it -- a real live dispatch (which can pace/sleep inside
                        # `llm_tag_suggestions` while waiting out a per-minute quota window) was
                        # indistinguishable from a genuinely stuck run: both showed "no tracked
                        # work active" for the heartbeat's whole run. This is the one call in the
                        # tag lane actually worth tracking (network round trip + pacing waits).
                        tag_call_metadata: dict[str, Any] = {}
                        with PROGRESS.track(
                            source=city.slug,
                            uid=str(ep.uid or ep.guid),
                            phase="tag-llm-dispatch",
                        ):
                            (
                                suggestions,
                                chapter_suggestions,
                                dispatched,
                                resolved_model,
                            ) = llm_tag_suggestions(
                                ctx.tag_backend,
                                taxonomy=taxonomy,
                                agenda_item_titles="",
                                agenda_text="",
                                transcript_text="",
                                recipe_hash=llm_recipe,
                                chapter_inputs=chapters,
                                agenda_documents=agenda_document_context(ep),
                                call_metadata_out=tag_call_metadata,
                                # Non-interactive tagging: dispatch calls are unpaced on the runner
                                # and should not carry an expiring deadline to the worker queue.
                                deadline_at=None,
                            )
                        queued_dispatch = bool(dispatched and resolved_model != "payload-too-large")
                        ctx.settle_tag_dispatch(queued_dispatch)
                        dispatch_settled = True
                        remember_call_attempt(
                            ep,
                            purpose="topic-tags:tagger",
                            recipe_hash=llm_recipe,
                            status="deferred" if dispatched else "resolved",
                            metadata=tag_call_metadata,
                            model=resolved_model or llm_route,
                            reason=(resolved_model or "") if dispatched else "",
                        )
                        if dispatched:
                            completed_llm_recipe = None
                            stats.defer(
                                "tag-llm-oversized"
                                if resolved_model == "payload-too-large"
                                else "tag-llm-dispatch",
                                sample=ep.uid or ep.guid,
                            )
                        else:
                            # Prefer the scheduler's actually-resolved model (a defensive read,
                            # not a load-bearing one: the policy above pins allowed_models to
                            # exactly the configured route, so the two only diverge if that
                            # pinning is ever loosened) over the precomputed llm_route, which is
                            # only known before dispatch and can't reflect a per-call selection.
                            # Same "<backend name>:<model>" shape as llm_route so calibration
                            # matrix keys and config/site_config.yml's fallback route strings
                            # keep matching either way.
                            candidate_provider_model = (
                                f"{getattr(ctx.tag_backend, 'name', 'litellm')}:{resolved_model}"
                                if resolved_model
                                else llm_route
                            )
                            chapter_candidates: list[dict[str, Any]] = []
                            for chapter in chapters:
                                chapter_candidates.extend(
                                    decorate_llm_candidates(
                                        chapter_suggestions.get(chapter["chapter_id"], []),
                                        episode_uid=ep.uid,
                                        episode_title=ep.title,
                                        provider_model=candidate_provider_model,
                                        taxonomy=taxonomy,
                                        recipe_hash=llm_recipe,
                                    )
                                )
                            # Episode-level LLM suggestions are rejected by the contract; keep the
                            # variable for compatibility with deferred/old responses, but the new
                            # rollout persists only chapter candidates alongside fresh rule rows.
                            candidate_tags = rule_candidates + chapter_candidates
                            completed_llm_recipe = llm_recipe
                            final_tags = project_visible_tags(candidate_tags, chapter_annotations)
                            final_hash = projection_hash
                    except Exception as exc:  # noqa: BLE001 — one bad model reply is item-local
                        if not dispatch_settled:
                            ctx.settle_tag_dispatch(False)
                        remember_call_attempt(
                            ep,
                            purpose="topic-tags:tagger",
                            recipe_hash=llm_recipe,
                            status="error",
                            metadata=tag_call_metadata,
                            model=llm_route,
                            reason=str(exc)[:500],
                        )
                        stats.errors.append(f"{ep.uid or ep.guid}: LLM tagging failed: {exc}")
                        candidate_tags = rule_candidates
                        completed_llm_recipe = None
            elif llm_enabled and completed_llm_recipe != llm_recipe and not chapters:
                # A chapter-only LLM rollout has no valid subject when the chapter finder has not
                # produced usable chapters yet. Mark the empty LLM projection resolved for this
                # input so the stage does not dispatch the same empty request on every run; the
                # chapter fingerprint will invalidate it naturally when chapters arrive.
                completed_llm_recipe = llm_recipe
                final_hash = projection_hash

            if prelabeler_enabled and prelabeler_model and candidate_tags:
                projectable_candidates = [
                    candidate
                    for candidate in candidate_tags
                    if candidate.get("source_kind", "llm") == "rule" or candidate.get("chapter_id")
                ]
                pending_prelabels = [
                    candidate
                    for candidate in projectable_candidates
                    if candidate.get("prelabeler_model") != prelabeler_model
                    or candidate.get("prelabeler_prompt_version") != prelabeler_prompt_version
                    or candidate.get("prelabeler_llm_schema_version")
                    != prelabeler_llm_schema_version
                    or candidate.get("prelabeler_decision")
                    not in {
                        "likely_correct",
                        "needs_human_review",
                        "likely_incorrect",
                    }
                ]
                if pending_prelabels:
                    if ctx.stop is not None and ctx.stop():
                        stats.defer("tag-prelabeler-stop", sample=ep.uid or ep.guid)
                    elif ctx.tag_prelabeler_dispatch_exhausted.is_set():
                        stats.defer("tag-prelabeler-no-quota", sample=ep.uid or ep.guid)
                    elif not ctx.reserve_tag_prelabeler_dispatch():
                        stats.defer("tag-prelabeler-no-quota", sample=ep.uid or ep.guid)
                    else:
                        prelabel_recipe = hashlib.sha1(
                            json.dumps(
                                {
                                    "llm_recipe": llm_recipe,
                                    "model": prelabeler_model,
                                    "prompt_version": prelabeler_prompt_version,
                                    "llm_schema_version": prelabeler_llm_schema_version,
                                    "candidates": sorted(
                                        str(item.get("candidate_id")) for item in pending_prelabels
                                    ),
                                },
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode()
                        ).hexdigest()[:16]
                        dispatch_settled = False
                        try:
                            prelabel_call_metadata: dict[str, Any] = {}
                            with PROGRESS.track(
                                source=city.slug,
                                uid=str(ep.uid or ep.guid),
                                phase="tag-prelabeler-dispatch",
                            ):
                                (
                                    prelabels,
                                    prelabel_dispatched,
                                    _prelabel_resolved_model,
                                ) = llm_prelabel_candidates(
                                    ctx.tag_backend,
                                    candidates=pending_prelabels,
                                    taxonomy=taxonomy,
                                    chapters=chapters,
                                    agenda_text=agenda_text,
                                    transcript_text=transcript_text,
                                    recipe_hash=prelabel_recipe,
                                    model=prelabeler_model,
                                    prompt_version=prelabeler_prompt_version,
                                    llm_schema_version=prelabeler_llm_schema_version,
                                    call_metadata_out=prelabel_call_metadata,
                                )
                            queued_prelabel = bool(
                                prelabel_dispatched
                                and _prelabel_resolved_model != "payload-too-large"
                            )
                            ctx.settle_tag_prelabeler_dispatch(queued_prelabel)
                            dispatch_settled = True
                            remember_call_attempt(
                                ep,
                                purpose="topic-tags:prelabeler",
                                recipe_hash=prelabel_recipe,
                                status="deferred" if prelabel_dispatched else "resolved",
                                metadata=prelabel_call_metadata,
                                model=_prelabel_resolved_model or prelabeler_model,
                                reason=(
                                    _prelabel_resolved_model
                                    if prelabel_dispatched and _prelabel_resolved_model
                                    else ""
                                ),
                            )
                            if prelabels:
                                candidate_tags = [
                                    {
                                        **candidate,
                                        **prelabels.get(str(candidate.get("candidate_id")), {}),
                                    }
                                    for candidate in candidate_tags
                                ]
                            if prelabel_dispatched:
                                stats.defer(
                                    "tag-prelabeler-oversized"
                                    if _prelabel_resolved_model == "payload-too-large"
                                    else "tag-prelabeler-dispatch",
                                    sample=ep.uid or ep.guid,
                                )
                        except Exception as exc:  # noqa: BLE001 — evaluator is item-local
                            if not dispatch_settled:
                                ctx.settle_tag_prelabeler_dispatch(False)
                            remember_call_attempt(
                                ep,
                                purpose="topic-tags:prelabeler",
                                recipe_hash=prelabel_recipe,
                                status="error",
                                metadata=prelabel_call_metadata,
                                model=prelabeler_model,
                                reason=str(exc)[:500],
                            )
                            stats.errors.append(f"{ep.uid or ep.guid}: pre-labeler failed: {exc}")

            # Re-project after the evaluator attempt. This is intentionally cheap and makes the
            # overlay a pure function of the persisted ledger + calibration state; a deferred or
            # failed evaluator therefore leaves the previous display decision intact.
            final_tags = project_visible_tags(candidate_tags, chapter_annotations)

            if llm_enabled and completed_llm_recipe == llm_recipe:
                candidate_tags = [
                    apply_admission(tag, config=evaluation_config, state=evaluation_state)
                    for tag in candidate_tags
                ]
                final_hash = projection_hash

            # Keep superseded/legacy LLM rows in the canonical append-oriented ledger. They are
            # intentionally excluded from `projectable_candidates` above and marked hidden here,
            # so recipe/chapter changes never erase audit evidence or make an old episode-level row
            # public again. Deterministic rows and the active current-recipe candidates remain the
            # live projection set.
            ledger_by_id: dict[str, dict[str, Any]] = {}
            for candidate in historical_candidates + candidate_tags:
                key = str(candidate.get("candidate_id") or "")
                if not key:
                    from citypods.llm_evaluation import candidate_id

                    key = candidate_id(candidate)
                    candidate = {**candidate, "candidate_id": key}
                ledger_by_id[key] = candidate
            ledger_candidates = list(ledger_by_id.values())

            # Cache the input fingerprint whenever this run captured the current inputs -- INCLUDING
            # when the LLM dispatch is still pending (`final_hash == rules_hash`, not
            # `projection_hash`). This is the crux of the no-re-fetch redesign: the next run's
            # in-memory triage recognises the inputs as unchanged and, seeing `tags_llm_recipe_hash`
            # still `None`, decides *from memory + remaining quota* whether to re-attempt the LLM --
            # instead of re-fetching agenda/transcript text just to rediscover "still pending",
            # which is what made the entire quota-parked backlog re-fetch every single run.
            # `tags_llm_recipe_hash` (set only on a resolved LLM tag) stays the sole "LLM done"
            # signal, so caching the fingerprint here never makes a pending episode look resolved.
            fingerprint_after = cheap_fingerprint
            if (
                ep.tags != final_tags
                or ep.chapter_tags != chapter_annotations
                or ep.llm_tag_candidates != ledger_candidates
                or ep.tags_llm_recipe_hash != completed_llm_recipe
                or ep.tags_spec_hash != final_hash
                or ep.tags_input_fingerprint != fingerprint_after
            ):
                ep.tags = final_tags
                ep.chapter_tags = chapter_annotations
                ep.llm_tag_candidates = ledger_candidates
                ep.tags_llm_recipe_hash = completed_llm_recipe
                ep.tags_spec_hash = final_hash
                ep.tags_input_fingerprint = fingerprint_after
                stats.ran += 1
            else:
                stats.reused += 1
        return stats


def _requests_fast_yield_exit(stop: Callable[[], bool] | None) -> bool:
    return bool(stop is not None and getattr(stop, "should_exit_immediately", lambda: False)())


def _asr_configured_timeout_seconds(ctx: StageContext, duration_hours: float) -> float | None:
    configured = ctx.asr_timeout_base_seconds + max(0.0, duration_hours) * (
        ctx.asr_timeout_per_hour_seconds
    )
    if configured <= 0:
        return None
    return configured * max(1.0, ctx.asr_timeout_safety_margin)


def _asr_remaining_backstop_seconds(ctx: StageContext) -> float | None:
    if ctx.asr_deadline is None:
        return None
    return ctx.asr_deadline - time.monotonic() - ctx.asr_timeout_budget_reserve_seconds


def _asr_timeout_seconds(ctx: StageContext, duration_hours: float) -> float | None:
    timeout = _asr_configured_timeout_seconds(ctx, duration_hours)
    remaining = _asr_remaining_backstop_seconds(ctx)
    if remaining is None:
        return timeout
    if remaining <= 0:
        return 0.0
    return min(timeout, remaining) if timeout is not None else remaining


def _record_asr_timeout(ep: Episode) -> None:
    ep.transcript_timeout_attempts += 1
    ep.transcript_timeout_last_attempt = datetime.now(UTC).isoformat()


def _reset_asr_timeout_backoff(ep: Episode) -> None:
    ep.transcript_timeout_attempts = 0
    ep.transcript_timeout_last_attempt = None
    ep.transcript_invalid_words_attempts = 0
    ep.transcript_invalid_words_last_attempt = None


def _asr_default_ratio(ctx: StageContext) -> float:
    one_hour = _asr_configured_timeout_seconds(ctx, 1.0)
    return max(0.001, (one_hour if one_hour is not None else 3600.0) / 3600.0)


_ASR_RUNTIME_LOG_LOCK_GUARD = threading.Lock()
_ASR_RUNTIME_LOG_LOCKS: dict[str, threading.Lock] = {}


def _asr_runtime_log_path_lock(path: Path | None) -> threading.Lock:
    if path is None:
        return threading.Lock()
    key = str(path)
    with _ASR_RUNTIME_LOG_LOCK_GUARD:
        return _ASR_RUNTIME_LOG_LOCKS.setdefault(key, threading.Lock())


class AsrRuntimeLog:
    """Rolling ASR runtime/recording-duration ratio log persisted in state."""

    max_samples = 100

    def __init__(self, path: Path | None, *, default_ratio: float):
        self.path = path
        self.default_ratio = max(0.001, float(default_ratio))
        self._lock = threading.Lock()
        self._path_lock = _asr_runtime_log_path_lock(path)
        self._samples: collections.deque[dict[str, float | str]] = collections.deque(
            maxlen=self.max_samples
        )
        self._load()

    @staticmethod
    def _coerce_sample(sample: dict, *, fallback_id: str) -> dict[str, float | str] | None:
        transcribe_seconds = float(sample.get("transcribe_seconds", 0) or 0)
        recording_seconds = float(sample.get("recording_seconds", 0) or 0)
        if transcribe_seconds <= 0 or recording_seconds <= 0:
            return None
        finished_at = float(sample.get("finished_at", sample.get("ts", 0)) or 0)
        sample_id = str(sample.get("id") or sample.get("sample_id") or fallback_id)
        return {
            "id": sample_id,
            "finished_at": finished_at,
            "transcribe_seconds": transcribe_seconds,
            "recording_seconds": recording_seconds,
        }

    @classmethod
    def _normalize_samples(
        cls, raw: list[dict], *, max_samples: int | None = None
    ) -> list[dict[str, float | str]]:
        limit = cls.max_samples if max_samples is None else max_samples
        by_id: dict[str, tuple[int, dict[str, float | str]]] = {}
        for idx, sample in enumerate(raw):
            coerced = cls._coerce_sample(sample, fallback_id=f"legacy-{idx}")
            if coerced is None:
                continue
            by_id[str(coerced["id"])] = (idx, coerced)
        ordered = [
            sample
            for _idx, sample in sorted(
                by_id.values(),
                key=lambda item: (float(item[1].get("finished_at", 0) or 0), item[0]),
            )
        ]
        return ordered[-limit:]

    def _read_samples_from_path_unlocked(self) -> list[dict[str, float | str]]:
        if self.path is not None and self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                return self._normalize_samples(list(data.get("samples", [])))
            except Exception:  # noqa: BLE001 - corrupt telemetry should not stop ASR
                return []
        return []

    def _replace_samples(self, samples: list[dict[str, float | str]]) -> None:
        with self._lock:
            self._samples.clear()
            self._samples.extend(samples[-self.max_samples :])

    def _load(self) -> None:
        with self._path_lock:
            self._replace_samples(self._read_samples_from_path_unlocked())

    def average_ratio(self) -> float:
        with self._lock:
            ratios = [
                s["transcribe_seconds"] / s["recording_seconds"]
                for s in self._samples
                if s.get("recording_seconds", 0) > 0
            ]
        if not ratios:
            return self.default_ratio
        return max(0.001, sum(ratios) / len(ratios))

    def estimate_seconds(self, recording_seconds: float) -> float:
        return max(0.0, recording_seconds) * self.average_ratio()

    def append(self, *, transcribe_seconds: float, recording_seconds: float) -> None:
        if transcribe_seconds <= 0 or recording_seconds <= 0:
            return
        sample = {
            "id": f"{time.time_ns()}-{threading.get_ident()}",
            "finished_at": time.time(),
            "transcribe_seconds": float(transcribe_seconds),
            "recording_seconds": float(recording_seconds),
        }
        if self.path is None:
            with self._lock:
                self._samples.append(sample)
            return
        with self._path_lock:
            samples = self._read_samples_from_path_unlocked()
            samples.append(sample)
            samples = self._normalize_samples(samples)
            self._replace_samples(samples)
            self._persist_unlocked()

    def _persist_unlocked(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "samples": list(self._samples)}
        self.path.write_text(json.dumps(payload, indent=2) + "\n")


class DiarizeRuntimeLog:
    """Rolling, recipe-specific pyannote runtime observations for the R7 pilot."""

    max_samples = 100

    def __init__(self, path: Path | None):
        self.path = path
        self._samples: list[dict[str, float | str]] = []
        if path is not None and path.exists():
            try:
                raw = json.loads(path.read_text())
                rows = raw.get("samples", []) if isinstance(raw, dict) else []
                self._samples = [
                    row
                    for row in rows
                    if isinstance(row, dict)
                    and isinstance(row.get("diarize_seconds"), int | float)
                    and isinstance(row.get("recording_seconds"), int | float)
                    and float(row["diarize_seconds"]) > 0
                    and float(row["recording_seconds"]) > 0
                ][-self.max_samples :]
            except (OSError, ValueError):
                self._samples = []

    def estimate_seconds(self, recording_seconds: float, *, recipe: str) -> float | None:
        ratios = [
            float(row["diarize_seconds"]) / float(row["recording_seconds"])
            for row in self._samples
            if row.get("recipe") == recipe
        ]
        if not ratios:
            return None
        return max(0.0, recording_seconds) * (sum(ratios) / len(ratios))

    def append(self, *, diarize_seconds: float, recording_seconds: float, recipe: str) -> None:
        if diarize_seconds <= 0 or recording_seconds <= 0:
            return
        self._samples.append(
            {
                "id": f"{time.time_ns()}-{threading.get_ident()}",
                "finished_at": time.time(),
                "diarize_seconds": float(diarize_seconds),
                "recording_seconds": float(recording_seconds),
                "recipe": recipe,
            }
        )
        self._samples = self._samples[-self.max_samples :]
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"version": 1, "samples": self._samples}, indent=2) + "\n"
            )


def _diarize_fits_remaining_budget(
    ctx: StageContext, runtime_log: DiarizeRuntimeLog, recording_seconds: float, recipe: str
) -> tuple[bool, float | None, float | None]:
    """Return whether a measured R7 profile says another meeting fits before the cutoff."""
    if ctx.diarize_start_deadline is None:
        return True, runtime_log.estimate_seconds(recording_seconds, recipe=recipe), None
    remaining = ctx.diarize_start_deadline - time.monotonic()
    estimate = runtime_log.estimate_seconds(recording_seconds, recipe=recipe)
    if remaining <= 0:
        return False, estimate, remaining
    if estimate is None:
        # Seed one observation per recipe; afterwards admission is measured rather than guessed.
        return True, None, remaining
    return estimate + ctx.diarize_start_reserve_seconds <= remaining, estimate, remaining


def _asr_remaining_start_seconds(ctx: StageContext) -> float | None:
    if ctx.asr_start_deadline is None:
        return None
    return ctx.asr_start_deadline - time.monotonic()


def _asr_fits_remaining_budget(
    ctx: StageContext, duration_hours: float, runtime_log: AsrRuntimeLog | None = None
) -> tuple[bool, float | None, float | None]:
    """Whether a new ASR item should start before the ASR start cutoff.

    Returns ``(fits, estimate, remaining)``. ``estimate`` is based on the rolling average of
    successful ASR runtime / recording duration samples; ``remaining`` is time until the start
    cutoff. Active inference may continue until the separate backstop.
    """
    remaining = _asr_remaining_start_seconds(ctx)
    if remaining is not None and remaining <= 0:
        return False, 0.0, remaining
    if runtime_log is None:
        runtime_log = AsrRuntimeLog(None, default_ratio=_asr_default_ratio(ctx))
    estimate = runtime_log.estimate_seconds(max(0.0, duration_hours) * 3600.0)
    if remaining is not None and estimate > remaining:
        return False, estimate, remaining
    return True, estimate, remaining


def _acquire_asr_semaphore(ctx: StageContext, sem: threading.Semaphore, ep_ref: str) -> bool:
    while True:
        if ctx.asr_abort_event is not None and ctx.asr_abort_event.is_set():
            print(
                f"[enrich] transcript asr skipped {ep_ref} reason=prior-timeout",
                flush=True,
            )
            return False
        if ctx.stop is not None and ctx.stop():
            return False
        if not sem.acquire(timeout=2):
            continue
        if ctx.asr_abort_event is not None and ctx.asr_abort_event.is_set():
            sem.release()
            print(
                f"[enrich] transcript asr skipped {ep_ref} reason=prior-timeout",
                flush=True,
            )
            return False
        if ctx.stop is not None and ctx.stop():
            sem.release()
            return False
        return True


def _episode_duration_hours(ep: Episode) -> tuple[float, str]:
    return episode_duration_hours(ep)


def _asr_local_duration_eligible(ctx: StageContext, duration_hours: float) -> bool:
    """Whether a known recording duration is safe for the in-process ASR backend.

    This is independent from the rolling runtime estimator: it protects local peak memory, while
    ``_asr_fits_remaining_budget`` protects the Actions wall-clock window. Unknown durations retain
    the existing behavior and are checked again after the hosted-audio probe.
    """
    limit = ctx.asr_local_max_duration_hours
    return limit <= 0 or duration_hours <= 0 or duration_hours <= limit


def _log_asr_external_required(
    ep_ref: str, *, duration_hours: float, duration_source: str, local_max_hours: float
) -> None:
    print(
        f"[enrich] transcript asr skipped {ep_ref} reason=external-required "
        f"duration_h={duration_hours:.2f} duration_source={duration_source} "
        f"local_max_duration_h={local_max_hours:.2f}",
        flush=True,
    )


class AudioStage:
    """Re-host audio, content-addressed by spec. Wraps the existing materialize pipeline."""

    name = "audio"
    version = AUDIO_PIPELINE_VERSION

    def process(
        self, provider, city: City, episodes: list[Episode], ctx: StageContext
    ) -> StageStats:
        if ctx.dry_run or ctx.storage is None:
            return StageStats(self.name)
        for ep in episodes:
            token = ensure_timeline_audio_repair_token(ep, REPAIR_AUDIO_REMATERIALIZE)
            if token:
                ep.audio_rebuild = token
        ms: MaterializeStats = materialize_audio(
            city,
            _timeline_ready(
                _playable(
                    _materialize_set(
                        episodes,
                        city.full_artifact_episodes,
                        feed_visible_per_body=city.max_episodes,
                        policy=ctx.backlog_policy,
                        city_slug=city.slug,
                    )
                )
            ),
            storage=ctx.storage,
            ffmpeg=ctx.ffmpeg,
            max_kbps=ctx.max_kbps,
            loudness_profile=ctx.loudness_profile,
            processing_profile=ctx.audio_processing_profile,
            resolve_media_url=lambda ep: provider.resolve_media_url(ep, city.source),
            stop=ctx.stop,
            source_cache=ctx.source_cache,
            max_workers=ctx.max_encodes_per_source,
            resource_admission=ctx.resource_admission,
            native_work_gate=ctx.native_work_gate,
            memory_reservation=ctx.memory_reservation,
            transport_telemetry=ctx.transport_telemetry,
            hosted_keys_cache=ctx.hosted_keys_cache,
            audio_artifact_cache=ctx.audio_artifact_cache,
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
            rate_limited=ms.rate_limited,
            defer_reasons=dict(ms.defer_reasons),
            defer_samples=list(ms.defer_samples),
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

    def _requires_decoded_source_basis(self) -> bool:
        return any(p.name == "silence" for p in self.planners)

    @staticmethod
    def _has_healthy_source_basis(basis: str | None, *, allow_concat_legacy: bool) -> bool:
        if basis is None:
            return allow_concat_legacy
        if basis == "decoded" or basis.startswith("decoded:"):
            return True
        return allow_concat_legacy and basis == "stream-sample"

    @staticmethod
    def _has_healthy_decoded_source_basis(ep: Episode) -> bool:
        if ep.timeline is None or timeline_digest(ep.timeline, ep.sources) == "":
            return True

        timeline_source_ids = {
            seg.source_id
            for seg in ep.timeline.segments
            if seg.kind == "source" and seg.source_id is not None
        }
        if not timeline_source_ids:
            return True

        sources_by_id = {src.id: src for src in ep.sources}
        if not sources_by_id:
            # Legacy persisted silence timelines may not have saved SourceMedia alongside the
            # EDL. Do not fan those out during the canary; explicit non-decoded source evidence
            # is stale, but absent source evidence waits for the planned version bump.
            return True
        # Multi-source timelines are owned by SwagitConcatPlanner; SilencePlanner skips them.
        # ``stream-sample`` is the concat planner's current basis, and older concat records may
        # have persisted source entries before duration_basis was populated.
        allow_concat_legacy = len(ep.sources) > 1
        return all(
            (src := sources_by_id.get(source_id)) is not None
            and TimelineStage._has_healthy_source_basis(
                src.duration_basis, allow_concat_legacy=allow_concat_legacy
            )
            for source_id in timeline_source_ids
        )

    def process(
        self, provider, city: City, episodes: list[Episode], ctx: StageContext
    ) -> StageStats:
        stats = StageStats(self.name)
        sig = self._signature()
        require_decoded_source_basis = self._requires_decoded_source_basis()
        all_eps = list(
            _materialize_set(
                episodes,
                city.full_artifact_episodes,
                feed_visible_per_body=city.max_episodes,
                policy=ctx.backlog_policy,
                city_slug=city.slug,
            )
        )

        if not self.planners:
            # No planners → identity path for all episodes; nothing to run in parallel.
            stats.reused = len(all_eps)
            return stats

        lock = threading.Lock()

        def _plan_one(ep: Episode) -> None:
            ep.timeline_defer_reason = ""
            # Already planned by this exact planner set+versions → don't recompute. A stale
            # signature (older set) falls through and re-plans.
            force_replan = needs_timeline_audio_repair(ep, REPAIR_TIMELINE_REPLAN)
            stale_source_basis = (
                require_decoded_source_basis and not self._has_healthy_decoded_source_basis(ep)
            )
            if (
                ep.timeline is not None
                and ep.timeline.version == sig
                and not force_replan
                and not stale_source_basis
            ):
                with lock:
                    stats.reused += 1
                return

            now = datetime.now(UTC)

            # Confirmed-dead media (empty/missing/invalid) polls on a flat cadence, and this gate
            # takes precedence over a repair flag. The integrity/repair block is audit-owned — the
            # audio lane preserves it from remote on push (records.protected_blocks_for_lane), so a
            # lane-side clear of the flag would not persist; if a repair flag could bypass this gate
            # it would re-download + re-decode a quarantined episode on *every* run. The flat clock
            # is anchored on the audio-lane-owned media_availability.last_check, so it is persistent
            # and self-managing: when the recheck comes due the episode re-plans, refreshing
            # last_check for another interval, and a genuinely recovered source is picked up then.
            # ``suspected_empty`` is not confirmed-dead, so it keeps the exponential ramp below.
            availability = ep.media_availability
            if (
                availability is not None
                and availability.is_confirmed_dead()
                and not confirmed_dead_recheck_due(ep, now)
            ):
                with lock:
                    stats.defer("dead-cooldown", sample=f"{ep.uid or ep.guid}:dead-cooldown")
                return

            # A confirmed-partial (genuinely short/incomplete) source publishes with a disclaimer
            # (GH#851) rather than being withheld, but there is still nothing to gain from
            # re-downloading + re-decoding it every run once its short length has reproduced —
            # the same flat-cadence reasoning as confirmed-dead media above, reusing the same
            # state-agnostic recheck gate.
            if (
                availability is not None
                and availability.is_confirmed_partial()
                and not confirmed_dead_recheck_due(ep, now)
            ):
                with lock:
                    stats.defer("partial-cooldown", sample=f"{ep.uid or ep.guid}:partial-cooldown")
                return

            # A repair flag is a one-shot "recheck now" for transient failures / broken (non-dead)
            # EDLs: it bypasses the exponential #120 backoff so a flagged episode re-plans
            # immediately. Such flags are cleared by the post-repair audit once the episode is
            # healthy (the audit owns the integrity block); a confirmed-dead episode is governed by
            # the flat gate above, not here.
            if (
                not force_replan
                and ep.materialize_error in _TIMELINE_BACKOFF_ERRORS
                and _in_backoff(ep, now)
            ):
                with lock:
                    reason = f"{ep.materialize_error}-backoff"
                    stats.defer(reason, sample=f"{ep.uid or ep.guid}:{reason}")
                return

            # Planning may be an expensive, restartable ffmpeg pass, so gate it on the shared
            # stop signal exactly like an encode — deferred to a later run, not an error.
            if ctx.stop is not None and ctx.stop():
                with lock:
                    stats.defer("stop-signal")
                return

            current: Timeline | None = ep.timeline
            changed = False
            progress_entry = PROGRESS.start(
                source=city.slug, uid=str(ep.uid or ep.guid), phase="timeline-plan"
            )
            try:
                try:
                    for planner in self.planners:
                        result = planner.plan(provider, city, ep, ctx, current)
                        if ep.timeline_defer_reason:
                            with lock:
                                stats.defer(
                                    ep.timeline_defer_reason,
                                    sample=f"{ep.uid or ep.guid}:{ep.timeline_defer_reason}",
                                )
                            return
                        if result is not None:
                            current = result
                            changed = True
                except RateLimitedMediaFetchError as exc:
                    # A planner's source-cache prefetch hit a provider 403/429. Record the
                    # per-episode backoff here so the failure is visible in the stage stats instead
                    # of vanishing into the global queue's blanket per-item catch.
                    with lock:
                        stats.rate_limited += 1
                        stats.errors.append(f"{ep.uid or ep.guid}: {exc}")
                    record_materialize_failure(ep, getattr(exc, "code", None) or "rate_limited")
                    return
                except Exception as exc:  # noqa: BLE001
                    # CR2-CP-41: any other planner exception used to propagate out of _plan_one;
                    # pool.map re-raises on the first worker exception, aborting timeline-plan
                    # processing for the rest of this source's episode batch. Record and continue,
                    # same as the RateLimitedMediaFetchError case above.
                    with lock:
                        stats.errors.append(f"{ep.uid or ep.guid}: {exc}")
                    record_materialize_failure(ep, "timeline-plan-error")
                    return

                if changed and current is not None:
                    # Stamp the planner-set signature so a future run can detect staleness.
                    ep.timeline = replace(current, version=sig)
                    with lock:
                        stats.ran += 1
                else:
                    # No planner fired → identity path; ep.timeline stays None (== identity).
                    with lock:
                        stats.reused += 1
            finally:
                PROGRESS.finish(progress_entry)

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

    Cut-span chapter starts are snapped by :func:`~citypods.timeline.remap` to the next kept
    served boundary, preserving provider markers that announce the next item after a removed
    silence/recess span. A marker with no later kept audio is still dropped.
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
        for ep in _materialize_set(
            episodes,
            city.full_artifact_episodes,
            feed_visible_per_body=city.max_episodes,
            policy=ctx.backlog_policy,
            city_slug=city.slug,
        ):
            if not _needs_chapter_remap(ep):
                stats.reused += 1
                continue
            source_id = ep.sources[0].id if ep.sources else "s0"
            raw_chapters = _chapters_in_source_time(ep)
            if not raw_chapters:
                stats.reused += 1
                continue
            # A cut chapter end stays None on purpose: the encoder derives it from the next
            # chapter's start, which is correct for a chapter truncated by a removed span
            # (clamping to the served duration would overlap later chapters).
            ep.source_chapters = [dict(ch) for ch in raw_chapters]
            ep.chapters = remap(
                ep.timeline,
                raw_chapters,
                source_id=source_id,
                snap_cut_starts=True,
            )
            # Stamp the EDL version so a later run can tell these served-time chapters were
            # remapped against *this* timeline (staleness — see _needs_chapter_remap).
            ep.chapters_basis = f"served:{ep.timeline.version}"
            stats.ran += 1
        return stats


def _served_chapter_basis_version(ep: Episode) -> str | None:
    if not ep.chapters_basis.startswith("served:"):
        return None
    _served, _sep, version = ep.chapters_basis.partition(":")
    return version or None


def _chapters_in_source_time(ep: Episode) -> list[dict]:
    if ep.source_chapters:
        return [dict(ch) for ch in ep.source_chapters]
    if ep.chapters_basis.startswith("served"):
        return []
    return [dict(ch) for ch in ep.chapters]


def _needs_chapter_remap(ep: Episode) -> bool:
    """True when ep.chapters need to be remapped from source-time to served-time."""
    if ep.chapters_basis.startswith("served"):
        # Served-time chapters are only safely reprojectable when the source-time originals were
        # retained (``ep.source_chapters``). Synthetic served-only chapter sets (e.g. concat)
        # intentionally leave that list empty and remain write-once.
        if not ep.source_chapters:
            return False
        if ep.timeline is None:
            return False
        return _served_chapter_basis_version(ep) != ep.timeline.version
    if not _chapters_in_source_time(ep):
        return False  # nothing to remap
    if ep.timeline is None:
        return False  # identity: source == served, no remap needed
    if timeline_digest(ep.timeline, ep.sources) == "":
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
        for ep in _materialize_set(
            episodes,
            city.full_artifact_episodes,
            feed_visible_per_body=city.max_episodes,
            policy=ctx.backlog_policy,
            city_slug=city.slug,
        ):
            if ep.source_chapters:
                stats.reused += 1
                continue
            if ep.chapters and not ep.chapters_basis.startswith("served"):
                ep.source_chapters = [dict(ch) for ch in ep.chapters]
                stats.reused += 1
                continue
            if ep.chapters and len(ep.sources) > 1:
                # Synthetic served-only chapter sets (currently Swagit concat) have no provider
                # source-time equivalent we can safely remap across multiple sources.
                stats.reused += 1
                continue
            if remaining <= 0:
                stats.defer("per-run-cap")
                continue
            if ctx.stop is not None and ctx.stop():
                stats.defer("wall-clock-budget")
                continue
            remaining -= 1
            try:
                chapters, transcript = fetch(ep, city.source)
            except Exception as exc:  # one bad page must not fail the whole source
                stats.errors.append(f"{ep.uid}: {exc}")
                continue
            # Transcript availability is independent of chapter availability. A provider page
            # with no agenda markers may still expose a transcript endpoint and must not lose it.
            if transcript and "transcript" not in (ep.links or {}):
                ep.links = {**(ep.links or {}), "transcript": transcript}
            if chapters:
                ep.source_chapters = [dict(ch) for ch in chapters]
                ep.chapters = [dict(ch) for ch in chapters]
                ep.chapters_basis = "source:s0"
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
        for ep in _materialize_set(
            episodes,
            city.full_artifact_episodes,
            feed_visible_per_body=city.max_episodes,
            policy=ctx.backlog_policy,
            city_slug=city.slug,
        ):
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


class AgendaTextStage:
    """Fetch agenda/packet documents, retain bounded text, and discover backup/minutes links."""

    name = "agenda_text"
    # Bumped 1 -> 2: content-based chapter attribution (chapter_index/chapter_title) and the
    # bounded second-hop enumeration-page follow are new manifest fields/behavior. Without this
    # bump, stage_is_dirty()'s generic version check would never re-derive an already-completed
    # episode's backup manifest, leaving it permanently on the old, unattributed shape.
    # Bumped 2 -> 3 (GH#1092): re-assess existing agenda artifacts through the quality gate and
    # backfill their quality envelope gradually during normal enrichment; no bulk migration run
    # is required, and only suspicious PDFs invoke OCR.
    version = "3"

    def process(
        self, provider, city: City, episodes: list[Episode], ctx: StageContext
    ) -> StageStats:
        import json as _json
        from datetime import UTC, datetime

        from citypods.agenda_text import (
            AGENDA_TEXT_MAX_CHARS,
            AGENDA_TEXT_QUALITY_VERSION,
            DocumentLink,
            assess_agenda_document,
            attribute_links_by_content,
            chapter_text_matches,
            extract_document,
            minutes_links,
        )
        from citypods.chapters import episode_served_chapters
        from citypods.http import fetch_document_bytes, make_session
        from citypods.records import (
            agenda_backup_backoff_until,
            agenda_text_backoff_until,
            source_key,
        )

        stats = StageStats(self.name)
        if ctx.dry_run or ctx.storage is None:
            return stats
        session = make_session()
        src_key = source_key(city)

        def fetch(url: str) -> tuple[bytes, str]:
            return fetch_document_bytes(session, _validated_document_url(city, url), timeout=30)

        for ep in _materialize_set(
            episodes,
            city.full_artifact_episodes,
            feed_visible_per_body=city.max_episodes,
            policy=ctx.backlog_policy,
            city_slug=city.slug,
        ):
            links_map = ep.links or {}
            agenda_url = (
                links_map.get("agenda_portal")
                or links_map.get("agenda")
                or links_map.get("agenda_packet")
            )
            if not agenda_url:
                stats.reused += 1
                continue
            now = datetime.now(UTC)
            if agenda_text_backoff_until(ep) and agenda_text_backoff_until(ep) > now:
                stats.defer("agenda-text-backoff")
                continue
            quality = ep.agenda_text_quality or {}
            agenda_artifact_key = links_map.get("agenda_text_artifact_key")
            if (
                ep.agenda_text_url == agenda_url
                and ep.agenda_backup_url
                and agenda_artifact_key
                and ep.agenda_text_attempts == 0
                and quality.get("status") == "accepted"
                and quality.get("pipeline_version") == AGENDA_TEXT_QUALITY_VERSION
                and not quality.get("last_assessment")
            ):
                stats.reused += 1
                continue
            if ctx.stop is not None and ctx.stop():
                stats.defer("wall-clock-budget", sample=ep.uid or ep.guid)
                continue
            try:
                import hashlib

                candidate_urls = []
                for candidate in (
                    links_map.get("agenda_portal"),
                    links_map.get("agenda"),
                    links_map.get("agenda_packet"),
                ):
                    if candidate and candidate not in candidate_urls:
                        candidate_urls.append(candidate)
                assessment = None
                content = b""
                discovered: list[DocumentLink] = []
                selected_url = agenda_url
                for candidate_url in candidate_urls:
                    try:
                        candidate_content, candidate_type = fetch(candidate_url)
                        candidate_assessment, candidate_links = assess_agenda_document(
                            candidate_content,
                            content_type=candidate_type,
                            source_url=candidate_url,
                        )
                    except Exception:
                        continue
                    content = candidate_content
                    selected_url = candidate_url
                    assessment, discovered = candidate_assessment, candidate_links
                    if assessment.status == "accepted":
                        break
                    # A portal shell may expose the actual PDF as a same-origin link.  Follow only
                    # validated links and keep the bounded candidate list deterministic.
                    for link in candidate_links:
                        if (
                            link.url not in candidate_urls
                            and _is_valid_document_url(city, link.url)
                            and ("pdf" in link.url.lower() or link.kind == "backup")
                        ):
                            candidate_urls.append(link.url)
                    if len(candidate_urls) >= 8:
                        break
                if assessment is None:
                    raise RuntimeError("agenda-candidate-fetch-failed")
                text = assessment.text[:AGENDA_TEXT_MAX_CHARS]
                links = [
                    link
                    for link in minutes_links(discovered)
                    if _is_valid_document_url(city, link.url)
                ]
                now = datetime.now(UTC)
                prior_quality = ep.agenda_text_quality or {}
                prior_assessment = prior_quality.get("last_assessment")
                if not isinstance(prior_assessment, dict):
                    prior_assessment = prior_quality
                quality = assessment.as_dict()
                document_hash = hashlib.sha256(content).hexdigest()
                same_document = (
                    prior_assessment.get("document_hash") == document_hash
                    and prior_assessment.get("source_url") == selected_url
                )
                same_accepted_document = (
                    prior_quality.get("document_hash") == document_hash
                    and prior_quality.get("source_url") == selected_url
                    and prior_quality.get("status") == "accepted"
                )
                quality["document_hash"] = document_hash
                quality["first_seen"] = (
                    prior_assessment.get("first_seen", now.isoformat())
                    if same_document
                    else now.isoformat()
                )
                quality["last_seen"] = now.isoformat()
                quality["assessment_attempts"] = (
                    int(prior_assessment.get("assessment_attempts", 0) or 0) + 1
                    if same_document
                    else 1
                )
                ep.agenda_text_last_attempt = datetime.now(UTC).isoformat()
                if assessment.status != "accepted":
                    ep.agenda_text_attempts += 1
                    stats.quality(assessment.reason)
                    stats.defer(
                        f"agenda-quality-{assessment.reason}",
                        sample=ep.uid or ep.guid,
                    )
                    had_accepted_artifact = prior_quality.get("status") == "accepted"
                    if had_accepted_artifact and not same_accepted_document:
                        # Keep the last known-good artifact eligible while retaining the new
                        # rejection for audit/diagnostics. A transient portal shell must not make
                        # an already-published agenda disappear from the feed.
                        retained_quality = dict(prior_quality)
                        retained_quality["last_assessment"] = quality
                        ep.agenda_text_quality = retained_quality
                    else:
                        ep.agenda_text_url = selected_url
                        ep.agenda_text_quality = quality
                        ep.links = dict(ep.links or {})
                        ep.links.pop("agenda_text_artifact", None)
                        ep.links.pop("agenda_text_artifact_key", None)
                        ep.links.pop("agenda_backup_artifact", None)
                        ep.links.pop("agenda_backup_artifact_key", None)
                        ep.agenda_backup_url = None
                    continue
                ep.agenda_text_url = selected_url
                ep.agenda_text_quality = quality
                ep.agenda_text_attempts = 0
                stats.quality(
                    "accepted-notice"
                    if assessment.eligibility == "notice"
                    else f"accepted-{assessment.method}"
                )
                agenda_artifact_url = _store_document(
                    ctx, src_key, ep.uid or ep.guid, "agenda", text.encode()
                )
                # DIAGNOSTIC: see the matching `[agenda_storage] recall` line above for how this
                # resolved. This is the exact branch chapter_agenda depends on -- when
                # `agenda_artifact_url` is falsy here, the episode is marked `accepted` (above)
                # but permanently lacks `links["agenda_text_artifact_key"]`, which is invisible
                # to this stage's own reuse fast-path on every later run. Remove once root-caused.
                print(
                    f"[agenda_text] artifact store result uid={ep.uid or ep.guid} "
                    f"source={src_key} content_bytes={len(text.encode())} "
                    f"url={agenda_artifact_url!r} will_attach_link={bool(agenda_artifact_url)}",
                    flush=True,
                )
                if agenda_artifact_url:
                    ep.links = dict(ep.links or {})
                    ep.links["agenda_text_artifact"] = agenda_artifact_url
                    ep.links["agenda_text_artifact_key"] = _document_key(
                        src_key, ep.uid or ep.guid, "agenda", text.encode()
                    )
                # Preserve a provider-supplied canonical link.  Otherwise retain the first
                # agenda-discovered minutes URL as a derived candidate for the previous meeting.
                if links:
                    ep.links = dict(ep.links or {})
                    ep.links["agenda_minutes_candidates"] = [link.url for link in links]
                    target = _previous_same_body(ep, episodes)
                    if target is not None and not (target.links or {}).get("minutes"):
                        target.links = dict(target.links or {})
                        target.links["minutes"] = links[0].url
                        target.links["minutes_source"] = "agenda_link"
                        target.links["minutes_source_episode_uid"] = ep.uid or ep.guid
                # Consolidated link manifest is intentionally bounded and source-attributed.
                # Follow a bounded, one-hop graph for item pages and packet links. The HTTP
                # session applies the SSRF gate to every hop; failures leave the source URL in
                # the manifest rather than erasing useful link-only backup material.
                candidates = list(discovered)
                packet_url = links_map.get("agenda_packet")
                if packet_url and packet_url != agenda_url:
                    try:
                        packet_content, packet_type = fetch(packet_url)
                        _, packet_links = extract_document(
                            packet_content, content_type=packet_type, source_url=packet_url
                        )
                        candidates.extend(packet_links)
                    except Exception:
                        candidates.append(
                            DocumentLink(packet_url, "Agenda Packet", agenda_url, kind="backup")
                        )
                candidates = list(
                    {
                        link.url: link
                        for link in candidates
                        if link is not None and _is_valid_document_url(city, link.url)
                    }.values()
                )[:50]
                # Attribute each candidate to a served chapter/agenda item by content (an item
                # identifier or the chapter title itself found in the link's own label/URL --
                # confirmed on real Legistar and Granicus agendas, platform-agnostic; see
                # citypods/agenda_text.py's attribute_links_by_content). A link that doesn't
                # content-match stays unattributed (chapter_index None) rather than guessed.
                #
                # `with_source_index=True` and the resulting remap below matter because
                # agenda_item_context()/chapter_tag_inputs() (citypods/tags.py) key this
                # manifest's chapter_index by SOURCE chapter position, not served-list position --
                # the same desync chapter_id() already guards against when remap() drops or snaps
                # a chapter
                # (see tests/test_tags.py::test_agenda_text_survives_a_snapped_chapter).
                # Storing a raw served-list position here would silently misattribute a surviving
                # chapter's backup text to whatever chapter now sits at that position after a drop.
                served_chapters = [
                    ch
                    for ch in episode_served_chapters(ep, with_source_index=True)
                    if isinstance(ch, dict) and ch.get("title")
                ]
                attributed_raw = attribute_links_by_content(candidates, served_chapters)
                attributed = [
                    (
                        served_chapters[index].get("source_index", index)
                        if index is not None
                        else None,
                        title,
                        link,
                    )
                    for index, title, link in attributed_raw
                ]
                manifest = [
                    {
                        "url": link.url,
                        "label": link.label,
                        "source_url": link.source_url,
                        "kind": link.kind,
                        "item_label": link.item_label,
                        "chapter_index": chapter_index,
                        "chapter_title": chapter_title,
                    }
                    for chapter_index, chapter_title, link in attributed
                ]
                if agenda_backup_backoff_until(ep) and agenda_backup_backoff_until(ep) > now:
                    stats.reused += 1
                    continue
                # Second-hop fetches are additional, unplanned work discovered while already
                # inside this episode's backup pass -- bound the TOTAL across the whole episode
                # (not just per originally-discovered link, which with up to 50 candidates x 10
                # sub-links each could otherwise reach ~500 sequential fetches for one episode)
                # and gate every fetch -- primary candidates and second-hop alike -- on
                # ctx.stop() so a long episode defers to a later run instead of overrunning the
                # wall-clock budget.
                second_hop_remaining = 20
                for chapter_index, chapter_title, link in attributed:
                    if link.url == agenda_url:
                        continue
                    if ctx.stop is not None and ctx.stop():
                        break
                    manifest_item = next(item for item in manifest if item["url"] == link.url)
                    try:
                        item_content, item_type = fetch(link.url)
                        item_text, item_links = extract_document(
                            item_content, content_type=item_type, source_url=link.url
                        )
                        item_truncated = len(item_text) > 20_000
                        item_text = item_text[:20_000]
                        manifest_item.update(
                            {
                                "text": item_text or None,
                                "truncated": item_truncated,
                                "source": "agenda-link",
                            }
                        )
                        # Second hop: some agenda platforms (e.g. Legistar's MeetingDetail ->
                        # LegislationDetail chain) link to a per-item page that itself only
                        # *enumerates* further backup-document links rather than being a document
                        # itself. Trigger only when this fetched page's own text confirms it
                        # belongs to the same chapter (a content match, not a page-shape guess)
                        # and it discovered further links -- a platform-agnostic signal, not
                        # tuned to any one provider's page structure.
                        if (
                            chapter_title
                            and item_links
                            and second_hop_remaining > 0
                            and chapter_text_matches(item_text, chapter_title)
                        ):
                            for sub_link in item_links[:10]:
                                if second_hop_remaining <= 0:
                                    break
                                if not _is_valid_document_url(city, sub_link.url) or any(
                                    m["url"] == sub_link.url for m in manifest
                                ):
                                    continue
                                if ctx.stop is not None and ctx.stop():
                                    break
                                second_hop_remaining -= 1
                                sub_entry = {
                                    "url": sub_link.url,
                                    "label": sub_link.label,
                                    "source_url": link.url,
                                    "kind": sub_link.kind,
                                    "item_label": sub_link.item_label,
                                    "chapter_index": chapter_index,
                                    "chapter_title": chapter_title,
                                }
                                try:
                                    sub_content, sub_type = fetch(sub_link.url)
                                    sub_text, _ = extract_document(
                                        sub_content, content_type=sub_type, source_url=sub_link.url
                                    )
                                    sub_truncated = len(sub_text) > 20_000
                                    sub_entry.update(
                                        {
                                            "text": sub_text[:20_000] or None,
                                            "truncated": sub_truncated,
                                            "source": "agenda-link-second-hop",
                                        }
                                    )
                                except Exception:
                                    sub_entry.update(
                                        {"text": None, "truncated": False, "source": "link-only"}
                                    )
                                manifest.append(sub_entry)
                    except Exception:
                        manifest_item.update(
                            {"text": None, "truncated": False, "source": "link-only"}
                        )
                backup_payload = _json.dumps(
                    {
                        "version": AGENDA_BACKUP_PIPELINE_VERSION,
                        "links": manifest,
                        "text": text,
                    },
                    sort_keys=True,
                ).encode()
                ep.agenda_backup_url = _store_document(
                    ctx,
                    src_key,
                    ep.uid or ep.guid,
                    "backup",
                    backup_payload,
                    "application/json",
                )
                # DIAGNOSTIC: see the matching `[agenda_storage] recall` line above.
                print(
                    f"[agenda_text] backup store result uid={ep.uid or ep.guid} "
                    f"source={src_key} payload_bytes={len(backup_payload)} "
                    f"url={ep.agenda_backup_url!r} will_attach_link={bool(ep.agenda_backup_url)}",
                    flush=True,
                )
                if ep.agenda_backup_url:
                    ep.links = dict(ep.links or {})
                    ep.links["agenda_backup_artifact"] = ep.agenda_backup_url
                    ep.links["agenda_backup_artifact_key"] = _document_key(
                        src_key, ep.uid or ep.guid, "backup", backup_payload
                    )
                ep.agenda_backup_attempts = 0
                ep.agenda_backup_last_attempt = now.isoformat()
                stats.ran += 1
            except Exception as exc:  # one malformed document must not stop the source
                ep.agenda_text_attempts += 1
                ep.agenda_text_last_attempt = now.isoformat()
                ep.agenda_backup_attempts += 1
                ep.agenda_backup_last_attempt = now.isoformat()
                stats.quality("fetch-failure")
                stats.errors.append(f"{ep.uid or ep.guid}: {exc}")
        return stats


class MinutesTextStage:
    """Fetch effective minutes documents and publish deterministic vote/roster sidecars."""

    name = "minutes_text"
    version = "1"

    def process(
        self, provider, city: City, episodes: list[Episode], ctx: StageContext
    ) -> StageStats:
        import json as _json
        from datetime import UTC, datetime

        from citypods.agenda_text import extract_document, parse_roster, parse_votes
        from citypods.http import fetch_document_bytes, make_session
        from citypods.records import minutes_text_backoff_until, source_key

        stats = StageStats(self.name)
        if ctx.dry_run or ctx.storage is None:
            return stats
        session = make_session()
        src_key = source_key(city)
        for ep in _materialize_set(
            episodes,
            city.full_artifact_episodes,
            feed_visible_per_body=city.max_episodes,
            policy=ctx.backlog_policy,
            city_slug=city.slug,
        ):
            minutes_url = (ep.links or {}).get("minutes")
            if not minutes_url:
                stats.reused += 1
                continue
            if (
                ep.minutes_text_url == minutes_url
                and ep.minutes_votes_url
                and ep.minutes_roster_url
            ):
                stats.reused += 1
                continue
            now = datetime.now(UTC)
            if minutes_text_backoff_until(ep) and minutes_text_backoff_until(ep) > now:
                stats.defer("minutes-text-backoff")
                continue
            if ctx.stop is not None and ctx.stop():
                stats.defer("wall-clock-budget", sample=ep.uid or ep.guid)
                continue
            try:
                content, content_type = fetch_document_bytes(
                    session, _validated_document_url(city, minutes_url), timeout=30
                )
                text, _ = extract_document(
                    content,
                    content_type=content_type,
                    source_url=minutes_url,
                )
                roster = parse_roster(text)
                raw_votes = parse_votes(text, roster=roster)
                grouped: dict[str | None, list[dict]] = {}
                for vote in raw_votes:
                    grouped.setdefault(vote.get("agenda_item"), []).append(
                        {
                            "member": vote.get("member"),
                            "vote": vote.get("value", "unknown"),
                            "evidence": vote.get("evidence"),
                        }
                    )
                votes = [
                    {
                        "agenda_item": item,
                        "chapter_index": None,
                        "votes": members,
                        "outcome": None,
                        "source_url": minutes_url,
                        "method": "deterministic",
                    }
                    for item, members in grouped.items()
                ]
                uid = ep.uid or ep.guid
                votes_payload = _json.dumps(
                    {
                        "version": MINUTES_VOTES_PIPELINE_VERSION,
                        "source_url": minutes_url,
                        "method": "deterministic",
                        "votes": votes,
                    },
                    sort_keys=True,
                ).encode()
                roster_payload = _json.dumps(
                    {
                        "version": MINUTES_ROSTER_PIPELINE_VERSION,
                        "source_url": minutes_url,
                        "method": "deterministic",
                        "members": roster,
                    },
                    sort_keys=True,
                ).encode()
                minutes_payload = _json.dumps(
                    {
                        "version": MINUTES_TEXT_PIPELINE_VERSION,
                        "source_url": minutes_url,
                        "text": text,
                    },
                    sort_keys=True,
                ).encode()
                votes_url = _store_document(
                    ctx,
                    src_key,
                    uid,
                    "minutes-votes",
                    votes_payload,
                    "application/json",
                )
                roster_url = _store_document(
                    ctx,
                    src_key,
                    uid,
                    "minutes-roster",
                    roster_payload,
                    "application/json",
                )
                artifact_url = _store_document(
                    ctx,
                    src_key,
                    uid,
                    "minutes-text",
                    minutes_payload,
                    "application/json",
                )
                if not (votes_url and roster_url and artifact_url):
                    raise RuntimeError("minutes sidecar storage returned no URL")
                updated_links = dict(ep.links or {})
                updated_links["minutes_text_artifact"] = artifact_url
                updated_links["minutes_text_artifact_key"] = _document_key(
                    src_key, uid, "minutes-text", minutes_payload
                )
                ep.minutes_text_url = minutes_url
                ep.minutes_text_attempts = 0
                ep.minutes_text_last_attempt = now.isoformat()
                ep.minutes_votes = votes
                ep.minutes_roster = roster
                ep.minutes_votes_url = votes_url
                ep.minutes_roster_url = roster_url
                ep.links = updated_links
                stats.ran += 1
            except Exception as exc:
                ep.minutes_text_attempts += 1
                ep.minutes_text_last_attempt = now.isoformat()
                stats.errors.append(f"{ep.uid or ep.guid}: {exc}")
        return stats


def _previous_same_body(ep: Episode, episodes: list[Episode]) -> Episode | None:
    if not ep.body:
        return None
    candidates = [
        other
        for other in episodes
        if other is not ep and other.body == ep.body and other.published < ep.published
    ]
    if not candidates:
        return None
    latest = max(item.published for item in candidates)
    latest_day_matches = [item for item in candidates if item.published.date() == latest.date()]
    return latest_day_matches[0] if len(latest_day_matches) == 1 else None


def _validated_document_url(city: City, url: str) -> str:
    """Validate provider-derived document URLs before fetching or persisting them."""
    from citypods.security import allowed_hosts_for_city, validate_source_url

    validate_source_url(
        url,
        allowed_hosts=allowed_hosts_for_city(city.provider, city.city_website),
        resolve=True,
    )
    return url


def _is_valid_document_url(city: City, url: str) -> bool:
    try:
        _validated_document_url(city, url)
    except ValueError:
        return False
    return True


def _store_document(
    ctx: StageContext,
    source: str,
    uid: str,
    kind: str,
    content: bytes,
    content_type: str = "text/plain",
) -> str | None:
    # DIAGNOSTIC (agenda-extraction storage-recall investigation): every caller of this helper
    # (agenda/backup/minutes-text/votes/roster) only reaches it on a freshly-processed ("ran")
    # episode, so call volume is bounded by that stage's own `ran` count -- never the much larger
    # `reused`/`queued` backlog -- and safe to log unconditionally. Remove once the mismatch
    # between AgendaTextStage's "accepted" quality state and a missing
    # ``links["agenda_text_artifact_key"]`` (chapter_agenda's "missing-agenda-artifact" defer,
    # 100% of the pool as of this investigation) is root-caused.
    if ctx.storage is None:
        print(
            f"[agenda_storage] recall source={source} uid={uid} kind={kind} path=no-storage",
            flush=True,
        )
        return None
    import tempfile
    from pathlib import Path

    key = _document_key(source, uid, kind, content)
    if ctx.storage.exists(key):
        url = ctx.storage.public_url(key)
        print(
            f"[agenda_storage] recall source={source} uid={uid} kind={kind} key={key} "
            f"path=exists bytes={len(content)} url={url!r}",
            flush=True,
        )
        return url
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / kind
        path.write_bytes(content)
        try:
            url = ctx.storage.put_file(key, path, content_type)
        except Exception as exc:
            print(
                f"[agenda_storage] recall source={source} uid={uid} kind={kind} key={key} "
                f"path=put_file-error bytes={len(content)} error={exc!r}",
                flush=True,
            )
            raise
        print(
            f"[agenda_storage] recall source={source} uid={uid} kind={kind} key={key} "
            f"path=put_file bytes={len(content)} url={url!r}",
            flush=True,
        )
        return url


def _document_key(source: str, uid: str, kind: str, content: bytes) -> str:
    import hashlib

    digest = hashlib.sha1(content).hexdigest()[:16]
    return f"documents/{source}/{uid}/{kind}-{digest}"


TRANSCRIPT_PIPELINE_VERSION = "1"
AGENDA_TEXT_PIPELINE_VERSION = "3"
# Bumped 2 -> 3 alongside AgendaTextStage.version (GH#1092). Existing records are gradually
# re-assessed during normal enrichment; old artifacts remain available until a replacement is
# accepted, and suspicious PDFs alone invoke OCR.
AGENDA_BACKUP_PIPELINE_VERSION = "2"
MINUTES_TEXT_PIPELINE_VERSION = "1"
MINUTES_VOTES_PIPELINE_VERSION = "1"
MINUTES_ROSTER_PIPELINE_VERSION = "1"
KNOWN_TEXT_ALIGN_PIPELINE_VERSION = PROVIDER_ALIGN_PIPELINE_VERSION
PROVIDER_NATIVE_PIPELINE_VERSION = "1"
PROVIDER_DIARIZE_PIPELINE_VERSION = "1"
DIARIZE_PIPELINE_VERSION = "1"
ASR_PIPELINE_VERSION = "3"  # H12: segment VTT + word-JSON sidecar; version-aware re-transcribe
CHAPTER_AGENDA_PIPELINE_VERSION = "1"
CHAPTER_LOCATOR_PIPELINE_VERSION = "1"

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


def _provider_transcript_spec_hash(content: bytes) -> str:
    """Raw provider document byte identity, deliberately independent of transcript recipes."""
    import hashlib

    return hashlib.sha1(content).hexdigest()[:12]


def _transcript_object_key(src_key: str, uid: str, spec: str, fmt: str) -> str:
    return f"transcripts/{src_key}/{uid}-{spec}.{fmt}"


def _provider_transcript_object_key(src_key: str, uid: str, spec: str, fmt: str) -> str:
    return f"transcripts/{src_key}/{uid}-provider-{spec}.{fmt}"


def _provider_align_spec_hash(ep: Episode, artifact: dict) -> str:
    tl_digest = timeline_digest(ep.timeline, ep.sources) if ep.timeline is not None else ""
    spec = {
        "v": PROVIDER_ALIGN_PIPELINE_VERSION,
        "provider": artifact.get("spec_hash"),
        "text": artifact.get("text_hash"),
        "model": artifact.get("model"),
        "timeline": tl_digest,
        "repair": timeline_audio_repair_token(ep, REPAIR_TRANSCRIPT_REGENERATE),
    }
    blob = json.dumps(spec, separators=(",", ":"), sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def _provider_align_object_key(src_key: str, uid: str, spec: str) -> str:
    return f"transcripts/{src_key}/{uid}-provider-align-{spec}.vtt"


def _provider_align_words_object_key(src_key: str, uid: str, spec: str) -> str:
    return f"transcripts/{src_key}/{uid}-provider-align-{spec}.words.json"


def _provider_native_words_object_key(src_key: str, uid: str, spec: str) -> str:
    return f"transcripts/{src_key}/{uid}-provider-native-{spec}.words.json"


_VTT_WORD_TIMESTAMP_RE = re.compile(r"<(?P<time>(?:(?:\d{2}:)?\d{2}:\d{2})[\.,]\d{3})>")
_SWAGIT_INLINE_TIMESTAMP_RE = re.compile(
    r"^\s*\[\s*(?P<time>\d{1,2}:\d{2}(?::\d{2}(?:[\.,]\d{1,3})?)?)\s*\]\s*$"
)
_SWAGIT_COARSE_MAX_GAP_SECONDS = 15 * 60


def _has_word_timing_vtt(content: bytes) -> bool:
    """Return whether a WebVTT source contains inline word timestamps.

    Cue timestamps alone are not sufficient for the downstream word-boundary features.  WebVTT
    word timing is represented by inline ``<HH:MM:SS.mmm>`` tags inside cue text; SRT never
    qualifies for the provider-native path.
    """
    if not content.lstrip().startswith(b"WEBVTT"):
        return False
    return any(_VTT_WORD_TIMESTAMP_RE.finditer(content.decode("utf-8-sig", errors="replace")))


def _provider_alignment_artifact_is_reusable(artifact: dict) -> bool:
    """Return whether a computed provider alignment has the current WhisperX recipe."""
    return artifact.get("align_pipeline_version") == PROVIDER_ALIGN_PIPELINE_VERSION


def _provider_alignment_source_format(registry: object) -> str | None:
    """Return the source format selected by the provider registry, if one is available."""
    if not isinstance(registry, dict):
        return None
    for artifact in (registry.get("candidate"), registry.get("known_good")):
        if isinstance(artifact, dict) and artifact.get("key"):
            source_format = str(artifact.get("format") or "").lower()
            return source_format or None
    return None


def _provider_vtt_words_json(content: bytes, *, basis: str = "served") -> bytes | None:
    """Convert inline word-timed VTT into the shared word-sidecar shape.

    Provider VTTs vary in whether every word has an explicit marker.  When markers exist, words
    between adjacent markers are distributed over that interval; this is only used for the
    provider-native preserve path, never for the computed provider-alignment path.
    """
    if not _has_word_timing_vtt(content):
        return None
    cues = _parse_timed_transcript(content, "vtt")
    out_segments: list[dict] = []
    for cue in cues:
        raw = str(cue.get("text") or "")
        markers = list(_VTT_WORD_TIMESTAMP_RE.finditer(raw))
        if not markers:
            continue
        words: list[dict] = []
        chunks: list[tuple[float, float, str]] = []
        # Providers commonly timestamp the *second* word onward.  Keep the leading words and
        # distribute them between the cue start and the first explicit marker.
        first_start = _parse_transcript_time(markers[0].group("time"))
        chunks.append((float(cue["start"]), first_start, raw[: markers[0].start()]))
        for index, marker in enumerate(markers):
            start = _parse_transcript_time(marker.group("time"))
            end = (
                _parse_transcript_time(markers[index + 1].group("time"))
                if index + 1 < len(markers)
                else float(cue["end"])
            )
            text = raw[
                marker.end() : markers[index + 1].start() if index + 1 < len(markers) else None
            ]
            chunks.append((start, end, text))
        for start, end, text in chunks:
            tokens = text.replace("\n", " ").split()
            if not tokens:
                continue
            end = max(start, end)
            step = (end - start) / len(tokens)
            for token_index, token in enumerate(tokens):
                word_start = start + step * token_index
                word_end = (
                    end if token_index + 1 == len(tokens) else start + step * (token_index + 1)
                )
                words.append({"w": token, "s": round(word_start, 3), "e": round(word_end, 3)})
        if words:
            plain = _VTT_WORD_TIMESTAMP_RE.sub("", raw).replace("\n", " ").strip()
            out_segments.append(
                {
                    "start": round(float(cue["start"]), 3),
                    "end": round(float(cue["end"]), 3),
                    "text": plain,
                    "words": words,
                }
            )
    if not out_segments:
        return None
    return json.dumps(
        {"schema": "2", "basis": basis, "segments": out_segments},
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _provider_source_text(content: bytes, fmt: str) -> str:
    """Extract provider wording for the full aligner."""
    if fmt in {"vtt", "srt"}:
        joined = "\n".join(
            str(cue.get("text") or "") for cue in _parse_timed_transcript(content, fmt)
        )
        joined = _VTT_WORD_TIMESTAMP_RE.sub("", joined)
        return _preprocess_align_text(joined)
    return _preprocess_align_text(content.decode("utf-8", errors="replace"))


def _provider_source_duration_hint(ep: Episode) -> float | None:
    """Return a source-clock duration suitable for closing a coarse transcript window."""
    duration = episode_source_duration_seconds(ep)
    if duration is not None:
        return duration
    if ep.timeline is not None:
        source_ends = [
            float(segment.source_end)
            for segment in ep.timeline.segments
            if segment.kind == "source" and segment.source_end is not None
        ]
        if source_ends:
            return max(source_ends)
        return None
    return episode_served_duration_seconds(ep)


def _parse_swagit_coarse_cues(ep: Episode, content: bytes) -> list[dict]:
    """Parse Swagit's standalone bracket timestamps into source-time cue windows.

    Swagit TXT transcripts put a timestamp on its own line, usually about every five minutes,
    rather than using VTT/SRT ``start --> end`` cues.  These are useful as broad alignment
    windows, but only when there are at least two monotonic anchors and a known source duration to
    close the final window.  Agenda/section lines are structural annotations, not spoken text, so
    standalone bracketed lines are removed from each block.
    """
    text = content.decode("utf-8-sig", errors="replace").replace("\r\n", "\n")
    anchors: list[tuple[float, list[str]]] = []
    for raw in text.split("\n"):
        line = raw.strip()
        match = _SWAGIT_INLINE_TIMESTAMP_RE.fullmatch(line)
        if match:
            try:
                timestamp = _parse_transcript_time(match.group("time"))
            except (TypeError, ValueError):
                return []
            anchors.append((timestamp, []))
            continue
        if not anchors:
            # This excludes the provider disclaimer and any leading agenda heading before the
            # first timed block. The first marker is the first reliable audio anchor.
            continue
        if line.startswith("[") and line.endswith("]"):
            continue
        anchors[-1][1].append(raw.rstrip())

    if len(anchors) < 2:
        return []
    duration = _provider_source_duration_hint(ep)
    if duration is None or duration <= 0:
        return []

    cues: list[dict] = []
    for index, (start, lines) in enumerate(anchors):
        end = anchors[index + 1][0] if index + 1 < len(anchors) else duration
        if start < 0 or end <= start or end > duration + 1.0:
            return []
        if end - start > _SWAGIT_COARSE_MAX_GAP_SECONDS:
            return []
        block = _preprocess_align_text("\n".join(lines))
        if block:
            cues.append({"start": float(start), "end": float(end), "text": block})

    # A single usable block is no better than full alignment: it provides no meaningful search
    # partition, so deliberately use the existing safe fallback.
    return cues if len(cues) >= 2 else []


def _provider_diarize_spec_hash(ep: Episode, artifact: dict) -> str:
    spec = {
        "v": PROVIDER_DIARIZE_PIPELINE_VERSION,
        "transcript": ep.transcript_spec_hash,
        "provider_align": artifact.get("align_spec_hash"),
        "minutes_roster": sorted(
            (
                {"name": item.get("name"), "status": item.get("status")}
                for item in ep.minutes_roster
                if isinstance(item, dict) and item.get("name")
            ),
            key=lambda item: (str(item["name"]).casefold(), str(item.get("status") or "")),
        ),
    }
    blob = json.dumps(spec, separators=(",", ":"), sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def _provider_diarize_object_key(src_key: str, uid: str, spec: str) -> str:
    return f"transcripts/{src_key}/{uid}-provider-diarize-{spec}.speakers.json"


def _provider_transcript_history(history: object, *, limit: int = 5) -> list[dict]:
    if not isinstance(history, list):
        return []
    return [item for item in history if isinstance(item, dict)][:limit]


# Swagit transcript discovery is a cheap HTTP operation, but the catalog is large and many old
# meetings have no transcript endpoint. Persisted probe state keeps those known misses out of every
# subsequent run while still allowing a provider to add transcripts later.
SWAGIT_TRANSCRIPT_ABSENT_BACKOFF_BASE = timedelta(days=7)
SWAGIT_TRANSCRIPT_ABSENT_BACKOFF_MAX = timedelta(days=90)
SWAGIT_TRANSCRIPT_ERROR_BACKOFF_BASE = timedelta(days=1)
SWAGIT_TRANSCRIPT_ERROR_BACKOFF_MAX = timedelta(days=7)
SWAGIT_TRANSCRIPT_AVAILABLE_RECHECK = timedelta(days=30)


def _provider_transcript_probe(ep: Episode) -> dict:
    """Return the persisted Swagit/provider transcript probe envelope, if valid."""
    registry = ep.provider_transcript or {}
    probe = registry.get("probe") if isinstance(registry, dict) else None
    return dict(probe) if isinstance(probe, dict) else {}


def _provider_transcript_probe_due(ep: Episode, *, url: str | None, now: datetime) -> bool:
    """Return whether a provider transcript URL should be fetched again."""
    probe = _provider_transcript_probe(ep)
    if not probe:
        # Pre-probe records already carry checked_at on their candidate/known-good artifact. Treat
        # that as a successful check so the rollout does not immediately re-download every old
        # provider transcript merely because the probe envelope was introduced later.
        registry = ep.provider_transcript or {}
        for artifact in (
            registry.get("candidate"),
            registry.get("known_good"),
        ):
            if not isinstance(artifact, dict) or (url and artifact.get("url") != url):
                continue
            checked_at = _parse_iso_utc(artifact.get("checked_at"))
            if checked_at is not None:
                return now >= checked_at + SWAGIT_TRANSCRIPT_AVAILABLE_RECHECK
        return True
    if url and probe.get("url") != url:
        return True
    next_retry = _parse_iso_utc(probe.get("next_retry_at"))
    return next_retry is None or now >= next_retry


def _record_provider_transcript_probe(
    ep: Episode,
    *,
    url: str,
    status: str,
    now: datetime,
    status_code: int | None = None,
) -> None:
    """Persist provider transcript probe status and its next retry time."""
    registry = dict(ep.provider_transcript or {})
    prior = registry.get("probe") if isinstance(registry.get("probe"), dict) else {}
    same_url = prior.get("url") == url
    attempts = int(prior.get("attempts") or 0) if same_url else 0
    if status == "available":
        attempts = 0
        delay = SWAGIT_TRANSCRIPT_AVAILABLE_RECHECK
    elif status == "absent":
        attempts += 1
        delay = _capped_exponential_backoff(
            SWAGIT_TRANSCRIPT_ABSENT_BACKOFF_BASE,
            SWAGIT_TRANSCRIPT_ABSENT_BACKOFF_MAX,
            attempts,
        )
    else:
        attempts += 1
        delay = _capped_exponential_backoff(
            SWAGIT_TRANSCRIPT_ERROR_BACKOFF_BASE,
            SWAGIT_TRANSCRIPT_ERROR_BACKOFF_MAX,
            attempts,
        )
    registry["probe"] = {
        "url": url,
        "status": status,
        "status_code": status_code,
        "attempts": attempts,
        "checked_at": now.isoformat(),
        "next_retry_at": (now + delay).isoformat(),
    }
    ep.provider_transcript = registry


def _claim_provider_transcript_probe(ctx: StageContext, source: str) -> bool:
    """Atomically consume one per-source provider probe from this build's shared budget."""
    with ctx.provider_transcript_probe_lock:
        remaining = ctx.provider_transcript_probe_remaining.setdefault(
            source, max(0, ctx.provider_transcript_probes_per_source)
        )
        if remaining <= 0:
            return False
        ctx.provider_transcript_probe_remaining[source] = remaining - 1
        return True


def _provider_transcript_artifact(
    *,
    source_url: str,
    key: str,
    hosted_url: str,
    spec: str,
    fmt: str,
    synced: bool,
    word_timed: bool = False,
    now: str,
    status: str,
) -> dict:
    return {
        "url": source_url,
        "key": key,
        "hosted_url": hosted_url,
        "spec_hash": spec,
        "format": fmt,
        "basis": "source:s0",
        "synced": synced,
        "word_timed": word_timed,
        "confidence": None,
        "checked_at": now,
        "fetched_at": now,
        "status": status,
    }


def _provider_transcript_promote_candidate(ep: Episode, artifact: dict) -> None:
    """Attach a newly-fetched provider document as the current candidate.

    PT-PR2 deliberately does not promote candidates to ``known_good``; PT-PR5's
    alignment/scoring path owns that decision. Keep replaced candidates in bounded history so
    operators can roll back once the scoring stages exist.
    """
    registry = dict(ep.provider_transcript or {})
    current_candidate = registry.get("candidate")
    history = _provider_transcript_history(registry.get("history"))
    if (
        isinstance(current_candidate, dict)
        and current_candidate.get("key")
        and current_candidate.get("spec_hash") != artifact.get("spec_hash")
    ):
        history.insert(0, current_candidate)
    registry["candidate"] = artifact
    registry["history"] = history[:5]
    ep.provider_transcript = registry


def _provider_transcript_note_checked(
    ep: Episode, spec_hash: str, source_url: str, *, now: str
) -> bool:
    """Update checked_at for an unchanged provider transcript registry entry.

    Returns True when an existing current entry covered ``spec_hash``.
    """
    registry = dict(ep.provider_transcript or {})
    for slot in ("candidate", "known_good"):
        artifact = registry.get(slot)
        if isinstance(artifact, dict) and artifact.get("spec_hash") == spec_hash:
            artifact = dict(artifact)
            artifact["url"] = source_url
            artifact["checked_at"] = now
            artifact["status"] = "unchanged"
            registry[slot] = artifact
            if slot == "known_good":
                candidate = registry.get("candidate")
                if (
                    isinstance(candidate, dict)
                    and candidate.get("key")
                    and candidate.get("spec_hash") != spec_hash
                ):
                    history = _provider_transcript_history(registry.get("history"))
                    history.insert(0, candidate)
                    registry["history"] = history[:5]
                    registry.pop("candidate", None)
            ep.provider_transcript = registry
            return True
    return False


def _parse_transcript_time(raw: str) -> float:
    raw = raw.strip().replace(",", ".")
    parts = raw.split(":")
    if len(parts) == 2:
        hours = 0
        minutes, seconds = parts
    elif len(parts) == 3:
        hours, minutes, seconds = parts
    else:
        raise ValueError(f"invalid transcript timestamp {raw!r}")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _format_vtt_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:06.3f}"


def _parse_timed_transcript(content: bytes, fmt: str) -> list[dict]:
    text = content.decode("utf-8-sig", errors="replace").replace("\r\n", "\n")
    cues: list[dict] = []
    pending: dict | None = None
    cue_text: list[str] = []

    def flush() -> None:
        nonlocal pending, cue_text
        if pending is not None and cue_text:
            cues.append({**pending, "text": "\n".join(cue_text).strip()})
        pending = None
        cue_text = []

    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            flush()
            continue
        if line == "WEBVTT" or line.startswith(("NOTE", "STYLE", "REGION")):
            continue
        if fmt == "srt" and line.isdigit() and pending is None:
            continue
        if "-->" in line:
            flush()
            start_raw, end_raw = line.split("-->", 1)
            end_raw = end_raw.strip().split()[0]
            pending = {
                "start": _parse_transcript_time(start_raw.strip().split()[-1]),
                "end": _parse_transcript_time(end_raw),
            }
            continue
        if pending is not None:
            cue_text.append(raw.rstrip())
    flush()
    return cues


def _served_duration_hint(ep: Episode) -> float | None:
    if ep.timeline is not None and ep.timeline.segments:
        return ep.timeline.segments[-1].served_end
    served = episode_served_duration_seconds(ep)
    if served is not None:
        return served
    return episode_source_duration_seconds(ep)


def _edited_timeline_served_duration(ep: Episode) -> float | None:
    if ep.timeline is None or timeline_digest(ep.timeline, ep.sources) == "":
        return None
    return edl_duration(ep.timeline)


def _remap_provider_cues(ep: Episode, cues: list[dict]) -> tuple[list[dict], float | None]:
    if not cues:
        return [], None
    total_duration = sum(max(0.0, c.get("end", 0.0) - c.get("start", 0.0)) for c in cues)
    if ep.timeline is None or timeline_digest(ep.timeline, ep.sources) == "":
        return [dict(c) for c in cues], 1.0
    source_id = ep.sources[0].id if ep.sources else "s0"
    remapped = remap(
        ep.timeline,
        cues,
        source_id=source_id,
        clamp_to=_served_duration_hint(ep),
    )
    remapped = [c for c in remapped if c.get("end") is not None and c["end"] > c["start"]]
    if total_duration > 0:
        kept_duration = sum(max(0.0, c["end"] - c["start"]) for c in remapped)
        confidence = max(0.0, min(1.0, kept_duration / total_duration))
    else:
        confidence = max(0.0, min(1.0, len(remapped) / len(cues)))
    return remapped, confidence


def _provider_alignment_inputs(
    ep: Episode, content: bytes, fmt: str
) -> tuple[str, list[dict] | None]:
    """Return provider alignment text and served-time cue windows when available.

    Provider VTT/SRT timestamps are based on the source recording.  The hosted audio may instead
    be an edited/served timeline, so remap the cue windows before giving them to stable-ts.  Keep
    the cleaned cue text alongside each window so ``align_words()`` receives the exact wording
    represented by those windows. Swagit's TXT format gets the same treatment when its standalone
    ``[HH:MM:SS]`` anchors form valid coarse windows. A malformed/empty cue set deliberately
    returns plain text and ``None`` so the caller uses the slower, unconstrained ``align()`` path.
    """
    if fmt == "txt":
        coarse = _parse_swagit_coarse_cues(ep, content)
        if coarse:
            remapped, _confidence = _remap_provider_cues(ep, coarse)
            timed_segments = [
                {
                    "start": float(cue["start"]),
                    "end": float(cue["end"]),
                    "text": str(cue.get("text") or "").strip(),
                }
                for cue in remapped
                if cue.get("text") and cue.get("start") is not None and cue.get("end") is not None
            ]
            timed_segments = [cue for cue in timed_segments if cue["end"] > cue["start"]]
            if len(timed_segments) >= 2:
                return "\n".join(cue["text"] for cue in timed_segments), timed_segments
        return _provider_source_text(content, fmt), None

    if fmt not in {"vtt", "srt"}:
        return _provider_source_text(content, fmt), None

    prepared: list[dict] = []
    for cue in _parse_timed_transcript(content, fmt):
        text = _VTT_WORD_TIMESTAMP_RE.sub("", str(cue.get("text") or "")).strip()
        text = _preprocess_align_text(text)
        start = cue.get("start")
        end = cue.get("end")
        if not text or start is None or end is None or float(end) <= float(start):
            continue
        prepared.append({"start": float(start), "end": float(end), "text": text})

    if not prepared:
        return _provider_source_text(content, fmt), None

    remapped, _confidence = _remap_provider_cues(ep, prepared)
    timed_segments = [
        {
            "start": float(cue["start"]),
            "end": float(cue["end"]),
            "text": str(cue.get("text") or "").strip(),
        }
        for cue in remapped
        if cue.get("text") and cue.get("start") is not None and cue.get("end") is not None
    ]
    timed_segments = [cue for cue in timed_segments if cue["end"] > cue["start"]]
    if not timed_segments:
        return _provider_source_text(content, fmt), None
    return "\n".join(cue["text"] for cue in timed_segments), timed_segments


def _provider_alignment_sections(ep: Episode, content: bytes, fmt: str) -> list[dict]:
    """Prepare provider wording and served-time windows for WhisperX alignment."""
    source_id = ep.sources[0].id if ep.sources else None
    if fmt in {"vtt", "srt"}:
        text = "\n".join(
            _VTT_WORD_TIMESTAMP_RE.sub("", str(cue.get("text") or ""))
            for cue in _parse_timed_transcript(content, fmt)
        )
    else:
        text = content.decode("utf-8-sig", errors="replace")
    return provider_sections(
        text,
        duration=episode_served_duration_seconds(ep),
        source_duration=episode_source_duration_seconds(ep),
        timeline=ep.timeline,
        source_id=source_id,
    )


def _alignment_input_hash(text: str, timed_segments: list[dict] | None = None) -> str:
    """Hash wording plus constrained windows so timing changes invalidate alignment artifacts."""
    if not timed_segments:
        # Preserve the existing recipe for untimed/plain-text alignment, so introducing the
        # constrained path does not invalidate every historical full-alignment artifact.
        return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    payload: object = {"text": text, "timed_segments": timed_segments}
    blob = json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def _provider_cues_to_vtt(cues: list[dict]) -> bytes:
    lines = ["WEBVTT", ""]
    for cue in cues:
        lines.append(f"{_format_vtt_time(cue['start'])} --> {_format_vtt_time(cue['end'])}")
        lines.extend(str(cue.get("text") or "").splitlines() or [""])
        lines.append("")
    return ("\n".join(lines)).encode("utf-8")


_SPEAKER_PREFIX_RE = re.compile(r"^\s*([A-Z][A-Za-z0-9 .,'&/-]{1,80}?):\s+(.+)$")


def _speaker_turns_from_cues(cues: list[dict]) -> tuple[list[dict], float | None]:
    turns: list[dict] = []
    for cue in cues:
        text = str(cue.get("text") or "").strip()
        if not text:
            continue
        match = _SPEAKER_PREFIX_RE.match(text.replace("\n", " "))
        if not match:
            continue
        speaker = re.sub(r"\s+", " ", match.group(1)).strip()
        spoken = match.group(2).strip()
        if not speaker or not spoken:
            continue
        turns.append(
            {
                "speaker": speaker,
                "start": round(float(cue["start"]), 3),
                "end": round(float(cue["end"]), 3),
                "text": spoken,
            }
        )
    if not cues:
        return [], None
    return turns, max(0.0, min(1.0, len(turns) / len(cues)))


def _confidence_rank(value: object) -> float:
    if value is None:
        return -1.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return -1.0


def _provider_align_candidate_wins(candidate: dict, known_good: dict | None) -> bool:
    if not known_good:
        return True
    return _confidence_rank(candidate.get("confidence")) >= _confidence_rank(
        known_good.get("confidence")
    )


def _provider_transcript_set_known_good(ep: Episode, artifact: dict) -> None:
    registry = dict(ep.provider_transcript or {})
    history = _provider_transcript_history(registry.get("history"))
    current_known = registry.get("known_good")
    if (
        isinstance(current_known, dict)
        and current_known.get("key")
        and current_known.get("spec_hash") != artifact.get("spec_hash")
    ):
        history.insert(0, current_known)
    registry["known_good"] = artifact
    registry.pop("candidate", None)
    registry["history"] = history[:5]
    ep.provider_transcript = registry


def _provider_transcript_reject_candidate(ep: Episode, artifact: dict) -> None:
    registry = dict(ep.provider_transcript or {})
    history = _provider_transcript_history(registry.get("history"))
    rejected = {**artifact, "status": "rejected"}
    history.insert(0, rejected)
    registry["history"] = history[:5]
    registry.pop("candidate", None)
    ep.provider_transcript = registry


def _read_storage_bytes(storage, key: str) -> bytes | None:
    if not key or not storage.exists(key) or not hasattr(storage, "get_file"):
        return None
    import tempfile as _tmp

    with _tmp.TemporaryDirectory() as t:
        local_path = Path(t) / "provider-transcript"
        if not storage.get_file(key, local_path):
            return None
        return local_path.read_bytes()


def _asr_object_key(src_key: str, uid: str, recipe: str) -> str:
    """ASR keys use an ``asr-`` infix to distinguish them from provider content-hash keys."""
    return f"transcripts/{src_key}/{uid}-asr-{recipe}.vtt"


def _asr_words_object_key(src_key: str, uid: str, recipe: str) -> str:
    """Word-level JSON sidecar key (H12), paired with the ASR VTT key for the same recipe."""
    return f"transcripts/{src_key}/{uid}-asr-{recipe}.words.json"


def _asr_recipe_hash(
    city: City,
    ep: Episode,
    align_hash: str | None,
    *,
    align_model: str | None = None,
    interpolate_method: str | None = None,
) -> str:
    """ASR recipe hash keyed on transcript media/timeline inputs, not audio-byte recipes."""
    return asr_mod.asr_spec_hash(
        transcript_media_hash(ep),
        city.asr_model,
        align_hash,
        KNOWN_TEXT_ALIGN_PIPELINE_VERSION if align_hash is not None else ASR_PIPELINE_VERSION,
        language=city.asr_language or None,
        compute_type=city.asr_compute_type,
        beam_size=city.asr_beam_size,
        initial_prompt=asr_initial_prompt(city.podcast_author, ep.body, ep.title),
        align_model=align_model,
        interpolate_method=interpolate_method,
    )


def _copy_storage_object(storage, src_key: str, dest_key: str, content_type: str) -> str:
    """Copy one storage object by download+upload; return copied/already-present/missing."""
    if storage.exists(dest_key):
        return "already-present"
    if not storage.exists(src_key) or not hasattr(storage, "get_file"):
        return "missing"
    import tempfile as _tmp

    with _tmp.TemporaryDirectory() as t:
        local_path = Path(t) / dest_key.rsplit("/", 1)[-1]
        if not storage.get_file(src_key, local_path):
            return "missing"
        storage.put_file(dest_key, local_path, content_type)
    return "copied"


def _adopt_asr_keys(ep: Episode, storage, asr_key: str, words_key: str, recipe: str) -> None:
    ep.transcript_key = asr_key
    ep.transcript_hosted_url = storage.public_url(asr_key)
    ep.transcript_words_key = words_key
    ep.transcript_words_url = storage.public_url(words_key)
    ep.transcript_spec_hash = recipe
    ep.transcript_pipeline_version = ASR_PIPELINE_VERSION
    ep.transcript_format = "vtt"
    ep.transcript_basis = "served"
    ep.transcript_synced = True
    ep.transcript_text_source = "asr"
    ep.transcript_timing_source = "computed"
    ep.transcript_selection = "asr"
    _reset_asr_timeout_backoff(ep)


def _preprocess_align_text(text: str) -> str:
    """Extract spoken dialogue from a meeting-minutes source transcript.

    Provider transcripts (CivicClerk, etc.) are *minutes documents*, not pure
    speech transcripts.  They include agenda headers, speaker attribution labels
    (``COUNCIL MEMBER SMITH:``), coarse timestamps, motion/vote boilerplate, and
    legal text that was never spoken aloud.  Passing that verbatim to a known-text aligner
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

    cleaned = "\n".join(lines_out)
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

    Uses a plain ``requests`` session (not ``make_session``) because hosted M4A files routinely
    exceed the 64 MiB ``MAX_RESPONSE_BYTES`` cap that ``make_session`` enforces on Content-Length.
    ``_download_audio_file`` retains the SSRF guard by validating the initial URL and every manual
    redirect hop before issuing the streaming request.
    """
    import tempfile as _tmp2

    with _tmp2.TemporaryDirectory() as t:
        dest = Path(t) / "audio.m4a"
        _download_audio_file(url, dest)
        yield dest


# Public alias for cross-module callers (bench.py's asr-bench command, CR2-CP-39) — the
# underscore name stays the internal one this module's own callers use.
download_hosted_audio = _download_audio


# Mid-stream connection drops (observed as ChunkedEncodingError wrapping IncompleteRead) during
# the multi-hundred-MB hosted-audio fetch — a transient blip on the CDN/storage side, not a bad
# URL. ``make_session()``'s urllib3 Retry only covers the initial connect/response, not a read
# that fails partway through ``iter_content()``, so retry the whole download here instead.
_AUDIO_DOWNLOAD_MAX_ATTEMPTS = 4
_AUDIO_DOWNLOAD_BACKOFF_SECONDS = 2.0

# Hosted audio is our own transcoded output (mono AAC, capped at 96 kbps — see media.py's
# ``max_kbps=96`` default), not raw provider media, so a legitimate file is small: even an extreme
# ~24h meeting tops out well under 1 GiB at that bitrate. Bound the stream so a malformed/hung
# response can't fill disk across the retry attempts above (each attempt reopens ``dest`` in "wb",
# so a capped attempt never leaves more than one over-cap file on disk).
_MAX_HOSTED_AUDIO_BYTES = 1_073_741_824  # 1 GiB


class HostedAudioTooLargeError(RuntimeError):
    """Hosted-audio stream exceeded the configured cap; this is not retried."""


def _download_audio_file(
    url: str,
    dest: Path,
    *,
    max_attempts: int = _AUDIO_DOWNLOAD_MAX_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    import requests as _req

    from citypods.http import USER_AGENT, clamped_retry_after_seconds

    redirect_statuses = {301, 302, 303, 307, 308}
    last: Exception | None = None
    for attempt in range(max_attempts):
        try:
            with _req.Session() as sess:
                sess.headers["User-Agent"] = USER_AGENT
                current_url = url
                redirects_followed = 0
                retry_download = False
                while True:
                    validate_source_url(current_url, resolve=True)
                    r = sess.get(
                        current_url,
                        timeout=300,
                        stream=True,
                        allow_redirects=False,
                    )
                    if getattr(r, "status_code", None) in redirect_statuses:
                        location = getattr(r, "headers", {}).get("Location")
                        if not location:
                            close = getattr(r, "close", None)
                            if close is not None:
                                close()
                            raise _req.exceptions.HTTPError(
                                "redirect response missing Location header", response=r
                            )
                        if redirects_followed >= MAX_REDIRECTS:
                            close = getattr(r, "close", None)
                            if close is not None:
                                close()
                            raise _req.exceptions.TooManyRedirects(
                                f"hosted audio exceeded {MAX_REDIRECTS} redirects", response=r
                            )
                        next_url = urljoin(current_url, str(location))
                        # The next loop iteration validates this Location-derived URL immediately
                        # before issuing the request, keeping redirect validation adjacent to the
                        # network boundary without resolving the same host twice.
                        close = getattr(r, "close", None)
                        if close is not None:
                            close()
                        current_url = next_url
                        redirects_followed += 1
                        continue
                    try:
                        r.raise_for_status()
                    except _req.exceptions.HTTPError as exc:
                        # Large hosted audio cannot use make_session() because its response-size
                        # cap is intentionally sized for feed/document traffic. Preserve the same
                        # bounded Retry-After policy here for the CDN's transient 429 responses.
                        if getattr(r, "status_code", None) != 429:
                            raise
                        last = exc
                        if attempt + 1 >= max_attempts:
                            retry_download = True
                            break
                        retry_after = clamped_retry_after_seconds(getattr(r, "headers", {}))
                        delay = (
                            retry_after
                            if retry_after is not None
                            else _AUDIO_DOWNLOAD_BACKOFF_SECONDS * (2**attempt)
                        )
                        close = getattr(r, "close", None)
                        if close is not None:
                            close()
                        sleep(delay)
                        retry_download = True
                        break
                    received = 0
                    try:
                        with open(dest, "wb") as f:
                            for chunk in r.iter_content(chunk_size=65536):
                                received += len(chunk)
                                if received > _MAX_HOSTED_AUDIO_BYTES:
                                    raise HostedAudioTooLargeError(
                                        f"hosted audio exceeded {_MAX_HOSTED_AUDIO_BYTES} bytes: "
                                        f"{url}"
                                    )
                                f.write(chunk)
                    finally:
                        close = getattr(r, "close", None)
                        if close is not None:
                            close()
                    break
                if retry_download:
                    continue
            return
        except (
            _req.exceptions.ChunkedEncodingError,
            _req.exceptions.ConnectionError,
            _req.exceptions.Timeout,
        ) as exc:
            last = exc
            if attempt + 1 >= max_attempts:
                break
            sleep(_AUDIO_DOWNLOAD_BACKOFF_SECONDS * (2**attempt))
    assert last is not None
    raise last


def _refresh_served_duration_from_audio(ep: Episode, audio_path: Path, ffmpeg_binary: str) -> str:
    """Set ``audio_duration_served`` to the probed duration of the hosted audio ASR just ran on.

    review/20: the served clock is the *real hosted file*, kept distinct from the EDL/cue clock. We
    therefore probe the actual object for **every** timeline — identity and edited alike — rather
    than trusting the EDL sum for edited episodes (the prior behavior, which masked a render that
    came out shorter than its EDL). The EDL is only a fallback when the probe is unavailable, so the
    field still gets populated. Uses the exact stream-sample clock, not the container's advisory
    ``format.duration`` (issue #849), so this field can't drift from the timeline-audio audit's own
    ``stream_sample_duration`` reading by container-rounding noise."""
    probed = _probe_served_duration_secs(audio_path, ffmpeg_binary)
    if probed is not None and probed > 0:
        prior = episode_served_duration_seconds(ep)
        if prior is None or abs(prior - probed) > 1.0:
            set_served_duration_seconds(ep, probed)
            return "hosted"
        return "served"

    return "served" if episode_served_duration_seconds(ep) else "unknown"


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
           A. WhisperX known-text alignment — when a stored untimed source transcript
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
    VTT/SRT cue windows and Swagit TXT coarse windows are remapped through non-identity
    timelines before stable-ts sees them. Untimed provider text remains ``synced = False``
    until computed alignment produces a served-time artifact. ASR-generated transcripts
    always use ``basis = "served"`` because ASR runs on the hosted (served) audio.
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

        word_sidecar_validity: dict[str, bool] = {}

        def _word_sidecar_present(k: str | None) -> bool:
            """Require a stored sidecar to contain at least one valid timed word."""
            if not k:
                return False
            present = _present(k)
            if not present and hosted_keys is not None:
                # The initial listing intentionally stays cheap, but a migration or inference
                # earlier in this same pass may have just created the destination key.
                present = ctx.storage.exists(k)
            if not present:
                return False
            if k not in word_sidecar_validity:
                word_sidecar_validity[k] = has_valid_timed_words(
                    _read_storage_bytes(ctx.storage, k) or b""
                )
            return word_sidecar_validity[k]

        def _store_asr_artifacts(ep: Episode, artifacts, recipe: str) -> None:
            """Write one inference result to this source's existing source-scoped object keys."""
            if not has_valid_timed_words(artifacts.words):
                raise ValueError("ASR produced no valid timed words")
            uid = ep.uid or ep.guid
            asr_key = _asr_object_key(src_key, uid, recipe)
            words_key = _asr_words_object_key(src_key, uid, recipe)
            with _tmp.TemporaryDirectory() as t:
                vtt_dest = Path(t) / "transcript.vtt"
                vtt_dest.write_bytes(artifacts.vtt)
                url = ctx.storage.put_file(asr_key, vtt_dest, TRANSCRIPT_MIME["vtt"])
                words_dest = Path(t) / "transcript.words.json"
                words_dest.write_bytes(artifacts.words)
                words_url = ctx.storage.put_file(words_key, words_dest, "application/json")

            ep.transcript_key = asr_key
            ep.transcript_hosted_url = url
            ep.transcript_words_key = words_key
            ep.transcript_words_url = words_url
            ep.transcript_spec_hash = recipe
            ep.transcript_pipeline_version = ASR_PIPELINE_VERSION
            ep.transcript_format = "vtt"
            ep.transcript_basis = "served"
            ep.transcript_synced = True
            ep.transcript_text_source = "asr"
            ep.transcript_timing_source = "computed"
            ep.transcript_selection = "asr"
            _reset_asr_timeout_backoff(ep)

        def _store_provider_align_artifacts(
            ep: Episode,
            artifacts,
            provider_artifact: dict,
            text_hash: str,
        ) -> None:
            """Store computed timing while retaining provider wording provenance."""
            if not has_valid_timed_words(artifacts.words):
                raise ValueError("alignment produced no valid timed words")
            uid = ep.uid or ep.guid
            align_artifact = {
                **provider_artifact,
                "text_hash": text_hash,
                "model": city.asr_alignment_model,
            }
            align_spec = _provider_align_spec_hash(ep, align_artifact)
            key = _provider_align_object_key(src_key, uid, align_spec)
            words_key = _provider_align_words_object_key(src_key, uid, align_spec)
            with _tmp.TemporaryDirectory() as t:
                vtt_dest = Path(t) / "provider-align.vtt"
                vtt_dest.write_bytes(artifacts.vtt)
                url = ctx.storage.put_file(key, vtt_dest, TRANSCRIPT_MIME["vtt"])
                words_dest = Path(t) / "provider-align.words.json"
                words_dest.write_bytes(artifacts.words)
                words_url = ctx.storage.put_file(words_key, words_dest, "application/json")

            known = dict(provider_artifact)
            known.update(
                {
                    "aligned_key": key,
                    "aligned_url": url,
                    "aligned_words_key": words_key,
                    "aligned_words_url": words_url,
                    "align_spec_hash": align_spec,
                    "align_pipeline_version": PROVIDER_ALIGN_PIPELINE_VERSION,
                    "alignment_method": "whisperx",
                    "align_coverage": artifacts.coverage,
                    "status": "known_good",
                }
            )
            _provider_transcript_set_known_good(ep, known)

            ep.transcript_key = key
            ep.transcript_hosted_url = url
            ep.transcript_words_key = words_key
            ep.transcript_words_url = words_url
            ep.transcript_spec_hash = align_spec
            ep.transcript_pipeline_version = f"provider-align:{PROVIDER_ALIGN_PIPELINE_VERSION}"
            ep.transcript_format = "vtt"
            ep.transcript_basis = "served"
            ep.transcript_synced = True
            ep.transcript_text_source = "provider"
            ep.transcript_timing_source = "computed"
            ep.transcript_selection = "provider-aligned"
            _reset_asr_timeout_backoff(ep)

        def _mark_provider_align_ineligible(ep: Episode, recipe: str, reason: str) -> None:
            """Route a failed known-text candidate to the ordinary full-ASR queue."""
            registry = dict(ep.provider_transcript or {})
            slot = "candidate" if isinstance(registry.get("candidate"), dict) else "known_good"
            artifact = dict(registry.get(slot) or {})
            artifact.update(
                {
                    "align_ineligible_pipeline_version": (
                        f"provider-align:{PROVIDER_ALIGN_PIPELINE_VERSION}"
                    ),
                    "align_ineligible_reason": reason,
                    "align_spec_hash": recipe,
                }
            )
            registry[slot] = artifact
            ep.provider_transcript = registry

        def _maybe_align_provider_transcript(
            ep: Episode,
            *,
            ep_ref: str,
            allow_active: bool,
        ) -> bool:
            """Preserve a provider VTT only when it already has word-level timing.

            Cue-level VTT/SRT timing is useful source material for the aligner, but it is not a
            sufficient served transcript because search, clips, and diarization consume word
            boundaries.  Non-word-timed provider documents deliberately return ``False`` here and
            enter the common stable-ts alignment path below.
            """
            if not (ep.audio_key and ep.hosted_audio_url):
                return False
            registry = ep.provider_transcript or {}
            if not isinstance(registry, dict):
                return False
            candidate = registry.get("candidate")
            known_good = registry.get("known_good")
            selected = None
            for artifact in (candidate, known_good):
                if (
                    isinstance(artifact, dict)
                    and artifact.get("key")
                    and artifact.get("format") in {"vtt", "srt"}
                ):
                    selected = dict(artifact)
                    break
            if selected is None:
                return False

            content = _read_storage_bytes(ctx.storage, selected["key"])
            if content is None:
                return False
            word_timed = bool(selected.get("word_timed")) or (
                selected.get("format") == "vtt" and _has_word_timing_vtt(content)
            )
            # A word-timed provider VTT is safe to preserve only on the identity timeline.  On an
            # edited timeline its word offsets are source-clock values, so the provider wording
            # goes through stable-ts against the actual served audio instead.
            if not word_timed or (
                ep.timeline is not None and timeline_digest(ep.timeline, ep.sources)
            ):
                return False
            native_words = _provider_vtt_words_json(content, basis="served")
            if native_words is None or not has_valid_timed_words(native_words):
                # The marker detector is intentionally permissive; require a usable sidecar too
                # before claiming that a provider-native transcript satisfies word-boundary users.
                return False

            selected = {
                **selected,
                "word_timed": True,
                "native_spec_hash": selected.get("spec_hash"),
                "aligned_at": datetime.now(UTC).isoformat(),
            }

            if isinstance(candidate, dict) and selected.get("spec_hash") == candidate.get(
                "spec_hash"
            ):
                if _provider_align_candidate_wins(selected, known_good):
                    selected["status"] = "known_good"
                    _provider_transcript_set_known_good(ep, selected)
                else:
                    _provider_transcript_reject_candidate(ep, selected)
                    if not isinstance(known_good, dict):
                        return False
                    return _maybe_align_provider_transcript(
                        ep, ep_ref=ep_ref, allow_active=allow_active
                    )
            else:
                registry = dict(ep.provider_transcript or {})
                selected["status"] = "known_good"
                registry["known_good"] = selected
                ep.provider_transcript = registry

            if not allow_active:
                return False

            key = selected["key"]
            if (
                ep.transcript_key == key
                and ep.transcript_synced
                and _present(key)
                and selected.get("words_key")
                and _word_sidecar_present(selected["words_key"])
            ):
                ep.transcript_hosted_url = ctx.storage.public_url(key)
                ep.transcript_words_key = selected.get("words_key")
                ep.transcript_words_url = (
                    ctx.storage.public_url(ep.transcript_words_key)
                    if ep.transcript_words_key and _word_sidecar_present(ep.transcript_words_key)
                    else None
                )
                stats.reused += 1
                return True

            words_key = selected.get("words_key") or _provider_native_words_object_key(
                src_key, ep.uid or ep.guid, selected["spec_hash"]
            )
            words_url = None
            words = native_words
            if words is not None and not _word_sidecar_present(words_key):
                with _tmp.TemporaryDirectory() as t:
                    words_dest = Path(t) / "provider.words.json"
                    words_dest.write_bytes(words)
                    words_url = ctx.storage.put_file(words_key, words_dest, "application/json")
            elif words is not None:
                words_url = ctx.storage.public_url(words_key)

            provider_registry = dict(ep.provider_transcript or {})
            known = dict(provider_registry.get("known_good") or selected)
            known["word_timed"] = True
            known["native_key"] = key
            known["native_url"] = selected.get("hosted_url") or ctx.storage.public_url(key)
            known["words_key"] = words_key if words is not None else None
            known["words_url"] = words_url or (
                ctx.storage.public_url(words_key) if words is not None else None
            )
            provider_registry["known_good"] = known
            ep.provider_transcript = provider_registry

            ep.transcript_key = key
            ep.transcript_hosted_url = selected.get("hosted_url") or ctx.storage.public_url(key)
            ep.transcript_spec_hash = selected["spec_hash"]
            ep.transcript_format = "vtt"
            ep.transcript_basis = "served"
            ep.transcript_synced = True
            ep.transcript_words_key = words_key if words is not None else None
            ep.transcript_words_url = known["words_url"]
            ep.transcript_pipeline_version = f"provider-native:{PROVIDER_NATIVE_PIPELINE_VERSION}"
            ep.transcript_text_source = "provider"
            ep.transcript_timing_source = "provider"
            ep.transcript_selection = "provider-native"
            _reset_asr_timeout_backoff(ep)
            stats.ran += 1
            print(
                f"[enrich] transcript provider-native done {ep_ref} word_timed=true",
                flush=True,
            )
            return True

        def _maybe_adopt_provider_alignment(
            ep: Episode,
            *,
            allow_active: bool,
        ) -> bool:
            """Switch to an already-computed provider alignment after an H15 route change."""
            if not allow_active:
                return False
            registry = ep.provider_transcript or {}
            if not isinstance(registry, dict):
                return False
            artifact = next(
                (
                    item
                    for item in (registry.get("candidate"), registry.get("known_good"))
                    if isinstance(item, dict) and item.get("aligned_key")
                ),
                None,
            )
            if artifact is None:
                return False
            if not _provider_alignment_artifact_is_reusable(artifact):
                return False
            key = artifact.get("aligned_key")
            words_key = artifact.get("aligned_words_key")
            if not key or not _present(key) or not _word_sidecar_present(words_key):
                return False
            ep.transcript_key = key
            ep.transcript_hosted_url = artifact.get("aligned_url") or ctx.storage.public_url(key)
            ep.transcript_words_key = words_key
            ep.transcript_words_url = artifact.get("aligned_words_url") or ctx.storage.public_url(
                words_key
            )
            ep.transcript_spec_hash = artifact.get("align_spec_hash")
            ep.transcript_pipeline_version = f"provider-align:{PROVIDER_ALIGN_PIPELINE_VERSION}"
            ep.transcript_format = "vtt"
            ep.transcript_basis = "served"
            ep.transcript_synced = True
            ep.transcript_text_source = "provider"
            ep.transcript_timing_source = "computed"
            ep.transcript_selection = "provider-aligned"
            return True

        cpu_threads = max(1, math.ceil((os.cpu_count() or 4) / city.asr_workers))
        runtime_log = AsrRuntimeLog(ctx.asr_runtime_log_path, default_ratio=_asr_default_ratio(ctx))

        # Route inference through the H13 execution backend. Default to an in-process LocalBackend
        # bound to this module's ``asr_mod`` so the path is behavior-preserving (and stays patchable
        # in tests); production injects the configured backend via ``ctx.compute_backend``.
        backend = ctx.compute_backend or LocalBackend(asr_mod)
        # H14a: under ``compute_backend: auto`` the injected backend is a DispatchCoordinator. Off-
        # runner dispatch is attempted in the ASR slot below (a cheap submit, before the on-runner
        # semaphore); the synchronous fallback runs on the coordinator's ``local`` backend, so the
        # on-runner path is identical whether or not a dispatcher is present.
        dispatcher = backend if isinstance(backend, DispatchCoordinator) else None
        if dispatcher is not None:
            backend = dispatcher.local_backend

        # In-process/test backends reuse the pre-loaded model object. Production's process-local
        # backend receives the serializable model name and owns loading/caching inside its child,
        # keeping native inference and its memory fully killable.
        _asr_model = None
        if city.asr_enabled:
            if getattr(backend, "isolates_inference", False):
                _asr_model = city.asr_model
            elif ctx.lane == "align":
                # The align lane must not load the full Whisper transcription model. The
                # per-job WhisperX loader owns its cached CTC model instead.
                _asr_model = city.asr_alignment_model
            else:
                try:
                    _asr_model = asr_mod.load_model(
                        city.asr_model, city.asr_compute_type, cpu_threads
                    )
                except Exception as exc:  # noqa: BLE001
                    stats.errors.append(f"ASR model load failed ({city.asr_model}): {exc}")
                    print(
                        f"[enrich] transcript model-load error slug={city.slug} "
                        f"provider={city.provider}: {exc}",
                        flush=True,
                    )

        materialized_episodes = _materialize_set(
            episodes,
            city.full_artifact_episodes,
            feed_visible_per_body=city.max_episodes,
            policy=ctx.backlog_policy,
            city_slug=city.slug,
            work_class="transcript-asr",
        )
        materialized_uids = {ep.uid or ep.guid for ep in materialized_episodes}

        # Provider discovery is broader than expensive artifact work. A known transcript endpoint
        # is useful evidence for every discovered episode, including deep-archive rows outside the
        # current materialization/ASR cohort. We iterate all records below and bound only inference
        # to the configured materialized set.
        for ep in episodes:
            inference_eligible = (ep.uid or ep.guid) in materialized_uids
            quality_body_name = canonical_body(ep.body or "") or ep.body or "(unknown)"
            quality_body_key_value = quality_body_key(quality_body_name)
            route = ctx.transcript_quality_routes.get((src_key, quality_body_key_value))
            label = ep.uid or ep.guid
            ep_ref = (
                f"slug={city.slug} provider={city.provider} source={src_key} "
                f"uid={label} guid={ep.guid}"
            )
            redo_stale_asr = False
            recheck_asr_key: str | None = None
            recheck_asr_words_key: str | None = None
            active_synced_reused = False
            provider_registry = ep.provider_transcript or {}
            provider_source = next(
                (
                    artifact
                    for artifact in (
                        provider_registry.get("candidate"),
                        provider_registry.get("known_good"),
                    )
                    if isinstance(artifact, dict) and artifact.get("url")
                ),
                None,
            )
            provider_url = (ep.links or {}).get("transcript")
            # A persisted provider artifact is authoritative even for older records whose
            # resource link was not retained. Re-attach its source URL without doing a network
            # probe; the artifact itself is already the durable transcript source.
            if not provider_url and isinstance(provider_source, dict):
                provider_url = provider_source.get("url")
                if provider_url:
                    ep.links = {**(ep.links or {}), "transcript": provider_url}
            provider_content: bytes | None = None
            provider_fetch_due = bool(provider_url)
            stored_provider_transcript = bool(
                ep.transcript_key
                and ep.transcript_synced
                and _present(ep.transcript_key)
                and not ep.transcript_key.rsplit("/", 1)[-1].startswith(f"{ep.uid or ep.guid}-asr-")
            )
            swagit_probe_method = getattr(provider, "probe_transcript", None)
            swagit_probe_url: str | None = None
            swagit_probe_deferred = False
            if city.provider == "swagit" and callable(swagit_probe_method):
                try:
                    swagit_probe_url = provider.transcript_url(ep)
                except Exception as exc:  # noqa: BLE001 -- malformed legacy record; keep ASR alive
                    print(f"[enrich] transcript probe unavailable {ep_ref}: {exc}", flush=True)
                probe_due = bool(
                    swagit_probe_url
                    and _provider_transcript_probe_due(
                        ep, url=swagit_probe_url, now=datetime.now(UTC)
                    )
                )
                should_probe = probe_due and (
                    (not provider_url and not stored_provider_transcript)
                    or provider_url == swagit_probe_url
                )
                if should_probe:
                    if ctx.stop is not None and ctx.stop():
                        stats.defer("stop-signal", sample=ep_ref)
                        swagit_probe_deferred = True
                    elif not _claim_provider_transcript_probe(ctx, src_key):
                        stats.defer("provider-transcript-probe-cap", sample=ep_ref)
                        swagit_probe_deferred = True
                    else:
                        try:
                            probe = swagit_probe_method(ep, city.source)
                            if 200 <= probe.status_code < 300 and probe.content.strip():
                                provider_url = probe.url
                                provider_content = probe.content
                                ep.links = {**(ep.links or {}), "transcript": provider_url}
                            elif probe.status_code in {404, 410} or (
                                200 <= probe.status_code < 300 and not probe.content.strip()
                            ):
                                _record_provider_transcript_probe(
                                    ep,
                                    url=probe.url,
                                    status="absent",
                                    status_code=probe.status_code,
                                    now=datetime.now(UTC),
                                )
                                print(
                                    f"[enrich] transcript probe absent {ep_ref} "
                                    f"status={probe.status_code}",
                                    flush=True,
                                )
                            else:
                                _record_provider_transcript_probe(
                                    ep,
                                    url=probe.url,
                                    status="error",
                                    status_code=probe.status_code,
                                    now=datetime.now(UTC),
                                )
                                print(
                                    f"[enrich] transcript probe unavailable {ep_ref} "
                                    f"status={probe.status_code}",
                                    flush=True,
                                )
                        except Exception as exc:  # noqa: BLE001 -- retryable provider probe
                            _record_provider_transcript_probe(
                                ep,
                                url=swagit_probe_url,
                                status="error",
                                now=datetime.now(UTC),
                            )
                            print(
                                f"[enrich] transcript probe error {ep_ref}: {exc}",
                                flush=True,
                            )
                if provider_url:
                    if swagit_probe_deferred:
                        provider_fetch_due = False
                    elif provider_content is not None:
                        # A successful probe already supplied the bytes; do not spend a second
                        # request (or a second probe-budget unit) fetching the same endpoint.
                        provider_fetch_due = True
                    elif provider_url == swagit_probe_url:
                        provider_fetch_due = _provider_transcript_probe_due(
                            ep, url=provider_url, now=datetime.now(UTC)
                        )
                    else:
                        # Preserve direct fetching for an explicitly advertised Swagit URL that
                        # is not the conventional transcript endpoint.
                        provider_fetch_due = True
            force_provider_selection = route is not None and route.prefers_provider_align
            force_asr_selection = route is not None and route.prefers_fresh_asr
            active_is_provider = ep.transcript_text_source == "provider" or str(
                ep.transcript_pipeline_version or ""
            ).startswith("provider-")
            # 1. Already synced: re-attach URL and done.  Runs unconditionally (even after
            #    stop()) so a yielded run still references every already-synced transcript.
            #    Version-aware (H12): an ASR transcript (key carries the ``-asr-`` infix) from an
            #    older pipeline version is re-done — gradually, because the ASR slot below is
            #    budget/stop-gated.  Provider transcripts (official text, no ``-asr-`` infix) are
            #    NEVER invalidated by an ASR-version bump.
            if ep.transcript_key and ep.transcript_synced and _present(ep.transcript_key):
                # ASR keys are ``{uid}-asr-{recipe}.vtt``; match that prefix on the filename
                # (not a bare ``-asr-`` substring, which a uid ending in ``-asr`` would trip).
                key_name = ep.transcript_key.rsplit("/", 1)[-1]
                is_asr = key_name.startswith(f"{ep.uid or ep.guid}-asr-")
                if is_asr:
                    # H15's accepted-recipe policy is a catalog-wide lever ("reducing
                    # accepted_active_recipes to only the current default is the explicit
                    # full-catalog reprocess lever"), so it must key on the catalog-wide
                    # transcript_pipeline_version, not transcript_spec_hash — the spec hash folds
                    # in transcript_media_hash(ep), which is unique per episode by construction,
                    # so no two episodes' hashes would ever collide and an operator-configured
                    # accepted list keyed on it could never protect more than one episode.
                    accepted_existing_asr = accepted_recipe_allowed(
                        ep.transcript_pipeline_version,
                        accepted_active_recipes=(
                            route.accepted_active_recipes if route is not None else ()
                        ),
                        minimum_quality_rank=(
                            route.minimum_quality_rank if route is not None else None
                        ),
                        recipe_ranks=(route.recipe_ranks if route is not None else None),
                    )
                    words_present = not ep.transcript_words_key or _word_sidecar_present(
                        ep.transcript_words_key
                    )
                    if accepted_existing_asr and words_present and not force_provider_selection:
                        ep.transcript_hosted_url = ctx.storage.public_url(ep.transcript_key)
                        if ep.transcript_words_key and _word_sidecar_present(
                            ep.transcript_words_key
                        ):
                            ep.transcript_words_url = ctx.storage.public_url(
                                ep.transcript_words_key
                            )
                        _reset_asr_timeout_backoff(ep)
                        stats.reused += 1
                        active_synced_reused = True
                        if not provider_url and not force_provider_selection:
                            continue
                    elif ep.transcript_words_key and not words_present:
                        redo_stale_asr = True
                        recheck_asr_key = ep.transcript_key
                        recheck_asr_words_key = ep.transcript_words_key
                    elif ep.transcript_pipeline_version != ASR_PIPELINE_VERSION:
                        redo_stale_asr = True  # own recipe/version changed; re-transcribe
                    elif ep.transcript_spec_hash:
                        redo_stale_asr = True
                        recheck_asr_key = ep.transcript_key
                        recheck_asr_words_key = ep.transcript_words_key
                    else:
                        ep.transcript_hosted_url = ctx.storage.public_url(ep.transcript_key)
                        if ep.transcript_words_key and _word_sidecar_present(
                            ep.transcript_words_key
                        ):
                            ep.transcript_words_url = ctx.storage.public_url(
                                ep.transcript_words_key
                            )
                        _reset_asr_timeout_backoff(ep)
                        stats.reused += 1
                        active_synced_reused = True
                        if not provider_url and not force_provider_selection:
                            continue
                else:
                    provider_align_stale = (
                        str(ep.transcript_pipeline_version or "").startswith("provider-align:")
                        and ep.transcript_pipeline_version
                        != f"provider-align:{PROVIDER_ALIGN_PIPELINE_VERSION}"
                    )
                    if provider_align_stale:
                        # A provider-align pipeline change can alter the alignment inputs (for
                        # example, by discovering coarse Swagit TXT windows). Do not re-adopt the
                        # old computed artifact; let the provider wording flow through the current
                        # alignment recipe below.
                        redo_stale_asr = True
                        active_is_provider = True
                    elif ep.transcript_words_key and not _word_sidecar_present(
                        ep.transcript_words_key
                    ):
                        # A provider VTT without a usable sidecar is not complete for the
                        # downstream word-boundary consumers; route it through alignment or ASR.
                        redo_stale_asr = True
                        active_is_provider = True
                    elif force_asr_selection:
                        redo_stale_asr = True
                        active_is_provider = True
                    else:
                        ep.transcript_hosted_url = ctx.storage.public_url(ep.transcript_key)
                        if ep.transcript_words_key and _word_sidecar_present(
                            ep.transcript_words_key
                        ):
                            ep.transcript_words_url = ctx.storage.public_url(
                                ep.transcript_words_key
                            )
                        _reset_asr_timeout_backoff(ep)
                        stats.reused += 1
                        active_synced_reused = True
                        if not provider_url and not force_provider_selection:
                            continue
            elif ep.transcript_key and ep.transcript_synced:
                key_name = ep.transcript_key.rsplit("/", 1)[-1]
                if key_name.startswith(f"{ep.uid or ep.guid}-asr-"):
                    redo_stale_asr = True
                    if ep.transcript_pipeline_version == ASR_PIPELINE_VERSION:
                        recheck_asr_key = ep.transcript_key
                        recheck_asr_words_key = ep.transcript_words_key

            if active_is_provider and force_asr_selection:
                # H15 routing is source/body policy, not an episode-level review gate. A route
                # change must be able to replace a previously served provider artifact.
                active_synced_reused = False
                redo_stale_asr = True

            # 1b. Untimed stored transcript: re-attach URL so the feed still shows it as a
            #     text note, then fall through to the ASR slot to attempt an upgrade.
            if ep.transcript_key and not ep.transcript_synced and _present(ep.transcript_key):
                ep.transcript_hosted_url = ctx.storage.public_url(ep.transcript_key)
                # fall through to step 3

            # 2. Provider source transcript slot: fetch due links and retain changed bytes as a
            #    candidate. PT-PR2 does not promote to known_good; the later provider-transcript-
            #    align/scoring path owns that decision. Swagit links are rechecked on a persisted
            #    schedule rather than on every run.
            if provider_url and provider_fetch_due:
                if city.provider == "swagit" and provider_content is None:
                    if not _claim_provider_transcript_probe(ctx, src_key):
                        stats.defer("provider-transcript-probe-cap", sample=ep_ref)
                        continue
                if ctx.stop is not None and ctx.stop():
                    if not active_synced_reused:
                        stats.defer("stop-signal")
                    continue

                try:
                    from citypods.http import make_session

                    print(
                        f"[enrich] transcript provider-fetch start {ep_ref}",
                        flush=True,
                    )
                    if provider_content is None:
                        validate_source_url(provider_url, resolve=True)
                        with make_session() as sess:
                            resp = sess.get(provider_url, timeout=30)
                        response_status = resp.status_code
                        content = resp.content
                    else:
                        response_status = 200
                        content = provider_content
                    if response_status >= 400:
                        if city.provider == "swagit":
                            _record_provider_transcript_probe(
                                ep,
                                url=provider_url,
                                status=("absent" if response_status in {404, 410} else "error"),
                                status_code=response_status,
                                now=datetime.now(UTC),
                            )
                        stats.errors.append(f"{ep.uid}: HTTP {response_status} for {provider_url}")
                        continue

                    if not content.strip():
                        if city.provider == "swagit":
                            _record_provider_transcript_probe(
                                ep,
                                url=provider_url,
                                status="absent",
                                status_code=response_status,
                                now=datetime.now(UTC),
                            )
                        stats.errors.append(
                            f"{ep.uid}: empty provider transcript for {provider_url}"
                        )
                        continue

                    fmt = _detect_format(content)
                    timed = is_timed_transcript(content)
                    spec = _provider_transcript_spec_hash(content)
                    key = _provider_transcript_object_key(src_key, ep.uid or ep.guid, spec, fmt)

                    registry = ep.provider_transcript or {}
                    candidate = registry.get("candidate") if isinstance(registry, dict) else None
                    known_good = registry.get("known_good") if isinstance(registry, dict) else None
                    matched_current = next(
                        (
                            artifact
                            for artifact in (candidate, known_good)
                            if isinstance(artifact, dict)
                            and artifact.get("spec_hash") == spec
                            and artifact.get("key")
                            and _present(artifact["key"])
                        ),
                        None,
                    )
                    url = matched_current.get("hosted_url") if matched_current else None
                    if matched_current is not None:
                        if _provider_transcript_note_checked(
                            ep,
                            spec,
                            provider_url,
                            now=datetime.now(UTC).isoformat(),
                        ):
                            stats.reused += 1
                            print(
                                f"[enrich] transcript provider-fetch unchanged {ep_ref} fmt={fmt}",
                                flush=True,
                            )
                            # A pre-existing active transcript, if any, still drives ASR decisions
                            # below. The provider-source registry is intentionally separate.
                            content = b""

                    if content:
                        with _tmp.TemporaryDirectory() as t:
                            dest = Path(t) / f"transcript.{fmt}"
                            dest.write_bytes(content)
                            mime = TRANSCRIPT_MIME.get(fmt, "text/plain")
                            url = ctx.storage.put_file(key, dest, mime)
                        if not url:
                            raise RuntimeError("provider transcript storage URL unavailable")
                        artifact = _provider_transcript_artifact(
                            source_url=provider_url,
                            key=key,
                            hosted_url=url,
                            spec=spec,
                            fmt=fmt,
                            synced=timed,
                            word_timed=fmt == "vtt" and _has_word_timing_vtt(content),
                            now=datetime.now(UTC).isoformat(),
                            status="candidate",
                        )
                        _provider_transcript_promote_candidate(ep, artifact)
                        if city.provider == "swagit":
                            _record_provider_transcript_probe(
                                ep,
                                url=provider_url,
                                status="available",
                                status_code=response_status,
                                now=datetime.now(UTC),
                            )
                        stats.ran += 1
                        print(
                            f"[enrich] transcript provider-fetch done {ep_ref} "
                            f"fmt={fmt} candidate=true",
                            flush=True,
                        )
                        # Do not promote or switch the active <podcast:transcript> here. The
                        # source/body route below owns that selection.
                    else:
                        # ``matched_current`` can turn an unchanged response into an empty local
                        # write after the response was validated. It is still an available
                        # endpoint and should get the long recheck interval.
                        if city.provider == "swagit":
                            _record_provider_transcript_probe(
                                ep,
                                url=provider_url,
                                status="available",
                                status_code=response_status,
                                now=datetime.now(UTC),
                            )
                except Exception as exc:  # noqa: BLE001
                    stats.errors.append(f"{ep.uid}: {exc}")
                    print(
                        f"[enrich] transcript provider-fetch error {ep_ref}: {exc}",
                        flush=True,
                    )
                    continue

            registry = ep.provider_transcript or {}
            active_provider = (
                registry.get("candidate") or registry.get("known_good")
                if isinstance(registry, dict)
                else None
            )
            if (
                active_provider is None
                and ep.transcript_key
                and ep.transcript_format in {"txt", "vtt", "srt"}
                and not ep.transcript_key.rsplit("/", 1)[-1].startswith(f"{ep.uid or ep.guid}-asr-")
            ):
                # Legacy records predate the provider registry. Their non-ASR transcript object is
                # still provider wording and must receive provider provenance when aligned.
                active_provider = {
                    "key": ep.transcript_key,
                    "format": ep.transcript_format,
                    "spec_hash": ep.transcript_spec_hash,
                    "hosted_url": ep.transcript_hosted_url,
                }
            if (
                not ep.transcript_key
                and isinstance(active_provider, dict)
                and active_provider.get("hosted_url")
            ):
                # Keep the provider source visible as an untimed note while the computed
                # word-timed artifact is queued; never expose it as the synced feed transcript.
                ep.transcript_hosted_url = active_provider["hosted_url"]
                ep.transcript_format = active_provider.get("format") or "txt"
                ep.transcript_synced = False

            # Apply the source/body H15 route dynamically. Existing artifacts are enough to switch
            # the served transcript; an episode does not need its own review decision.
            if _maybe_adopt_provider_alignment(
                ep,
                allow_active=force_provider_selection and not active_synced_reused,
            ):
                continue

            # Preserve a provider-native artifact only when its VTT carries word timings. All
            # other provider documents continue through the common WhisperX path below.
            if _maybe_align_provider_transcript(
                ep,
                ep_ref=ep_ref,
                allow_active=not active_synced_reused and not force_asr_selection,
            ):
                continue

            if not inference_eligible:
                # The provider source was probed and any already-computed route selection was
                # applied above; full ASR/align remains bounded by the artifact cohort.
                continue

            # Legacy active provider transcripts remain valid input for alignment. They are not
            # invalidated by PT-PR2; future PT-PR3 exposure logic will decide whether they should
            # keep driving <podcast:transcript>.
            if ep.transcript_key and not ep.transcript_synced and _present(ep.transcript_key):
                ep.transcript_hosted_url = ctx.storage.public_url(ep.transcript_key)

            # A freshly-captured provider source document is not an active synced transcript yet.
            # Continue to the ASR slot unless a prior active synced transcript already exists.
            if active_synced_reused or (ep.transcript_synced and not redo_stale_asr):
                _reset_asr_timeout_backoff(ep)
                continue

            # 3. ASR slot (issue #110): produce a timed VTT from hosted audio.
            #    Guards: model must be loaded (skip gracefully if load failed), need hosted
            #    audio with a stable spec hash (implies ChaptersStage ran).
            if _asr_model is None:
                continue
            if not (ep.audio_key and ep.audio_spec_hash and ep.hosted_audio_url):
                continue
            if ep.transcript_synced and not redo_stale_asr:
                continue  # step 2 may have just set this in the same pass
            # Audio that failed to materialize (or produced no probeable duration) is not safely
            # transcribable: feeding it to ASR wastes a scarce serial slot and the audio bytes may
            # be broken. Skip it here and let the audio lane re-encode it (materialize_audio no
            # longer reuses an errored record). #run-25: ~600 such episodes were monopolizing slots.
            if ep.materialize_error:
                stats.defer("audio-error")
                print(
                    f"[enrich] transcript asr skipped {ep_ref} reason=audio-error "
                    f"error={ep.materialize_error}",
                    flush=True,
                )
                continue
            timeout_backoff_until = transcript_timeout_backoff_until(ep)
            if timeout_backoff_until is not None and datetime.now(UTC) < timeout_backoff_until:
                stats.defer("timeout-backoff")
                print(
                    f"[enrich] transcript asr skipped {ep_ref} reason=timeout-backoff "
                    f"attempts={ep.transcript_timeout_attempts} "
                    f"retry_at={timeout_backoff_until.isoformat()}",
                    flush=True,
                )
                continue
            if ctx.asr_abort_event is not None and ctx.asr_abort_event.is_set():
                stats.defer("prior-timeout")
                print(
                    f"[enrich] transcript asr skipped {ep_ref} reason=prior-timeout",
                    flush=True,
                )
                continue

            # Determine served-time WhisperX sections from the persisted provider wording. A
            # word-timed native VTT remains native on the identity timeline; all other formats
            # receive computed timings against the hosted (served-time) audio.
            align_sections: list[dict] | None = None
            provider_alignment_requested = isinstance(active_provider, dict) and bool(
                active_provider.get("key")
            )
            if provider_alignment_requested:
                provider_content = _read_storage_bytes(ctx.storage, active_provider["key"])
                if provider_content is not None:
                    provider_fmt = str(active_provider.get("format") or "txt")
                    provider_word_timed = provider_fmt == "vtt" and (
                        active_provider.get("word_timed") or _has_word_timing_vtt(provider_content)
                    )
                    if not provider_word_timed or (
                        ep.timeline is not None and timeline_digest(ep.timeline, ep.sources)
                    ):
                        align_sections = _provider_alignment_sections(
                            ep, provider_content, provider_fmt
                        )
            elif ep.transcript_hosted_url and ep.transcript_format in {"txt", "vtt", "srt"}:
                # Legacy records may have the URL but not the provider registry yet.
                try:
                    from citypods.http import make_session

                    with make_session() as sess:
                        r = sess.get(ep.transcript_hosted_url, timeout=30)
                    if r.status_code == 200:
                        align_sections = _provider_alignment_sections(
                            ep, r.content, ep.transcript_format
                        )
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"[enrich] transcript align-sections unavailable {ep_ref}: {exc}",
                        flush=True,
                    )
            if force_asr_selection:
                align_sections = None

            if align_sections:
                if provider_align_ineligible(ep.provider_transcript):
                    stats.defer("alignment-ineligible")
                    continue

            # Lane gating (H6b): the sharded asr.yml runs a single-model lane so WhisperX and
            # faster-whisper never co-load in one runner. ``transcribe`` forces fresh
            # transcription (drop the alignment hint → never load WhisperX); ``align`` only handles
            # source transcripts (others defer to a transcribe lane). The default (None) lane keeps
            # the auto per-episode behavior for a direct ``citypods enrich``. Apply this before the
            # alignment-enabled guard: production's transcribe lane must deliberately ignore source
            # text and generate fresh ASR even while the separate align lane remains disabled.
            if (
                ctx.lane == "transcribe"
                and provider_alignment_requested
                and not provider_align_ineligible(ep.provider_transcript)
            ):
                stats.defer("provider-align-lane")
                continue
            if ctx.lane == "transcribe":
                align_sections = None
            elif ctx.lane == "align" and not align_sections:
                stats.defer("align-lane-no-source-text")
                print(
                    f"[enrich] transcript asr skipped {ep_ref} reason=align-lane-no-source-text",
                    flush=True,
                )
                continue

            # H15 routing payoff (review/12's "unblock"): a route_mode of "provider-align" means
            # this specific source/body has cleared the calibration-gated trust check (see
            # TranscriptQualityRoute / _route_from_row), so it schedules the cheap align lane
            # even while the site-wide asr_alignment_enabled default stays off elsewhere.
            align_lane_unblocked = route is not None and route.prefers_provider_align
            if (
                align_sections
                and not provider_alignment_requested
                and not (city.asr_alignment_enabled or align_lane_unblocked)
            ):
                stats.defer("alignment-disabled")
                print(
                    f"[enrich] transcript asr skipped {ep_ref} reason=alignment-disabled",
                    flush=True,
                )
                continue

            ensure_timeline_audio_repair_token(ep, REPAIR_TRANSCRIPT_REGENERATE)
            align_hash = (
                hashlib.sha1(
                    json.dumps(align_sections, separators=(",", ":"), sort_keys=True).encode()
                ).hexdigest()[:12]
                if align_sections
                else None
            )
            # Alignment falls back to fresh transcription on quality/runtime errors, so keep the
            # fresh prompt/beam inputs in the recipe even when alignment is the primary path.
            initial_prompt = asr_initial_prompt(city.podcast_author, ep.body, ep.title)
            recipe = _asr_recipe_hash(
                city,
                ep,
                align_hash,
                align_model=city.asr_alignment_model if align_sections else None,
                interpolate_method=city.asr_alignment_interpolate if align_sections else None,
            )
            asr_key = _asr_object_key(src_key, ep.uid or ep.guid, recipe)
            words_key = _asr_words_object_key(src_key, ep.uid or ep.guid, recipe)
            cache_key = (ep.uid or ep.guid, recipe)

            migrate_asr_key: str | None = None
            migrate_asr_words_key: str | None = None
            migration_missing = False
            if recheck_asr_key:
                if (
                    ep.transcript_spec_hash == recipe
                    and recheck_asr_key == asr_key
                    and _word_sidecar_present(words_key)
                ):
                    _adopt_asr_keys(ep, ctx.storage, asr_key, words_key, recipe)
                    stats.reused += 1
                    continue
                if (
                    provider_url
                    and not align_sections
                    and recheck_asr_words_key
                    and _word_sidecar_present(recheck_asr_words_key)
                ):
                    ep.transcript_hosted_url = ctx.storage.public_url(recheck_asr_key)
                    ep.transcript_words_url = ctx.storage.public_url(recheck_asr_words_key)
                    _reset_asr_timeout_backoff(ep)
                    stats.reused += 1
                    continue
                migrate_asr_key = recheck_asr_key
                migrate_asr_words_key = recheck_asr_words_key

            if migrate_asr_key:
                vtt_status = _copy_storage_object(
                    ctx.storage, migrate_asr_key, asr_key, TRANSCRIPT_MIME["vtt"]
                )
                words_status = (
                    _copy_storage_object(
                        ctx.storage, migrate_asr_words_key, words_key, "application/json"
                    )
                    if migrate_asr_words_key
                    else "missing"
                )
                if (
                    vtt_status != "missing"
                    and words_status != "missing"
                    and _word_sidecar_present(words_key)
                ):
                    _adopt_asr_keys(ep, ctx.storage, asr_key, words_key, recipe)
                    if "copied" in {vtt_status, words_status}:
                        stats.asr_migration_copied += 1
                        outcome = "copied"
                    else:
                        stats.asr_migration_already_present += 1
                        outcome = "already-present"
                    stats.reused += 1
                    print(
                        f"[enrich] transcript asr migration {outcome} {ep_ref} "
                        f"from={migrate_asr_key} to={asr_key}",
                        flush=True,
                    )
                    continue
                stats.asr_migration_missing += 1
                migration_missing = True
                print(
                    f"[enrich] transcript asr migration missing {ep_ref} "
                    f"vtt={vtt_status} words={words_status}; regenerating",
                    flush=True,
                )

            if (
                not migration_missing
                and _present(asr_key)
                and _word_sidecar_present(words_key)
                and not force_provider_selection
            ):
                ep.transcript_key = asr_key
                ep.transcript_hosted_url = ctx.storage.public_url(asr_key)
                ep.transcript_words_key = words_key
                ep.transcript_words_url = ctx.storage.public_url(words_key)
                ep.transcript_synced = True
                ep.transcript_basis = "served"
                ep.transcript_format = "vtt"
                ep.transcript_spec_hash = recipe
                ep.transcript_pipeline_version = ASR_PIPELINE_VERSION
                ep.transcript_text_source = "asr"
                ep.transcript_timing_source = "computed"
                ep.transcript_selection = "asr"
                _reset_asr_timeout_backoff(ep)
                stats.reused += 1
                continue

            cached_artifacts = ctx.asr_artifact_cache.get(cache_key)
            if cached_artifacts is not None:
                try:
                    if cached_artifacts.aligned:
                        assert isinstance(active_provider, dict)
                        _store_provider_align_artifacts(
                            ep, cached_artifacts.artifacts, active_provider, align_hash or ""
                        )
                    else:
                        _store_asr_artifacts(ep, cached_artifacts.artifacts, recipe)
                except Exception as exc:  # noqa: BLE001
                    stats.errors.append(f"{ep.uid}: ASR dedupe store: {exc}")
                    print(
                        f"[enrich] transcript asr dedupe-store error {ep_ref}: {exc}",
                        flush=True,
                    )
                    continue
                stats.reused += 1
                print(
                    f"[enrich] transcript asr reused {ep_ref} reason=deduplicated-run",
                    flush=True,
                )
                continue

            # 3b. External dispatch (H14a): under ``compute_backend: auto`` with an external GPU
            #     backend configured, hand transcription off-runner instead of running
            #     faster-whisper here. It's a cheap submit — the worker reads the audio from its
            #     public URL and writes the content-addressed artifact back, which the reuse check
            #     above reconciles on a later run — so it skips the on-runner ASR semaphore / native
            #     gate / audio download. ``try_dispatch`` returns ``None`` when it would overflow to
            #     ``local``, and the synchronous on-runner path below then runs unchanged.
            if dispatcher is not None and dispatcher.dispatch_enabled and not align_sections:
                work_class = "transcript-align" if align_sections else "transcript-asr"
                disp_uid = ep.uid or ep.guid
                if dispatcher.is_inflight(src_key, disp_uid, work_class):
                    stats.defer("dispatched-prior-run")
                    print(
                        f"[enrich] transcript asr in-flight {ep_ref} reason=dispatched-prior-run",
                        flush=True,
                    )
                    continue
                handle = dispatcher.try_dispatch(
                    WorkItem(
                        source_key=src_key,
                        episode_uid=disp_uid,
                        work_class=work_class,
                        published=ep.published,
                        city_slug=city.slug,
                        body=ep.body or "",
                    ),
                    InferenceJob(
                        task="align" if align_sections else "transcribe",
                        inputs={
                            "audio_url": ep.hosted_audio_url,
                            "audio_key": ep.audio_key,
                            "language": city.asr_language or None,
                            "model": city.asr_alignment_model if align_sections else city.asr_model,
                            "compute_type": city.asr_compute_type,
                            "beam_size": city.asr_beam_size if not align_sections else None,
                            "initial_prompt": initial_prompt,
                            "sections": align_sections,
                            "interpolate_method": city.asr_alignment_interpolate,
                        },
                        recipe_hash=recipe,
                    ),
                )
                if handle is not None:
                    stats.dispatched += 1
                    print(
                        f"[enrich] transcript asr dispatched {ep_ref} "
                        f"backend={handle.backend} ref={handle.ref}",
                        flush=True,
                    )
                    continue
                # overflow to local — fall through to the on-runner synchronous path below.

            # Preflight with the known episode duration before taking the scarce ASR slot or
            # downloading audio. A later probe of the hosted audio may refine this. Local duration
            # eligibility is a peak-memory guard; the existing runtime estimate remains the
            # independent wall-clock guard.
            preflight_dur_h, preflight_duration_source = _episode_duration_hours(ep)
            if preflight_duration_source != "unknown":
                if not _asr_local_duration_eligible(ctx, preflight_dur_h):
                    stats.defer("external-required")
                    _log_asr_external_required(
                        ep_ref,
                        duration_hours=preflight_dur_h,
                        duration_source=preflight_duration_source,
                        local_max_hours=ctx.asr_local_max_duration_hours,
                    )
                    continue
                fits_budget, estimate_s, remaining_s = _asr_fits_remaining_budget(
                    ctx, preflight_dur_h, runtime_log
                )
                if not fits_budget:
                    stats.defer("insufficient-budget")
                    estimate_label = (
                        f"{estimate_s / 60:.1f}" if estimate_s is not None else "unknown"
                    )
                    remaining_label = (
                        f"{max(0.0, remaining_s) / 60:.1f}"
                        if remaining_s is not None
                        else "unknown"
                    )
                    print(
                        f"[enrich] transcript asr skipped {ep_ref} "
                        "reason=insufficient-budget "
                        f"estimate_m={estimate_label} remaining_m={remaining_label}",
                        flush=True,
                    )
                    continue

            if ctx.stop is not None and ctx.stop():
                stats.defer("stop-signal")
                continue
            if ctx.resource_admission is not None and not ctx.resource_admission.wait(
                kind="asr", label=ep.uid or ep.guid, stop=ctx.stop
            ):
                stats.defer("resource-admission")
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
                if not _acquire_asr_semaphore(ctx, sem, ep_ref):
                    stats.defer("asr-slot-stop")
                    continue
                print(
                    f"[enrich] transcript asr acquired {ep_ref}",
                    flush=True,
                )

            # Reserve this exact inference identity. With multiple ASR permits, aliases can reach
            # this point concurrently; one becomes the leader and followers wait for its result.
            cache_leader, cached_artifacts = ctx.asr_artifact_cache.claim(cache_key)
            if not cache_leader:
                if sem is not None:
                    sem.release()
                    sem = None
                try:
                    if cached_artifacts.aligned:
                        assert isinstance(active_provider, dict)
                        _store_provider_align_artifacts(
                            ep, cached_artifacts.artifacts, active_provider, align_hash or ""
                        )
                    else:
                        _store_asr_artifacts(ep, cached_artifacts.artifacts, recipe)
                except Exception as exc:  # noqa: BLE001
                    stats.errors.append(f"{ep.uid}: ASR dedupe store: {exc}")
                    print(
                        f"[enrich] transcript asr dedupe-store error {ep_ref}: {exc}",
                        flush=True,
                    )
                    continue
                stats.reused += 1
                print(
                    f"[enrich] transcript asr reused {ep_ref} reason=deduplicated-run",
                    flush=True,
                )
                continue
            if ctx.asr_abort_event is not None and ctx.asr_abort_event.is_set():
                ctx.asr_artifact_cache.abort(cache_key)
                if sem is not None:
                    sem.release()
                    sem = None
                stats.defer("prior-timeout")
                continue

            native_gate_acquired = False
            if ctx.native_work_gate is not None:
                native_gate_acquired = ctx.native_work_gate.acquire(
                    kind="asr", label=ep.uid or ep.guid, stop=ctx.stop
                )
                if not native_gate_acquired:
                    ctx.asr_artifact_cache.abort(cache_key)
                    if sem is not None:
                        sem.release()
                        sem = None
                    stats.defer("native-gate-stop")
                    continue

            audio_tmp = _tmp.TemporaryDirectory()
            audio_path = Path(audio_tmp.name) / "audio.m4a"
            try:
                _download_audio_file(ep.hosted_audio_url, audio_path)
            except Exception as exc:  # noqa: BLE001
                ctx.asr_artifact_cache.abort(cache_key)
                audio_tmp.cleanup()
                if native_gate_acquired and ctx.native_work_gate is not None:
                    ctx.native_work_gate.release(kind="asr")
                    native_gate_acquired = False
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
                served_duration = episode_served_duration_seconds(ep)
                print(
                    f"[enrich] transcript audio-probe {ep_ref} duration_s={served_duration:.1f}",
                    flush=True,
                )

            dur_h, duration_source = _episode_duration_hours(ep)
            if probe_source == "hosted":
                duration_source = "hosted"
            duration_label = f"{dur_h:.1f}" if duration_source != "unknown" else "unknown"
            mode = "align" if align_sections else "transcribe"
            if not _asr_local_duration_eligible(ctx, dur_h):
                ctx.asr_artifact_cache.abort(cache_key)
                audio_tmp.cleanup()
                if native_gate_acquired and ctx.native_work_gate is not None:
                    ctx.native_work_gate.release(kind="asr")
                    native_gate_acquired = False
                if sem is not None:
                    sem.release()
                    sem = None
                stats.defer("external-required")
                _log_asr_external_required(
                    ep_ref,
                    duration_hours=dur_h,
                    duration_source=duration_source,
                    local_max_hours=ctx.asr_local_max_duration_hours,
                )
                continue
            fits_budget, estimate_s, remaining_s = _asr_fits_remaining_budget(
                ctx, dur_h, runtime_log
            )
            if not fits_budget:
                ctx.asr_artifact_cache.abort(cache_key)
                audio_tmp.cleanup()
                if native_gate_acquired and ctx.native_work_gate is not None:
                    ctx.native_work_gate.release(kind="asr")
                    native_gate_acquired = False
                if sem is not None:
                    sem.release()
                    sem = None
                stats.defer("insufficient-budget")
                estimate_label = f"{estimate_s / 60:.1f}" if estimate_s is not None else "unknown"
                remaining_label = (
                    f"{max(0.0, remaining_s) / 60:.1f}" if remaining_s is not None else "unknown"
                )
                print(
                    f"[enrich] transcript asr skipped {ep_ref} reason=insufficient-budget "
                    f"estimate_m={estimate_label} remaining_m={remaining_label}",
                    flush=True,
                )
                continue

            timeout_s = _asr_timeout_seconds(ctx, dur_h)
            timeout_label = f"{timeout_s / 60:.1f}" if timeout_s is not None else "disabled"
            print(
                f"[enrich] transcript asr start {ep_ref} mode={mode} duration_h={duration_label} "
                f"duration_source={duration_source} timeout_m={timeout_label}",
                flush=True,
            )

            # Keep the backend call on a helper thread so the orchestrator can poll the item
            # deadline. Production's process-local backend runs native inference in a persistent
            # child that ``terminate_active`` can kill/restart; injected in-process test/legacy
            # backends retain the conservative abandoned-thread fallback. Results arriving after
            # a timeout are discarded and never reported as completed work.
            _artifacts: list = []
            _err: list[Exception] = []
            _aligned: list[bool] = []
            _needs_fresh_asr: list[bool] = []
            _alignment_ineligible_reason: list[str] = []
            _release_abandoned_asr_slot = threading.Event()
            _release_abandoned_native_gate = threading.Event()

            # Bind per-iteration values as default args so the closure captures their
            # current values, not a reference that may change in future loop iterations
            # (ruff B023).  The thread calls _infer() with no positional args.
            def _infer(
                _at=align_sections,
                _ep_ref=ep_ref,
                _audio=audio_path,
                _audio_tmp=audio_tmp,
                _result=_artifacts,
                _errors=_err,
                _was_aligned=_aligned,
                _needs_fresh=_needs_fresh_asr,
                _ineligible_reason=_alignment_ineligible_reason,
                _sem=sem,
                _release_abandoned=_release_abandoned_asr_slot,
                _native_gate=ctx.native_work_gate,
                _release_native=_release_abandoned_native_gate,
                _backend=backend,
                _recipe=recipe,
                _initial_prompt=initial_prompt,
            ) -> None:
                try:

                    def _transcribe_fresh():
                        return _backend.run_inference(
                            InferenceJob(
                                task="transcribe",
                                inputs={
                                    "audio_path": _audio,
                                    "model": _asr_model,
                                    "language": city.asr_language or None,
                                    "compute_type": city.asr_compute_type,
                                    "beam_size": city.asr_beam_size,
                                    "initial_prompt": _initial_prompt,
                                    "cpu_threads": cpu_threads,
                                },
                                recipe_hash=_recipe,
                            )
                        ).output

                    if _at:
                        try:
                            _result.append(
                                _backend.run_inference(
                                    InferenceJob(
                                        task="align",
                                        inputs={
                                            "audio_path": _audio,
                                            "sections": _at,
                                            "model": city.asr_alignment_model,
                                            "language": city.asr_language or None,
                                            "compute_type": city.asr_compute_type,
                                            "cpu_threads": cpu_threads,
                                            "interpolate_method": city.asr_alignment_interpolate,
                                        },
                                        recipe_hash=_recipe,
                                    )
                                ).output
                            )
                            _was_aligned.append(True)
                        except Exception as _align_exc:  # noqa: BLE001
                            _quality_error = getattr(asr_mod, "AlignmentQualityError", None)
                            if _quality_error is not None and isinstance(
                                _align_exc, _quality_error
                            ):
                                _ineligible_reason.append("raw-coverage-below-90-percent")
                                _needs_fresh.append(True)
                                print(
                                    f"[enrich] transcript alignment-ineligible {_ep_ref}; "
                                    "queued for full ASR: "
                                    f"{_align_exc}",
                                    flush=True,
                                )
                                return
                            reason = "alignment-error"
                            if ctx.lane == "align":
                                _ineligible_reason.append(reason)
                                _needs_fresh.append(True)
                                print(
                                    f"[enrich] transcript {reason} {_ep_ref}; "
                                    f"queued for full ASR: {_align_exc}",
                                    flush=True,
                                )
                                return
                            print(
                                f"[enrich] transcript {reason} {_ep_ref}, "
                                f"retrying as transcribe: {_align_exc}",
                                flush=True,
                            )
                            _result.append(_transcribe_fresh())
                            _was_aligned.append(False)
                            _ineligible_reason.append(reason)
                    else:
                        _result.append(_transcribe_fresh())
                        _was_aligned.append(False)
                except Exception as _exc:  # noqa: BLE001
                    _errors.append(_exc)
                finally:
                    if _release_abandoned.is_set() and _sem is not None:
                        _sem.release()
                    if _release_native.is_set() and _native_gate is not None:
                        _native_gate.release(kind="asr")
                    _audio_tmp.cleanup()

            # CR2-CP-40: ep.uid can be falsy for a not-yet-assigned-uid episode; use the same
            # ep.uid or ep.guid fallback already established elsewhere in this function instead
            # of slicing ep.uid directly (None[:8] raises TypeError).
            _t = threading.Thread(target=_infer, daemon=True, name=f"asr-{label[:8]}")
            _asr_started_at = time.monotonic()
            _t.start()

            _abandoned = False
            _timeout_at = time.monotonic() + timeout_s if timeout_s is not None else None
            # Register the in-flight inference so the heartbeat's progress snapshot shows a busy
            # ASR shard as busy. Native inference runs in a killable child process, so without this
            # the per-thread PROGRESS registry is empty for the whole (possibly multi-hour)
            # transcription and a healthy run is indistinguishable from a hung one — the exact
            # failure progress.py exists to prevent (run #25). Cleared on every exit via finally.
            _asr_progress = PROGRESS.start(source=city.slug, uid=str(label), phase=f"asr-{mode}")
            try:
                while _t.is_alive():
                    # Do not abandon active ASR merely because a newer scheduled ASR run is queued
                    # or the start cutoff has passed. Once native transcription starts, let it
                    # finish unless the separate backstop timeout below fires.
                    if _timeout_at is not None and time.monotonic() >= _timeout_at:
                        _abandoned = True
                        ctx.asr_artifact_cache.abort(cache_key)
                        message = f"{label}: ASR timeout after {timeout_s / 60:.1f}m"
                        stats.errors.append(message)
                        stats.defer("timeout")
                        _record_asr_timeout(ep)
                        terminate = getattr(backend, "terminate_active", None)
                        worker_terminated = bool(terminate()) if callable(terminate) else False
                        if worker_terminated:
                            _t.join(timeout=10)
                        if worker_terminated and not _t.is_alive():
                            print(
                                f"[enrich] transcript asr timeout {ep_ref} seconds={timeout_s:.0f} "
                                "worker=terminated result=discarded",
                                flush=True,
                            )
                            if sem is not None:
                                sem.release()
                                sem = None
                            if native_gate_acquired and ctx.native_work_gate is not None:
                                ctx.native_work_gate.release(kind="asr")
                                native_gate_acquired = False
                        else:
                            if ctx.asr_abort_event is not None:
                                ctx.asr_abort_event.set()
                            print(
                                f"[enrich] transcript asr timeout {ep_ref} seconds={timeout_s:.0f} "
                                "(inference continues in background, result discarded; "
                                "remaining ASR skipped this run)",
                                flush=True,
                            )
                            if sem is not None:
                                _release_abandoned_asr_slot.set()
                                sem = None
                            if native_gate_acquired and ctx.native_work_gate is not None:
                                _release_abandoned_native_gate.set()
                                native_gate_acquired = False
                            if ctx.asr_abandoned_event is not None:
                                ctx.asr_abandoned_event.set()
                        break
                    time.sleep(2)
            finally:
                PROGRESS.finish(_asr_progress)

            if _abandoned:
                # Only the combined-enrich (lane=None) path sets fast_yield_exit; the transcribe/
                # align lanes pass None, so this is a no-op there. When it is set, a backstop
                # timeout that coincides with a code/human supersession fast-exits so the queued
                # build can deploy without waiting on native ASR teardown.
                if ctx.fast_yield_exit is not None and _requests_fast_yield_exit(ctx.stop):
                    ctx.fast_yield_exit()
                continue

            if _needs_fresh_asr:
                _mark_provider_align_ineligible(
                    ep,
                    recipe,
                    _alignment_ineligible_reason[0],
                )
                ctx.asr_artifact_cache.abort(cache_key)
                if sem is not None:
                    sem.release()
                    sem = None
                if native_gate_acquired and ctx.native_work_gate is not None:
                    ctx.native_work_gate.release(kind="asr")
                    native_gate_acquired = False
                audio_tmp.cleanup()
                stats.defer(
                    "alignment-low-quality"
                    if _alignment_ineligible_reason[0] == "raw-coverage-below-90-percent"
                    else "alignment-error"
                )
                continue

            if _err:
                ctx.asr_artifact_cache.abort(cache_key)
                if sem is not None:
                    sem.release()
                    sem = None
                if native_gate_acquired and ctx.native_work_gate is not None:
                    ctx.native_work_gate.release(kind="asr")
                    native_gate_acquired = False
                audio_tmp.cleanup()
                stats.errors.append(f"{ep.uid}: ASR: {_err[0]}")
                print(
                    f"[enrich] transcript asr error {ep_ref}: {_err[0]}",
                    flush=True,
                )
                continue

            if not _artifacts:
                ctx.asr_artifact_cache.abort(cache_key)
                if sem is not None:
                    sem.release()
                    sem = None
                if native_gate_acquired and ctx.native_work_gate is not None:
                    ctx.native_work_gate.release(kind="asr")
                    native_gate_acquired = False
                audio_tmp.cleanup()
                stats.errors.append(f"{ep.uid}: ASR: inference produced no result")
                print(
                    f"[enrich] transcript asr error {ep_ref}: inference produced no result",
                    flush=True,
                )
                continue

            artifacts = _artifacts[0]
            if _alignment_ineligible_reason:
                _mark_provider_align_ineligible(ep, recipe, _alignment_ineligible_reason[0])
            # Publish before releasing the ASR slot so every follower observes completed bytes.
            ctx.asr_artifact_cache.complete(cache_key, artifacts, aligned=_aligned[0])
            if sem is not None:
                sem.release()
                sem = None
            if native_gate_acquired and ctx.native_work_gate is not None:
                ctx.native_work_gate.release(kind="asr")
                native_gate_acquired = False
            audio_tmp.cleanup()

            try:
                if _aligned[0] and align_sections:
                    assert isinstance(active_provider, dict)
                    _store_provider_align_artifacts(
                        ep,
                        artifacts,
                        active_provider,
                        align_hash or "",
                    )
                else:
                    _store_asr_artifacts(ep, artifacts, recipe)
                asr_elapsed = time.monotonic() - _asr_started_at
                runtime_log.append(
                    transcribe_seconds=asr_elapsed,
                    recording_seconds=max(0.0, dur_h) * 3600.0,
                )
                if _aligned[0]:
                    stats.aligned += 1
                    outcome = "aligned"
                else:
                    stats.transcribed += 1
                    outcome = "transcribed"
                stats.ran += 1
                if recheck_asr_key:
                    stats.asr_migration_regenerated += 1
                if ctx.transcript_quality_state_dir is not None:
                    try:
                        record_l1_sample(
                            ctx.transcript_quality_state_dir,
                            source_key=src_key,
                            body_key=quality_body_key_value,
                            body_name=quality_body_name,
                            episode_uid=label,
                            method="align" if _aligned[0] else "transcribe",
                            coverage=artifacts.coverage,
                            word_logprob_mean=artifacts.word_logprob_mean,
                            word_logprob_p10=artifacts.word_logprob_p10,
                            model_id=city.asr_model,
                            recipe_hash=recipe,
                            max_events=ctx.transcript_quality_raw_log_cap,
                        )
                    except Exception as exc:  # noqa: BLE001 - L1 telemetry is best-effort
                        print(
                            f"[enrich] transcript l1-telemetry error {ep_ref}: {exc}",
                            flush=True,
                        )
                print(
                    f"[enrich] transcript asr done {ep_ref} method={outcome} "
                    f"asr_seconds={asr_elapsed:.1f} "
                    f"recording_seconds={max(0.0, dur_h) * 3600.0:.1f} "
                    f"ratio={runtime_log.average_ratio():.3f}",
                    flush=True,
                )
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
                if native_gate_acquired and ctx.native_work_gate is not None:
                    ctx.native_work_gate.release(kind="asr")
                    native_gate_acquired = False

        return stats


class ProviderTranscriptDiarizeStage:
    """Derive speaker turns from selected provider-aligned transcripts.

    PT-PR6 keeps this deliberately conservative: provider transcripts that already encode
    speaker labels (`SPEAKER: words`) produce a content-addressed `speakers.json`; transcripts
    without usable speaker labels record a speakers error but keep serving the successful
    transcript text.
    """

    name = "diarize"
    version = PROVIDER_DIARIZE_PIPELINE_VERSION

    def process(
        self, provider, city: City, episodes: list[Episode], ctx: StageContext
    ) -> StageStats:
        from citypods.records import source_key as _src_key

        stats = StageStats(self.name)
        if ctx.dry_run or ctx.storage is None:
            return stats

        src_key = _src_key(city)
        for ep in _materialize_set(
            episodes,
            city.full_artifact_episodes,
            feed_visible_per_body=city.max_episodes,
            policy=ctx.backlog_policy,
            city_slug=city.slug,
            work_class="provider-transcript-diarize",
        ):
            label = ep.uid or ep.guid
            registry = ep.provider_transcript or {}
            known_good = registry.get("known_good") if isinstance(registry, dict) else None
            if not (
                isinstance(known_good, dict)
                and ep.transcript_key
                and ep.transcript_synced
                and "-provider-align-" in ep.transcript_key
            ):
                continue
            spec = _provider_diarize_spec_hash(ep, known_good)
            key = _provider_diarize_object_key(src_key, label, spec)
            if (
                ep.speakers_key == key
                and ep.speakers_synced
                and ctx.storage.exists(ep.speakers_key)
            ):
                ep.speakers_url = ctx.storage.public_url(key)
                stats.reused += 1
                continue
            if ctx.stop is not None and ctx.stop():
                stats.defer("stop-signal")
                continue

            content = _read_storage_bytes(ctx.storage, ep.transcript_key)
            if content is None:
                ep.speakers_error = "missing-provider-aligned-transcript"
                stats.errors.append(f"{label}: missing provider-aligned transcript")
                continue
            try:
                cues = _parse_timed_transcript(content, ep.transcript_format or "vtt")
                turns, confidence = _speaker_turns_from_cues(cues)
            except Exception as exc:  # noqa: BLE001
                ep.speakers_error = f"parse-error: {exc}"
                stats.errors.append(f"{label}: speaker parse: {exc}")
                continue
            if not turns:
                ep.speakers_key = None
                ep.speakers_url = None
                ep.speakers_spec_hash = spec
                ep.speakers_format = "json"
                ep.speakers_synced = False
                ep.speakers_confidence = confidence
                ep.speakers_pipeline_version = PROVIDER_DIARIZE_PIPELINE_VERSION
                ep.speakers_error = "no-speaker-labels"
                known_good = {**known_good, "diarize_status": "no-speaker-labels"}
                registry = dict(ep.provider_transcript or {})
                registry["known_good"] = known_good
                ep.provider_transcript = registry
                stats.defer("no-speaker-labels")
                continue

            payload = {
                "schema": "1",
                "basis": "served",
                "source": "provider-transcript",
                "confidence": confidence,
                "turns": turns,
                # Minutes rosters are candidate vocabulary for future diarization/identity
                # assignment. They never rewrite provider speaker labels by themselves.
                "candidate_members": [
                    member.get("name")
                    for member in ep.minutes_roster
                    if isinstance(member, dict) and member.get("name")
                ],
            }
            with tempfile.TemporaryDirectory() as t:
                dest = Path(t) / "speakers.json"
                dest.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True))
                url = ctx.storage.put_file(key, dest, "application/json")

            ep.speakers_key = key
            ep.speakers_url = url
            ep.speakers_spec_hash = spec
            ep.speakers_format = "json"
            ep.speakers_synced = True
            ep.speakers_confidence = confidence
            ep.speakers_pipeline_version = PROVIDER_DIARIZE_PIPELINE_VERSION
            ep.speakers_error = None
            ep.speakers_source = "provider"
            known_good = {
                **known_good,
                "diarize_spec_hash": spec,
                "diarize_confidence": confidence,
                "diarize_status": "known_good",
            }
            registry = dict(ep.provider_transcript or {})
            registry["known_good"] = known_good
            ep.provider_transcript = registry
            stats.ran += 1
        return stats


def _diarize_spec_hash(ep: Episode, model: str, embedding_model: str) -> str:
    spec = {
        "v": DIARIZE_PIPELINE_VERSION,
        "audio": ep.audio_key or ep.hosted_audio_url,
        "words": ep.transcript_words_key,
        "transcript": ep.transcript_spec_hash,
        "model": model,
        "embedding_model": embedding_model,
    }
    blob = json.dumps(spec, separators=(",", ":"), sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def _diarize_runtime_recipe(model: str, embedding_model: str) -> str:
    """Identify a runtime profile without mixing content-addressed episode inputs into it."""
    return f"{DIARIZE_PIPELINE_VERSION}:{model}:{embedding_model}"


def _diarize_object_key(src_key: str, uid: str, spec: str) -> str:
    return f"transcripts/{src_key}/{uid}-diarize-{spec}.speakers.json"


class NativeDiarizeStage:
    """Run R7 native diarization on hosted audio without modifying transcript wording."""

    name = "native_diarize"
    version = DIARIZE_PIPELINE_VERSION

    def process(
        self, provider, city: City, episodes: list[Episode], ctx: StageContext
    ) -> StageStats:
        from citypods.compute.base import JobHandle, JobResult
        from citypods.records import source_key

        stats = StageStats(self.name)
        config = ctx.speaker_config or {}
        if not config.get("enabled") or ctx.dry_run or ctx.storage is None:
            return stats
        model = str(config.get("model") or DEFAULT_DIARIZE_MODEL)
        embedding_model = str(config.get("embedding_model") or DEFAULT_EMBEDDING_MODEL)
        runtime_recipe = _diarize_runtime_recipe(model, embedding_model)
        from citypods.speakers import (
            load_turn_evidence,
            pilot_selected,
            public_turn,
            save_turn_evidence,
        )

        turn_evidence = (
            load_turn_evidence(ctx.speaker_turn_evidence_path)
            if ctx.speaker_turn_evidence_path is not None
            else {"version": 1, "episodes": {}}
        )
        runtime_log = DiarizeRuntimeLog(ctx.diarize_runtime_log_path)
        # External workers reserve this work class but do not yet materialize R7 artifacts.  Use
        # the pinned local pyannote stack until their pull-worker writer is implemented instead
        # of accepting a handle that no worker can complete.
        backend = LocalBackend(asr_mod)
        src_key = source_key(city)
        for ep in _materialize_set(
            episodes,
            city.full_artifact_episodes,
            feed_visible_per_body=city.max_episodes,
            policy=ctx.backlog_policy,
            city_slug=city.slug,
            work_class="transcript-diarize",
        ):
            uid = ep.uid or ep.guid
            if not pilot_selected(config, city.slug, ep.body):
                # Clear stale no-output markers from the old exact-body matcher. A later pass will
                # see newly selected bodies immediately, while valid selected artifacts reuse.
                marker = ep.stage_completion.get(self.name)
                if isinstance(marker, dict) and not marker.get("output"):
                    ep.stage_completion.pop(self.name, None)
                stats.quality("pilot-not-selected")
                continue
            if ep.speakers_source == "provider" and ep.speakers_synced:
                stats.reused += 1
                continue
            if not (ep.hosted_audio_url and ep.transcript_synced and ep.transcript_words_key):
                stats.defer("missing-timed-words", sample=uid)
                continue
            words_raw = _read_storage_bytes(ctx.storage, ep.transcript_words_key)
            if words_raw is None or not has_valid_timed_words(words_raw):
                stats.defer("invalid-timed-words", sample=uid)
                continue
            spec = _diarize_spec_hash(ep, model, embedding_model)
            key = _diarize_object_key(src_key, uid, spec)
            if ep.speakers_key == key and ep.speakers_synced and ctx.storage.exists(key):
                ep.speakers_url = ctx.storage.public_url(key)
                stats.reused += 1
                continue
            if ctx.stop is not None and ctx.stop():
                stats.defer("wall-clock-budget", sample=uid)
                continue
            recording_seconds = max(0.0, episode_served_duration_seconds(ep) or 0.0)
            fits, estimate, remaining = _diarize_fits_remaining_budget(
                ctx, runtime_log, recording_seconds, runtime_recipe
            )
            if not fits:
                stats.defer("runtime-budget", sample=uid)
                print(
                    f"[enrich] diarize defer uid={uid} estimate_s={estimate or 0:.1f} "
                    f"remaining_s={remaining or 0:.1f}",
                    flush=True,
                )
                continue
            # Do not let a prior transient error look like this attempt's outcome.
            ep.speakers_error = None
            try:
                from citypods.diarize import attach_transcript_words

                words = json.loads(words_raw.decode())
                if not isinstance(words, dict):
                    raise ValueError("timed transcript words must be a JSON object")
                diarize_started_at = time.monotonic()
                with _download_audio(ep.hosted_audio_url) as audio_path:
                    outcome = backend.run_inference(
                        InferenceJob(
                            task="diarize",
                            inputs={
                                "audio_path": audio_path,
                                "model": model,
                                "embedding_model": embedding_model,
                                # Secret-only by design: never read a token from committed config.
                                "token": os.environ.get("HF_TOKEN")
                                or os.environ.get("HUGGINGFACE_HUB_TOKEN"),
                                "device": config.get("device"),
                            },
                            recipe_hash=spec,
                        )
                    )
                if isinstance(outcome, JobHandle):
                    stats.defer("diarize-dispatched", sample=uid)
                    continue
                if not isinstance(outcome, JobResult):
                    raise ValueError("diarize backend returned an invalid result")
                artifact = outcome.output
                attach_transcript_words(artifact.turns, words)
                # The public diarization object deliberately never contains numerical voice
                # vectors.  They remain private review evidence keyed to this exact recipe.
                private_turns = [dict(turn) for turn in artifact.turns]
                turn_evidence.setdefault("episodes", {})[uid] = {
                    "spec_hash": spec,
                    "turns": private_turns,
                }
                payload = {
                    "schema": "2",
                    "basis": "served",
                    "source": "native",
                    "engine": getattr(artifact, "engine", "pyannote"),
                    "model": getattr(artifact, "model", model),
                    "embedding_recipe": embedding_model,
                    "clusters": getattr(artifact, "clusters", []),
                    "turns": [public_turn(turn) for turn in artifact.turns],
                }
                with tempfile.TemporaryDirectory() as directory:
                    dest = Path(directory) / "speakers.json"
                    dest.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True))
                    url = ctx.storage.put_file(key, dest, "application/json")
                ep.speakers_key = key
                ep.speakers_url = url
                ep.speakers_spec_hash = spec
                ep.speakers_format = "json"
                ep.speakers_synced = True
                ep.speakers_confidence = None
                ep.speakers_pipeline_version = DIARIZE_PIPELINE_VERSION
                ep.speakers_error = None
                ep.speakers_source = "native"
                runtime_log.append(
                    diarize_seconds=time.monotonic() - diarize_started_at,
                    recording_seconds=recording_seconds,
                    recipe=runtime_recipe,
                )
                stats.ran += 1
            except Exception as exc:  # noqa: BLE001 - per-item native inference must be restartable.
                ep.speakers_error = f"native-diarize-error: {exc}"
                stats.errors.append(f"{uid}: {exc}")
        if ctx.speaker_turn_evidence_path is not None:
            save_turn_evidence(ctx.speaker_turn_evidence_path, turn_evidence)
        return stats


class SpeakerIdentityStage:
    """Observe minutes continuity and project named turns onto already-grounded R6 quotes."""

    name = "speaker_identity"
    version = IDENTITY_PIPELINE_VERSION

    def process(
        self, provider, city: City, episodes: list[Episode], ctx: StageContext
    ) -> StageStats:
        from citypods.speakers import (
            auto_publish_allowed,
            calibration_cell,
            chair_reference_candidates,
            load_registry,
            load_turn_evidence,
            observe_attendance,
            pilot_capture_context,
            pilot_selected,
            profile_matches,
            quote_attribution,
            reference_candidate_id,
            refresh_membership_status,
            roster_person_ids,
            save_registry,
            shadow_candidate_id,
        )

        stats = StageStats(self.name)
        if ctx.speaker_registry_path is None or ctx.storage is None or ctx.dry_run:
            return stats
        registry = load_registry(ctx.speaker_registry_path)
        turn_evidence = (
            load_turn_evidence(ctx.speaker_turn_evidence_path)
            if ctx.speaker_turn_evidence_path is not None
            else {"episodes": {}}
        )
        for ep in episodes:
            if not pilot_selected(ctx.speaker_config or {}, city.slug, ep.body):
                continue
            if ep.minutes_roster or ep.minutes_votes:
                observe_attendance(
                    registry,
                    city_slug=city.slug,
                    body=ep.body,
                    episode_uid=ep.uid or ep.guid,
                    published=ep.published,
                    roster=ep.minutes_roster,
                    votes=ep.minutes_votes,
                )
        refresh_membership_status(registry)
        evaluation: dict = {"reviews": []}
        if ctx.speaker_evaluation_state_path and ctx.speaker_evaluation_state_path.exists():
            try:
                evaluation = json.loads(ctx.speaker_evaluation_state_path.read_text())
            except (OSError, ValueError):
                evaluation = {"reviews": []}
        for ep in episodes:
            if not pilot_selected(ctx.speaker_config or {}, city.slug, ep.body):
                continue
            if not ep.speakers_key:
                continue
            raw = _read_storage_bytes(ctx.storage, ep.speakers_key)
            try:
                payload = json.loads((raw or b"{}").decode())
            except (UnicodeDecodeError, ValueError):
                continue
            turns = payload.get("turns") if isinstance(payload, dict) else None
            if not isinstance(turns, list):
                continue
            private_episode = (turn_evidence.get("episodes") or {}).get(ep.uid or ep.guid, {})
            if not isinstance(private_episode, dict):
                private_episode = {}
            private_turns = private_episode.get("turns")
            if private_episode.get("spec_hash") != ep.speakers_spec_hash or not isinstance(
                private_turns, list
            ):
                private_turns = turns
            capture_context = pilot_capture_context(ctx.speaker_config or {}, city.slug, ep.body)
            if capture_context is None:
                continue
            embedding_recipe = str(
                payload.get("embedding_recipe")
                or f"{city.provider}:{payload.get('model') or 'unknown'}"
            )
            engine_recipe = ":".join(
                (
                    city.provider,
                    str(payload.get("engine") or "unknown"),
                    str(ep.speakers_pipeline_version or DIARIZE_PIPELINE_VERSION),
                    str(payload.get("model") or "unknown"),
                    embedding_recipe,
                )
            )
            known_names = [
                value
                for person in (registry.get("people") or {}).values()
                if isinstance(person, dict)
                for value in [person.get("display_name"), *(person.get("aliases") or [])]
                if value
            ]
            known_names.extend(
                str(item.get("name"))
                for item in ep.minutes_roster
                if isinstance(item, dict) and item.get("name")
            )
            if ep.transcript_words_key:
                words_raw = _read_storage_bytes(ctx.storage, ep.transcript_words_key)
                try:
                    words = json.loads((words_raw or b"{}").decode())
                except (UnicodeDecodeError, ValueError):
                    words = {}
                if isinstance(words, dict):
                    reference_rows = evaluation.setdefault("reference_candidates", {})
                    if not isinstance(reference_rows, dict):
                        reference_rows = {}
                        evaluation["reference_candidates"] = reference_rows
                    for candidate in chair_reference_candidates(
                        words, private_turns, known_names=known_names
                    ):
                        candidate_id = reference_candidate_id(
                            city_slug=city.slug,
                            body=ep.body,
                            episode_uid=ep.uid or ep.guid,
                            recipe=engine_recipe,
                            proposed_name=str(candidate["display_name"]),
                            cue_start=float(candidate["cue_start"]),
                            turn=candidate,
                        )
                        if candidate_id not in reference_rows:
                            stats.quality("chair-reference-candidate")
                        reference_rows[candidate_id] = {
                            **candidate,
                            "candidate_id": candidate_id,
                            "city_slug": city.slug,
                            "body": ep.body or "",
                            "engine_recipe": engine_recipe,
                            "episode_uid": ep.uid or ep.guid,
                            "episode_title": ep.title,
                            "embedding_recipe": embedding_recipe,
                            "capture_context": capture_context,
                        }
            cell = calibration_cell(
                city.slug, ep.body, engine_recipe, capture_context=capture_context
            )
            publish = auto_publish_allowed(
                evaluation,
                cell=cell,
                engine=str(payload.get("engine") or ""),
            )
            allowed_ids = {
                ident
                for ident, person in (registry.get("people") or {}).items()
                if isinstance(person, dict) and person.get("status") == "active"
            }
            # Once minutes arrive their roster is a correction constraint.  It does not make a
            # name true by itself, but it may silently replace a previous provisional voice-only
            # assignment with the next calibrated roster candidate.
            roster_ids = roster_person_ids(registry, ep.minutes_roster)
            if roster_ids is not None:
                allowed_ids &= roster_ids
            changed = False
            enriched_turns = []
            for turn in private_turns:
                if not isinstance(turn, dict) or not isinstance(turn.get("embedding"), list):
                    enriched_turns.append(turn)
                    continue
                matches = profile_matches(
                    registry,
                    turn["embedding"],
                    embedding_recipe=embedding_recipe,
                    allowed_ids=allowed_ids,
                )
                from citypods.speakers import assign_turn

                enriched_turns.append(
                    assign_turn(
                        turn,
                        matches,
                        publish=publish,
                        confirmed=roster_ids is not None,
                        minimum_score=float(ctx.speaker_config.get("minimum_match_score", 0.75)),
                    )
                )
            for turn in enriched_turns:
                identity = turn.get("identity") if isinstance(turn, dict) else None
                if not isinstance(identity, dict) or identity.get("status") != "shadow":
                    continue
                candidate_id = shadow_candidate_id(
                    city_slug=city.slug,
                    body=ep.body,
                    episode_uid=ep.uid or ep.guid,
                    recipe=engine_recipe,
                    turn=turn,
                )
                candidate_rows = evaluation.setdefault("candidates", {})
                if not isinstance(candidate_rows, dict):
                    candidate_rows = {}
                    evaluation["candidates"] = candidate_rows
                candidate_rows[candidate_id] = {
                    "candidate_id": candidate_id,
                    "city_slug": city.slug,
                    "body": ep.body or "",
                    "engine_recipe": engine_recipe,
                    "episode_uid": ep.uid or ep.guid,
                    "episode_title": ep.title,
                    "start": turn.get("start"),
                    "end": turn.get("end"),
                    "speaker_id": identity.get("speaker_id"),
                    "display_name": identity.get("display_name"),
                    "transcript_text_hash": turn.get("transcript_text_hash"),
                    "capture_context": capture_context,
                }
            for candidate in ep.moment_pullquote_candidates or []:
                if not isinstance(candidate, dict):
                    continue
                attribution = quote_attribution(candidate, enriched_turns)
                if attribution != candidate.get("speaker_attribution"):
                    registry.setdefault("history", []).append(
                        {
                            "kind": "attribution-correction"
                            if ep.minutes_roster
                            else "attribution-projection",
                            "episode_uid": ep.uid or ep.guid,
                            "candidate_id": candidate.get("candidate_id"),
                            "prior": candidate.get("speaker_attribution"),
                            "next": attribution,
                            "observed_at": datetime.now(UTC).isoformat(),
                        }
                    )
                    if attribution is None:
                        candidate.pop("speaker_attribution", None)
                    else:
                        candidate["speaker_attribution"] = attribution
                    changed = True
            if changed:
                # The diarization object is content-addressed solely by audio/transcript/model
                # inputs. Identity is a mutable, calibrated projection, so it belongs on the
                # durable R6 candidate ledger rather than rewriting immutable speaker bytes.
                stats.ran += 1
        save_registry(ctx.speaker_registry_path, registry)
        if ctx.speaker_evaluation_state_path is not None:
            from citypods.speakers import save_evaluation

            save_evaluation(ctx.speaker_evaluation_state_path, evaluation)
        return stats


def _chapter_llm_backend(ctx):
    """Build one policy-aware chapter backend per run, cached on the shared context."""

    backend = getattr(ctx, "chapter_llm_backend", None)
    if backend is None:
        from citypods.compute.llm import LiteLLMBackend, LLMBackendConfig

        backend = LiteLLMBackend(LLMBackendConfig.from_env(), storage=ctx.storage)
        ctx.chapter_llm_backend = backend
    return backend


def _cancel_chapter_fallbacks(ctx, stats: StageStats, states: list[dict]) -> None:
    """Cancel queued agenda or locator fallbacks and discard their matching deferred handles."""
    from citypods.compute.llm_deferred import discard_deferred

    candidates = [
        (state.get(recipe_key), state.get(ref_key))
        for state in states
        for recipe_key, ref_key in (("recipe", "job_ref"), ("locator_recipe", "locator_job_ref"))
        if isinstance(state.get(recipe_key), str) and isinstance(state.get(ref_key), str)
    ]
    refs = [ref for _recipe, ref in candidates]
    cancelled_refs: set[str] = set()
    cancel = getattr(_chapter_llm_backend(ctx), "cancel_batch", None)
    if refs and callable(cancel):
        try:
            cancelled_refs = set(cancel(refs).get("cancelled", ()))
        except Exception as exc:  # noqa: BLE001 -- exclusion must still prevent new work
            stats.errors.append(f"provider chapter cancellation batch: {exc}")
    for recipe, ref in candidates:
        if ref in cancelled_refs:
            discard_deferred(ctx.storage, recipe, expected_ref=ref)


def _write_chapter_json(storage, key: str, value: dict) -> str:
    """Upload a bounded generated-chapter artifact without exposing its local temp path."""

    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "chapter-artifact.json"
        path.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        return storage.put_file(key, path, "application/json")


class AgendaChapterCandidatesStage:
    """Extract source-grounded agenda candidates through the production Mistral route."""

    name = "chapter_agenda"
    version = CHAPTER_AGENDA_PIPELINE_VERSION

    def process(
        self, provider, city: City, episodes: list[Episode], ctx: StageContext
    ) -> StageStats:
        from citypods.chapter_artifacts import artifact_key
        from citypods.chapter_jobs import build_agenda_job, finalize_agenda_job
        from citypods.chapter_titles import AGENDA_PRODUCTION_MODEL
        from citypods.compute.base import JobHandle, JobResult
        from citypods.compute.llm import dispatch_job_batch

        stats = StageStats(self.name)
        if ctx.dry_run or ctx.storage is None:
            return stats

        materialized = _materialize_set(
            episodes,
            city.full_artifact_episodes,
            feed_visible_per_body=city.max_episodes,
            policy=ctx.backlog_policy,
            city_slug=city.slug,
            work_class="chapter-agenda",
        )

        # Provider chapters are canonical.  Cancel any still-queued fallback job before clearing
        # its derived state; an in-flight attempt is deliberately allowed to settle, but cannot
        # be published because this stage records the exclusion below.  One batched cancellation
        # keeps this migration from recreating the per-episode Worker-invocation pattern v2 fixed.
        exclusions: list[tuple[Episode, dict]] = []
        for ep in materialized:
            if ep.source_chapters:
                exclusions.append((ep, dict(ep.generated_agenda_candidates or {})))
        if exclusions:
            _cancel_chapter_fallbacks(ctx, stats, [state for _ep, state in exclusions])
            for ep, _state in exclusions:
                ep.generated_agenda_candidates = {
                    "status": "not_applicable",
                    "reason": "provider_chapters",
                    "provider_chapter_count": len(ep.source_chapters),
                    "locator_status": "not_applicable",
                }
                ep.generated_chapters = []
                ep.generated_chapters_spec_hash = ""
                stats.reused += 1

        # Pass 1: validate and build every eligible episode's job without dispatching any of
        # them yet, so the whole run's jobs can be submitted in one enqueue_batch call below
        # instead of one Worker request per episode (see review/44's 2026-08-18 incident
        # retrospective -- per-job dispatch/reconcile calls were exactly the Worker-request
        # volume that incident flagged as the thing worth fixing next).
        prepared: list[tuple[Episode, str, Any, str, str]] = []
        for ep in materialized:
            if ep.source_chapters:
                continue
            uid = ep.uid or ep.guid
            source_artifact = (ep.links or {}).get("agenda_text_artifact_key")
            if not uid or not source_artifact:
                # DIAGNOSTIC: split out the case that shouldn't be possible on paper -- agenda_text
                # already marked this episode `accepted` (so it presumably stored a document) but
                # this stage's own dependency, `links["agenda_text_artifact_key"]`, was never set.
                # A bounded sample of uids (`defer_samples`) lets a live run be cross-referenced
                # against agenda_text's `[agenda_text] artifact store result` line for the same
                # uid. Remove once root-caused; see the matching note in `_store_document`.
                if uid and (ep.agenda_text_quality or {}).get("status") == "accepted":
                    stats.defer("missing-agenda-artifact-despite-accepted-quality", sample=uid)
                else:
                    stats.defer("missing-agenda-artifact")
                continue
            raw = _read_storage_bytes(ctx.storage, source_artifact)
            if raw is None:
                # DIAGNOSTIC: a different failure mode than the one above -- the link exists but
                # the object it points at could not be read back (deleted, wrong bucket/prefix,
                # transient read error). Kept as its own reason so it doesn't get conflated with
                # "never had a link" in the aggregate breakdown.
                stats.defer("agenda-artifact-key-present-but-unreadable", sample=uid)
                continue
            agenda_text = raw.decode("utf-8", errors="replace")
            source_hash = hashlib.sha256(raw).hexdigest()
            if ctx.stop is not None and ctx.stop():
                stats.defer("stop-signal")
                continue
            try:
                job = build_agenda_job(
                    episode_uid=uid,
                    agenda_text=agenda_text,
                    agenda_source_hash=source_hash,
                )
            except Exception as exc:  # noqa: BLE001 -- one malformed agenda must not abort the
                # build pass for every other episode
                stats.errors.append(f"{uid}: agenda chapter extraction: {exc}")
                continue
            prepared.append((ep, uid, job, agenda_text, source_hash))

        if not prepared:
            return stats

        # Pass 2: one dispatch for the whole batch.
        results = dispatch_job_batch(
            _chapter_llm_backend(ctx), [job for _, _, job, _, _ in prepared]
        )

        # Pass 3: finalize each episode against its own result, same as the old per-job code.
        for (ep, uid, job, agenda_text, source_hash), result in zip(prepared, results, strict=True):
            if isinstance(result, Exception):
                stats.errors.append(f"{uid}: agenda chapter extraction: {result}")
                continue
            try:
                if isinstance(result, JobHandle):
                    ep.generated_agenda_candidates = {
                        "status": "pending",
                        "recipe": job.recipe_hash,
                        # AGENDA_PRODUCTION_MODELS (R13) now offers more than one same-priority
                        # candidate; record the model the scheduler actually reserved for this
                        # dispatch, not just the first/label candidate.
                        "model": result.model or AGENDA_PRODUCTION_MODEL,
                        "source_hash": source_hash,
                        "job_ref": result.ref,
                    }
                    stats.defer("llm-pending")
                    continue
                if not isinstance(result, JobResult):
                    stats.errors.append(f"{uid}: unexpected agenda job result")
                    continue
                artifact = finalize_agenda_job(
                    result,
                    episode_uid=uid,
                    agenda_text=agenda_text,
                    agenda_source_hash=source_hash,
                )
                key = artifact_key("agenda", uid, artifact.recipe)
                url = _write_chapter_json(ctx.storage, key, artifact.to_dict())
                ep.generated_agenda_candidates = {
                    **artifact.to_dict(),
                    "artifact_key": key,
                    "artifact_url": url,
                }
                stats.ran += 1
            except Exception as exc:  # noqa: BLE001 -- one malformed agenda must not abort the
                # finalize pass for every other episode
                stats.errors.append(f"{uid}: agenda chapter extraction: {exc}")
        return stats


class ChapterBoundaryLocatorStage:
    """Locate agenda candidates in the complete timed transcript."""

    name = "chapter_locator"
    version = CHAPTER_LOCATOR_PIPELINE_VERSION

    def process(
        self, provider, city: City, episodes: list[Episode], ctx: StageContext
    ) -> StageStats:
        from citypods.chapter_artifacts import AgendaCandidatesArtifact, artifact_key
        from citypods.chapter_jobs import build_locator_job, finalize_locator_job
        from citypods.chapter_locator import build_locator_units
        from citypods.compute.base import JobHandle, JobResult
        from citypods.compute.llm import dispatch_job_batch

        stats = StageStats(self.name)
        if ctx.dry_run or ctx.storage is None:
            return stats

        # Pass 1: validate and build every eligible episode's job without dispatching any of
        # them yet -- see AgendaChapterCandidatesStage.process for why (review/44's 2026-08-18
        # incident retrospective).
        prepared: list[tuple[Episode, str, Any, Any, str, list, str, dict]] = []
        for ep in _materialize_set(
            episodes,
            city.full_artifact_episodes,
            feed_visible_per_body=city.max_episodes,
            policy=ctx.backlog_policy,
            city_slug=city.slug,
            work_class="chapter-locator",
        ):
            if ep.source_chapters:
                # AgendaChapterCandidatesStage normally records this at the same time it clears
                # historical generated output. Keep the locator independently safe for a
                # locator-only lane invocation or a partially persisted earlier agenda pass.
                raw = dict(ep.generated_agenda_candidates or {})
                _cancel_chapter_fallbacks(ctx, stats, [raw])
                raw.update(
                    {
                        "status": "not_applicable",
                        "reason": "provider_chapters",
                        "provider_chapter_count": len(ep.source_chapters),
                        "locator_status": "not_applicable",
                    }
                )
                ep.generated_agenda_candidates = raw
                ep.generated_chapters = []
                ep.generated_chapters_spec_hash = ""
                stats.reused += 1
                continue
            uid = ep.uid or ep.guid
            raw_agenda = ep.generated_agenda_candidates or {}
            if not uid or raw_agenda.get("status") not in {"completed", "accepted"}:
                stats.defer("agenda-not-complete")
                continue
            words = _read_storage_bytes(ctx.storage, ep.transcript_words_key)
            vtt = _read_storage_bytes(ctx.storage, ep.transcript_key)
            units, unit_source = build_locator_units(words_data=words, vtt_data=vtt)
            if not units:
                stats.defer("missing-timed-transcript")
                continue
            if ctx.stop is not None and ctx.stop():
                stats.defer("stop-signal")
                continue
            try:
                agenda = AgendaCandidatesArtifact.from_dict(raw_agenda)
                selected_data = words if unit_source == "words" else vtt
                transcript_hash = hashlib.sha256(selected_data or b"").hexdigest()
                job = build_locator_job(
                    episode_uid=uid,
                    agenda=agenda,
                    transcript_hash=transcript_hash,
                    units=units,
                )
            except Exception as exc:  # noqa: BLE001 -- one locator failure must not abort the
                # build pass for every other episode
                stats.errors.append(f"{uid}: chapter locator: {exc}")
                continue
            prepared.append((ep, uid, job, agenda, transcript_hash, units, unit_source, raw_agenda))

        if not prepared:
            return stats

        # Pass 2: one dispatch for the whole batch.
        results = dispatch_job_batch(
            _chapter_llm_backend(ctx), [job for _, _, job, _, _, _, _, _ in prepared]
        )

        # Pass 3: finalize each episode against its own result, same as the old per-job code.
        for (ep, uid, job, agenda, transcript_hash, units, unit_source, raw_agenda), result in zip(
            prepared, results, strict=True
        ):
            if isinstance(result, Exception):
                stats.errors.append(f"{uid}: chapter locator: {result}")
                continue
            try:
                if isinstance(result, JobHandle):
                    raw_agenda = dict(raw_agenda)
                    raw_agenda.update(
                        {
                            "locator_status": "pending",
                            "locator_recipe": job.recipe_hash,
                            "locator_job_ref": result.ref,
                        }
                    )
                    ep.generated_agenda_candidates = raw_agenda
                    stats.defer("llm-pending")
                    continue
                if not isinstance(result, JobResult):
                    stats.errors.append(f"{uid}: unexpected locator job result")
                    continue
                boundary = finalize_locator_job(
                    result,
                    episode_uid=uid,
                    agenda=agenda,
                    transcript_hash=transcript_hash,
                    units=units,
                )
                boundary_key = artifact_key("boundary", uid, boundary.recipe)
                boundary_url = _write_chapter_json(ctx.storage, boundary_key, boundary.to_dict())
                items = {item.index: item for item in agenda.items}
                generated = []
                for anchor in boundary.anchors:
                    item = items.get(anchor.get("agenda_item_index"))
                    start = anchor.get("start")
                    # BoundaryResultArtifact schema ensures start is always set, but guard
                    # defensively so a malformed persisted anchor cannot cause a TypeError in
                    # episode_public_chapters (which calls float(start) unconditionally).
                    if item is None or item.status != "accepted" or start is None:
                        continue
                    generated.append(
                        {
                            "start": start,
                            "title": item.title,
                            "agenda_item_index": item.index,
                            "display_ref": item.display_ref,
                            "evidence_text": item.evidence_text,
                            "unit_id": anchor.get("unit_id"),
                            "transition_quote": anchor.get("transition_quote"),
                            "basis": anchor.get("basis", "served"),
                            "generated": True,
                            "model": boundary.model,
                            "prompt_version": boundary.prompt_version,
                            "artifact_key": boundary_key,
                        }
                    )
                ep.generated_chapters = generated
                ep.generated_chapters_spec_hash = boundary.recipe
                ep.generated_agenda_candidates = {
                    **dict(raw_agenda),
                    "locator_status": "completed",
                    "boundary_artifact_key": boundary_key,
                    "boundary_artifact_url": boundary_url,
                    "transcript_unit_source": unit_source,
                }
                stats.ran += 1
            except Exception as exc:  # noqa: BLE001 -- one locator failure must not abort the
                # finalize pass for every other episode
                stats.errors.append(f"{uid}: chapter locator: {exc}")
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
        AgendaTextStage(),
        MinutesTextStage(),
        ProviderTranscriptDiarizeStage(),
        TagsStage(),
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
        LinksStage(),
        AgendaTextStage(),
        MinutesTextStage(),
        ProviderTranscriptDiarizeStage(),
        TagsStage(),
    ]


def r6_stages() -> list[EnrichmentStage]:
    """R6's opt-in stages, kept separate so legacy stage ordering remains stable."""
    return [MomentsStage(), MomentJudgeStage(), MomentAdmissionStage(), VideoClipsStage()]


def r7_stages() -> list[EnrichmentStage]:
    """R7 is opt-in while its per-body identity calibration warms up."""
    return [NativeDiarizeStage(), SpeakerIdentityStage()]


# Which stages each H6b lane runs (review/12 §H6). A lane runs ONLY its own work-class stages so it
# never re-derives — and so, via the whole-record state push, never regresses — a sibling lane's
# artifact: the ``transcribe`` lane must not re-run ``audio`` (which would write an audio block from
# its start-of-run snapshot), and the ``audio`` lane must not run ``transcript``. The default/None
# lane (full ``enrich``/``all``, manual single-source) runs every stage. This pairs with
# ``records.protected_blocks_for_lane`` — owned-stages and owned-blocks must stay consistent; extend
# both together when a lane lands (e.g. ``"diarize": frozenset({"diarize"})`` — review/12 §H5).
LANE_STAGES: dict[str, frozenset[str]] = {
    # The audio worker owns document extraction too: it is source-scoped, needs the complete
    # meeting list for conservative prior-meeting minutes inheritance, and does not depend on ASR.
    "audio": frozenset(
        {
            "chapters",
            "timeline",
            "remap",
            "audio",
            "links",
            "agenda_text",
            "minutes_text",
        }
    ),
    "transcribe": frozenset({"transcript"}),
    "align": frozenset({"transcript"}),
    "diarize": frozenset({"diarize", "native_diarize"}),
    "speaker-identity": frozenset({"speaker_identity"}),
    "tag": frozenset({"tags"}),
    "moments": frozenset({"moments", "moment-judge", "moment-admission", "video-clips"}),
    "chapter-agenda": frozenset({"chapter_agenda"}),
    "chapter-locator": frozenset({"chapter_locator", "generated_chapters"}),
    "chapter": frozenset({"chapter_agenda", "chapter_locator", "generated_chapters"}),
}


def run_stages(
    provider,
    city: City,
    episodes: list[Episode],
    stages: list[EnrichmentStage],
    ctx: StageContext,
    *,
    quiet: bool = False,
) -> list[StageStats]:
    """Run ``stages`` over ``episodes`` in order, timing each. ``quiet`` suppresses the
    per-stage log lines — used by the PR3 global queue, which dispatches per *episode* and
    would otherwise emit thousands of per-stage lines; it logs its own per-item summary.

    When ``ctx.lane`` selects an H6b work-class lane, stages outside that lane are skipped
    (``LANE_STAGES``) so the lane computes only its own artifact — the second half of the
    cross-lane write-isolation fix (review/12 §H6)."""
    allowed = LANE_STAGES.get(ctx.lane) if ctx.lane is not None else None
    out: list[StageStats] = []
    for stage in stages:
        if allowed is not None and stage.name not in allowed:
            continue
        dirty = [
            ep
            for ep in episodes
            if stage_is_dirty(stage, ep, city, speaker_config=ctx.speaker_config)
        ]
        clean = len(episodes) - len(dirty)
        if not dirty:
            stat = StageStats(stage.name, reused=clean)
            out.append(stat)
            if not quiet:
                print(
                    f"[enrich] stage skip slug={city.slug} provider={city.provider} "
                    f"stage={stage.name} episodes={len(episodes)} reason=completion-cache",
                    flush=True,
                )
            continue
        if not quiet:
            print(
                f"[enrich] stage start slug={city.slug} provider={city.provider} "
                f"stage={stage.name} episodes={len(dirty)} clean={clean}",
                flush=True,
            )
        t0 = time.perf_counter()
        stat = stage.process(provider, city, dirty, ctx)
        stat.seconds = time.perf_counter() - t0
        stat.reused += clean
        _mark_stage_complete(stage, dirty, city, stat, speaker_config=ctx.speaker_config)
        if not quiet:
            print(
                f"[enrich] stage done slug={city.slug} provider={city.provider} stage={stage.name} "
                f"ran={stat.ran} reused={stat.reused} queued={stat.skipped} "
                f"errors={len(stat.errors)} seconds={stat.seconds:.1f}",
                flush=True,
            )
        out.append(stat)
        # A provider throttle in a planner is the materialization attempt for this item. Continuing
        # to AudioStage would immediately repeat the same source-cache request and double-count one
        # failure, so stop after recording the backoff.
        if stat.rate_limited:
            break
    return out
