"""Build the resource report: machine JSON, a Markdown summary (GitHub job summary), and a
static what-if admin page whose calculator runs client-side.

Pure-ish: ``build_report`` derives inputs from the city list + (optional) persisted state and
runs ``projection``. ``to_markdown`` / ``to_admin_html`` render it. No network.
"""

from __future__ import annotations

import json
from pathlib import Path

from citypods.projection import (
    ModelInputs,
    archived_per_feed,
    at_scale,
    measured_inputs,
    project,
    savings_if_capped,
)

# Providers whose audio we always host (ephemeral/HLS) vs. direct providers (hosted only when a
# city sets extract_audio). Mirrors media._should_host.
_HLS_PROVIDERS = {"civicplus", "swagit"}
SCALE_SCENARIOS = (200, 500, 1000, 5000)
_ADMIN_TEMPLATE = Path(__file__).resolve().parent / "assets" / "admin.html"
_STATUS_TEMPLATE = Path(__file__).resolve().parent / "assets" / "status.html"


def _hosted_fraction(cities: list) -> float:
    if not cities:
        return 1.0
    hosted = sum(
        1 for c in cities if c.provider in _HLS_PROVIDERS or getattr(c, "extract_audio", False)
    )
    return hosted / len(cities)


def _truncation_stats(cities: list, state_dir: Path | None) -> dict:
    """How many feeds are showing fewer episodes than they have archived.

    A feed is "truncated" when its archive holds more episodes than ``max_episodes`` allows the
    RSS feed to render. With the append-only archive this is expected and healthy — it just means
    the feed cap is the binding limit, not the amount of content we have.
    """
    if not state_dir or not cities:
        return {"checked": 0, "truncated": 0, "max_gap": 0, "examples": []}

    from citypods.bodies import matches
    from citypods.records import load_records, source_key

    truncated = 0
    max_gap = 0
    examples: list[str] = []
    checked = 0
    # The record store is shared across every body on the same source (``source_key`` ignores the
    # per-board ``body`` filter), so it holds *all* bodies' episodes. Each feed truncates to its own
    # body, so count per feed against that body's slice — not the whole shared archive (issue: the
    # admin panel reported every Denton/Dallas board as having thousands of "hidden" episodes).
    cache: dict[str, dict] = {}
    for city in cities:
        key = source_key(city)
        if key not in cache:
            cache[key] = load_records(Path(state_dir), key)
        body = city.source.get("body")
        archived = sum(1 for r in cache[key].values() if not body or matches(r.get("body"), body))
        if archived == 0:
            continue
        checked += 1
        cap = city.max_episodes
        gap = archived - cap
        if gap > 0:
            truncated += 1
            max_gap = max(max_gap, gap)
            if len(examples) < 3:
                examples.append(f"{city.slug} ({archived} archived, {cap} shown)")

    return {"checked": checked, "truncated": truncated, "max_gap": max_gap, "examples": examples}


def _audio_failure_stats(cities: list, state_dir: Path | None) -> dict:
    """Project-wide count of episodes that can't be materialized, split by category (issue #120).

    ``deferred`` = MEDIA_DEFERRED (in backoff, will retry); ``dead`` = no usable media (MEDIA_DEAD).
    Counted once per source (per-body feeds share a record store)."""
    empty = {"deferred": 0, "dead": 0, "examples": []}
    if not state_dir or not cities:
        return empty

    from citypods.audit import count_audio_failures
    from citypods.records import load_records, source_key

    deferred = dead = 0
    examples: list[str] = []
    seen: set[str] = set()
    for city in cities:
        key = source_key(city)
        if key in seen:
            continue
        seen.add(key)
        records = load_records(Path(state_dir), key)
        d, x = count_audio_failures(records)
        deferred += d
        dead += x
        if x and len(examples) < 3:
            examples.append(f"{city.slug} ({x} dead)")
    return {"deferred": deferred, "dead": dead, "examples": examples}


