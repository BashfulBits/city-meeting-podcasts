"""Persistent per-episode records: stable identity, split invalidation hashes, and the
source-level store that backs feed rendering.

Why this exists (see project memory, "episode-record / identity refactor"):

  * **Stable identity.** The RSS ``<guid>`` must not change when a city migrates providers
    (Granicus<->Swagit), or every subscriber re-downloads the back catalog. ``episode_uid``
    is derived from real-world facts (author + body + date), not the provider's volatile id.

  * **Split invalidation.** ``audio_spec_hash`` covers everything that determines the audio
    *bytes* (source identity + codec/bitrate + chapters + pipeline version); a change re-encodes.
    ``feed_content_hash`` covers everything in the RSS item (notes/summary/links/duration +
    template fingerprint); a change only re-renders. So a new summary re-renders without
    re-encoding, while added chapters do both — each gated independently.

  * **Content-addressed audio.** The object key embeds ``audio_spec_hash`` so the URL changes
    only when the bytes would, giving cache-busting, rollback, and orphan detection for free.

  * **Persistence.** Derived artifacts (audio URL, transcript, summary, chapters) are expensive
    and live in ``state/sources/<source_key>/episodes.json`` so they are computed once per
    meeting and reused across the combined feed and every per-board feed of that source.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from citypods.bodies import body_key, canonical_body
from citypods.models import City, Episode
from citypods.timeline import Segment, SourceMedia, Timeline, timeline_digest

SCHEMA_VERSION = 2
# Bump to force every audio file to be regenerated (e.g. a codec/loudness policy change that
# isn't otherwise captured by the per-episode spec inputs below).
AUDIO_PIPELINE_VERSION = "1"

# Exponential backoff for repeatedly-failing materializations (issue #120): a source whose audio
# won't resolve (e.g. a Swagit meeting with no usable media) must stop being re-tried every run,
# or it churns the run's time + budget forever. Wait ``BACKOFF_BASE * 2**(attempts-1)``, capped at
# ``BACKOFF_MAX``, before re-attempting. A successful host resets the counter.
BACKOFF_BASE = timedelta(days=1)
BACKOFF_MAX = timedelta(days=30)
TRANSCRIPT_TIMEOUT_BACKOFF_BASE = timedelta(days=1)
TRANSCRIPT_TIMEOUT_BACKOFF_MAX = timedelta(days=30)


def _in_backoff(ep: Episode, now: datetime) -> bool:
    """True if ``ep`` failed recently enough to still be inside its materialization backoff."""
    if ep.materialize_attempts <= 0 or not ep.materialize_last_attempt:
        return False
    try:
        last = datetime.fromisoformat(ep.materialize_last_attempt)
    except ValueError:
        return False
    delay = min(BACKOFF_MAX, BACKOFF_BASE * 2 ** (ep.materialize_attempts - 1))
    return now < last + delay


def transcript_timeout_backoff_until(ep: Episode) -> datetime | None:
    """When this episode's local-ASR timeout backoff expires, or ``None`` if not backing off."""
    if ep.transcript_timeout_attempts <= 0 or not ep.transcript_timeout_last_attempt:
        return None
    try:
        last = datetime.fromisoformat(ep.transcript_timeout_last_attempt)
    except ValueError:
        return None
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    else:
        last = last.astimezone(UTC)
    delay = min(
        TRANSCRIPT_TIMEOUT_BACKOFF_MAX,
        TRANSCRIPT_TIMEOUT_BACKOFF_BASE * 2 ** (ep.transcript_timeout_attempts - 1),
    )
    return last + delay


def source_key(city: City) -> str:
    """Stable id for a city's media source, ignoring the per-board ``body`` filter, so the
    combined feed and every per-board feed of one city share one record store + audio object."""
    src = {k: v for k, v in city.source.items() if k != "body"}
    raw = f"{city.provider}|{json.dumps(src, sort_keys=True)}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def shard_assignment(
    source_keys: Iterable[str], num_shards: int, *, weights: Mapping[str, float] | None = None
) -> dict[str, int]:
    """Deterministic, source-atomic shard assignment for H6b audio/ASR workflows.

    The unit of ownership is still the distinct ``source_key``: a source goes to exactly one shard,
    so concurrent shards never write the same ``state/sources/<key>/episodes.json`` file. Within
    that constraint, assign heavier sources first to the currently-lightest shard. When all weights
    are equal or omitted this naturally falls back to balanced source counts, so no shard is empty
    until ``#sources < num_shards``.

    ``weights`` are advisory estimates of source work, not durable identity. ``run.py`` currently
    passes each source's *remaining* audio backlog (episodes still needing an encode under the
    current spec, via ``pending_audio_work``), falling back to ``1.0`` for a never-crawled source
    whose backlog is unknown — so a source that's mostly caught up doesn't keep crowding out one
    with a real backlog, and every matrix job still computes the same partition from local state
    even if a sibling shard pushes state while this workflow is running.

    Tradeoff vs. hash-mod: adding a source or changing weights can move keys to different shards.
    Harmless — records are keyed by ``source_key`` (not by shard) and the push is scoped per
    ``sources/<key>/``, so a reshuffle only changes which shard refreshes a record next run; no
    record is lost or clobbered."""
    if num_shards < 1:
        raise ValueError(f"num_shards must be >= 1, got {num_shards}")

    distinct = sorted(set(source_keys))
    if not distinct:
        return {}

    def _weight(key: str) -> float:
        raw = 1.0 if weights is None else weights.get(key, 1.0)
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"shard weight for {key!r} must be numeric, got {raw!r}") from exc
        if value < 0:
            raise ValueError(f"shard weight for {key!r} must be >= 0, got {value}")
        return value

    ordered = sorted(distinct, key=lambda key: (-_weight(key), key))
    loads = [0.0] * num_shards
    counts = [0] * num_shards
    assignment: dict[str, int] = {}
    for key in ordered:
        shard = min(range(num_shards), key=lambda i: (loads[i], counts[i], i))
        assignment[key] = shard
        loads[shard] += _weight(key)
        counts[shard] += 1
    return assignment


