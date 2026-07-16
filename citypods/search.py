"""Build the static, client-side meeting search index.

The index is generated from durable episode records, never rendered HTML.  Search is a render
concern: it reads already-produced content-addressed sidecars, makes no provider requests, and
publishes one bounded JSON shard per source for lazy browser loading.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import shutil
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from citypods.chapters import episode_served_chapters
from citypods.feeds import episode_resource_links, meeting_page_url
from citypods.models import City, Episode
from citypods.records import load_records, record_to_episode, source_key
from citypods.tags import chapter_id

SEARCH_SCHEMA_VERSION = 3
SEARCH_CACHE_VERSION = "4"
SEARCH_ASSET = "minisearch-7.1.2.js"
SEARCH_LICENSE = "LICENSES/minisearch-7.1.2.txt"
SEARCH_FIELDS = (
    "title",
    "body",
    "city",
    "city_label",
    "date",
    "description",
    "tags",
    "link_labels_text",
    "chapters_text",
    "agenda_text",
    "backup_text",
    "backup_labels_text",
    "minutes_text",
    "roster_text",
    "votes_text",
    "transcript_text",
)
SHARD_SOFT_GZIP_BYTES = 1_000_000
SHARD_HARD_GZIP_BYTES = 2_000_000
_MAX_TRANSCRIPT_SEGMENTS = 20_000
_MAX_TRANSCRIPT_TEXT_CHARS = 200_000
_MAX_TEXT_CHARS = 200_000


def _clean(value: Any, limit: int = _MAX_TEXT_CHARS) -> str:
    if value is None:
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text[:limit]


def _text_values(value: Any) -> list[str]:
    """Flatten small structured fields used by present/future tag, vote, and roster stages."""
    if value is None:
        return []
    if isinstance(value, str | int | float):
        return [_clean(value, 10_000)]
    if isinstance(value, dict):
        values: list[str] = []
        for key, item in value.items():
            if key not in {"evidence", "source_url", "method", "chapter_index"}:
                values.extend(_text_values(item))
        return values
    if isinstance(value, list | tuple):
        values: list[str] = []
        for item in value:
            values.extend(_text_values(item))
        return values
    return []


def _read_artifact(storage: Any, key: str | None, *, cache: dict[str, bytes]) -> bytes | None:
    """Read a sidecar once without retaining temporary files or touching a provider endpoint."""
    if not key or storage is None or not hasattr(storage, "get_file"):
        return None
    if key in cache:
        return cache[key]
    with tempfile.TemporaryDirectory(prefix="citypods-search-") as tmp:
        path = Path(tmp) / "artifact"
        try:
            if not storage.get_file(key, path) or not path.exists():
                return None
            data = path.read_bytes()
        except Exception:
            return None
    # Per-field extractors bound what reaches the public shard.  Do not byte-truncate JSON here:
    # that turns an otherwise valid word/backup sidecar into invalid JSON and silently loses it.
    cache[key] = data
    return data


def _json_bytes(data: bytes | None) -> Any:
    if not data:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _storage_key_from_public_url(storage: Any, url: str | None) -> str | None:
    """Recover an old sidecar key when only its own public URL was persisted.

    Older R3 records predate the explicit ``*_artifact_key`` fields.  We only reverse URLs that
    exactly live under this storage object's public prefix, avoiding arbitrary URL fetches and path
    traversal-like keys.
    """
    if not url or storage is None or not hasattr(storage, "public_url"):
        return None
    try:
        raw_prefix = str(storage.public_url(""))
    except Exception:
        return None
    parsed = urlsplit(url)
    prefixes = {
        raw_prefix if raw_prefix.endswith("/") else f"{raw_prefix}/",
        f"{raw_prefix.rstrip('/')}/",
    }
    prefix = next((candidate for candidate in prefixes if url.startswith(candidate)), None)
    if parsed.query or parsed.fragment or prefix is None:
        return None
    key = url[len(prefix) :]
    if not key or key.startswith("/") or any(part in {"", ".", ".."} for part in key.split("/")):
        return None
    return key


def _artifact_key(ep: Episode, kind: str, storage: Any) -> str | None:
    links = ep.links or {}
    if kind == "transcript":
        return ep.transcript_words_key or _storage_key_from_public_url(
            storage, ep.transcript_words_url
        )
    key = links.get(f"{kind}_artifact_key")
    if key:
        return str(key)
    # The initial R3 sidecars stored public artifact URLs before explicit object keys were added.
    # Prefer that URL over the similarly named provider-source URL on the Episode itself.
    artifact_url = links.get(f"{kind}_artifact")
    if artifact_url:
        recovered = _storage_key_from_public_url(storage, str(artifact_url))
        if recovered:
            return recovered
    url_field = {
        "agenda_text": "agenda_text_url",
        "agenda_backup": "agenda_backup_url",
        "minutes_text": "minutes_text_url",
    }.get(kind)
    return (
        _storage_key_from_public_url(storage, getattr(ep, url_field, None)) if url_field else None
    )


def _transcript_segments(
    data: bytes | None, chapters: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    payload = _json_bytes(data)
    if not isinstance(payload, dict):
        return []
    segments: list[dict[str, Any]] = []
    text_chars = 0
    for item in payload.get("segments") or []:
        if not isinstance(item, dict) or not item.get("text"):
            continue
        try:
            start = max(0.0, float(item.get("start", 0)))
        except (TypeError, ValueError):
            start = 0.0
        text = _clean(item["text"], min(12_000, _MAX_TRANSCRIPT_TEXT_CHARS - text_chars))
        if not text:
            continue
        chapter = None
        for index, candidate in enumerate(chapters or []):
            if start >= candidate["start"] and (
                index == len(chapters or []) - 1 or start < (chapters or [])[index + 1]["start"]
            ):
                chapter = candidate.get("id")
                break
        segments.append({"text": text, "start": round(start, 3), "chapter_id": chapter})
        text_chars += len(text)
        if len(segments) >= _MAX_TRANSCRIPT_SEGMENTS or text_chars >= _MAX_TRANSCRIPT_TEXT_CHARS:
            break
    return segments


def _document_text(data: bytes | None, *, backup: bool = False) -> tuple[str, list[str]]:
    if not data:
        return "", []
    if not backup:
        try:
            return _clean(data.decode("utf-8", errors="replace")), []
        except UnicodeDecodeError:  # defensive: retained for non-standard decoder implementations
            return "", []
    payload = _json_bytes(data)
    if not isinstance(payload, dict):
        return "", []
    parts = [_clean(payload.get("text"))]
    labels: list[str] = []
    for link in payload.get("links") or []:
        if not isinstance(link, dict):
            continue
        label = _clean(link.get("label"), 500)
        if label:
            labels.append(label)
        text = _clean(link.get("text"))
        if text:
            parts.append(text)
    return _clean(" ".join(part for part in parts if part)), labels


def _availability(ep: Episode) -> tuple[str, bool]:
    if ep.media_availability is None:
        return "unknown", False
    return ep.media_availability.effective_state(), ep.media_availability.is_withheld()


def _record_to_document(
    city: City,
    record: dict[str, Any],
    *,
    base_url: str,
    storage: Any,
    artifact_cache: dict[str, bytes],
) -> dict[str, Any] | None:
    # The aggregate is a discovery/reconciliation view.  A suppressed overlap already has a
    # durable canonical record in its dedicated source, so indexing it would recreate duplicates.
    integrity = record.get("integrity") if isinstance(record.get("integrity"), dict) else {}
    if integrity.get("aggregate_suppressed"):
        return None
    ep = record_to_episode(record)
    if not ep.uid:
        return None
    city_key = city.city_entity or city.slug
    city_label = city.podcast_author or city.city_entity or city.slug
    body = ep.body or city.source.get("body") or city.podcast_title
    availability, withheld = _availability(ep)
    chapters: list[dict[str, Any]] = []
    chapter_annotations = {
        annotation.get("chapter_id"): annotation
        for annotation in ep.chapter_tags
        if isinstance(annotation, dict)
    }
    for index, chapter in enumerate(episode_served_chapters(ep)):
        if not chapter.get("title"):
            continue
        try:
            start = round(float(chapter.get("start", 0)), 3)
        except (TypeError, ValueError):
            start = 0.0
        cid = chapter_id(ep, chapter, index)
        chapters.append(
            {
                "id": cid,
                "title": _clean(chapter["title"], 2_000),
                "start": start,
                "tags": [
                    tag.get("id")
                    for tag in (chapter_annotations.get(cid) or {}).get("tags", [])
                    if isinstance(tag, dict) and tag.get("id")
                ],
            }
        )
    agenda_data = _read_artifact(
        storage, _artifact_key(ep, "agenda_text", storage), cache=artifact_cache
    )
    backup_data = _read_artifact(
        storage, _artifact_key(ep, "agenda_backup", storage), cache=artifact_cache
    )
    minutes_data = _read_artifact(
        storage, _artifact_key(ep, "minutes_text", storage), cache=artifact_cache
    )
    transcript_data = _read_artifact(
        storage, _artifact_key(ep, "transcript", storage), cache=artifact_cache
    )
    agenda_text, _ = _document_text(agenda_data)
    backup_text, backup_labels = _document_text(backup_data, backup=True)
    minutes_text, _ = _document_text(minutes_data, backup=True)
    tags = [_clean(tag, 500) for tag in _text_values(record.get("tags") or getattr(ep, "tags", []))]
    date = ep.published.date().isoformat()
    return {
        "id": ep.uid,
        "uid": ep.uid,
        "title": _clean(ep.title, 2_000),
        "body": _clean(body, 1_000),
        "city": city_key,
        "city_label": _clean(city_label, 1_000),
        "date": date,
        "date_label": ep.published.strftime("%B %d, %Y").replace(" 0", " "),
        "year": ep.published.year,
        "media_availability_state": availability,
        "is_withheld": withheld,
        "page_url": meeting_page_url(city, ep, base_url),
        "links": [{"label": label, "url": url} for label, url in episode_resource_links(ep)],
        "tags": tags,
        "chapters": chapters,
        "segments": _transcript_segments(transcript_data, chapters),
        "agenda_text": agenda_text or None,
        "backup_text": backup_text or None,
        "backup_labels": backup_labels,
        "minutes_text": minutes_text or None,
        "roster_text": " ".join(_text_values(ep.minutes_roster)),
        "votes_text": " ".join(_text_values(ep.minutes_votes)),
        "description": _clean(ep.summary or ep.description),
    }


def _shard_source(cities: Iterable[City]) -> dict[str, City]:
    representatives: dict[str, City] = {}
    for city in cities:
        representatives.setdefault(source_key(city), city)
    return representatives


def _shard_cities(cities: Iterable[City]) -> dict[str, list[City]]:
    grouped: dict[str, list[City]] = {}
    for city in cities:
        grouped.setdefault(source_key(city), []).append(city)
    return grouped


def _city_for_record(candidates: list[City], record: dict[str, Any]) -> City:
    """Prefer a body-specific feed when one shares this source's record store."""
    body = str(record.get("body") or "").casefold()
    if body:
        for city in candidates:
            configured = str(city.source.get("body") or "").casefold()
            if configured and (configured == body or configured in body or body in configured):
                return city
    return next((city for city in candidates if not city.source.get("body")), candidates[0])