def _measured_archive_items(cities: list, state_dir: Path | None) -> int | None:
    """Average records retained **per feed** from the append-only archive — the correct input
    for the projection model, which multiplies by number of feeds to estimate total storage.

    Multiple city configs (feeds) can share a single source key (e.g. all per-board feeds of a
    city share one ``episodes.json``). Averaging raw per-source counts and then multiplying by
    feed count would overstate storage. Instead we sum records across all unique source keys
    (= total unique audio files we store) and divide by the number of feeds, so the model's
    ``storage_gb = hosted_feeds × archive_items × gb_per_ep`` gives the correct total.

    Returns None when no state exists, so the projection falls back to the render cap.
    """
    if not state_dir or not cities:
        return None

    from citypods.records import load_records, source_key

    # Sum records across each unique source key exactly once.
    seen: set[str] = set()
    total_records = 0
    for city in cities:
        key = source_key(city)
        if key in seen:
            continue
        seen.add(key)
        records = load_records(Path(state_dir), key)
        total_records += len(records)

    if not seen:
        return None
    # Divide by number of feeds (not sources) — the projection multiplies by feeds.
    return round(total_records / len(cities))


def _load_run_history(state_dir: Path) -> list[dict]:
    path = Path(state_dir) / "run_history.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def build_report(cities: list, *, site_config: dict, state_dir: Path | None = None) -> dict:
    defaults = site_config.get("defaults", {})
    cap_raw = defaults.get("materialize_budget_per_run")
    base = ModelInputs(
        episodes_per_feed=int(defaults.get("max_episodes", 50)),
        kbps=int(defaults.get("audio_max_kbps", 96)),
        per_run_cap=int(cap_raw) if cap_raw is not None else None,
    )
    history = _load_run_history(state_dir) if state_dir else []
    inputs = measured_inputs(
        cities,
        run_history=history,
        hosted_feeds=round(_hosted_fraction(cities) * len(cities)) if cities else None,
        archive_items=_measured_archive_items(cities, state_dir),
        base=base,
    )
    current = project(inputs)
    scenarios = {str(f): at_scale(inputs, f).as_dict() for f in SCALE_SCENARIOS}

    # Retention what-if (issue #109): how much B2 $/mo ratcheting the archive cap down would free.
    retained = archived_per_feed(inputs)
    candidates = [c for c in (50, 100, 250, 500, 1000, 2000) if c < retained]
    retention = {
        "max_archive_items": int(defaults.get("max_archive_items", 5000)),
        "max_archive_age_years": int(defaults.get("max_archive_age_years", 1000)),
        "retained_per_feed": retained,
        "savings": [savings_if_capped(inputs, c) for c in candidates],
    }

    # Feed truncation: how many sources have more archived episodes than max_episodes renders.
    truncation = _truncation_stats(cities, state_dir)

    # Un-materializable audio tally (#120): deferred (in backoff) vs dead (no usable media).
    audio_failures = _audio_failure_stats(cities, state_dir)

    # "host all audio" = host_frac 1.0 regardless of provider
    host_all_inputs = ModelInputs(**{**inputs.__dict__, "host_frac": 1.0})
    host_all = project(host_all_inputs)

    # time-bounded (drop the per-run cap) — what the model recommends
    time_bound = project(ModelInputs(**{**inputs.__dict__, "per_run_cap": None}))

    return {
        "generated_for_feeds": len(cities),
        "current": current.as_dict(),
        "host_all_audio": host_all.as_dict(),
        "time_bounded": time_bound.as_dict(),
        "scale_scenarios": scenarios,
        "retention": retention,
        "truncation": truncation,
        "audio_failures": audio_failures,
        "notes": {
            "b2_free_gb": 10,
            "b2_usd_per_gb_month": 0.006,
            "egress": "free via Cloudflare Bandwidth Alliance / R2 native",
            "calibrated": bool(history),
        },
    }


