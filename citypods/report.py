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
    rows = []
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    # Sharded H6b audio/ASR jobs cannot safely push shared run_history.jsonl: each shard starts
    # from the same pulled snapshot and would overwrite sibling appends. They instead push unique
    # JSON event files under run_events/, which are safe to merge here for status/projection.
    for event in sorted((Path(state_dir) / "run_events").glob("*.json")):
        try:
            rows.append(json.loads(event.read_text()))
        except (OSError, ValueError):
            continue
    seen: set[str] = set()
    unique: list[dict] = []
    for row in sorted(rows, key=lambda r: str(r.get("ts") or "")):
        sig = json.dumps(row, sort_keys=True, separators=(",", ":"))
        if sig in seen:
            continue
        seen.add(sig)
        unique.append(row)
    return unique


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


def _classify_record(
    rec: dict, max_kbps: int, loudness_profile: str = "", *, host_direct: bool = False
) -> str:
    """Return the pipeline state for one record (mutually exclusive taxonomy from issue #124).

    States (in order of precedence):
      served         — hosted audio exists and spec matches current desired spec (or "legacy")
      stale          — hosted audio exists but spec no longer matches (re-encode queued)
      linked_video   — direct provider MP4 link; config says not to host this episode's audio
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

    should_host = media_kind == "hls" or (media_kind == "direct" and host_direct)
    if not should_host and media_kind == "direct":
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
        state = _classify_record(
            rec,
            max_kbps,
            loudness_profile=loudness_profile,
            host_direct=bool(city.extract_audio),
        )

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


def _source_actuals(
    records_cache: dict[str, dict],
    *,
    max_kbps: int,
    loudness_profile: str = "",
    host_direct_by_source: dict[str, bool] | None = None,
) -> dict:
    """Aggregate source-record actuals exactly once per ``source_key``.

    The status page has feed/body rows for diagnostics, but headline operational numbers should
    come from the canonical source records. Summing feed rows can miss unconfigured bodies and can
    double-count combined feeds plus per-board feeds that share one record store.
    """
    totals = {
        "meetings_archived": 0,
        "hosted_audio": 0,
        "linked_video": 0,
        "served": 0,
        "stale": 0,
        "pending": 0,
        "deferred": 0,
        "dead": 0,
        "transient_errors": 0,
        "hours_hosted": 0.0,
        "hours_linked": 0.0,
        "gb_stored": 0.0,
        "gb_exact": True,
        "tx_synced": 0,
        "tx_text": 0,
        "tx_hosted": 0,
        "tx_pending": 0,
    }
    host_direct_by_source = host_direct_by_source or {}
    for key, recs in records_cache.items():
        host_direct = host_direct_by_source.get(key, False)
        for rec in recs.values():
            totals["meetings_archived"] += 1
            audio = rec.get("audio") or {}
            duration_s = rec.get("duration") or audio.get("duration_served") or 0
            if not duration_s and audio.get("bytes"):
                duration_s = audio["bytes"] * 8 / (max_kbps * 1000)
            state = _classify_record(
                rec,
                max_kbps,
                loudness_profile=loudness_profile,
                host_direct=host_direct,
            )

            if state in ("served", "stale"):
                totals["hosted_audio"] += 1
                totals["hours_hosted"] += duration_s / 3600
                raw_bytes = audio.get("bytes")
                if raw_bytes is not None:
                    totals["gb_stored"] += raw_bytes / 1e9
                else:
                    totals["gb_exact"] = False
                totals[state] += 1
            elif state == "linked_video":
                totals["linked_video"] += 1
                totals["hours_linked"] += duration_s / 3600
            elif state == "deferred":
                totals["deferred"] += 1
            elif state == "dead":
                totals["dead"] += 1
            elif state == "transient_error":
                totals["transient_errors"] += 1
            else:
                totals["pending"] += 1

            tx = rec.get("transcript") or {}
            if tx.get("synced"):
                totals["tx_synced"] += 1
            elif tx.get("key"):
                totals["tx_text"] += 1
            if audio.get("url"):
                totals["tx_hosted"] += 1
                if not tx.get("key"):
                    totals["tx_pending"] += 1

    totals["hours_hosted"] = round(totals["hours_hosted"], 2)
    totals["hours_linked"] = round(totals["hours_linked"], 2)
    totals["gb_stored"] = round(totals["gb_stored"], 4)
    return totals


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


def _run_sort_value(row: dict) -> str:
    return str(row.get("ts") or "")


def _same_logical_run(a: dict, b: dict) -> bool:
    """True for shard events belonging to the same workflow/lane run."""
    if not a.get("scoped") or not b.get("scoped"):
        return False
    if not a.get("github_run_id") or not b.get("github_run_id"):
        return False
    return (
        a.get("github_run_id") == b.get("github_run_id")
        and a.get("phase") == b.get("phase")
        and a.get("lane") == b.get("lane")
    )


def _run_candidates(run_summary: dict, history: list[dict]) -> list[dict]:
    candidates = list(history)
    if run_summary:
        candidates.append(run_summary)
    if not candidates:
        return []

    # De-duplicate the run_summary row when it is also present in run_history.jsonl.
    unique: list[dict] = []
    seen: set[str] = set()
    for row in candidates:
        sig = json.dumps(row, sort_keys=True, separators=(",", ":"))
        if sig in seen:
            continue
        seen.add(sig)
        unique.append(row)
    return sorted(unique, key=_run_sort_value)


def _sum_stage_totals(rows: list[dict]) -> dict:
    totals: dict[str, dict] = {}
    keys = (
        "ran",
        "encoded",
        "credited",
        "aligned",
        "transcribed",
        "reused",
        "backlog",
        "seconds",
        "bytes",
        "errors",
    )
    for row in rows:
        for name, stage in (row.get("stages") or {}).items():
            out = totals.setdefault(name, {k: 0 for k in keys})
            for k in keys:
                out[k] += stage.get(k, 0) or 0
    for stage in totals.values():
        if "seconds" in stage:
            stage["seconds"] = round(stage["seconds"], 1)
    return totals


def _merge_logical_run_group(group: list[dict]) -> dict:
    """Merge one sharded logical run into the same summary shape as one event row."""
    if not group:
        return {}
    if len(group) == 1:
        return dict(group[0])

    group = sorted(group, key=_run_sort_value)
    merged = dict(group[-1])
    for key in ("cities", "built", "skipped", "errors", "materialized", "materialize_encoded"):
        merged[key] = sum(row.get(key, 0) or 0 for row in group)
    merged["materialize_seconds"] = round(
        sum(row.get("materialize_seconds", 0.0) or 0.0 for row in group), 1
    )
    merged["stages"] = _sum_stage_totals(group)
    merged["shards"] = sorted(row.get("shard") for row in group if row.get("shard"))
    provider_errors: dict[str, int] = {}
    for row in group:
        for provider, n in (row.get("provider_errors") or {}).items():
            provider_errors[provider] = provider_errors.get(provider, 0) + int(n or 0)
    merged["provider_errors"] = provider_errors
    peaks = [
        row.get("peak_load_per_cpu") for row in group if row.get("peak_load_per_cpu") is not None
    ]
    mems = [row.get("min_mem_avail_mb") for row in group if row.get("min_mem_avail_mb") is not None]
    waits = [
        row.get("gate_wait_seconds") for row in group if row.get("gate_wait_seconds") is not None
    ]
    windows = [
        row.get("window_used_pct") for row in group if row.get("window_used_pct") is not None
    ]
    if peaks:
        merged["peak_load_per_cpu"] = max(peaks)
    if mems:
        merged["min_mem_avail_mb"] = min(mems)
    if waits:
        merged["gate_wait_seconds"] = round(sum(waits), 1)
    if windows:
        merged["window_used_pct"] = max(windows)
    return merged


def _logical_run_groups(run_summary: dict, history: list[dict]) -> list[dict]:
    """Return run summaries with scoped sibling shards aggregated."""
    rows = _run_candidates(run_summary, history)
    groups: dict[tuple, list[dict]] = {}
    for idx, row in enumerate(rows):
        if row.get("scoped") and row.get("github_run_id"):
            key = ("scoped", row.get("github_run_id"), row.get("phase"), row.get("lane"))
        else:
            key = ("single", idx)
        groups.setdefault(key, []).append(row)
    return [_merge_logical_run_group(group) for group in groups.values()]


def _latest_run_summary(run_summary: dict, history: list[dict]) -> dict:
    """Return the newest run summary, aggregating sibling shard events when possible."""
    unique = _run_candidates(run_summary, history)
    if not unique:
        return {}

    latest = max(unique, key=_run_sort_value)
    group = [row for row in unique if _same_logical_run(row, latest)] or [latest]
    return _merge_logical_run_group(group)


def _latest_stage_runs(run_summary: dict, history: list[dict]) -> dict[str, dict]:
    """Latest completed logical run per reported stage.

    ``_latest_run_summary`` intentionally answers "what was the newest lane?" For the status page's
    pipeline table we need a different question: "what was the newest run that actually reported
    each stage?" That lets audio remain visible after a later ASR-only run, and generalizes to
    future transcript/diarization workers that may report multiple output stages from one event.
    """
    latest: dict[str, dict] = {}
    for group in _logical_run_groups(run_summary, history):
        stages = group.get("stages") or {}
        if not stages:
            continue
        for name, totals in stages.items():
            prev = latest.get(name)
            if prev and _run_sort_value(prev) >= _run_sort_value(group):
                continue
            latest[name] = {
                "ts": group.get("ts"),
                "phase": group.get("phase"),
                "lane": group.get("lane"),
                "shard": group.get("shard"),
                "shards": group.get("shards"),
                "scoped": group.get("scoped"),
                "github_run_id": group.get("github_run_id"),
                "github_run_url": group.get("github_run_url"),
                "built": group.get("built", 0),
                "skipped": group.get("skipped", 0),
                "errors": group.get("errors", 0),
                "totals": totals,
            }
    return latest


def _manifest_for_status(
    cities: list, records_cache: dict[str, dict], state_dir: Path | None
) -> tuple[dict, dict]:
    """Return work-list counts plus small provenance metadata for the status page.

    ``work.json`` can contain live async/shard state such as leases, but it is a derived sidecar
    and may lag records after source edits or scoped shard pushes. Build a fresh manifest from the
    canonical records, then overlay only the non-derivable live state from persisted work items.
    """
    if not state_dir:
        return {}, {"source": "none", "items": 0, "persisted_items": 0, "overlaid_items": 0}

    from citypods.ops.workqueue import build_manifest, load_manifest, manifest_counts
    from citypods.records import source_key

    city_by_source: dict = {}
    for city in cities:
        city_by_source.setdefault(source_key(city), city)
    manifest_sources = [
        (k, city_by_source[k], recs) for k, recs in records_cache.items() if k in city_by_source
    ]
    items = build_manifest(manifest_sources)

    persisted = load_manifest(state_dir)
    persisted_by_key = {
        (it.source_key, it.episode_uid, it.work_class): it
        for it in persisted
        if it.source_key in city_by_source
    }
    overlaid = 0
    for item in items:
        prev = persisted_by_key.get((item.source_key, item.episode_uid, item.work_class))
        if prev is None or item.state == "done":
            continue
        # Preserve sidecar-only operational state. Completion is still derived from records, so a
        # stale persisted ``queued`` item cannot undo a record that now has its artifact.
        if prev.lease_owner or prev.state in {"running", "backoff", "dead"}:
            item.state = prev.state
            item.lease_owner = prev.lease_owner
            item.lease_expires = prev.lease_expires
            item.last_error = prev.last_error
            item.next_retry = prev.next_retry
            item.observed_seconds = prev.observed_seconds
            item.est_seconds = prev.est_seconds
            overlaid += 1

    return manifest_counts(items), {
        "source": "derived+work.json" if persisted else "derived",
        "items": len(items),
        "persisted_items": len(persisted),
        "overlaid_items": overlaid,
    }


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
    host_direct_by_source: dict[str, bool] = {}
    if state_dir:
        seen: set[str] = set()
        for city in cities:
            key = source_key(city)
            host_direct_by_source[key] = host_direct_by_source.get(key, False) or bool(
                city.extract_audio
            )
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
    actuals = _source_actuals(
        records_cache,
        max_kbps=max_kbps,
        loudness_profile=loudness_profile,
        host_direct_by_source=host_direct_by_source,
    )

    # KPI totals
    meetings_archived = actuals["meetings_archived"]
    hosted_audio = actuals["hosted_audio"]
    linked_video = actuals["linked_video"]
    hours_hosted = actuals["hours_hosted"]
    hours_linked = actuals["hours_linked"]
    gb_stored = actuals["gb_stored"]
    gb_exact = actuals["gb_exact"]
    monthly_cost = round(max(0.0, gb_stored - B2_FREE_GB) * B2_USD_PER_GB_MONTH, 4)

    # Issue tallies and top-N sources
    def _top(field: str, n: int = 3) -> list[list]:
        rows = sorted(feed_rows, key=lambda r: r[field], reverse=True)
        return [[r["slug"], r[field]] for r in rows if r[field] > 0][:n]

    # Backlog & projection
    shared_run_summary = _load_run_summary(Path(state_dir)) if state_dir else {}
    history = _load_run_history(Path(state_dir)) if state_dir else []
    run_summary = _latest_run_summary(shared_run_summary, history)
    stage_runs = _latest_stage_runs(shared_run_summary, history)
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
        hosted_feeds=round(_hosted_fraction(cities) * len(cities)) if cities else None,
        archive_items=_measured_archive_items(cities, state_dir),
        base=base,
    )
    proj = project(inputs)

    # Use source-record actuals for stale/issue counts; feed rows are diagnostic and can overlap
    # when one source backs both combined and per-board feeds.
    stale_total = actuals["stale"]
    stale_drain_runs = (
        round(stale_total / proj.per_run_throughput, 2) if proj.per_run_throughput else None
    )
    stale_drain_days = (
        round(stale_drain_runs * (inputs.cycle_hours / 24), 2) if stale_drain_runs else None
    )

    # Transcript coverage/backlog from canonical records. This is the durable source of truth for
    # ASR progress; run summaries are only telemetry and sharded runs may complete out of order.
    tx_synced_total = actuals["tx_synced"]
    tx_text_total = actuals["tx_text"]
    tx_pending = actuals["tx_pending"]
    tx_hosted_total = actuals["tx_hosted"]
    tx_known_total = tx_synced_total + tx_text_total
    tx_coverage_pct = (
        round(100.0 * tx_synced_total / tx_hosted_total, 1) if tx_hosted_total else None
    )
    tx_per_run_hist = [
        (r.get("stages") or {}).get("transcript", {}).get("transcribed", 0)
        + (r.get("stages") or {}).get("transcript", {}).get("aligned", 0)
        for r in history
        if (r.get("stages") or {}).get("transcript")
    ]
    tx_per_run = round(sum(tx_per_run_hist) / len(tx_per_run_hist), 2) if tx_per_run_hist else None
    tx_drain_runs = round(tx_pending / tx_per_run, 1) if tx_per_run else None
    tx_drain_days = round(tx_drain_runs * (inputs.cycle_hours / 24), 1) if tx_drain_runs else None

    # Backlog by work-class (H5): derive the work manifest and bucket the *actionable*
    # (feed_visible) backlog by audio / transcript-asr / transcript-align, surfacing the
    # alignment-disabled and deep-archive counts. Derived fresh from records (hybrid model).
    wc_counts, manifest_meta = _manifest_for_status(
        cities, records_cache, Path(state_dir) if state_dir else None
    )
    by_work_class = wc_counts.get("by_work_class", {})
    audio_work = by_work_class.get("audio", {})
    audio_open = sum(
        n for state, n in audio_work.items() if state not in {"done", "alignment-disabled"}
    )
    transcript_open = sum(
        n
        for cls, states in by_work_class.items()
        if cls.startswith("transcript-")
        for state, n in states.items()
        if state not in {"done", "alignment-disabled"}
    )
    audio_pending = audio_open if by_work_class else actuals["pending"]
    transcript_pending = transcript_open if by_work_class else tx_pending
    audio_done_for_coverage = audio_work.get("done", 0) if by_work_class else hosted_audio
    audio_coverage_den = audio_done_for_coverage + audio_pending
    audio_coverage_pct = (
        round(100.0 * audio_done_for_coverage / audio_coverage_den, 1)
        if audio_coverage_den
        else None
    )
    audio_capacity_per_day = proj.capacity_per_day
    audio_backlog_total = audio_pending + stale_total
    audio_backlog_days = (
        round(audio_backlog_total / audio_capacity_per_day, 1) if audio_capacity_per_day else None
    )

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
            "audio_missing": audio_pending,
            "audio_stale": stale_total,
            "audio_coverage_pct": audio_coverage_pct,
            "audio_coverage_scope": "feed_visible" if by_work_class else "source_records",
            "linked_video": linked_video,
            "hours_hosted": hours_hosted,
            "hours_linked": hours_linked,
            "gb_stored": gb_stored,
            "gb_exact": gb_exact,
            "monthly_cost_usd": monthly_cost,
            "transcripts_synced": tx_synced_total,
            "transcripts_text": tx_text_total,
            "transcripts_missing": tx_pending,
            "transcripts_hosted": tx_hosted_total,
            "transcript_coverage_pct": tx_coverage_pct,
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
                "phase": run_summary.get("phase"),
                "lane": run_summary.get("lane"),
                "shard": run_summary.get("shard"),
                "shards": run_summary.get("shards"),
                "scoped": run_summary.get("scoped"),
            },
        },
        "backlog": {
            "pending": audio_pending,
            "stale": stale_total,
            "stale_drain_runs": stale_drain_runs,
            "stale_drain_days": stale_drain_days,
            "stage_totals": run_summary.get("stages", {}),
            "stage_runs": stage_runs,
            "capacity_per_day": proj.capacity_per_day,
            "inflow_per_day": round(proj.inflow_per_day, 2),
            "keeps_up": proj.keeps_up,
            "full_backfill_days": round(proj.full_backfill_days, 2),
            "audio_backlog": {
                "total": audio_backlog_total,
                "pending": audio_pending,
                "stale": stale_total,
                "estimated_days": audio_backlog_days,
            },
            "transcript_backlog": {
                "total_pending": transcript_pending,
                "synced": tx_synced_total,
                "text_only": tx_text_total,
                "known": tx_known_total,
                "hosted_eligible": tx_hosted_total,
                "coverage_pct": tx_coverage_pct,
                "tx_per_run": tx_per_run,
                "estimated_runs": tx_drain_runs,
                "estimated_days": tx_drain_days,
            },
            # H5: actionable backlog bucketed by output artifact (audio / transcript-asr /
            # transcript-align), plus alignment-disabled + inert deep-archive counts.
            "by_work_class": by_work_class,
            "work_pending": wc_counts.get("feed_visible_pending", 0),
            "alignment_disabled": wc_counts.get("alignment_disabled", 0),
            "deep_archive_items": wc_counts.get("deep_archive_items", 0),
            "work_manifest": manifest_meta,
        },
        "issues": {
            "deferred": actuals["deferred"],
            "dead": actuals["dead"],
            "transient_errors": actuals["transient_errors"],
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
            "hosted_feeds": round(inputs.feeds * inputs.host_frac),
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
