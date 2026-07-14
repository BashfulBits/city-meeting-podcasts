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
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from citypods import asr as asr_mod
from citypods.asr import asr_initial_prompt
from citypods.bodies import body_key, canonical_body
from citypods.compute import DispatchCoordinator, InferenceJob
from citypods.compute.local import LocalBackend
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
    _in_backoff,
    confirmed_dead_recheck_due,
    transcript_media_hash,
    transcript_timeout_backoff_until,
)
from citypods.resources import MemoryReservation, NativeWorkGate, ResourceAdmission
from citypods.security import validate_source_url
from citypods.timeline import Timeline, edl_duration, remap, timeline_digest
from citypods.transcript_quality import (
    TranscriptQualityRoute,
    accepted_recipe_allowed,
    quality_body_key,
    record_l1_sample,
)


def _materialize_set(
    episodes: list[Episode],
    max_per_body: int,
    *,
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
    by_body: dict[str, list[Episode]] = collections.defaultdict(list)
    for ep in episodes:
        by_body[body_key(canonical_body(ep.body or ""))].append(ep)
    out: list[Episode] = []
    for eps in by_body.values():
        eps.sort(key=lambda e: e.published, reverse=True)
        out.extend(eps[:max_per_body])
    if policy is not None and policy.keys:
        key = sort_key_for(policy)
        out.sort(
            key=lambda ep: key(
                workitem_from_episode(ep, city_slug=city_slug, work_class=work_class)
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


class AsrArtifactCache:
    """Thread-safe run-local reuse for identical stable-uid + ASR-recipe work.

    The durable transcript layout remains source-scoped, but one real-world meeting may appear in
    multiple source views. The planner co-locates those aliases on one shard; this cache lets the
    first completed inference supply bytes to every source-local object without re-running ASR.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._values: dict[tuple[str, str], object] = {}
        self._inflight: set[tuple[str, str]] = set()

    def get(self, key: tuple[str, str]) -> object | None:
        with self._condition:
            return self._values.get(key)

    def claim(self, key: tuple[str, str]) -> tuple[bool, object | None]:
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

    def complete(self, key: tuple[str, str], value: object) -> None:
        with self._condition:
            self._values[key] = value
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
    # through — ``local`` (in-process faster-whisper/stable-ts) by default; H14 swaps in the
    # Modal/Beam dispatch adapters here with no stage change. None ⇒ build an in-process
    # ``LocalBackend`` on the stage's ``asr_mod`` (keeps the default path behavior-preserving).
    compute_backend: object | None = None
    stop: Callable[[], bool] | None = None
    chapters_per_source: int = 10_000  # ~unbounded; build() lowers it only for the PR preview
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
    # runs the transcript pass forcing fresh faster-whisper transcription (never loads stable-ts);
    # ``"align"`` runs the transcript pass align-only (stable-ts, episodes with a source transcript)
    # so the two ASR models never co-load in one runner. The pass selection lives in run.py; this
    # field tells TranscriptStage which ASR model path to take.
    lane: str | None = None


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
    dispatched: int = 0  # H14a: handed to an external GPU backend (off-runner); pending next render
    asr_migration_copied: int = 0
    asr_migration_already_present: int = 0
    asr_migration_missing: int = 0
    asr_migration_regenerated: int = 0
    rate_limited: int = 0  # audio encodes that hit HTTP 403 / provider throttle (GH#300)
    defer_reasons: dict[str, int] = field(default_factory=dict)
    defer_samples: list[str] = field(default_factory=list)

    def defer(self, reason: str, count: int = 1, *, sample: str | None = None) -> None:
        """Record restartable work left for a later run, grouped by a stable reason token."""
        self.skipped += count
        self.defer_reasons[reason] = self.defer_reasons.get(reason, 0) + count
        if sample and len(self.defer_samples) < 5:
            self.defer_samples.append(sample)

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
                        city.max_episodes,
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
                episodes, city.max_episodes, policy=ctx.backlog_policy, city_slug=city.slug
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
        for ep in _materialize_set(
            episodes, city.max_episodes, policy=ctx.backlog_policy, city_slug=city.slug
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
            ep.chapters = remap(ep.timeline, raw_chapters, source_id=source_id)
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
            episodes, city.max_episodes, policy=ctx.backlog_policy, city_slug=city.slug
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
                ep.source_chapters = [dict(ch) for ch in chapters]
                ep.chapters = [dict(ch) for ch in chapters]
                ep.chapters_basis = "source:s0"
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
        for ep in _materialize_set(
            episodes, city.max_episodes, policy=ctx.backlog_policy, city_slug=city.slug
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
    version = "1"

    def process(
        self, provider, city: City, episodes: list[Episode], ctx: StageContext
    ) -> StageStats:
        import json as _json
        from datetime import UTC, datetime

        from citypods.agenda_text import (
            AGENDA_TEXT_MAX_CHARS,
            DocumentLink,
            extract_document,
            minutes_links,
        )
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
            episodes, city.max_episodes, policy=ctx.backlog_policy, city_slug=city.slug
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
            if ep.agenda_text_url == agenda_url and ep.agenda_backup_url:
                stats.reused += 1
                continue
            if ctx.stop is not None and ctx.stop():
                stats.skipped += 1
                continue
            try:
                content, content_type = fetch(agenda_url)
                text, discovered = extract_document(
                    content, content_type=content_type, source_url=agenda_url
                )
                text = text[:AGENDA_TEXT_MAX_CHARS]
                links = [
                    link
                    for link in minutes_links(discovered)
                    if _is_valid_document_url(city, link.url)
                ]
                ep.agenda_text_url = agenda_url
                ep.agenda_text_attempts = 0
                ep.agenda_text_last_attempt = datetime.now(UTC).isoformat()
                agenda_artifact_url = _store_document(
                    ctx, src_key, ep.uid or ep.guid, "agenda", text.encode()
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
                manifest = [
                    {
                        "url": link.url,
                        "label": link.label,
                        "source_url": link.source_url,
                        "kind": link.kind,
                        "item_label": link.item_label,
                    }
                    for link in candidates
                ]
                if agenda_backup_backoff_until(ep) and agenda_backup_backoff_until(ep) > now:
                    stats.reused += 1
                    continue
                for link in candidates:
                    if link.url == agenda_url:
                        continue
                    try:
                        item_content, item_type = fetch(link.url)
                        item_text, _ = extract_document(
                            item_content, content_type=item_type, source_url=link.url
                        )
                        item_truncated = len(item_text) > 20_000
                        item_text = item_text[:20_000]
                        manifest_item = next(item for item in manifest if item["url"] == link.url)
                        manifest_item.update(
                            {
                                "text": item_text or None,
                                "truncated": item_truncated,
                                "source": "agenda-link",
                            }
                        )
                    except Exception:
                        manifest_item = next(item for item in manifest if item["url"] == link.url)
                        manifest_item.update(
                            {"text": None, "truncated": False, "source": "link-only"}
                        )
                ep.agenda_backup_url = _store_document(
                    ctx,
                    src_key,
                    ep.uid or ep.guid,
                    "backup",
                    _json.dumps(
                        {
                            "version": AGENDA_BACKUP_PIPELINE_VERSION,
                            "links": manifest,
                            "text": text,
                        },
                        sort_keys=True,
                    ).encode(),
                    "application/json",
                )
                ep.agenda_backup_attempts = 0
                ep.agenda_backup_last_attempt = now.isoformat()
                stats.ran += 1
            except Exception as exc:  # one malformed document must not stop the source
                ep.agenda_text_attempts += 1
                ep.agenda_text_last_attempt = now.isoformat()
                ep.agenda_backup_attempts += 1
                ep.agenda_backup_last_attempt = now.isoformat()
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
            episodes, city.max_episodes, policy=ctx.backlog_policy, city_slug=city.slug
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
                stats.skipped += 1
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
    if ctx.storage is None:
        return None
    import tempfile
    from pathlib import Path

    key = _document_key(source, uid, kind, content)
    if ctx.storage.exists(key):
        return ctx.storage.public_url(key)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / kind
        path.write_bytes(content)
        return ctx.storage.put_file(key, path, content_type)


def _document_key(source: str, uid: str, kind: str, content: bytes) -> str:
    import hashlib

    digest = hashlib.sha1(content).hexdigest()[:16]
    return f"documents/{source}/{uid}/{kind}-{digest}"


TRANSCRIPT_PIPELINE_VERSION = "1"
AGENDA_TEXT_PIPELINE_VERSION = "1"
AGENDA_BACKUP_PIPELINE_VERSION = "1"
MINUTES_TEXT_PIPELINE_VERSION = "1"
MINUTES_VOTES_PIPELINE_VERSION = "1"
MINUTES_ROSTER_PIPELINE_VERSION = "1"
PROVIDER_ALIGN_PIPELINE_VERSION = "1"
PROVIDER_DIARIZE_PIPELINE_VERSION = "1"
ASR_PIPELINE_VERSION = "3"  # H12: segment VTT + word-JSON sidecar; version-aware re-transcribe

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
        "timeline": tl_digest,
        "repair": timeline_audio_repair_token(ep, REPAIR_TRANSCRIPT_REGENERATE),
    }
    blob = json.dumps(spec, separators=(",", ":"), sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def _provider_align_object_key(src_key: str, uid: str, spec: str) -> str:
    return f"transcripts/{src_key}/{uid}-provider-align-{spec}.vtt"


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


def _provider_transcript_artifact(
    *,
    source_url: str,
    key: str,
    hosted_url: str,
    spec: str,
    fmt: str,
    synced: bool,
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


def _asr_recipe_hash(city: City, ep: Episode, align_hash: str | None) -> str:
    """ASR recipe hash keyed on transcript media/timeline inputs, not audio-byte recipes."""
    return asr_mod.asr_spec_hash(
        transcript_media_hash(ep),
        city.asr_model,
        align_hash,
        ASR_PIPELINE_VERSION,
        language=city.asr_language or None,
        compute_type=city.asr_compute_type,
        beam_size=city.asr_beam_size,
        initial_prompt=asr_initial_prompt(city.podcast_author, ep.body, ep.title),
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
    _reset_asr_timeout_backoff(ep)


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


# Public alias for cross-module callers (bench.py's asr-bench command, CR2-CP-39) — the
# underscore name stays the internal one this module's own callers use.
download_hosted_audio = _download_audio


def _download_audio_file(url: str, dest: Path) -> None:
    import requests as _req

    from citypods.http import USER_AGENT

    with _req.Session() as sess:
        sess.headers["User-Agent"] = USER_AGENT
        r = sess.get(url, timeout=300, stream=True)
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)


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

        def _store_asr_artifacts(ep: Episode, artifacts, recipe: str) -> None:
            """Write one inference result to this source's existing source-scoped object keys."""
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
            _reset_asr_timeout_backoff(ep)

        def _maybe_align_provider_transcript(
            ep: Episode,
            *,
            ep_ref: str,
            allow_active: bool,
        ) -> bool:
            """Promote and optionally publish a timed provider document as served-time VTT."""
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
                    and artifact.get("synced")
                    and artifact.get("format") in {"vtt", "srt"}
                ):
                    selected = dict(artifact)
                    break
            if selected is None:
                return False

            content = _read_storage_bytes(ctx.storage, selected["key"])
            if content is None:
                return False
            cues = _parse_timed_transcript(content, selected.get("format") or "vtt")
            remapped_cues, confidence = _remap_provider_cues(ep, cues)
            if not remapped_cues:
                return False

            align_spec = _provider_align_spec_hash(ep, selected)
            selected = {
                **selected,
                "confidence": confidence,
                "align_spec_hash": align_spec,
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

            key = _provider_align_object_key(src_key, ep.uid or ep.guid, align_spec)
            if ep.transcript_key == key and ep.transcript_synced and _present(key):
                ep.transcript_hosted_url = ctx.storage.public_url(key)
                stats.reused += 1
                return True

            with _tmp.TemporaryDirectory() as t:
                dest = Path(t) / "provider-aligned.vtt"
                dest.write_bytes(_provider_cues_to_vtt(remapped_cues))
                url = ctx.storage.put_file(key, dest, TRANSCRIPT_MIME["vtt"])

            provider_registry = dict(ep.provider_transcript or {})
            known = dict(provider_registry.get("known_good") or selected)
            known["aligned_key"] = key
            known["aligned_url"] = url
            provider_registry["known_good"] = known
            ep.provider_transcript = provider_registry

            ep.transcript_key = key
            ep.transcript_hosted_url = url
            ep.transcript_spec_hash = align_spec
            ep.transcript_format = "vtt"
            ep.transcript_basis = "served"
            ep.transcript_synced = True
            ep.transcript_words_key = None
            ep.transcript_words_url = None
            ep.transcript_pipeline_version = f"provider-align:{PROVIDER_ALIGN_PIPELINE_VERSION}"
            _reset_asr_timeout_backoff(ep)
            stats.ran += 1
            stats.aligned += 1
            print(
                f"[enrich] transcript provider-align done {ep_ref} "
                f"confidence={confidence if confidence is not None else 'unknown'}",
                flush=True,
            )
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

        for ep in _materialize_set(
            episodes,
            city.max_episodes,
            policy=ctx.backlog_policy,
            city_slug=city.slug,
            work_class="transcript-asr",
        ):
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
            provider_url = (ep.links or {}).get("transcript")
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
                    words_present = not ep.transcript_words_key or _present(ep.transcript_words_key)
                    if accepted_existing_asr and words_present:
                        ep.transcript_hosted_url = ctx.storage.public_url(ep.transcript_key)
                        if ep.transcript_words_key and _present(ep.transcript_words_key):
                            ep.transcript_words_url = ctx.storage.public_url(
                                ep.transcript_words_key
                            )
                        _reset_asr_timeout_backoff(ep)
                        stats.reused += 1
                        active_synced_reused = True
                        if not provider_url:
                            continue
                    elif ep.transcript_pipeline_version != ASR_PIPELINE_VERSION:
                        redo_stale_asr = True  # own recipe/version changed; re-transcribe
                    elif ep.transcript_spec_hash:
                        redo_stale_asr = True
                        recheck_asr_key = ep.transcript_key
                        recheck_asr_words_key = ep.transcript_words_key
                    else:
                        ep.transcript_hosted_url = ctx.storage.public_url(ep.transcript_key)
                        if ep.transcript_words_key and _present(ep.transcript_words_key):
                            ep.transcript_words_url = ctx.storage.public_url(
                                ep.transcript_words_key
                            )
                        _reset_asr_timeout_backoff(ep)
                        stats.reused += 1
                        active_synced_reused = True
                        if not provider_url:
                            continue
                else:
                    ep.transcript_hosted_url = ctx.storage.public_url(ep.transcript_key)
                    if ep.transcript_words_key and _present(ep.transcript_words_key):
                        ep.transcript_words_url = ctx.storage.public_url(ep.transcript_words_key)
                    _reset_asr_timeout_backoff(ep)
                    stats.reused += 1
                    active_synced_reused = True
                    if not provider_url:
                        continue
            elif ep.transcript_key and ep.transcript_synced:
                key_name = ep.transcript_key.rsplit("/", 1)[-1]
                if key_name.startswith(f"{ep.uid or ep.guid}-asr-"):
                    redo_stale_asr = True
                    if ep.transcript_pipeline_version == ASR_PIPELINE_VERSION:
                        recheck_asr_key = ep.transcript_key
                        recheck_asr_words_key = ep.transcript_words_key

            # 1b. Untimed stored transcript: re-attach URL so the feed still shows it as a
            #     text note, then fall through to the ASR slot to attempt an upgrade.
            if ep.transcript_key and not ep.transcript_synced and _present(ep.transcript_key):
                ep.transcript_hosted_url = ctx.storage.public_url(ep.transcript_key)
                # fall through to step 3

            # 2. Provider source transcript slot: fetch every pass that reaches the item and
            #    retain changed bytes as a candidate. PT-PR2 does not promote to known_good; the
            #    later provider-transcript-align/scoring path owns that decision.
            if provider_url:
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
                    validate_source_url(provider_url, resolve=True)
                    with make_session() as sess:
                        resp = sess.get(provider_url, timeout=30)
                    if resp.status_code >= 400:
                        stats.errors.append(f"{ep.uid}: HTTP {resp.status_code} for {provider_url}")
                        continue

                    content = resp.content
                    if not content.strip():
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

                        artifact = _provider_transcript_artifact(
                            source_url=provider_url,
                            key=key,
                            hosted_url=url,
                            spec=spec,
                            fmt=fmt,
                            synced=timed,
                            now=datetime.now(UTC).isoformat(),
                            status="candidate",
                        )
                        _provider_transcript_promote_candidate(ep, artifact)
                        stats.ran += 1
                        print(
                            f"[enrich] transcript provider-fetch done {ep_ref} "
                            f"fmt={fmt} candidate=true",
                            flush=True,
                        )
                        # Do not promote or switch the active <podcast:transcript> here. If an
                        # ASR/provider-aligned transcript is already active, it keeps serving;
                        # PT-PR3 decides temporary feed exposure for provider documents.
                except Exception as exc:  # noqa: BLE001
                    stats.errors.append(f"{ep.uid}: {exc}")
                    print(
                        f"[enrich] transcript provider-fetch error {ep_ref}: {exc}",
                        flush=True,
                    )
                    continue

            if provider_url:
                registry = ep.provider_transcript or {}
                active_provider = registry.get("known_good") or registry.get("candidate")
                if (
                    not ep.transcript_hosted_url
                    and isinstance(active_provider, dict)
                    and active_provider.get("hosted_url")
                    and active_provider.get("format") == "txt"
                ):
                    ep.transcript_hosted_url = active_provider["hosted_url"]
                    ep.transcript_format = "txt"

            # PT-PR5: timed provider source documents are evaluated in source time, remapped
            # through the canonical timeline, and promoted to known_good only when their
            # confidence is at least the previous known_good. Do not replace an already-active
            # ASR/provider transcript in this rollout; the H15 trust router owns that policy.
            if _maybe_align_provider_transcript(
                ep,
                ep_ref=ep_ref,
                allow_active=not active_synced_reused,
            ):
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
            if route is not None and route.prefers_fresh_asr:
                align_text = None

            # Lane gating (H6b): the sharded asr.yml runs a single-model lane so faster-whisper and
            # stable-ts never co-load in one runner. ``transcribe`` forces fresh transcription (drop
            # the alignment hint → never load stable-ts); ``align`` only handles episodes with a
            # source transcript (others defer to a transcribe lane). The default (None) lane keeps
            # the auto per-episode behavior for a direct ``citypods enrich``. Apply this before the
            # alignment-enabled guard: production's transcribe lane must deliberately ignore source
            # text and generate fresh ASR even while the separate align lane remains disabled.
            if ctx.lane == "transcribe":
                align_text = None
            elif ctx.lane == "align" and align_text is None:
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
            if align_text and not (city.asr_alignment_enabled or align_lane_unblocked):
                stats.defer("alignment-disabled")
                print(
                    f"[enrich] transcript asr skipped {ep_ref} reason=alignment-disabled",
                    flush=True,
                )
                continue

            ensure_timeline_audio_repair_token(ep, REPAIR_TRANSCRIPT_REGENERATE)
            align_hash = hashlib.sha1(align_text.encode()).hexdigest()[:12] if align_text else None
            # Alignment falls back to fresh transcription on quality/runtime errors, so keep the
            # fresh prompt/beam inputs in the recipe even when alignment is the primary path.
            initial_prompt = asr_initial_prompt(city.podcast_author, ep.body, ep.title)
            recipe = _asr_recipe_hash(city, ep, align_hash)
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
                    and _present(words_key)
                ):
                    _adopt_asr_keys(ep, ctx.storage, asr_key, words_key, recipe)
                    stats.reused += 1
                    continue
                if provider_url and align_text is None:
                    ep.transcript_hosted_url = ctx.storage.public_url(recheck_asr_key)
                    if recheck_asr_words_key and _present(recheck_asr_words_key):
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
                if vtt_status != "missing" and words_status != "missing":
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

            if not migration_missing and _present(asr_key):
                ep.transcript_key = asr_key
                ep.transcript_hosted_url = ctx.storage.public_url(asr_key)
                if _present(words_key):
                    ep.transcript_words_key = words_key
                    ep.transcript_words_url = ctx.storage.public_url(words_key)
                ep.transcript_synced = True
                ep.transcript_basis = "served"
                ep.transcript_format = "vtt"
                ep.transcript_spec_hash = recipe
                ep.transcript_pipeline_version = ASR_PIPELINE_VERSION
                _reset_asr_timeout_backoff(ep)
                stats.reused += 1
                continue

            cached_artifacts = ctx.asr_artifact_cache.get(cache_key)
            if cached_artifacts is not None:
                try:
                    _store_asr_artifacts(ep, cached_artifacts, recipe)
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
            if dispatcher is not None and dispatcher.dispatch_enabled:
                work_class = "transcript-align" if align_text else "transcript-asr"
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
                        task="align" if align_text else "transcribe",
                        inputs={
                            "audio_url": ep.hosted_audio_url,
                            "audio_key": ep.audio_key,
                            "language": city.asr_language or None,
                            "model": city.asr_model,
                            "compute_type": city.asr_compute_type,
                            "beam_size": city.asr_beam_size if not align_text else None,
                            "initial_prompt": initial_prompt,
                            "text": align_text,
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
                    _store_asr_artifacts(ep, cached_artifacts, recipe)
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
            mode = "align" if align_text else "transcribe"
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
            _release_abandoned_asr_slot = threading.Event()
            _release_abandoned_native_gate = threading.Event()

            # Bind per-iteration values as default args so the closure captures their
            # current values, not a reference that may change in future loop iterations
            # (ruff B023).  The thread calls _infer() with no positional args.
            def _infer(
                _ep=ep,
                _at=align_text,
                _ep_ref=ep_ref,
                _audio=audio_path,
                _audio_tmp=audio_tmp,
                _result=_artifacts,
                _errors=_err,
                _was_aligned=_aligned,
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
                                            "text": _at,
                                            "model": _asr_model,
                                            "language": city.asr_language or None,
                                            "cpu_threads": cpu_threads,
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
            # Publish before releasing the ASR slot so every follower observes completed bytes.
            ctx.asr_artifact_cache.complete(cache_key, artifacts)
            if sem is not None:
                sem.release()
                sem = None
            if native_gate_acquired and ctx.native_work_gate is not None:
                ctx.native_work_gate.release(kind="asr")
                native_gate_acquired = False
            audio_tmp.cleanup()

            try:
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
            city.max_episodes,
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
    ]


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
    "diarize": frozenset({"diarize"}),
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
        if not quiet:
            print(
                f"[enrich] stage start slug={city.slug} provider={city.provider} "
                f"stage={stage.name} episodes={len(episodes)}",
                flush=True,
            )
        t0 = time.perf_counter()
        stat = stage.process(provider, city, episodes, ctx)
        stat.seconds = time.perf_counter() - t0
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
