#!/usr/bin/env python
"""Build a read-only transcript-boundary locator benchmark manifest (GH#1078).

This is the first, offline/model-free slice of the locator plan.  It selects a deterministic,
body-diverse cohort from records that already have canonical chapter timings, a synced transcript,
word-sidecar/VTT timing, and a stored agenda-text artifact.  When artifact fetching is enabled it
measures the exact timed-unit request assembled by :mod:`citypods.chapter_locator`, records the
Mistral/Gemini context decision, and reports the independent agenda-title evidence join.

The output is research evidence only.  It never writes episode state, calls a model, or publishes
chapters.  Keep manifests outside the repository (for example under ``/private/tmp``) unless a
small aggregate is intentionally copied into ``review/40``.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

try:  # Works both as a package import in tests and as a directly-run research script.
    from .audit_chapters import BenchmarkSample, collect_benchmark_cohort
except ImportError:  # pragma: no cover - exercised by the CLI invocation path
    from audit_chapters import BenchmarkSample, collect_benchmark_cohort

from citypods.agenda_text import classify_agenda_text, extract_agenda_title_candidates
from citypods.bodies import body_key, canonical_body
from citypods.chapter_locator import build_locator_request, build_locator_units
from citypods.chapter_titles import match_title_candidates
from citypods.compute.llm_policy import estimate_tokens
from citypods.config import load_city_configs, load_site_config
from citypods.http import make_session

DATASET_VERSION = 1
DURATION_BUCKETS = ("under-2h", "2-to-4h", "4-to-8h", "8h-plus", "unknown")


@dataclass(frozen=True)
class LocatorBenchmarkMeasurement:
    """Artifact and context measurements for one selected canonical example."""

    provider: str
    slug: str
    uid: str
    body: str
    published: str
    duration_seconds: float | None
    duration_bucket: str
    canonical_count: int
    canonical_timed_count: int
    agenda_candidate_count: int | None
    agenda_matched_count: int | None
    agenda_match_pct: float | None
    agenda_artifact_class: str | None
    transcript_bytes: int | None
    words_bytes: int | None
    agenda_bytes: int | None
    transcript_tokens: int | None
    words_tokens: int | None
    agenda_tokens: int | None
    locator_input_tokens: int | None
    locator_model: str | None
    locator_unit_count: int | None
    locator_unit_source: str | None
    error: str | None = None
    words_error: str | None = None


def duration_bucket(seconds: float | None) -> str:
    """Return stable long-meeting strata used before any model evaluation."""
    if seconds is None or seconds < 0:
        return "unknown"
    if seconds < 2 * 60 * 60:
        return "under-2h"
    if seconds < 4 * 60 * 60:
        return "2-to-4h"
    if seconds < 8 * 60 * 60:
        return "4-to-8h"
    return "8h-plus"


def classify_agenda_artifact(text: str, *, candidate_count: int) -> str:
    """Classify a fetched agenda artifact for eligibility diagnostics, not admission."""
    shared_class = classify_agenda_text(text)
    if shared_class != "complete":
        return shared_class
    return "structural-candidates" if candidate_count else "no-structural-candidates"


def _dedupe_candidates(
    benchmark: Mapping[str, object],
) -> dict[str, list[BenchmarkSample]]:
    """Collapse multiple feed projections of one provider/UID before selecting a cohort."""
    deduped: dict[str, list[BenchmarkSample]] = {}
    for provider, row in benchmark.items():
        candidates = getattr(row, "candidates", ())
        by_uid: dict[str, BenchmarkSample] = {}
        for sample in candidates:
            # A shared source can project the same meeting through several configured feeds.  The
            # UID is stable across those projections; retain a deterministic slug for evidence.
            previous = by_uid.get(sample.uid)
            if previous is None or (sample.slug, sample.body) < (previous.slug, previous.body):
                by_uid[sample.uid] = sample
        deduped[provider] = sorted(
            by_uid.values(), key=lambda sample: (sample.published, sample.uid), reverse=True
        )
    return deduped


def select_locator_samples(
    benchmark: Mapping[str, object], *, per_provider: int, vtt_per_provider: int = 0
) -> dict[str, list[BenchmarkSample]]:
    """Select a deterministic provider × duration × body-diverse locator cohort.

    The first pass takes at most one recent meeting per body in each duration bucket.  Remaining
    slots are filled by recency, still deduplicated by UID.  This is a selector, not a claim that
    the resulting sample is statistically representative; its purpose is to expose short,
    ordinary, long, and extreme-context meetings before spending model quota.
    """
    if per_provider < 1:
        raise ValueError("per_provider must be positive")
    if vtt_per_provider < 0 or vtt_per_provider > per_provider:
        raise ValueError("vtt_per_provider must be between zero and per_provider")
    deduped = _dedupe_candidates(benchmark)
    selected: dict[str, list[BenchmarkSample]] = {}
    for provider, candidates in deduped.items():
        buckets: dict[str, list[BenchmarkSample]] = defaultdict(list)
        for sample in candidates:
            buckets[duration_bucket(sample.duration_seconds)].append(sample)
        for rows in buckets.values():
            rows.sort(key=lambda sample: (sample.body, sample.published, sample.uid), reverse=True)

        fallback_candidates = [
            sample for sample in candidates if sample.words_key is None and sample.words_url is None
        ]
        chosen: list[BenchmarkSample] = fallback_candidates[:vtt_per_provider]
        chosen_uids: set[str] = {sample.uid for sample in chosen}
        # Round-robin buckets so a provider with a long tail does not become a recent-short-only
        # benchmark.  Within each bucket, prefer a new body before a second meeting from a body.
        body_seen: set[tuple[str, str]] = set()
        while len(chosen) < per_provider:
            progressed = False
            for bucket in DURATION_BUCKETS:
                rows = buckets.get(bucket, [])
                candidate = next(
                    (
                        sample
                        for sample in rows
                        if sample.uid not in chosen_uids
                        and (bucket, body_key(canonical_body(sample.body))) not in body_seen
                    ),
                    None,
                )
                if candidate is None:
                    continue
                chosen.append(candidate)
                chosen_uids.add(candidate.uid)
                body_seen.add((bucket, body_key(canonical_body(candidate.body))))
                progressed = True
                if len(chosen) == per_provider:
                    break
            if not progressed:
                break
        if len(chosen) < per_provider:
            for sample in candidates:
                if sample.uid in chosen_uids:
                    continue
                chosen.append(sample)
                chosen_uids.add(sample.uid)
                if len(chosen) == per_provider:
                    break
        selected[provider] = sorted(chosen, key=lambda sample: (sample.published, sample.uid))
    return selected


def cohort_summary(benchmark: Mapping[str, object]) -> dict[str, object]:
    """Summarize eligible and UID-deduplicated rows without fetching any artifact."""
    deduped = _dedupe_candidates(benchmark)
    providers: dict[str, object] = {}
    for provider, row in benchmark.items():
        candidates = list(getattr(row, "candidates", ()))
        unique = deduped.get(provider, [])
        body_counts: dict[str, int] = defaultdict(int)
        buckets: dict[str, int] = defaultdict(int)
        timing_sources: dict[str, int] = defaultdict(int)
        for sample in unique:
            body_counts[body_key(canonical_body(sample.body))] += 1
            buckets[duration_bucket(sample.duration_seconds)] += 1
            timing_sources["words" if sample.words_key or sample.words_url else "vtt"] += 1
        providers[provider] = {
            "eligible_feed_rows": len(candidates),
            "uid_deduplicated_episodes": len(unique),
            "body_count": len(body_counts),
            "duration_buckets": {bucket: buckets.get(bucket, 0) for bucket in DURATION_BUCKETS},
            "timing_sources": {
                source: timing_sources.get(source, 0) for source in ("words", "vtt")
            },
            "bodies": dict(sorted(body_counts.items())),
        }
    return {"providers": providers}


def _structural_items(text: str):
    """Create bounded locator items for sizing only from complete source lines.

    Phase 0 does not have the LLM title/evidence extraction output yet, so it uses the existing
    structural candidate's original agenda line as both evidence and a sizing cue.  This avoids a
    title-only lower bound while keeping the helper honest: the later production packet will add
    any adjacent ID/number lines and independently validated locator cues.
    """
    candidates = extract_agenda_title_candidates(text)
    # Import lazily to keep this research helper's import surface small.
    from citypods.chapter_locator import LocatorAgendaItem

    lines = text.splitlines()
    items = []
    for index, candidate in enumerate(candidates):
        line = (
            lines[candidate.line_number - 1].strip()
            if 0 < candidate.line_number <= len(lines)
            else ""
        )
        evidence = line or candidate.title
        items.append(
            LocatorAgendaItem(
                index=index,
                title=candidate.title,
                evidence_text=evidence,
                locator_cues=tuple(dict.fromkeys((candidate.title, evidence))),
            )
        )
    return candidates, tuple(items)


def measure_locator_samples(
    selected: Mapping[str, list[BenchmarkSample]], *, fetch_bytes
) -> list[LocatorBenchmarkMeasurement]:
    """Fetch selected artifacts and measure the exact existing locator request, never a model."""
    measurements: list[LocatorBenchmarkMeasurement] = []
    for provider, samples in selected.items():
        for sample in samples:
            base = dict(
                provider=provider,
                slug=sample.slug,
                uid=sample.uid,
                body=sample.body,
                published=sample.published,
                duration_seconds=sample.duration_seconds,
                duration_bucket=duration_bucket(sample.duration_seconds),
                canonical_count=sample.chapter_count,
                canonical_timed_count=min(len(sample.canonical_starts), len(sample.canonical_ends)),
            )
            words_error = None
            try:
                transcript = fetch_bytes(sample.transcript_url)
                agenda = fetch_bytes(sample.agenda_text_url)
                transcript_text = transcript.decode("utf-8", errors="replace")
                agenda_text = agenda.decode("utf-8", errors="replace")
                transcript_tokens = estimate_tokens([{"content": transcript_text}])
                agenda_tokens = estimate_tokens([{"content": agenda_text}])
            except Exception as exc:  # research reports retain failures rather than hiding them
                measurements.append(
                    LocatorBenchmarkMeasurement(
                        **base,
                        agenda_candidate_count=None,
                        agenda_matched_count=None,
                        agenda_match_pct=None,
                        agenda_artifact_class=None,
                        transcript_bytes=None,
                        words_bytes=None,
                        agenda_bytes=None,
                        transcript_tokens=None,
                        words_tokens=None,
                        agenda_tokens=None,
                        locator_input_tokens=None,
                        locator_model=None,
                        locator_unit_count=None,
                        locator_unit_source=None,
                        error=str(exc),
                        words_error=None,
                    )
                )
                continue

            words = None
            if sample.words_url:
                try:
                    words = fetch_bytes(sample.words_url)
                except Exception as exc:  # VTT fallback remains valid when the sidecar is absent.
                    words_error = str(exc)
            words_tokens = (
                estimate_tokens([{"content": words.decode("utf-8", errors="replace")}])
                if words
                else None
            )
            units, unit_source = build_locator_units(words_data=words, vtt_data=transcript)
            candidates, items = _structural_items(agenda_text)
            matches = match_title_candidates(sample.canonical_titles, candidates)
            agenda_class = classify_agenda_artifact(agenda_text, candidate_count=len(candidates))
            request = build_locator_request(items, units) if units and items else None
            error = None
            if not units:
                error = "no timed transcript units in word sidecar or VTT"
            elif not items:
                error = "agenda text produced no structural candidates"
            measurements.append(
                LocatorBenchmarkMeasurement(
                    **base,
                    agenda_candidate_count=len(candidates),
                    agenda_matched_count=len(matches),
                    agenda_match_pct=round(100 * len(matches) / sample.chapter_count, 1)
                    if sample.chapter_count
                    else None,
                    agenda_artifact_class=agenda_class,
                    transcript_bytes=len(transcript),
                    words_bytes=len(words) if words is not None else None,
                    agenda_bytes=len(agenda),
                    transcript_tokens=transcript_tokens,
                    words_tokens=words_tokens,
                    agenda_tokens=agenda_tokens,
                    locator_input_tokens=request.input_tokens if request else None,
                    locator_model=request.model if request else None,
                    locator_unit_count=len(units),
                    locator_unit_source=unit_source,
                    error=error,
                    words_error=words_error,
                )
            )
    return measurements


def build_manifest(
    benchmark: Mapping[str, object],
    *,
    per_provider: int,
    vtt_per_provider: int = 0,
    measurements: list[LocatorBenchmarkMeasurement],
) -> dict[str, object]:
    selected = select_locator_samples(
        benchmark, per_provider=per_provider, vtt_per_provider=vtt_per_provider
    )
    return {
        "version": DATASET_VERSION,
        "purpose": "offline transcript-boundary locator sizing and eligibility",
        "selection": {
            "per_provider": per_provider,
            "vtt_per_provider": vtt_per_provider,
            "duration_buckets": list(DURATION_BUCKETS),
        },
        "cohort": cohort_summary(benchmark),
        "selected": {
            provider: [
                {
                    "slug": sample.slug,
                    "uid": sample.uid,
                    "body": sample.body,
                    "published": sample.published,
                    "duration_seconds": sample.duration_seconds,
                    "duration_bucket": duration_bucket(sample.duration_seconds),
                    "chapter_count": sample.chapter_count,
                }
                for sample in samples
            ]
            for provider, samples in selected.items()
        },
        "measurements": [asdict(row) for row in measurements],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--per-provider", type=int, default=12)
    parser.add_argument(
        "--include-vtt-fallback",
        action="store_true",
        help="include synced VTT-only rows and force one per provider when available",
    )
    parser.add_argument(
        "--cohort-only",
        action="store_true",
        help="write the eligibility/selection manifest without fetching public artifacts",
    )
    args = parser.parse_args(argv)
    if args.per_provider < 1:
        parser.error("--per-provider must be positive")

    site = load_site_config("config/site_config.yml")
    cities = load_city_configs("config", site.get("defaults", {}))
    benchmark = collect_benchmark_cohort(
        cities,
        args.state_dir,
        sample_size=999999,
        allow_vtt_fallback=args.include_vtt_fallback,
    )
    vtt_per_provider = 1 if args.include_vtt_fallback else 0
    selected = select_locator_samples(
        benchmark, per_provider=args.per_provider, vtt_per_provider=vtt_per_provider
    )
    measurements: list[LocatorBenchmarkMeasurement] = []
    if not args.cohort_only:
        session = make_session()

        def fetch_bytes(url: str) -> bytes:
            response = session.get(url, timeout=30)
            response.raise_for_status()
            return response.content

        measurements = measure_locator_samples(selected, fetch_bytes=fetch_bytes)
    manifest = build_manifest(
        benchmark,
        per_provider=args.per_provider,
        vtt_per_provider=vtt_per_provider,
        measurements=measurements,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps({"output": str(args.output), "measurements": len(measurements)}, sort_keys=True)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
