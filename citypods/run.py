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
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from citypods.artwork import render_cover
from citypods.bodies import body_key, canonical_body, filter_by_body
from citypods.config import load_city_configs, load_site_config
from citypods.feeds import build_rss, has_items
from citypods.media import CommandFfmpeg, FfmpegRunner, GlobalBudget, materialize_audio
from citypods.models import City, Episode
from citypods.providers import get_provider
from citypods.providers.base import ProviderError
from citypods.records import (
    assign_uids,
    episode_to_record,
    feed_content_hash,
    load_records,
    merge_persisted,
    migrate_legacy_manifests,
    save_records,
    source_key,
)
from citypods.site import render_city_page, render_index
from citypods.state import (
    build_fingerprint,
    load_etag_cache,
    resolve_state_dir,
    save_etag_cache,
)
from citypods.storage import make_storage
from citypods.storage.base import StorageBackend


def _materialize_set(episodes: list[Episode], max_per_body: int) -> list[Episode]:
    """The subset worth hosting: the most-recent ``max_per_body`` per body. Every per-board
    feed shows at most that many of its body, and the combined feed is a subset of the union,
    so this hosts exactly what some feed can display — never the deep archive."""
    by_body: dict[str, list[Episode]] = collections.defaultdict(list)
    for ep in episodes:
        by_body[body_key(canonical_body(ep.body or ""))].append(ep)
    out: list[Episode] = []
    for eps in by_body.values():
        eps.sort(key=lambda e: e.published, reverse=True)
        out.extend(eps[:max_per_body])
    return out


class SourcePipeline:
    """Fetch + enrich each distinct source once per build and share the result across all of
    its per-board feeds. Work is serialized per source key (so N board feeds don't re-fetch /
    re-materialize the same source concurrently); different sources still run in parallel.
    """

    def __init__(
        self,
        *,
        state_dir: Path,
        storage: StorageBackend | None,
        ffmpeg: FfmpegRunner,
        max_kbps: int,
        per_source_budget: int,
        global_budget: GlobalBudget | None,
        dry_run: bool,
    ):
        self.state_dir = state_dir
        self.storage = storage
        self.ffmpeg = ffmpeg
        self.max_kbps = max_kbps
        self.per_source_budget = per_source_budget
        self.global_budget = global_budget
        self.dry_run = dry_run
        self._cache: dict[str, list[Episode]] = {}
        self._notes: dict[str, str] = {}
        self._locks: dict[str, threading.Lock] = collections.defaultdict(threading.Lock)
        self._guard = threading.Lock()

    def enrich(self, city: City) -> list[Episode]:
        key = source_key(city)
        with self._guard:
            lock = self._locks[key]
        with lock:
            if key in self._cache:
                return self._cache[key]
            provider = get_provider(city.provider)
            episodes = provider.fetch_episodes(city.source)  # ProviderError propagates
            assign_uids(city, episodes)
            merge_persisted(episodes, load_records(self.state_dir, key))
            seeded = migrate_legacy_manifests(self.state_dir, episodes)

            note = ""
            if not self.dry_run and self.storage is not None:
                stats = materialize_audio(
                    city,
                    _materialize_set(episodes, city.max_episodes),
                    storage=self.storage,
                    ffmpeg=self.ffmpeg,
                    budget=self.per_source_budget,
                    max_kbps=self.max_kbps,
                    resolve_media_url=lambda ep: provider.resolve_media_url(ep, city.source),
                    global_budget=self.global_budget,
                )
                if stats.hosted or stats.reused or stats.skipped_budget:
                    note = f"audio {stats.hosted}+{stats.reused} hosted"
                    if stats.skipped_budget:
                        note += f", {stats.skipped_budget} queued"
                if seeded:
                    note += f", {seeded} legacy"
                if stats.errors:
                    note += f", {len(stats.errors)} media errors"
            if not self.dry_run:
                save_records(
                    self.state_dir,
                    key,
                    {ep.uid: episode_to_record(ep) for ep in episodes if ep.uid},
                )
            self._cache[key] = episodes
            self._notes[key] = note
            return episodes

    def note(self, city: City) -> str:
        return self._notes.get(source_key(city), "")


