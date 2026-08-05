#!/usr/bin/env python
"""Summarize development-only confidence/coverage tradeoffs for a scorer output.

This is a read-only GH#1078 research diagnostic. It consumes the per-item validation details
written by ``train_transition_scorer.py`` and never reads or writes production artifacts. Provider
labels in the input are used only to measure risk/coverage; they are not runtime features.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


def _flatten(details: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for episode in details:
        for item_index, item in (episode.get("item_diagnostics") or {}).items():
            rows.append(
                {
                    "uid": episode.get("uid"),
                    "item_index": item_index,
                    **item,
                }
            )
    return rows


def _threshold_row(
    rows: Sequence[Mapping[str, Any]],
    *,
    field: str,
    threshold: float,
) -> dict[str, Any]:
    selected = [row for row in rows if float(row.get(field) or 0.0) >= threshold]
    hits = sum(bool(row.get("learned_hit")) for row in selected)
    total_hits = sum(bool(row.get("learned_hit")) for row in rows)
    episodes = {str(row.get("uid")): [] for row in rows}
    for row in rows:
        episodes[str(row.get("uid"))].append(row)
    compact_episodes = sum(
        bool(items) and all(float(item.get(field) or 0.0) >= threshold for item in items)
        for items in episodes.values()
    )
    return {
        "field": field,
        "threshold": threshold,
        "item_count": len(rows),
        "selected_items": len(selected),
        "item_coverage": round(len(selected) / len(rows), 4) if rows else None,
        "selected_hits": hits,
        "total_hits": total_hits,
        "recall_over_all_items": round(hits / len(rows), 4) if rows else None,
        "conditional_hit_rate": round(hits / len(selected), 4) if selected else None,
        "episode_count": len(episodes),
        "episodes_all_items_selected": compact_episodes,
        "episode_coverage": (round(compact_episodes / len(episodes), 4) if episodes else None),
    }


def analyze(
    payload: Mapping[str, Any], *, model: str, field: str, thresholds: Sequence[float]
) -> dict[str, Any]:
    model_payload = (payload.get("models") or {}).get(model) or {}
    details = model_payload.get("validation_episode_details") or []
    rows = _flatten(details)
    return {
        "model": model,
        "field": field,
        "episodes": len(details),
        "items": len(rows),
        "thresholds": [
            _threshold_row(rows, field=field, threshold=float(threshold))
            for threshold in thresholds
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--model", default="hist_gradient_boosting")
    parser.add_argument("--field", choices=("top_probability", "margin"), default="margin")
    parser.add_argument(
        "--threshold",
        type=float,
        nargs="+",
        default=[0.0, 0.001, 0.005, 0.01, 0.02, 0.05, 0.1],
    )
    parser.add_argument("--write", type=Path, required=True)
    args = parser.parse_args(argv)
    result = analyze(
        json.loads(args.input.read_text(encoding="utf-8")),
        model=args.model,
        field=args.field,
        thresholds=args.threshold,
    )
    args.write.parent.mkdir(parents=True, exist_ok=True)
    args.write.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
