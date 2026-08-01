#!/usr/bin/env python
"""Freeze a stratified, read-only agenda-title LLM shadow sample for GH#1078."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path

from citypods.bodies import body_key, canonical_body, matches
from citypods.config import load_city_configs, load_site_config

MATCH_THRESHOLD = 0.8
EpisodeKey = tuple[str, str, str]
EpisodeIdentity = tuple[str, str]


def read_jsonl(path: Path):
    """Yield JSON objects from a local research dataset file."""
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            yield json.loads(line)


def key(row: dict) -> EpisodeKey:
    """Return the dataset identity shared by alignment and candidate records."""
    return row["provider"], row["slug"], row["uid"]


def stable_order(row: dict) -> str:
    """Return a deterministic pseudo-random order independent of source-store ordering."""
    value = "\x1f".join((row["provider"], row["slug"], row["uid"])).encode()
    return hashlib.sha256(value).hexdigest()


def coverage_bucket(match_rate: float) -> str:
    """Coarsely stratify observed deterministic source-title coverage."""
    if match_rate >= 0.8:
        return "high"
    if match_rate >= 0.4:
        return "medium"
    return "low"


def select_diverse(rows: list[dict], count: int) -> list[dict]:
    """Favor body diversity before filling a stratum by deterministic order."""
    by_body: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_body[body_key(canonical_body(row["body"]))].append(row)
    for body_rows in by_body.values():
        body_rows.sort(key=stable_order)
    selected: list[dict] = []
    while len(selected) < count:
        added = False
        for body in sorted(by_body):
            if by_body[body] and len(selected) < count:
                selected.append(by_body[body].pop(0))
                added = True
        if not added:
            break
    return selected


def build_sample(
    dataset_dir: Path,
    *,
    per_provider_per_bucket: int,
    excluded: set[EpisodeIdentity] = frozenset(),
    configured_bodies: dict[str, str | None] | None = None,
    avoided_body_keys: set[str] = frozenset(),
    excluded_slugs: set[str] = frozenset(),
    excluded_uids: set[str] = frozenset(),
    stratum_counts: dict[tuple[str, str], int] | None = None,
    min_agenda_candidates: int = 0,
) -> list[dict]:
    """Select held-out meetings across provider and deterministic-coverage strata."""
    episodes: dict[EpisodeKey, dict] = {}
    for alignment in read_jsonl(dataset_dir / "alignments.jsonl"):
        episode = episodes.setdefault(
            key(alignment),
            {
                "provider": alignment["provider"],
                "slug": alignment["slug"],
                "uid": alignment["uid"],
                "published": alignment["published"],
                "body": alignment["body"],
                "split": alignment["split"],
                "canonical_title_count": 0,
                "matched_title_count": 0,
                "agenda_candidate_count": 0,
            },
        )
        episode["canonical_title_count"] += 1
        if alignment["similarity"] is not None and alignment["similarity"] >= MATCH_THRESHOLD:
            episode["matched_title_count"] += 1
    for candidate in read_jsonl(dataset_dir / "agenda_candidates.jsonl"):
        episode = episodes.get(key(candidate))
        if episode is not None:
            episode["agenda_candidate_count"] += 1
    strata: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for episode in episodes.values():
        if (
            episode["split"] != "test"
            or not episode["canonical_title_count"]
            # Shared provider sources project the same episode through multiple feed slugs. The
            # provider/UID identity, not the feed projection, defines held-out independence.
            or (episode["provider"], episode["uid"]) in excluded
            or episode["slug"] in excluded_slugs
            or episode["uid"] in excluded_uids
            or body_key(canonical_body(episode["body"])) in avoided_body_keys
            or episode["agenda_candidate_count"] < min_agenda_candidates
            or (
                configured_bodies is not None
                and configured_bodies.get(episode["slug"]) is not None
                and not matches(episode["body"], configured_bodies[episode["slug"]] or "")
            )
        ):
            continue
        episode["deterministic_match_rate"] = round(
            episode["matched_title_count"] / episode["canonical_title_count"], 6
        )
        episode["coverage_bucket"] = coverage_bucket(episode["deterministic_match_rate"])
        strata[(episode["provider"], episode["coverage_bucket"])].append(episode)
    selected: list[dict] = []
    for provider in sorted({provider for provider, _ in strata}):
        for bucket in ("high", "medium", "low"):
            count = (stratum_counts or {}).get((provider, bucket), per_provider_per_bucket)
            selected.extend(select_diverse(strata[(provider, bucket)], count))
    return sorted(
        selected, key=lambda row: (row["provider"], row["coverage_bucket"], stable_order(row))
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=Path("config"),
        help="feed configs used to exclude source-archive rows invisible to their feed",
    )
    parser.add_argument(
        "--exclude-sample",
        type=Path,
        action="append",
        help="a previously frozen sample whose episodes must not enter this selection",
    )
    parser.add_argument("--per-provider-per-bucket", type=int, default=16)
    parser.add_argument(
        "--min-agenda-candidates",
        type=int,
        default=0,
        help="require at least this many structural agenda candidates (default: 0)",
    )
    parser.add_argument(
        "--stratum-count",
        action="append",
        metavar="PROVIDER/BUCKET=COUNT",
        help="optional targeted count, for example swagit/low=3; unspecified strata use default",
    )
    parser.add_argument(
        "--avoid-sample-bodies",
        type=Path,
        help="sample whose canonical bodies should not be selected again",
    )
    parser.add_argument(
        "--exclude-slug",
        action="append",
        default=[],
        help="exclude a feed slug from this selection; repeatable",
    )
    parser.add_argument(
        "--exclude-uid",
        action="append",
        default=[],
        help="exclude a provider episode UID from this selection; repeatable",
    )
    args = parser.parse_args(argv)
    if args.per_provider_per_bucket < 0:
        parser.error("--per-provider-per-bucket must be non-negative")
    if args.min_agenda_candidates < 0:
        parser.error("--min-agenda-candidates must be non-negative")
    excluded = {
        (row["provider"], row["uid"])
        for sample_path in args.exclude_sample or []
        for row in json.loads(sample_path.read_text())
    }
    site = load_site_config(args.config_dir / "site_config.yml")
    configured_bodies = {
        city.slug: city.source.get("body")
        for city in load_city_configs(args.config_dir, site.get("defaults", {}))
    }
    stratum_counts: dict[tuple[str, str], int] = {}
    for raw in args.stratum_count or []:
        name, separator, raw_count = raw.partition("=")
        provider, slash, bucket = name.partition("/")
        if not separator or not slash or not provider or bucket not in {"high", "medium", "low"}:
            parser.error("--stratum-count must be PROVIDER/(high|medium|low)=COUNT")
        try:
            count = int(raw_count)
        except ValueError:
            parser.error("--stratum-count count must be an integer")
        if count < 0:
            parser.error("--stratum-count count must be non-negative")
        stratum_counts[(provider, bucket)] = count
    avoided_body_keys = {
        body_key(canonical_body(row["body"]))
        for row in json.loads(args.avoid_sample_bodies.read_text())
    } if args.avoid_sample_bodies else set()
    sample = build_sample(
        args.dataset_dir,
        per_provider_per_bucket=args.per_provider_per_bucket,
        excluded=excluded,
        configured_bodies=configured_bodies,
        avoided_body_keys=avoided_body_keys,
        excluded_slugs=set(args.exclude_slug),
        excluded_uids=set(args.exclude_uid),
        stratum_counts=stratum_counts,
        min_agenda_candidates=args.min_agenda_candidates,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(sample, indent=2, sort_keys=True) + "\n")
    summary = {
        "episodes": len(sample),
        "by_provider_bucket": {
            f"{provider}/{bucket}": sum(
                row["provider"] == provider and row["coverage_bucket"] == bucket for row in sample
            )
            for provider in sorted({row["provider"] for row in sample})
            for bucket in ("high", "medium", "low")
        },
    }
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