@dataclass
class CityResult:
    slug: str
    status: str  # "built" | "skipped" | "error"
    episode_count: int = 0
    detail: str = ""
    has_audio: bool = True
    has_video: bool = False


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
) -> tuple[CityResult, dict | None]:
    """Returns the result and the new cache entry (or None to leave unchanged)."""
    if request_delay:
        time.sleep(request_delay)

    try:
        episodes = pipeline.enrich(city)
    except ProviderError as exc:
        return CityResult(city.slug, "error", detail=str(exc)), None

    # Filter the shared source episodes to this feed's body, then cap to the most-recent
    # max_episodes (a feed never shows more).
    fetched = len(episodes)
    feed_eps = filter_by_body(episodes, city.source.get("body"))
    feed_eps.sort(key=lambda e: e.published, reverse=True)
    feed_eps = feed_eps[: city.max_episodes]
    detail = f"{fetched} fetched"
    if fetched > len(feed_eps):
        detail += f", {len(feed_eps)} after filter/cap"
    note = pipeline.note(city)
    if note:
        detail += f", {note}"

    has_audio = has_items(feed_eps, "audio")
    has_video = has_items(feed_eps, "video")

    # feed_content_hash drives the re-render skip: hash the render-relevant fields + build
    # fingerprint. Unchanged + outputs present -> skip the (re-)render entirely. (Audio is a
    # separate concern, gated by audio_spec_hash inside materialize_audio.)
    content_hash = feed_content_hash(feed_eps, fingerprint)
    new_entry = {"content_hash": content_hash}
    cache_entry = cache.get(city.slug)
    if (
        not dry_run
        and cache_entry is not None
        and cache_entry.get("content_hash") == content_hash
        and _city_outputs_exist(output_dir, city.slug)
    ):
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

    if not dry_run:
        city_dir = output_dir / city.slug
        city_dir.mkdir(parents=True, exist_ok=True)
        if has_audio:
            (city_dir / "audio_feed.xml").write_text(build_rss(city, feed_eps, "audio", base_url))
        if has_video:
            (city_dir / "video_feed.xml").write_text(build_rss(city, feed_eps, "video", base_url))
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


def build(
    *,
    site_config_path: str | Path = "site_config.yml",
    cities_dir: str | Path = "cities",
    output_dir: str | Path = "docs",
    base_url: str | None = None,
    only_slug: str | None = None,
    dry_run: bool = False,
    ffmpeg: FfmpegRunner | None = None,
) -> list[CityResult]:
    site_config = load_site_config(site_config_path)
    cities = load_city_configs(cities_dir, site_config.get("defaults", {}))
    if only_slug:
        cities = [c for c in cities if c.slug == only_slug]
        if not cities:
            raise ValueError(f"no city with slug {only_slug!r}")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    base_url = _resolve_base_url(base_url, site_config)

    state_dir = resolve_state_dir(site_config, output_dir)
    cache = load_etag_cache(state_dir)
    fingerprint = build_fingerprint(base_url)
    request_delay = float(site_config.get("request_delay_seconds", 0.1))
    max_workers = int(site_config.get("max_workers", 20))
    defaults = site_config.get("defaults", {})
    per_source_budget = int(defaults.get("materialize_budget_per_city", 5))
    total_budget = int(defaults.get("materialize_budget_per_run", 25))
    global_budget = GlobalBudget(total_budget) if total_budget > 0 else None
    max_kbps = int(defaults.get("audio_max_kbps", 96))
    storage = make_storage(site_config, base_url, output_dir)
    ffmpeg = ffmpeg or CommandFfmpeg(max_kbps=max_kbps)
    pipeline = SourcePipeline(
        state_dir=state_dir,
        storage=storage,
        ffmpeg=ffmpeg,
        max_kbps=max_kbps,
        per_source_budget=per_source_budget,
        global_budget=global_budget,
        dry_run=dry_run,
    )

    results: list[CityResult] = []
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
            )
            for c in cities
        ]
        for fut in futures:
            result, entry = fut.result()
            results.append(result)
            if entry is not None:
                cache[result.slug] = entry

    if not dry_run:
        save_etag_cache(state_dir, cache)
        all_cities = load_city_configs(cities_dir, site_config.get("defaults", {}))
        feed_info = {r.slug: {"has_audio": r.has_audio, "has_video": r.has_video} for r in results}
        (output_dir / "index.html").write_text(
            render_index(all_cities, site_config, base_url, feed_info)
        )
        _write_cname(output_dir, site_config)
        (output_dir / "meta.json").write_text(
            json.dumps(
                {"cities": len(all_cities), "built": sum(r.status == "built" for r in results)},
                indent=2,
            )
            + "\n"
        )

    return results


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
