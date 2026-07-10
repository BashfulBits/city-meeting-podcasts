#!/usr/bin/env python
"""Normalize canonical duration fields for persisted episode records.

Manual repair tool for H21. It scans persisted ``episodes.json`` records, fills missing
``served_duration_seconds`` from either a hosted-audio probe or the persisted timeline, and writes
the result back only when ``--apply`` is passed.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from citypods.config import load_site_config
from citypods.durations import record_served_duration_seconds, set_served_duration_seconds
from citypods.media import probe_hosted_audio_duration_seconds
from citypods.records import (
    load_records,
    protected_blocks_for_lane,
    record_to_episode,
    save_records,
)
from citypods.state import resolve_state_dir
from citypods.statesync import pull_state, push_records_merged
from citypods.storage import make_storage
from citypods.timeline import edl_duration

DEFAULT_MAX_ITEMS = 200
_DURATION_CHANGE_TOLERANCE_SECONDS = 0.5


@dataclass
class NormalizeSummary:
    probed: int = 0
    timeline_fallback: int = 0
    unchanged: int = 0
    skipped: int = 0
    failed: int = 0
    changed: int = 0
    examined: int = 0
    touched_sources: set[str] = field(default_factory=set)

    def as_dict(self) -> dict:
        return {
            "probed": self.probed,
            "timeline_fallback": self.timeline_fallback,
            "unchanged": self.unchanged,
            "skipped": self.skipped,
            "failed": self.failed,
            "changed": self.changed,
            "examined": self.examined,
            "touched_sources": sorted(self.touched_sources or set()),
        }


def _record_timeline_fallback_seconds(rec: dict) -> float | None:
    ep = record_to_episode(rec)
    timeline = ep.timeline
    if timeline is None or not timeline.segments:
        return None
    fallback = edl_duration(timeline)
    return fallback if fallback is not None and fallback > 0 else None


def _jsonl_row(
    *,
    source_key: str,
    uid: str,
    outcome: str,
    reason: str,
    before: float | None,
    after: float | None,
) -> dict:
    return {
        "source_key": source_key,
        "uid": uid,
        "outcome": outcome,
        "reason": reason,
        "before_served_duration_seconds": before,
        "after_served_duration_seconds": after,
    }


def _duration_changed(
    before: float | None,
    after: float | None,
    *,
    had_canonical: bool,
) -> bool:
    if not had_canonical or before is None or after is None:
        return not had_canonical or before != after
    return abs(before - after) > _DURATION_CHANGE_TOLERANCE_SECONDS


def normalize_records(
    records: dict,
    *,
    source_key: str,
    storage,
    ffmpeg_binary: str,
    uid_filter: str | None = None,
    max_items: int = DEFAULT_MAX_ITEMS,
    probe_existing: bool = True,
    apply: bool = False,
) -> tuple[list[dict], NormalizeSummary, bool]:
    """Normalize one source's records; returns ``(rows, summary, changed)``."""
    rows: list[dict] = []
    summary = NormalizeSummary()
    changed = False
    processed = 0
    for uid, rec in records.items():
        if uid_filter and uid != uid_filter:
            continue
        if processed >= max_items:
            rows.append(
                _jsonl_row(
                    source_key=source_key,
                    uid=uid,
                    outcome="skipped",
                    reason="max-items",
                    before=record_served_duration_seconds(rec),
                    after=record_served_duration_seconds(rec),
                )
            )
            summary.skipped += 1
            continue
        processed += 1
        summary.examined += 1
        before = record_served_duration_seconds(rec)
        if before is not None and rec.get("served_duration_seconds"):
            rows.append(
                _jsonl_row(
                    source_key=source_key,
                    uid=uid,
                    outcome="unchanged",
                    reason="canonical-present",
                    before=before,
                    after=before,
                )
            )
            summary.unchanged += 1
            continue

        audio = rec.get("audio") or {}
        probed = None
        probe_reason = "probe-disabled"
        if probe_existing and audio.get("key"):
            try:
                probed, probe_reason = probe_hosted_audio_duration_seconds(
                    storage,
                    audio["key"],
                    ffmpeg_binary=ffmpeg_binary,
                )
            except Exception:
                probed, probe_reason = None, "probe-exception"
        if probed is not None and probed > 0:
            had_canonical = rec.get("served_duration_seconds") is not None
            if apply:
                set_served_duration_seconds(rec, probed)
            rows.append(
                _jsonl_row(
                    source_key=source_key,
                    uid=uid,
                    outcome="probed",
                    reason=probe_reason,
                    before=before,
                    after=probed,
                )
            )
            summary.probed += 1
            if _duration_changed(before, probed, had_canonical=had_canonical):
                summary.changed += 1
                changed = True
                summary.touched_sources.add(source_key)
            continue

        outcome = "failed" if audio.get("key") else "skipped"
        reason = probe_reason if audio.get("key") else "no-hosted-audio-or-timeline"
        rows.append(
            _jsonl_row(
                source_key=source_key,
                uid=uid,
                outcome=outcome,
                reason=reason,
                before=before,
                after=before,
            )
        )
        if outcome == "failed":
            summary.failed += 1
        else:
            summary.skipped += 1
    return rows, summary, changed


