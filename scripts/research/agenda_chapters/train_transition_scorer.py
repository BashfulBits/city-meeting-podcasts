#!/usr/bin/env python
"""Train and evaluate a small supervised agenda-to-transcript transition scorer.

This is a read-only GH#1078 research tool.  Provider chapter starts are used as labels only on
the development side of the frozen cohort; they are never included in the feature vector or in a
runtime request.  The script deliberately excludes provider-only, procedural, ambiguous, and
unmatched agenda relationships from positive/negative training labels.  It samples hard negatives
from the existing lexical/TF-IDF retrieval paths so the classifier learns to rerank plausible
windows rather than merely distinguish speech from empty text. Optional speech-rate and
distance-weighted transition-word/phrase features are research ablations and default off.

The held-out manifest split is not read unless explicitly requested.  Keep outputs and the optional
artifact cache outside the repository (for example under ``/private/tmp``).
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss
from sklearn.model_selection import GroupShuffleSplit

from citypods.chapter_locator import LocatorUnit, build_locator_units
from citypods.http import make_session
from scripts.research.agenda_chapters.evaluate_locator_retrieval import (
    _identifier_tokens,
    _query_text,
    _tokens,
    lexical_scores,
    ranked_unit_indices,
    tfidf_score_rows,
    union_ranked_indices,
)

RANDOM_SEED = 1078
DEFAULT_MODEL = "mistral/mistral-medium-2508"
SPEECH_RATE_MODES = ("none", "vector", "derivative", "both")
TRANSITION_PHRASE_MODES = ("none", "learned")
SPEECH_RATE_OFFSETS = tuple(range(-30, 31))
SPEECH_RATE_BIN_SECONDS = 1.0
SPEECH_RATE_SMOOTHING_RADIUS = 2
FEATURE_NAMES = (
    "lexical_score",
    "tfidf_score",
    "lexical_rank_fraction",
    "tfidf_rank_fraction",
    "lexical_local_peak",
    "tfidf_local_peak",
    "previous_unit_novelty",
    "next_unit_novelty",
    "local_change_peak",
    "local_change_mean",
    "token_overlap_query_fraction",
    "token_overlap_unit_fraction",
    "identifier_overlap_count",
    "query_identifier_count",
    "unit_identifier_count",
    "transition_cue_count",
    "transition_cue_any",
    "start_fraction",
    "unit_duration_seconds",
    "previous_gap_seconds",
    "next_gap_seconds",
    "unit_token_count",
    "item_position_fraction",
    "item_count_log",
    "title_token_count",
    "evidence_token_count",
)
SPEECH_RATE_FEATURE_NAMES = tuple(
    [f"speech_rate_norm_{offset:+d}s" for offset in SPEECH_RATE_OFFSETS]
    + [f"speech_rate_derivative_{offset:+d}s" for offset in SPEECH_RATE_OFFSETS]
    + ["speech_rate_available", "speech_rate_reference_median", "speech_rate_reference_scale"]
)
TRANSITION_PHRASE_FEATURE_NAMES = (
    "transition_phrase_score",
    "transition_phrase_max",
    "transition_phrase_positive_mass",
    "transition_phrase_negative_mass",
    "transition_phrase_hit_count",
    "transition_phrase_available",
)


@dataclass(frozen=True)
class SpeechRateReference:
    """Robust episode-level scale used to normalize a candidate's rate vector."""

    available: bool
    median: float = 0.0
    scale: float = 1.0


@dataclass(frozen=True)
class TransitionPhraseModel:
    """Training-fold log-odds model for words and short phrases near boundaries."""

    weights: Mapping[str, float]
    positive_documents: int
    background_documents: int
    positive_terms: int
    positive_episodes: int = 0
    available: bool = False

    @property
    def top_positive_phrases(self) -> tuple[tuple[str, float], ...]:
        return tuple(
            sorted(
                ((term, weight) for term, weight in self.weights.items() if weight > 0),
                key=lambda pair: (-pair[1], pair[0]),
            )[:50]
        )


WordTiming = tuple[float, float]
Artifact = tuple[
    Mapping[str, Any],
    list[LocatorUnit],
    str | None,
    tuple[WordTiming, ...] | None,
    SpeechRateReference,
]
_CUE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bnext (?:item|agenda item|matter)\b",
        r"\bmove(?: on| to)\b",
        r"\bgo to\b",
        r"\bitem (?:number|no\.?|#)\b",
        r"\bagenda item\b",
        r"\bpublic hearing\b",
        r"\b(?:motion|second|vote|voted|approved)\b",
        r"\b(?:consider|take up|turn to)\b",
    )
)


def _validate_speech_rate_mode(mode: str) -> str:
    if mode not in SPEECH_RATE_MODES:
        raise ValueError(f"unknown speech-rate mode: {mode}")
    return mode


def _validate_transition_phrase_mode(mode: str) -> str:
    if mode not in TRANSITION_PHRASE_MODES:
        raise ValueError(f"unknown transition-phrase mode: {mode}")
    return mode


def _phrase_terms(text: str, *, max_n: int = 3) -> set[str]:
    """Return unique stopword-filtered word/phrase terms for one timed unit."""
    tokens = _tokens(text)
    return {
        " ".join(tokens[index : index + size])
        for size in range(1, max_n + 1)
        for index in range(len(tokens) - size + 1)
    }


def fit_transition_phrase_model(
    rows: Sequence[tuple[Mapping[str, Any], Mapping[str, Any], Sequence[LocatorUnit]]],
    *,
    window_seconds: float = 30.0,
    min_positive_episodes: int = 2,
    max_terms: int = 2000,
    smoothing: float = 0.25,
    decay_seconds: float = 8.0,
    post_boundary_weight: float = 0.35,
) -> TransitionPhraseModel:
    """Learn transition-word/phrase log odds from training episodes only.

    A document is one timed transcript unit. Units within ``window_seconds`` of a strong
    provider chapter start form the positive transition context; all other units are background.
    Positive evidence is weighted by distance to the nearest known start and downweighted after
    the start, where substantive item discussion is more likely than transition language. Rates
    are aggregated per episode before log odds are calculated, so a long or repetitive meeting
    cannot dominate the map. This is deliberately an agenda-independent cue model, so it can be
    used to rerank any generated agenda item without leaking its hidden provider target into the
    feature vector.
    """
    if decay_seconds <= 0:
        raise ValueError("decay_seconds must be positive")
    if not 0 < post_boundary_weight <= 1:
        raise ValueError("post_boundary_weight must be in (0, 1]")
    positive_term_scores: Counter[str] = Counter()
    background_term_rates: Counter[str] = Counter()
    positive_episode_counts: Counter[str] = Counter()
    positive_documents = 0
    background_documents = 0
    positive_episodes = 0
    for _row, crosswalk_row, units in rows:
        targets = _strong_targets(crosswalk_row)
        starts = [start for values in targets.values() for start in values]
        if not starts:
            continue
        episode_positive: dict[str, float] = {}
        episode_background: Counter[str] = Counter()
        episode_background_documents = 0
        for unit in units:
            terms = _phrase_terms(unit.text)
            if not terms:
                continue
            distance = min(abs(unit.start - start) for start in starts)
            if distance <= window_seconds:
                weight = math.exp(-distance / decay_seconds)
                if unit.start > min(starts, key=lambda start: abs(unit.start - start)):
                    weight *= post_boundary_weight
                for term in terms:
                    episode_positive[term] = max(episode_positive.get(term, 0.0), weight)
                positive_documents += 1
            else:
                episode_background_documents += 1
                episode_background.update(terms)
        if episode_positive and episode_background_documents:
            positive_episodes += 1
            background_documents += episode_background_documents
            for term, score in episode_positive.items():
                positive_term_scores[term] += score
                positive_episode_counts[term] += 1
            for term, count in episode_background.items():
                background_term_rates[term] += count / episode_background_documents
    if not positive_documents or not background_documents:
        return TransitionPhraseModel(
            {}, positive_documents, background_documents, 0, positive_episodes, False
        )

    weights: dict[str, float] = {}
    for term, positive_score in positive_term_scores.items():
        if positive_episode_counts[term] < min_positive_episodes:
            continue
        positive_rate = (positive_score + smoothing) / (positive_episodes + 2 * smoothing)
        background_rate = (background_term_rates.get(term, 0.0) + smoothing) / (
            positive_episodes + 2 * smoothing
        )
        weights[term] = float(np.clip(math.log(positive_rate / background_rate), -6.0, 6.0))
    if len(weights) > max_terms:
        weights = dict(
            sorted(weights.items(), key=lambda pair: (-abs(pair[1]), pair[0]))[:max_terms]
        )
    return TransitionPhraseModel(
        weights=weights,
        positive_documents=positive_documents,
        background_documents=background_documents,
        positive_terms=len(weights),
        positive_episodes=positive_episodes,
        available=bool(weights),
    )


