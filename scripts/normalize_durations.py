#!/usr/bin/env python
"""Normalize canonical duration fields for persisted episode records.

Manual repair tool for H21. It scans persisted ``episodes.json`` records, probes hosted audio to
audit or repair ``served_duration_seconds``, and writes the probed value back only when
``--apply`` is passed.
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
    save_records,
)
from citypods.state import resolve_state_dir
from citypods.statesync import pull_state, push_records_merged
from citypods.storage import make_storage

DEFAULT_MAX_ITEMS = 200
_DURATION_CHANGE_TOLERANCE_SECONDS = 0.5


@dataclass
class NormalizeSummary:
    probe_attempted: int = 0
    probe_succeeded: int = 0
    probe_failed: int = 0
    unchanged: int = 0
    skipped: int = 0
    failed: int = 0
    changed: int = 0
    examined: int = 0
    canonical_null_before: int = 0
    canonical_null_after: int = 0
    canonical_set_before: int = 0
    canonical_set_after: int = 0
    canonical_matched_probe_before: int = 0
    canonical_matched_probe_after: int = 0
    canonical_mismatched_probe_before: int = 0
    canonical_mismatched_probe_after: int = 0
    legacy_null_before: int = 0
    legacy_null_after: int = 0
    legacy_set_before: int = 0
    legacy_set_after: int = 0
    legacy_matched_probe_before: int = 0
    legacy_matched_probe_after: int = 0
    legacy_mismatched_probe_before: int = 0
    legacy_mismatched_probe_after: int = 0
    canonical_written_from_probe: int = 0
    canonical_unchanged_match_probe: int = 0
    touched_sources: set[str] = field(default_factory=set)

    def as_dict(self) -> dict:
        return {
            "probe_attempted": self.probe_attempted,
            "probe_succeeded": self.probe_succeeded,
            "probe_failed": self.probe_failed,
            "unchanged": self.unchanged,
            "skipped": self.skipped,
            "failed": self.failed,
            "changed": self.changed,
            "examined": self.examined,
            "canonical_null": {
                "before": self.canonical_null_before,
                "after": self.canonical_null_after,
            },
            "canonical_set": {
                "before": self.canonical_set_before,
                "after": self.canonical_set_after,
            },
            "canonical_matched_probe": {
                "before": self.canonical_matched_probe_before,
                "after": self.canonical_matched_probe_after,
            },
            "canonical_mismatched_probe": {
                "before": self.canonical_mismatched_probe_before,
                "after": self.canonical_mismatched_probe_after,
            },
            "legacy_null": {
                "before": self.legacy_null_before,
                "after": self.legacy_null_after,
            },
            "legacy_set": {
                "before": self.legacy_set_before,
                "after": self.legacy_set_after,
            },
            "legacy_matched_probe": {
                "before": self.legacy_matched_probe_before,
                "after": self.legacy_matched_probe_after,
            },
            "legacy_mismatched_probe": {
                "before": self.legacy_mismatched_probe_before,
                "after": self.legacy_mismatched_probe_after,
            },
            "canonical_written_from_probe": {
                "before": 0,
                "after": self.canonical_written_from_probe,
            },
            "canonical_unchanged_match_probe": {
                "before": 0,
                "after": self.canonical_unchanged_match_probe,
            },
            "touched_sources": sorted(self.touched_sources or set()),
        }


def _positive_seconds(value: object) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def _canonical_served_duration_seconds(rec: dict) -> float | None:
    return _positive_seconds(rec.get("served_duration_seconds"))


def _legacy_served_duration_seconds(rec: dict) -> float | None:
    audio = rec.get("audio") or {}
    return _positive_seconds(audio.get("duration_served")) if isinstance(audio, dict) else None


def _jsonl_row(
    *,
    source_key: str,
    uid: str,
    outcome: str,
    reason: str,
    before: float | None,
    after: float | None,
    canonical_before: float | None,
    canonical_after: float | None,
    legacy_before: float | None,
    legacy_after: float | None,
    probe: float | None,
) -> dict:
    return {
        "source_key": source_key,
        "uid": uid,
        "outcome": outcome,
        "reason": reason,
        "before_served_duration_seconds": before,
        "after_served_duration_seconds": after,
        "canonical_before_served_duration_seconds": canonical_before,
        "canonical_after_served_duration_seconds": canonical_after,
        "legacy_before_served_duration_seconds": legacy_before,
        "legacy_after_served_duration_seconds": legacy_after,
        "probe_served_duration_seconds": probe,
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


def _matches_probe(value: float | None, probe: float) -> bool:
    return value is not None and abs(value - probe) <= _DURATION_CHANGE_TOLERANCE_SECONDS


def _capture_before_after_counts(
    summary: NormalizeSummary,
    *,
    canonical_before: float | None,
    canonical_after: float | None,
    legacy_before: float | None,
    legacy_after: float | None,
    probe: float | None,
) -> None:
    if canonical_before is None:
        summary.canonical_null_before += 1
    else:
        summary.canonical_set_before += 1
    if canonical_after is None:
        summary.canonical_null_after += 1
    else:
        summary.canonical_set_after += 1

    if legacy_before is None:
        summary.legacy_null_before += 1
    else:
        summary.legacy_set_before += 1
    if legacy_after is None:
        summary.legacy_null_after += 1
    else:
        summary.legacy_set_after += 1

    if probe is None:
        return
    if _matches_probe(canonical_before, probe):
        summary.canonical_matched_probe_before += 1
    elif canonical_before is not None:
        summary.canonical_mismatched_probe_before += 1
    if _matches_probe(canonical_after, probe):
        summary.canonical_matched_probe_after += 1
    elif canonical_after is not None:
        summary.canonical_mismatched_probe_after += 1

    if _matches_probe(legacy_before, probe):
        summary.legacy_matched_probe_before += 1
    elif legacy_before is not None:
        summary.legacy_mismatched_probe_before += 1
    if _matches_probe(legacy_after, probe):
        summary.legacy_matched_probe_after += 1
    elif legacy_after is not None:
        summary.legacy_mismatched_probe_after += 1


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
            before = record_served_duration_seconds(rec)
            canonical_before = _canonical_served_duration_seconds(rec)
            legacy_before = _legacy_served_duration_seconds(rec)
            rows.append(
                _jsonl_row(
                    source_key=source_key,
                    uid=uid,
                    outcome="skipped",
                    reason="max-items",
                    before=before,
                    after=before,
                    canonical_before=canonical_before,
                    canonical_after=canonical_before,
                    legacy_before=legacy_before,
                    legacy_after=legacy_before,
                    probe=None,
                )
            )
            summary.skipped += 1
            _capture_before_after_counts(
                summary,
                canonical_before=canonical_before,
                canonical_after=canonical_before,
                legacy_before=legacy_before,
                legacy_after=legacy_before,
                probe=None,
            )
            continue
        processed += 1
        summary.examined += 1
        before = record_served_duration_seconds(rec)
        canonical_before = _canonical_served_duration_seconds(rec)
        legacy_before = _legacy_served_duration_seconds(rec)

        audio = rec.get("audio") or {}
        has_hosted_audio = bool(audio.get("key"))
        if not probe_existing and has_hosted_audio and canonical_before is not None:
            rows.append(
                _jsonl_row(
                    source_key=source_key,
                    uid=uid,
                    outcome="unchanged",
                    reason="canonical-present-probe-disabled",
                    before=before,
                    after=before,
                    canonical_before=canonical_before,
                    canonical_after=canonical_before,
                    legacy_before=legacy_before,
                    legacy_after=legacy_before,
                    probe=None,
                )
            )
            summary.unchanged += 1
            _capture_before_after_counts(
                summary,
                canonical_before=canonical_before,
                canonical_after=canonical_before,
                legacy_before=legacy_before,
                legacy_after=legacy_before,
                probe=None,
            )
            continue
        if not has_hosted_audio and canonical_before is not None:
            rows.append(
                _jsonl_row(
                    source_key=source_key,
                    uid=uid,
                    outcome="unchanged",
                    reason="canonical-present-no-hosted-audio",
                    before=before,
                    after=before,
                    canonical_before=canonical_before,
                    canonical_after=canonical_before,
                    legacy_before=legacy_before,
                    legacy_after=legacy_before,
                    probe=None,
                )
            )
            summary.unchanged += 1
            _capture_before_after_counts(
                summary,
                canonical_before=canonical_before,
                canonical_after=canonical_before,
                legacy_before=legacy_before,
                legacy_after=legacy_before,
                probe=None,
            )
            continue

        probed = None
        probe_reason = "probe-disabled"
        if probe_existing and has_hosted_audio:
            summary.probe_attempted += 1
            try:
                probed, probe_reason = probe_hosted_audio_duration_seconds(
                    storage,
                    audio["key"],
                    ffmpeg_binary=ffmpeg_binary,
                )
            except Exception:
                probed, probe_reason = None, "probe-exception"
        if probed is not None and probed > 0:
            summary.probe_succeeded += 1
            had_canonical = canonical_before is not None
            if apply:
                set_served_duration_seconds(rec, probed)
            canonical_after = _canonical_served_duration_seconds(rec)
            legacy_after = _legacy_served_duration_seconds(rec)
            rows.append(
                _jsonl_row(
                    source_key=source_key,
                    uid=uid,
                    outcome="probed",
                    reason=probe_reason,
                    before=before,
                    after=record_served_duration_seconds(rec),
                    canonical_before=canonical_before,
                    canonical_after=canonical_after,
                    legacy_before=legacy_before,
                    legacy_after=legacy_after,
                    probe=probed,
                )
            )
            _capture_before_after_counts(
                summary,
                canonical_before=canonical_before,
                canonical_after=canonical_after,
                legacy_before=legacy_before,
                legacy_after=legacy_after,
                probe=probed,
            )
            if (
                apply
                and not _matches_probe(canonical_before, probed)
                and _matches_probe(canonical_after, probed)
            ):
                summary.canonical_written_from_probe += 1
            if _matches_probe(canonical_before, probed) and _matches_probe(canonical_after, probed):
                summary.canonical_unchanged_match_probe += 1
            if _duration_changed(before, probed, had_canonical=had_canonical):
                summary.changed += 1
                changed = True
                summary.touched_sources.add(source_key)
            else:
                summary.unchanged += 1
            continue

        outcome = "failed" if has_hosted_audio else "skipped"
        reason = probe_reason if has_hosted_audio else "no-hosted-audio"
        canonical_after = _canonical_served_duration_seconds(rec)
        legacy_after = _legacy_served_duration_seconds(rec)
        rows.append(
            _jsonl_row(
                source_key=source_key,
                uid=uid,
                outcome=outcome,
                reason=reason,
                before=before,
                after=record_served_duration_seconds(rec),
                canonical_before=canonical_before,
                canonical_after=canonical_after,
                legacy_before=legacy_before,
                legacy_after=legacy_after,
                probe=None,
            )
        )
        _capture_before_after_counts(
            summary,
            canonical_before=canonical_before,
            canonical_after=canonical_after,
            legacy_before=legacy_before,
            legacy_after=legacy_after,
            probe=None,
        )
        if outcome == "failed":
            if probe_existing:
                summary.probe_failed += 1
            summary.failed += 1
        else:
            summary.skipped += 1
    return rows, summary, changed


def _render_summary(summary: NormalizeSummary, *, apply: bool) -> str:
    verb = "applied" if apply else "dry-run"
    rows = [
        ("Probe attempted", 0, summary.probe_attempted),
        ("Probe succeeded", 0, summary.probe_succeeded),
        ("Probe failed", 0, summary.probe_failed),
        (
            "Canonical served_duration_seconds null",
            summary.canonical_null_before,
            summary.canonical_null_after,
        ),
        (
            "Canonical served_duration_seconds set",
            summary.canonical_set_before,
            summary.canonical_set_after,
        ),
        (
            "Canonical served_duration_seconds matched probe",
            summary.canonical_matched_probe_before,
            summary.canonical_matched_probe_after,
        ),
        (
            "Canonical served_duration_seconds did not match probe",
            summary.canonical_mismatched_probe_before,
            summary.canonical_mismatched_probe_after,
        ),
        (
            "Legacy-readable served duration null",
            summary.legacy_null_before,
            summary.legacy_null_after,
        ),
        (
            "Legacy-readable served duration set",
            summary.legacy_set_before,
            summary.legacy_set_after,
        ),
        (
            "Legacy-readable served duration matched probe",
            summary.legacy_matched_probe_before,
            summary.legacy_matched_probe_after,
        ),
        (
            "Legacy-readable served duration did not match probe",
            summary.legacy_mismatched_probe_before,
            summary.legacy_mismatched_probe_after,
        ),
        ("Canonical written from probe", 0, summary.canonical_written_from_probe),
        (
            "Canonical unchanged because already matched probe",
            0,
            summary.canonical_unchanged_match_probe,
        ),
    ]
    return "\n".join(
        [
            f"# Duration normalization ({verb})",
            "",
            f"- examined: {summary.examined}",
            f"- changed: {summary.changed}",
            f"- probe attempted: {summary.probe_attempted}",
            f"- probe succeeded: {summary.probe_succeeded}",
            f"- probe failed: {summary.probe_failed}",
            f"- unchanged: {summary.unchanged}",
            f"- skipped: {summary.skipped}",
            f"- failed: {summary.failed}",
            f"- touched sources: {len(summary.touched_sources or set())}",
            "",
            "| Metric | Before | After |",
            "|---|---:|---:|",
            *[f"| {label} | {before} | {after} |" for label, before, after in rows],
            "",
            (
                "_Note: 'After' reflects persisted state after this run; in dry-run mode it "
                "matches the current record state._"
            ),
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
    ap.add_argument(
        "--ffmpeg-binary",
        default="ffmpeg",
        help="ffmpeg binary path; ffprobe is derived from it by replacing the trailing component",
    )
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

    ffmpeg_binary = args.ffmpeg_binary
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
        total.probe_attempted += summary.probe_attempted
        total.probe_succeeded += summary.probe_succeeded
        total.probe_failed += summary.probe_failed
        total.unchanged += summary.unchanged
        total.skipped += summary.skipped
        total.failed += summary.failed
        total.changed += summary.changed
        total.examined += summary.examined
        total.canonical_null_before += summary.canonical_null_before
        total.canonical_null_after += summary.canonical_null_after
        total.canonical_set_before += summary.canonical_set_before
        total.canonical_set_after += summary.canonical_set_after
        total.canonical_matched_probe_before += summary.canonical_matched_probe_before
        total.canonical_matched_probe_after += summary.canonical_matched_probe_after
        total.canonical_mismatched_probe_before += summary.canonical_mismatched_probe_before
        total.canonical_mismatched_probe_after += summary.canonical_mismatched_probe_after
        total.legacy_null_before += summary.legacy_null_before
        total.legacy_null_after += summary.legacy_null_after
        total.legacy_set_before += summary.legacy_set_before
        total.legacy_set_after += summary.legacy_set_after
        total.legacy_matched_probe_before += summary.legacy_matched_probe_before
        total.legacy_matched_probe_after += summary.legacy_matched_probe_after
        total.legacy_mismatched_probe_before += summary.legacy_mismatched_probe_before
        total.legacy_mismatched_probe_after += summary.legacy_mismatched_probe_after
        total.canonical_written_from_probe += summary.canonical_written_from_probe
        total.canonical_unchanged_match_probe += summary.canonical_unchanged_match_probe
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