def _render_summary(summary: NormalizeSummary, *, apply: bool) -> str:
    verb = "applied" if apply else "dry-run"
    return "\n".join(
        [
            f"# Duration normalization ({verb})",
            "",
            f"- examined: {summary.examined}",
            f"- changed: {summary.changed}",
            f"- probed: {summary.probed}",
            f"- timeline fallback: {summary.timeline_fallback}",
            f"- unchanged: {summary.unchanged}",
            f"- skipped: {summary.skipped}",
            f"- failed: {summary.failed}",
            f"- touched sources: {len(summary.touched_sources or set())}",
            "",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default="", help="exact source key to normalize")
    ap.add_argument("--uid", default="", help="exact uid to normalize")
    ap.add_argument(
        "--max-items",
        type=int,
        default=DEFAULT_MAX_ITEMS,
        help=f"max records to process per run (default: {DEFAULT_MAX_ITEMS})",
    )
    ap.add_argument(
        "--probe-existing",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="re-probe hosted audio when available (default: true)",
    )
    ap.add_argument("--apply", action="store_true", help="persist normalized records")
    ap.add_argument("--site-config", default="config/site_config.yml")
    ap.add_argument("--output-dir", default="docs")
    ap.add_argument("--jsonl-out", default="duration-normalize/normalize.jsonl")
    ap.add_argument("--summary-out", default="duration-normalize/summary.json")
    ap.add_argument("--summary-md-out", default="duration-normalize/summary.md")
    args = ap.parse_args(argv)

    site_config = load_site_config(args.site_config)
    storage = make_storage(site_config, "https://example.invalid", args.output_dir)
    state_dir = resolve_state_dir(site_config, args.output_dir)
    if storage is not None:
        pulled = pull_state(storage, state_dir)
        print(f"state: pulled {pulled} file(s) from durable storage")

    sources = (
        [args.source]
        if args.source
        else sorted(p.parent.name for p in Path(state_dir).glob("sources/*/episodes.json"))
    )
    if not sources:
        print("no sources found in state")
        return 1

    ffmpeg_binary = "ffmpeg"
    all_rows: list[dict] = []
    total = NormalizeSummary()
    changed_sources: set[str] = set()
    for source_key in sources:
        records = load_records(state_dir, source_key)
        if not records:
            continue
        rows, summary, changed = normalize_records(
            records,
            source_key=source_key,
            storage=storage,
            ffmpeg_binary=ffmpeg_binary,
            uid_filter=args.uid or None,
            max_items=args.max_items,
            probe_existing=args.probe_existing,
            apply=args.apply,
        )
        all_rows.extend(rows)
        total.probed += summary.probed
        total.timeline_fallback += summary.timeline_fallback
        total.unchanged += summary.unchanged
        total.skipped += summary.skipped
        total.failed += summary.failed
        total.changed += summary.changed
        total.examined += summary.examined
        total.touched_sources.update(summary.touched_sources or set())
        if changed and args.apply:
            save_records(state_dir, source_key, records)
            changed_sources.add(source_key)

    if args.apply and changed_sources and storage is not None:
        push_records_merged(
            storage,
            state_dir,
            sorted(changed_sources),
            protected_blocks=protected_blocks_for_lane("audio"),
            log=lambda msg: print(msg, flush=True),
        )

    jsonl_out = Path(args.jsonl_out)
    jsonl_out.parent.mkdir(parents=True, exist_ok=True)
    jsonl_out.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in all_rows))

    summary_out = Path(args.summary_out)
    summary_out.parent.mkdir(parents=True, exist_ok=True)
    summary_out.write_text(json.dumps(total.as_dict(), indent=2) + "\n")

    summary_md_out = Path(args.summary_md_out)
    summary_md_out.parent.mkdir(parents=True, exist_ok=True)
    summary_md_out.write_text(_render_summary(total, apply=args.apply))

    print(_render_summary(total, apply=args.apply))
    return 0


if __name__ == "__main__":
    sys.exit(main())
