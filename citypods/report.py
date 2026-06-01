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


def _hosted_fraction(cities: list) -> float:
    if not cities:
        return 1.0
    hosted = sum(
        1 for c in cities if c.provider in _HLS_PROVIDERS or getattr(c, "extract_audio", False)
    )
    return hosted / len(cities)


def _measured_archive_items(state_dir: Path | None) -> int | None:
    """Average records retained per source in the append-only archive (issue #109) — the real
    storage driver, which decouples from the feed's render cap (``max_episodes``). None when no
    records exist yet, so the projection falls back to the render cap."""
    if not state_dir:
        return None
    counts = []
    for path in Path(state_dir).glob("sources/*/episodes.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        counts.append(len(data.get("episodes") or {}))
    if not counts:
        return None
    return round(sum(counts) / len(counts))


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
    base = ModelInputs(
        episodes_per_feed=int(defaults.get("max_episodes", 50)),
        kbps=int(defaults.get("audio_max_kbps", 96)),
        per_run_cap=int(defaults.get("materialize_budget_per_run", 25)),
    )
    history = _load_run_history(state_dir) if state_dir else []
    inputs = measured_inputs(
        cities,
        run_history=history,
        hosted_feeds=round(_hosted_fraction(cities) * len(cities)) if cities else None,
        archive_items=_measured_archive_items(state_dir),
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
            f"Consider `materialize_budget_per_run = {c['recommended_per_run']}` (or time-bounded)."
        )
    ret = report.get("retention")
    if ret:
        line = (
            f"- **Archive retention:** {ret['retained_per_feed']} recordings/feed retained "
            f"(cap {ret['max_archive_items']}, ≤{ret['max_archive_age_years']}y)"
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
    lines += ["", "### At scale (storage / month)", "", "| Feeds | TB | $/mo |", "|--:|--:|--:|"]
    for f in ("200", "500", "1000", "5000"):
        s = report["scale_scenarios"][f]
        lines.append(f"| {f} | {s['storage_gb'] / 1000:.2f} | ${s['monthly_cost_usd']:.2f} |")
    lines += ["", "_Open `/admin/` for the interactive what-if calculator._", ""]
    return "\n".join(lines)


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
