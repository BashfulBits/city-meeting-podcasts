#!/usr/bin/env python
"""Report materialized episode chapter coverage by provider (GH#1078).

The audit is deliberately read-only: it never writes state or mutates GitHub.  By default it
reads the local durable-state mirror; pass ``--pull-state`` to refresh that mirror from the
canonical object-store snapshot first.  This makes it safe to run locally and in a scheduled
research job while still reporting the actual materialized catalog rather than a provider's
current (and often truncated) archive view.

Usage:
    PYTHONPATH=. python scripts/research/agenda_chapters/audit_chapters.py --pull-state
    PYTHONPATH=. python scripts/research/agenda_chapters/audit_chapters.py \
        --state-dir /path/to/state --json
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path

from citypods.agenda_text import extract_agenda_title_candidates
from citypods.chapter_titles import match_title_candidates
from citypods.compute.llm_policy import estimate_tokens
from citypods.config import load_city_configs, load_site_config
from citypods.http import make_session
from citypods.records import load_records, source_key
from citypods.state import pull_canonical_state, resolve_state_dir


@dataclass(frozen=True)
class ChapterlessSample:
    """A bounded, public-reference sample for manual upstream spot checks."""

    slug: str
    uid: str
    published: str
    title: str
    video_url: str
    chapter_stage: str


@dataclass
class ProviderCoverage:
    """Chapter coverage aggregate for one provider across configured record stores."""

    episodes: int = 0
    source_chapters: int = 0
    chapters: int = 0
    chapterless: int = 0
    attempted_empty: int = 0
    legacy_or_unknown: int = 0
    missing_record_stores: list[str] = field(default_factory=list)
    samples: list[ChapterlessSample] = field(default_factory=list)


@dataclass(frozen=True)
class BenchmarkSample:
    """One canonical-chapter record suitable for offline locator evaluation."""

    slug: str
    uid: str
    published: str
    body: str
    title: str
    chapter_count: int
    canonical_titles: tuple[str, ...]
    duration_seconds: float | None
    transcript_key: str
    words_key: str
    agenda_text_key: str
    transcript_url: str
    agenda_text_url: str
    agenda_url: str


@dataclass
class ProviderBenchmark:
    """Eligibility aggregate for a canonical chapter-locator benchmark."""

    episodes: int = 0
    by_body: dict[str, int] = field(default_factory=dict)
    samples: list[BenchmarkSample] = field(default_factory=list)
    candidates: list[BenchmarkSample] = field(default_factory=list, repr=False)


@dataclass(frozen=True)
class ArtifactMeasurement:
    """Read-only full-context input measurement for one benchmark example."""

    provider: str
    slug: str
    uid: str
    body: str
    transcript_bytes: int | None
    agenda_bytes: int | None
    transcript_tokens: int | None
    agenda_tokens: int | None
    combined_tokens: int | None
    error: str | None = None


@dataclass(frozen=True)
class TitleBenchmarkMeasurement:
    """Read-only comparison of main-agenda candidates to canonical chapter titles."""

    provider: str
    slug: str
    uid: str
    canonical_count: int
    candidate_count: int
    matched_count: int
    match_pct: float | None
    mean_similarity: float | None
    error: str | None = None


def _chapter_list(record: Mapping[str, object], field_name: str) -> list[object]:
    value = record.get(field_name)
    return value if isinstance(value, list) else []


def _chapter_stage(record: Mapping[str, object]) -> str:
    completion = record.get("stage_completion")
    if not isinstance(completion, Mapping):
        return "legacy/unknown"
    chapters = completion.get("chapters")
    if not isinstance(chapters, Mapping):
        return "legacy/unknown"
    state = chapters.get("state")
    return state if isinstance(state, str) and state else "legacy/unknown"


def _episode_provider(record: Mapping[str, object], fallback: str) -> str:
    """Use recorded source provenance when unambiguous, otherwise its configured provider."""
    sources = record.get("sources")
    if not isinstance(sources, list):
        return fallback
    providers = {
        source.get("provider")
        for source in sources
        if isinstance(source, Mapping) and isinstance(source.get("provider"), str)
    }
    return providers.pop() if len(providers) == 1 else fallback


def _sample(record: Mapping[str, object], *, slug: str, uid: str, stage: str) -> ChapterlessSample:
    links = record.get("links")
    canonical = links.get("canonical_video") if isinstance(links, Mapping) else None
    video_url = canonical if isinstance(canonical, str) else record.get("video_url")
    return ChapterlessSample(
        slug=slug,
        uid=uid,
        published=str(record.get("published") or ""),
        title=str(record.get("title") or ""),
        video_url=str(video_url or ""),
        chapter_stage=stage,
    )


def _benchmark_sample(
    record: Mapping[str, object], *, slug: str, uid: str
) -> BenchmarkSample | None:
    """Return a fully-persisted canonical example, or ``None`` without guessing artifacts.

    This intentionally checks pointers rather than fetching them.  The benchmark runner can use
    these immutable artifact keys later; the audit remains safe to run against production state
    without network credentials or model access.
    """
    chapters = _chapter_list(record, "source_chapters") or _chapter_list(record, "chapters")
    canonical_titles = tuple(
        chapter["title"].strip()
        for chapter in chapters
        if isinstance(chapter, Mapping)
        and isinstance(chapter.get("title"), str)
        and chapter["title"].strip()
    )
    audio = record.get("audio")
    transcript = record.get("transcript")
    links = record.get("links")
    audio_key = audio.get("key") if isinstance(audio, Mapping) else record.get("audio_key")
    duration = audio.get("duration_served") if isinstance(audio, Mapping) else None
    if not isinstance(transcript, Mapping) or not isinstance(links, Mapping):
        return None
    transcript_key = transcript.get("key")
    words_key = transcript.get("words_key")
    agenda_text_key = links.get("agenda_text_artifact_key")
    transcript_url = transcript.get("url")
    agenda_text_url = links.get("agenda_text_artifact")
    agenda_url = links.get("agenda_portal") or links.get("agenda") or links.get("agenda_packet")
    if not (
        chapters
        and isinstance(audio_key, str)
        and isinstance(transcript_key, str)
        and transcript.get("synced") is True
        and isinstance(words_key, str)
        and isinstance(agenda_text_key, str)
        and isinstance(transcript_url, str)
        and isinstance(agenda_text_url, str)
        and isinstance(agenda_url, str)
    ):
        return None
    return BenchmarkSample(
        slug=slug,
        uid=uid,
        published=str(record.get("published") or ""),
        body=str(record.get("body") or "(unclassified)"),
        title=str(record.get("title") or ""),
        chapter_count=len(chapters),
        canonical_titles=canonical_titles,
        duration_seconds=float(duration) if isinstance(duration, int | float) else None,
        transcript_key=transcript_key,
        words_key=words_key,
        agenda_text_key=agenda_text_key,
        transcript_url=transcript_url,
        agenda_text_url=agenda_text_url,
        agenda_url=agenda_url,
    )


def collect_coverage(
    cities: Iterable, state_dir: Path, *, sample_size: int
) -> dict[str, ProviderCoverage]:
    """Read configured record stores and return provider-grouped chapter coverage."""
    coverage: defaultdict[str, ProviderCoverage] = defaultdict(ProviderCoverage)
    for city in cities:
        records = load_records(state_dir, source_key(city))
        if not records:
            coverage[city.provider].missing_record_stores.append(city.slug)
            continue
        for uid, record in records.items():
            if not isinstance(record, Mapping):
                continue
            provider = _episode_provider(record, city.provider)
            row = coverage[provider]
            row.episodes += 1
            source_chapters = _chapter_list(record, "source_chapters")
            chapters = _chapter_list(record, "chapters")
            row.source_chapters += bool(source_chapters)
            row.chapters += bool(chapters)
            if source_chapters or chapters:
                continue
            row.chapterless += 1
            stage = _chapter_stage(record)
            if stage == "complete-empty":
                row.attempted_empty += 1
            else:
                row.legacy_or_unknown += 1
            row.samples.append(_sample(record, slug=city.slug, uid=str(uid), stage=stage))

    for row in coverage.values():
        row.samples.sort(key=lambda sample: (sample.published, sample.uid), reverse=True)
        del row.samples[sample_size:]
        row.missing_record_stores.sort()
    return dict(sorted(coverage.items()))


def collect_benchmark_cohort(
    cities: Iterable, state_dir: Path, *, sample_size: int
) -> dict[str, ProviderBenchmark]:
    """Select canonical examples with all persisted locator inputs, grouped by provider."""
    cohort: defaultdict[str, ProviderBenchmark] = defaultdict(ProviderBenchmark)
    for city in cities:
        records = load_records(state_dir, source_key(city))
        for uid, record in records.items():
            if not isinstance(record, Mapping):
                continue
            sample = _benchmark_sample(record, slug=city.slug, uid=str(uid))
            if sample is None:
                continue
            provider = _episode_provider(record, city.provider)
            row = cohort[provider]
            row.episodes += 1
            row.by_body[sample.body] = row.by_body.get(sample.body, 0) + 1
            row.candidates.append(sample)
    for row in cohort.values():
        row.candidates.sort(key=lambda sample: (sample.published, sample.uid), reverse=True)
        row.samples = list(row.candidates)
        del row.samples[sample_size:]
        row.by_body = dict(sorted(row.by_body.items()))
    return dict(sorted(cohort.items()))


def _stratified_samples(row: ProviderBenchmark, *, limit: int) -> list[BenchmarkSample]:
    """Choose one newest example per body before filling remaining slots by recency."""
    selected: list[BenchmarkSample] = []
    seen_bodies: set[str] = set()
    for sample in row.candidates:
        if sample.body not in seen_bodies:
            selected.append(sample)
            seen_bodies.add(sample.body)
            if len(selected) == limit:
                return selected
    for sample in row.candidates:
        if sample not in selected:
            selected.append(sample)
            if len(selected) == limit:
                break
    return selected


def _longest_samples(row: ProviderBenchmark, *, limit: int) -> list[BenchmarkSample]:
    """Choose the longest durable recordings, retaining a deterministic tiebreaker."""
    return sorted(
        row.candidates,
        key=lambda sample: (sample.duration_seconds or 0.0, sample.published, sample.uid),
        reverse=True,
    )[:limit]


def measure_benchmark_samples(
    benchmark: Mapping[str, ProviderBenchmark],
    *,
    sample_size: int,
    fetch_bytes,
    selection="stratified",
) -> list[ArtifactMeasurement]:
    """Fetch a bounded stratified sample and estimate full-context input tokens.

    ``fetch_bytes`` is injected to keep selection and sizing deterministic in tests. The CLI
    supplies the guarded project HTTP session; this function never calls a model.
    """
    if selection not in {"stratified", "longest"}:
        raise ValueError(f"unknown benchmark selection {selection!r}")
    selector = _stratified_samples if selection == "stratified" else _longest_samples
    measurements: list[ArtifactMeasurement] = []
    for provider, row in benchmark.items():
        for sample in selector(row, limit=sample_size):
            try:
                transcript = fetch_bytes(sample.transcript_url)
                agenda = fetch_bytes(sample.agenda_text_url)
                transcript_text = transcript.decode("utf-8", errors="replace")
                agenda_text = agenda.decode("utf-8", errors="replace")
            except Exception as exc:
                measurements.append(
                    ArtifactMeasurement(
                        provider=provider,
                        slug=sample.slug,
                        uid=sample.uid,
                        body=sample.body,
                        transcript_bytes=None,
                        agenda_bytes=None,
                        transcript_tokens=None,
                        agenda_tokens=None,
                        combined_tokens=None,
                        error=str(exc),
                    )
                )
                continue
            transcript_tokens = estimate_tokens([{"content": transcript_text}])
            agenda_tokens = estimate_tokens([{"content": agenda_text}])
            measurements.append(
                ArtifactMeasurement(
                    provider=provider,
                    slug=sample.slug,
                    uid=sample.uid,
                    body=sample.body,
                    transcript_bytes=len(transcript),
                    agenda_bytes=len(agenda),
                    transcript_tokens=transcript_tokens,
                    agenda_tokens=agenda_tokens,
                    combined_tokens=transcript_tokens + agenda_tokens,
                )
            )
    return measurements


match_agenda_titles = match_title_candidates


def measure_title_candidates(
    benchmark: Mapping[str, ProviderBenchmark], *, sample_size: int, fetch_bytes
) -> list[TitleBenchmarkMeasurement]:
    """Evaluate structural main-agenda candidates against known provider chapter titles.

    The evaluator fetches only the persisted main-agenda artifact. It never fetches backup
    materials, calls a model, or changes episode state.
    """
    measurements: list[TitleBenchmarkMeasurement] = []
    for provider, row in benchmark.items():
        for sample in _stratified_samples(row, limit=sample_size):
            if not sample.canonical_titles:
                measurements.append(
                    TitleBenchmarkMeasurement(
                        provider=provider,
                        slug=sample.slug,
                        uid=sample.uid,
                        canonical_count=0,
                        candidate_count=0,
                        matched_count=0,
                        match_pct=None,
                        mean_similarity=None,
                        error="canonical chapters have no titles",
                    )
                )
                continue
            try:
                agenda_text = fetch_bytes(sample.agenda_text_url).decode("utf-8", errors="replace")
                candidates = extract_agenda_title_candidates(agenda_text)
                matches = match_title_candidates(sample.canonical_titles, candidates)
            except Exception as exc:
                measurements.append(
                    TitleBenchmarkMeasurement(
                        provider=provider,
                        slug=sample.slug,
                        uid=sample.uid,
                        canonical_count=len(sample.canonical_titles),
                        candidate_count=0,
                        matched_count=0,
                        match_pct=None,
                        mean_similarity=None,
                        error=str(exc),
                    )
                )
                continue
            matched_count = len(matches)
            measurements.append(
                TitleBenchmarkMeasurement(
                    provider=provider,
                    slug=sample.slug,
                    uid=sample.uid,
                    canonical_count=len(sample.canonical_titles),
                    candidate_count=len(candidates),
                    matched_count=matched_count,
                    match_pct=round(100 * matched_count / len(sample.canonical_titles), 1),
                    mean_similarity=round(sum(matches) / matched_count, 3) if matches else None,
                )
            )
    return measurements


def report_dict(
    coverage: Mapping[str, ProviderCoverage],
    benchmark: Mapping[str, ProviderBenchmark] | None = None,
) -> dict[str, object]:
    """Stable JSON-serializable report shape for issue evidence and downstream tooling."""
    providers = {}
    for provider, row in coverage.items():
        providers[provider] = {
            **asdict(row),
            "chapterless_pct": (
                round(100 * row.chapterless / row.episodes, 1) if row.episodes else 0.0
            ),
        }
    benchmark_providers = {
        provider: {
            "episodes": row.episodes,
            "by_body": row.by_body,
            "samples": [
                {
                    key: value
                    for key, value in asdict(sample).items()
                    if key
                    not in {"transcript_url", "agenda_text_url", "agenda_url", "canonical_titles"}
                }
                for sample in row.samples
            ],
        }
        for provider, row in (benchmark or {}).items()
    }
    return {"providers": providers, "benchmark": {"providers": benchmark_providers}}


def render_markdown(
    coverage: Mapping[str, ProviderCoverage],
    benchmark: Mapping[str, ProviderBenchmark] | None = None,
) -> str:
    """Render concise, paste-ready evidence for the consolidated Issue 1078 report."""
    lines = [
        "| Provider | Episodes | Chapterless | Stage complete-empty | Legacy / unknown |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for provider, row in coverage.items():
        percentage = 100 * row.chapterless / row.episodes if row.episodes else 0.0
        lines.append(
            f"| `{provider}` | {row.episodes} | {row.chapterless} ({percentage:.1f}%) | "
            f"{row.attempted_empty} | {row.legacy_or_unknown} |"
        )
    for provider, row in coverage.items():
        if row.missing_record_stores:
            shown = row.missing_record_stores[:10]
            remainder = len(row.missing_record_stores) - len(shown)
            lines.append(
                f"\n`{provider}` has no local materialized record store for "
                f"{len(row.missing_record_stores)} configured feed(s): "
                + ", ".join(f"`{slug}`" for slug in shown)
                + (f", and {remainder} more" if remainder else "")
                + "."
            )
        if row.samples:
            lines.extend([f"\n### `{provider}` chapterless samples", ""])
            for sample in row.samples:
                lines.append(
                    f"- `{sample.slug}` / `{sample.uid}` ({sample.published}; "
                    f"chapter stage: `{sample.chapter_stage}`): {sample.title} — {sample.video_url}"
                )
    if benchmark:
        lines.extend(
            [
                "\n## Canonical locator benchmark cohort",
                "",
                "Eligible rows have canonical chapters, hosted audio, a synced transcript and "
                "word-sidecar, plus a persisted main-agenda text artifact. "
                "The audit does not fetch those artifacts.",
                "",
                "| Provider | Eligible episodes | Bodies |",
                "| --- | ---: | --- |",
            ]
        )
        for provider, row in benchmark.items():
            bodies = ", ".join(f"{body} ({count})" for body, count in row.by_body.items())
            lines.append(f"| `{provider}` | {row.episodes} | {bodies} |")
        for provider, row in benchmark.items():
            if row.samples:
                lines.extend([f"\n### `{provider}` benchmark samples", ""])
                for sample in row.samples:
                    lines.append(
                        f"- `{sample.slug}` / `{sample.uid}` ({sample.published}; "
                        f"{sample.chapter_count} chapters): {sample.title}"
                    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--site-config", default="config/site_config.yml")
    parser.add_argument("--output-dir", default="docs")
    parser.add_argument(
        "--state-dir",
        type=Path,
        help="read this local state directory instead of resolving it from site config",
    )
    parser.add_argument(
        "--measure-longest",
        type=int,
        default=0,
        help="fetch this many longest benchmark artifacts per provider and estimate input tokens",
    )
    parser.add_argument(
        "--pull-state",
        action="store_true",
        help="refresh the local state mirror from canonical object storage before reading",
    )
    parser.add_argument("--sample-size", type=int, default=5)
    parser.add_argument(
        "--measure-samples",
        type=int,
        default=0,
        help=(
            "fetch this many stratified benchmark artifacts per provider and estimate input tokens"
        ),
    )
    parser.add_argument(
        "--title-benchmark-samples",
        type=int,
        default=0,
        help=(
            "fetch this many stratified main-agenda artifacts per provider and compare "
            "structural title candidates with canonical chapter titles"
        ),
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)
    if args.sample_size < 1:
        parser.error("--sample-size must be at least 1")
    if args.measure_samples < 0:
        parser.error("--measure-samples must be non-negative")
    if args.measure_longest < 0:
        parser.error("--measure-longest must be non-negative")
    if args.title_benchmark_samples < 0:
        parser.error("--title-benchmark-samples must be non-negative")
    if args.measure_samples and args.measure_longest:
        parser.error("--measure-samples and --measure-longest cannot be combined")
    if args.pull_state and args.state_dir:
        parser.error("--pull-state cannot be combined with --state-dir")

    site_config = load_site_config(args.site_config)
    cities = load_city_configs(args.config_dir, site_config.get("defaults", {}))
    state_dir = (
        pull_canonical_state(site_config, args.output_dir)
        if args.pull_state
        else args.state_dir or resolve_state_dir(site_config, Path(args.output_dir))
    )
    coverage = collect_coverage(cities, Path(state_dir), sample_size=args.sample_size)
    benchmark = collect_benchmark_cohort(cities, Path(state_dir), sample_size=args.sample_size)
    measurements: list[ArtifactMeasurement] = []
    title_measurements: list[TitleBenchmarkMeasurement] = []
    measure_count = args.measure_samples or args.measure_longest
    if measure_count or args.title_benchmark_samples:
        session = make_session()

        def fetch_bytes(url: str) -> bytes:
            response = session.get(url, timeout=30)
            response.raise_for_status()
            return response.content

        if measure_count:
            measurements = measure_benchmark_samples(
                benchmark,
                sample_size=measure_count,
                fetch_bytes=fetch_bytes,
                selection="longest" if args.measure_longest else "stratified",
            )
        if args.title_benchmark_samples:
            title_measurements = measure_title_candidates(
                benchmark,
                sample_size=args.title_benchmark_samples,
                fetch_bytes=fetch_bytes,
            )
    if args.json:
        report = report_dict(coverage, benchmark)
        report["benchmark"]["measurements"] = [asdict(row) for row in measurements]
        report["benchmark"]["title_measurements"] = [asdict(row) for row in title_measurements]
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_markdown(coverage, benchmark))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
