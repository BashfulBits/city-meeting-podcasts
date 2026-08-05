#!/usr/bin/env python
"""Prepare a fixed-case review packet for agenda evidence recovery.

This is a scoring-only follow-up to the original locator crosswalk packet. It keeps the original
review cases fixed, but replaces the candidate list with strict plus shadow-recovered agenda items
from a recovered manifest. Only cases with at least one recovered item are included. The packet
uses a new case-id namespace so its decisions cannot be confused with the original labels.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "mistral/mistral-medium-2508"


def _candidate(item: dict[str, Any], index: int) -> dict[str, object]:
    recovery_method = item.get("recovery_method")
    return {
        "candidate_id": f"C{index + 1:03d}",
        "index": index,
        "title": item.get("title"),
        "display_ref": item.get("display_ref"),
        "evidence_text": item.get("evidence_text"),
        "agenda_line_start": item.get("line_start"),
        "agenda_line_end": item.get("line_end"),
        "recovered": bool(recovery_method),
        "recovery_method": recovery_method,
    }


def prepare_packet(
    base_packet: dict[str, Any],
    recovered_manifest: dict[str, Any],
    *,
    model: str = DEFAULT_MODEL,
) -> dict[str, object]:
    """Build a packet without changing the base packet's selected episodes."""

    manifest_by_uid = {
        row["uid"]: row for row in recovered_manifest.get("episodes", []) if row.get("uid")
    }
    cases: list[dict[str, object]] = []
    for base_case in base_packet.get("cases", []):
        uid = base_case.get("episode", {}).get("uid")
        row = manifest_by_uid.get(uid)
        if row is None:
            continue
        generated = (row.get("generated_agenda") or {}).get(model) or {}
        items = generated.get("items") or []
        recovered_items = [item for item in items if item.get("recovery_method")]
        if not recovered_items:
            continue
        cases.append(
            {
                "case_id": f"RXR-{base_case['case_id'].split('-')[-1]}",
                "original_case_id": base_case["case_id"],
                "category": base_case.get("category"),
                "episode": base_case.get("episode"),
                "provider_chapter": base_case.get("provider_chapter"),
                "agenda": base_case.get("agenda"),
                "recovery_summary": {
                    "strict_candidate_count": len(items) - len(recovered_items),
                    "recovered_candidate_count": len(recovered_items),
                    "raw_rejected_count": generated.get("rejected_count", 0),
                    "unrecovered_count": generated.get("unrecovered_count", 0),
                },
                "candidates": [_candidate(item, index) for index, item in enumerate(items)],
            }
        )
    return {
        "version": 1,
        "purpose": "fixed-case human review of shadow agenda evidence recovery",
        "model": model,
        "split": base_packet.get("split"),
        "source_packet": base_packet.get("seed"),
        "timings_included": False,
        "unique_episodes": len({case["episode"]["uid"] for case in cases}),
        "cases": cases,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-packet", type=Path, required=True)
    parser.add_argument("--recovered-manifest", type=Path, required=True)
    parser.add_argument("--write", type=Path, required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args(argv)
    packet = prepare_packet(
        json.loads(args.base_packet.read_text(encoding="utf-8")),
        json.loads(args.recovered_manifest.read_text(encoding="utf-8")),
        model=args.model,
    )
    args.write.parent.mkdir(parents=True, exist_ok=True)
    args.write.write_text(json.dumps(packet, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"cases": len(packet["cases"]), "unique_episodes": packet["unique_episodes"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