def to_markdown(report: dict) -> str:
    c = report["current"]
    tb = report["time_bounded"]
    lines = [
        "## 📊 Resource projection",
        "",
        f"- **Feeds:** {report['generated_for_feeds']}",
        f"- **Audio stored:** {c['storage_gb']:.1f} GB → **${c['monthly_cost_usd']:.2f}/mo** (B2)",
        f"- **Per-run throughput:** {c['per_run_throughput']} episodes "
        f"(time-budget could do {c['time_bound_throughput']})",
        f"- **Capacity:** {c['capacity_per_day']}/day vs inflow {c['inflow_per_day']:.0f}/day "
        f"→ {'✅ keeps up' if c['keeps_up'] else '⚠️ falling behind'}",
        f"- **Full backfill:** {c['full_backfill_episodes']} eps → "
        f"{c['full_backfill_days']:.0f} days at current budget",
    ]
    if c["cap_is_bottleneck"]:
        lines.append(
            f"- ⚠️ **The per-run cap ({c['inputs']['per_run_cap']}) is the bottleneck.** "
            f"Time-bounded would do **{tb['per_run_throughput']}/run** "
            f"({tb['capacity_per_day']}/day) → full backfill in "
            f"**{tb['full_backfill_days']:.0f} days** instead of {c['full_backfill_days']:.0f}. "
            f"Remove the cap (delete `materialize_budget_per_run`) to use the wall-clock window."
        )
    ret = report.get("retention")
    if ret:
        line = (
            f"- **Archive retention:** ~{ret['retained_per_feed']} recordings/feed "
            f"(total÷feeds; cap {ret['max_archive_items']}, ≤{ret['max_archive_age_years']}y)"
        )
        if ret["savings"]:
            best = max(ret["savings"], key=lambda s: s["monthly_cost_delta"])
            line += (
                f" — capping at {best['candidate_items']}/feed would free "
                f"{best['storage_gb_freed']:.0f} GB (**${best['monthly_cost_delta']:.2f}/mo**)"
            )
        else:
            line += " — nothing to reclaim at current volume"
        lines.append(line)
    trunc = report.get("truncation")
    if trunc and trunc.get("checked", 0) > 0:
        if trunc["truncated"] == 0:
            lines.append(
                f"- **Feed cap (`max_episodes`):** no feeds truncated "
                f"({trunc['checked']} sources checked, all within cap)"
            )
        else:
            ex = f" e.g. {trunc['examples'][0]}" if trunc.get("examples") else ""
            lines.append(
                f"- ⚠️ **Feed cap (`max_episodes`):** {trunc['truncated']} of {trunc['checked']} "
                f"sources truncated (up to {trunc['max_gap']} episodes hidden){ex}"
            )
    af = report.get("audio_failures")
    if af and (af["deferred"] or af["dead"]):
        ex = f" e.g. {af['examples'][0]}" if af.get("examples") else ""
        icon = "⚠️ " if af["dead"] else ""
        lines.append(
            f"- {icon}**Un-materializable audio:** {af['dead']} dead (no usable media){ex}, "
            f"{af['deferred']} deferred (MEDIA_DEFERRED; in backoff, will retry)"
        )

    lines += ["", "### At scale (storage / month)", "", "| Feeds | TB | $/mo |", "|--:|--:|--:|"]
    for f in ("200", "500", "1000", "5000"):
        s = report["scale_scenarios"][f]
        lines.append(f"| {f} | {s['storage_gb'] / 1000:.2f} | ${s['monthly_cost_usd']:.2f} |")
    lines += ["", "_Open `/admin/` for the interactive what-if calculator._", ""]
    return "\n".join(lines)