def _author_key(city: City) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (city.podcast_author or city.slug or "").lower())
    return re.sub(r"-+", "-", slug).strip("-")


def _uid(author: str, body: str | None, date: str, seq: int) -> str:
    key = body_key(canonical_body(body or ""))
    raw = f"{author}|{key}|{date}|{seq}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def assign_uids(city: City, episodes: list[Episode]) -> None:
    """Assign each episode a provider-independent ``uid``. Episodes that share (body, date)
    — e.g. a morning and an evening session — are disambiguated by a stable sequence ordered
    by publish time, so the uid survives a provider change as long as both meetings do."""
    author = _author_key(city)
    buckets: dict[tuple[str, str], list[Episode]] = {}
    for ep in episodes:
        k = (body_key(canonical_body(ep.body or "")), ep.published.date().isoformat())
        buckets.setdefault(k, []).append(ep)
    for (_, date), eps in buckets.items():
        for seq, ep in enumerate(sorted(eps, key=lambda e: e.published)):
            ep.uid = _uid(author, ep.body, date, seq)


def audio_spec_hash(
    ep: Episode,
    *,
    max_kbps: int,
    loudness_profile: str = "",
    processing_profile: str = "",
) -> str:
    """Hash of everything that determines the audio bytes.

    **Identity path (v1-compatible):** when no timeline manipulation, rebuild nonce, or
    loudness/audio-processing profile is active, and the episode has at most one source, the
    spec dict is byte-identical to the v1 format — same JSON → same SHA1 → no re-encode storm
    when this model first ships. Only episodes that are *actually* manipulated (non-identity
    timeline, nonce stamped, multi-source concat, speech processing) get the new format and key.

    **v2 format** (all other cases): adds ``timeline``, ``loudness``, ``processing``,
    ``sources``, ``rebuild`` fields. New fields are included at their defaults (``""``, ``[]``)
    so future features that set them only re-encode the episodes they actually affect.

    Note: the HLS *resolved* URL is tokenized/expiring and is deliberately excluded.
    Identity-equivalence intentionally keys the v1 path on ``ep.video_url`` (the stable
    source handle today), **not** on ``SourceMedia.ref`` — so once ``TimelineStage`` starts
    registering a single identity source, the hash stays byte-identical and no re-encode
    storm occurs. Do not "fix" this to read ``ref``: it would change every identity hash.
    """
    tl_digest = timeline_digest(ep.timeline) if ep.timeline is not None else ""
    loudness = loudness_profile
    processing = processing_profile
    rebuild = ep.audio_rebuild or ""

    if not tl_digest and not rebuild and not loudness and not processing and len(ep.sources) <= 1:
        # v1-compatible format: byte-identical for identity episodes.
        spec = {
            "v": AUDIO_PIPELINE_VERSION,
            "source": ep.video_url,
            "max_kbps": max_kbps,
            "chapters": ep.chapters,
        }
    else:
        source_refs = [s.ref for s in ep.sources] if ep.sources else [ep.video_url]
        spec = {
            "v": AUDIO_PIPELINE_VERSION,
            "max_kbps": max_kbps,
            "timeline": tl_digest,
            "loudness": loudness,
            "processing": processing,
            "chapters": ep.chapters,
            "sources": source_refs,
            "rebuild": rebuild,
        }

    blob = json.dumps(spec, separators=(",", ":"), sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def audio_object_key(city: City, ep: Episode, spec: str) -> str:
    """Content-addressed storage key: changes iff the audio spec changes."""
    return f"{city.provider}/{source_key(city)}/{ep.uid}-{spec}.m4a"


def feed_content_hash(episodes: list[Episode], fingerprint: str) -> str:
    """Hash of the render-relevant fields of the (filtered+capped) feed. Drives the
    re-render skip. Includes notes/summary/links/chapters so an enrichment change re-renders.

    Note: adding a field here (e.g. the v2 ``chapters_basis`` / ``audio_duration_served``)
    changes every feed's hash once, so the first deploy after this lands re-renders the whole
    catalog. That's a cheap render-phase pass (not a re-encode) — expected, like a
    template-fingerprint bump, not a regression."""
    payload = [
        [
            e.uid,
            e.title,
            e.published.isoformat(),
            e.description,
            e.summary,
            e.transcript_hosted_url,
            e.transcript_synced,
            e.transcript_basis,
            sorted((e.links or {}).items()),
            e.chapters,
            e.chapters_basis,
            e.duration,
            e.audio_duration_served,
            e.hosted_audio_url,
            e.video_url,
            e.media_kind,
        ]
        for e in sorted(episodes, key=lambda e: e.uid or "")
    ]
    blob = json.dumps([fingerprint, payload], separators=(",", ":"), sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


# --- the store -------------------------------------------------------------------------


def records_path(state_dir: Path, src_key: str) -> Path:
    return Path(state_dir) / "sources" / src_key / "episodes.json"


def load_records(state_dir: Path, src_key: str) -> dict:
    path = records_path(state_dir, src_key)
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    return data.get("episodes", {}) if isinstance(data, dict) else {}


def save_records(state_dir: Path, src_key: str, records: dict) -> None:
    path = records_path(state_dir, src_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    envelope = {"schema_version": SCHEMA_VERSION, "episodes": records}
    path.write_text(json.dumps(envelope, indent=2, sort_keys=True) + "\n")


def pending_audio_work(
    state_dir: Path,
    src_key: str,
    *,
    extract_audio: bool,
    max_kbps: int,
    loudness_profile: str,
    processing_profile: str,
    now: datetime | None = None,
) -> int:
    """Count this source's episodes that still need an audio encode, for sharding weight.

    Mirrors only the local half of ``media.py``'s cheap-pass reuse check (spec match +
    ``hosted_audio_url`` set + not backing off) — it deliberately skips that check's
    ``storage.exists()`` probe so this stays I/O-free at shard-assignment time. An episode
    that already has a matching, hosted spec is "finished" and shouldn't inflate the weight
    of a source that's actually almost caught up; one that's in backoff won't be retried this
    run either, so it's not pending work right now.
    """
    now = now or datetime.now(UTC)
    pending = 0
    for rec in load_records(state_dir, src_key).values():
        ep = record_to_episode(rec)
        if ep.media_kind != "hls" and not extract_audio:
            continue
        spec = audio_spec_hash(
            ep,
            max_kbps=max_kbps,
            loudness_profile=loudness_profile,
            processing_profile=processing_profile,
        )
        legacy_ok = ep.audio_spec_hash == "legacy" and not (loudness_profile or processing_profile)
        spec_ok = bool(ep.hosted_audio_url) and (ep.audio_spec_hash == spec or legacy_ok)
        if spec_ok:
            continue
        if _in_backoff(ep, now):
            continue
        pending += 1
    return pending


TranscribeRoute = Literal["local", "dispatch", "blocked", "inflight"]
TranscribeRouteClassifier = Callable[[Episode, float | None], TranscribeRoute]

# Shard weights use cost-seconds as a common proxy. Local work uses recording duration because it
# dominates runner occupancy; external dispatch and blocked inspection are cheap control-plane work.
# These constants keep those items represented without letting a 10-hour external-only meeting
# distort the balance of GitHub runners that will not transcribe it.
TRANSCRIBE_DISPATCH_WEIGHT_SECONDS = 60.0
TRANSCRIBE_BLOCKED_WEIGHT_SECONDS = 1.0
TRANSCRIBE_UNKNOWN_LOCAL_WEIGHT_SECONDS = 2 * 3600.0


@dataclasses.dataclass(frozen=True)
class TranscribeShardWork:
    """Routing-aware work estimate for one source in the transcribe lane.

    ``local_seconds`` is the recording-duration proxy handled synchronously on the shard.
    External jobs contribute a small fixed dispatch cost, blocked jobs a minimal scan/defer cost,
    and already-in-flight jobs no new work. H14's canonical planner can provide a route classifier
    based on one budget/capacity snapshot; until real external adapters exist, the default
    classifier treats recordings above the local duration ceiling as blocked.
    """

    local_seconds: float = 0.0
    local_items: int = 0
    dispatch_items: int = 0
    blocked_items: int = 0
    inflight_items: int = 0

    def shard_weight(
        self,
        *,
        dispatch_seconds: float = TRANSCRIBE_DISPATCH_WEIGHT_SECONDS,
        blocked_seconds: float = TRANSCRIBE_BLOCKED_WEIGHT_SECONDS,
    ) -> float:
        return (
            self.local_seconds
            + self.dispatch_items * dispatch_seconds
            + self.blocked_items * blocked_seconds
        )


def estimate_transcribe_shard_work(
    state_dir: Path,
    src_key: str,
    *,
    asr_enabled: bool,
    asr_pipeline_version: str,
    local_max_duration_hours: float,
    route_classifier: TranscribeRouteClassifier | None = None,
    unknown_local_seconds: float = TRANSCRIBE_UNKNOWN_LOCAL_WEIGHT_SECONDS,
) -> TranscribeShardWork:
    """Estimate routing-aware transcribe work for source-atomic shard assignment.

    The default route model matches current production: locally eligible work is weighted by audio
    duration, while known recordings above ``local_max_duration_hours`` contribute only a minimal
    blocked/defer cost. A genuinely unknown duration gets a conservative local estimate because the
    current stage may still attempt it after probing hosted audio.

    H14 supplies ``route_classifier`` from a single canonical planner snapshot. It may classify an
    item as ``dispatch`` (small submit/reconcile cost), ``local`` (duration-weighted fallback),
    ``blocked`` (cheap defer), or ``inflight`` (no new shard work). Keeping routing outside this
    helper prevents four matrix jobs from independently guessing against changing GPU budgets.

    Mirrors only the local, I/O-free half of ``TranscriptStage.process``'s ``lane="transcribe"``
    reuse check (the audio-readiness gate + the synced/stale-ASR-version check): it skips that
    stage's ``storage.exists()`` probes and provider-transcript fetch, the same tradeoff
    ``pending_audio_work`` makes for the audio lane. One known overestimate follows from skipping
    the fetch: an episode with an unfetched provider transcript URL that *would* resolve to an
    already-timed transcript (skipping ASR entirely) still counts as pending here. Advisory only,
    same as ``pending_audio_work`` — it sizes a shard, it doesn't gate what that shard transcribes.
    An episode without hosted audio yet contributes nothing: it isn't transcribable this run
    regardless of lane.
    """
    local_seconds = 0.0
    local_items = 0
    unknown_local_items = 0
    dispatch_items = 0
    blocked_items = 0
    inflight_items = 0
    for _uid, route, duration_seconds in _iter_transcribe_routes(
        state_dir,
        src_key,
        asr_enabled=asr_enabled,
        asr_pipeline_version=asr_pipeline_version,
        local_max_duration_hours=local_max_duration_hours,
        route_classifier=route_classifier,
    ):
        if route == "local":
            local_items += 1
            if duration_seconds is not None:
                local_seconds += duration_seconds
            else:
                unknown_local_items += 1
        elif route == "dispatch":
            dispatch_items += 1
        elif route == "blocked":
            blocked_items += 1
        elif route == "inflight":
            inflight_items += 1

    # Weight unknown-duration local items by the average *known* local duration in THIS source,
    # not a flat per-item ceiling. A source with many not-yet-probed episodes (a large backfill, or
    # audio whose duration never got recorded) would otherwise accumulate a multi-thousand-hour
    # weight from the fallback alone and pin its whole, unsplittable backlog to one shard (run #25:
    # one source estimated at ~3,550h). ``unknown_local_seconds`` only applies when the source has
    # no known duration to average against.
    if unknown_local_items:
        known_local_items = local_items - unknown_local_items
        per_item = (
            local_seconds / known_local_items
            if known_local_items
            else max(0.0, unknown_local_seconds)
        )
        local_seconds += unknown_local_items * per_item

    return TranscribeShardWork(
        local_seconds=local_seconds,
        local_items=local_items,
        dispatch_items=dispatch_items,
        blocked_items=blocked_items,
        inflight_items=inflight_items,
    )


def _iter_transcribe_routes(
    state_dir: Path,
    src_key: str,
    *,
    asr_enabled: bool,
    asr_pipeline_version: str,
    local_max_duration_hours: float,
    route_classifier: TranscribeRouteClassifier | None = None,
) -> Iterator[tuple[str, TranscribeRoute, float | None]]:
    """Yield ``(uid, route, duration_seconds)`` for each record that is transcribe **work this
    run** — the shared classification behind the source-atomic estimate
    (:func:`estimate_transcribe_shard_work`) and the per-episode plan
    (:func:`pending_transcribe_items`), so both agree on exactly which uids are pending and why.

    Skips (yields nothing for) records that are not transcribable this run: no hosted audio yet, a
    failed materialization (the audio lane re-encodes it), or a transcript already synced at the
    current ASR pipeline version. ``inflight`` is yielded but represents no new shard work.
    """
    if not asr_enabled:
        return
    now = datetime.now(UTC)
    for uid, rec in load_records(state_dir, src_key).items():
        ep = record_to_episode(rec)
        if not (ep.audio_key and ep.audio_spec_hash and ep.hosted_audio_url):
            continue
        # Audio that failed to materialize isn't transcribable this run — the transcribe stage
        # skips it and the audio lane re-encodes it. Counting it here would re-inflate the shard
        # weight (and the backlog) with work ASR will never do, the exact distortion run #25 hit.
        if ep.materialize_error:
            continue
        if ep.transcript_synced:
            key_name = (ep.transcript_key or "").rsplit("/", 1)[-1]
            is_stale_asr = (
                key_name.startswith(f"{ep.uid or ep.guid}-asr-")
                and ep.transcript_pipeline_version != asr_pipeline_version
            )
            if not is_stale_asr:
                continue
        timeout_backoff_until = transcript_timeout_backoff_until(ep)
        if timeout_backoff_until is not None and now < timeout_backoff_until:
            yield uid, "blocked", None
            continue
        raw_duration = ep.audio_duration_served or ep.duration
        duration_seconds = float(raw_duration) if raw_duration and raw_duration > 0 else None
        if route_classifier is not None:
            route = route_classifier(ep, duration_seconds)
        elif (
            duration_seconds is not None
            and local_max_duration_hours > 0
            and duration_seconds > local_max_duration_hours * 3600
        ):
            route = "blocked"
        else:
            route = "local"
        if route not in ("local", "dispatch", "blocked", "inflight"):
            raise ValueError(f"unknown transcribe shard route {route!r}")
        yield uid, route, duration_seconds


def pending_transcribe_items(
    state_dir: Path,
    src_key: str,
    *,
    asr_enabled: bool,
    asr_pipeline_version: str,
    local_max_duration_hours: float,
    route_classifier: TranscribeRouteClassifier | None = None,
    unknown_local_seconds: float = TRANSCRIBE_UNKNOWN_LOCAL_WEIGHT_SECONDS,
) -> list[tuple[str, float]]:
    """Per-episode transcribe backlog: ``[(uid, weight_seconds), ...]`` (review/18 §3.1).

    The per-episode counterpart of :func:`estimate_transcribe_shard_work`: instead of one aggregate
    weight per source it emits one entry per **pending** uid, so the transcribe planner can spread a
    single skewed source across all shards. ``inflight`` items contribute no new work and are
    omitted; ``local``/``dispatch``/``blocked`` items carry the same cost-second proxy the aggregate
    uses, so ``sum(weights) == estimate_transcribe_shard_work(...).shard_weight()``. Plan size
    tracks *backlog*, not catalog: a caught-up source contributes no entries.
    """
    routes = list(
        _iter_transcribe_routes(
            state_dir,
            src_key,
            asr_enabled=asr_enabled,
            asr_pipeline_version=asr_pipeline_version,
            local_max_duration_hours=local_max_duration_hours,
            route_classifier=route_classifier,
        )
    )
    # Resolve unknown-duration local weight to the per-source average of known local durations
    # (matching the aggregate estimator), so per-uid weights sum to the source's shard weight.
    known = [d for _u, r, d in routes if r == "local" and d is not None]
    unknown_per_item = sum(known) / len(known) if known else max(0.0, unknown_local_seconds)
    items: list[tuple[str, float]] = []
    for uid, route, duration_seconds in routes:
        if route == "inflight":
            continue
        if route == "dispatch":
            weight = TRANSCRIBE_DISPATCH_WEIGHT_SECONDS
        elif route == "blocked":
            weight = TRANSCRIBE_BLOCKED_WEIGHT_SECONDS
        elif duration_seconds is not None:
            weight = duration_seconds
        else:
            weight = unknown_per_item
        items.append((uid, weight))
    return items


def referenced_audio_keys(state_dir: Path) -> set[str]:
    """Every *managed* object key currently referenced by any source's records — the live set
    an orphan GC keeps; anything in storage outside this set is a candidate for deletion.

    Includes both the per-episode **audio** key and the **transcript** key. Transcripts are
    content-addressed objects too (``transcripts/<src>/<uid>-<spec>.<fmt>``, written by
    TranscriptStage), so they MUST be in the live set or ``scripts/gc_audio.py`` — which by
    default sweeps every object under the bucket — would reap live hosted transcripts the first
    time it runs with ``--apply``. (Clip objects are not produced yet; when soundbites land they
    should either be added here or given an ephemeral/derivable GC policy of their own.)

    The name is kept for its callers (gc_audio, report, statesync); read it as
    "referenced object keys.\""""
    keys: set[str] = set()
    for path in Path(state_dir).glob("sources/*/episodes.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        for rec in (data.get("episodes") or {}).values():
            audio_key = (rec.get("audio") or {}).get("key")
            if audio_key:
                keys.add(audio_key)
            transcript = rec.get("transcript") or {}
            transcript_key = transcript.get("key")
            if transcript_key:
                keys.add(transcript_key)
            words_key = transcript.get("words_key")
            if words_key:
                keys.add(words_key)
    return keys


def episode_to_record(ep: Episode) -> dict:
    return {
        "uid": ep.uid,
        "provider_guid": ep.guid,
        "title": ep.title,
        "published": ep.published.isoformat(),
        "body": ep.body,
        "media_kind": ep.media_kind,
        "video_url": ep.video_url,
        "duration": ep.duration,
        "links": ep.links,
        "chapters": ep.chapters,
        "chapters_basis": ep.chapters_basis,
        "summary": ep.summary,
        # v2 transcript block (INFRA-8): replaces old transcript_url (external link).
        # External provider transcript links remain in ep.links["transcript"].
        "transcript": {
            "key": ep.transcript_key,
            "url": ep.transcript_hosted_url,
            "spec_hash": ep.transcript_spec_hash,
            "format": ep.transcript_format,
            "basis": ep.transcript_basis,
            "synced": ep.transcript_synced,
            "words_key": ep.transcript_words_key,
            "words_url": ep.transcript_words_url,
            "pipeline_version": ep.transcript_pipeline_version,
            "timeout_attempts": ep.transcript_timeout_attempts,
            "timeout_last_attempt": ep.transcript_timeout_last_attempt,
        }
        if ep.transcript_key or ep.transcript_timeout_attempts
        else None,
        # v2: source-media registry and timeline EDL (omitted when empty/identity).
        "sources": [dataclasses.asdict(s) for s in ep.sources] if ep.sources else [],
        "timeline": dataclasses.asdict(ep.timeline) if ep.timeline is not None else None,
        "audio": {
            "key": ep.audio_key,
            "url": ep.hosted_audio_url,
            "spec_hash": ep.audio_spec_hash,
            "bytes": ep.audio_bytes,
            "encode_time": ep.audio_encode_time,
            "duration_served": ep.audio_duration_served,
            "rebuild": ep.audio_rebuild or None,  # omit when empty to keep records clean
            # Materialization backoff state (#120): persisted so failures back off across runs.
            "attempts": ep.materialize_attempts,
            "last_attempt": ep.materialize_last_attempt,
            "error": ep.materialize_error,
        },
    }


def _coerce_non_negative_int(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _transcript_fields_from_rec(rec: dict) -> dict:
    """Extract transcript artifact fields from a v2 record.  Returns empty-value dict for v1
    records (where the old ``transcript_url`` field is silently dropped — those transcripts
    will be re-scraped by TranscriptStage on the next enrich run)."""
    t = rec.get("transcript") or {}
    if not isinstance(t, dict):
        return {}
    return {
        "transcript_key": t.get("key"),
        "transcript_hosted_url": t.get("url"),
        "transcript_spec_hash": t.get("spec_hash"),
        "transcript_format": t.get("format"),
        "transcript_basis": t.get("basis", "source:s0"),
        "transcript_synced": bool(t.get("synced", False)),
        "transcript_words_key": t.get("words_key"),
        "transcript_words_url": t.get("words_url"),
        "transcript_pipeline_version": t.get("pipeline_version"),
        "transcript_timeout_attempts": _coerce_non_negative_int(t.get("timeout_attempts")),
        "transcript_timeout_last_attempt": t.get("timeout_last_attempt"),
    }


def _source_media_from_dict(d: dict) -> SourceMedia:
    known = {f.name for f in dataclasses.fields(SourceMedia)}
    return SourceMedia(**{k: v for k, v in d.items() if k in known})


def _timeline_from_dict(d: dict) -> Timeline:
    return Timeline(
        version=d["version"],
        segments=tuple(Segment(**s) for s in d.get("segments", [])),
        basis=d.get("basis", "served"),
    )


def record_to_episode(rec: dict) -> Episode:
    """Rebuild an :class:`Episode` from a stored record — the inverse of
    :func:`episode_to_record`. Used to render feeds from the *full* append-only archive,
    including episodes that have dropped out of the provider's current window (Granicus
    100-item cap, Swagit windowing) and so are no longer in a fresh fetch.

    Handles lazy v1→v2 schema upgrade: v1 records lack ``sources``, ``timeline``, and
    ``chapters_basis``; they default to empty/identity/source:s0 which preserves existing
    behaviour until a stage enriches the episode and re-persists it as v2.
    """
    published = rec.get("published")
    when = datetime.fromisoformat(published) if published else datetime.now(UTC)
    audio = rec.get("audio") or {}

    sources_data = rec.get("sources") or []
    sources = [_source_media_from_dict(s) for s in sources_data]

    tl_data = rec.get("timeline")
    timeline = _timeline_from_dict(tl_data) if tl_data else None

    return Episode(
        guid=rec.get("provider_guid") or "",
        title=rec.get("title") or "",
        published=when,
        video_url=rec.get("video_url") or "",
        duration=rec.get("duration"),
        media_kind=rec.get("media_kind") or "direct",
        body=rec.get("body"),
        uid=rec.get("uid"),
        hosted_audio_url=audio.get("url"),
        audio_key=audio.get("key"),
        audio_spec_hash=audio.get("spec_hash"),
        materialize_attempts=audio.get("attempts") or 0,
        materialize_last_attempt=audio.get("last_attempt"),
        materialize_error=audio.get("error"),
        audio_bytes=audio.get("bytes"),
        links=rec.get("links") or {},
        chapters=rec.get("chapters") or [],
        summary=rec.get("summary") or "",
        # v2 transcript block (INFRA-8); v1 records with old transcript_url silently dropped.
        **_transcript_fields_from_rec(rec),
        # v2 fields (default to identity/empty for v1 records — lazy upgrade)
        sources=sources,
        timeline=timeline,
        chapters_basis=rec.get("chapters_basis", "source:s0"),
        audio_rebuild=audio.get("rebuild") or "",
        audio_encode_time=audio.get("encode_time"),
        audio_duration_served=audio.get("duration_served"),
    )


def merge_records(persisted: dict, fresh: dict) -> dict:
    """Append-only merge of the record store: keep every previously-known episode and let a
    freshly-fetched record win on a uid collision (fresh provider fields + re-enriched
    artifacts are authoritative). This is what stops content that left the provider window
    from being silently dropped — the core of issue #109."""
    return {**persisted, **fresh}


# --- cross-lane write isolation (review/12 §H6) ----------------------------------------
#
# The expensive, independently-owned, content-addressed derived artifacts that live as their own
# block inside an episode record. Each block is produced by exactly one enrich *lane* (the H6b
# sharded ``audio`` / ``transcribe`` / ``align`` workflows). Two lanes can touch the SAME
# ``state/sources/<key>/episodes.json`` at overlapping read→write windows (the audio cron and the
# ASR cron run on different schedules), so a lane that pulled state before a sibling lane's write
# would, on its own whole-record push, silently regress the sibling's block (an ASR run finishing
# after an audio run re-uploads its start-of-run audio block, erasing freshly hosted audio). The
# foreign-block-preserving push (``statesync.push_records_merged``) prevents that by re-reading the
# freshest remote and keeping the blocks the running lane does not own.
#
# **Extensibility (diarization — review/12 §H5 diarization-forward schema).** When the reserved
# ``diarization`` lane lands and writes a ``speakers`` block, add it to ``ARTIFACT_BLOCKS`` and map
# ``"diarize": frozenset({"speakers"})`` in ``_LANE_OWNED_BLOCKS``. The merge/push logic is
# block-set-driven and needs no other change; ``episode_to_record`` / ``record_to_episode`` /
# ``referenced_audio_keys`` gain the new block alongside the existing ``audio`` / ``transcript``.
ARTIFACT_BLOCKS: frozenset[str] = frozenset({"audio", "transcript"})

# Which artifact block(s) each lane writes authoritatively. A lane absent here (e.g. ``None`` — a
# full unsharded enrich or a manual single-source run that runs *every* stage) owns everything, so
# it preserves nothing (behaves like the legacy whole-record push).
_LANE_OWNED_BLOCKS: dict[str, frozenset[str]] = {
    "audio": frozenset({"audio"}),
    "transcribe": frozenset({"transcript"}),
    "align": frozenset({"transcript"}),
}


def protected_blocks_for_lane(lane: str | None) -> frozenset[str]:
    """Artifact blocks a scoped ``lane`` run must PRESERVE from the freshest remote because it does
    not own them — everything else (its owned artifact plus the re-fetched provider/render fields)
    is written fresh. ``None``/unknown lane owns every artifact, so nothing is protected."""
    owned = _LANE_OWNED_BLOCKS.get(lane, ARTIFACT_BLOCKS) if lane is not None else ARTIFACT_BLOCKS
    return ARTIFACT_BLOCKS - owned


def merge_preserving_foreign(
    remote: dict,
    local: dict,
    protected: frozenset[str],
    *,
    owned_uids: frozenset[str] | None = None,
) -> dict:
    """Merge a scoped lane run's ``local`` records to push against the freshest ``remote`` snapshot,
    preserving the ``protected`` artifact blocks (the ones this lane does not own) from ``remote``.

    Two preservation axes (review/18 §3.2):

    * **Across blocks** (always): for a uid this run writes, keep ``remote``'s value for each
      ``protected`` block — the artifacts this *lane* does not own (audio vs transcript) — so a
      concurrent sibling *lane* is never regressed (review/12 §H6).
    * **Across uids** (only when ``owned_uids`` is given): a per-episode-sharded transcribe run
      holds the *whole* source in ``local`` (it pulled the full snapshot) but owns only some uids.
      For a uid it does **not** own, its ``local`` artifact block is snapshot-stale — a sibling
      *shard* may have just written a fresh one — so we must never write it. ``owned_uids=None``
      reproduces the source-atomic behavior exactly (the run owns every uid in ``local``), keeping
      audio/align and the unsharded full enrich byte-for-byte unchanged.

    Rules, per uid:
      * uid only in ``remote`` (a sibling discovered/owns it) — kept as-is.
      * uid in ``local`` but **not owned** — keep ``remote`` as-is if present; if it is newly
        discovered (absent from ``remote``), record its provider/render fields but **no artifact
        block** (§2.2 unowned-uid rule — a sibling will own and write it).
      * uid owned (or ``owned_uids is None``) and only in ``local`` — taken whole.
      * uid owned and in both — take ``local`` (fresh provider/render fields + this lane's
        artifact), but for each ``protected`` block keep ``remote``'s value when remote has one;
        when remote lacks the block, local's is kept so a block is never dropped.
    """
    merged = {uid: dict(rec) for uid, rec in remote.items()}
    for uid, local_rec in local.items():
        if owned_uids is not None and uid not in owned_uids:
            # uid this run does NOT own: never write our snapshot-stale artifact for it.
            if uid not in remote:
                # newly discovered, unowned — keep provider/render fields, drop artifact blocks.
                merged[uid] = {k: v for k, v in local_rec.items() if k not in ARTIFACT_BLOCKS}
            # else: keep remote as-is (already copied) — a sibling owns/writes it.
            continue
        rec = dict(local_rec)
        remote_rec = remote.get(uid)
        if remote_rec:
            for block in protected:
                if remote_rec.get(block):
                    rec[block] = remote_rec[block]
        merged[uid] = rec
    return merged


def prune_archive(records: dict, *, max_items: int, max_age_years: float, now=None) -> dict:
    """Bound the otherwise append-only archive: keep the newest ``max_items`` records and drop
    any older than ``max_age_years``. Defaults are set arbitrarily high (see build()), so this
    is a no-op in normal operation — but the lever exists so retention can be ratcheted down
    later (a pruned record's audio key falls out of ``referenced_audio_keys`` and the orphan GC
    reclaims its audio on the usual cycle). Records with an unparseable ``published`` are kept
    (fail safe — never drop content we can't date)."""
    now = now or datetime.now(UTC)
    cutoff = now.timestamp() - max_age_years * 365.25 * 86400

    def _ts(rec: dict) -> float | None:
        published = rec.get("published")
        if not published:
            return None
        try:
            return datetime.fromisoformat(published).timestamp()
        except ValueError:
            return None

    kept = {uid: rec for uid, rec in records.items() if (_ts(rec) is None or _ts(rec) >= cutoff)}
    if len(kept) <= max_items:
        return kept
    # Keep the newest max_items; undated records sort last (kept only if room remains).
    ordered = sorted(
        kept.items(), key=lambda kv: (_ts(kv[1]) is not None, _ts(kv[1]) or 0.0), reverse=True
    )
    return dict(ordered[:max_items])


def merge_persisted(episodes: list[Episode], records: dict) -> None:
    """Attach previously-computed derived artifacts (audio, summary, links, chapters,
    transcript) from the store onto freshly-fetched episodes, matched by uid. Fresh provider
    fields (title/description/published) win; derived fields come from the store."""
    for ep in episodes:
        rec = records.get(ep.uid or "")
        if not rec:
            continue
        audio = rec.get("audio") or {}
        ep.audio_key = audio.get("key")
        ep.hosted_audio_url = audio.get("url")
        ep.audio_spec_hash = audio.get("spec_hash")
        ep.materialize_attempts = audio.get("attempts") or 0
        ep.materialize_last_attempt = audio.get("last_attempt")
        ep.materialize_error = audio.get("error")
        ep.audio_bytes = audio.get("bytes")
        ep.audio_encode_time = audio.get("encode_time")
        ep.audio_duration_served = audio.get("duration_served")
        ep.audio_rebuild = audio.get("rebuild") or ""
        ep.summary = rec.get("summary", ep.summary)
        t = rec.get("transcript") or {}
        if isinstance(t, dict):
            if t.get("key"):
                ep.transcript_key = t.get("key")
                ep.transcript_hosted_url = t.get("url")
                ep.transcript_spec_hash = t.get("spec_hash")
                ep.transcript_format = t.get("format")
                ep.transcript_basis = t.get("basis", "source:s0")
                ep.transcript_synced = bool(t.get("synced", False))
            ep.transcript_timeout_attempts = _coerce_non_negative_int(t.get("timeout_attempts"))
            ep.transcript_timeout_last_attempt = t.get("timeout_last_attempt")
        ep.links = rec.get("links") or ep.links
        ep.chapters = rec.get("chapters") or ep.chapters
        ep.chapters_basis = rec.get("chapters_basis", ep.chapters_basis)
        if rec.get("duration") and not ep.duration:
            ep.duration = rec["duration"]
        # v2: restore sources and timeline from record (lazy upgrade: absent → defaults)
        sources_data = rec.get("sources") or []
        if sources_data and not ep.sources:
            ep.sources = [_source_media_from_dict(s) for s in sources_data]
        tl_data = rec.get("timeline")
        if tl_data and ep.timeline is None:
            ep.timeline = _timeline_from_dict(tl_data)


def migrate_legacy_manifests(state_dir: Path, episodes: list[Episode]) -> int:
    """One-time carry-over from the old per-slug ``audio_manifest.json`` ({guid: {key,url}}):
    seed already-hosted audio onto records by matching the provider guid, so the identity
    refactor doesn't force a full re-encode of audio we already paid to produce. The legacy
    object keeps its old key; ``spec_hash`` is left as ``"legacy"`` (treated as up-to-date
    until a real spec change). Returns how many episodes were seeded."""
    legacy: dict[str, dict] = {}
    for mf in Path(state_dir).glob("*/audio_manifest.json"):
        try:
            legacy.update(json.loads(mf.read_text()))
        except (OSError, ValueError):
            continue
    if not legacy:
        return 0
    seeded = 0
    for ep in episodes:
        if ep.hosted_audio_url:
            continue
        entry = legacy.get(ep.guid)
        if entry and entry.get("url"):
            ep.hosted_audio_url = entry["url"]
            ep.audio_key = entry.get("key")
            ep.audio_spec_hash = "legacy"
            seeded += 1
    return seeded
