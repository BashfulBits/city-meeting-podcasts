#!/usr/bin/env python
"""Garbage-collect orphaned content-addressed objects from storage.

Audio and transcripts are content-addressed (the key embeds a spec hash), so when an episode's
artifact is regenerated — chapters added, a bitrate bump, a new transcript version, or a duplicate
source view coalesced (GH#421) — the old object is left behind, no longer referenced by any record.
This sweep deletes those orphans.

The live set comes from ``records.referenced_audio_keys`` (despite the name, it returns both
audio AND transcript keys), so hosted transcripts are protected, not reaped.

Safe by default:
  * dry-run unless ``--apply`` is given;
  * ``--pull-state`` first restores the durable bucket state so the live set is current — never
    reap against a stale/partial set of records;
  * only deletes objects older than ``--min-age-days`` (default 7) so an object written by a
    build that hasn't yet persisted its record isn't reaped out from under it;
  * refuses to run if no records are found (would look like everything is orphaned);
  * never touches the durable ``state/`` snapshot.

With ``--out DIR`` it writes a machine-readable report for the scheduled workflow:
``orphans.tsv`` (every candidate key + size + city), ``summary.json``, ``issue-body.md`` (a
per-city table), and ``has_orphans`` (``true``/``false``).

By default it scans the whole bucket (``--prefix ""``); pass ``--prefix`` to scope it. Note:
clip objects (``clips/…``) are not produced yet — when soundbites land, either reference them
in the live set or give them their own ephemeral GC policy before running this unscoped.

Usage:
    PYTHONPATH=. python scripts/gc_audio.py [--apply] [--pull-state] [--min-age-days N] \
        [--prefix P] [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from citypods.config import load_city_configs, load_site_config
from citypods.records import referenced_audio_keys, source_key
from citypods.state import resolve_state_dir
from citypods.statesync import STATE_PREFIX
from citypods.storage import make_storage

_UNCONFIGURED = "(unconfigured)"
_OTHER = "(other)"


@dataclass(frozen=True)
class Orphan:
    key: str
    bytes: int
    city: str
    last_modified: str | None


def source_to_city(cities: list) -> dict[str, str]:
    """Map each configured source_key to its government entity (``city:``) or slug.

    Per-board and combined feeds of one entity collapse to the same label, which is what makes the
    GC report read as "per city" rather than "per feed". A source_key with no configured city
    (e.g. a removed feed) is attributed to ``(unconfigured)`` at report time.
    """
    mapping: dict[str, str] = {}
    for city in cities:
        mapping[source_key(city)] = city.city_entity or city.slug
    return mapping


def attribute(key: str, mapping: dict[str, str]) -> str:
    """Resolve a storage key to a city label via its source_key segment."""
    segments = key.split("/")
    src: str | None = None
    if segments[0] == "transcripts" and len(segments) >= 3:
        src = segments[1]  # transcripts/<source_key>/<uid>-<spec>.<fmt>
    elif len(segments) >= 3 and segments[0] not in {"clips", STATE_PREFIX}:
        src = segments[1]  # <provider>/<source_key>/<uid>-<spec>.m4a
    if src is None:
        return _OTHER
    return mapping.get(src, _UNCONFIGURED)


def summarize(orphans: list[Orphan]) -> dict:
    by_city: dict[str, dict[str, int]] = defaultdict(lambda: {"files": 0, "bytes": 0})
    for o in orphans:
        by_city[o.city]["files"] += 1
        by_city[o.city]["bytes"] += o.bytes
    return {
        "by_city": {city: dict(v) for city, v in sorted(by_city.items())},
        "total_files": len(orphans),
        "total_bytes": sum(o.bytes for o in orphans),
    }


def human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def render_issue_body(summary: dict, *, applied: bool) -> str:
    verb = "reclaimed" if applied else "reclaimable"
    lines = [
        f"## Audio storage GC — {summary['total_files']} orphan object(s), "
        f"{human_bytes(summary['total_bytes'])} {verb}",
        "",
        (
            "These content-addressed audio/transcript objects are no longer referenced by any "
            "record (regenerated artifacts, retired recipes, or coalesced duplicate source views, "
            "GH#421). This run was a **dry-run** — nothing was deleted."
            if not applied
            else "These objects were **deleted** by a manual `apply` run of the GC workflow."
        ),
        "",
        "| City | Files | Size |",
        "|---|---:|---:|",
    ]
    for city, v in summary["by_city"].items():
        lines.append(f"| {city} | {v['files']} | {human_bytes(v['bytes'])} |")
    lines.append(
        f"| **Total** | **{summary['total_files']}** | **{human_bytes(summary['total_bytes'])}** |"
    )
    if not applied:
        lines += [
            "",
            "To actually delete these objects, run the **Audio orphan GC** workflow via "
            "*Run workflow* with `apply = true`.",
        ]
    return "\n".join(lines) + "\n"


def _write_report(out_dir: Path, orphans: list[Orphan], summary: dict, *, applied: bool) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = ["key\tbytes\tcity\tlast_modified"]
    rows += [f"{o.key}\t{o.bytes}\t{o.city}\t{o.last_modified or ''}" for o in orphans]
    (out_dir / "orphans.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (out_dir / "issue-body.md").write_text(
        render_issue_body(summary, applied=applied), encoding="utf-8"
    )
    (out_dir / "has_orphans").write_text("true" if orphans else "false", encoding="utf-8")


def _non_negative_days(value: str) -> float:
    """A negative ``--min-age-days`` would push the cutoff into the future, making every object
    look old enough to reap — including freshly written ones. Reject it."""
    days = float(value)
    if days < 0:
        raise argparse.ArgumentTypeError("--min-age-days must be >= 0")
    return days


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="actually delete (default: dry-run)")
    ap.add_argument(
        "--pull-state",
        action="store_true",
        help="restore the durable bucket state first so the live set is current (no-op for a "
        "sync-less local backend)",
    )
    ap.add_argument(
        "--min-age-days",
        type=_non_negative_days,
        default=7.0,
        help="skip objects newer than this (must be >= 0)",
    )
    ap.add_argument("--prefix", default="", help="limit to keys under this storage prefix")
    ap.add_argument("--site-config", default="config/site_config.yml")
    ap.add_argument("--config-dir", default="config", help="config dir for per-city attribution")
    ap.add_argument("--output-dir", default="docs")
    ap.add_argument("--out", default=None, help="write the GC report (tsv/json/md) into this dir")
    args = ap.parse_args(argv)

    site_config = load_site_config(args.site_config)
    storage = make_storage(site_config, "https://example.invalid", args.output_dir)
    if storage is None or not hasattr(storage, "iter_objects"):
        print("no storage backend with GC support configured; nothing to do")
        return 0

    state_dir = resolve_state_dir(site_config, args.output_dir)
    if args.pull_state:
        from citypods.statesync import pull_state

        pull_state(storage, state_dir)

    referenced = referenced_audio_keys(state_dir)
    if not referenced:
        print("no records found; refusing to GC (would look like everything is orphaned)")
        return 1

    try:
        cities = load_city_configs(args.config_dir, site_config.get("defaults", {}))
    except (FileNotFoundError, OSError):
        cities = []
    mapping = source_to_city(cities)

    cutoff = datetime.now(UTC) - timedelta(days=args.min_age_days)
    orphans: list[Orphan] = []
    kept_young = 0
    for key, last_modified, size in storage.iter_objects(args.prefix):
        if key in referenced or key.startswith(f"{STATE_PREFIX}/"):
            continue  # never reap the durable state snapshot
        if last_modified is not None and last_modified > cutoff:
            kept_young += 1
            continue
        if args.apply:
            storage.delete(key)
            print(f"DELETED  {key}")
        else:
            print(f"ORPHAN   {key}")
        orphans.append(
            Orphan(
                key=key,
                bytes=int(size or 0),
                city=attribute(key, mapping),
                last_modified=last_modified.isoformat() if last_modified is not None else None,
            )
        )

    summary = summarize(orphans)
    if args.out:
        _write_report(Path(args.out), orphans, summary, applied=args.apply)

    action = "deleted" if args.apply else "orphaned (dry-run)"
    print(
        f"\n{len(referenced)} referenced; {summary['total_files']} {action} "
        f"({human_bytes(summary['total_bytes'])}); "
        f"{kept_young} skipped (younger than {args.min_age_days}d)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