def _classify_record(rec: dict, max_kbps: int, loudness_profile: str = "") -> str:
    """Return the pipeline state for one record (mutually exclusive taxonomy from issue #124).

    States (in order of precedence):
      served         — hosted audio exists and spec matches current desired spec (or "legacy")
      stale          — hosted audio exists but spec no longer matches (re-encode queued)
      linked_video   — direct provider MP4 link; we never host this episode's audio
      deferred       — MEDIA_DEFERRED (in materialization backoff, will retry)
      dead           — no usable media (#120)
      transient_error— last attempt failed for an uncategorized reason (in exponential backoff)
      pending        — HLS episode, no enclosure yet, never attempted
    """
    from citypods.records import audio_spec_hash as _spec_hash
    from citypods.records import record_to_episode

    audio = rec.get("audio") or {}
    hosted_url = audio.get("url")
    spec_hash = audio.get("spec_hash")
    error = audio.get("error")
    media_kind = rec.get("media_kind", "direct")

    if hosted_url:
        if spec_hash in ("legacy", None):
            return "served"
        ep = record_to_episode(rec)
        return (
            "served"
            if spec_hash == _spec_hash(ep, max_kbps=max_kbps, loudness_profile=loudness_profile)
            else "stale"
        )

    if media_kind == "direct":
        return "linked_video"
    if error == "deferred":
        return "deferred"
    if error == "dead":
        return "dead"
    if error is not None:
        return "transient_error"
    return "pending"


def _feed_row(city, records: dict, *, max_kbps: int, loudness_profile: str = "") -> dict:
    """Aggregate per-episode stats for one feed (city config), filtered by body where applicable."""
    from citypods.bodies import matches

    body = city.source.get("body")
    episodes = hosted = linked_video = served = stale = 0
    pending = deferred = dead = transient_errors = 0
    hours_hosted = hours_linked = gb_stored = 0.0
    gb_exact = True
    last_pub = None
    tx_synced = tx_text = tx_none = 0

    for rec in records.values():
        if body and not matches(rec.get("body"), body):
            continue
        episodes += 1

        pub = rec.get("published")
        if pub:
            try:
                from datetime import datetime

                dt = datetime.fromisoformat(pub)
                if last_pub is None or dt > last_pub:
                    last_pub = dt
            except ValueError:
                pass

        audio = rec.get("audio") or {}
        duration_s = rec.get("duration") or audio.get("duration_served") or 0
        # Bytes-based estimate for providers (e.g. Swagit) that never supply a duration and
        # whose reuse path skips the ffprobe that would set duration_served.
        if not duration_s and audio.get("bytes"):
            duration_s = audio["bytes"] * 8 / (max_kbps * 1000)
        state = _classify_record(rec, max_kbps, loudness_profile=loudness_profile)

        if state in ("served", "stale"):
            hosted += 1
            hours_hosted += duration_s / 3600
            raw_bytes = audio.get("bytes")
            if raw_bytes is not None:
                gb_stored += raw_bytes / 1e9
            else:
                gb_exact = False
            if state == "served":
                served += 1
            else:
                stale += 1
        elif state == "linked_video":
            linked_video += 1
            hours_linked += duration_s / 3600
        elif state == "deferred":
            deferred += 1
        elif state == "dead":
            dead += 1
        elif state == "transient_error":
            transient_errors += 1
        else:
            pending += 1

        t = rec.get("transcript") or {}
        if t.get("synced"):
            tx_synced += 1
        elif t.get("key"):
            tx_text += 1
        else:
            tx_none += 1

    if dead > 0 or transient_errors > 0:
        health = "error"
    elif deferred > 0 or stale > 0 or pending > 0:
        health = "warn"
    else:
        health = "ok"

    return {
        "slug": city.slug,
        "body": city.source.get("body"),
        "provider": city.provider,
        "podcast_author": city.podcast_author,
        "state": city.state,
        "episodes": episodes,
        "hosted": hosted,
        "linked_video": linked_video,
        "served": served,
        "stale": stale,
        "pending": pending,
        "deferred": deferred,
        "dead": dead,
        "transient_errors": transient_errors,
        "hours_hosted": round(hours_hosted, 2),
        "hours_linked": round(hours_linked, 2),
        "gb_stored": round(gb_stored, 4),
        "gb_exact": gb_exact,
        "last_published": last_pub.date().isoformat() if last_pub else None,
        "health": health,
        "tx_synced": tx_synced,
        "tx_text": tx_text,
        "tx_none": tx_none,
    }


