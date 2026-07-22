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

import copy
import dataclasses
import hashlib
import json
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from citypods.availability import MediaAvailability
from citypods.bodies import body_key, canonical_body
from citypods.chapters import episode_served_chapters
from citypods.durations import (
    episode_duration_hours,
    episode_served_duration_seconds,
    episode_source_duration_seconds,
    record_served_duration_seconds,
    record_source_duration_seconds,
    set_served_duration_seconds,
    set_source_duration_seconds,
)
from citypods.integrity import (
    REPAIR_AUDIO_REMATERIALIZE,
    REPAIR_TRANSCRIPT_REGENERATE,
    timeline_audio_repair_token,
)
from citypods.models import AgendaRecord, City, Episode
from citypods.providers.base import AgendaSource
from citypods.timeline import Segment, SourceMedia, Timeline, timeline_digest

SCHEMA_VERSION = 2
CALENDAR_SCHEMA_VERSION = 1
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
AGENDA_TEXT_BACKOFF_BASE = timedelta(days=1)
AGENDA_TEXT_BACKOFF_MAX = timedelta(days=14)
AGENDA_BACKUP_BACKOFF_BASE = timedelta(days=1)
AGENDA_BACKUP_BACKOFF_MAX = timedelta(days=14)
MINUTES_TEXT_BACKOFF_BASE = timedelta(days=1)
MINUTES_TEXT_BACKOFF_MAX = timedelta(days=14)

# Once an episode is *confirmed dead* (empty/missing/invalid) it is no longer "failing" — it is a
# known-dead recording we poll occasionally in case the source is restored. Recheck it on a flat
# cadence instead of the exponential #120 backoff, so a re-check that stays dead just sleeps another
# full interval without escalating a cooldown or filing new findings (GH#795). ``suspected_empty``
# stays on the exponential backoff so it can reach its second silent confirmation quickly.
CONFIRMED_DEAD_RECHECK_INTERVAL = timedelta(days=30)

# Audio shard planning uses recording seconds as its common cost proxy. A withheld recording still
# reaches TimelineStage so it can recover, but it does not enter AudioStage; give that cheap recheck
# a small non-zero cost without letting known-empty media dominate a shard. Unknown-duration
# playable work uses the source's own known-duration average, matching the ASR planner's anti-skew
# behavior.
AUDIO_WITHHELD_RECHECK_WEIGHT_SECONDS = 5.0
AUDIO_UNKNOWN_DURATION_WEIGHT_SECONDS = 2 * 3600.0


