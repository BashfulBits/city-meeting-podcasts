#!/usr/bin/env python
"""Evaluate deterministic transcript-window retrieval for GH#1078.

This research-only command compares compact lexical, TF-IDF similarity, and high-recall-union
candidate windows against provider chapter starts. Provider chapters are read only for scoring;
they are never included in the candidate request. No model is called and no episode state is
changed. The TF-IDF path is intentionally a lightweight embedding proxy, not a claim that it
replaces a semantic embedding model.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from citypods.chapter_locator import (
    LocatorAgendaItem,
    LocatorUnit,
    build_locator_request,
    build_locator_units,
)
from citypods.http import make_session

try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
except ImportError as exc:  # pragma: no cover - optional research extra
    raise SystemExit(
        "install the chapter-research extra to run locator retrieval evaluation"
    ) from exc

DEFAULT_MODEL = "mistral/mistral-medium-2508"
DEFAULT_TOP_K = (1, 3, 5, 10)
_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_IDENTIFIER_RE = re.compile(
    r"(?<![a-z0-9])(?:[a-z]{1,8}\d+[a-z0-9]*(?:[./-][a-z0-9]+)*|\d+[./-][a-z0-9]+)(?![a-z0-9])",
    re.IGNORECASE,
)
_STOPWORDS = frozenset(
    "a an and are at be by for from in into is it of on or that the this to with".split()
)


def _tokens(text: str) -> list[str]:
    return [token for token in _TOKEN_RE.findall(text.casefold()) if token not in _STOPWORDS]


def _identifier_tokens(text: str) -> set[str]:
    return {match.group(0).casefold() for match in _IDENTIFIER_RE.finditer(text)}


def _query_text(item: Mapping[str, Any]) -> str:
    cues = item.get("locator_cues") or []
    return " ".join(
        str(value)
        for value in [item.get("title"), item.get("evidence_text"), item.get("display_ref"), *cues]
        if value
    )


def lexical_scores(item: Mapping[str, Any], units: Sequence[LocatorUnit]) -> list[float]:
    """Score units by rare agenda tokens, with formal identifiers weighted more heavily."""
    if not units:
        return []
    unit_tokens = [_tokens(unit.text) for unit in units]
    document_frequency = Counter(token for tokens in unit_tokens for token in set(tokens))
    query_tokens = set(_tokens(_query_text(item)))
    identifiers = _identifier_tokens(_query_text(item))
    if not query_tokens:
        return [0.0] * len(units)
    query_weights: dict[str, float] = {}
    for token in query_tokens:
        # Common meeting boilerplate contributes little; rare terms remain useful even when the
        # transcript uses a short announcement rather than the full agenda wording.
        idf = math.log((len(units) + 1) / (document_frequency[token] + 1)) + 1.0
        query_weights[token] = idf * (3.0 if token in identifiers else 1.0)
    denominator = sum(query_weights.values())
    return [
        sum(query_weights[token] for token in set(tokens) if token in query_weights) / denominator
        for tokens in unit_tokens
    ]


def tfidf_score_rows(
    items: Sequence[Mapping[str, Any]], units: Sequence[LocatorUnit]
) -> list[list[float]]:
    """Return TF-IDF cosine rows, fitting one episode-level vectorizer for efficiency."""
    if not units or not items:
        return [[] for _ in items]
    vectorizer = TfidfVectorizer(
        lowercase=True,
        ngram_range=(1, 2),
        token_pattern=r"(?u)\b\w[\w./-]*\b",
        sublinear_tf=True,
    )
    unit_matrix = vectorizer.fit_transform([unit.text for unit in units])
    item_matrix = vectorizer.transform([_query_text(item) for item in items])
    return cosine_similarity(item_matrix, unit_matrix).tolist()


def ranked_unit_indices(scores: Sequence[float], *, top_k: int) -> list[int]:
    """Return deterministic top-k unit indices, retaining ties by chronology."""
    ordered = sorted(enumerate(scores), key=lambda pair: (-pair[1], pair[0]))
    return [index for index, _ in ordered[:top_k]]


def union_ranked_indices(
    lexical: Sequence[float], tfidf: Sequence[float], *, top_k: int, neighbor_radius: int = 1
) -> list[int]:
    """Union both top-k lists and their adjacent timed units, sorted chronologically."""
    selected = set(ranked_unit_indices(lexical, top_k=top_k))
    selected.update(ranked_unit_indices(tfidf, top_k=top_k))
    for index in list(selected):
        selected.update(
            range(
                max(0, index - neighbor_radius),
                min(len(lexical), index + neighbor_radius + 1),
            )
        )
    return sorted(selected)


def _ranked_indices_for_method(
    method: str,
    item_index: int,
    top_k: int,
    neighbor_radius: int,
    lexical_by_item: Sequence[Sequence[float]],
    tfidf_by_item: Sequence[Sequence[float]],
) -> list[int]:
    if method == "lexical":
        return ranked_unit_indices(lexical_by_item[item_index], top_k=top_k)
    if method == "tfidf":
        return ranked_unit_indices(tfidf_by_item[item_index], top_k=top_k)
    return union_ranked_indices(
        lexical_by_item[item_index],
        tfidf_by_item[item_index],
        top_k=top_k,
        neighbor_radius=neighbor_radius,
    )


def _unit_hits(
    units: Sequence[LocatorUnit], indices: Iterable[int], start: float, tolerance: float
) -> bool:
    return any(abs(units[index].start - start) <= tolerance for index in indices)


def _strong_targets(crosswalk_row: Mapping[str, Any]) -> list[tuple[int, int, float]]:
    targets: list[tuple[int, int, float]] = []
    for chapter_index, chapter in enumerate(crosswalk_row.get("provider_chapters", [])):
        item_index = chapter.get("best_generated_item_index")
        start = chapter.get("start")
        if (
            chapter.get("status") == "strong"
            and isinstance(item_index, int)
            and isinstance(start, (int, float))
        ):
            targets.append((chapter_index, item_index, float(start)))
    return targets


def _targets_by_item(targets: Sequence[tuple[int, int, float]]) -> dict[int, list[float]]:
    """Group strong provider starts by generated item for candidate-side scoring."""
    grouped: dict[int, list[float]] = defaultdict(list)
    for _chapter_index, item_index, start in targets:
        grouped[item_index].append(start)
    return grouped


def score_episode(
    *,
    manifest_row: Mapping[str, Any],
    crosswalk_row: Mapping[str, Any],
    units: Sequence[LocatorUnit],
    top_ks: Sequence[int],
    tolerance: float,
    model: str,
    neighbor_radius: int = 1,
    baseline_crosswalk_row: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if neighbor_radius < 0:
        raise ValueError("neighbor_radius must be non-negative")
    generated = (manifest_row.get("generated_agenda") or {}).get(model) or {}
    items = generated.get("items") or []
    item_rows = [
        LocatorAgendaItem(
            index=index,
            title=str(item.get("title") or ""),
            evidence_text=str(item.get("evidence_text") or ""),
            display_ref=item.get("display_ref"),
            locator_cues=tuple(
                cue
                for cue in (item.get("title"), item.get("evidence_text"), item.get("display_ref"))
                if cue
            ),
        )
        for index, item in enumerate(items)
    ]
    lexical_by_item = [lexical_scores(item, units) for item in items]
    tfidf_by_item = tfidf_score_rows(items, units)
    all_targets = _strong_targets(crosswalk_row)
    baseline_targets = (
        _strong_targets(baseline_crosswalk_row)
        if baseline_crosswalk_row is not None
        else all_targets
    )
    all_targets_by_item = _targets_by_item(all_targets)
    baseline_targets_by_item = _targets_by_item(baseline_targets)
    hits_by_k: dict[str, dict[str, int]] = defaultdict(dict)
    baseline_hits_by_k: dict[str, dict[str, int]] = defaultdict(dict)
    candidate_hits_by_k: dict[str, dict[str, int]] = defaultdict(dict)
    baseline_candidate_hits_by_k: dict[str, dict[str, int]] = defaultdict(dict)
    for top_k in top_ks:
        for method in ("lexical", "tfidf", "union"):
            hits = 0
            baseline_hits = 0
            candidate_hits = 0
            baseline_candidate_hits = 0

            ranked_by_item = {
                item_index: _ranked_indices_for_method(
                    method,
                    item_index,
                    top_k,
                    neighbor_radius,
                    lexical_by_item,
                    tfidf_by_item,
                )
                for item_index in set(all_targets_by_item) | set(baseline_targets_by_item)
                if item_index < len(items)
            }
            for _chapter_index, item_index, start in all_targets:
                if item_index >= len(items):
                    continue
                indices = ranked_by_item[item_index]
                hit = _unit_hits(units, indices, start, tolerance)
                hits += hit
            for item_index, starts in all_targets_by_item.items():
                if item_index >= len(items):
                    continue
                candidate_hits += any(
                    _unit_hits(units, ranked_by_item[item_index], start, tolerance)
                    for start in starts
                )
            for _, item_index, start in baseline_targets:
                if item_index >= len(items):
                    continue
                baseline_hits += _unit_hits(units, ranked_by_item[item_index], start, tolerance)
            for item_index, starts in baseline_targets_by_item.items():
                if item_index >= len(items):
                    continue
                baseline_candidate_hits += any(
                    _unit_hits(units, ranked_by_item[item_index], start, tolerance)
                    for start in starts
                )
            hits_by_k[str(top_k)][method] = hits
            baseline_hits_by_k[str(top_k)][method] = baseline_hits
            candidate_hits_by_k[str(top_k)][method] = candidate_hits
            baseline_candidate_hits_by_k[str(top_k)][method] = baseline_candidate_hits
    full_context_tokens = None
    full_context_model = None
    full_context_error = None
    if item_rows and units:
        try:
            request = build_locator_request(item_rows, units)
            full_context_tokens = request.input_tokens
            full_context_model = request.model
        except ValueError as exc:
            full_context_error = str(exc)
    return {
        "uid": manifest_row.get("uid"),
        "provider": manifest_row.get("provider"),
        "slug": manifest_row.get("slug"),
        "split": manifest_row.get("split"),
        "unit_count": len(units),
        "agenda_item_count": len(items),
        "covered_provider_chapters": len(all_targets),
        "baseline_covered_provider_chapters": len(baseline_targets),
        "covered_generated_candidates": len(all_targets_by_item),
        "baseline_covered_generated_candidates": len(baseline_targets_by_item),
        "newly_covered_provider_chapters": len(all_targets) - len(baseline_targets),
        "newly_covered_generated_candidates": len(all_targets_by_item)
        - len(baseline_targets_by_item),
        "tolerance_seconds": tolerance,
        "neighbor_radius": neighbor_radius,
        "hits": hits_by_k,
        "baseline_hits": baseline_hits_by_k,
        "candidate_hits": candidate_hits_by_k,
        "baseline_candidate_hits": baseline_candidate_hits_by_k,
        "full_context_input_tokens": full_context_tokens,
        "full_context_model": full_context_model,
        "full_context_error": full_context_error,
    }


def _fetch(session, url: str | None) -> bytes | None:
    if not url:
        return None
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return response.content


def evaluate(
    manifest: Mapping[str, Any],
    crosswalk: Mapping[str, Any],
    *,
    model: str,
    split: str | None,
    limit_per_provider: int | None,
    top_ks: Sequence[int],
    tolerance: float,
    neighbor_radius: int,
    baseline_crosswalk: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    manifest_rows = [
        row for row in manifest.get("episodes", []) if split is None or row.get("split") == split
    ]
    by_uid = {row.get("uid"): row for row in crosswalk.get("episodes", [])}
    baseline_by_uid = (
        {row.get("uid"): row for row in baseline_crosswalk.get("episodes", [])}
        if baseline_crosswalk is not None
        else {}
    )
    selected: dict[str, int] = defaultdict(int)
    session = make_session()
    rows: list[dict[str, Any]] = []
    for row in sorted(
        manifest_rows,
        key=lambda value: (str(value.get("provider")), str(value.get("uid"))),
    ):
        provider = str(row.get("provider") or "unknown")
        if limit_per_provider is not None and selected[provider] >= limit_per_provider:
            continue
        crosswalk_row = by_uid.get(row.get("uid"))
        if crosswalk_row is None:
            continue
        try:
            transcript = _fetch(session, (row.get("transcript") or {}).get("url"))
            words = _fetch(session, (row.get("transcript") or {}).get("words_url"))
        except Exception as exc:  # retain source failures as explicit research diagnostics
            rows.append(
                {
                    "uid": row.get("uid"),
                    "provider": provider,
                    "error": f"artifact_fetch_failed: {exc}",
                }
            )
            selected[provider] += 1
            continue
        units, unit_source = build_locator_units(words_data=words, vtt_data=transcript)
        if not units:
            rows.append({"uid": row.get("uid"), "provider": provider, "error": "no_timed_units"})
            selected[provider] += 1
            continue
        result = score_episode(
            manifest_row=row,
            crosswalk_row=crosswalk_row,
            units=units,
            top_ks=top_ks,
            tolerance=tolerance,
            model=model,
            neighbor_radius=neighbor_radius,
            baseline_crosswalk_row=baseline_by_uid.get(row.get("uid")),
        )
        result["unit_source"] = unit_source
        rows.append(result)
        selected[provider] += 1
    summary: dict[str, Any] = {"rows": len(rows), "by_provider": {}}
    for provider in sorted({row.get("provider") for row in rows}):
        provider_rows = [row for row in rows if row.get("provider") == provider and "hits" in row]
        denominator = sum(int(row["covered_provider_chapters"]) for row in provider_rows)
        baseline_denominator = sum(
            int(row["baseline_covered_provider_chapters"]) for row in provider_rows
        )
        candidate_denominator = sum(
            int(row["covered_generated_candidates"]) for row in provider_rows
        )
        baseline_candidate_denominator = sum(
            int(row["baseline_covered_generated_candidates"]) for row in provider_rows
        )
        by_k: dict[str, Any] = {}
        for top_k in top_ks:
            by_k[str(top_k)] = {}
            for method in ("lexical", "tfidf", "union"):
                hits = sum(int(row["hits"][str(top_k)][method]) for row in provider_rows)
                baseline_hits = sum(
                    int(row["baseline_hits"][str(top_k)][method]) for row in provider_rows
                )
                candidate_hits = sum(
                    int(row["candidate_hits"][str(top_k)][method]) for row in provider_rows
                )
                baseline_candidate_hits = sum(
                    int(row["baseline_candidate_hits"][str(top_k)][method]) for row in provider_rows
                )
                by_k[str(top_k)][method] = {
                    "hits": hits,
                    "denominator": denominator,
                    "recall": round(hits / denominator, 4) if denominator else None,
                    "baseline_hits": baseline_hits,
                    "baseline_denominator": baseline_denominator,
                    "baseline_recall": (
                        round(baseline_hits / baseline_denominator, 4)
                        if baseline_denominator
                        else None
                    ),
                    "candidate_hits": candidate_hits,
                    "candidate_denominator": candidate_denominator,
                    "candidate_recall": (
                        round(candidate_hits / candidate_denominator, 4)
                        if candidate_denominator
                        else None
                    ),
                    "baseline_candidate_hits": baseline_candidate_hits,
                    "baseline_candidate_denominator": baseline_candidate_denominator,
                    "baseline_candidate_recall": (
                        round(baseline_candidate_hits / baseline_candidate_denominator, 4)
                        if baseline_candidate_denominator
                        else None
                    ),
                }
        summary["by_provider"][provider] = {"rows": len(provider_rows), "by_top_k": by_k}
    return {
        "version": 1,
        "purpose": "scoring-only deterministic locator retrieval comparison",
        "model": model,
        "split": split,
        "top_ks": list(top_ks),
        "tolerance_seconds": tolerance,
        "neighbor_radius": neighbor_radius,
        "summary": summary,
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--crosswalk", type=Path, required=True)
    parser.add_argument(
        "--baseline-crosswalk",
        type=Path,
        help="optional strict crosswalk for paired recall on the same provider chapters",
    )
    parser.add_argument("--write", type=Path, required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--split", default="development")
    parser.add_argument("--limit-per-provider", type=int)
    parser.add_argument("--tolerance", type=float, default=60.0)
    parser.add_argument(
        "--neighbor-radius",
        type=int,
        default=1,
        help="adjacent timed units added around each union hit (default: 1)",
    )
    parser.add_argument("--top-k", type=int, nargs="+", default=list(DEFAULT_TOP_K))
    args = parser.parse_args(argv)
    result = evaluate(
        json.loads(args.manifest.read_text(encoding="utf-8")),
        json.loads(args.crosswalk.read_text(encoding="utf-8")),
        model=args.model,
        split=args.split,
        limit_per_provider=args.limit_per_provider,
        top_ks=tuple(args.top_k),
        tolerance=args.tolerance,
        neighbor_radius=args.neighbor_radius,
        baseline_crosswalk=(
            json.loads(args.baseline_crosswalk.read_text(encoding="utf-8"))
            if args.baseline_crosswalk
            else None
        ),
    )
    args.write.parent.mkdir(parents=True, exist_ok=True)
    args.write.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(result["summary"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