def _city_rows(feed_rows: list[dict]) -> list[dict]:
    """Roll up per-feed rows by ``podcast_author`` (the city grouping key)."""
    from collections import defaultdict

    buckets: dict[str, list] = defaultdict(list)
    for row in feed_rows:
        buckets[row["podcast_author"]].append(row)

    rows = []
    for author, feeds in sorted(buckets.items()):
        gb = sum(f["gb_stored"] for f in feeds)
        providers = sorted({f["provider"] for f in feeds})
        pub_dates = [f["last_published"] for f in feeds if f["last_published"]]
        last_published = max(pub_dates) if pub_dates else None
        rows.append(
            {
                "city": author,
                "state": feeds[0]["state"],
                "feeds": len(feeds),
                "provider": providers[0] if len(providers) == 1 else ", ".join(providers),
                "episodes": sum(f["episodes"] for f in feeds),
                "hosted": sum(f["hosted"] for f in feeds),
                "linked_video": sum(f["linked_video"] for f in feeds),
                "served": sum(f["served"] for f in feeds),
                "stale": sum(f["stale"] for f in feeds),
                "pending": sum(f["pending"] for f in feeds),
                "deferred": sum(f["deferred"] for f in feeds),
                "dead": sum(f["dead"] for f in feeds),
                "transient_errors": sum(f["transient_errors"] for f in feeds),
                "hours_hosted": round(sum(f["hours_hosted"] for f in feeds), 2),
                "hours_linked": round(sum(f["hours_linked"] for f in feeds), 2),
                "gb_stored": round(gb, 4),
                "gb_exact": all(f["gb_exact"] for f in feeds),
                "last_published": last_published,
                "health_ok": sum(1 for f in feeds if f["health"] == "ok"),
                "health_warn": sum(1 for f in feeds if f["health"] == "warn"),
                "health_error": sum(1 for f in feeds if f["health"] == "error"),
                "tx_synced": sum(f["tx_synced"] for f in feeds),
                "tx_text": sum(f["tx_text"] for f in feeds),
                "tx_none": sum(f["tx_none"] for f in feeds),
            }
        )
    return rows