def transition_phrase_features(text: str, model: TransitionPhraseModel | None) -> list[float]:
    """Return compact learned phrase statistics for one timed unit."""
    if model is None or not model.available:
        return [0.0] * len(TRANSITION_PHRASE_FEATURE_NAMES)
    weights = [model.weights[term] for term in _phrase_terms(text) if term in model.weights]
    if not weights:
        return [0.0, 0.0, 0.0, 0.0, 0.0, 1.0]
    positive = [weight for weight in weights if weight > 0]
    negative = [weight for weight in weights if weight < 0]
    normalizer = math.sqrt(len(weights))
    return [
        float(sum(weights) / len(weights)),
        float(max(weights)),
        float(sum(positive) / normalizer),
        float(sum(negative) / normalizer),
        float(len(weights)),
        1.0,
    ]


def _word_timestamps(data: bytes | None) -> tuple[WordTiming, ...]:
    """Extract valid word spans from the ASR sidecar without changing locator units."""
    if not data:
        return ()
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ()
    if not isinstance(payload, dict):
        return ()
    result: list[WordTiming] = []
    for segment in payload.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        for word in segment.get("words") or []:
            if not isinstance(word, dict) or not str(word.get("w") or "").strip():
                continue
            try:
                start = float(word.get("s", word.get("start")))
                end = float(word.get("e", word.get("end")))
            except (TypeError, ValueError):
                continue
            if 0 <= start <= end:
                result.append((start, end))
    return tuple(sorted(result))


def _speech_rate_samples(
    word_times: Sequence[WordTiming] | np.ndarray, centers: np.ndarray
) -> np.ndarray:
    """Return words/second in one-second bins centered at ``centers``."""
    if len(word_times) == 0:
        return np.zeros(len(centers), dtype=np.float32)
    starts = (
        word_times
        if isinstance(word_times, np.ndarray)
        else np.asarray([start for start, _end in word_times], dtype=np.float64)
    )
    half_bin = SPEECH_RATE_BIN_SECONDS / 2.0
    left = np.searchsorted(starts, centers - half_bin, side="left")
    right = np.searchsorted(starts, centers + half_bin, side="left")
    return ((right - left) / SPEECH_RATE_BIN_SECONDS).astype(np.float32)


def _smooth_speech_rate(values: np.ndarray, *, radius: int) -> np.ndarray:
    if radius <= 0 or len(values) < 2:
        return values.astype(np.float32, copy=True)
    width = radius * 2 + 1
    padded = np.pad(values, (radius, radius), mode="edge")
    kernel = np.full(width, 1.0 / width, dtype=np.float32)
    return np.convolve(padded, kernel, mode="valid").astype(np.float32)


def build_speech_rate_reference(
    word_times: Sequence[WordTiming], *, episode_start: float, episode_end: float
) -> SpeechRateReference:
    """Build a robust episode-level speech-rate scale from word timings only."""
    if not word_times or episode_end < episode_start:
        return SpeechRateReference(available=False)
    start = math.floor(episode_start)
    end = math.ceil(episode_end)
    centers = np.arange(start, end + 1, SPEECH_RATE_BIN_SECONDS, dtype=np.float64)
    samples = _speech_rate_samples(word_times, centers)
    positive = samples[samples > 0]
    if not len(positive):
        return SpeechRateReference(available=False)
    median = float(np.median(positive))
    mad = float(np.median(np.abs(positive - median)))
    scale = max(1.4826 * mad, 0.25, median * 0.1)
    return SpeechRateReference(available=True, median=median, scale=scale)


def speech_rate_vector(
    word_times: Sequence[WordTiming] | np.ndarray,
    *,
    center: float,
    reference: SpeechRateReference,
    smoothing_radius: int = SPEECH_RATE_SMOOTHING_RADIUS,
) -> tuple[np.ndarray, np.ndarray]:
    """Return normalized rate and first-derivative vectors for ``center ± 30`` seconds."""
    offsets = np.asarray(SPEECH_RATE_OFFSETS, dtype=np.float64)
    raw = _speech_rate_samples(word_times, center + offsets)
    smoothed = _smooth_speech_rate(raw, radius=smoothing_radius)
    if not reference.available:
        normalized = np.zeros(len(offsets), dtype=np.float32)
    else:
        normalized = ((smoothed - reference.median) / reference.scale).astype(np.float32)
    derivative = np.gradient(normalized, SPEECH_RATE_BIN_SECONDS).astype(np.float32)
    return normalized, derivative


def _speech_rate_features(
    *,
    word_times: Sequence[WordTiming] | np.ndarray | None,
    center: float,
    reference: SpeechRateReference,
    mode: str,
    smoothing_radius: int,
) -> list[float]:
    mode = _validate_speech_rate_mode(mode)
    if mode == "none":
        return []
    word_input = word_times if word_times is not None else ()
    normalized, derivative = speech_rate_vector(
        word_input,
        center=center,
        reference=reference,
        smoothing_radius=smoothing_radius,
    )
    values: list[float] = []
    if mode in {"vector", "both"}:
        values.extend(float(value) for value in normalized)
    if mode in {"derivative", "both"}:
        values.extend(float(value) for value in derivative)
    values.extend(
        [
            float(reference.available),
            float(reference.median),
            float(reference.scale),
        ]
    )
    return values


def _speech_rate_feature_rows(
    units: Sequence[LocatorUnit],
    *,
    word_times: Sequence[WordTiming] | None,
    reference: SpeechRateReference,
    mode: str,
    smoothing_radius: int,
) -> tuple[tuple[float, ...], ...]:
    """Precompute one optional speech-rate feature vector per timed unit."""
    mode = _validate_speech_rate_mode(mode)
    if mode == "none":
        return tuple(() for _unit in units)
    starts = np.asarray([start for start, _end in word_times or ()], dtype=np.float64)
    return tuple(
        tuple(
            _speech_rate_features(
                word_times=starts,
                center=unit.start,
                reference=reference,
                mode=mode,
                smoothing_radius=smoothing_radius,
            )
        )
        for unit in units
    )


def _transition_phrase_feature_rows(
    units: Sequence[LocatorUnit],
    *,
    model: TransitionPhraseModel | None,
    mode: str,
) -> tuple[tuple[float, ...], ...]:
    mode = _validate_transition_phrase_mode(mode)
    if mode == "none":
        return tuple(() for _unit in units)
    return tuple(transition_phrase_features(unit.text, model) for unit in units)


def feature_names_for_mode(mode: str, transition_phrase_mode: str = "none") -> tuple[str, ...]:
    """Return serialized feature names for one research scorer feature combination."""
    mode = _validate_speech_rate_mode(mode)
    transition_phrase_mode = _validate_transition_phrase_mode(transition_phrase_mode)
    names: list[str] = list(FEATURE_NAMES)
    if mode in {"vector", "both"}:
        names.extend(f"speech_rate_norm_{offset:+d}s" for offset in SPEECH_RATE_OFFSETS)
    if mode in {"derivative", "both"}:
        names.extend(f"speech_rate_derivative_{offset:+d}s" for offset in SPEECH_RATE_OFFSETS)
    if mode != "none":
        names.extend(
            ["speech_rate_available", "speech_rate_reference_median", "speech_rate_reference_scale"]
        )
    if transition_phrase_mode == "learned":
        names.extend(TRANSITION_PHRASE_FEATURE_NAMES)
    return tuple(names)


