"""Offline tests for the supervised transition-scoring research tool."""

from __future__ import annotations

import random

import numpy as np

from citypods.chapter_locator import LocatorUnit
from scripts.research.agenda_chapters.analyze_transition_risk import analyze
from scripts.research.agenda_chapters.train_transition_scorer import (
    FEATURE_NAMES,
    SPEECH_RATE_FEATURE_NAMES,
    TRANSITION_PHRASE_FEATURE_NAMES,
    _greedy_distinct_assignment,
    _pairwise_examples,
    _strong_targets,
    build_episode_features,
    build_speech_rate_reference,
    feature_names_for_mode,
    fit_transition_phrase_model,
    speech_rate_vector,
    transition_phrase_features,
)


def _units() -> list[LocatorUnit]:
    return [
        LocatorUnit("u00001", 0.0, 5.0, "Call to order."),
        LocatorUnit("u00002", 10.0, 15.0, "Next item DCA26-0002B proposed revision."),
        LocatorUnit("u00003", 20.0, 25.0, "Motion to approve the proposed revision."),
    ]


def test_strong_targets_exclude_nonstrong_relationships():
    assert _strong_targets(
        {
            "provider_chapters": [
                {"status": "strong", "best_generated_item_index": 0, "start": 10.0},
                {"status": "possible", "best_generated_item_index": 1, "start": 20.0},
            ]
        }
    ) == {0: [10.0]}


def test_build_episode_features_labels_timed_positive_and_hard_negative_rows():
    features, labels, identities, targets = build_episode_features(
        {
            "generated_agenda": {
                "mistral/mistral-medium-2508": {
                    "items": [
                        {
                            "title": "Proposed revision",
                            "evidence_text": "DCA26-0002B proposed revision",
                            "display_ref": "DCA26-0002B",
                        }
                    ]
                }
            }
        },
        {
            "provider_chapters": [
                {"status": "strong", "best_generated_item_index": 0, "start": 10.0}
            ]
        },
        _units(),
        model="mistral/mistral-medium-2508",
        label_tolerance=1.0,
        hard_top_k=1,
        neighbor_radius=1,
        random_negatives=1,
        randomizer=random.Random(1078),
    )
    assert targets == {0: [10.0]}
    assert len(features) == len(labels) == len(identities)
    assert len(FEATURE_NAMES) == len(features[0])
    assert 1 in labels
    assert any(label == 0 for label in labels)
    assert identities[labels.index(1)] == (0, 1)


def test_speech_rate_vector_is_fixed_normalized_and_differentiable():
    word_times = tuple((float(index), float(index) + 0.2) for index in range(0, 90, 2))
    reference = build_speech_rate_reference(
        word_times,
        episode_start=0.0,
        episode_end=90.0,
    )
    normalized, derivative = speech_rate_vector(
        word_times,
        center=45.0,
        reference=reference,
        smoothing_radius=2,
    )
    assert reference.available is True
    assert normalized.shape == (61,)
    assert derivative.shape == (61,)
    assert np.isfinite(normalized).all()
    assert np.isfinite(derivative).all()
    assert np.any(np.abs(derivative) > 0)


def test_speech_rate_missing_word_timing_is_explicitly_masked():
    features, labels, identities, targets = build_episode_features(
        {
            "generated_agenda": {
                "mistral/mistral-medium-2508": {
                    "items": [
                        {
                            "title": "Proposed revision",
                            "evidence_text": "DCA26-0002B proposed revision",
                            "display_ref": "DCA26-0002B",
                        }
                    ]
                }
            }
        },
        {
            "provider_chapters": [
                {"status": "strong", "best_generated_item_index": 0, "start": 10.0}
            ]
        },
        _units(),
        model="mistral/mistral-medium-2508",
        label_tolerance=1.0,
        hard_top_k=1,
        neighbor_radius=1,
        random_negatives=1,
        randomizer=random.Random(1078),
        speech_rate_mode="both",
        word_times=(),
    )
    assert targets == {0: [10.0]}
    assert len(features) == len(labels) == len(identities)
    assert len(features[0]) == len(FEATURE_NAMES) + len(SPEECH_RATE_FEATURE_NAMES)
    speech_start = len(FEATURE_NAMES)
    assert set(features[0][speech_start : speech_start + 61]) == {0.0}
    assert set(features[0][speech_start + 61 : speech_start + 122]) == {0.0}
    assert features[0][speech_start + 122] == 0.0
    assert features[0][speech_start + 123] == 0.0
    assert features[0][speech_start + 124] == 0.0


