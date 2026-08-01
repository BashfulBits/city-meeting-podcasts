#!/usr/bin/env python
"""Score GH#1078 candidate methods against frozen, source-backed human gold labels."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

EpisodeKey = tuple[str, str, str]


def _read_jsonl(path: Path):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            yield json.loads(line)


def _overlap(left: dict, right: dict) -> float:
    if left["meeting_id"] != right["meeting_id"]:
        return 0.0
    start = max(left["line_start"], right["line_start"])
    end = min(left["line_end"], right["line_end"])
    if end < start:
        return 0.0
    union_start = min(left["line_start"], right["line_start"])
    union_end = max(left["line_end"], right["line_end"])
    return (end - start + 1) / (union_end - union_start + 1)


def _one_to_one(predictions: list[dict], actual: list[dict]) -> tuple[set[int], set[int]]:
    pairs = sorted(
        (
            (_overlap(prediction, gold), prediction_index, gold_index)
            for prediction_index, prediction in enumerate(predictions)
            for gold_index, gold in enumerate(actual)
        ),
        reverse=True,
    )
    used_predictions: set[int] = set()
    used_gold: set[int] = set()
    for score, prediction_index, gold_index in pairs:
        if score <= 0:
            break
        if prediction_index not in used_predictions and gold_index not in used_gold:
            used_predictions.add(prediction_index)
            used_gold.add(gold_index)
    return used_predictions, used_gold


def _metrics(predictions: list[dict], gold: list[dict]) -> dict[str, object]:
    required = [row for row in gold if row["label"] == "positive_required"]
    optional = [row for row in gold if row["label"] == "positive_optional"]
    neutral = [row for row in gold if row["label"] in {"neutral_optional", "uncertain"}]
    primary_predicted, primary_matched = _one_to_one(predictions, required)
    remaining_predictions = [
        prediction for index, prediction in enumerate(predictions) if index not in primary_predicted
    ]
    optional_predicted, optional_matched = _one_to_one(remaining_predictions, optional)
    neutral_predicted, _ = _one_to_one(remaining_predictions, neutral)
    # Translate the local remaining indices to original prediction indices.
    remaining_indexes = [
        index for index in range(len(predictions)) if index not in primary_predicted
    ]
    optional_original = {remaining_indexes[index] for index in optional_predicted}
    neutral_original = {remaining_indexes[index] for index in neutral_predicted}
    counted_predictions = set(range(len(predictions))) - optional_original - neutral_original
    true_positive = len(primary_predicted)
    false_positive = len(counted_predictions - primary_predicted)
    false_negative = len(required) - len(primary_matched)
    precision = (
        true_positive / (true_positive + false_positive)
        if true_positive + false_positive
        else 1.0
    )
    recall = true_positive / len(required) if required else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "gold_required": len(required),
        "gold_optional": len(optional),
        "predictions": len(predictions),
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "neutral_predictions": len(optional_original | neutral_original),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "optional_recall": round(len(optional_matched) / len(optional), 4) if optional else None,
    }


def _by_meeting(predictions: list[dict], gold: list[dict]) -> dict[str, float]:
    meetings = sorted({row["meeting_id"] for row in gold})
    scores = []
    count_errors = []
    for meeting_id in meetings:
        result = _metrics(
            [row for row in predictions if row["meeting_id"] == meeting_id],
            [row for row in gold if row["meeting_id"] == meeting_id],
        )
        scores.append(result["f1"])
        count_errors.append(abs(result["predictions"] - result["gold_required"]))
    return {
        "meetings": len(meetings),
        "macro_f1": round(sum(scores) / len(scores), 4) if scores else 0.0,
        "mean_absolute_count_error": round(sum(count_errors) / len(count_errors), 4)
        if count_errors
        else 0.0,
    }


def _method_rows_from_model(
    *, model_directory: str, keys: dict[str, EpisodeKey], outputs_dir: Path
) -> list[dict]:
    by_episode = {episode: meeting_id for meeting_id, episode in keys.items()}
    rows = []
    for output_path in (outputs_dir / model_directory).glob("*.json"):
        output = json.loads(output_path.read_text())
        if output.get("status") != "completed":
            raise RuntimeError(f"incomplete output: {output_path}")
        episode = (
            output["episode"]["provider"],
            output["episode"]["slug"],
            output["episode"]["uid"],
        )
        meeting_id = by_episode.get(episode)
        if meeting_id is None:
            continue
        rows.extend(
            {
                "meeting_id": meeting_id,
                "line_start": item["line_start"],
                "line_end": item["line_end"],
                "title": item["title"],
            }
            for item in output["items"]
        )
    return rows


def _method_rows_from_candidates(
    *, candidate_path: Path, keys: dict[str, EpisodeKey]
) -> list[dict]:
    by_episode = {episode: meeting_id for meeting_id, episode in keys.items()}
    rows = []
    for candidate in _read_jsonl(candidate_path):
        episode = (candidate["provider"], candidate["slug"], candidate["uid"])
        meeting_id = by_episode.get(episode)
        if meeting_id is not None:
            rows.append(
                {
                    "meeting_id": meeting_id,
                    "line_start": candidate["agenda_line_number"],
                    "line_end": candidate["agenda_line_number"],
                    "title": candidate["agenda_candidate_title"],
                }
            )
    return rows


def _method_rows_from_classifier(path: Path) -> list[dict]:
    payload = json.loads(path.read_text())
    return [
        {"meeting_id": meeting_id, **row}
        for meeting_id, suggestions in payload["suggestions"].items()
        for row in suggestions
    ]


def _strata(predictions: list[dict], gold: list[dict]) -> dict[str, dict[str, object]]:
    result = {}
    for provider in sorted({row["provider"] for row in gold}):
        for bucket in sorted({row["coverage_bucket"] for row in gold}):
            selected = [
                row
                for row in gold
                if row["provider"] == provider and row["coverage_bucket"] == bucket
            ]
            if selected:
                meeting_ids = {row["meeting_id"] for row in selected}
                result[f"{provider}/{bucket}"] = _metrics(
                    [row for row in predictions if row["meeting_id"] in meeting_ids], selected
                )
    return result


def _report(name: str, predictions: list[dict], gold: list[dict]) -> dict[str, object]:
    return {
        "method": name,
        "overall": _metrics(predictions, gold),
        "per_meeting": _by_meeting(predictions, gold),
        "by_provider_coverage": _strata(predictions, gold),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument(
        "--outputs-dir",
        type=Path,
        help="directory containing the three historical baseline model folders",
    )
    parser.add_argument(
        "--include-default-models",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="include the historical baseline folders under --outputs-dir (default: true)",
    )
    parser.add_argument(
        "--model-output",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help=(
            "additional completed model-output directories, for example "
            "hybrid-medium=/tmp/results/model"
        ),
    )
    parser.add_argument("--agenda-candidates", type=Path)
    parser.add_argument(
        "--classifier-suggestions",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="one or more frozen classifier suggestion files",
    )
    parser.add_argument("--write", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.include_default_models and args.outputs_dir is None:
        parser.error("--outputs-dir is required unless --no-include-default-models is used")
    if args.agenda_candidates is None:
        parser.error("--agenda-candidates is required to score the deterministic method")

    gold = [row for row in _read_jsonl(args.gold) if row["source_backed"]]
    keys = {
        entry["adjudication_id"]: tuple(entry["episode"])
        for entry in json.loads(args.key.read_text())
    }
    methods = {
        "deterministic": _method_rows_from_candidates(
            candidate_path=args.agenda_candidates, keys=keys
        )
    }
    if args.include_default_models:
        methods = {
            "mistral-large": _method_rows_from_model(
                model_directory="mistral--mistral-large-latest",
                keys=keys,
                outputs_dir=args.outputs_dir,
            ),
            "mistral-medium": _method_rows_from_model(
                model_directory="mistral--mistral-medium-2508",
                keys=keys,
                outputs_dir=args.outputs_dir,
            ),
            "deepseek": _method_rows_from_model(
                model_directory="deepseek--deepseek-v4-flash",
                keys=keys,
                outputs_dir=args.outputs_dir,
            ),
            **methods,
        }
    for raw in args.classifier_suggestions:
        name, separator, path = raw.partition("=")
        if not separator or not name or not path:
            parser.error("--classifier-suggestions must be NAME=PATH")
        methods[f"classifier-{name}"] = _method_rows_from_classifier(Path(path))
    for raw in args.model_output:
        name, separator, path = raw.partition("=")
        if not separator or not name or not path:
            parser.error("--model-output must be NAME=PATH")
        methods[name] = [
            row
            for output_path in path.split(",")
            for row in _method_rows_from_model(
                model_directory=".", keys=keys, outputs_dir=Path(output_path)
            )
        ]
    report = {
        "gold_rows": len(gold),
        "methods": [_report(name, rows, gold) for name, rows in methods.items()],
    }
    args.write.parent.mkdir(parents=True, exist_ok=True)
    args.write.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    for method in report["methods"]:
        overall = method["overall"]
        print(
            f"{method['method']}: P={overall['precision']:.4f} R={overall['recall']:.4f} "
            f"F1={overall['f1']:.4f} predictions={overall['predictions']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
