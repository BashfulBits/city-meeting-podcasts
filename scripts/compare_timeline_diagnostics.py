#!/usr/bin/env python
"""Compare before/after timeline-audio diagnostic JSONL artifacts.

The "before" artifact should come from a manual repair-cohort audit run. Rows selected for repair
carry ``repair_selected: true``. The "after" artifact should come from a later audit after the
audio/transcript lanes have had time to consume the repair flags.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _load_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open() as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{lineno}: invalid JSON: {exc}") from exc
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _delta(row: dict[str, Any]) -> float | None:
    raw = row.get("stream_delta")
    if not isinstance(raw, (int, float)):
        return None
    return abs(float(raw))


def _cohort_rows(rows: list[dict[str, Any]], cohort: str | None) -> list[dict[str, Any]]:
    selected = [r for r in rows if r.get("repair_selected") is True]
    if cohort:
        selected = [r for r in selected if r.get("repair_cohort") == cohort]
    return selected


def _max_delta_by_uid(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_uid: dict[str, dict[str, Any]] = {}
    for row in rows:
        uid = row.get("uid")
        if not uid:
            continue
        uid = str(uid)
        current = by_uid.get(uid)
        if current is None or (_delta(row) or -1.0) > (_delta(current) or -1.0):
            by_uid[uid] = row
    return by_uid


def compare(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    *,
    cohort: str | None,
    success_delta: float,
) -> dict[str, Any]:
    before_by_uid = _max_delta_by_uid(_cohort_rows(before, cohort))
    after_by_uid = _max_delta_by_uid(after)

    details: list[dict[str, Any]] = []
    fixed = still_mismatch = missing_after = worsened = audio_key_changed = 0
    for uid, before_row in sorted(before_by_uid.items()):
        after_row = after_by_uid.get(uid)
        before_delta = _delta(before_row)
        after_delta = _delta(after_row) if after_row else None
        if after_row is None:
            outcome = "missing-after"
            missing_after += 1
        elif after_row.get("check") == "ok" or (
            after_delta is not None and after_delta <= success_delta
        ):
            outcome = "fixed"
            fixed += 1
        else:
            outcome = "still-mismatch"
            still_mismatch += 1
        if (
            before_delta is not None
            and after_delta is not None
            and after_delta > before_delta + success_delta
        ):
            worsened += 1
        changed_audio = bool(
            after_row and before_row.get("audio_key") != after_row.get("audio_key")
        )
        if changed_audio:
            audio_key_changed += 1
        details.append(
            {
                "uid": uid,
                "before_slug": before_row.get("slug"),
                "after_slug": after_row.get("slug") if after_row else None,
                "before_check": before_row.get("check"),
                "after_check": after_row.get("check") if after_row else None,
                "before_delta": before_delta,
                "after_delta": after_delta,
                "audio_key_changed": changed_audio,
                "outcome": outcome,
            }
        )

    return {
        "cohort": cohort,
        "success_delta": success_delta,
        "selected_uids": len(before_by_uid),
        "fixed": fixed,
        "still_mismatch": still_mismatch,
        "missing_after": missing_after,
        "worsened": worsened,
        "audio_key_changed": audio_key_changed,
        "details": details,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("before", type=Path)
    ap.add_argument("after", type=Path)
    ap.add_argument("--cohort", help="only compare rows selected for this repair cohort")
    ap.add_argument(
        "--success-delta",
        type=float,
        default=0.1,
        help="stream-delta threshold considered fixed in the after artifact",
    )
    ap.add_argument("--summary-only", action="store_true", help="omit per-UID details")
    args = ap.parse_args(argv)

    if args.success_delta < 0:
        ap.error("--success-delta must be >= 0")
    result = compare(
        _load_rows(args.before),
        _load_rows(args.after),
        cohort=args.cohort,
        success_delta=args.success_delta,
    )
    if args.summary_only:
        result = {k: v for k, v in result.items() if k != "details"}
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
