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

import hashlib
import json
import re
from pathlib import Path

from citypods.bodies import body_key, canonical_body
from citypods.models import City, Episode

SCHEMA_VERSION = 1
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


def audio_spec_hash(ep: Episode, *, max_kbps: int) -> str:
    """Hash of everything that determines the audio bytes. Note: the HLS *resolved* URL is
    tokenized/expiring and is deliberately excluded — only the stable source page url is used."""
    spec = {
        "v": AUDIO_PIPELINE_VERSION,
        "source": ep.video_url,
        "max_kbps": max_kbps,
        "chapters": ep.chapters,
    }
    blob = json.dumps(spec, separators=(",", ":"), sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def audio_object_key(city: City, ep: Episode, spec: str) -> str:
    """Content-addressed storage key: changes iff the audio spec changes."""
    return f"{city.provider}/{source_key(city)}/{ep.uid}-{spec}.m4a"


def feed_content_hash(episodes: list[Episode], fingerprint: str) -> str:
    """Hash of the render-relevant fields of the (filtered+capped) feed. Drives the
    re-render skip. Includes notes/summary/links/chapters so an enrichment change re-renders."""
    payload = [
        [
            e.uid,
            e.title,
            e.published.isoformat(),
            e.description,
            e.summary,
            e.transcript_url,
            sorted((e.links or {}).items()),
            e.chapters,
            e.duration,
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
        "summary": ep.summary,
        "transcript_url": ep.transcript_url,
        "audio": {
            "key": ep.audio_key,
            "url": ep.hosted_audio_url,
            "spec_hash": ep.audio_spec_hash,
        },
    }


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
        ep.summary = rec.get("summary", ep.summary)
        ep.transcript_url = rec.get("transcript_url", ep.transcript_url)
        ep.links = rec.get("links") or ep.links
        ep.chapters = rec.get("chapters") or ep.chapters
        if rec.get("duration") and not ep.duration:
            ep.duration = rec["duration"]


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
