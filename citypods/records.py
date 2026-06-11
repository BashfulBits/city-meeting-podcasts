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
from datetime import UTC, datetime
from pathlib import Path

from citypods.bodies import body_key, canonical_body
from citypods.models import City, Episode
from citypods.timeline import Segment, SourceMedia, Timeline, timeline_digest

SCHEMA_VERSION = 2
# Bump to force every audio file to be regenerated (e.g. a codec/loudness policy change that
# isn't otherwise captured by the per-episode spec inputs below).
AUDIO_PIPELINE_VERSION = "1"


def source_key(city: City) -> str:
    """Stable id for a city's media source, ignoring the per-board ``body`` filter, so the
    combined feed and every per-board feed of one city share one record store + audio object."""
    src = {k: v for k, v in city.source.items() if k != "body"}
    raw = f"{city.provider}|{json.dumps(src, sort_keys=True)}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


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


def audio_spec_hash(ep: Episode, *, max_kbps: int, loudness_profile: str = "") -> str:
    """Hash of everything that determines the audio bytes.

    **Identity path (v1-compatible):** when no timeline manipulation, rebuild nonce, or
    loudness profile is active, and the episode has at most one source, the spec dict is
    byte-identical to the v1 format — same JSON → same SHA1 → no re-encode storm when
    this model first ships.  Only episodes that are *actually* manipulated (non-identity
    timeline, nonce stamped, multi-source concat) get the new format and a new key.

    **v2 format** (all other cases): adds ``timeline``, ``loudness``, ``sources``,
    ``rebuild`` fields.  New fields are included at their defaults (``""``, ``[]``) so
    future features that set them only re-encode the episodes they actually affect.

    Note: the HLS *resolved* URL is tokenized/expiring and is deliberately excluded.
    Identity-equivalence intentionally keys the v1 path on ``ep.video_url`` (the stable
    source handle today), **not** on ``SourceMedia.ref`` — so once ``TimelineStage`` starts
    registering a single identity source, the hash stays byte-identical and no re-encode
    storm occurs. Do not "fix" this to read ``ref``: it would change every identity hash.
    """
    tl_digest = timeline_digest(ep.timeline) if ep.timeline is not None else ""
    loudness = loudness_profile
    rebuild = ep.audio_rebuild or ""

    if not tl_digest and not rebuild and not loudness and len(ep.sources) <= 1:
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
        }
        if ep.transcript_key
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
        if isinstance(t, dict) and t.get("key"):
            ep.transcript_key = t.get("key")
            ep.transcript_hosted_url = t.get("url")
            ep.transcript_spec_hash = t.get("spec_hash")
            ep.transcript_format = t.get("format")
            ep.transcript_basis = t.get("basis", "source:s0")
            ep.transcript_synced = bool(t.get("synced", False))
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
