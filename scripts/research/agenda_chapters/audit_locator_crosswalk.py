#!/usr/bin/env python
"""Audit the scoring-only join between generated agenda items and provider chapters.

This is deliberately not a retrieval input builder.  It reads the hidden provider chapter section
and the final generated agenda output together, producing an auditable set of likely, ambiguous,
and unmatched relationships.  The result helps establish which provider starts are scoreable
before lexical, embedding, or hybrid transcript retrieval is implemented.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from citypods.agenda_text import extract_agenda_title_candidates

DATASET_VERSION = 1
DEFAULT_MODEL = "mistral/mistral-medium-2508"
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_IDENTIFIER_RE = re.compile(
    r"(?<![a-z0-9])(?:[a-z]{1,5}\d{1,4}(?:[-./][a-z0-9]+)+|\d{1,4}[-/]\d{1,4}(?:[-./][a-z0-9]+)*)(?![a-z0-9])",
    re.I,
)
_ROLE_HINTS = (
    (
        "section_or_procedural",
        re.compile(
            r"\b(call to order|invocation|pledge|public comment|public communications|"
            r"consent agenda|consent calendar|executive session|work session|"
            r"special presentations?|recognitions?|announcements?|adjournment)\b",
            re.I,
        ),
    ),
)
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "at",
        "by",
        "for",
        "from",
        "in",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
    }
)


def _normalize(value: str) -> str:
    return " ".join(_TOKEN_RE.findall(value.casefold()))


def _tokens(value: str) -> set[str]:
    return {token for token in _TOKEN_RE.findall(value.casefold()) if token not in _STOPWORDS}


def _identifiers(value: str) -> set[str]:
    return {match.group(0).casefold() for match in _IDENTIFIER_RE.finditer(value)}


def _compact(value: str) -> str:
    return "".join(_TOKEN_RE.findall(value.casefold()))


def _display_ref_hit(display_ref: object, chapter_title: str) -> bool:
    if not isinstance(display_ref, str) or not display_ref.strip():
        return False
    reference = display_ref.casefold().strip()
    if not reference:
        return False
    # A compact ``i`` or ``1`` search would falsely match ordinary words such as ``insurance``.
    # Preserve punctuation and allow only layout whitespace around dotted hierarchical refs.
    escaped = re.escape(reference).replace(r"\.", r"\s*\.\s*")
    return bool(
        re.search(
            rf"(?<![a-z0-9]){escaped}(?![a-z0-9])",
            chapter_title.casefold(),
        )
    )


def _role_hint(title: str) -> str | None:
    for name, pattern in _ROLE_HINTS:
        if pattern.search(title):
            return name
    return None


def _containment(chapter_tokens: set[str], candidate_tokens: set[str]) -> float:
    if not chapter_tokens or not candidate_tokens:
        return 0.0
    return len(chapter_tokens & candidate_tokens) / len(chapter_tokens)


def _pair_features(chapter_title: str, item: dict[str, Any]) -> dict[str, Any]:
    chapter_norm = _normalize(chapter_title)
    title = str(item.get("title") or "")
    evidence = str(item.get("evidence_text") or "")
    title_norm = _normalize(title)
    evidence_norm = _normalize(evidence)
    chapter_tokens = _tokens(chapter_title)
    title_tokens = _tokens(title)
    evidence_tokens = _tokens(evidence)
    title_ratio = SequenceMatcher(a=chapter_norm, b=title_norm).ratio()
    evidence_ratio = SequenceMatcher(a=chapter_norm, b=evidence_norm).ratio()
    containment = max(
        _containment(chapter_tokens, title_tokens),
        _containment(chapter_tokens, evidence_tokens),
    )
    identifier_overlap = sorted(_identifiers(chapter_title) & _identifiers(f"{title} {evidence}"))
    display_ref_hit = _display_ref_hit(item.get("display_ref"), chapter_title)
    substring = bool(
        chapter_norm
        and ((chapter_norm in title_norm) or (chapter_norm in evidence_norm))
        or bool(title_norm and title_norm in chapter_norm)
    )
    score = max(title_ratio, evidence_ratio, containment)
    if substring:
        score = 1.0
    if identifier_overlap:
        score = max(score, 0.90)
    if display_ref_hit:
        score = max(score, min(1.0, score + 0.05))
    return {
        "score": round(score, 4),
        "title_ratio": round(title_ratio, 4),
        "evidence_ratio": round(evidence_ratio, 4),
        "token_containment": round(containment, 4),
        "identifier_overlap": identifier_overlap,
        "display_ref_hit": display_ref_hit,
        "substring": substring,
    }


def _chapter_status(top: dict[str, Any] | None, second: dict[str, Any] | None) -> str:
    if top is None or top["score"] < 0.60:
        return "unmatched"
    if second is not None and second["score"] >= 0.60 and top["score"] - second["score"] < 0.08:
        return "ambiguous"
    if top["score"] >= 0.82 or top["identifier_overlap"] or top["substring"]:
        return "strong"
    return "possible"


def _item_status(
    best: dict[str, Any] | None,
    *,
    best_chapter_owner: int | None,
    item_index: int,
    chapter_ambiguous: bool,
) -> str:
    if best is None or best["score"] < 0.60:
        return "unmapped"
    if best_chapter_owner != item_index or chapter_ambiguous:
        return "conflicted"
    return "mapped"


def _source_candidates(agenda_text: str) -> list[dict[str, Any]]:
    return [
        {"title": candidate.title, "line_number": candidate.line_number}
        for candidate in extract_agenda_title_candidates(agenda_text)
    ]


def _source_match(chapter_title: str, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    scored = [
        (_pair_features(chapter_title, candidate), index)
        for index, candidate in enumerate(candidates)
    ]
    scored.sort(
        key=lambda pair: (
            pair[0]["score"],
            pair[0]["identifier_overlap"] != [],
            pair[0]["substring"],
            -pair[1],
        )
    )
    if not scored:
        return None
    features, index = scored[-1]
    candidate = candidates[index]
    status = "unmatched"
    if features["score"] >= 0.82 or features["identifier_overlap"] or features["substring"]:
        status = "strong"
    elif features["score"] >= 0.60:
        status = "possible"
    return {
        "status": status,
        "agenda_candidate_index": index,
        "agenda_line_number": candidate["line_number"],
        "agenda_title": candidate["title"],
        **features,
    }


def _episode_crosswalk(
    row: dict[str, Any],
    gold: dict[str, Any],
    model: str,
    *,
    agenda_text: str | None = None,
) -> dict[str, Any]:
    generated = row.get("generated_agenda", {}).get(model, {})
    items = generated.get("items", []) if isinstance(generated, dict) else []
    chapters = gold.get("chapters", [])
    source_candidates = _source_candidates(agenda_text) if agenda_text is not None else None
    pair_rows: list[tuple[int, int, dict[str, Any]]] = []
    for chapter_index, chapter in enumerate(chapters):
        chapter_title = str(chapter.get("title") or "")
        for item_index, item in enumerate(items):
            features = _pair_features(chapter_title, item)
            pair_rows.append((chapter_index, item_index, features))

    chapter_matches: list[dict[str, Any]] = []
    chapter_candidate_counts: Counter[int] = Counter()
    for chapter_index, chapter in enumerate(chapters):
        candidates = sorted(
            (
                (features, item_index)
                for current_chapter, item_index, features in pair_rows
                if current_chapter == chapter_index
            ),
            key=lambda pair: (pair[0]["score"], pair[0]["identifier_overlap"] != [], pair[1]),
            reverse=True,
        )
        top = candidates[0] if candidates else None
        second = candidates[1] if len(candidates) > 1 else None
        top_features, top_item_index = top if top else (None, None)
        second_features, second_item_index = second if second else (None, None)
        if top_features and top_features["score"] >= 0.60:
            chapter_candidate_counts[top_item_index] += 1
        chapter_matches.append(
            {
                "provider_chapter_index": chapter_index,
                "provider_title": chapter.get("title"),
                "start": chapter.get("start"),
                "role_hint": _role_hint(str(chapter.get("title") or "")),
                "status": _chapter_status(top_features, second_features),
                "best_generated_item_index": top_item_index,
                "best": top_features,
                "second_generated_item_index": second_item_index,
                "second": second_features,
                "top_candidates": [
                    {
                        "generated_item_index": item_index,
                        "title": items[item_index].get("title"),
                        **features,
                    }
                    for features, item_index in candidates[:3]
                ],
                "source_best": (
                    _source_match(str(chapter.get("title") or ""), source_candidates)
                    if source_candidates is not None
                    else None
                ),
            }
        )

    item_matches: list[dict[str, Any]] = []
    for item_index, item in enumerate(items):
        candidates = sorted(
            (
                (features, chapter_index)
                for chapter_index, current_item, features in pair_rows
                if current_item == item_index
            ),
            key=lambda pair: (pair[0]["score"], pair[0]["identifier_overlap"] != [], -pair[1]),
            reverse=True,
        )
        top = candidates[0] if candidates else None
        top_features, top_chapter_index = top if top else (None, None)
        chapter_owner = (
            chapter_matches[top_chapter_index]["best_generated_item_index"]
            if top_chapter_index is not None
            else None
        )
        chapter_ambiguous = bool(
            top_chapter_index is not None
            and chapter_matches[top_chapter_index]["status"] == "ambiguous"
        )
        item_matches.append(
            {
                "generated_item_index": item_index,
                "title": item.get("title"),
                "display_ref": item.get("display_ref"),
                "status": _item_status(
                    top_features,
                    best_chapter_owner=chapter_owner,
                    item_index=item_index,
                    chapter_ambiguous=chapter_ambiguous,
                ),
                "best_provider_chapter_index": top_chapter_index,
                "best": top_features,
            }
        )

    return {
        "uid": row["uid"],
        "provider": row["provider"],
        "slug": row["slug"],
        "split": row["split"],
        "generated_item_count": len(items),
        "provider_chapter_count": len(chapters),
        "provider_chapters": chapter_matches,
        "generated_items": item_matches,
        "summary": {
            "provider_chapters": dict(Counter(match["status"] for match in chapter_matches)),
            "source_matches": dict(
                Counter(
                    match["source_best"]["status"]
                    for match in chapter_matches
                    if match["source_best"] is not None
                )
            ),
            "source_match_without_strong_generated_match": sum(
                match["source_best"] is not None
                and match["source_best"]["status"] in {"strong", "possible"}
                and match["status"] not in {"strong"}
                for match in chapter_matches
            ),
            "generated_items": dict(Counter(match["status"] for match in item_matches)),
            "chapters_with_multiple_item_candidates": sum(
                count > 1 for count in chapter_candidate_counts.values()
            ),
        },
    }


def build_crosswalk(
    manifest: dict[str, Any],
    gold: dict[str, Any],
    model: str,
    *,
    agenda_cache: Path | None = None,
) -> dict[str, Any]:
    rows_by_uid = {row["uid"]: row for row in manifest.get("episodes", [])}
    gold_by_uid = {row["uid"]: row for row in gold.get("episodes", [])}
    missing = sorted(set(rows_by_uid) - set(gold_by_uid))
    if missing:
        raise ValueError(f"gold is missing manifest UIDs: {missing[:5]}")
    episodes = [
        _episode_crosswalk(
            rows_by_uid[uid],
            gold_by_uid[uid],
            model,
            agenda_text=(
                (agenda_cache / f"{rows_by_uid[uid]['slug']}--{uid}.agenda.txt").read_text(
                    encoding="utf-8", errors="replace"
                )
                if agenda_cache is not None
                and (agenda_cache / f"{rows_by_uid[uid]['slug']}--{uid}.agenda.txt").exists()
                else None
            ),
        )
        for uid in sorted(rows_by_uid)
    ]
    chapter_statuses = Counter(
        match["status"] for episode in episodes for match in episode["provider_chapters"]
    )
    item_statuses = Counter(
        match["status"] for episode in episodes for match in episode["generated_items"]
    )
    source_statuses = Counter(
        match["source_best"]["status"]
        for episode in episodes
        for match in episode["provider_chapters"]
        if match["source_best"] is not None
    )
    return {
        "version": DATASET_VERSION,
        "purpose": "scoring-only generated-agenda to provider-chapter crosswalk audit",
        "model": model,
        "thresholds": {"possible": 0.60, "strong": 0.82, "ambiguity_margin": 0.08},
        "episodes": episodes,
        "summary": {
            "episodes": len(episodes),
            "provider_chapters": sum(chapter_statuses.values()),
            "generated_items": sum(item_statuses.values()),
            "provider_chapter_status": dict(chapter_statuses),
            "source_status": dict(source_statuses),
            "generated_item_status": dict(item_statuses),
            "source_match_without_strong_generated_match": sum(
                episode["summary"]["source_match_without_strong_generated_match"]
                for episode in episodes
            ),
            "chapters_with_multiple_item_candidates": sum(
                episode["summary"]["chapters_with_multiple_item_candidates"] for episode in episodes
            ),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--write", type=Path, required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--agenda-cache",
        type=Path,
        help="optional local cache named <slug>--<uid>.agenda.txt for source-level diagnostics",
    )
    args = parser.parse_args(argv)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    gold = json.loads(args.gold.read_text(encoding="utf-8"))
    result = build_crosswalk(manifest, gold, args.model, agenda_cache=args.agenda_cache)
    args.write.parent.mkdir(parents=True, exist_ok=True)
    args.write.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result["summary"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
