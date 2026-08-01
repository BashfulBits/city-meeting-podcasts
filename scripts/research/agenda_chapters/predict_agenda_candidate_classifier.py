#!/usr/bin/env python
"""Apply a frozen weak-label agenda-candidate classifier to a review subset.

Its predictions are *suggestions only*.  The model was trained on deterministic alignments to
provider chapter titles, so this script must not be used to score or label the agenda-derived gold
set it helps construct.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from evaluate_agenda_candidate_classifier import (
    RANDOM_SEED,
    assign_validation,
    best_f1_threshold,
    build_examples,
    episode_key,
    load_episode_metadata,
    read_jsonl,
)
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import SGDClassifier
from sklearn.svm import LinearSVC


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--keys", type=Path, required=True, help="adjudication unblinding-key JSON")
    parser.add_argument("--write", type=Path, required=True)
    parser.add_argument("--model", choices=("linear-svc", "sgd-logistic"), required=True)
    parser.add_argument(
        "--include-context",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="include neighboring titles and position features (default: false)",
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
    vectorizer = TfidfVectorizer(
        analyzer="char_wb", ngram_range=(3, 5), min_df=2, max_features=100_000, sublinear_tf=True
    )
    classifier = (
        LinearSVC(class_weight="balanced", random_state=RANDOM_SEED)
        if args.model == "linear-svc"
        else SGDClassifier(
            loss="log_loss",
            alpha=1e-5,
            class_weight="balanced",
            max_iter=30,
            random_state=RANDOM_SEED,
        )
    )
    classifier.fit(vectorizer.fit_transform(train_features), train_labels)
    threshold = best_f1_threshold(
        validation_labels, classifier.decision_function(vectorizer.transform(validation_features))
    )

    adjudication_by_key = {
        tuple(entry["episode"]): entry["adjudication_id"]
        for entry in json.loads(args.keys.read_text())
    }
    candidates: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for candidate in read_jsonl(args.dataset_dir / "agenda_candidates.jsonl"):
        key = episode_key(candidate)
        if key in adjudication_by_key:
            candidates[key].append(candidate)

    suggestions: dict[str, list[dict]] = {}
    for key, rows in candidates.items():
        ordered = sorted(rows, key=lambda row: row["agenda_candidate_index"])
        scores = classifier.decision_function(
            vectorizer.transform([row["agenda_candidate_title"] for row in ordered])
        )
        suggestions[adjudication_by_key[key]] = [
            {
                "title": row["agenda_candidate_title"],
                "line_start": row["agenda_line_number"],
                "line_end": row["agenda_line_number"],
                "evidence_quote": row["agenda_candidate_title"],
                "score": round(float(score), 6),
            }
            for row, score in zip(ordered, scores, strict=True)
            if score >= threshold
        ]
    args.write.parent.mkdir(parents=True, exist_ok=True)
    feature_name = "context" if args.include_context else "title-only"
    args.write.write_text(
        json.dumps(
            {
                "model": f"{args.model}-{feature_name}-weak-alignment",
                "threshold": round(threshold, 6),
                "suggestions": suggestions,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    print(
        json.dumps(
            {
                "meetings": len(suggestions),
                "selected_candidates": sum(len(rows) for rows in suggestions.values()),
                "model": args.model,
                "include_context": args.include_context,
                "threshold": round(threshold, 6),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
