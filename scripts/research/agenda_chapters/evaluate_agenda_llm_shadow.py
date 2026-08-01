#!/usr/bin/env python
"""Score completed GH#1078 agenda-title shadow outputs without any additional LLM calls.

This is deliberately a lexical proxy, not semantic adjudication.  It uses the exact one-to-one
matcher that built the frozen cohort and reports both all-canonical coverage and coverage of the
cohort's pre-existing deterministic ``agenda-derived`` subset.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from citypods.agenda_text import AgendaTitleCandidate
from citypods.chapter_titles import match_title_candidates

MATCH_THRESHOLD = 0.8
EpisodeKey = tuple[str, str, str]


def _key(row: dict) -> EpisodeKey:
    return row["provider"], row["slug"], row["uid"]


def _read_jsonl(path: Path):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            yield json.loads(line)


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


def _cohort(sample_path: Path, alignments_path: Path) -> dict[EpisodeKey, dict]:
    selected = {_key(row): row for row in json.loads(sample_path.read_text())}
    episodes: dict[EpisodeKey, dict] = {
        key: {"sample": row, "canonical_titles": [], "baseline_titles": []}
        for key, row in selected.items()
    }
    for row in _read_jsonl(alignments_path):
        key = _key(row)
        episode = episodes.get(key)
        if episode is None:
            continue
        episode["canonical_titles"].append(row["canonical_title"])
        if row["similarity"] is not None and row["similarity"] >= MATCH_THRESHOLD:
            episode["baseline_titles"].append(row["canonical_title"])
    if missing := [key for key, episode in episodes.items() if not episode["canonical_titles"]]:
        raise RuntimeError(f"frozen sample is absent from alignment data: {missing!r}")
    return episodes


def _metrics(rows: list[dict]) -> dict[str, float | int | None]:
    generated = sum(row["generated_count"] for row in rows)
    canonical = sum(row["canonical_count"] for row in rows)
    baseline = sum(row["baseline_count"] for row in rows)
    all_matches = sum(row["all_matches"] for row in rows)
    baseline_matches = sum(row["baseline_matches"] for row in rows)
    return {
        "episodes": len(rows),
        "generated_items": generated,
        "rejected_items": sum(row["rejected_count"] for row in rows),
        "all_canonical_lexical_matches": all_matches,
        "agenda_derived_lexical_matches": baseline_matches,
        "micro_all_canonical_precision": round(all_matches / generated, 6) if generated else None,
        "micro_all_canonical_recall": round(all_matches / canonical, 6) if canonical else None,
        "micro_agenda_derived_precision": (
            round(baseline_matches / generated, 6) if generated else None
        ),
        "micro_agenda_derived_recall": (
            round(baseline_matches / baseline, 6) if baseline else None
        ),
        "macro_all_canonical_precision": _mean(
            [row["all_precision"] for row in rows if row["all_precision"] is not None]
        ),
        "macro_canonical_recall": _mean(
            [row["all_canonical_recall"] for row in rows if row["all_canonical_recall"] is not None]
        ),
        "macro_agenda_derived_precision": _mean(
            [row["baseline_precision"] for row in rows if row["baseline_precision"] is not None]
        ),
        "macro_agenda_derived_recall": _mean(
            [row["baseline_recall"] for row in rows if row["baseline_recall"] is not None]
        ),
        "mean_absolute_agenda_derived_count_error": _mean(
            [abs(row["generated_count"] - row["baseline_count"]) for row in rows]
        ),
    }


def evaluate_model(model_dir: Path, episodes: dict[EpisodeKey, dict]) -> dict:
    rows: list[dict] = []
    for path in sorted(model_dir.glob("*.json")):
        output = json.loads(path.read_text())
        if output.get("status") != "completed":
            raise RuntimeError(f"incomplete model output: {path}")
        key = _key(output["episode"])
        episode = episodes.get(key)
        if episode is None:
            raise RuntimeError(f"model output outside frozen sample: {path}")
        candidates = [
            AgendaTitleCandidate(title=item["title"], line_number=item["line_start"])
            for item in output["items"]
        ]
        generated_count = len(candidates)
        canonical_count = len(episode["canonical_titles"])
        baseline_count = len(episode["baseline_titles"])
        all_matches = len(
            match_title_candidates(
                episode["canonical_titles"], candidates, threshold=MATCH_THRESHOLD
            )
        )
        baseline_matches = len(
            match_title_candidates(
                episode["baseline_titles"], candidates, threshold=MATCH_THRESHOLD
            )
        )
        rows.append(
            {
                "key": key,
                "provider": episode["sample"]["provider"],
                "coverage_bucket": episode["sample"]["coverage_bucket"],
                "generated_count": generated_count,
                "rejected_count": len(output["rejected"]),
                "canonical_count": canonical_count,
                "baseline_count": baseline_count,
                "all_matches": all_matches,
                "baseline_matches": baseline_matches,
                "all_precision": all_matches / generated_count if generated_count else None,
                "all_canonical_recall": all_matches / canonical_count if canonical_count else None,
                "baseline_precision": (
                    baseline_matches / generated_count if generated_count else None
                ),
                "baseline_recall": baseline_matches / baseline_count if baseline_count else None,
            }
        )
    if set(row["key"] for row in rows) != set(episodes):
        raise RuntimeError("model output does not cover exactly the frozen sample")
    by_stratum: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        by_stratum[row["provider"], row["coverage_bucket"]].append(row)
    return {
        "overall": _metrics(rows),
        "by_provider_coverage_bucket": {
            f"{provider}/{bucket}": _metrics(group)
            for (provider, bucket), group in sorted(by_stratum.items())
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=Path, required=True)
    parser.add_argument("--alignments", type=Path, required=True)
    parser.add_argument("--outputs-dir", type=Path, required=True)
    parser.add_argument("--write", type=Path)
    args = parser.parse_args(argv)
    episodes = _cohort(args.sample, args.alignments)
    result = {
        "method": "one_to_one_lexical_agenda_title_similarity_proxy",
        "match_threshold": MATCH_THRESHOLD,
        "semantic_adjudication": "not performed",
        "models": {
            model_dir.name: evaluate_model(model_dir, episodes)
            for model_dir in sorted(path for path in args.outputs_dir.iterdir() if path.is_dir())
        },
    }
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.write:
        args.write.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
