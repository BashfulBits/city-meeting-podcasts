#!/usr/bin/env python
"""Evaluate agenda-only selection of known provider-derived chapter titles (GH#1078).

Labels are weak: a candidate is positive when the deterministic matcher aligned it to a supplied
provider chapter title, and every other extracted candidate in that agenda is negative.  The model
never receives provider chapter titles; it receives only a candidate, adjacent agenda candidates,
and coarse position features.  Metrics therefore measure replication of that derived set, not
independent chapter accuracy.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import SGDClassifier
from sklearn.metrics import (
    average_precision_score,
    precision_recall_curve,
    precision_recall_fscore_support,
)
from sklearn.svm import LinearSVC

MATCH_THRESHOLD = 0.8
RANDOM_SEED = 1078
EpisodeKey = tuple[str, str, str]


@dataclass(frozen=True)
class EpisodeMetadata:
    provider: str
    body: str
    published: str
    split: str
    positive_indices: frozenset[int]


def read_jsonl(path: Path):
    """Yield JSON objects from a local research dataset file."""
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            yield json.loads(line)


def episode_key(row: dict) -> EpisodeKey:
    """Return the stable identity shared by candidate and alignment records."""
    return row["provider"], row["slug"], row["uid"]


def load_episode_metadata(path: Path) -> dict[EpisodeKey, EpisodeMetadata]:
    """Load source-derived positives plus chronological provider/body split metadata."""
    rows: dict[EpisodeKey, dict] = {}
    for row in read_jsonl(path):
        key = episode_key(row)
        episode = rows.setdefault(
            key,
            {
                "provider": row["provider"],
                "body": row["body"],
                "published": row["published"],
                "split": row["split"],
                "positive_indices": set(),
            },
        )
        if row["similarity"] is not None and row["similarity"] >= MATCH_THRESHOLD:
            episode["positive_indices"].add(row["agenda_candidate_index"])
    return {
        key: EpisodeMetadata(
            provider=value["provider"],
            body=value["body"],
            published=value["published"],
            split=value["split"],
            positive_indices=frozenset(value["positive_indices"]),
        )
        for key, value in rows.items()
    }


def assign_validation(episodes: dict[EpisodeKey, EpisodeMetadata]) -> set[EpisodeKey]:
    """Reserve the newest fifth of the pre-existing train families for threshold selection."""
    families: dict[tuple[str, str], list[tuple[EpisodeKey, EpisodeMetadata]]] = defaultdict(list)
    for key, metadata in episodes.items():
        if metadata.split == "train":
            families[(metadata.provider, metadata.body)].append((key, metadata))
    validation: set[EpisodeKey] = set()
    for family in families.values():
        ordered = sorted(family, key=lambda item: (item[1].published, item[0]))
        validation.update(key for key, _ in ordered[-max(1, round(len(ordered) * 0.2)) :])
    return validation


def candidate_feature(
    candidate: dict, candidates: list[dict], index: int, *, include_context: bool
) -> str:
    """Represent one agenda candidate without access to any provider chapter title."""
    if not include_context:
        return candidate["agenda_candidate_title"]
    previous = candidates[index - 1]["agenda_candidate_title"] if index else "(start)"
    following = (
        candidates[index + 1]["agenda_candidate_title"] if index + 1 < len(candidates) else "(end)"
    )
    position = min(9, 10 * index // max(1, len(candidates)))
    length = min(9, len(candidates) // 5)
    return (
        f"candidate {candidate['agenda_candidate_title']}\n"
        f"previous {previous}\nfollowing {following}\n"
        f"position_bucket_{position} agenda_length_bucket_{length}"
    )


def build_examples(
    candidate_path: Path,
    episodes: dict[EpisodeKey, EpisodeMetadata],
    validation: set[EpisodeKey],
    *,
    include_context: bool,
) -> dict[str, tuple[list[str], np.ndarray, list[EpisodeKey], list[int]]]:
    """Build strict-binary candidate examples and preserve identities for set metrics."""
    grouped: dict[EpisodeKey, list[dict]] = defaultdict(list)
    for candidate in read_jsonl(candidate_path):
        key = episode_key(candidate)
        if key in episodes:
            grouped[key].append(candidate)
    examples: dict[str, tuple[list[str], list[int], list[EpisodeKey], list[int]]] = {
        split: ([], [], [], []) for split in ("train", "validation", "test")
    }
    for key, candidates in grouped.items():
        candidates.sort(key=lambda candidate: candidate["agenda_candidate_index"])
        metadata = episodes[key]
        split = (
            "test" if metadata.split == "test" else "validation" if key in validation else "train"
        )
        features, labels, keys, candidate_indices = examples[split]
        for index, candidate in enumerate(candidates):
            features.append(
                candidate_feature(candidate, candidates, index, include_context=include_context)
            )
            labels.append(int(candidate["agenda_candidate_index"] in metadata.positive_indices))
            keys.append(key)
            candidate_indices.append(candidate["agenda_candidate_index"])
    return {
        split: (features, np.asarray(labels, dtype=np.int8), keys, candidate_indices)
        for split, (features, labels, keys, candidate_indices) in examples.items()
    }


def best_f1_threshold(labels: np.ndarray, probabilities: np.ndarray) -> float:
    """Choose an operating threshold solely from the chronological validation split."""
    precision, recall, thresholds = precision_recall_curve(labels, probabilities)
    f1 = 2 * precision[:-1] * recall[:-1] / np.maximum(precision[:-1] + recall[:-1], 1e-12)
    return float(thresholds[int(np.argmax(f1))])


def candidate_metrics(
    labels: np.ndarray, probabilities: np.ndarray, threshold: float
) -> dict[str, float]:
    """Return threshold-free and thresholded candidate classification metrics."""
    predictions = probabilities >= threshold
    precision, recall, f1, _ = precision_recall_fscore_support(
        labels, predictions, average="binary", zero_division=0
    )
    return {
        "average_precision": round(float(average_precision_score(labels, probabilities)), 6),
        "precision": round(float(precision), 6),
        "recall": round(float(recall), 6),
        "f1": round(float(f1), 6),
    }


def meeting_set_metrics(
    labels: np.ndarray, probabilities: np.ndarray, keys: list[EpisodeKey], threshold: float
) -> dict[str, float | int]:
    """Measure agreement between predicted and derived title sets per held-out meeting."""
    actual: dict[EpisodeKey, set[int]] = defaultdict(set)
    predicted: dict[EpisodeKey, set[int]] = defaultdict(set)
    meeting_ids = set(keys)
    for index, key in enumerate(keys):
        if labels[index]:
            actual[key].add(index)
        if probabilities[index] >= threshold:
            predicted[key].add(index)
    f1_scores: list[float] = []
    count_errors: list[int] = []
    for key in meeting_ids:
        intersection = len(actual[key] & predicted[key])
        denominator = len(actual[key]) + len(predicted[key])
        f1_scores.append(2 * intersection / denominator if denominator else 1.0)
        count_errors.append(abs(len(actual[key]) - len(predicted[key])))
    return {
        "meetings": len(f1_scores),
        "macro_f1": round(float(np.mean(f1_scores)), 6),
        "mean_absolute_count_error": round(float(np.mean(count_errors)), 6),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument(
        "--include-context",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="include adjacent candidate and coarse agenda-position features (default: true)",
    )
    parser.add_argument(
        "--model",
        choices=("sgd-logistic", "linear-svc"),
        default="sgd-logistic",
        help="sparse linear classifier to evaluate (default: sgd-logistic)",
    )
    args = parser.parse_args(argv)
    episodes = load_episode_metadata(args.dataset_dir / "alignments.jsonl")
    validation = assign_validation(episodes)
    examples = build_examples(
        args.dataset_dir / "agenda_candidates.jsonl",
        episodes,
        validation,
        include_context=args.include_context,
    )
    train_features, train_labels, _, _ = examples["train"]
    validation_features, validation_labels, _, _ = examples["validation"]
    test_features, test_labels, test_keys, _ = examples["test"]
    vectorizer = TfidfVectorizer(
        analyzer="char_wb", ngram_range=(3, 5), min_df=2, max_features=100_000, sublinear_tf=True
    )
    train_matrix = vectorizer.fit_transform(train_features)
    classifier = (
        SGDClassifier(
            loss="log_loss",
            alpha=1e-5,
            class_weight="balanced",
            max_iter=30,
            random_state=RANDOM_SEED,
        )
        if args.model == "sgd-logistic"
        else LinearSVC(class_weight="balanced", random_state=RANDOM_SEED)
    )
    classifier.fit(train_matrix, train_labels)
    validation_scores = classifier.decision_function(vectorizer.transform(validation_features))
    threshold = best_f1_threshold(validation_labels, validation_scores)
    test_scores = classifier.decision_function(vectorizer.transform(test_features))
    result = {
        "label_provenance": "strict_binary_deterministic_agenda_to_provider_chapter_match",
        "positive_match_threshold": MATCH_THRESHOLD,
        "feature_inputs": (
            "candidate_text_adjacent_candidates_coarse_position"
            if args.include_context
            else "candidate_text"
        ),
        "model": args.model,
        "operating_threshold_from_validation": round(threshold, 6),
        "examples": {split: int(len(examples[split][1])) for split in examples},
        "test_candidate_metrics": candidate_metrics(test_labels, test_scores, threshold),
        "test_meeting_set_metrics": meeting_set_metrics(
            test_labels, test_scores, test_keys, threshold
        ),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
