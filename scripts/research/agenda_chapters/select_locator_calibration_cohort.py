#!/usr/bin/env python
"""Select a diverse, source-backed full-context locator calibration cohort.

This is a read-only selector. It uses only the frozen test split's episode metadata and generated
agenda availability; provider chapter starts remain in the separate packet gold section and are
never part of a model request. The fixed quotas intentionally limit repeated bodies so a prompt
or provider quirk cannot dominate a 16-episode confidence experiment.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

AGENDA_MODEL = "mistral/mistral-medium-2508"
MIN_DURATION_SECONDS = 20 * 60
PROVIDER_QUOTAS = {"granicus": 8, "swagit": 8}
DURATION_QUOTAS = {
    # Granicus has only one long test episode whose agenda extraction is complete enough for
    # this locator slice; do not admit known high-rejection agenda artifacts just to fill a bucket.
    "granicus": {"under-2h": 4, "2-to-4h": 3, "4-to-8h": 1},
    "swagit": {"under-2h": 2, "2-to-4h": 4, "4-to-8h": 2},
}
MIN_GENERATED_ITEMS = 4
MAX_REJECTED_ITEMS = 10


def select_rows(manifest: dict[str, Any], *, split: str = "test") -> list[dict[str, Any]]:
    candidates = [
        row
        for row in manifest.get("episodes", [])
        if row.get("split") == split
        and row.get("provider") in PROVIDER_QUOTAS
        and not row.get("exclusions")
        and AGENDA_MODEL in (row.get("ready_models") or [])
        and float(row.get("duration_seconds") or 0.0) >= MIN_DURATION_SECONDS
        and len(((row.get("generated_agenda") or {}).get(AGENDA_MODEL) or {}).get("items") or [])
        >= MIN_GENERATED_ITEMS
        and int(
            ((row.get("generated_agenda") or {}).get(AGENDA_MODEL) or {}).get("rejected_count", 0)
        )
        <= MAX_REJECTED_ITEMS
    ]
    selected: list[dict[str, Any]] = []
    used_bodies: set[str] = set()
    for provider in PROVIDER_QUOTAS:
        provider_rows = [row for row in candidates if row.get("provider") == provider]
        # Reserve the rare long meetings before the abundant short/medium buckets consume the
        # same body families.
        bucket_order = ("4-to-8h", "2-to-4h", "under-2h")
        for bucket in bucket_order:
            quota = DURATION_QUOTAS[provider][bucket]
            bucket_rows = sorted(
                (
                    row
                    for row in provider_rows
                    if row.get("duration_bucket") == bucket
                    and str(row.get("body_key") or row.get("body")) not in used_bodies
                ),
                key=lambda row: (
                    -float(row.get("duration_seconds") or 0.0)
                    if bucket == "4-to-8h"
                    else float(row.get("duration_seconds") or 0.0),
                    str(row.get("uid")),
                ),
            )
            chosen: list[dict[str, Any]] = []
            for row in bucket_rows:
                body_key = str(row.get("body_key") or row.get("body"))
                if body_key in used_bodies:
                    continue
                chosen.append(row)
                used_bodies.add(body_key)
                if len(chosen) == quota:
                    break
            if len(chosen) != quota:
                raise ValueError(
                    f"cannot fill {provider}/{bucket}: needed {quota}, found {len(chosen)}"
                )
            selected.extend(chosen)
    if len(selected) != sum(PROVIDER_QUOTAS.values()):
        raise AssertionError("cohort size does not match provider quotas")
    return sorted(selected, key=lambda row: (str(row.get("provider")), str(row.get("uid"))))


def cohort_manifest(manifest: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "version": 1,
        "purpose": "read-only 16-episode confidence-calibration locator cohort",
        "agenda_model": AGENDA_MODEL,
        "source_split": "test",
        "provider_quotas": PROVIDER_QUOTAS,
        "duration_quotas": DURATION_QUOTAS,
        "body_cap": 1,
        "minimum_duration_seconds": MIN_DURATION_SECONDS,
        "minimum_generated_items": MIN_GENERATED_ITEMS,
        "maximum_rejected_items": MAX_REJECTED_ITEMS,
        "provider_labels_in_requests": False,
        "uids": [str(row["uid"]) for row in rows],
        "episodes": rows,
        "summary": {
            "episodes": len(rows),
            "providers": dict(Counter(str(row.get("provider")) for row in rows)),
            "duration_buckets": dict(Counter(str(row.get("duration_bucket")) for row in rows)),
            "bodies": len({str(row.get("body_key") or row.get("body")) for row in rows}),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--write", type=Path, required=True)
    parser.add_argument("--split", default="test")
    args = parser.parse_args(argv)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    rows = select_rows(manifest, split=args.split)
    result = cohort_manifest(manifest, rows)
    args.write.parent.mkdir(parents=True, exist_ok=True)
    args.write.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(result["summary"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