def _fetch(session: Any, url: str | None) -> bytes | None:
    if not url:
        return None
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return response.content


def _artifact_bytes(
    session: Any, row: Mapping[str, Any], *, cache_dir: Path | None
) -> tuple[bytes | None, bytes | None, str | None]:
    """Load words/VTT, optionally caching public bytes in a temporary research directory."""
    uid = str(row.get("uid"))
    transcript = row.get("transcript") or {}
    values: dict[str, bytes | None] = {}
    for kind, url_key in (("words", "words_url"), ("vtt", "url")):
        path = cache_dir / f"{uid}.{kind}" if cache_dir else None
        if path and path.exists():
            values[kind] = path.read_bytes()
            continue
        values[kind] = _fetch(session, transcript.get(url_key))
        if path and values[kind] is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(values[kind])
    units, source = build_locator_units(words_data=values["words"], vtt_data=values["vtt"])
    return values["words"], values["vtt"], source if units else None


def _strong_targets(crosswalk_row: Mapping[str, Any]) -> dict[int, list[float]]:
    """Return strong provider starts grouped by generated agenda item index."""
    grouped: dict[int, list[float]] = defaultdict(list)
    for chapter in crosswalk_row.get("provider_chapters", []):
        item_index = chapter.get("best_generated_item_index")
        start = chapter.get("start")
        if (
            chapter.get("status") == "strong"
            and isinstance(item_index, int)
            and isinstance(start, (int, float))
        ):
            grouped[item_index].append(float(start))
    return dict(grouped)


def _rank_fraction(scores: Sequence[float]) -> list[float]:
    if not scores:
        return []
    ordered = np.argsort(-np.asarray(scores), kind="stable").tolist()
    ranks = [0.0] * len(scores)
    denominator = max(1, len(scores) - 1)
    for rank, index in enumerate(ordered):
        ranks[index] = 1.0 - rank / denominator
    return ranks


def _feature_row(
    item: Mapping[str, Any],
    unit: LocatorUnit,
    index: int,
    units: Sequence[LocatorUnit],
    lexical: Sequence[float],
    tfidf: Sequence[float],
    *,
    lexical_rank: Sequence[float],
    tfidf_rank: Sequence[float],
    item_index: int,
    item_count: int,
    speech_rate_mode: str = "none",
    word_times: Sequence[WordTiming] | None = None,
    speech_rate_reference: SpeechRateReference | None = None,
    speech_rate_smoothing_radius: int = SPEECH_RATE_SMOOTHING_RADIUS,
    speech_rate_values: Sequence[float] | None = None,
    transition_phrase_mode: str = "none",
    transition_phrase_model: TransitionPhraseModel | None = None,
    transition_phrase_values: Sequence[float] | None = None,
) -> list[float]:
    query_tokens = set(_tokens(_query_text(item)))
    unit_tokens = set(_tokens(unit.text))
    query_identifiers = _identifier_tokens(_query_text(item))
    unit_identifiers = _identifier_tokens(unit.text)
    previous = lexical[index - 1] if index else lexical[index]
    following = lexical[index + 1] if index + 1 < len(units) else lexical[index]
    previous_tfidf = tfidf[index - 1] if index else tfidf[index]
    following_tfidf = tfidf[index + 1] if index + 1 < len(units) else tfidf[index]
    previous_tokens = set(_tokens(units[index - 1].text)) if index else unit_tokens
    following_tokens = (
        set(_tokens(units[index + 1].text)) if index + 1 < len(units) else unit_tokens
    )

    def novelty(other_tokens: set[str]) -> float:
        union = unit_tokens | other_tokens
        if not union:
            return 0.0
        return 1.0 - len(unit_tokens & other_tokens) / len(union)

    previous_novelty = novelty(previous_tokens)
    following_novelty = novelty(following_tokens)
    previous_gap = unit.start - units[index - 1].end if index else 0.0
    next_gap = units[index + 1].start - unit.end if index + 1 < len(units) else 0.0
    cues = sum(bool(pattern.search(unit.text)) for pattern in _CUE_PATTERNS)
    duration = max(0.0, unit.end - unit.start)
    total_duration = max(1.0, units[-1].end - units[0].start)
    features = [
        float(lexical[index]),
        float(tfidf[index]),
        float(lexical_rank[index]),
        float(tfidf_rank[index]),
        float(lexical[index] - max(previous, following)),
        float(tfidf[index] - max(previous_tfidf, following_tfidf)),
        previous_novelty,
        following_novelty,
        max(previous_novelty, following_novelty),
        (previous_novelty + following_novelty) / 2.0,
        len(query_tokens & unit_tokens) / max(1, len(query_tokens)),
        len(query_tokens & unit_tokens) / max(1, len(unit_tokens)),
        float(len(query_identifiers & unit_identifiers)),
        float(len(query_identifiers)),
        float(len(unit_identifiers)),
        float(cues),
        float(bool(cues)),
        float((unit.start - units[0].start) / total_duration),
        duration,
        max(0.0, previous_gap),
        max(0.0, next_gap),
        float(len(unit_tokens)),
        item_index / max(1, item_count - 1),
        math.log1p(item_count),
        float(len(_tokens(str(item.get("title") or "")))),
        float(len(_tokens(str(item.get("evidence_text") or "")))),
    ]
    if speech_rate_mode != "none":
        if speech_rate_values is None:
            reference = speech_rate_reference or build_speech_rate_reference(
                word_times or (), episode_start=units[0].start, episode_end=units[-1].end
            )
            speech_rate_values = _speech_rate_features(
                word_times=word_times,
                center=unit.start,
                reference=reference,
                mode=speech_rate_mode,
                smoothing_radius=speech_rate_smoothing_radius,
            )
        features.extend(float(value) for value in speech_rate_values)
    transition_phrase_mode = _validate_transition_phrase_mode(transition_phrase_mode)
    if transition_phrase_mode == "learned":
        if transition_phrase_values is None:
            transition_phrase_values = transition_phrase_features(
                unit.text, transition_phrase_model
            )
        features.extend(float(value) for value in transition_phrase_values)
    return features


