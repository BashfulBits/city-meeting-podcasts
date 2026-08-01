#!/usr/bin/env python
"""Build a read-only agenda-to-canonical-chapter alignment dataset (GH#1078).

The output retains both aligned and unmatched canonical titles.  It is research data only: no
episode, artifact, or durable state record is changed.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from audit_chapters import BenchmarkSample, collect_benchmark_cohort

from citypods.agenda_text import (
    AgendaTitleCandidate,
    agenda_title_similarity,
    extract_agenda_title_candidates,
)
from citypods.config import load_city_configs, load_site_config
from citypods.http import make_session
from citypods.storage.s3 import b2_from_env

MATCH_THRESHOLD = 0.8
DATASET_VERSION = 2


@dataclass(frozen=True)
class Alignment:
    canonical_index: int
    candidate_index: int
    similarity: float


def align_titles(
    sample: BenchmarkSample, candidates: list[AgendaTitleCandidate]
) -> list[Alignment]:
    """Return deterministic one-to-one close matches, retaining indices for research rows."""
    pairs = sorted(
        (
            (agenda_title_similarity(canonical, candidate.title), canonical_index, candidate_index)
            for canonical_index, canonical in enumerate(sample.canonical_titles)
            for candidate_index, candidate in enumerate(candidates)
        ),
        reverse=True,
    )
    used_canonical: set[int] = set()
    used_candidates: set[int] = set()
    alignments: list[Alignment] = []
    for similarity, canonical_index, candidate_index in pairs:
        if similarity < MATCH_THRESHOLD:
            break
        if canonical_index in used_canonical or candidate_index in used_candidates:
            continue
        used_canonical.add(canonical_index)
        used_candidates.add(candidate_index)
        alignments.append(Alignment(canonical_index, candidate_index, round(similarity, 6)))
    return sorted(alignments, key=lambda alignment: alignment.canonical_index)


def assign_chronological_splits(rows: list[dict]) -> None:
    """Reserve each provider/body family's newest 20% of meetings for held-out evaluation."""
    episodes: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in rows:
        episodes[(row["provider"], row["slug"], row["uid"])].append(row)
    families: dict[tuple[str, str], list[list[dict]]] = defaultdict(list)
    for episode_rows in episodes.values():
        first = episode_rows[0]
        families[(first["provider"], first["body"])].append(episode_rows)
    for episode_rows in families.values():
        ordered = sorted(episode_rows, key=lambda rows: (rows[0]["published"], rows[0]["uid"]))
        test_count = max(1, round(len(ordered) * 0.2))
        for index, rows_for_episode in enumerate(ordered):
            split = "test" if index >= len(ordered) - test_count else "train"
            for row in rows_for_episode:
                row["split"] = split


def build_rows(
    samples: list[tuple[str, BenchmarkSample]], fetch_agenda, checkpoint_path: Path
) -> tuple[list[dict], list[dict], list[dict]]:
    """Fetch agendas and materialize rows, checkpointing each episode for safe resumption."""
    rows, failures, candidate_rows, processed = load_checkpoint(checkpoint_path)
    if processed:
        print(f"alignment-dataset: resuming after {len(processed)} episodes", flush=True)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_file = checkpoint_path.open("a", encoding="utf-8")
    try:
        for sample_index, (provider, sample) in enumerate(samples, start=1):
            key = (provider, sample.slug, sample.uid)
            if key in processed:
                continue
            if sample_index == 1 or sample_index % 10 == 0 or sample_index == len(samples):
                print(f"alignment-dataset: episode {sample_index}/{len(samples)}", flush=True)
            episode_rows: list[dict] = []
            episode_candidate_rows: list[dict] = []
            failure: dict | None = None
            try:
                agenda_text = fetch_agenda(sample).decode("utf-8", errors="replace")
                candidates = extract_agenda_title_candidates(agenda_text)
                episode_candidate_rows = [
                    {
                        "provider": provider,
                        "slug": sample.slug,
                        "uid": sample.uid,
                        "agenda_candidate_index": candidate_index,
                        "agenda_candidate_title": candidate.title,
                        "agenda_line_number": candidate.line_number,
                    }
                    for candidate_index, candidate in enumerate(candidates)
                ]
                matched = {
                    alignment.canonical_index: alignment
                    for alignment in align_titles(sample, candidates)
                }
                for canonical_index, canonical_title in enumerate(sample.canonical_titles):
                    alignment = matched.get(canonical_index)
                    candidate = candidates[alignment.candidate_index] if alignment else None
                    episode_rows.append(
                        {
                            "provider": provider,
                            "slug": sample.slug,
                            "uid": sample.uid,
                            "published": sample.published,
                            "body": sample.body,
                            "canonical_index": canonical_index,
                            "canonical_title": canonical_title,
                            "agenda_candidate_index": (
                                alignment.candidate_index if alignment else None
                            ),
                            "agenda_candidate_title": candidate.title if candidate else None,
                            "agenda_line_number": candidate.line_number if candidate else None,
                            "similarity": alignment.similarity if alignment else None,
                        }
                    )
            except Exception as exc:
                failure = {
                    "provider": provider,
                    "slug": sample.slug,
                    "uid": sample.uid,
                    "error": str(exc),
                }
            entry = {
                "provider": provider,
                "slug": sample.slug,
                "uid": sample.uid,
                "rows": episode_rows,
            }
            if failure:
                entry["failure"] = failure
                failures.append(failure)
            else:
                rows.extend(episode_rows)
                candidate_rows.extend(episode_candidate_rows)
            entry["agenda_candidates"] = episode_candidate_rows
            checkpoint_file.write(json.dumps(entry, sort_keys=True) + "\n")
            checkpoint_file.flush()
    finally:
        checkpoint_file.close()
    assign_chronological_splits(rows)
    return rows, failures, candidate_rows


