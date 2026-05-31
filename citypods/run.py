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
from citypods.bodies import filter_by_body
from citypods.config import load_city_configs, load_site_config
from citypods.feeds import build_rss, has_items
from citypods.media import CommandFfmpeg, FfmpegRunner, GlobalBudget
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
from citypods.site import (
    render_city_page,
    render_index,
    render_redirect_feed,
    render_redirect_page,
)
from citypods.stages import StageContext, default_stages, run_stages
from citypods.state import (
    build_fingerprint,
    load_etag_cache,
    resolve_state_dir,
    save_etag_cache,
)
from citypods.statesync import pull_state, push_state
from citypods.storage import make_storage


class SourcePipeline:
    """Fetch + enrich each distinct source once per build and share the result across all of
    its per-board feeds. Work is serialized per source key (so N board feeds don't re-fetch /
    re-enrich the same source concurrently); different sources still run in parallel.

    Enrichment runs an ordered list of stages (see citypods/stages.py); audio is the only
    built-in stage today, but transcript/summary/chapters/links slot in without touching this.
    """

    def __init__(self, *, state_dir: Path, stages, ctx: StageContext):
        self.state_dir = state_dir
        self.stages = stages
        self.ctx = ctx
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

            stats = run_stages(provider, city, episodes, self.stages, self.ctx)
            notes = [s.note() for s in stats if s.note()]
            if seeded:
                notes.append(f"{seeded} legacy")
            if not self.ctx.dry_run:
                save_records(
                    self.state_dir,
                    key,
                    {ep.uid: episode_to_record(ep) for ep in episodes if ep.uid},
                )
            self._cache[key] = episodes
            self._notes[key] = ", ".join(notes)
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
    fingerprint = build_fingerprint(base_url)
    request_delay = float(site_config.get("request_delay_seconds", 0.1))
    max_workers = int(site_config.get("max_workers", 20))
    defaults = site_config.get("defaults", {})
    per_source_budget = int(defaults.get("materialize_budget_per_city", 5))
    total_budget = int(defaults.get("materialize_budget_per_run", 25))
    # Chapter scrapes are one cheap page fetch each (no encode), so they can run ahead of the
    # audio re-encode budget; defaults to the same cap.
    chapters_budget = int(defaults.get("chapters_budget_per_run", total_budget))
    max_kbps = int(defaults.get("audio_max_kbps", 96))
    storage = make_storage(site_config, base_url, output_dir)

    # Restore the durable state snapshot from the bucket (canonical) before loading any state,
    # so a missing/evicted actions/cache self-heals instead of losing derived artifacts.
    if not dry_run:
        restored = pull_state(storage, state_dir)
        if restored:
            print(f"state: restored {restored} file(s) from durable storage")
    cache = load_etag_cache(state_dir)
    ffmpeg = ffmpeg or CommandFfmpeg(max_kbps=max_kbps)
    ctx = StageContext(
        storage=storage,
        ffmpeg=ffmpeg,
        max_kbps=max_kbps,
        per_source_budget=per_source_budget,
        dry_run=dry_run,
        budgets={
            "audio": GlobalBudget(total_budget) if total_budget > 0 else None,
            "chapters": GlobalBudget(chapters_budget) if chapters_budget > 0 else None,
        },
    )
    pipeline = SourcePipeline(state_dir=state_dir, stages=default_stages(), ctx=ctx)

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
        _write_aliases(output_dir, base_url, all_cities, feed_info)
        _write_cname(output_dir, site_config)
        _prune_stale_dirs(output_dir, all_cities)
        (output_dir / "meta.json").write_text(
            json.dumps(
                {"cities": len(all_cities), "built": sum(r.status == "built" for r in results)},
                indent=2,
            )
            + "\n"
        )
        # Persist the updated record store + cache back to durable storage. The bucket — not
        # actions/cache — is the source of truth for derived artifacts.
        pushed = push_state(storage, state_dir)
        if pushed:
            print(f"state: pushed {pushed} file(s) to durable storage")

    return results


# Top-level files/dirs the build owns directly; never pruned as a stale slug.
_RESERVED_DOC_NAMES = {"audio", "assets", "static", ".git"}


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