def build_episode_features(
    manifest_row: Mapping[str, Any],
    crosswalk_row: Mapping[str, Any],
    units: Sequence[LocatorUnit],
    *,
    model: str,
    label_tolerance: float,
    hard_top_k: int,
    neighbor_radius: int,
    random_negatives: int,
    randomizer: random.Random,
    speech_rate_mode: str = "none",
    word_times: Sequence[WordTiming] | None = None,
    speech_rate_reference: SpeechRateReference | None = None,
    speech_rate_smoothing_radius: int = SPEECH_RATE_SMOOTHING_RADIUS,
    transition_phrase_mode: str = "none",
    transition_phrase_model: TransitionPhraseModel | None = None,
) -> tuple[list[list[float]], list[int], list[tuple[int, int]], dict[int, list[float]]]:
    """Build sampled training rows and preserve item/unit identities for evaluation."""
    generated = (manifest_row.get("generated_agenda") or {}).get(model) or {}
    items = generated.get("items") or []
    targets = _strong_targets(crosswalk_row)
    if not units or not items or not targets:
        return [], [], [], targets
    lexical_by_item = [lexical_scores(item, units) for item in items]
    tfidf_by_item = tfidf_score_rows(items, units)
    lexical_ranks = [_rank_fraction(scores) for scores in lexical_by_item]
    tfidf_ranks = [_rank_fraction(scores) for scores in tfidf_by_item]
    speech_rate_rows = _speech_rate_feature_rows(
        units,
        word_times=word_times,
        reference=speech_rate_reference
        or build_speech_rate_reference(
            word_times or (), episode_start=units[0].start, episode_end=units[-1].end
        ),
        mode=speech_rate_mode,
        smoothing_radius=speech_rate_smoothing_radius,
    )
    phrase_rows = _transition_phrase_feature_rows(
        units, model=transition_phrase_model, mode=transition_phrase_mode
    )
    features: list[list[float]] = []
    labels: list[int] = []
    identities: list[tuple[int, int]] = []
    all_indices = list(range(len(units)))
    for item_index, starts in targets.items():
        if item_index >= len(items):
            continue
        positive = {
            index
            for index, unit in enumerate(units)
            if min(abs(unit.start - start) for start in starts) <= label_tolerance
        }
        hard: set[int] = set(ranked_unit_indices(lexical_by_item[item_index], top_k=hard_top_k))
        hard.update(ranked_unit_indices(tfidf_by_item[item_index], top_k=hard_top_k))
        hard.update(
            union_ranked_indices(
                lexical_by_item[item_index],
                tfidf_by_item[item_index],
                top_k=hard_top_k,
                neighbor_radius=neighbor_radius,
            )
        )
        negatives = [index for index in hard if index not in positive]
        available = [index for index in all_indices if index not in positive and index not in hard]
        if available and random_negatives:
            negatives.extend(randomizer.sample(available, min(random_negatives, len(available))))
        selected = sorted(positive | set(negatives))
        for index in selected:
            features.append(
                _feature_row(
                    items[item_index],
                    units[index],
                    index,
                    units,
                    lexical_by_item[item_index],
                    tfidf_by_item[item_index],
                    lexical_rank=lexical_ranks[item_index],
                    tfidf_rank=tfidf_ranks[item_index],
                    item_index=item_index,
                    item_count=len(items),
                    speech_rate_mode=speech_rate_mode,
                    word_times=word_times,
                    speech_rate_reference=speech_rate_reference,
                    speech_rate_smoothing_radius=speech_rate_smoothing_radius,
                    speech_rate_values=speech_rate_rows[index],
                    transition_phrase_mode=transition_phrase_mode,
                    transition_phrase_model=transition_phrase_model,
                    transition_phrase_values=phrase_rows[index],
                )
            )
            labels.append(int(index in positive))
            identities.append((item_index, index))
    return features, labels, identities, targets


def _pairwise_examples(
    features: Sequence[Sequence[float]],
    labels: Sequence[int],
    identities: Sequence[tuple[int, int]],
    *,
    max_pairs_per_item: int,
    randomizer: random.Random,
) -> tuple[list[list[float]], list[int]]:
    """Build balanced positive-vs-negative comparisons grouped by agenda item.

    A comparison is kept in both directions so a linear logistic model can learn a ranking
    function without treating unrelated agenda items as negatives for one another.
    """
    by_item: dict[int, dict[int, list[int]]] = defaultdict(lambda: {0: [], 1: []})
    for row_index, (label, identity) in enumerate(zip(labels, identities, strict=True)):
        by_item[identity[0]][int(label)].append(row_index)
    pair_features: list[list[float]] = []
    pair_labels: list[int] = []
    for grouped in by_item.values():
        positives = grouped[1]
        negatives = grouped[0]
        pairs = [(positive, negative) for positive in positives for negative in negatives]
        if max_pairs_per_item > 0 and len(pairs) > max_pairs_per_item:
            pairs = randomizer.sample(pairs, max_pairs_per_item)
        for positive, negative in pairs:
            positive_row = np.asarray(features[positive], dtype=np.float32)
            negative_row = np.asarray(features[negative], dtype=np.float32)
            pair_features.append((positive_row - negative_row).tolist())
            pair_labels.append(1)
            pair_features.append((negative_row - positive_row).tolist())
            pair_labels.append(0)
    return pair_features, pair_labels


def _all_item_features(
    item: Mapping[str, Any],
    item_index: int,
    items: Sequence[Mapping[str, Any]],
    units: Sequence[LocatorUnit],
    lexical: Sequence[float],
    tfidf: Sequence[float],
    lexical_rank: Sequence[float],
    tfidf_rank: Sequence[float],
    speech_rate_mode: str = "none",
    word_times: Sequence[WordTiming] | None = None,
    speech_rate_reference: SpeechRateReference | None = None,
    speech_rate_smoothing_radius: int = SPEECH_RATE_SMOOTHING_RADIUS,
    speech_rate_feature_rows: Sequence[Sequence[float]] | None = None,
    transition_phrase_mode: str = "none",
    transition_phrase_model: TransitionPhraseModel | None = None,
    transition_phrase_feature_rows: Sequence[Sequence[float]] | None = None,
) -> np.ndarray:
    if speech_rate_feature_rows is None:
        speech_rate_feature_rows = _speech_rate_feature_rows(
            units,
            word_times=word_times,
            reference=speech_rate_reference
            or build_speech_rate_reference(
                word_times or (), episode_start=units[0].start, episode_end=units[-1].end
            ),
            mode=speech_rate_mode,
            smoothing_radius=speech_rate_smoothing_radius,
        )
    if transition_phrase_feature_rows is None:
        transition_phrase_feature_rows = _transition_phrase_feature_rows(
            units, model=transition_phrase_model, mode=transition_phrase_mode
        )
    rows = np.asarray(
        [
            _feature_row(
                item,
                unit,
                index,
                units,
                lexical,
                tfidf,
                lexical_rank=lexical_rank,
                tfidf_rank=tfidf_rank,
                item_index=item_index,
                item_count=len(items),
                speech_rate_mode=speech_rate_mode,
                word_times=word_times,
                speech_rate_reference=speech_rate_reference,
                speech_rate_smoothing_radius=speech_rate_smoothing_radius,
                speech_rate_values=speech_rate_feature_rows[index],
                transition_phrase_mode=transition_phrase_mode,
                transition_phrase_model=transition_phrase_model,
                transition_phrase_values=transition_phrase_feature_rows[index],
            )
            for index, unit in enumerate(units)
        ],
        dtype=np.float32,
    )
    return rows


def _positive_hits(
    units: Sequence[LocatorUnit],
    indices: Sequence[int],
    starts: Sequence[float],
    tolerance: float,
) -> bool:
    return any(
        abs(units[index].start - start) <= tolerance for index in indices for start in starts
    )


def _greedy_distinct_assignment(
    probabilities_by_item: Mapping[int, np.ndarray], *, candidate_count: int
) -> dict[int, int]:
    """Assign distinct high-scoring units without imposing agenda order.

    This is deliberately a small research baseline, not a production decoder. It permits agenda
    skips and reordering; its only constraint is that two agenda items do not reuse the same timed
    unit. A later dynamic-programming decoder can replace it if this diversity constraint helps.
    """
    edges: list[tuple[float, int, int]] = []
    for item_index, probabilities in probabilities_by_item.items():
        order = np.argsort(-probabilities, kind="stable").tolist()
        limit = len(order) if candidate_count <= 0 else min(candidate_count, len(order))
        edges.extend(
            (float(probabilities[unit_index]), item_index, unit_index)
            for unit_index in order[:limit]
        )
    assignment: dict[int, int] = {}
    used_units: set[int] = set()
    for _score, item_index, unit_index in sorted(
        edges, key=lambda edge: (-edge[0], edge[1], edge[2])
    ):
        if item_index in assignment or unit_index in used_units:
            continue
        assignment[item_index] = unit_index
        used_units.add(unit_index)
    return assignment