def _search_record_inputs(record: dict[str, Any]) -> dict[str, Any]:
    """The durable fields that can change an emitted search document.

    Availability probes and audio materialization bookkeeping change frequently but do not alter
    search output.  Excluding them makes the render cache useful instead of rebuilding every shard
    after routine health checks.
    """
    transcript = record.get("transcript") if isinstance(record.get("transcript"), dict) else {}
    availability = (
        record.get("media_availability")
        if isinstance(record.get("media_availability"), dict)
        else {}
    )
    integrity = record.get("integrity") if isinstance(record.get("integrity"), dict) else {}
    agenda = record.get("agenda_text") if isinstance(record.get("agenda_text"), dict) else {}
    backup = record.get("agenda_backup") if isinstance(record.get("agenda_backup"), dict) else {}
    minutes = record.get("minutes_text") if isinstance(record.get("minutes_text"), dict) else {}
    return {
        "uid": record.get("uid"),
        "title": record.get("title"),
        "body": record.get("body"),
        "published": record.get("published"),
        "summary": record.get("summary"),
        "description": record.get("description"),
        "links": record.get("links"),
        "tags": record.get("tags"),
        "chapter_tags": record.get("chapter_tags"),
        "agenda_text_url": agenda.get("url"),
        "agenda_backup_url": backup.get("url"),
        "minutes_text_url": minutes.get("url"),
        "minutes_votes": record.get("minutes_votes"),
        "minutes_roster": record.get("minutes_roster"),
        "transcript_words_key": transcript.get("words_key"),
        "transcript_words_url": transcript.get("words_url"),
        "chapters": record.get("chapters"),
        "source_chapters": record.get("source_chapters"),
        "chapters_basis": record.get("chapters_basis"),
        "timeline": record.get("timeline"),
        "availability": {
            "state": availability.get("state"),
            "operator_override": availability.get("operator_override"),
        },
        "aggregate_suppressed": integrity.get("aggregate_suppressed"),
    }


