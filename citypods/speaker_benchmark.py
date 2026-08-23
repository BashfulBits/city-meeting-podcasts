"""Offline evaluator for a curated R7 diarization/identity gold set.

The runner deliberately consumes normalized JSON rather than importing either engine.  A private
gold bundle can therefore compare pyannote and WeSpeaker on identical meetings without placing
reference audio, embeddings, or model credentials in the repository or a scheduled workflow.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path


def _cases(value: object) -> dict[str, list[dict]]:
    if isinstance(value, Mapping) and isinstance(value.get("cases"), list):
        rows = value["cases"]
    elif isinstance(value, Mapping):
        rows = [{"id": key, "turns": turns} for key, turns in value.items()]
    else:
        raise ValueError("benchmark JSON must be an object or contain a cases list")
    result: dict[str, list[dict]] = {}
    for row in rows:
        if (
            not isinstance(row, Mapping)
            or not row.get("id")
            or not isinstance(row.get("turns"), list)
        ):
            raise ValueError("each gold/prediction case needs id and turns")
        turns: list[dict] = []
        for index, turn in enumerate(row["turns"]):
            if not isinstance(turn, Mapping):
                raise ValueError(f"case {row['id']!r} turn {index} must be an object")
            start, end = turn.get("start"), turn.get("end")
            if not isinstance(start, int | float) or not isinstance(end, int | float):
                raise ValueError(f"case {row['id']!r} turn {index} needs numeric start and end")
            if float(end) < float(start):
                raise ValueError(f"case {row['id']!r} turn {index} ends before it starts")
            turns.append(dict(turn))
        result[str(row["id"])] = turns
    return result


def _runtime_seconds(value: object) -> float | None:
    value = value.get("runtime_seconds") if isinstance(value, Mapping) else None
    return float(value) if isinstance(value, int | float) and value >= 0 else None


def _overlap(left: Mapping, right: Mapping) -> float:
    end = min(float(left["end"]), float(right["end"]))
    start = max(float(left["start"]), float(right["start"]))
    return max(0.0, end - start)


def _best_cluster_map(gold: list[dict], predicted: list[dict]) -> dict[str, str]:
    weights: list[tuple[float, str, str]] = []
    for pred in predicted:
        for truth in gold:
            span = _overlap(pred, truth)
            if span:
                weights.append((span, str(pred.get("cluster", "")), str(truth.get("speaker", ""))))
    result: dict[str, str] = {}
    used: set[str] = set()
    for _, cluster, speaker in sorted(weights, reverse=True):
        if cluster and speaker and cluster not in result and speaker not in used:
            result[cluster] = speaker
            used.add(speaker)
    return result


def compare(gold: list[dict], predicted: list[dict], *, boundary_tolerance: float = 1.0) -> dict:
    """Return time-weighted cluster, overlap, boundary, and optional identity metrics."""
    mapping = _best_cluster_map(gold, predicted)
    total = correct = 0.0
    for pred in predicted:
        for truth in gold:
            span = _overlap(pred, truth)
            total += span
            if mapping.get(str(pred.get("cluster", ""))) == str(truth.get("speaker", "")):
                correct += span
    gold_overlap = [row for row in gold if row.get("overlap")]
    pred_overlap = [row for row in predicted if row.get("overlap")]
    overlap_hits = sum(any(_overlap(row, truth) for truth in gold_overlap) for row in pred_overlap)
    gold_overlap_hits = sum(
        any(_overlap(row, prediction) for prediction in pred_overlap) for row in gold_overlap
    )
    starts = [float(row["start"]) for row in gold]
    ends = [float(row["end"]) for row in gold]
    predicted_edges = [float(row[edge]) for row in predicted for edge in ("start", "end")]
    boundary_hits = sum(
        any(abs(edge - actual) <= boundary_tolerance for edge in predicted_edges)
        for actual in starts + ends
    )
    named = [row for row in predicted if isinstance(row.get("identity"), Mapping)]
    identity_correct = sum(
        any(
            _overlap(row, truth) and row["identity"].get("speaker_id") == truth.get("speaker_id")
            for truth in gold
        )
        for row in named
    )
    return {
        "turn_cluster_accuracy": correct / total if total else None,
        "overlap_precision": overlap_hits / len(pred_overlap) if pred_overlap else None,
        "overlap_recall": gold_overlap_hits / len(gold_overlap) if gold_overlap else None,
        "boundary_recall": boundary_hits / (len(starts) + len(ends)) if starts else None,
        "identity_precision": identity_correct / len(named) if named else None,
        "cluster_map": mapping,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="citypods speaker-benchmark")
    parser.add_argument("--gold", required=True, type=Path)
    parser.add_argument("--pyannote", required=True, type=Path)
    parser.add_argument("--wespeaker", required=True, type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    gold = _cases(json.loads(args.gold.read_text(encoding="utf-8")))
    report = {"version": 1, "engines": {}}
    for engine, path in (("pyannote", args.pyannote), ("wespeaker", args.wespeaker)):
        raw_predictions = json.loads(path.read_text(encoding="utf-8"))
        predictions = _cases(raw_predictions)
        report["engines"][engine] = {
            "runtime_seconds": _runtime_seconds(raw_predictions),
            "cases": {
                case: compare(gold_turns, predictions.get(case, []))
                for case, gold_turns in gold.items()
            },
        }
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


__all__ = ["compare", "main"]
