#!/usr/bin/env python
"""Prepare a manual review packet for calibrated locator proposals.

This is research-only.  It joins model proposals by the source evidence reference
(``episode, agenda item, timed transcript unit``), so the reviewer adjudicates the
same evidence once even when several models chose it.  Provider chapter timings and
model identities are retained in the packet for analysis, but provider targets are
never included in the displayed review fields.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from citypods.chapter_locator import LocatorUnit, build_locator_units
from citypods.http import make_session
from scripts.research.agenda_chapters.build_locator_packets import _agenda_items
from scripts.research.agenda_chapters.train_transition_scorer import _artifact_bytes, _fetch


def _lines(value: bytes | None) -> list[str]:
    if not value:
        return []
    return value.decode("utf-8", errors="replace").splitlines()


def _context(units: list[LocatorUnit], index: int, *, radius: int = 3) -> list[dict[str, Any]]:
    start = max(0, index - radius)
    end = min(len(units), index + radius + 1)
    return [
        {
            "unit_id": unit.id,
            "start": round(unit.start, 3),
            "end": round(unit.end, 3),
            "text": unit.text,
            "is_selected": position == index,
        }
        for position, unit in enumerate(units[start:end], start=start)
    ]


def _result_anchors(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    route = (result.get("routes") or {}).get("full") or {}
    anchors = route.get("anchors") or []
    return [anchor for anchor in anchors if isinstance(anchor, dict)]


def build_review_packet(
    manifest: Mapping[str, Any],
    packets: Mapping[str, Any],
    results: Mapping[str, Mapping[str, Any]],
    *,
    cache_dir: Path | None,
    context_radius: int = 3,
) -> dict[str, Any]:
    rows = {str(row.get("uid")): row for row in manifest.get("episodes", [])}
    packet_rows = {str(row.get("uid")): row for row in packets.get("packets", [])}
    session = make_session()
    cases: dict[tuple[str, int, str], dict[str, Any]] = {}
    run_status: list[dict[str, Any]] = []
    errors = list(packets.get("errors") or [])

    for model, output in results.items():
        for result in output.get("results", []):
            uid = str(result.get("uid"))
            run_status.append(
                {
                    "model": model,
                    "uid": uid,
                    "status": result.get("status"),
                    "error": result.get("error"),
                    "has_full_route": bool((result.get("routes") or {}).get("full")),
                }
            )
            row = rows.get(uid)
            packet = packet_rows.get(uid)
            if not row or not packet:
                errors.append({"uid": uid, "model": model, "error": "missing packet/manifest row"})
                continue
            try:
                words, vtt, unit_source = _artifact_bytes(session, row, cache_dir=cache_dir)
                units, unit_source = build_locator_units(words_data=words, vtt_data=vtt)
            except Exception as exc:  # pragma: no cover - network failures are packet data
                errors.append({"uid": uid, "model": model, "error": f"{type(exc).__name__}: {exc}"})
                continue
            unit_by_id = {unit.id: (position, unit) for position, unit in enumerate(units)}
            agenda_model = str(packets.get("agenda_model") or "mistral/mistral-medium-2508")
            agenda = _agenda_items(row, agenda_model)
            agenda_by_index = {item.index: item for item in agenda}
            agenda_bytes = _fetch(session, (row.get("agenda") or {}).get("url"))
            agenda_lines = _lines(agenda_bytes)
            for anchor in _result_anchors(result):
                try:
                    item_index = int(anchor["agenda_item_index"])
                    unit_id = str(anchor["unit_id"])
                except (KeyError, TypeError, ValueError):
                    continue
                item = agenda_by_index.get(item_index)
                unit_info = unit_by_id.get(unit_id)
                if item is None or unit_info is None:
                    errors.append(
                        {
                            "uid": uid,
                            "model": model,
                            "error": "proposal references unknown agenda item or unit",
                            "agenda_item_index": item_index,
                            "unit_id": unit_id,
                        }
                    )
                    continue
                position, unit = unit_info
                key = (uid, item_index, unit_id)
                case = cases.setdefault(
                    key,
                    {
                        "case_id": f"{uid}:{item_index}:{unit_id}",
                        "episode": {
                            "uid": uid,
                            "provider": row.get("provider"),
                            "slug": row.get("slug"),
                            "body": row.get("body"),
                            "published": row.get("published"),
                            "duration_seconds": row.get("duration_seconds"),
                            "duration_bucket": row.get("duration_bucket"),
                        },
                        "agenda": {
                            "url": (row.get("agenda") or {}).get("url"),
                            "lines": agenda_lines,
                        },
                        "agenda_item": {
                            "index": item_index,
                            "title": item.title,
                            "display_ref": item.display_ref,
                            "evidence_text": item.evidence_text,
                        },
                        "evidence_reference": {
                            "unit_id": unit.id,
                            "start": round(unit.start, 3),
                            "end": round(unit.end, 3),
                            "transcript_text": unit.text,
                            "context": _context(units, position, radius=context_radius),
                            "unit_source": unit_source or packet.get("unit_source"),
                        },
                        "proposals": [],
                    },
                )
                proposal = {
                    "model": model,
                    "start": anchor.get("start"),
                    "confidence": anchor.get("confidence"),
                    "item_confidence": anchor.get("item_confidence"),
                    "boundary_confidence": anchor.get("boundary_confidence"),
                    "evidence_type": anchor.get("evidence_type"),
                    "alternative_agenda_item_index": anchor.get("alternative_agenda_item_index"),
                    "alternative_unit_id": anchor.get("alternative_unit_id"),
                    "uncertainty_reason": anchor.get("uncertainty_reason"),
                    "calibration_missing_fields": anchor.get("calibration_missing_fields") or [],
                    "transition_quote": anchor.get("transition_quote"),
                }
                case["proposals"].append(proposal)

    ordered = sorted(
        cases.values(),
        key=lambda case: (
            str(case["episode"].get("provider")),
            str(case["episode"].get("uid")),
            int(case["agenda_item"].get("index", 0)),
            float(case["evidence_reference"].get("start", 0.0)),
        ),
    )
    for case in ordered:
        case["models"] = sorted({proposal["model"] for proposal in case["proposals"]})
    return {
        "version": 1,
        "purpose": "research-only calibrated locator manual adjudication",
        "label_schema": {
            "evidence_status": ["supported", "no_evidence", "ambiguous"],
            "item_correctness": ["correct", "incorrect"],
            "boundary_validity": ["valid", "invalid", "no_boundary"],
            "rule": (
                "Choose evidence status first. For supported evidence, choose item correctness "
                "and boundary validity independently."
            ),
        },
        "models": sorted(results),
        "provider_targets_included": False,
        "cases": ordered,
        "run_status": run_status,
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--packets", type=Path, required=True)
    parser.add_argument("--result", action="append", required=True, metavar="MODEL=PATH")
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--context-radius", type=int, default=3)
    parser.add_argument("--write", type=Path, required=True)
    args = parser.parse_args(argv)
    result_files: dict[str, Mapping[str, Any]] = {}
    for spec in args.result:
        model, separator, path = spec.partition("=")
        if not separator or not model or not path:
            raise SystemExit("--result must be MODEL=PATH")
        result_files[model] = json.loads(Path(path).read_text(encoding="utf-8"))
    result = build_review_packet(
        json.loads(args.manifest.read_text(encoding="utf-8")),
        json.loads(args.packets.read_text(encoding="utf-8")),
        result_files,
        cache_dir=args.cache_dir,
        context_radius=max(0, args.context_radius),
    )
    args.write.parent.mkdir(parents=True, exist_ok=True)
    args.write.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        json.dumps({"cases": len(result["cases"]), "errors": len(result["errors"])}, sort_keys=True)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