def test_speech_rate_vector_features_integrate_with_episode_features():
    word_times = tuple((float(index), float(index) + 0.2) for index in range(0, 40, 2))
    features, labels, identities, _targets = build_episode_features(
        {
            "generated_agenda": {
                "mistral/mistral-medium-2508": {
                    "items": [
                        {
                            "title": "Proposed revision",
                            "evidence_text": "DCA26-0002B proposed revision",
                            "display_ref": "DCA26-0002B",
                        }
                    ]
                }
            }
        },
        {
            "provider_chapters": [
                {"status": "strong", "best_generated_item_index": 0, "start": 10.0}
            ]
        },
        _units(),
        model="mistral/mistral-medium-2508",
        label_tolerance=1.0,
        hard_top_k=1,
        neighbor_radius=1,
        random_negatives=1,
        randomizer=random.Random(1078),
        speech_rate_mode="vector",
        word_times=word_times,
    )
    assert len(features) == len(labels) == len(identities)
    assert len(features[0]) == len(FEATURE_NAMES) + 61 + 3


def test_feature_names_match_combined_feature_row_width():
    rows = [
        (
            {"uid": "train"},
            {
                "provider_chapters": [
                    {"status": "strong", "best_generated_item_index": 0, "start": 10.0}
                ]
            },
            _units(),
        ),
    ]
    phrase_model = fit_transition_phrase_model(rows, window_seconds=1.0, min_positive_episodes=1)
    features, _labels, _identities, _targets = build_episode_features(
        {
            "generated_agenda": {
                "mistral/mistral-medium-2508": {
                    "items": [{"title": "Proposed revision", "evidence_text": "next item"}]
                }
            }
        },
        rows[0][1],
        _units(),
        model="mistral/mistral-medium-2508",
        label_tolerance=1.0,
        hard_top_k=1,
        neighbor_radius=1,
        random_negatives=1,
        randomizer=random.Random(1078),
        speech_rate_mode="both",
        word_times=tuple((float(i), float(i) + 0.2) for i in range(0, 40, 2)),
        transition_phrase_mode="learned",
        transition_phrase_model=phrase_model,
    )
    assert len(features[0]) == len(feature_names_for_mode("both", "learned"))


def test_transition_phrase_model_learns_training_fold_cues():
    rows = [
        (
            {"uid": "train"},
            {
                "provider_chapters": [
                    {"status": "strong", "best_generated_item_index": 0, "start": 10.0}
                ]
            },
            [
                LocatorUnit("u0", 0.0, 5.0, "routine opening remarks"),
                LocatorUnit("u1", 10.0, 15.0, "next agenda item parks funding"),
                LocatorUnit("u2", 30.0, 35.0, "routine discussion continues"),
            ],
        ),
        (
            {"uid": "train-2"},
            {
                "provider_chapters": [
                    {"status": "strong", "best_generated_item_index": 0, "start": 10.0}
                ]
            },
            [
                LocatorUnit("u0", 0.0, 5.0, "routine opening remarks"),
                LocatorUnit("u1", 10.0, 15.0, "next agenda item parks funding"),
                LocatorUnit("u2", 30.0, 35.0, "routine discussion continues"),
            ],
        ),
    ]
    model = fit_transition_phrase_model(rows, window_seconds=1.0, min_positive_episodes=2)
    assert model.available is True
    assert "next agenda item" in model.weights
    positive = transition_phrase_features("next agenda item parks funding", model)
    background = transition_phrase_features("routine discussion continues", model)
    assert len(positive) == len(TRANSITION_PHRASE_FEATURE_NAMES)
    assert positive[0] > background[0]
    assert positive[-1] == background[-1] == 1.0