def _load_run_summary(state_dir: Path) -> dict:
    path = state_dir / "run_summary.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def build_status(cities: list, *, site_config: dict, state_dir: Path | None = None) -> dict:
    """Build the operational status snapshot for the /admin/status/ dashboard (issue #124).

    Classifies every archived episode into the taxonomy from the issue (served, stale, pending,
    deferred, dead, transient_error, linked_video), aggregates per-feed and per-city, and stitches
    in run history and projection model output. Pure: reads the local state files, no network.
    """
    import os
    from datetime import UTC, datetime

    from citypods.projection import (
        B2_FREE_GB,
        B2_USD_PER_GB_MONTH,
        gb_per_episode,
        measured_inputs,
        project,
    )
    from citypods.records import load_records, referenced_audio_keys, source_key

    now = datetime.now(UTC)
    defaults = site_config.get("defaults", {})
    max_kbps = int(defaults.get("audio_max_kbps", 96))
    loudness_profile = str(defaults.get("audio_loudness_profile", ""))
    avg_duration_h = 2.0

    records_cache: dict[str, dict] = {}
    if state_dir:
        seen: set[str] = set()
        for city in cities:
            key = source_key(city)
            if key not in seen:
                seen.add(key)
                records_cache[key] = load_records(Path(state_dir), key)

    # Per-feed rows
    feed_rows: list[dict] = []
    for city in cities:
        key = source_key(city) if state_dir else ""
        recs = records_cache.get(key, {})
        feed_rows.append(
            _feed_row(city, recs, max_kbps=max_kbps, loudness_profile=loudness_profile)
        )

    city_rows = _city_rows(feed_rows)

    # KPI totals
    meetings_archived = sum(r["episodes"] for r in feed_rows)
    hosted_audio = sum(r["hosted"] for r in feed_rows)
    linked_video = sum(r["linked_video"] for r in feed_rows)
    hours_hosted = round(sum(r["hours_hosted"] for r in feed_rows), 2)
    hours_linked = round(sum(r["hours_linked"] for r in feed_rows), 2)
    gb_stored = round(sum(r["gb_stored"] for r in feed_rows), 4)
    gb_exact = all(r["gb_exact"] for r in feed_rows)
    monthly_cost = round(max(0.0, gb_stored - B2_FREE_GB) * B2_USD_PER_GB_MONTH, 4)

    # Issue tallies and top-N sources
    def _top(field: str, n: int = 3) -> list[list]:
        rows = sorted(feed_rows, key=lambda r: r[field], reverse=True)
        return [[r["slug"], r[field]] for r in rows if r[field] > 0][:n]

    # Backlog & projection
    run_summary = _load_run_summary(Path(state_dir)) if state_dir else {}
    history = _load_run_history(Path(state_dir)) if state_dir else []
    history_tail = list(reversed(history[-10:])) if history else []

    cap_raw = defaults.get("materialize_budget_per_run")
    base = ModelInputs(
        episodes_per_feed=int(defaults.get("max_episodes", 50)),
        kbps=max_kbps,
        per_run_cap=int(cap_raw) if cap_raw is not None else None,
    )
    inputs = measured_inputs(
        cities,
        run_history=history,
        hosted_feeds=hosted_audio or None,
        archive_items=_measured_archive_items(cities, state_dir),
        base=base,
    )
    proj = project(inputs)

    stale_total = sum(r["stale"] for r in feed_rows)
    stale_drain_runs = (
        round(stale_total / proj.per_run_throughput, 2) if proj.per_run_throughput else None
    )
    stale_drain_days = (
        round(stale_drain_runs * (inputs.cycle_hours / 24), 2) if stale_drain_runs else None
    )

    # Transcript backlog: hosted episodes that have no ASR transcript artifact yet.
    tx_pending = 0
    for recs in records_cache.values():
        for rec in recs.values():
            audio = rec.get("audio") or {}
            if audio.get("url"):
                tx = rec.get("transcript") or {}
                if not tx.get("key"):
                    tx_pending += 1
    tx_per_run_hist = [
        (r.get("stages") or {}).get("transcript", {}).get("transcribed", 0)
        + (r.get("stages") or {}).get("transcript", {}).get("aligned", 0)
        for r in history
        if (r.get("stages") or {}).get("transcript")
    ]
    tx_per_run = round(sum(tx_per_run_hist) / len(tx_per_run_hist), 2) if tx_per_run_hist else None
    tx_drain_runs = round(tx_pending / tx_per_run, 1) if tx_per_run else None
    tx_drain_days = round(tx_drain_runs * (inputs.cycle_hours / 24), 1) if tx_drain_runs else None

    # Storage detail
    ref_keys = len(referenced_audio_keys(Path(state_dir))) if state_dir else None
    retained = _measured_archive_items(cities, state_dir) or int(defaults.get("max_episodes", 50))
    g_per_ep = gb_per_episode(max_kbps, avg_duration_h)

    # Oldest publication year across all records — used by the age-cap what-if slider.
    oldest_pub_year: int | None = None
    for recs in records_cache.values():
        for rec in recs.values():
            pub = rec.get("published")
            if pub:
                try:
                    yr = datetime.fromisoformat(pub).year
                    if oldest_pub_year is None or yr < oldest_pub_year:
                        oldest_pub_year = yr
                except ValueError:
                    pass

    top_city_storage = sorted(city_rows, key=lambda r: r["gb_stored"], reverse=True)[:5]
    top_feed_storage = sorted(feed_rows, key=lambda r: r["gb_stored"], reverse=True)[:5]

    github_repo = os.environ.get("GITHUB_REPOSITORY")
    site_url = (
        f"https://{site_config['custom_domain']}" if site_config.get("custom_domain") else None
    )

    return {
        "snapshot_ts": now.isoformat(),
        "kpis": {
            "feeds": len(cities),
            "meetings_archived": meetings_archived,
            "hosted_audio": hosted_audio,
            "linked_video": linked_video,
            "hours_hosted": hours_hosted,
            "hours_linked": hours_linked,
            "gb_stored": gb_stored,
            "gb_exact": gb_exact,
            "monthly_cost_usd": monthly_cost,
            "last_build": {
                "ts": run_summary.get("ts"),
                "status": (
                    "errors"
                    if run_summary.get("errors", 0) > 0
                    else ("built" if run_summary.get("built", 0) > 0 else "skipped")
                )
                if run_summary
                else None,
                "built": run_summary.get("built", 0),
                "skipped": run_summary.get("skipped", 0),
                "errors": run_summary.get("errors", 0),
                "github_run_url": run_summary.get("github_run_url"),
                "github_run_id": run_summary.get("github_run_id"),
            },
        },
        "backlog": {
            "pending": sum(r["pending"] for r in feed_rows),
            "stale": stale_total,
            "stale_drain_runs": stale_drain_runs,
            "stale_drain_days": stale_drain_days,
            "stage_totals": run_summary.get("stages", {}),
            "capacity_per_day": proj.capacity_per_day,
            "inflow_per_day": round(proj.inflow_per_day, 2),
            "keeps_up": proj.keeps_up,
            "full_backfill_days": round(proj.full_backfill_days, 2),
            "audio_backlog": {
                "total": sum(r["pending"] for r in feed_rows) + stale_total,
                "pending": sum(r["pending"] for r in feed_rows),
                "stale": stale_total,
                "estimated_days": round(proj.full_backfill_days, 1),
            },
            "transcript_backlog": {
                "total_pending": tx_pending,
                "tx_per_run": tx_per_run,
                "estimated_runs": tx_drain_runs,
                "estimated_days": tx_drain_days,
            },
        },
        "issues": {
            "deferred": sum(r["deferred"] for r in feed_rows),
            "dead": sum(r["dead"] for r in feed_rows),
            "transient_errors": sum(r["transient_errors"] for r in feed_rows),
            "top_deferred": _top("deferred"),
            "top_dead": _top("dead"),
            "top_errors": _top("transient_errors"),
        },
        "feeds_by_feed": feed_rows,
        "feeds_by_city": city_rows,
        "storage": {
            "referenced_keys": ref_keys,
            "gb_stored": gb_stored,
            "gb_exact": gb_exact,
            "monthly_cost_usd": monthly_cost,
            "archive_cap": int(defaults.get("max_archive_items", 5000)),
            "archive_age_years": int(defaults.get("max_archive_age_years", 1000)),
            "retained_per_feed": retained,
            "gb_per_ep": round(g_per_ep, 5),
            "hosted_feeds": sum(1 for r in feed_rows if r["hosted"] > 0),
            "oldest_publication_year": oldest_pub_year,
            "top_by_city": [
                {"city": r["city"], "gb_stored": r["gb_stored"]} for r in top_city_storage
            ],
            "top_by_feed": [
                {"slug": r["slug"], "gb_stored": r["gb_stored"]} for r in top_feed_storage
            ],
        },
        "run_history": history_tail,
        "config": {
            "github_repo": github_repo,
            "max_kbps": max_kbps,
            "site_url": site_url,
        },
    }


def to_status_html(status: dict) -> str:
    """Render the operational status snapshot as a self-contained static HTML page.

    The HTML template lives in ``assets/status.html`` with a ``__STATUS_JSON__`` placeholder.
    Must be served behind Cloudflare Access — it publishes actuals (cost, errors, GB).
    """
    data = json.dumps(status, indent=2)
    html = _STATUS_TEMPLATE.read_text()
    return html.replace("__STATUS_JSON__", data)


def to_admin_html(report: dict) -> str:
    """A self-contained what-if page; the model is re-implemented in JS so sliders are live with
    no server. Seeded with the measured current inputs. The HTML lives in
    ``assets/admin.html`` with ``__REPORT_JSON__`` / ``__SEED_JSON__`` placeholders.

    NOTE: public on GitHub Pages. The page is a calculator; current live actuals seed the slider
    defaults but are not announced. Lock ``/admin/*`` behind Cloudflare Access to hide actuals.
    """
    data = json.dumps(report, indent=2)
    seed = json.dumps(report["current"]["inputs"])
    html = _ADMIN_TEMPLATE.read_text()
    return html.replace("__REPORT_JSON__", data).replace("__SEED_JSON__", seed)
