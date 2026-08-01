#!/usr/bin/env python
"""Create a blinded, stratified human/LLM adjudication packet for GH#1078.

The public packet deliberately carries anonymous A/B generated-title sets.  Keep its adjacent key
file out of the adjudicator's context; it is only for unblinding aggregate results afterward.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

MATCH_THRESHOLD = 0.8
EpisodeKey = tuple[str, str, str]


def _key(row: dict) -> EpisodeKey:
    return row["provider"], row["slug"], row["uid"]


def _stable(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _read_jsonl(path: Path):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            yield json.loads(line)


def _canonical_titles(sample_path: Path, alignments_path: Path) -> dict[EpisodeKey, list[str]]:
    selected = {_key(row): row for row in json.loads(sample_path.read_text())}
    titles: dict[EpisodeKey, list[str]] = defaultdict(list)
    for row in _read_jsonl(alignments_path):
        key = _key(row)
        if key in selected:
            titles[key].append(row["canonical_title"])
    if set(titles) != set(selected):
        raise RuntimeError("alignment data does not cover exactly the frozen sample")
    return titles


def _read_model_outputs(outputs_dir: Path) -> dict[str, dict[EpisodeKey, dict]]:
    models: dict[str, dict[EpisodeKey, dict]] = {}
    for directory in sorted(path for path in outputs_dir.iterdir() if path.is_dir()):
        rows = {}
        for path in directory.glob("*.json"):
            output = json.loads(path.read_text())
            if output.get("status") != "completed":
                raise RuntimeError(f"incomplete output: {path}")
            rows[_key(output["episode"])] = output
        models[directory.name] = rows
    if len(models) != 2:
        raise RuntimeError(
            "blinded paired adjudication requires exactly two completed model directories"
        )
    return models


def _lexical_match_count(canonical_titles: list[str], output: dict) -> int:
    """Mirror the frozen cohort's greedy title matcher without importing LLM-facing modules."""

    def normalized(value: str) -> str:
        return " ".join(
            "".join(char if char.isalnum() else " " for char in value.casefold()).split()
        )

    pairs = sorted(
        (
            (
                1.0
                if normalized(canonical) in normalized(item["title"])
                or normalized(item["title"]) in normalized(canonical)
                else SequenceMatcher(a=normalized(canonical), b=normalized(item["title"])).ratio(),
                canonical_index,
                generated_index,
            )
            for canonical_index, canonical in enumerate(canonical_titles)
            for generated_index, item in enumerate(output["items"])
            if normalized(canonical) and normalized(item["title"])
        ),
        reverse=True,
    )
    used_canonical: set[int] = set()
    used_generated: set[int] = set()
    matches = 0
    for similarity, canonical_index, generated_index in pairs:
        if similarity < MATCH_THRESHOLD:
            break
        if canonical_index in used_canonical or generated_index in used_generated:
            continue
        used_canonical.add(canonical_index)
        used_generated.add(generated_index)
        matches += 1
    return matches


def _select(rows: list[dict], *, per_stratum: int) -> list[dict]:
    """Select one lexical-tie control then the largest model disagreements per stratum."""
    if per_stratum < 2:
        raise ValueError("per_stratum must be at least two")
    controls = sorted(
        rows,
        key=lambda row: (abs(row["match_delta"]), _stable(row["opaque_id"])),
    )
    disagreements = sorted(
        rows,
        key=lambda row: (-abs(row["match_delta"]), _stable(row["opaque_id"])),
    )
    chosen = [controls[0]]
    chosen_ids = {chosen[0]["opaque_id"]}
    for row in disagreements:
        if row["opaque_id"] in chosen_ids:
            continue
        chosen.append(row)
        chosen_ids.add(row["opaque_id"])
        if len(chosen) == per_stratum:
            break
    if len(chosen) != per_stratum:
        raise RuntimeError("stratum lacks enough distinct episodes")
    chosen[0]["selection_role"] = "lexical-tie-control"
    for row in chosen[1:]:
        row["selection_role"] = "lexical-disagreement"
    return chosen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=Path, required=True)
    parser.add_argument("--alignments", type=Path, required=True)
    parser.add_argument("--outputs-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--per-provider-bucket", type=int, default=4)
    parser.add_argument(
        "--all",
        action="store_true",
        help="include every supplied meeting instead of selecting a paired subset per stratum",
    )
    args = parser.parse_args(argv)
    sample_rows = {_key(row): row for row in json.loads(args.sample.read_text())}
    canonical = _canonical_titles(args.sample, args.alignments)
    models = _read_model_outputs(args.outputs_dir)
    model_names = tuple(sorted(models))
    if any(set(outputs) != set(sample_rows) for outputs in models.values()):
        raise RuntimeError("model outputs do not cover exactly the frozen sample")

    strata: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for key, sample in sample_rows.items():
        left, right = (models[name][key] for name in model_names)
        opaque_id = _stable("\x1f".join(key))[:16]
        strata[sample["provider"], sample["coverage_bucket"]].append(
            {
                "key": key,
                "opaque_id": opaque_id,
                "sample": sample,
                "canonical_titles": canonical[key],
                "outputs": {model_names[0]: left, model_names[1]: right},
                "match_delta": _lexical_match_count(canonical[key], left)
                - _lexical_match_count(canonical[key], right),
            }
        )
    if args.all:
        selected = []
        for stratum in sorted(strata):
            for row in sorted(strata[stratum], key=lambda value: _stable(value["opaque_id"])):
                row["selection_role"] = "all-meetings"
                selected.append(row)
    else:
        selected = [
            row
            for stratum in sorted(strata)
            for row in _select(strata[stratum], per_stratum=args.per_provider_bucket)
        ]
    packet, key = [], []
    for row in selected:
        labels = ("A", "B")
        # Stable randomized order means the packet remains reproducible while hiding model IDs.
        if int(_stable(row["opaque_id"])[0], 16) % 2:
            labels = tuple(reversed(labels))
        mapping = dict(zip(labels, model_names, strict=True))
        packet.append(
            {
                "adjudication_id": row["opaque_id"],
                "provider": row["sample"]["provider"],
                "coverage_bucket": row["sample"]["coverage_bucket"],
                "canonical_titles": row["canonical_titles"],
                "generated_title_sets": [
                    {
                        "label": label,
                        "items": [
                            {
                                "title": item["title"],
                                "evidence_quote": item["evidence_quote"],
                                "line_start": item["line_start"],
                                "line_end": item["line_end"],
                            }
                            for item in row["outputs"][mapping[label]]["items"]
                        ],
                    }
                    for label in labels
                ],
            }
        )
        key.append(
            {
                "adjudication_id": row["opaque_id"],
                "episode": row["key"],
                "selection_role": row["selection_role"],
                "lexical_match_delta": row["match_delta"],
                "label_to_model": mapping,
            }
        )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "packet.json").write_text(json.dumps(packet, indent=2) + "\n")
    (args.output_dir / "unblinding-key.json").write_text(json.dumps(key, indent=2) + "\n")
    print(f"wrote {len(packet)} blinded adjudication meetings across {len(strata)} strata")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
