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
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from citypods.artwork import render_cover
from citypods.bodies import filter_by_body
from citypods.config import load_city_configs, load_site_config
from citypods.feeds import build_rss, chapters_json, chapters_url, has_items
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
    merge_records,
    migrate_legacy_manifests,
    prune_archive,
    record_to_episode,
    save_records,
    source_key,
)
from citypods.security import SecurityError
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
from citypods.statesync import pull_state, push_state, reconcile_state
from citypods.storage import make_storage

# Retention caps for the append-only archive (issue #109). Deliberately set arbitrarily high:
# nothing is pruned in normal operation, but the lever exists so retention can be ratcheted down
# later (the admin/usage report shows the storage cost of keeping old recordings around).
DEFAULT_MAX_ARCHIVE_ITEMS = 5000
DEFAULT_MAX_ARCHIVE_AGE_YEARS = 1000.0


class SourcePipeline:
    """Fetch + enrich each distinct source once per build and share the result across all of
    its per-board feeds. Work is serialized per source key (so N board feeds don't re-fetch /
    re-enrich the same source concurrently); different sources still run in parallel.

    Enrichment runs an ordered list of stages (see citypods/stages.py); audio is the only
    built-in stage today, but transcript/summary/chapters/links slot in without touching this.
    """

    def __init__(
        self,
        *,
        state_dir: Path,
        stages,
        ctx: StageContext,
        max_archive_items: int = DEFAULT_MAX_ARCHIVE_ITEMS,
        max_archive_age_years: float = DEFAULT_MAX_ARCHIVE_AGE_YEARS,
    ):
        self.state_dir = state_dir
        self.stages = stages
        self.ctx = ctx
        self.max_archive_items = max_archive_items
        self.max_archive_age_years = max_archive_age_years
        self._cache: dict[str, list[Episode]] = {}
        self._notes: dict[str, str] = {}
        self._locks: dict[str, threading.Lock] = collections.defaultdict(threading.Lock)
        self._guard = threading.Lock()
        # Per-stage cost totals across all sources this run (for run history / projection).
        self.stage_totals: dict[str, dict] = collections.defaultdict(
            lambda: {
                "ran": 0,
                "reused": 0,
                "backlog": 0,
                "seconds": 0.0,
                "bytes": 0,
                "errors": 0,
                "error_samples": [],
            }
        )

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
            persisted = load_records(self.state_dir, key)
            merge_persisted(episodes, persisted)
            seeded = migrate_legacy_manifests(self.state_dir, episodes)

            stats = run_stages(provider, city, episodes, self.stages, self.ctx)
            notes = [s.note() for s in stats if s.note()]
            with self._guard:
                for s in stats:
                    t = self.stage_totals[s.name]
                    t["ran"] += s.ran
                    t["reused"] += s.reused
                    t["backlog"] += s.skipped
                    t["seconds"] += s.seconds
                    t["bytes"] += s.bytes_written
                    t["errors"] += len(s.errors)
                    if s.errors and len(t["error_samples"]) < 3:
                        t["error_samples"].extend(s.errors[: 3 - len(t["error_samples"])])
            if seeded:
                notes.append(f"{seeded} legacy")

            # Append-only archive (issue #109): merge this run's freshly-enriched records over
            # the persisted store (fresh wins on uid) instead of replacing it, so a meeting that
            # left the provider window keeps its record + audio. Bounded by the (high) retention
            # caps so it never grows truly unbounded.
            fresh = {ep.uid: episode_to_record(ep) for ep in episodes if ep.uid}
            combined = prune_archive(
                merge_records(persisted, fresh),
                max_items=self.max_archive_items,
                max_age_years=self.max_archive_age_years,
            )
            archived = len(combined) - len(fresh)
            if archived > 0:
                notes.append(f"{archived} archived")
            if not self.ctx.dry_run:
                save_records(self.state_dir, key, combined)

            # Render from the full archive: prefer this run's in-memory enriched Episode for a
            # uid, else rehydrate a persisted-only one (dropped from the provider window).
            fetched_by_uid = {ep.uid: ep for ep in episodes if ep.uid}
            archive = [
                fetched_by_uid.get(uid) or record_to_episode(rec) for uid, rec in combined.items()
            ]
            self._cache[key] = archive
            self._notes[key] = ", ".join(notes)
            return archive

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
    except (ProviderError, SecurityError) as exc:
        # SecurityError: a source/redirect URL was blocked by the SSRF gate (audit #S1) —
        # treat as a per-city error so one bad submission can't fail the whole build.
        return CityResult(city.slug, "error", detail=str(exc)), None

    # Filter the shared source archive to this feed's body, then cap to the most-recent
    # max_episodes (a feed never shows more; the archive itself retains far more — issue #109).
    archived = len(episodes)
    feed_eps = filter_by_body(episodes, city.source.get("body"))
    feed_eps.sort(key=lambda e: e.published, reverse=True)
    feed_eps = feed_eps[: city.max_episodes]
    detail = f"{archived} archived"
    if archived > len(feed_eps):
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
        _write_chapter_sidecars(city_dir, city, feed_eps, base_url)
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

    # Opt-in: derive per-run budgets from a wall-clock target so a run fills (most of) the 6h
    # GitHub Actions window instead of stopping at a flat count — this is what drains a large
    # backfill in days instead of months (see review/03). Disabled by default
    # (run_time_budget_minutes = 0) so behavior is unchanged unless turned on. Reads the measured
    # seconds/episode from restored run history when available, else a config estimate.
    time_budget_min = float(defaults.get("run_time_budget_minutes", 0))
    if time_budget_min > 0:
        safety = float(defaults.get("budget_safety", 0.8))
        window_s = time_budget_min * 60 * safety
        sec_ep = _measured_sec_per_ep(state_dir) or float(defaults.get("seconds_per_episode", 90))
        sec_chapter = float(defaults.get("seconds_per_chapter_fetch", 3))
        total_budget = max(1, int(window_s / max(sec_ep, 1e-9)))
        chapters_budget = max(1, int(window_s / max(sec_chapter, 1e-9)))
        print(
            f"budget: time-bounded ({time_budget_min:.0f}m × {safety}) → "
            f"audio {total_budget}/run @ {sec_ep:.0f}s/ep, chapters {chapters_budget}/run"
        )

    # Per-source cap as a fraction of the finalized global budget, not a magic constant: it only
    # exists to stop one source monopolizing a single run (e.g. Dallas's huge Swagit backlog
    # crowding out Denton), so it must self-scale with the global budget. A flat constant either
    # throttles a concentrated backlog far below the run's capacity (5/run when 24 are available)
    # or — if you divide the global budget evenly by source count — reserves capacity for sources
    # that have nothing to do. An explicit ``materialize_budget_per_city`` still overrides.
    source_fraction = float(defaults.get("materialize_source_fraction", 0.5))
    per_source_budget = int(
        defaults.get("materialize_budget_per_city")
        or max(1, math.ceil(total_budget * source_fraction))
    )
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
    pipeline = SourcePipeline(
        state_dir=state_dir,
        stages=default_stages(),
        ctx=ctx,
        max_archive_items=int(defaults.get("max_archive_items", DEFAULT_MAX_ARCHIVE_ITEMS)),
        max_archive_age_years=float(
            defaults.get("max_archive_age_years", DEFAULT_MAX_ARCHIVE_AGE_YEARS)
        ),
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

    # Per-stage activity for the run, to stdout (build logs). Makes "did audio actually
    # materialize?" answerable without the step-summary report: ``ran`` is newly produced this
    # run, ``reused`` already up to date, ``queued`` deferred by budget (remaining backlog), plus
    # error count and a few sample messages so a re-host that triggers but fails downstream is
    # visible rather than hiding behind the feed-level "0 errors" line.
    for name, t in sorted(pipeline.stage_totals.items()):
        if not (t["ran"] or t["reused"] or t["backlog"] or t["errors"]):
            continue
        print(
            f"{name}: {t['ran']} ran, {t['reused']} reused, {t['backlog']} queued, "
            f"{t['errors']} errors ({t['seconds']:.0f}s)"
        )
        for msg in t["error_samples"]:
            print(f"    ! {msg}")

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
        _record_run_history(state_dir, results, pipeline.stage_totals)
        # Persist the updated record store + cache back to durable storage. The bucket — not
        # actions/cache — is the source of truth for derived artifacts.
        pushed = push_state(storage, state_dir)
        if pushed:
            print(f"state: pushed {pushed} file(s) to durable storage")
        # Reap remote state objects with no local counterpart (e.g. records orphaned by a
        # source edit that changed source_key) so they don't leak or pin orphaned audio.
        reclaimed = reconcile_state(storage, state_dir)
        if reclaimed:
            print(f"state: reclaimed {reclaimed} stale remote file(s)")

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


def _write_chapter_sidecars(
    city_dir: Path, city: City, episodes: list[Episode], base_url: str
) -> None:
    """Write each chaptered episode's Podcasting 2.0 chapters JSON to
    ``<slug>/chapters/<uid>.json`` (referenced by ``<podcast:chapters>``), and prune sidecars
    for episodes that no longer have chapters. Hosted from Pages, so chapters surface even for
    direct enclosures we don't re-host."""
    chap_dir = city_dir / "chapters"
    wanted: set[str] = set()
    for ep in episodes:
        if chapters_url(city, ep, base_url):
            wanted.add(f"{ep.uid}.json")
            chap_dir.mkdir(parents=True, exist_ok=True)
            (chap_dir / f"{ep.uid}.json").write_text(chapters_json(ep))
    if chap_dir.exists():
        for stale in chap_dir.glob("*.json"):
            if stale.name not in wanted:
                stale.unlink()


RUN_HISTORY_NAME = "run_history.jsonl"
RUN_SUMMARY_NAME = "run_summary.json"
_RUN_HISTORY_KEEP = 1000  # cap the rolling log so the synced state stays small


def _record_run_history(state_dir: Path, results: list, stage_totals: dict) -> None:
    """Append one line to ``run_history.jsonl`` (rolling, capped) and write ``run_summary.json``
    (latest only). This is the data spine for the resource projection: it lets the model use a
    *measured* seconds/episode and per-stage backlog instead of defaults. Lives in the durable
    state (synced to the bucket), so trends survive cache eviction."""
    from datetime import UTC, datetime

    import citypods.records as _records  # avoid import cycle at module load

    stages = {
        name: {
            "ran": t["ran"],
            "reused": t["reused"],
            "backlog": t["backlog"],
            "seconds": round(t["seconds"], 1),
            "bytes": t["bytes"],
            "errors": t["errors"],
        }
        for name, t in stage_totals.items()
    }
    audio = stages.get("audio", {})
    summary = {
        "ts": datetime.now(UTC).isoformat(),
        "schema_version": _records.SCHEMA_VERSION,
        "cities": len(results),
        "built": sum(r.status == "built" for r in results),
        "skipped": sum(r.status == "skipped" for r in results),
        "errors": sum(r.status == "error" for r in results),
        # convenience keys the projection's measured_inputs() reads to calibrate sec/ep
        "materialized": audio.get("ran", 0),
        "materialize_seconds": audio.get("seconds", 0.0),
        "stages": stages,
    }
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / RUN_SUMMARY_NAME).write_text(json.dumps(summary, indent=2) + "\n")

    path = state_dir / RUN_HISTORY_NAME
    lines = path.read_text().splitlines() if path.exists() else []
    lines.append(json.dumps(summary))
    path.write_text("\n".join(lines[-_RUN_HISTORY_KEEP:]) + "\n")


def _measured_sec_per_ep(state_dir: Path) -> float | None:
    """Observed seconds per materialized episode from the last run summary, if present and
    meaningful. Used to size the time-bounded budget; None falls back to a config estimate.
    (``run_summary.json`` is written by a future run-history change; absent today → None.)"""
    path = Path(state_dir) / "run_summary.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        eps = data.get("materialized", 0)
        secs = data.get("materialize_seconds", 0.0)
        if eps and secs:
            return secs / eps
    except (OSError, ValueError):
        pass
    return None


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