def _score_validation_episode(
    manifest_row: Mapping[str, Any],
    crosswalk_row: Mapping[str, Any],
    units: Sequence[LocatorUnit],
    models: Mapping[str, Any],
    *,
    model_name: str,
    agenda_model: str,
    score_tolerance: float,
    top_ks: Sequence[int],
    candidate_pool_top_k: int,
    neighbor_radius: int,
    reconcile_candidate_count: int,
    speech_rate_mode: str,
    word_times: Sequence[WordTiming] | None,
    speech_rate_reference: SpeechRateReference,
    speech_rate_smoothing_radius: int,
    transition_phrase_mode: str,
    transition_phrase_model: TransitionPhraseModel | None,
    score_all_items: bool = False,
) -> dict[str, Any]:
    generated = (manifest_row.get("generated_agenda") or {}).get(agenda_model) or {}
    items = generated.get("items") or []
    targets = _strong_targets(crosswalk_row)
    result: dict[str, Any] = {
        "uid": manifest_row.get("uid"),
        "provider": manifest_row.get("provider"),
        "slug": manifest_row.get("slug"),
        "covered_provider_chapters": sum(len(starts) for starts in targets.values()),
        "covered_generated_candidates": len(targets),
        "hits": {},
        "candidate_hits": {},
        "baseline_hits": {},
        "baseline_candidate_hits": {},
        "learned_only_hits": {},
        "learned_only_candidate_hits": {},
        "combined_hits": {},
        "combined_candidate_hits": {},
        "reconciled_hits": 0,
        "reconciled_candidate_hits": 0,
        "confidence": {},
        "item_diagnostics": {},
    }
    probabilities_by_item: dict[int, np.ndarray] = {}
    lexical_by_item = [lexical_scores(item, units) for item in items]
    tfidf_by_item = tfidf_score_rows(items, units)
    lexical_ranks = [_rank_fraction(scores) for scores in lexical_by_item]
    tfidf_ranks = [_rank_fraction(scores) for scores in tfidf_by_item]
    speech_rate_rows = _speech_rate_feature_rows(
        units,
        word_times=word_times,
        reference=speech_rate_reference,
        mode=speech_rate_mode,
        smoothing_radius=speech_rate_smoothing_radius,
    )
    phrase_rows = _transition_phrase_feature_rows(
        units, model=transition_phrase_model, mode=transition_phrase_mode
    )
    scored_item_indices = range(len(items)) if score_all_items else targets
    for item_index in scored_item_indices:
        if item_index >= len(items):
            continue
        feature_matrix = _all_item_features(
            items[item_index],
            item_index,
            items,
            units,
            lexical_by_item[item_index],
            tfidf_by_item[item_index],
            lexical_ranks[item_index],
            tfidf_ranks[item_index],
            speech_rate_mode=speech_rate_mode,
            word_times=word_times,
            speech_rate_reference=speech_rate_reference,
            speech_rate_smoothing_radius=speech_rate_smoothing_radius,
            speech_rate_feature_rows=speech_rate_rows,
            transition_phrase_mode=transition_phrase_mode,
            transition_phrase_model=transition_phrase_model,
            transition_phrase_feature_rows=phrase_rows,
        )
        probabilities_by_item[item_index] = models[model_name].predict_proba(feature_matrix)[:, 1]
        probabilities = probabilities_by_item[item_index]
        pool = (
            list(range(len(units)))
            if candidate_pool_top_k <= 0
            else union_ranked_indices(
                lexical_by_item[item_index],
                tfidf_by_item[item_index],
                top_k=candidate_pool_top_k,
                neighbor_radius=neighbor_radius,
            )
        )
        order = sorted(pool, key=lambda unit_index: (-probabilities[unit_index], unit_index))
        top = float(probabilities[order[0]]) if len(order) else 0.0
        second = float(probabilities[order[1]]) if len(order) > 1 else 0.0
        result["confidence"][str(item_index)] = {
            "top_probability": round(top, 6),
            "margin": round(top - second, 6),
        }
        result["item_diagnostics"][str(item_index)] = {
            "target_starts": [round(start, 3) for start in targets.get(item_index, ())],
            "top_unit_start": round(units[order[0]].start, 3) if order else None,
            "top_probability": round(top, 6),
            "margin": round(top - second, 6),
            "learned_top_units": [
                {
                    "id": units[unit_index].id,
                    "start": round(units[unit_index].start, 3),
                    "score": round(float(probabilities[unit_index]), 6),
                }
                for unit_index in order[:10]
            ],
        }
    reconciled = _greedy_distinct_assignment(
        probabilities_by_item, candidate_count=reconcile_candidate_count
    )
    for item_index, starts in targets.items():
        unit_index = reconciled.get(item_index)
        hit = bool(
            unit_index is not None and _positive_hits(units, [unit_index], starts, score_tolerance)
        )
        result["reconciled_hits"] += (
            sum(_positive_hits(units, [unit_index], [start], score_tolerance) for start in starts)
            if unit_index is not None
            else 0
        )
        result["reconciled_candidate_hits"] += int(hit)
        diagnostic = result["item_diagnostics"].get(str(item_index))
        if diagnostic is not None:
            diagnostic["reconciled_hit"] = hit
            diagnostic["reconciled_unit_start"] = (
                round(units[unit_index].start, 3) if unit_index is not None else None
            )
    for top_k in top_ks:
        chapter_hits = 0
        candidate_hits = 0
        baseline_chapter_hits = 0
        baseline_candidate_hits = 0
        learned_only_chapter_hits = 0
        learned_only_candidate_hits = 0
        combined_chapter_hits = 0
        combined_candidate_hits = 0
        for item_index, starts in targets.items():
            probabilities = probabilities_by_item.get(item_index)
            if probabilities is None:
                continue
            pool = (
                list(range(len(units)))
                if candidate_pool_top_k <= 0
                else union_ranked_indices(
                    lexical_by_item[item_index],
                    tfidf_by_item[item_index],
                    top_k=candidate_pool_top_k,
                    neighbor_radius=neighbor_radius,
                )
            )
            indices = sorted(pool, key=lambda unit_index: (-probabilities[unit_index], unit_index))[
                :top_k
            ]
            baseline_indices = union_ranked_indices(
                lexical_by_item[item_index],
                tfidf_by_item[item_index],
                top_k=top_k,
                neighbor_radius=neighbor_radius,
            )
            baseline_chapter_hits += sum(
                _positive_hits(units, baseline_indices, [start], score_tolerance)
                for start in starts
            )
            baseline_candidate_hits += int(
                _positive_hits(units, baseline_indices, starts, score_tolerance)
            )
            combined_indices = sorted(set(indices) | set(baseline_indices))
            combined_chapter_hits += sum(
                _positive_hits(units, combined_indices, [start], score_tolerance)
                for start in starts
            )
            combined_candidate_hits += int(
                _positive_hits(units, combined_indices, starts, score_tolerance)
            )
            learned_chapter_hits = sum(
                _positive_hits(units, indices, [start], score_tolerance) for start in starts
            )
            learned_candidate_hit = int(_positive_hits(units, indices, starts, score_tolerance))
            chapter_hits += learned_chapter_hits
            candidate_hits += learned_candidate_hit
            learned_only_chapter_hits += sum(
                _positive_hits(units, indices, [start], score_tolerance)
                and not _positive_hits(units, baseline_indices, [start], score_tolerance)
                for start in starts
            )
            learned_only_candidate_hits += int(
                learned_candidate_hit
                and not _positive_hits(units, baseline_indices, starts, score_tolerance)
            )
            if top_k == max(top_ks):
                diagnostic = result["item_diagnostics"][str(item_index)]
                diagnostic["learned_hit"] = bool(
                    _positive_hits(units, indices, starts, score_tolerance)
                )
                diagnostic["baseline_hit"] = bool(
                    _positive_hits(units, baseline_indices, starts, score_tolerance)
                )
                diagnostic["combined_hit"] = bool(
                    _positive_hits(units, combined_indices, starts, score_tolerance)
                )
                diagnostic["learned_top_unit_start"] = (
                    round(units[indices[0]].start, 3) if indices else None
                )
                diagnostic["baseline_top_unit_start"] = (
                    round(units[baseline_indices[0]].start, 3) if baseline_indices else None
                )
        result["hits"][str(top_k)] = chapter_hits
        result["candidate_hits"][str(top_k)] = candidate_hits
        result["baseline_hits"][str(top_k)] = baseline_chapter_hits
        result["baseline_candidate_hits"][str(top_k)] = baseline_candidate_hits
        result["learned_only_hits"][str(top_k)] = learned_only_chapter_hits
        result["learned_only_candidate_hits"][str(top_k)] = learned_only_candidate_hits
        result["combined_hits"][str(top_k)] = combined_chapter_hits
        result["combined_candidate_hits"][str(top_k)] = combined_candidate_hits
    return result


