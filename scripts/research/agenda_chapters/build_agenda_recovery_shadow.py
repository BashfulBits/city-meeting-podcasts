#!/usr/bin/env python
"""Build a source-only recovered agenda shadow from completed raw LLM responses.

This command never calls a model and never mutates episode state.  It preserves the strict accepted
items, adds separately marked source-recovered candidates, and retains every unresolved rejection
for later OCR/LLM investigation.  Keep its output outside the repository.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from citypods.chapter_titles import recover_agenda_item_extractor_response

DATASET_VERSION = 1


def _strict_item(item: Any) -> dict[str, object]:
    return {
        "display_ref": item.display_ref,
        "title": item.title,
        "evidence_text": item.evidence_quote,
        "line_start": item.line_start,
        "line_end": item.line_end,
        "evidence_span_repaired": item.evidence_span_repaired,
    }


def _recovered_item(item: Any) -> dict[str, object]:
    return {
        "raw_index": item.raw_index,
        "display_ref": item.display_ref,
        "original_display_ref": item.original_display_ref,
        "title": item.title,
        "evidence_quote": item.evidence_quote,
        "evidence_text": item.source_evidence,
        "line_start": item.line_start,
        "line_end": item.line_end,
        "recovery_method": item.recovery_method,
        "evidence_span_repaired": item.evidence_span_repaired,
    }


def build_shadow(raw_root: Path, agenda_cache: Path) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    for path in sorted(raw_root.rglob("*.json")):
        if path.name == "summary.json":
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        episode = payload.get("episode")
        if not isinstance(episode, dict):
            continue
        uid = episode.get("uid")
        slug = episode.get("slug")
        if not isinstance(uid, str) or not isinstance(slug, str):
            continue
        agenda_path = agenda_cache / f"{slug}--{uid}.agenda.txt"
        if not agenda_path.exists():
            rows.append(
                {
                    "provider": episode.get("provider"),
                    "slug": slug,
                    "uid": uid,
                    "source_file": str(path),
                    "agenda_error": "agenda cache missing",
                    "strict_items": [],
                    "recovered_items": [],
                    "unrecovered": [],
                }
            )
            continue
        assessment = recover_agenda_item_extractor_response(
            str(payload.get("raw_response") or ""),
            agenda_text=agenda_path.read_text(encoding="utf-8", errors="replace"),
        )
        rows.append(
            {
                "provider": episode.get("provider"),
                "slug": slug,
                "uid": uid,
                "body": episode.get("body"),
                "source_file": str(path),
                "strict_items": [_strict_item(item) for item in assessment.items],
                "recovered_items": [_recovered_item(item) for item in assessment.recovered],
                "unrecovered": [
                    {
                        "index": item.index,
                        "display_ref": item.display_ref,
                        "reason": item.reason,
                    }
                    for item in assessment.unrecovered
                ],
            }
        )
    strict_count = sum(len(row["strict_items"]) for row in rows)
    recovered_count = sum(len(row["recovered_items"]) for row in rows)
    unrecovered_count = sum(len(row["unrecovered"]) for row in rows)
    return {
        "version": DATASET_VERSION,
        "purpose": "shadow-only source recovery of rejected agenda extraction items",
        "rows": rows,
        "summary": {
            "rows": len(rows),
            "strict_items": strict_count,
            "recovered_items": recovered_count,
            "unrecovered_items": unrecovered_count,
            "rows_with_recovered_items": sum(bool(row["recovered_items"]) for row in rows),
            "rows_with_unrecovered_items": sum(bool(row["unrecovered"]) for row in rows),
            "recovery_methods": dict(
                Counter(item["recovery_method"] for row in rows for item in row["recovered_items"])
            ),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--agenda-cache", type=Path, required=True)
    parser.add_argument("--write", type=Path, required=True)
    args = parser.parse_args(argv)
    result = build_shadow(args.raw_root, args.agenda_cache)
    args.write.parent.mkdir(parents=True, exist_ok=True)
    args.write.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(result["summary"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
