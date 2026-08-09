#!/usr/bin/env python
"""Score a completed locator shadow run without changing episode state.

The locator request deliberately does not contain provider chapters.  This report joins the
completed model responses to a separate, hidden ``gold.json`` and optional scoring-only
crosswalk after the call has finished.  It reports quality, structural, and operational signals
separately; in particular, ``suspected_wrong_item`` is not a human-adjudicated false-positive
count and must never be presented as one.

Example::

    PYTHONPATH=. python scripts/research/agenda_chapters/report_locator_shadow.py \
      --results /private/tmp/locator-run.json \
      --gold /private/tmp/gold.json \
      --crosswalk /private/tmp/crosswalk.json \
      --write /private/tmp/locator-report.json --tolerance 60

Inputs and outputs are research artifacts.  Credentials, raw transcripts, and raw model payloads
remain outside the repository.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

REPORT_VERSION = 1


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float) and math.isfinite(float(value)):
        return float(value)
    return None


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _gold_starts(gold_row: Mapping[str, Any]) -> list[float]:
    return [
        start
        for chapter in gold_row.get("chapters", [])
        if isinstance(chapter, Mapping) and (start := _number(chapter.get("start"))) is not None
    ]


def _strong_targets(crosswalk_row: Mapping[str, Any]) -> list[tuple[int, float]]:
    """Return only source crosswalk relationships strong enough for scoring item correctness."""
    targets: list[tuple[int, float]] = []
    for chapter in crosswalk_row.get("provider_chapters", []):
        if not isinstance(chapter, Mapping) or chapter.get("status") != "strong":
            continue
        item_index = chapter.get("best_generated_item_index")
        start = _number(chapter.get("start"))
        if isinstance(item_index, int) and not isinstance(item_index, bool) and start is not None:
            targets.append((item_index, start))
    return targets


def _nearest_matches(
    starts: Sequence[float], targets: Sequence[float], tolerance: float
) -> list[tuple[int, int, float]]:
    """Greedily make one-to-one nearest matches, preserving duplicate-safe denominators."""
    pairs = sorted(
        (abs(start - target), index, target_index)
        for index, start in enumerate(starts)
        for target_index, target in enumerate(targets)
        if abs(start - target) <= tolerance
    )
    used_starts: set[int] = set()
    used_targets: set[int] = set()
    matches: list[tuple[int, int, float]] = []
    for distance, index, target_index in pairs:
        if index in used_starts or target_index in used_targets:
            continue
        used_starts.add(index)
        used_targets.add(target_index)
        matches.append((index, target_index, distance))
    return matches


def _anchor_starts(route: Mapping[str, Any]) -> list[dict[str, Any]]:
    anchors = route.get("anchors")
    if not isinstance(anchors, list):
        return []
    rows: list[dict[str, Any]] = []
    for anchor in anchors:
        if not isinstance(anchor, Mapping):
            continue
        start = _number(anchor.get("start"))
        if start is None:
            continue
        item_index = anchor.get("agenda_item_index")
        rows.append(
            {
                "start": start,
                "agenda_item_index": item_index
                if isinstance(item_index, int) and not isinstance(item_index, bool)
                else None,
                "unit_id": anchor.get("unit_id"),
            }
        )
    return rows


def score_route(
    route_results: Iterable[Mapping[str, Any]],
    gold_by_uid: Mapping[str, Mapping[str, Any]],
    *,
    crosswalk_by_uid: Mapping[str, Mapping[str, Any]] | None = None,
    tolerance: float = 60.0,
) -> dict[str, Any]:
    """Aggregate one model route from completed per-episode results.

    Provider-start recall/precision is timing-only.  Correct-item precision uses only ``strong``
    crosswalk rows and is reported separately, so a weak or ambiguous title join cannot silently
    become a false-positive label.  All counts are integer audit signals; percentages are derived
    only when their denominator is known.
    """
    if tolerance < 0:
        raise ValueError("tolerance must be non-negative")
    totals: Counter[str] = Counter()
    boundary_errors: list[float] = []
    per_provider: dict[str, Counter[str]] = defaultdict(Counter)
    completed_rows = 0
    route_name = None
    token_total = 0.0
    cost_total = 0.0
    latency_total = 0.0
    for row in route_results:
        uid = str(row.get("uid") or "")
        route = row.get("route") if isinstance(row, Mapping) else None
        route_name = route_name or (str(route) if route else None)
        provider = str(row.get("provider") or "unknown")
        status = str(row.get("status") or "completed")
        if status != "completed":
            totals["failed_episodes"] += 1
            per_provider[provider]["failed_episodes"] += 1
            continue
        completed_rows += 1
        totals["completed_episodes"] += 1
        per_provider[provider]["completed_episodes"] += 1
        gold_row = gold_by_uid.get(uid, {})
        crosswalk_row = (crosswalk_by_uid or {}).get(uid, {})
        gold_starts = _gold_starts(gold_row)
        anchors = _anchor_starts(row)
        totals["provider_chapters"] += len(gold_starts)
        totals["anchors"] += len(anchors)
        per_provider[provider]["provider_chapters"] += len(gold_starts)
        per_provider[provider]["anchors"] += len(anchors)
        timing_matches = _nearest_matches(
            [float(anchor["start"]) for anchor in anchors], gold_starts, tolerance
        )
        totals["provider_start_hits"] += len(timing_matches)
        per_provider[provider]["provider_start_hits"] += len(timing_matches)
        boundary_errors.extend(distance for _, _, distance in timing_matches)

        strong_targets = _strong_targets(crosswalk_row)
        correct = 0
        for anchor in anchors:
            item_index = anchor.get("agenda_item_index")
            matching = [
                abs(float(anchor["start"]) - target_start)
                for target_item, target_start in strong_targets
                if target_item == item_index
            ]
            if matching and min(matching) <= tolerance:
                correct += 1
            elif any(
                abs(float(anchor["start"]) - target_start) <= tolerance
                for _, target_start in strong_targets
            ):
                totals["suspected_wrong_item"] += 1
                per_provider[provider]["suspected_wrong_item"] += 1
        totals["correct_item_valid_boundary"] += correct
        per_provider[provider]["correct_item_valid_boundary"] += correct

        # This is deliberately named suspected: generated_items are a deterministic crosswalk,
        # not a human adjudication of whether a skipped agenda item was discussed.
        generated_items = crosswalk_row.get("generated_items", [])
        if isinstance(generated_items, list):
            undiscussed = {
                item.get("generated_item_index")
                for item in generated_items
                if isinstance(item, Mapping) and item.get("status") in {"unmapped", "conflicted"}
            }
            skipped = sum(anchor.get("agenda_item_index") in undiscussed for anchor in anchors)
            totals["suspected_skipped_item_anchors"] += skipped
            per_provider[provider]["suspected_skipped_item_anchors"] += skipped

        if len({anchor.get("unit_id") for anchor in anchors}) != len(anchors):
            totals["duplicate_unit_anchors"] += 1
        starts = [float(anchor["start"]) for anchor in anchors]
        if any(left >= right for left, right in zip(starts, starts[1:], strict=False)):
            totals["non_monotonic_anchors"] += 1
        agenda_count = row.get("agenda_item_count")
        # build_report injects agenda_item_count from the route-level record into each enriched
        # row, so a Mapping-route fallback here is never reached in practice.
        if isinstance(agenda_count, int) and agenda_count >= len(anchors):
            totals["abstained_items"] += agenda_count - len(anchors)
        for key, target in (
            ("input_tokens", "tokens"),
            ("cost_usd", "cost_usd"),
            ("latency_seconds", "latency_seconds"),
        ):
            value = _number(row.get(key))
            # build_report propagates input_tokens/cost_usd/latency_seconds into each enriched
            # row, so no Mapping-route fallback is needed here.
            if value is not None:
                if target == "tokens":
                    token_total += value
                elif target == "cost_usd":
                    cost_total += value
                else:
                    latency_total += value

    totals["malformed_or_failed_routes"] = totals["failed_episodes"]
    totals["episodes"] = completed_rows + totals["failed_episodes"]
    result: dict[str, Any] = {
        "route": route_name,
        "tolerance_seconds": tolerance,
        "counts": dict(totals),
        "metrics": {
            "provider_start_recall": _rate(
                totals["provider_start_hits"], totals["provider_chapters"]
            ),
            "provider_start_precision": _rate(totals["provider_start_hits"], totals["anchors"]),
            "correct_item_valid_boundary_precision": _rate(
                totals["correct_item_valid_boundary"], totals["anchors"]
            ),
            "suspected_wrong_item_rate": _rate(totals["suspected_wrong_item"], totals["anchors"]),
            "malformed_or_failed_rate": _rate(
                totals["malformed_or_failed_routes"], totals["episodes"]
            ),
            "abstention_rate": _rate(
                totals["abstained_items"], totals["abstained_items"] + totals["anchors"]
            ),
            "boundary_error_mean_seconds": round(sum(boundary_errors) / len(boundary_errors), 3)
            if boundary_errors
            else None,
            "boundary_error_p95_seconds": round(
                sorted(boundary_errors)[max(0, math.ceil(len(boundary_errors) * 0.95) - 1)], 3
            )
            if boundary_errors
            else None,
        },
        "operations": {
            "input_tokens": int(token_total) if token_total else None,
            "cost_usd": round(cost_total, 6) if cost_total else None,
            "latency_seconds": round(latency_total, 3) if latency_total else None,
        },
        "by_provider": {
            provider: {
                "counts": dict(counts),
                "provider_start_recall": _rate(
                    counts["provider_start_hits"], counts["provider_chapters"]
                ),
                "provider_start_precision": _rate(counts["provider_start_hits"], counts["anchors"]),
                "correct_item_valid_boundary_precision": _rate(
                    counts["correct_item_valid_boundary"], counts["anchors"]
                ),
            }
            for provider, counts in sorted(per_provider.items())
        },
    }
    return result


def evaluate_gate(
    report: Mapping[str, Any],
    *,
    min_completed_episodes: int | None = None,
    min_provider_start_recall: float | None = None,
    min_correct_item_precision: float | None = None,
    max_suspected_wrong_rate: float | None = None,
    max_failed_rate: float | None = None,
) -> dict[str, Any]:
    """Evaluate only explicitly supplied rollout gates; omitted gates remain undecided."""
    counts = report.get("counts", {})
    metrics = report.get("metrics", {})
    checks: dict[str, bool | None] = {}

    def check(name: str, value: object, threshold: float | int | None, *, minimum: bool) -> None:
        if threshold is None or not isinstance(value, int | float):
            checks[name] = None
        elif minimum:
            checks[name] = float(value) >= float(threshold)
        else:
            checks[name] = float(value) <= float(threshold)

    check(
        "completed_episodes", counts.get("completed_episodes"), min_completed_episodes, minimum=True
    )
    check(
        "provider_start_recall",
        metrics.get("provider_start_recall"),
        min_provider_start_recall,
        minimum=True,
    )
    check(
        "correct_item_valid_boundary_precision",
        metrics.get("correct_item_valid_boundary_precision"),
        min_correct_item_precision,
        minimum=True,
    )
    check(
        "suspected_wrong_item_rate",
        metrics.get("suspected_wrong_item_rate"),
        max_suspected_wrong_rate,
        minimum=False,
    )
    check(
        "malformed_or_failed_rate",
        metrics.get("malformed_or_failed_rate"),
        max_failed_rate,
        minimum=False,
    )
    decided = [value for value in checks.values() if value is not None]
    return {
        "status": "pass"
        if decided and all(decided)
        else "fail"
        if decided and not all(decided)
        else "not_configured",
        "checks": checks,
    }


def build_report(
    results: Mapping[str, Any],
    gold: Mapping[str, Any],
    *,
    crosswalk: Mapping[str, Any] | None = None,
    tolerance: float = 60.0,
    gate_kwargs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a versioned route report from a runner result and hidden scoring artifacts."""
    gold_by_uid = {
        str(row.get("uid")): row for row in gold.get("episodes", []) if isinstance(row, Mapping)
    }
    crosswalk_by_uid = {
        str(row.get("uid")): row
        for row in (crosswalk or {}).get("episodes", [])
        if isinstance(row, Mapping)
    }
    by_route: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in results.get("results", []):
        if not isinstance(row, Mapping):
            continue
        routes = row.get("routes")
        if isinstance(routes, Mapping):
            for route, route_result in routes.items():
                if isinstance(route_result, Mapping):
                    enriched = dict(route_result)
                    enriched.setdefault("uid", row.get("uid"))
                    enriched.setdefault("provider", row.get("provider"))
                    enriched.setdefault("agenda_item_count", row.get("agenda_item_count"))
                    by_route[str(route)].append(enriched)
    reports = {
        route: score_route(
            rows, gold_by_uid, crosswalk_by_uid=crosswalk_by_uid, tolerance=tolerance
        )
        for route, rows in sorted(by_route.items())
    }
    return {
        "version": REPORT_VERSION,
        "purpose": "read-only locator shadow quality and rollout report",
        "provider_labels_in_requests": results.get("provider_labels_in_requests") is True,
        "routes": reports,
        "gates": {
            route: evaluate_gate(report, **dict(gate_kwargs or {}))
            for route, report in reports.items()
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--crosswalk", type=Path)
    parser.add_argument("--write", type=Path, required=True)
    parser.add_argument("--tolerance", type=float, default=60.0)
    parser.add_argument("--min-completed-episodes", type=int)
    parser.add_argument("--min-provider-start-recall", type=float)
    parser.add_argument("--min-correct-item-precision", type=float)
    parser.add_argument("--max-suspected-wrong-rate", type=float)
    parser.add_argument("--max-failed-rate", type=float)
    args = parser.parse_args(argv)
    results = json.loads(args.results.read_text(encoding="utf-8"))
    gold = json.loads(args.gold.read_text(encoding="utf-8"))
    crosswalk = json.loads(args.crosswalk.read_text(encoding="utf-8")) if args.crosswalk else None
    report = build_report(
        results,
        gold,
        crosswalk=crosswalk,
        tolerance=args.tolerance,
        gate_kwargs={
            "min_completed_episodes": args.min_completed_episodes,
            "min_provider_start_recall": args.min_provider_start_recall,
            "min_correct_item_precision": args.min_correct_item_precision,
            "max_suspected_wrong_rate": args.max_suspected_wrong_rate,
            "max_failed_rate": args.max_failed_rate,
        },
    )
    args.write.parent.mkdir(parents=True, exist_ok=True)
    args.write.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
