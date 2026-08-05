#!/usr/bin/env python
"""Prepare a small human-review packet for the locator crosswalk (GH#1078).

The packet is intentionally development-only and scoring-only.  It shows one provider chapter
at a time beside every generated agenda candidate from that meeting and the source agenda lines
behind those candidates.  Provider chapter timings are deliberately omitted: the review adjudicates
agenda/candidate relationships before transcript-boundary retrieval is evaluated.

The selector uses a deterministic, one-case-per-episode sample across both providers.  Its strata
are edge cases discovered by ``audit_locator_crosswalk.py`` plus a small set of clear controls.
The resulting JSON is suitable for the localhost review UI and should be kept outside the repo.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

DATASET_VERSION = 1
DEFAULT_MODEL = "mistral/mistral-medium-2508"
DEFAULT_SEED = "locator-crosswalk-review-v1"
CATEGORY_TARGETS = {
    "ambiguous": 12,
    "source_strong_generated_gap": 12,
    "procedural_or_consent": 8,
    "unmatched_structural_or_hierarchical": 8,
    "clear_control": 8,
}
_PROCEDURAL_RE = re.compile(
    r"\b(call to order|invocation|pledge|public comment|public input|public hearing|"
    r"consent agenda|consent calendar|executive session|work session|future agenda|"
    r"adjournment|announcement|recognition|proclamation|presentation)\b",
    re.I,
)
_HIERARCHICAL_RE = re.compile(r"(?<!\w)\d+\s*\.\s*[A-Z](?:\s*\.|\s|$)")


def _hash_key(seed: str, *parts: object) -> str:
    value = "|".join(str(part) for part in parts)
    return hashlib.sha256(f"{seed}|{value}".encode()).hexdigest()


def _agenda_lines(cache: Path, slug: str, uid: str) -> list[str]:
    path = cache / f"{slug}--{uid}.agenda.txt"
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def _line_excerpt(
    lines: list[str], start: int | None, end: int | None, padding: int = 2
) -> list[dict[str, object]]:
    if not lines or not isinstance(start, int) or not isinstance(end, int):
        return []
    first = max(1, start - padding)
    last = min(len(lines), end + padding)
    return [
        {"number": number, "text": lines[number - 1], "highlight": start <= number <= end}
        for number in range(first, last + 1)
    ]


def _category(chapter: dict[str, Any]) -> str:
    """Assign one mutually exclusive review stratum, in edge-case priority order."""
    title = str(chapter.get("provider_title") or "")
    source = chapter.get("source_best") or {}
    top_candidates = [
        candidate
        for candidate in chapter.get("top_candidates", [])
        if float(candidate.get("score", 0)) >= 0.60
    ]
    if chapter.get("status") == "ambiguous" or len(top_candidates) > 1:
        return "ambiguous"
    if source.get("status") in {"strong", "possible"} and chapter.get("status") != "strong":
        return "source_strong_generated_gap"
    if chapter.get("role_hint") or _PROCEDURAL_RE.search(title):
        return "procedural_or_consent"
    hierarchical = bool(_HIERARCHICAL_RE.search(title)) or any(
        "." in str(candidate.get("display_ref") or "")
        for candidate in chapter.get("top_candidates", [])
    )
    if (
        chapter.get("status") in {"unmatched", "possible"}
        or source.get("status") in {None, "unmatched"}
        or hierarchical
    ):
        return "unmatched_structural_or_hierarchical"
    return "clear_control"


def _episode_fields(manifest_row: dict[str, Any]) -> dict[str, object]:
    agenda = manifest_row.get("agenda") or {}
    # Do not copy transcript/word-sidecar pointers or provider chapter data into this packet.
    return {
        "uid": manifest_row["uid"],
        "provider": manifest_row["provider"],
        "slug": manifest_row["slug"],
        "body": manifest_row.get("body"),
        "published": manifest_row.get("published"),
        "duration_bucket": manifest_row.get("duration_bucket"),
        "agenda_url": agenda.get("url"),
        "agenda_bytes": agenda.get("bytes"),
    }


def _candidate_items(manifest_row: dict[str, Any], model: str) -> list[dict[str, object]]:
    generated_root = manifest_row.get("generated_agenda") or {}
    generated = generated_root.get(model, {}) if isinstance(generated_root, dict) else {}
    return list(generated.get("items", [])) if isinstance(generated, dict) else []


def _group_rows(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row["uid"])].append(row)
    return groups


def _pick_category(
    rows: list[dict[str, Any]],
    *,
    category: str,
    target: int,
    used_uids: set[str],
    seed: str,
) -> list[dict[str, Any]]:
    """Pick unique episodes while balancing providers, bodies, and duration buckets."""
    available = _group_rows([row for row in rows if str(row["uid"]) not in used_uids])
    by_provider: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidates in available.values():
        # One chapter per episode per packet.  Prefer a stable candidate within an episode.
        chosen = sorted(
            candidates,
            key=lambda row: _hash_key(seed, category, row["uid"], row["provider_chapter_index"]),
        )[0]
        by_provider[str(chosen["provider"])].append(chosen)
    providers = sorted(by_provider)
    if not providers:
        return []
    quotas = {provider: target // len(providers) for provider in providers}
    for provider in providers[: target % len(providers)]:
        quotas[provider] += 1
    selected: list[dict[str, Any]] = []
    used_bodies: Counter[str] = Counter()
    used_durations: Counter[str] = Counter()
    for provider in providers:
        pool = by_provider[provider]
        while quotas[provider] and pool:
            pool.sort(
                key=lambda row: (
                    used_bodies[str(row["body_key"])],
                    used_durations[str(row["duration_bucket"])],
                    _hash_key(seed, category, row["uid"], row["provider_chapter_index"]),
                )
            )
            row = pool.pop(0)
            selected.append(row)
            used_uids.add(str(row["uid"]))
            used_bodies[str(row["body_key"])] += 1
            used_durations[str(row["duration_bucket"])] += 1
            quotas[provider] -= 1
    # If a provider had too few unique episodes, fill from the remaining providers rather than
    # silently returning a short packet.
    if len(selected) < target:
        remaining = [row for provider in providers for row in by_provider[provider]]
        remaining.sort(
            key=lambda row: _hash_key(seed, category, row["uid"], row["provider_chapter_index"])
        )
        for row in remaining:
            if len(selected) >= target:
                break
            if str(row["uid"]) in used_uids:
                continue
            selected.append(row)
            used_uids.add(str(row["uid"]))
    return selected


def prepare_packet(
    manifest: dict[str, Any],
    crosswalk: dict[str, Any],
    *,
    agenda_cache: Path,
    split: str = "development",
    model: str = DEFAULT_MODEL,
    seed: str = DEFAULT_SEED,
) -> dict[str, object]:
    manifest_rows = {
        row["uid"]: row for row in manifest.get("episodes", []) if row.get("split") == split
    }
    rows: list[dict[str, Any]] = []
    for episode in crosswalk.get("episodes", []):
        manifest_row = manifest_rows.get(episode.get("uid"))
        if manifest_row is None:
            continue
        lines = _agenda_lines(agenda_cache, manifest_row["slug"], manifest_row["uid"])
        for chapter in episode.get("provider_chapters", []):
            category = _category(chapter)
            rows.append(
                {
                    "uid": episode["uid"],
                    "provider": episode["provider"],
                    "body": manifest_row.get("body"),
                    "body_key": manifest_row.get("body_key"),
                    "duration_bucket": manifest_row.get("duration_bucket"),
                    "provider_chapter_index": chapter["provider_chapter_index"],
                    "provider_title": chapter.get("provider_title"),
                    "category": category,
                    "agenda_lines": lines,
                    "agenda_source_available": bool(lines),
                    "episode": _episode_fields(manifest_row),
                    "candidates": _candidate_items(manifest_row, model),
                }
            )
    used_uids: set[str] = set()
    selected: list[dict[str, Any]] = []
    for category, target in CATEGORY_TARGETS.items():
        selected.extend(
            _pick_category(
                [row for row in rows if row["category"] == category],
                category=category,
                target=target,
                used_uids=used_uids,
                seed=seed,
            )
        )
    if len(selected) != sum(CATEGORY_TARGETS.values()):
        raise ValueError(
            f"could not select requested packet size: got {len(selected)} "
            f"wanted {sum(CATEGORY_TARGETS.values())}; "
            f"available={Counter(row['category'] for row in rows)}"
        )
    selected.sort(
        key=lambda row: _hash_key(seed, row["category"], row["uid"], row["provider_chapter_index"])
    )
    cases: list[dict[str, object]] = []
    for number, row in enumerate(selected, 1):
        candidates = []
        for index, item in enumerate(row["candidates"]):
            candidates.append(
                {
                    "candidate_id": f"C{index + 1:03d}",
                    "index": index,
                    "title": item.get("title"),
                    "display_ref": item.get("display_ref"),
                    "evidence_text": item.get("evidence_text"),
                    "agenda_line_start": item.get("line_start"),
                    "agenda_line_end": item.get("line_end"),
                }
            )
        cases.append(
            {
                "case_id": f"LXR-{number:03d}",
                "category": row["category"],
                "episode": row["episode"],
                "provider_chapter": {
                    "index": row["provider_chapter_index"],
                    "title": row["provider_title"],
                },
                "agenda": {
                    "source_available": row["agenda_source_available"],
                    "lines": row["agenda_lines"],
                    "url": row["episode"].get("agenda_url"),
                },
                "candidates": candidates,
            }
        )
    return {
        "version": DATASET_VERSION,
        "purpose": "development-only human adjudication of agenda/provider-chapter crosswalk",
        "model": model,
        "split": split,
        "seed": seed,
        "timings_included": False,
        "selection_targets": CATEGORY_TARGETS,
        "selection_counts": dict(Counter(case["category"] for case in cases)),
        "unique_episodes": len({case["episode"]["uid"] for case in cases}),
        "cases": cases,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--crosswalk", type=Path, required=True)
    parser.add_argument("--agenda-cache", type=Path, required=True)
    parser.add_argument("--write", type=Path, required=True)
    parser.add_argument("--split", default="development")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--seed", default=DEFAULT_SEED)
    args = parser.parse_args(argv)
    packet = prepare_packet(
        json.loads(args.manifest.read_text(encoding="utf-8")),
        json.loads(args.crosswalk.read_text(encoding="utf-8")),
        agenda_cache=args.agenda_cache,
        split=args.split,
        model=args.model,
        seed=args.seed,
    )
    args.write.parent.mkdir(parents=True, exist_ok=True)
    args.write.write_text(json.dumps(packet, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {key: packet[key] for key in ("split", "selection_counts", "unique_episodes")},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