def _aggregate(rows: Sequence[Mapping[str, Any]], top_ks: Sequence[int]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for top_k in top_ks:
        chapters = sum(int(row["covered_provider_chapters"]) for row in rows)
        candidates = sum(int(row["covered_generated_candidates"]) for row in rows)
        chapter_hits = sum(int(row["hits"][str(top_k)]) for row in rows)
        candidate_hits = sum(int(row["candidate_hits"][str(top_k)]) for row in rows)
        baseline_chapter_hits = sum(int(row["baseline_hits"][str(top_k)]) for row in rows)
        baseline_candidate_hits = sum(
            int(row["baseline_candidate_hits"][str(top_k)]) for row in rows
        )
        learned_only_chapter_hits = sum(int(row["learned_only_hits"][str(top_k)]) for row in rows)
        learned_only_candidate_hits = sum(
            int(row["learned_only_candidate_hits"][str(top_k)]) for row in rows
        )
        combined_chapter_hits = sum(int(row["combined_hits"][str(top_k)]) for row in rows)
        combined_candidate_hits = sum(
            int(row["combined_candidate_hits"][str(top_k)]) for row in rows
        )
        reconciled_chapter_hits = sum(int(row.get("reconciled_hits", 0)) for row in rows)
        reconciled_candidate_hits = sum(
            int(row.get("reconciled_candidate_hits", 0)) for row in rows
        )
        result[str(top_k)] = {
            "chapter_hits": chapter_hits,
            "chapter_denominator": chapters,
            "chapter_recall": round(chapter_hits / chapters, 4) if chapters else None,
            "candidate_hits": candidate_hits,
            "candidate_denominator": candidates,
            "candidate_recall": round(candidate_hits / candidates, 4) if candidates else None,
            "baseline_chapter_hits": baseline_chapter_hits,
            "baseline_chapter_recall": (
                round(baseline_chapter_hits / chapters, 4) if chapters else None
            ),
            "baseline_candidate_hits": baseline_candidate_hits,
            "baseline_candidate_recall": (
                round(baseline_candidate_hits / candidates, 4) if candidates else None
            ),
            "learned_only_chapter_hits": learned_only_chapter_hits,
            "learned_only_candidate_hits": learned_only_candidate_hits,
            "combined_chapter_hits": combined_chapter_hits,
            "combined_chapter_recall": (
                round(combined_chapter_hits / chapters, 4) if chapters else None
            ),
            "combined_candidate_hits": combined_candidate_hits,
            "combined_candidate_recall": (
                round(combined_candidate_hits / candidates, 4) if candidates else None
            ),
            "reconciled_chapter_hits": reconciled_chapter_hits,
            "reconciled_chapter_recall": (
                round(reconciled_chapter_hits / chapters, 4) if chapters else None
            ),
            "reconciled_candidate_hits": reconciled_candidate_hits,
            "reconciled_candidate_recall": (
                round(reconciled_candidate_hits / candidates, 4) if candidates else None
            ),
        }
    return result


def _load_episode_artifacts(
    rows: Sequence[Mapping[str, Any]],
    *,
    cache_dir: Path | None,
    include_speech_rate: bool,
) -> tuple[dict[str, Artifact], list[dict[str, Any]]]:
    session = make_session()
    artifacts: dict[str, Artifact] = {}
    errors: list[dict[str, Any]] = []
    for row in rows:
        try:
            words, vtt, source = _artifact_bytes(session, row, cache_dir=cache_dir)
            units, unit_source = build_locator_units(words_data=words, vtt_data=vtt)
            if not units:
                errors.append({"uid": row.get("uid"), "error": "no_timed_units"})
                continue
            word_times = _word_timestamps(words) if include_speech_rate else None
            reference = (
                build_speech_rate_reference(
                    word_times,
                    episode_start=units[0].start,
                    episode_end=units[-1].end,
                )
                if word_times
                else SpeechRateReference(available=False)
            )
            artifacts[str(row.get("uid"))] = (
                row,
                units,
                unit_source or source,
                word_times,
                reference,
            )
        except Exception as exc:  # preserve explicit research diagnostics
            errors.append({"uid": row.get("uid"), "error": f"artifact_fetch_failed: {exc}"})
    return artifacts, errors


def _grouped_validation_indices(
    usable_rows: Sequence[tuple[Mapping[str, Any], Mapping[str, Any], list[LocatorUnit]]],
    validation_fraction: float,
) -> tuple[set[int], set[int]]:
    """Split body groups within each provider so validation retains provider coverage."""
    by_provider: dict[str, list[int]] = defaultdict(list)
    for index, (row, _crosswalk, _units) in enumerate(usable_rows):
        by_provider[str(row.get("provider") or "unknown")].append(index)
    train: set[int] = set()
    validation: set[int] = set()
    for _provider, indices in sorted(by_provider.items()):
        provider_groups = [
            f"{usable_rows[index][0].get('provider')}:{usable_rows[index][0].get('slug')}"
            for index in indices
        ]
        if len(set(provider_groups)) < 2:
            train.update(indices)
            continue
        splitter = GroupShuffleSplit(
            n_splits=1, test_size=validation_fraction, random_state=RANDOM_SEED
        )
        local_train, local_validation = next(
            splitter.split(np.zeros(len(indices)), groups=provider_groups)
        )
        train.update(indices[local_index] for local_index in local_train.tolist())
        validation.update(indices[local_index] for local_index in local_validation.tolist())
    return train, validation


def train_and_evaluate(
    manifest: Mapping[str, Any],
    crosswalk: Mapping[str, Any],
    *,
    agenda_model: str,
    split: str,
    validation_fraction: float,
    label_tolerance: float,
    score_tolerance: float,
    hard_top_k: int,
    neighbor_radius: int,
    random_negatives: int,
    pairwise_max_pairs: int,
    reconcile_candidate_count: int,
    candidate_pool_top_k: int,
    top_ks: Sequence[int],
    cache_dir: Path | None,
    speech_rate_mode: str = "none",
    speech_rate_smoothing_radius: int = SPEECH_RATE_SMOOTHING_RADIUS,
    transition_phrase_mode: str = "none",
    transition_phrase_window_seconds: float = 30.0,
    transition_phrase_min_positive_episodes: int = 2,
    transition_phrase_max_terms: int = 2000,
    transition_phrase_decay_seconds: float = 8.0,
    transition_phrase_post_boundary_weight: float = 0.35,
    exclude_uids: Sequence[str] = (),
    checkpoint_uids: Sequence[str] = (),
) -> dict[str, Any]:
    _validate_speech_rate_mode(speech_rate_mode)
    _validate_transition_phrase_mode(transition_phrase_mode)
    if speech_rate_smoothing_radius < 0:
        raise ValueError("speech_rate_smoothing_radius must be non-negative")
    excluded = {str(uid) for uid in exclude_uids}
    rows = [
        row
        for row in manifest.get("episodes", [])
        if row.get("split") == split and str(row.get("uid")) not in excluded
    ]
    crosswalk_by_uid = {row.get("uid"): row for row in crosswalk.get("episodes", [])}
    artifacts, errors = _load_episode_artifacts(
        rows,
        cache_dir=cache_dir,
        include_speech_rate=speech_rate_mode != "none",
    )
    checkpoint_set = {str(uid) for uid in checkpoint_uids}
    checkpoint_rows = [
        row for row in manifest.get("episodes", []) if str(row.get("uid")) in checkpoint_set
    ]
    checkpoint_artifacts, checkpoint_errors = _load_episode_artifacts(
        checkpoint_rows,
        cache_dir=cache_dir,
        include_speech_rate=speech_rate_mode != "none",
    )
    randomizer = random.Random(RANDOM_SEED)
    pairwise_randomizer = random.Random(RANDOM_SEED + 1)
    groups = []
    usable_rows: list[tuple[Mapping[str, Any], Mapping[str, Any], list[LocatorUnit]]] = []
    for row in rows:
        uid = row.get("uid")
        if uid not in artifacts or uid not in crosswalk_by_uid:
            continue
        _, units, _source, _word_times, _speech_rate_reference = artifacts[str(uid)]
        targets = _strong_targets(crosswalk_by_uid[uid])
        if not targets:
            continue
        usable_rows.append((row, crosswalk_by_uid[uid], units))
        groups.append(f"{row.get('provider')}:{row.get('slug')}")
    if len(set(groups)) < 2:
        raise ValueError("at least two grouped development bodies are required")
    train_set, validation_set = _grouped_validation_indices(usable_rows, validation_fraction)
    phrase_model = (
        fit_transition_phrase_model(
            [usable_rows[index] for index in sorted(train_set)],
            window_seconds=transition_phrase_window_seconds,
            min_positive_episodes=transition_phrase_min_positive_episodes,
            max_terms=transition_phrase_max_terms,
            decay_seconds=transition_phrase_decay_seconds,
            post_boundary_weight=transition_phrase_post_boundary_weight,
        )
        if transition_phrase_mode == "learned"
        else None
    )
    train_features: list[list[float]] = []
    train_labels: list[int] = []
    pairwise_features: list[list[float]] = []
    pairwise_labels: list[int] = []
    for index in sorted(train_set):
        row, crosswalk_row, units = usable_rows[index]
        _artifact_row, _artifact_units, _source, word_times, speech_rate_reference = artifacts[
            str(row.get("uid"))
        ]
        features, labels, identities, _targets = build_episode_features(
            row,
            crosswalk_row,
            units,
            model=agenda_model,
            label_tolerance=label_tolerance,
            hard_top_k=hard_top_k,
            neighbor_radius=neighbor_radius,
            random_negatives=random_negatives,
            randomizer=randomizer,
            speech_rate_mode=speech_rate_mode,
            word_times=word_times,
            speech_rate_reference=speech_rate_reference,
            speech_rate_smoothing_radius=speech_rate_smoothing_radius,
            transition_phrase_mode=transition_phrase_mode,
            transition_phrase_model=phrase_model,
        )
        train_features.extend(features)
        train_labels.extend(labels)
        episode_pair_features, episode_pair_labels = _pairwise_examples(
            features,
            labels,
            identities,
            max_pairs_per_item=pairwise_max_pairs,
            randomizer=pairwise_randomizer,
        )
        pairwise_features.extend(episode_pair_features)
        pairwise_labels.extend(episode_pair_labels)
    if not train_features or len(set(train_labels)) < 2:
        raise ValueError("training rows must contain both positive and negative labels")
    x_train = np.asarray(train_features, dtype=np.float32)
    y_train = np.asarray(train_labels, dtype=np.int8)
    classifiers: dict[str, Any] = {
        "logistic": LogisticRegression(
            class_weight="balanced", max_iter=1000, random_state=RANDOM_SEED
        ),
        "hist_gradient_boosting": HistGradientBoostingClassifier(
            learning_rate=0.08,
            max_iter=180,
            max_leaf_nodes=15,
            l2_regularization=1.0,
            random_state=RANDOM_SEED,
        ),
    }
    weights = np.where(
        y_train == 1, 1.0, float(np.sum(y_train == 1)) / max(1, np.sum(y_train == 0))
    )
    classifiers["logistic"].fit(x_train, y_train)
    classifiers["hist_gradient_boosting"].fit(x_train, y_train, sample_weight=weights)
    if pairwise_features and len(set(pairwise_labels)) == 2:
        pair_x = np.asarray(pairwise_features, dtype=np.float32)
        pair_y = np.asarray(pairwise_labels, dtype=np.int8)
        classifiers["pairwise_logistic"] = LogisticRegression(
            class_weight="balanced",
            fit_intercept=False,
            max_iter=1000,
            random_state=RANDOM_SEED,
        )
        classifiers["pairwise_logistic"].fit(pair_x, pair_y)

    validation_labels: list[int] = []
    validation_scores: dict[str, list[float]] = {name: [] for name in classifiers}
    for index in sorted(validation_set):
        row, crosswalk_row, units = usable_rows[index]
        _artifact_row, _artifact_units, _source, word_times, speech_rate_reference = artifacts[
            str(row.get("uid"))
        ]
        # Gather a separately sampled validation classification set for calibration diagnostics.
        features, labels, identities, _targets = build_episode_features(
            row,
            crosswalk_row,
            units,
            model=agenda_model,
            label_tolerance=label_tolerance,
            hard_top_k=hard_top_k,
            neighbor_radius=neighbor_radius,
            random_negatives=random_negatives,
            randomizer=randomizer,
            speech_rate_mode=speech_rate_mode,
            word_times=word_times,
            speech_rate_reference=speech_rate_reference,
            speech_rate_smoothing_radius=speech_rate_smoothing_radius,
            transition_phrase_mode=transition_phrase_mode,
            transition_phrase_model=phrase_model,
        )
        validation_labels.extend(labels)
        for name, classifier in classifiers.items():
            validation_scores[name].extend(classifier.predict_proba(np.asarray(features))[:, 1])

    summary: dict[str, Any] = {}
    for name in classifiers:
        scored_rows: list[dict[str, Any]] = []
        for index in sorted(validation_set):
            row, crosswalk_row, units = usable_rows[index]
            _artifact_row, _artifact_units, _source, word_times, speech_rate_reference = artifacts[
                str(row.get("uid"))
            ]
            scored_rows.append(
                _score_validation_episode(
                    row,
                    crosswalk_row,
                    units,
                    classifiers,
                    model_name=name,
                    agenda_model=agenda_model,
                    score_tolerance=score_tolerance,
                    top_ks=top_ks,
                    candidate_pool_top_k=candidate_pool_top_k,
                    neighbor_radius=neighbor_radius,
                    reconcile_candidate_count=reconcile_candidate_count,
                    speech_rate_mode=speech_rate_mode,
                    word_times=word_times,
                    speech_rate_reference=speech_rate_reference,
                    speech_rate_smoothing_radius=speech_rate_smoothing_radius,
                    transition_phrase_mode=transition_phrase_mode,
                    transition_phrase_model=phrase_model,
                )
            )
        scores = np.asarray(validation_scores[name], dtype=np.float64)
        labels = np.asarray(validation_labels, dtype=np.int8)
        summary[name] = {
            "rows": len(scored_rows),
            "metrics": _aggregate(scored_rows, top_ks),
            "validation_episode_details": scored_rows,
            "validation_average_precision": round(
                float(average_precision_score(labels, scores)), 6
            ),
            "validation_brier_score": round(float(brier_score_loss(labels, scores)), 6),
            "checkpoint_episode_details": [],
        }
    for name in classifiers:
        for row in checkpoint_rows:
            uid = str(row.get("uid"))
            crosswalk_row = crosswalk_by_uid.get(row.get("uid"))
            artifact = checkpoint_artifacts.get(uid)
            if crosswalk_row is None or artifact is None:
                continue
            _artifact_row, units, _source, word_times, speech_rate_reference = artifact
            summary[name]["checkpoint_episode_details"].append(
                _score_validation_episode(
                    row,
                    crosswalk_row,
                    units,
                    classifiers,
                    model_name=name,
                    agenda_model=agenda_model,
                    score_tolerance=score_tolerance,
                    top_ks=top_ks,
                    candidate_pool_top_k=candidate_pool_top_k,
                    neighbor_radius=neighbor_radius,
                    reconcile_candidate_count=reconcile_candidate_count,
                    speech_rate_mode=speech_rate_mode,
                    word_times=word_times,
                    speech_rate_reference=speech_rate_reference,
                    speech_rate_smoothing_radius=speech_rate_smoothing_radius,
                    transition_phrase_mode=transition_phrase_mode,
                    transition_phrase_model=phrase_model,
                    score_all_items=True,
                )
            )
    return {
        "version": 3,
        "purpose": "read-only supervised agenda-to-transcript transition scoring benchmark",
        "agenda_model": agenda_model,
        "split": split,
        "label_tolerance_seconds": label_tolerance,
        "score_tolerance_seconds": score_tolerance,
        "hard_top_k": hard_top_k,
        "neighbor_radius": neighbor_radius,
        "reconcile_candidate_count": reconcile_candidate_count,
        "random_negatives_per_item": random_negatives,
        "pairwise_max_pairs_per_item": pairwise_max_pairs,
        "candidate_pool_top_k": candidate_pool_top_k,
        "speech_rate_mode": speech_rate_mode,
        "speech_rate_bin_seconds": SPEECH_RATE_BIN_SECONDS,
        "speech_rate_offsets_seconds": [*SPEECH_RATE_OFFSETS],
        "speech_rate_smoothing_radius": speech_rate_smoothing_radius,
        "transition_phrase_mode": transition_phrase_mode,
        "transition_phrase_window_seconds": transition_phrase_window_seconds,
        "transition_phrase_min_positive_episodes": transition_phrase_min_positive_episodes,
        "transition_phrase_max_terms": transition_phrase_max_terms,
        "transition_phrase_decay_seconds": transition_phrase_decay_seconds,
        "transition_phrase_post_boundary_weight": transition_phrase_post_boundary_weight,
        "feature_names": list(feature_names_for_mode(speech_rate_mode, transition_phrase_mode)),
        "feature_inputs": (
            "agenda evidence plus timed transcript text, local temporal/cue features, "
            "adjacent-unit token novelty, optional word-timed speech-rate shape, and optional "
            "training-fold transition-word/phrase log odds"
        ),
        "label_provenance": "strong_provider_chapter_crosswalk_development_only",
        "episodes": {
            "usable": len(usable_rows),
            "train": len(train_set),
            "validation": len(validation_set),
        },
        "groups": {
            "train": sorted({groups[index] for index in train_set}),
            "validation": sorted({groups[index] for index in validation_set}),
        },
        "training_examples": {
            "rows": len(train_labels),
            "positive": int(np.sum(y_train)),
            "pairwise_rows": len(pairwise_labels),
        },
        "artifact_errors": errors,
        "excluded_uids": sorted(excluded),
        "checkpoint_uids": sorted(checkpoint_set),
        "checkpoint_artifact_errors": checkpoint_errors,
        "transition_phrase_model": {
            "available": bool(phrase_model and phrase_model.available),
            "positive_documents": phrase_model.positive_documents if phrase_model else 0,
            "background_documents": phrase_model.background_documents if phrase_model else 0,
            "positive_episodes": phrase_model.positive_episodes if phrase_model else 0,
            "positive_terms": phrase_model.positive_terms if phrase_model else 0,
            "top_positive_phrases": (
                [
                    {"phrase": phrase, "weight": round(weight, 6)}
                    for phrase, weight in phrase_model.top_positive_phrases
                ]
                if phrase_model
                else []
            ),
        },
        "models": summary,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--crosswalk", type=Path, required=True)
    parser.add_argument("--write", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--split", default="development")
    parser.add_argument("--validation-fraction", type=float, default=0.25)
    parser.add_argument("--label-tolerance", type=float, default=30.0)
    parser.add_argument("--score-tolerance", type=float, default=60.0)
    parser.add_argument("--hard-top-k", type=int, default=25)
    parser.add_argument("--neighbor-radius", type=int, default=2)
    parser.add_argument(
        "--reconcile-candidate-count",
        type=int,
        default=50,
        help="distinct-unit assignment pool per item; 0 uses every unit (default: 50)",
    )
    parser.add_argument("--random-negatives", type=int, default=25)
    parser.add_argument(
        "--pairwise-max-pairs",
        type=int,
        default=50,
        help="maximum positive/negative comparisons per agenda item (default: 50)",
    )
    parser.add_argument(
        "--candidate-pool-top-k",
        type=int,
        default=0,
        help="union pool size before learned reranking; 0 scores all units (default: 0)",
    )
    parser.add_argument(
        "--speech-rate-mode",
        choices=SPEECH_RATE_MODES,
        default="none",
        help="optional word-timed rate vector family: vector, derivative, both, or none",
    )
    parser.add_argument(
        "--speech-rate-smoothing-radius",
        type=int,
        default=SPEECH_RATE_SMOOTHING_RADIUS,
        help="neighbor bins on each side for the moving-average smoother (default: 2)",
    )
    parser.add_argument(
        "--transition-phrase-mode",
        choices=TRANSITION_PHRASE_MODES,
        default="none",
        help="optional training-fold word/phrase cue features (default: none)",
    )
    parser.add_argument(
        "--transition-phrase-window-seconds",
        type=float,
        default=30.0,
        help="positive context around each provider start for phrase learning (default: 30)",
    )
    parser.add_argument(
        "--transition-phrase-min-positive-episodes",
        type=int,
        default=2,
        help="minimum training episodes containing a phrase near a boundary (default: 2)",
    )
    parser.add_argument(
        "--transition-phrase-max-terms",
        type=int,
        default=2000,
        help="maximum learned word/phrase terms retained by absolute log odds (default: 2000)",
    )
    parser.add_argument(
        "--transition-phrase-decay-seconds",
        type=float,
        default=8.0,
        help="distance decay constant for boundary phrase evidence (default: 8)",
    )
    parser.add_argument(
        "--transition-phrase-post-boundary-weight",
        type=float,
        default=0.35,
        help="relative weight for terms after the boundary (default: 0.35)",
    )
    parser.add_argument(
        "--exclude-uid",
        action="append",
        default=[],
        help="episode UID excluded before fitting and validation; may be repeated",
    )
    parser.add_argument(
        "--checkpoint-uid",
        action="append",
        default=[],
        help="episode UID scored after fitting but excluded from fitting/validation; may repeat",
    )
    parser.add_argument("--top-k", type=int, nargs="+", default=[1, 3, 5, 10])
    args = parser.parse_args(argv)
    if not 0 < args.validation_fraction < 1:
        parser.error("--validation-fraction must be between 0 and 1")
    if args.speech_rate_smoothing_radius < 0:
        parser.error("--speech-rate-smoothing-radius must be non-negative")
    if args.transition_phrase_window_seconds <= 0:
        parser.error("--transition-phrase-window-seconds must be positive")
    if args.transition_phrase_min_positive_episodes < 1:
        parser.error("--transition-phrase-min-positive-episodes must be positive")
    if args.transition_phrase_max_terms < 1:
        parser.error("--transition-phrase-max-terms must be positive")
    if args.transition_phrase_decay_seconds <= 0:
        parser.error("--transition-phrase-decay-seconds must be positive")
    if not 0 < args.transition_phrase_post_boundary_weight <= 1:
        parser.error("--transition-phrase-post-boundary-weight must be in (0, 1]")
    result = train_and_evaluate(
        json.loads(args.manifest.read_text(encoding="utf-8")),
        json.loads(args.crosswalk.read_text(encoding="utf-8")),
        agenda_model=args.model,
        split=args.split,
        validation_fraction=args.validation_fraction,
        label_tolerance=args.label_tolerance,
        score_tolerance=args.score_tolerance,
        hard_top_k=args.hard_top_k,
        neighbor_radius=args.neighbor_radius,
        reconcile_candidate_count=args.reconcile_candidate_count,
        random_negatives=args.random_negatives,
        pairwise_max_pairs=args.pairwise_max_pairs,
        candidate_pool_top_k=args.candidate_pool_top_k,
        top_ks=tuple(args.top_k),
        cache_dir=args.cache_dir,
        speech_rate_mode=args.speech_rate_mode,
        speech_rate_smoothing_radius=args.speech_rate_smoothing_radius,
        transition_phrase_mode=args.transition_phrase_mode,
        transition_phrase_window_seconds=args.transition_phrase_window_seconds,
        transition_phrase_min_positive_episodes=args.transition_phrase_min_positive_episodes,
        transition_phrase_max_terms=args.transition_phrase_max_terms,
        transition_phrase_decay_seconds=args.transition_phrase_decay_seconds,
        transition_phrase_post_boundary_weight=args.transition_phrase_post_boundary_weight,
        exclude_uids=args.exclude_uid,
        checkpoint_uids=args.checkpoint_uid,
    )
    args.write.parent.mkdir(parents=True, exist_ok=True)
    args.write.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(result["models"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
