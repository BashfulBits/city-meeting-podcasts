#!/usr/bin/env python
"""Train a local, weakly supervised agenda-title ranker for GH#1078 research.

This script is deliberately outside the pipeline.  Its positive labels are high-confidence
deterministic title alignments, so its held-out metrics measure agreement with that heuristic,
not independently verified provider truth.  Use its output to select an adjudication sample,
never to admit generated chapters to production.
"""

from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from pathlib import Path

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import SGDClassifier
from sklearn.metrics import average_precision_score, roc_auc_score

RANDOM_SEED = 1078
EpisodeKey = tuple[str, str, str]


def episode_key(row: dict) -> EpisodeKey:
    """Return the stable dataset identity shared by alignment and candidate rows."""
    return row["provider"], row["slug"], row["uid"]


def read_jsonl(path: Path):
    """Yield JSON objects from a local dataset file."""
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            yield json.loads(line)


def select_positives(path: Path, minimum_similarity: float) -> list[dict]:
    """Select deterministic, one-to-one matches as intentionally weak positive labels."""
    return [
        row
        for row in read_jsonl(path)
        if row["similarity"] is not None and row["similarity"] >= minimum_similarity
    ]


def load_candidates(path: Path) -> dict[EpisodeKey, list[dict]]:
    """Group structural agenda candidates by their meeting identity."""
    candidates: dict[EpisodeKey, list[dict]] = defaultdict(list)
    for row in read_jsonl(path):
        candidates[episode_key(row)].append(row)
    for episode_candidates in candidates.values():
        episode_candidates.sort(key=lambda row: row["agenda_candidate_index"])
    return candidates


def balanced_pairs(
    positives: list[dict],
    candidates: dict[EpisodeKey, list[dict]],
    *,
    negative_ratio: int,
    randomizer: random.Random,
) -> tuple[list[str], list[str], np.ndarray]:
    """Make positive pairs and deterministic, within-agenda negative pairs."""
    canonical_titles: list[str] = []
    candidate_titles: list[str] = []
    labels: list[int] = []
    for positive in positives:
        agenda_candidates = candidates[episode_key(positive)]
        positive_index = positive["agenda_candidate_index"]
        positive_candidate = next(
            candidate
            for candidate in agenda_candidates
            if candidate["agenda_candidate_index"] == positive_index
        )
        canonical_titles.append(positive["canonical_title"])
        candidate_titles.append(positive_candidate["agenda_candidate_title"])
        labels.append(1)
        negatives = [
            candidate
            for candidate in agenda_candidates
            if candidate["agenda_candidate_index"] != positive_index
        ]
        for candidate in randomizer.sample(negatives, k=min(negative_ratio, len(negatives))):
            canonical_titles.append(positive["canonical_title"])
            candidate_titles.append(candidate["agenda_candidate_title"])
            labels.append(0)
    return canonical_titles, candidate_titles, np.asarray(labels, dtype=np.int8)


def pair_features(vectorizer: TfidfVectorizer, left: list[str], right: list[str]):
    """Return sparse overlap features for title pairs in one shared n-gram vocabulary."""
    return vectorizer.transform(left).multiply(vectorizer.transform(right))


def rank_metrics(
    classifier: SGDClassifier,
    vectorizer: TfidfVectorizer,
    positives: list[dict],
    candidates: dict[EpisodeKey, list[dict]],
) -> dict[str, float | int]:
    """Measure whether the weak positive ranks within its own meeting agenda."""
    ranks: list[int] = []
    for positive in positives:
        agenda_candidates = candidates[episode_key(positive)]
        candidate_titles = [candidate["agenda_candidate_title"] for candidate in agenda_candidates]
        left = [positive["canonical_title"]] * len(candidate_titles)
        scores = classifier.predict_proba(pair_features(vectorizer, left, candidate_titles))[:, 1]
        positive_position = next(
            index
            for index, candidate in enumerate(agenda_candidates)
            if candidate["agenda_candidate_index"] == positive["agenda_candidate_index"]
        )
        rank = 1 + int(np.count_nonzero(scores > scores[positive_position]))
        ranks.append(rank)
    return {
        "queries": len(ranks),
        "recall_at_1": round(sum(rank == 1 for rank in ranks) / len(ranks), 6),
        "recall_at_3": round(sum(rank <= 3 for rank in ranks) / len(ranks), 6),
        "mean_reciprocal_rank": round(float(np.mean([1 / rank for rank in ranks])), 6),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--minimum-similarity", type=float, default=0.9)
    parser.add_argument("--negative-ratio", type=int, default=4)
    parser.add_argument("--max-train-positives", type=int, default=50_000)
    parser.add_argument("--max-test-positives", type=int, default=10_000)
    args = parser.parse_args(argv)
    if not 0 < args.minimum_similarity <= 1:
        parser.error("--minimum-similarity must be between zero and one")
    if args.negative_ratio < 1:
        parser.error("--negative-ratio must be positive")

    positives = select_positives(args.dataset_dir / "alignments.jsonl", args.minimum_similarity)
    candidates = load_candidates(args.dataset_dir / "agenda_candidates.jsonl")
    train = [row for row in positives if row["split"] == "train"][: args.max_train_positives]
    test = [row for row in positives if row["split"] == "test"][: args.max_test_positives]
    randomizer = random.Random(RANDOM_SEED)
    train_left, train_right, train_labels = balanced_pairs(
        train, candidates, negative_ratio=args.negative_ratio, randomizer=randomizer
    )
    test_left, test_right, test_labels = balanced_pairs(
        test, candidates, negative_ratio=args.negative_ratio, randomizer=randomizer
    )
    vectorizer = TfidfVectorizer(
        analyzer="char_wb", ngram_range=(3, 5), min_df=2, max_features=100_000, sublinear_tf=True
    )
    vectorizer.fit(train_left + train_right)
    classifier = SGDClassifier(
        loss="log_loss", alpha=1e-5, class_weight="balanced", max_iter=30, random_state=RANDOM_SEED
    )
    classifier.fit(pair_features(vectorizer, train_left, train_right), train_labels)
    probabilities = classifier.predict_proba(pair_features(vectorizer, test_left, test_right))[:, 1]
    result = {
        "label_provenance": "weak_deterministic_agenda_title_match",
        "minimum_similarity": args.minimum_similarity,
        "train_positive_pairs": len(train),
        "test_positive_pairs": len(test),
        "pair_roc_auc": round(float(roc_auc_score(test_labels, probabilities)), 6),
        "pair_average_precision": round(
            float(average_precision_score(test_labels, probabilities)), 6
        ),
        "ranking": rank_metrics(classifier, vectorizer, test, candidates),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
