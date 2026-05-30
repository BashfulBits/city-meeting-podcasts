"""Build orchestration: change detection, concurrency, and writing docs/."""

from __future__ import annotations

import collections
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from citypods.bodies import filter_by_body
from citypods.config import load_city_configs, load_site_config
from citypods.feeds import build_rss, has_items
from citypods.media import (
    CommandFfmpeg,
    FfmpegRunner,
    GlobalBudget,
    _source_key,
    load_manifest,
    materialize_audio,
    save_manifest,
)
from citypods.models import ChangeToken, City
from citypods.providers import get_provider
from citypods.providers.base import ProviderError
from citypods.site import render_city_page, render_index
from citypods.storage import make_storage
from citypods.storage.base import StorageBackend

ETAG_CACHE_NAME = ".feed_etags.json"


class FetchCache:
    """Fetch each distinct source once per build and share it across its per-board feeds.

    Without this, N per-board feeds of one city would each re-fetch the same (often large)
    source page concurrently and hammer/time-out the server. Fetches are serialized per
    source key; different sources still fetch in parallel.
    """

    def __init__(self):
        self._data: dict[str, list] = {}
        self._locks: dict[str, threading.Lock] = collections.defaultdict(threading.Lock)
        self._guard = threading.Lock()

    def get_or_fetch(self, key: str, fetch):
        with self._guard:
            lock = self._locks[key]
        with lock:
            if key not in self._data:
                self._data[key] = fetch()
            return self._data[key]


@dataclass
class CityResult:
    slug: str
    status: str  # "built" | "skipped" | "error"
    episode_count: int = 0
    detail: str = ""


def _token_to_dict(token: ChangeToken | None) -> dict:
    if token is None:
        return {}
    return {
        "etag": token.etag,
        "last_modified": token.last_modified,
        "content_hash": token.content_hash,
    }


def load_etag_cache(output_dir: Path) -> dict:
    path = output_dir / ETAG_CACHE_NAME
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save_etag_cache(output_dir: Path, cache: dict) -> None:
    path = output_dir / ETAG_CACHE_NAME
    path.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n")


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
    storage: StorageBackend | None,
    ffmpeg: FfmpegRunner,
    budget: int,
    global_budget: GlobalBudget | None,
    fetch_cache: FetchCache,
) -> tuple[CityResult, dict | None]:
    """Returns the result and the new cache entry (or None to leave unchanged)."""
    if request_delay:
        time.sleep(request_delay)
    provider = get_provider(city.provider)

    try:
        token = provider.detect_change(city.source)
    except ProviderError as exc:
        return CityResult(city.slug, "error", detail=str(exc)), None

    cached = cache.get(city.slug)
    if (
        token is not None
        and not token.is_empty()
        and cached == _token_to_dict(token)
        and _city_outputs_exist(output_dir, city.slug)
    ):
        return CityResult(city.slug, "skipped"), None

    try:
        # Shared across all per-board feeds of this source (fetched once per build).
        cached = fetch_cache.get_or_fetch(
            _source_key(city), lambda: provider.fetch_episodes(city.source)
        )
        episodes = list(cached)
    except ProviderError as exc:
        return CityResult(city.slug, "error", detail=str(exc)), None

    # Filter a mixed feed to one body/committee (per-board feeds), then cap to the most
    # recent max_episodes BEFORE materialization (a feed never shows more, and re-hosting
    # the deep archive is expensive).
    fetched = len(episodes)
    episodes = filter_by_body(episodes, city.source.get("body"))
    episodes.sort(key=lambda e: e.published, reverse=True)
    episodes = episodes[: city.max_episodes]
    detail = f"{fetched} fetched"
    if fetched > len(episodes):
        detail += f", {len(episodes)} after filter/cap"

    # Materialize hosted audio (HLS providers always; direct providers when extract_audio).
    # Skipped in dry-run (uploads are writes) and when no storage backend is available.
    if not dry_run and storage is not None:
        manifest = load_manifest(output_dir, city.slug)
        stats = materialize_audio(
            city,
            episodes,
            storage=storage,
            manifest=manifest,
            ffmpeg=ffmpeg,
            budget=budget,
            resolve_media_url=lambda ep: provider.resolve_media_url(ep, city.source),
            global_budget=global_budget,
        )
        save_manifest(output_dir, city.slug, manifest)
        if stats.hosted or stats.reused or stats.skipped_budget:
            detail += f", audio {stats.hosted}+{stats.reused} hosted"
            if stats.skipped_budget:
                detail += f", {stats.skipped_budget} queued"
        if stats.errors:
            detail += f", {len(stats.errors)} media errors"

    has_audio = has_items(episodes, "audio")
    has_video = has_items(episodes, "video")

    if not dry_run:
        city_dir = output_dir / city.slug
        city_dir.mkdir(parents=True, exist_ok=True)
        if has_audio:
            (city_dir / "audio_feed.xml").write_text(build_rss(city, episodes, "audio", base_url))
        if has_video:
            (city_dir / "video_feed.xml").write_text(build_rss(city, episodes, "video", base_url))
        else:
            (city_dir / "video_feed.xml").unlink(missing_ok=True)
        (city_dir / "index.html").write_text(
            render_city_page(
                city, base_url, len(episodes), has_audio=has_audio, has_video=has_video
            )
        )
        (city_dir / "meta.json").write_text(
            json.dumps(
                {
                    "slug": city.slug,
                    "episodes": len(episodes),
                    "has_audio": has_audio,
                    "has_video": has_video,
                },
                indent=2,
            )
            + "\n"
        )

    return (
        CityResult(city.slug, "built", episode_count=len(episodes), detail=detail),
        _token_to_dict(token),
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

    cache = load_etag_cache(output_dir)
    request_delay = float(site_config.get("request_delay_seconds", 0.1))
    max_workers = int(site_config.get("max_workers", 20))
    defaults = site_config.get("defaults", {})
    budget = int(defaults.get("materialize_budget_per_city", 5))
    total_budget = int(defaults.get("materialize_budget_per_run", 25))
    global_budget = GlobalBudget(total_budget) if total_budget > 0 else None
    storage = make_storage(site_config, base_url, output_dir)
    ffmpeg = ffmpeg or CommandFfmpeg(max_kbps=int(defaults.get("audio_max_kbps", 96)))
    fetch_cache = FetchCache()

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
                storage,
                ffmpeg,
                budget,
                global_budget,
                fetch_cache,
            )
            for c in cities
        ]
        for fut in futures:
            result, entry = fut.result()
            results.append(result)
            if entry is not None:
                cache[result.slug] = entry

    if not dry_run:
        save_etag_cache(output_dir, cache)
        all_cities = load_city_configs(cities_dir, site_config.get("defaults", {}))
        (output_dir / "index.html").write_text(render_index(all_cities, site_config, base_url))
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