def test_transition_phrase_model_weights_near_start_and_downweights_after_start():
    rows = []
    for uid in ("train-a", "train-b"):
        rows.append(
            (
                {"uid": uid},
                {
                    "provider_chapters": [
                        {"status": "strong", "best_generated_item_index": 0, "start": 10.0}
                    ]
                },
                [
                    LocatorUnit("u0", 0.0, 5.0, "ordinary background"),
                    LocatorUnit("u1", 10.0, 15.0, "boundary phrase"),
                    LocatorUnit("u2", 20.0, 25.0, "after phrase"),
                    LocatorUnit("u3", 100.0, 105.0, "ordinary background"),
                ],
            )
        )
    model = fit_transition_phrase_model(
        rows,
        window_seconds=30.0,
        min_positive_episodes=2,
        decay_seconds=8.0,
        post_boundary_weight=0.35,
    )
    assert model.weights["boundary phrase"] > model.weights["after phrase"]


def test_transition_phrase_features_integrate_without_speech_rate():
    rows = [
        (
            {"uid": "train"},
            {
                "provider_chapters": [
                    {"status": "strong", "best_generated_item_index": 0, "start": 10.0}
                ]
            },
            _units(),
        ),
    ]
    phrase_model = fit_transition_phrase_model(rows, window_seconds=1.0, min_positive_episodes=1)
    features, labels, identities, _targets = build_episode_features(
        {
            "generated_agenda": {
                "mistral/mistral-medium-2508": {
                    "items": [{"title": "Proposed revision", "evidence_text": "next item"}]
                }
            }
        },
        {
            "provider_chapters": [
                {"status": "strong", "best_generated_item_index": 0, "start": 10.0}
            ]
        },
        _units(),
        model="mistral/mistral-medium-2508",
        label_tolerance=1.0,
        hard_top_k=1,
        neighbor_radius=1,
        random_negatives=1,
        randomizer=random.Random(1078),
        transition_phrase_mode="learned",
        transition_phrase_model=phrase_model,
    )
    assert len(features) == len(labels) == len(identities)
    assert len(features[0]) == len(FEATURE_NAMES) + len(TRANSITION_PHRASE_FEATURE_NAMES)


def test_pairwise_examples_compare_only_units_for_same_agenda_item():
    features, labels = _pairwise_examples(
        [[1.0, 0.0], [0.0, 1.0], [0.5, 0.5], [0.1, 0.9]],
        [1, 0, 1, 0],
        [(0, 0), (0, 1), (1, 0), (1, 1)],
        max_pairs_per_item=10,
        randomizer=random.Random(1078),
    )
    assert len(features) == len(labels) == 4
    assert labels.count(1) == labels.count(0) == 2
    assert features[0] == [1.0, -1.0]
    assert features[1] == [-1.0, 1.0]


def test_transition_risk_reports_item_and_episode_coverage():
    result = analyze(
        {
            "models": {
                "hist_gradient_boosting": {
                    "validation_episode_details": [
                        {
                            "uid": "a",
                            "item_diagnostics": {
                                "0": {"margin": 0.2, "learned_hit": True},
                                "1": {"margin": 0.01, "learned_hit": False},
                            },
                        }
                    ]
                }
            }
        },
        model="hist_gradient_boosting",
        field="margin",
        thresholds=(0.1,),
    )
    row = result["thresholds"][0]
    assert row["selected_items"] == 1
    assert row["selected_hits"] == 1
    assert row["episode_coverage"] == 0.0


def test_distinct_assignment_does_not_force_agenda_order():
    assignment = _greedy_distinct_assignment(
        {
            0: np.asarray([0.9, 0.8, 0.1]),
            1: np.asarray([0.95, 0.7, 0.2]),
        },
        candidate_count=2,
    )
    assert assignment == {1: 0, 0: 1}