def _capped_exponential_backoff(
    base: timedelta,
    cap: timedelta,
    attempts: int,
) -> timedelta:
    """Bound exponential backoff before timedelta multiplication can overflow."""
    if attempts <= 0:
        return timedelta(0)
    if base <= timedelta(0) or cap <= base:
        return cap
    base_us = base // timedelta(microseconds=1)
    cap_us = cap // timedelta(microseconds=1)
    multiplier_cap = max(1, (cap_us + base_us - 1) // base_us)
    exponent = attempts - 1
    if exponent >= (multiplier_cap - 1).bit_length():
        return cap
    return min(cap, base * (2**exponent))


def _parse_iso_utc(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp and normalize it to UTC, or ``None`` if absent/unparseable.

    Shared by the backoff/recheck gates (``_in_backoff``, ``confirmed_dead_recheck_due``,
    ``transcript_timeout_backoff_until``) so they cannot drift in how they read persisted times."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def agenda_text_backoff_until(ep: Episode) -> datetime | None:
    stamp = _parse_iso_utc(ep.agenda_text_last_attempt)
    if not stamp or ep.agenda_text_attempts <= 0:
        return None
    return stamp + _capped_exponential_backoff(
        AGENDA_TEXT_BACKOFF_BASE, AGENDA_TEXT_BACKOFF_MAX, ep.agenda_text_attempts
    )


def agenda_backup_backoff_until(ep: Episode) -> datetime | None:
    stamp = _parse_iso_utc(ep.agenda_backup_last_attempt)
    if not stamp or ep.agenda_backup_attempts <= 0:
        return None
    return stamp + _capped_exponential_backoff(
        AGENDA_BACKUP_BACKOFF_BASE, AGENDA_BACKUP_BACKOFF_MAX, ep.agenda_backup_attempts
    )


def minutes_text_backoff_until(ep: Episode) -> datetime | None:
    stamp = _parse_iso_utc(ep.minutes_text_last_attempt)
    if not stamp or ep.minutes_text_attempts <= 0:
        return None
    return stamp + _capped_exponential_backoff(
        MINUTES_TEXT_BACKOFF_BASE, MINUTES_TEXT_BACKOFF_MAX, ep.minutes_text_attempts
    )


def _in_backoff(ep: Episode, now: datetime) -> bool:
    """True if ``ep`` failed recently enough to still be inside its materialization backoff."""
    if ep.materialize_attempts <= 0:
        return False
    last = _parse_iso_utc(ep.materialize_last_attempt)
    if last is None:
        return False
    delay = _capped_exponential_backoff(BACKOFF_BASE, BACKOFF_MAX, ep.materialize_attempts)
    return now < last + delay


def confirmed_dead_recheck_due(ep: Episode, now: datetime) -> bool:
    """True when a confirmed-dead episode is due for its periodic recheck.

    Anchored on the availability verdict's ``last_check`` (stamped/refreshed every time the verdict
    is recorded). A missing or unparseable anchor is treated as due, so a freshly-confirmed episode
    rechecks once and then settles onto the flat cadence. Callers must gate on
    ``media_availability.is_confirmed_dead()`` before consulting this."""
    av = ep.media_availability
    last = _parse_iso_utc(av.last_check if av is not None else None)
    if last is None:
        return True
    return now >= last + CONFIRMED_DEAD_RECHECK_INTERVAL


def transcript_timeout_backoff_until(ep: Episode) -> datetime | None:
    """When this episode's local-ASR timeout backoff expires, or ``None`` if not backing off."""
    if ep.transcript_timeout_attempts <= 0:
        return None
    last = _parse_iso_utc(ep.transcript_timeout_last_attempt)
    if last is None:
        return None
    delay = _capped_exponential_backoff(
        TRANSCRIPT_TIMEOUT_BACKOFF_BASE,
        TRANSCRIPT_TIMEOUT_BACKOFF_MAX,
        ep.transcript_timeout_attempts,
    )
    return last + delay


def source_key(city: City) -> str:
    """Stable id for a city's media source, ignoring the per-board ``body`` filter, so the
    combined feed and every per-board feed of one city share one record store + audio object.

    ``source_id`` is the provider-migration seam: once pinned to the pre-migration key it keeps
    the append-only namespace stable while provider/source transport configuration changes.
    """
    if city.source_id:
        return city.source_id
    src = {k: v for k, v in city.source.items() if k != "body"}
    raw = f"{city.provider}|{json.dumps(src, sort_keys=True)}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def source_family_key(city: City) -> str:
    """Shard family for configured feed views of one government entity.

    The entity reference remains stable when one per-board feed needs a wider ``feed_urls`` set
    than the combined feed. Configurations without an entity retain source-key behavior.
    """
    if city.city_entity:
        raw = f"{city.provider}|entity:{city.city_entity}"
        return hashlib.sha1(raw.encode()).hexdigest()[:12]
    return source_key(city)


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
    passes each source's duration-weighted remaining audio work (playable/unknown episodes still
    needing an encode under the current spec, plus a small fixed cost for withheld-media recovery
    rechecks, via ``estimate_audio_shard_work``), falling back to a conservative unknown-duration
    recording for a never-crawled source whose backlog is unknown. Every matrix job computes the
    same partition from local state even if a sibling shard pushes state while this workflow runs.

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


def assign_uids(city: City, episodes: list[AgendaSource]) -> None:
    """Assign provider-independent UIDs to episode or calendar records.

    Records that share ``(body, date)`` — e.g. a morning and an evening
    session — are disambiguated by a stable sequence ordered by publish time.
    The same algorithm gives a verified calendar row and its matching recording
    the same UID without turning a no-video row into an Episode.
    """
    author = _author_key(city)
    buckets: dict[tuple[str, str], list[AgendaSource]] = {}
    for ep in episodes:
        k = (body_key(canonical_body(ep.body or "")), ep.published.date().isoformat())
        buckets.setdefault(k, []).append(ep)
    for (_, date), eps in buckets.items():
        for seq, ep in enumerate(sorted(eps, key=lambda e: e.published)):
            ep.uid = _uid(author, ep.body, date, seq)

    # Provider migrations occasionally rename a body, shift a meeting date, or reorder same-day
    # sessions.  A reviewed config mapping may bind that replacement-provider GUID to the prior
    # stable UID.  Fail closed if an override would collapse two fetched rows.
    for ep in episodes:
        provider_guid = getattr(ep, "guid", None)
        override = city.uid_overrides.get(provider_guid) if provider_guid else None
        if override:
            ep.uid = override
    by_uid: dict[str, str] = {}
    for ep in episodes:
        if not ep.uid:
            continue
        provider_guid = getattr(ep, "guid", None) or getattr(ep, "video_guid", None) or ep.uid
        prior_guid = by_uid.get(ep.uid)
        if prior_guid is not None and prior_guid != provider_guid:
            raise ValueError(
                f"{city.slug}: provider episodes {prior_guid!r} and {provider_guid!r} resolve to "
                f"the same stable UID {ep.uid!r}"
            )
        by_uid[ep.uid] = provider_guid


def _fill_missing_links(target: dict, source: Mapping[str, object] | None) -> None:
    """Add non-empty official links without replacing the target's primary links."""
    for key, value in (source or {}).items():
        if value:
            target.setdefault(key, value)


def merge_calendar_backfill(episodes: list[Episode], calendar_records: list[AgendaRecord]) -> None:
    """Merge explicitly video-linked calendar rows into the primary episode list.

    The stable Granicus MediaPlayer URL is the join key.  Native archive data
    wins when the same recording appears in both sources, while the calendar's
    additional official links fill gaps.  Only rows carrying both a canonical
    video GUID and a media URL become Episodes; agenda-only rows remain in the
    separate calendar store.
    """
    by_guid = {episode.guid: episode for episode in episodes}
    for record in calendar_records:
        if not record.video_guid or not record.video_url:
            continue
        existing = by_guid.get(record.video_guid)
        if existing is None:
            existing = Episode(
                guid=record.video_guid,
                title=record.title or f"{record.body} Meeting",
                published=record.published,
                video_url=record.video_url,
                body=record.body,
                links=dict(record.links),
            )
            by_guid[existing.guid] = existing
            episodes.append(existing)
            continue
        _fill_missing_links(existing.links, record.links)


def attach_auxiliary_agenda_links(episodes: list[Episode], aux_records: list[AgendaSource]) -> None:
    """Attach official auxiliary links to matching primary episodes in place.

    A video-linked calendar row joins its primary recording by the canonical
    Granicus MediaPlayer GUID first. This remains reliable when a calendar has
    additional same-day sessions (whose local identity sequence differs from
    the recording-only archive) and lets a persisted calendar restore its
    links after a temporary companion outage. Rows without a video identity
    retain the original UID/date-body fallback. Primary links win on key
    collision, so a companion cannot replace the primary provider's canonical
    agenda or video reference.
    """
    by_uid = {record.uid: record for record in aux_records if record.uid}
    by_video_guid = {
        record.video_guid: record
        for record in aux_records
        if isinstance(record, AgendaRecord) and record.video_guid
    }
    for episode in episodes:
        record = by_video_guid.get(episode.guid) or by_uid.get(episode.uid)
        if record is None:
            continue
        _fill_missing_links(episode.links, record.links)


def reconcile_cross_feed_episodes(
    canonical_sources: Iterable[Iterable[Episode]], aggregate: list[Episode]
) -> tuple[list[Episode], list[Episode], int]:
    """Build a public aggregate projection and a complete durable observation archive.

    Swagit archive views can expose the same recording under a dedicated body view and a
    city-wide archive.  The provider GUID is the only safe cross-view join key: body/date
    matching can collapse distinct same-day sessions.  Canonical episodes retain their
    existing UID and content-addressed identity.  Matched observations are retained in the
    complete archive as suppressed copies carrying the canonical UID; the public projection
    contains only recordings not already represented by a canonical feed.
    """
    by_guid: dict[str, Episode] = {}
    for episodes in canonical_sources:
        for episode in episodes:
            by_guid.setdefault(episode.guid, episode)

    unique: list[Episode] = []
    complete: list[Episode] = []
    reconciled = 0
    for episode in aggregate:
        existing = by_guid.get(episode.guid)
        if existing is None:
            unique.append(episode)
            complete.append(episode)
            continue
        _fill_missing_links(existing.links, episode.links)
        observation = copy.deepcopy(existing)
        _fill_missing_links(observation.links, episode.links)
        observation.integrity = dict(observation.integrity or {})
        observation.integrity["aggregate_suppressed"] = True
        complete.append(observation)
        reconciled += 1
    return unique, complete, reconciled


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
    tl_digest = timeline_digest(ep.timeline, ep.sources) if ep.timeline is not None else ""
    loudness = loudness_profile
    processing = processing_profile
    rebuild = ep.audio_rebuild or timeline_audio_repair_token(ep, REPAIR_AUDIO_REMATERIALIZE)

    if not tl_digest and not rebuild and not loudness and not processing and len(ep.sources) <= 1:
        # v1-compatible format: byte-identical for identity episodes.
        spec = {
            "v": AUDIO_PIPELINE_VERSION,
            "source": ep.video_url,
            "max_kbps": max_kbps,
            "chapters": episode_served_chapters(ep),
        }
    else:
        source_refs = [s.ref for s in ep.sources] if ep.sources else [ep.video_url]
        spec = {
            "v": AUDIO_PIPELINE_VERSION,
            "max_kbps": max_kbps,
            "timeline": tl_digest,
            "loudness": loudness,
            "processing": processing,
            "chapters": episode_served_chapters(ep),
            "sources": source_refs,
            "rebuild": rebuild,
        }

    blob = json.dumps(spec, separators=(",", ":"), sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def transcript_media_hash(ep: Episode) -> str:
    """Hash of ASR transcript media inputs, independent of audio mastering bytes."""
    tl_digest = timeline_digest(ep.timeline, ep.sources) if ep.timeline is not None else ""
    source_refs = [s.ref for s in ep.sources] if ep.sources else [ep.video_url]
    spec = {
        "v": 1,
        "sources": source_refs,
        "timeline": tl_digest,
        "repair": timeline_audio_repair_token(ep, REPAIR_TRANSCRIPT_REGENERATE),
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
            e.tags,
            e.transcript_hosted_url,
            e.transcript_synced,
            e.transcript_basis,
            sorted((e.links or {}).items()),
            episode_served_chapters(e),
            e.chapters_basis,
            episode_source_duration_seconds(e),
            episode_served_duration_seconds(e),
            e.hosted_audio_url,
            e.video_url,
            e.media_kind,
        ]
        for e in sorted(episodes, key=lambda e: e.uid or "")
    ]
    blob = json.dumps([fingerprint, payload], separators=(",", ":"), sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def meeting_page_hash(ep: Episode) -> str:
    """Hash of the render-relevant fields for one meeting page.

    Deliberately separate from ``feed_content_hash`` because meeting pages project the retained
    archive, not the capped current feed window.
    """
    payload = {
        "uid": ep.uid,
        "title": ep.title,
        "published": ep.published.isoformat(),
        "description": ep.description,
        "summary": ep.summary,
        "tags": ep.tags,
        "chapter_tags": ep.chapter_tags,
        "links": sorted((ep.links or {}).items()),
        "chapters": episode_served_chapters(ep),
        "chapters_basis": ep.chapters_basis,
        "transcript_url": ep.transcript_hosted_url,
        "transcript_format": ep.transcript_format,
        "transcript_synced": ep.transcript_synced,
        "transcript_basis": ep.transcript_basis,
        "transcript_words_url": ep.transcript_words_url,
        "source_duration": episode_source_duration_seconds(ep),
        "served_duration": episode_served_duration_seconds(ep),
        "hosted_audio_url": ep.hosted_audio_url,
        "video_url": ep.video_url,
        "media_kind": ep.media_kind,
        "media_availability": (
            {
                "state": ep.media_availability.state,
                "effective_state": ep.media_availability.effective_state(),
                "reason": ep.media_availability.reason,
                "operator_override": ep.media_availability.operator_override,
                "operator_reason": ep.media_availability.operator_reason,
                "last_check": ep.media_availability.last_check,
                "recovered_at": ep.media_availability.recovered_at,
                "withheld": ep.media_availability.is_withheld(),
            }
            if ep.media_availability is not None
            else None
        ),
        "timeline": timeline_digest(ep.timeline, ep.sources) if ep.timeline is not None else "",
        "sources": [dataclasses.asdict(source) for source in ep.sources],
    }
    blob = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)
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


def calendar_records_path(state_dir: Path, src_key: str) -> Path:
    """Path of the append-only, non-podcast calendar metadata store for a source."""
    return Path(state_dir) / "sources" / src_key / "calendar.json"


def calendar_record_to_dict(record: AgendaRecord) -> dict:
    """Serialize a non-synthetic calendar row without any media-stage state."""
    return {
        "uid": record.uid,
        "title": record.title,
        "body": record.body,
        "published": record.published.isoformat(),
        "links": dict(record.links or {}),
        "video_guid": record.video_guid,
        "video_url": record.video_url,
    }


def calendar_record_from_dict(record: dict) -> AgendaRecord | None:
    """Rehydrate one durable calendar row; skip malformed rows rather than failing a render."""
    uid = record.get("uid")
    body = record.get("body")
    published = record.get("published")
    if not isinstance(uid, str) or not uid or not isinstance(body, str) or not body:
        return None
    try:
        when = datetime.fromisoformat(str(published))
    except ValueError:
        return None
    return AgendaRecord(
        body=body,
        title=str(record.get("title") or f"{body} Meeting"),
        published=when,
        links=dict(record.get("links") or {}),
        video_guid=record.get("video_guid") or None,
        video_url=record.get("video_url") or None,
        uid=uid,
    )


def load_calendar_records(state_dir: Path, src_key: str) -> dict[str, AgendaRecord]:
    """Load the durable calendar catalog, returning an empty mapping when absent."""
    path = calendar_records_path(state_dir, src_key)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    raw_records = data.get("records", {}) if isinstance(data, dict) else {}
    if not isinstance(raw_records, dict):
        return {}
    records: dict[str, AgendaRecord] = {}
    for key, value in raw_records.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        if record := calendar_record_from_dict(value):
            records[key] = record
    return records


def save_calendar_records(
    state_dir: Path, src_key: str, records: Mapping[str, AgendaRecord]
) -> None:
    """Persist all known calendar rows.  This store intentionally has no retention prune."""
    path = calendar_records_path(state_dir, src_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = {uid: calendar_record_to_dict(record) for uid, record in records.items()}
    envelope = {"schema_version": CALENDAR_SCHEMA_VERSION, "records": serialized}
    path.write_text(json.dumps(envelope, indent=2, sort_keys=True) + "\n")


def merge_calendar_records(
    persisted: Mapping[str, AgendaRecord], fresh: Iterable[AgendaRecord]
) -> dict[str, AgendaRecord]:
    """Append-only merge of the calendar catalog, preserving prior official links.

    Calendar sources can temporarily omit old documents. A fresh row updates its
    title/date/video reference; an empty fresh link never removes a persisted
    one, while a non-empty fresh official URL can correct a previous URL.
    """
    merged = dict(persisted)
    for record in fresh:
        if not record.uid:
            continue
        previous = merged.get(record.uid)
        if previous is None:
            merged[record.uid] = record
            continue
        merged[record.uid] = AgendaRecord(
            body=record.body or previous.body,
            title=record.title or previous.title,
            published=record.published,
            links={
                **previous.links,
                **{key: value for key, value in record.links.items() if value},
            },
            video_guid=record.video_guid or previous.video_guid,
            video_url=record.video_url or previous.video_url,
            uid=record.uid,
        )
    return merged


def estimate_audio_shard_work(
    state_dir: Path,
    src_key: str,
    *,
    extract_audio: bool,
    max_kbps: int,
    loudness_profile: str,
    processing_profile: str,
    now: datetime | None = None,
) -> float:
    """Estimate this source's audio-lane work in recording seconds, for shard assignment.

    Mirrors only the local half of ``media.py``'s cheap-pass reuse check (spec match +
    ``hosted_audio_url`` set + not backing off) — it deliberately skips that check's
    ``storage.exists()`` probe so this stays I/O-free at shard-assignment time. An episode
    that already has a matching, hosted spec is "finished" and shouldn't inflate the weight
    of a source that's actually almost caught up; one that's in backoff won't be retried this
    run either, so it's not pending work right now.

    Pending playable or not-yet-classified media is weighted by its expected served duration:
    the current Timeline when available, then the last hosted served duration, then the provider
    duration. Unknown durations use this source's average known pending duration (or a conservative
    fallback when the whole source is unknown). Availability-withheld media does not enter
    ``AudioStage`` but is deliberately rechecked by ``TimelineStage`` for recovery, so it
    contributes only a small fixed cost instead of its often-misleading declared duration.
    """
    now = now or datetime.now(UTC)
    known_seconds: list[float] = []
    unknown_items = 0
    withheld_items = 0
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
        if spec_ok and not ep.materialize_error:
            continue
        if _in_backoff(ep, now):
            continue
        if ep.media_availability is not None and ep.media_availability.is_withheld():
            withheld_items += 1
            continue

        duration: float | None = None
        if ep.timeline is not None and ep.timeline.segments:
            duration = float(ep.timeline.segments[-1].served_end)
        else:
            duration_hours, source = episode_duration_hours(ep)
            if source != "unknown":
                duration = duration_hours * 3600.0
        if duration is None or duration <= 0:
            unknown_items += 1
        else:
            known_seconds.append(duration)

    unknown_weight = (
        sum(known_seconds) / len(known_seconds)
        if known_seconds
        else AUDIO_UNKNOWN_DURATION_WEIGHT_SECONDS
    )
    return (
        sum(known_seconds)
        + unknown_items * unknown_weight
        + withheld_items * AUDIO_WITHHELD_RECHECK_WEIGHT_SECONDS
    )


TranscribeRoute = Literal["local", "dispatch", "blocked", "inflight", "copy"]
TranscribeRouteClassifier = Callable[[Episode, float | None], TranscribeRoute]
TranscriptStalePredicate = Callable[[Episode], bool]

# Shard weights use cost-seconds as a common proxy. Local work uses recording duration because it
# dominates runner occupancy; external dispatch and blocked inspection are cheap control-plane work.
# These constants keep those items represented without letting a 10-hour external-only meeting
# distort the balance of GitHub runners that will not transcribe it.
TRANSCRIBE_DISPATCH_WEIGHT_SECONDS = 60.0
TRANSCRIBE_BLOCKED_WEIGHT_SECONDS = 1.0
TRANSCRIBE_COPY_WEIGHT_SECONDS = 1.0
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
    copy_items: int = 0

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
            + self.copy_items * TRANSCRIBE_COPY_WEIGHT_SECONDS
        )


def estimate_transcribe_shard_work(
    state_dir: Path,
    src_key: str,
    *,
    asr_enabled: bool,
    asr_pipeline_version: str,
    local_max_duration_hours: float,
    route_classifier: TranscribeRouteClassifier | None = None,
    transcript_stale: TranscriptStalePredicate | None = None,
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
    ``estimate_audio_shard_work`` makes for the audio lane. One known overestimate follows from
    skipping the fetch: an episode with an unfetched provider transcript URL that *would* resolve
    to an already-timed transcript (skipping ASR entirely) still counts as pending here. Advisory
    only, same as ``estimate_audio_shard_work`` — it sizes a shard, it doesn't gate what that shard
    transcribes. An episode without hosted audio yet contributes nothing: it isn't transcribable
    this run regardless of lane.
    """
    local_seconds = 0.0
    local_items = 0
    unknown_local_items = 0
    dispatch_items = 0
    blocked_items = 0
    inflight_items = 0
    copy_items = 0
    for _uid, route, duration_seconds in _iter_transcribe_routes(
        state_dir,
        src_key,
        asr_enabled=asr_enabled,
        asr_pipeline_version=asr_pipeline_version,
        local_max_duration_hours=local_max_duration_hours,
        route_classifier=route_classifier,
        transcript_stale=transcript_stale,
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
        elif route == "copy":
            copy_items += 1

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
        copy_items=copy_items,
    )


def _iter_transcribe_routes(
    state_dir: Path,
    src_key: str,
    *,
    asr_enabled: bool,
    asr_pipeline_version: str,
    local_max_duration_hours: float,
    route_classifier: TranscribeRouteClassifier | None = None,
    transcript_stale: TranscriptStalePredicate | None = None,
) -> Iterator[tuple[str, TranscribeRoute, float | None]]:
    """Yield ``(uid, route, duration_seconds)`` for each record that is transcribe **work this
    run** — the shared classification behind the source-atomic estimate
    (:func:`estimate_transcribe_shard_work`) and the per-episode plan
    (:func:`pending_transcribe_items`), so both agree on exactly which uids are pending and why.

    Skips (yields nothing for) records that are not transcribable this run: no hosted audio yet,
    availability-withheld media, a failed materialization (the audio lane re-encodes it), or a
    transcript already synced at the current ASR pipeline version. ``inflight`` is yielded but
    represents no new shard work.
    """
    if not asr_enabled:
        return
    now = datetime.now(UTC)
    for uid, rec in load_records(state_dir, src_key).items():
        ep = record_to_episode(rec)
        if not (ep.audio_key and ep.audio_spec_hash and ep.hosted_audio_url):
            continue
        # ``AudioStage`` withholds confirmed empty/missing/invalid recordings from feeds and
        # media-affecting work. A legacy hosted object may still exist, but it is not a valid ASR
        # input (for example, a subtitle-only M4A produced for an empty recording).
        if ep.media_availability is not None and ep.media_availability.is_withheld():
            continue
        # Audio that failed to materialize isn't transcribable this run — the transcribe stage
        # skips it and the audio lane re-encodes it. Counting it here would re-inflate the shard
        # weight (and the backlog) with work ASR will never do, the exact distortion run #25 hit.
        if ep.materialize_error:
            continue
        if ep.transcript_synced:
            key_name = (ep.transcript_key or "").rsplit("/", 1)[-1]
            is_asr = key_name.startswith(f"{ep.uid or ep.guid}-asr-")
            copy_migration = (
                is_asr
                and ep.transcript_pipeline_version == asr_pipeline_version
                and transcript_stale is not None
                and transcript_stale(ep)
            )
            if copy_migration:
                yield uid, "copy", None
                continue
            if not (is_asr and ep.transcript_pipeline_version != asr_pipeline_version):
                continue
        timeout_backoff_until = transcript_timeout_backoff_until(ep)
        if timeout_backoff_until is not None and now < timeout_backoff_until:
            yield uid, "blocked", None
            continue
        duration_hours, _duration_source = episode_duration_hours(ep)
        duration_seconds = duration_hours * 3600.0 if duration_hours > 0 else None
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
        if route not in ("local", "dispatch", "blocked", "inflight", "copy"):
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
    transcript_stale: TranscriptStalePredicate | None = None,
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
            transcript_stale=transcript_stale,
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
        elif route == "copy":
            weight = TRANSCRIBE_COPY_WEIGHT_SECONDS
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
            speakers_key = (rec.get("speakers") or {}).get("key")
            if speakers_key:
                keys.add(speakers_key)
            provider_transcript = rec.get("provider_transcript") or {}
            if isinstance(provider_transcript, dict):
                for slot in ("known_good", "candidate"):
                    artifact = provider_transcript.get(slot) or {}
                    if isinstance(artifact, dict) and artifact.get("key"):
                        keys.add(artifact["key"])
                for artifact in provider_transcript.get("history") or []:
                    if isinstance(artifact, dict) and artifact.get("key"):
                        keys.add(artifact["key"])
            for link_key, link_value in (rec.get("links") or {}).items():
                if link_key.endswith("_artifact_key") and isinstance(link_value, str):
                    keys.add(link_value)
    return keys


def episode_to_record(ep: Episode) -> dict:
    record = {
        "uid": ep.uid,
        "provider_guid": ep.guid,
        "title": ep.title,
        "published": ep.published.isoformat(),
        "body": ep.body,
        "media_kind": ep.media_kind,
        "video_url": ep.video_url,
        "links": ep.links,
        "source_chapters": ep.source_chapters or None,
        "chapters": ep.chapters,
        "chapters_basis": ep.chapters_basis,
        "summary": ep.summary,
        "tags": ep.tags or None,
        "chapter_tags": ep.chapter_tags or None,
        "llm_tag_candidates": ep.llm_tag_candidates or None,
        "tags_llm_recipe_hash": ep.tags_llm_recipe_hash,
        "tags_spec_hash": ep.tags_spec_hash,
        "tags_input_fingerprint": ep.tags_input_fingerprint,
        "agenda_text": {
            "url": ep.agenda_text_url,
            "attempts": ep.agenda_text_attempts,
            "last_attempt": ep.agenda_text_last_attempt,
        }
        if ep.agenda_text_url or ep.agenda_text_attempts
        else None,
        "agenda_backup": {
            "url": ep.agenda_backup_url,
            "attempts": ep.agenda_backup_attempts,
            "last_attempt": ep.agenda_backup_last_attempt,
        }
        if ep.agenda_backup_url or ep.agenda_backup_attempts
        else None,
        "minutes_text": {
            "url": ep.minutes_text_url,
            "attempts": ep.minutes_text_attempts,
            "last_attempt": ep.minutes_text_last_attempt,
        }
        if ep.minutes_text_url or ep.minutes_text_attempts
        else None,
        "minutes_votes": {"url": ep.minutes_votes_url, "items": ep.minutes_votes}
        if ep.minutes_votes_url or ep.minutes_votes
        else None,
        "minutes_roster": {"url": ep.minutes_roster_url, "members": ep.minutes_roster}
        if ep.minutes_roster_url or ep.minutes_roster
        else None,
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
            "media_error": ep.transcript_media_error,
            "media_error_last_attempt": ep.transcript_media_error_last_attempt,
            "media_error_audio_identity": ep.transcript_media_error_audio_identity,
        }
        # A word-JSON sidecar is independently useful to search/clips/diarization.  Do not drop
        # its pointer merely because a producer did not set the feed-facing transcript key.
        if (
            ep.transcript_key
            or ep.transcript_words_key
            or ep.transcript_words_url
            or ep.transcript_timeout_attempts
            or ep.transcript_media_error
        )
        else None,
        # Provider/city-supplied source transcript registry.  Separate from the active
        # transcript block so ASR/alignment can be the podcast transcript while the original
        # city document remains downloadable and eligible for refresh/re-alignment.
        "provider_transcript": ep.provider_transcript or None,
        "speakers": {
            "key": ep.speakers_key,
            "url": ep.speakers_url,
            "spec_hash": ep.speakers_spec_hash,
            "format": ep.speakers_format,
            "synced": ep.speakers_synced,
            "confidence": ep.speakers_confidence,
            "pipeline_version": ep.speakers_pipeline_version,
            "error": ep.speakers_error,
        }
        if ep.speakers_key or ep.speakers_error
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
            "rebuild": ep.audio_rebuild or None,  # omit when empty to keep records clean
            # Materialization backoff state (#120): persisted so failures back off across runs.
            "attempts": ep.materialize_attempts,
            "last_attempt": ep.materialize_last_attempt,
            "error": ep.materialize_error,
            "error_spec_hash": ep.materialize_error_spec_hash,
        },
        # Durable media-availability verdict (H16 PR3): omitted when never classified so the
        # record stays clean for direct enclosures we don't re-host and for pre-PR3 records.
        "media_availability": _availability_to_dict(ep.media_availability),
        # Audit-owned diagnostic/repair metadata. Kept out of render hashes and preserved across
        # scoped lane pushes so repair intent survives ordinary audio/transcript work.
        "integrity": ep.integrity or None,
    }
    set_source_duration_seconds(record, episode_source_duration_seconds(ep))
    set_served_duration_seconds(record, episode_served_duration_seconds(ep))
    return record


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
        "transcript_media_error": t.get("media_error"),
        "transcript_media_error_last_attempt": t.get("media_error_last_attempt"),
        "transcript_media_error_audio_identity": t.get("media_error_audio_identity"),
    }


def _speakers_fields_from_rec(rec: dict) -> dict:
    s = rec.get("speakers") or {}
    if not isinstance(s, dict):
        return {}
    return {
        "speakers_key": s.get("key"),
        "speakers_url": s.get("url"),
        "speakers_spec_hash": s.get("spec_hash"),
        "speakers_format": s.get("format"),
        "speakers_synced": bool(s.get("synced", False)),
        "speakers_confidence": s.get("confidence"),
        "speakers_pipeline_version": s.get("pipeline_version"),
        "speakers_error": s.get("error"),
    }


def _availability_to_dict(av: MediaAvailability | None) -> dict | None:
    """Serialize the durable availability verdict (H16 PR3), or ``None`` to omit the block."""
    if av is None:
        return None
    return dataclasses.asdict(av)


def _availability_from_rec(rec: dict) -> MediaAvailability | None:
    """Rebuild the availability verdict from a record. Older records lack the block -> ``None``
    (the episode is simply re-classified on the next audio run)."""
    block = rec.get("media_availability")
    if not isinstance(block, dict) or not block.get("state"):
        return None
    known = {f.name for f in dataclasses.fields(MediaAvailability)}
    return MediaAvailability(**{k: v for k, v in block.items() if k in known})


def _source_media_from_dict(d: dict) -> SourceMedia:
    known = {f.name for f in dataclasses.fields(SourceMedia)}
    return SourceMedia(**{k: v for k, v in d.items() if k in known})


def _timeline_from_dict(d: dict) -> Timeline | None:
    """Rebuild a persisted timeline. A legacy/newer record's segment dict may carry keys this
    version's ``Segment`` doesn't declare (or lack a ``version``) — filter/default like the
    sibling ``_source_media_from_dict``/``_availability_from_rec`` rebuilders, rather than
    raising and failing episode load entirely (CR2-CP-27). A missing ``version`` falls back to
    ``""``, which can't match any real planner-set signature, so the episode simply re-plans on
    the next run instead of crashing.

    A segment dict that is internally inconsistent for its own ``kind`` (Segment's own
    ``__post_init__`` guard, CR2-CP-13) is treated the same way: this episode's timeline is
    unrecoverable from this record, so it returns ``None`` (identity) and re-plans, rather than
    raising out of here and aborting the whole episode-list load for every other episode.
    """
    known = {f.name for f in dataclasses.fields(Segment)}
    try:
        return Timeline(
            version=d.get("version", ""),
            segments=tuple(
                Segment(**{k: v for k, v in s.items() if k in known})
                for s in d.get("segments", [])
                if isinstance(s, dict)
            ),
            basis=d.get("basis", "served"),
        )
    except (TypeError, ValueError, AttributeError):
        return None


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
    agenda = rec.get("agenda_text") or {}
    backup = rec.get("agenda_backup") or {}
    minutes = rec.get("minutes_text") or {}
    votes = rec.get("minutes_votes") or {}
    roster = rec.get("minutes_roster") or {}
    source_duration = record_source_duration_seconds(rec)
    served_duration = record_served_duration_seconds(rec)

    sources_data = rec.get("sources") or []
    sources = [_source_media_from_dict(s) for s in sources_data]

    tl_data = rec.get("timeline")
    timeline = _timeline_from_dict(tl_data) if tl_data else None

    return Episode(
        guid=rec.get("provider_guid") or "",
        title=rec.get("title") or "",
        published=when,
        video_url=rec.get("video_url") or "",
        duration=int(round(source_duration)) if source_duration is not None else None,
        source_duration_seconds=source_duration,
        media_kind=rec.get("media_kind") or "direct",
        body=rec.get("body"),
        uid=rec.get("uid"),
        hosted_audio_url=audio.get("url"),
        audio_key=audio.get("key"),
        audio_spec_hash=audio.get("spec_hash"),
        materialize_attempts=audio.get("attempts") or 0,
        materialize_last_attempt=audio.get("last_attempt"),
        materialize_error=audio.get("error"),
        materialize_error_spec_hash=audio.get("error_spec_hash"),
        audio_bytes=audio.get("bytes"),
        links=rec.get("links") or {},
        agenda_text_url=agenda.get("url"),
        agenda_text_attempts=_coerce_non_negative_int(agenda.get("attempts")),
        agenda_text_last_attempt=agenda.get("last_attempt"),
        agenda_backup_url=backup.get("url"),
        agenda_backup_attempts=_coerce_non_negative_int(backup.get("attempts")),
        agenda_backup_last_attempt=backup.get("last_attempt"),
        minutes_text_url=minutes.get("url"),
        minutes_text_attempts=_coerce_non_negative_int(minutes.get("attempts")),
        minutes_text_last_attempt=minutes.get("last_attempt"),
        minutes_votes_url=votes.get("url"),
        minutes_votes=votes.get("items") if isinstance(votes.get("items"), list) else [],
        minutes_roster_url=roster.get("url"),
        minutes_roster=roster.get("members") if isinstance(roster.get("members"), list) else [],
        source_chapters=rec.get("source_chapters") or [],
        chapters=rec.get("chapters") or [],
        provider_transcript=(
            rec.get("provider_transcript")
            if isinstance(rec.get("provider_transcript"), dict)
            else {}
        ),
        summary=rec.get("summary") or "",
        tags=rec.get("tags") if isinstance(rec.get("tags"), list) else [],
        chapter_tags=rec.get("chapter_tags") if isinstance(rec.get("chapter_tags"), list) else [],
        llm_tag_candidates=(
            rec.get("llm_tag_candidates") if isinstance(rec.get("llm_tag_candidates"), list) else []
        ),
        tags_llm_recipe_hash=rec.get("tags_llm_recipe_hash"),
        tags_spec_hash=rec.get("tags_spec_hash"),
        tags_input_fingerprint=rec.get("tags_input_fingerprint"),
        # v2 transcript block (INFRA-8); v1 records with old transcript_url silently dropped.
        **_transcript_fields_from_rec(rec),
        **_speakers_fields_from_rec(rec),
        # v2 fields (default to identity/empty for v1 records — lazy upgrade)
        sources=sources,
        timeline=timeline,
        chapters_basis=rec.get("chapters_basis", "source:s0"),
        audio_rebuild=audio.get("rebuild") or "",
        audio_encode_time=audio.get("encode_time"),
        audio_duration_served=served_duration,
        served_duration_seconds=served_duration,
        integrity=rec.get("integrity") if isinstance(rec.get("integrity"), dict) else {},
        media_availability=_availability_from_rec(rec),
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
# ``media_availability`` (H16 PR3) is produced by the audio lane's detection pass, so it is an
# audio-owned artifact block: a sibling ``transcribe``/``align`` push must preserve it, exactly like
# the ``audio`` block, or it would regress a freshly-written availability verdict. The
# ``provider_transcript`` registry is a transcript-lane artifact: PT-PR5/PT-PR6 update candidate /
# known-good confidence and must preserve it from audio-lane snapshots.
ARTIFACT_BLOCKS: frozenset[str] = frozenset(
    {
        "audio",
        "transcript",
        "provider_transcript",
        "speakers",
        "media_availability",
        "integrity",
        "agenda_text",
        "agenda_backup",
        "minutes_text",
        "minutes_votes",
        "minutes_roster",
        "tags",
        "chapter_tags",
        "llm_tag_candidates",
        "tags_llm_recipe_hash",
        "tags_spec_hash",
        "tags_input_fingerprint",
    }
)
PLANNING_FIELDS: frozenset[str] = frozenset(
    {"sources", "timeline", "source_chapters", "chapters", "chapters_basis"}
)

# Which artifact block(s) each lane writes authoritatively. Document extraction runs with the
# source-scoped audio lane: it has the complete archive needed to attach agenda-derived minutes to
# a prior meeting and is independent of ASR. A lane absent here (e.g. ``None`` — a full unsharded
# enrich or a manual single-source run that runs *every* stage) owns everything, so it preserves
# nothing (behaves like the legacy whole-record push).
_LANE_OWNED_BLOCKS: dict[str, frozenset[str]] = {
    "audio": frozenset(
        {
            "audio",
            "media_availability",
            "agenda_text",
            "agenda_backup",
            "minutes_text",
            "minutes_votes",
            "minutes_roster",
        }
    ),
    "transcribe": frozenset({"transcript", "provider_transcript"}),
    "align": frozenset({"transcript", "provider_transcript"}),
    "diarize": frozenset({"speakers", "provider_transcript"}),
    "tag": frozenset(
        {
            "tags",
            "chapter_tags",
            "llm_tag_candidates",
            "tags_llm_recipe_hash",
            "tags_spec_hash",
            "tags_input_fingerprint",
        }
    ),
}


def protected_blocks_for_lane(lane: str | None) -> frozenset[str]:
    """Artifact blocks a scoped ``lane`` run must PRESERVE from the freshest remote because it does
    not own them — everything else (its owned artifact plus the re-fetched provider/render fields)
    is written fresh. ``None``/unknown lane owns every artifact, so nothing is protected."""
    owned = _LANE_OWNED_BLOCKS.get(lane, ARTIFACT_BLOCKS) if lane is not None else ARTIFACT_BLOCKS
    return ARTIFACT_BLOCKS - owned


def _basis_rank(basis: object) -> int:
    if isinstance(basis, str) and (basis == "decoded" or basis.startswith("decoded:")):
        return 3
    if basis == "stream-sample":
        return 2
    if basis == "container":
        return 1
    return 0


def _record_source_basis_rank(rec: dict) -> int:
    sources = rec.get("sources") or []
    ranks = [_basis_rank(src.get("duration_basis")) for src in sources if isinstance(src, dict)]
    return min(ranks) if ranks else 0


def _record_timeline_version_rank(rec: dict) -> tuple[int, ...]:
    timeline = rec.get("timeline")
    version = timeline.get("version") if isinstance(timeline, dict) else ""
    return tuple(int(n) for n in re.findall(r":(\d+)", str(version or "")))


def _record_planning_rank(rec: dict) -> tuple[tuple[int, ...], int, int]:
    return (
        _record_timeline_version_rank(rec),
        _record_source_basis_rank(rec),
        1 if rec.get("source_chapters") else 0,
        1 if rec.get("timeline") else 0,
    )


def _preserve_remote_planning_if_better(
    rec: dict, remote_rec: dict, protected: frozenset[str]
) -> None:
    """Keep decoded/newer planning metadata from being clobbered by a stale scoped push.

    Scoped lanes re-read remote state to protect sibling artifact blocks, but the timeline/source
    metadata sits in the whole-record body. A long-running shard that started before a repair can
    therefore carry container/legacy planning locally and overwrite a fresher decoded remote plan.
    When remote planning is strictly better, keep it and its associated artifact blocks; local
    artifacts were computed against the stale plan and are not safe to attach to the newer EDL.

    **Never DROP an artifact block this lane owns and just produced.** A block outside ``protected``
    is one this run authoritatively wrote (e.g. ``transcript`` for the transcribe lane). If remote's
    plan is strictly better but remote has no value for that owned block, the old code popped/nulled
    it — silently discarding a just-completed transcript. That is uniquely catastrophic for the
    transcribe lane: the VTT/words artifact is already uploaded, so the lease reaper infers the item
    is *done* from artifact presence, yet the record shows ``transcript: null`` and nothing ever
    reconciles the two → a permanent, invisible loss (surfaced live on GH#831's first long Modal
    canary). So an owned block is only ever *replaced* by a truthy remote value (the legitimate
    audio-vs-decoded-plan case remote HAS the fresher artifact), never deleted. Non-owned
    (``protected``) blocks and planning fields keep the original replace-or-drop behavior — the
    freshest remote plan governs those, and the local copy for them is snapshot-stale anyway.
    """
    if _record_planning_rank(remote_rec) <= _record_planning_rank(rec):
        return
    owned_artifacts = ARTIFACT_BLOCKS - protected
    for key in PLANNING_FIELDS | ARTIFACT_BLOCKS:
        if key in remote_rec and remote_rec[key]:
            rec[key] = remote_rec[key]
        elif key == "source_chapters" and rec.get(key):
            # A better remote planning record that simply predates source_chapters should not erase
            # the canonical raw chapter input we already hold locally.
            continue
        elif key in owned_artifacts:
            # This run owns and just produced this block; a better remote plan that simply lacks it
            # must not erase it. Keep local's value.
            continue
        else:
            rec.pop(key, None)


def _preserve_remote_served_duration_if_protected(
    rec: dict, remote_rec: dict, protected: frozenset[str]
) -> None:
    """Keep a fresher remote served duration when this lane does not own audio."""
    if "audio" not in protected:
        return
    remote_served = record_served_duration_seconds(remote_rec)
    if remote_served is None:
        return
    set_served_duration_seconds(rec, remote_served)


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
            _preserve_remote_served_duration_if_protected(rec, remote_rec, protected)
            _preserve_remote_planning_if_better(rec, remote_rec, protected)
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
        ep.materialize_error_spec_hash = audio.get("error_spec_hash")
        ep.audio_bytes = audio.get("bytes")
        ep.audio_encode_time = audio.get("encode_time")
        ep.audio_rebuild = audio.get("rebuild") or ""
        ep.summary = rec.get("summary", ep.summary)
        transcript_fields = _transcript_fields_from_rec(rec)
        if transcript_fields.get("transcript_key"):
            ep.transcript_key = transcript_fields["transcript_key"]
            ep.transcript_hosted_url = transcript_fields["transcript_hosted_url"]
            ep.transcript_spec_hash = transcript_fields["transcript_spec_hash"]
            ep.transcript_format = transcript_fields["transcript_format"]
            ep.transcript_basis = transcript_fields["transcript_basis"]
            ep.transcript_synced = transcript_fields["transcript_synced"]
            ep.transcript_words_key = transcript_fields["transcript_words_key"]
            ep.transcript_words_url = transcript_fields["transcript_words_url"]
            ep.transcript_pipeline_version = transcript_fields["transcript_pipeline_version"]
        ep.transcript_timeout_attempts = transcript_fields.get("transcript_timeout_attempts", 0)
        ep.transcript_timeout_last_attempt = transcript_fields.get(
            "transcript_timeout_last_attempt"
        )
        ep.transcript_media_error = transcript_fields.get("transcript_media_error")
        ep.transcript_media_error_last_attempt = transcript_fields.get(
            "transcript_media_error_last_attempt"
        )
        ep.transcript_media_error_audio_identity = transcript_fields.get(
            "transcript_media_error_audio_identity"
        )
        provider_transcript = rec.get("provider_transcript")
        ep.provider_transcript = (
            provider_transcript if isinstance(provider_transcript, dict) else {}
        )
        speakers = rec.get("speakers") or {}
        if isinstance(speakers, dict):
            ep.speakers_key = speakers.get("key")
            ep.speakers_url = speakers.get("url")
            ep.speakers_spec_hash = speakers.get("spec_hash")
            ep.speakers_format = speakers.get("format")
            ep.speakers_synced = bool(speakers.get("synced", False))
            ep.speakers_confidence = speakers.get("confidence")
            ep.speakers_pipeline_version = speakers.get("pipeline_version")
            ep.speakers_error = speakers.get("error")
        # Persisted links are derived artifacts, except a freshly supplied provider link.  In
        # particular an agenda-derived minutes URL must never mask a later canonical provider URL.
        persisted_links = rec.get("links") or {}
        merged_links = dict(persisted_links)
        for key, value in (ep.links or {}).items():
            if value and (key not in merged_links or key == "minutes"):
                merged_links[key] = value
        if (ep.links or {}).get("minutes") and merged_links.get("minutes_source") == "agenda_link":
            merged_links.pop("minutes_source", None)
            merged_links.pop("minutes_source_episode_uid", None)
        ep.links = merged_links or ep.links
        agenda = rec.get("agenda_text") or {}
        backup = rec.get("agenda_backup") or {}
        minutes = rec.get("minutes_text") or {}
        votes = rec.get("minutes_votes") or {}
        roster = rec.get("minutes_roster") or {}
        ep.agenda_text_url = agenda.get("url")
        ep.agenda_text_attempts = _coerce_non_negative_int(agenda.get("attempts"))
        ep.agenda_text_last_attempt = agenda.get("last_attempt")
        ep.agenda_backup_url = backup.get("url")
        ep.agenda_backup_attempts = _coerce_non_negative_int(backup.get("attempts"))
        ep.agenda_backup_last_attempt = backup.get("last_attempt")
        ep.minutes_text_url = minutes.get("url")
        ep.minutes_text_attempts = _coerce_non_negative_int(minutes.get("attempts"))
        ep.minutes_text_last_attempt = minutes.get("last_attempt")
        ep.minutes_votes_url = votes.get("url")
        ep.minutes_votes = votes.get("items") if isinstance(votes.get("items"), list) else []
        ep.minutes_roster_url = roster.get("url")
        ep.minutes_roster = roster.get("members") if isinstance(roster.get("members"), list) else []
        ep.source_chapters = rec.get("source_chapters") or ep.source_chapters
        ep.chapters = rec.get("chapters") or ep.chapters
        ep.chapters_basis = rec.get("chapters_basis", ep.chapters_basis)
        ep.tags = rec.get("tags") or ep.tags
        ep.chapter_tags = rec.get("chapter_tags") or ep.chapter_tags
        ep.llm_tag_candidates = rec.get("llm_tag_candidates") or ep.llm_tag_candidates
        ep.tags_llm_recipe_hash = rec.get("tags_llm_recipe_hash", ep.tags_llm_recipe_hash)
        ep.tags_spec_hash = rec.get("tags_spec_hash", ep.tags_spec_hash)
        ep.tags_input_fingerprint = rec.get("tags_input_fingerprint", ep.tags_input_fingerprint)
        persisted_source = record_source_duration_seconds(rec)
        if persisted_source is not None and episode_source_duration_seconds(ep) is None:
            set_source_duration_seconds(ep, persisted_source)
        persisted_served = record_served_duration_seconds(rec)
        if persisted_served is not None:
            set_served_duration_seconds(ep, persisted_served)
        # v2: restore sources and timeline from record (lazy upgrade: absent → defaults)
        sources_data = rec.get("sources") or []
        if sources_data and not ep.sources:
            ep.sources = [_source_media_from_dict(s) for s in sources_data]
        tl_data = rec.get("timeline")
        if tl_data and ep.timeline is None:
            ep.timeline = _timeline_from_dict(tl_data)
        integrity = rec.get("integrity")
        if isinstance(integrity, dict):
            ep.integrity = integrity
        availability = _availability_from_rec(rec)
        if availability is not None:
            ep.media_availability = availability


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