def _shard_hash(records: dict[str, Any], candidates: list[City], base_url: str) -> str:
    """Hash only local, durable inputs so unchanged shards skip sidecar reads and writes."""
    payload = {
        "version": SEARCH_CACHE_VERSION,
        "base_url": base_url.rstrip("/"),
        "records": {uid: _search_record_inputs(record) for uid, record in records.items()},
        "views": [
            {
                "slug": city.slug,
                "body": city.source.get("body"),
                "entity": city.city_entity,
                "title": city.podcast_title,
                "author": city.podcast_author,
            }
            for city in candidates
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _coverage(documents: Iterable[dict[str, Any]]) -> dict[str, int]:
    """Return an exact, additive transcript-coverage numerator and denominator."""
    values = list(documents)
    return {
        "episode_count": len(values),
        "transcript_episode_count": sum(bool(document["segments"]) for document in values),
    }


def _write_search_asset(output_dir: Path) -> None:
    assets = Path(__file__).resolve().parent / "assets"
    sources = [(relative, assets / relative) for relative in (SEARCH_ASSET, SEARCH_LICENSE)]
    for _, source in sources:
        if not source.is_file():
            raise FileNotFoundError(f"missing vendored search asset: {source}")
    for relative, source in sources:
        target = output_dir / "assets" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def build_search_index(
    state_dir: str | Path,
    cities: Iterable[City],
    output_dir: str | Path,
    base_url: str,
    *,
    storage: Any = None,
    cache: dict[str, Any] | None = None,
    stop: Callable[[], bool] | None = None,
) -> dict[str, Any] | None:
    """Build deterministic source shards and return the small public manifest.

    ``cache`` belongs in the existing render cache.  It makes the normal scheduled run avoid
    downloading every transcript/document sidecar when the durable records are unchanged.  ``stop``
    is checked before each restartable source/record unit.  A stopped build leaves the last complete
    manifest intact and returns ``None`` so its caller can avoid advertising a partial first build.
    """
    state_dir = Path(state_dir)
    output_dir = Path(output_dir)
    search_dir = output_dir / "data" / "search"
    search_dir.mkdir(parents=True, exist_ok=True)
    all_cities = list(cities)
    representatives = _shard_source(all_cities)
    candidates_by_source = _shard_cities(all_cities)
    cache_shards = cache.setdefault("shards", {}) if cache is not None else {}
    artifact_cache: dict[str, bytes] = {}
    manifest: list[dict[str, Any]] = []
    wanted_names = {"manifest.json"}

    for src_key, city in sorted(representatives.items()):
        if stop is not None and stop():
            return None
        filename = f"{src_key}.json"
        wanted_names.add(filename)
        shard_path = search_dir / filename
        records = load_records(state_dir, src_key)
        candidates = candidates_by_source[src_key]
        digest = _shard_hash(records, candidates, base_url)
        cached = cache_shards.get(src_key)
        if (
            isinstance(cached, dict)
            and cached.get("version") == SEARCH_CACHE_VERSION
            and cached.get("hash") == digest
            and shard_path.exists()
            and isinstance(cached.get("manifest"), dict)
        ):
            manifest.append(cached["manifest"])
            continue

        documents: list[dict[str, Any]] = []
        for record in records.values():
            if stop is not None and stop():
                return None
            document = _record_to_document(
                _city_for_record(candidates, record),
                record,
                base_url=base_url,
                storage=storage,
                artifact_cache=artifact_cache,
            )
            if document:
                documents.append(document)
        documents.sort(key=lambda document: (document["date"], document["uid"]), reverse=True)
        coverage = _coverage(documents)
        transcript_coverage_pct = (
            round(100 * coverage["transcript_episode_count"] / coverage["episode_count"], 1)
            if coverage["episode_count"]
            else 0.0
        )
        body_coverage = {
            body: _coverage(document for document in documents if document["body"] == body)
            for body in sorted({document["body"] for document in documents if document["body"]})
        }
        city_key = city.city_entity or city.slug
        city_label = city.podcast_author or city.city_entity or city.slug
        shard = {
            "schema_version": SEARCH_SCHEMA_VERSION,
            "source_key": src_key,
            "city": city_key,
            "city_label": city_label,
            "body": city.source.get("body") or city.podcast_title,
            "fields": list(SEARCH_FIELDS),
            "documents": documents,
        }
        shard_bytes = (json.dumps(shard, indent=2, sort_keys=True) + "\n").encode()
        shard_path.write_bytes(shard_bytes)
        gzip_bytes = len(gzip.compress(shard_bytes))
        size_warning = (
            "hard"
            if gzip_bytes > SHARD_HARD_GZIP_BYTES
            else "soft"
            if gzip_bytes > SHARD_SOFT_GZIP_BYTES
            else None
        )
        entry = {
            "source_key": src_key,
            "city": city_key,
            "city_label": city_label,
            "body": shard["body"],
            "bodies": sorted({document["body"] for document in documents if document["body"]}),
            "tags": sorted({tag for document in documents for tag in document["tags"]}),
            "shard_url": f"{base_url.rstrip('/')}/data/search/{filename}",
            **coverage,
            "transcript_coverage_pct": transcript_coverage_pct,
            "body_coverage": body_coverage,
            "shard_bytes": len(shard_bytes),
            "shard_gzip_bytes": gzip_bytes,
            "size_warning": size_warning,
        }
        manifest.append(entry)
        cache_shards[src_key] = {"version": SEARCH_CACHE_VERSION, "hash": digest, "manifest": entry}

    # Cached docs survive deploys.  Remove deleted-source shards and their cache entries so neither
    # browser search nor the next cache hit can serve a retired feed.
    for shard_path in search_dir.glob("*.json"):
        if shard_path.name not in wanted_names:
            shard_path.unlink()
    for src_key in list(cache_shards):
        if src_key not in representatives:
            del cache_shards[src_key]

    manifest.sort(key=lambda shard: (shard["city_label"].casefold(), shard["source_key"]))
    manifest_payload = {
        "schema_version": SEARCH_SCHEMA_VERSION,
        "fields": list(SEARCH_FIELDS),
        "shards": manifest,
        "warnings": [
            {"source_key": shard["source_key"], "size_warning": shard["size_warning"]}
            for shard in manifest
            if shard["size_warning"]
        ],
    }
    (search_dir / "manifest.json").write_text(
        json.dumps(manifest_payload, indent=2, sort_keys=True) + "\n"
    )
    _write_search_asset(output_dir)
    return manifest_payload