def load_checkpoint(
    checkpoint_path: Path,
) -> tuple[list[dict], list[dict], list[dict], set[tuple[str, str, str]]]:
    """Load completed episode entries from a local, append-only research checkpoint."""
    rows: list[dict] = []
    failures: list[dict] = []
    candidate_rows: list[dict] = []
    processed: set[tuple[str, str, str]] = set()
    if not checkpoint_path.exists():
        return rows, failures, candidate_rows, processed
    for line in checkpoint_path.read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        key = (entry["provider"], entry["slug"], entry["uid"])
        processed.add(key)
        if "failure" in entry:
            failures.append(entry["failure"])
        else:
            rows.extend(entry["rows"])
            candidate_rows.extend(entry["agenda_candidates"])
    return rows, failures, candidate_rows, processed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--agenda-timeout-seconds", type=float, default=15.0)
    args = parser.parse_args(argv)
    if args.agenda_timeout_seconds <= 0:
        parser.error("--agenda-timeout-seconds must be positive")
    site = load_site_config("config/site_config.yml")
    cities = load_city_configs("config", site.get("defaults", {}))
    cohort = collect_benchmark_cohort(cities, args.state_dir, sample_size=999999)
    samples = [(provider, sample) for provider, row in cohort.items() for sample in row.candidates]
    session = make_session()
    storage = b2_from_env()
    if storage is None:
        raise RuntimeError("B2 storage is not configured")

    with tempfile.TemporaryDirectory(prefix="citypods-alignment-agendas-") as temp_dir:
        temporary_dir = Path(temp_dir)

        def fetch_agenda(sample: BenchmarkSample) -> bytes:
            try:
                response = session.get(sample.agenda_text_url, timeout=args.agenda_timeout_seconds)
                response.raise_for_status()
                return response.content
            except Exception as public_error:
                local = temporary_dir / f"{sample.uid}.agenda.txt"
                if not storage.get_file(sample.agenda_text_key, local):
                    raise RuntimeError(
                        f"public agenda artifact unavailable and B2 key missing: {public_error}"
                    ) from public_error
                print(f"alignment-dataset: B2 fallback {sample.uid}", flush=True)
                return local.read_bytes()

        rows, failures, candidate_rows = build_rows(
            samples, fetch_agenda, args.output_dir / "checkpoint-v2.jsonl"
        )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "alignments.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
    )
    (args.output_dir / "agenda_candidates.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in candidate_rows)
    )
    manifest = {
        "version": DATASET_VERSION,
        "match_threshold": MATCH_THRESHOLD,
        "episodes": len({(row["provider"], row["slug"], row["uid"]) for row in rows}),
        "rows": len(rows),
        "matched_rows": sum(row["similarity"] is not None for row in rows),
        "agenda_candidates": len(candidate_rows),
        "failures": failures,
        "splits": {
            split: sum(row["split"] == split for row in rows) for split in ("train", "test")
        },
    }
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
