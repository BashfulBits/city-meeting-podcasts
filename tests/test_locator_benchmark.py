"""Offline tests for the transcript-boundary benchmark selector and measurements."""

from __future__ import annotations

from types import SimpleNamespace

from scripts.research.agenda_chapters.audit_chapters import BenchmarkSample
from scripts.research.agenda_chapters.build_locator_benchmark import (
    classify_agenda_artifact,
    cohort_summary,
    duration_bucket,
    measure_locator_samples,
    select_locator_samples,
)


def _sample(
    uid: str,
    *,
    body: str,
    duration: float | None,
    published: str = "2026-07-01T00:00:00+00:00",
    vtt_only: bool = False,
) -> BenchmarkSample:
    return BenchmarkSample(
        slug="city-feed",
        uid=uid,
        published=published,
        body=body,
        title="Meeting",
        chapter_count=2,
        canonical_titles=("1. Call to Order", "2. Budget"),
        duration_seconds=duration,
        transcript_key=f"transcripts/{uid}.vtt",
        words_key=None if vtt_only else f"transcripts/{uid}.words.json",
        words_url=None if vtt_only else f"https://objects.test/{uid}.words.json",
        agenda_text_key=f"documents/{uid}.txt",
        transcript_url=f"https://objects.test/{uid}.vtt",
        agenda_text_url=f"https://objects.test/{uid}.txt",
        agenda_url="https://provider.test/agenda",
        canonical_starts=(1.0, 10.0),
        canonical_ends=(10.0, 20.0),
    )


def test_duration_buckets_expose_extreme_meetings():
    assert duration_bucket(None) == "unknown"
    assert duration_bucket(7199) == "under-2h"
    assert duration_bucket(7200) == "2-to-4h"
    assert duration_bucket(14400) == "4-to-8h"
    assert duration_bucket(28800) == "8h-plus"


def test_selector_deduplicates_feed_projections_and_round_robins_buckets():
    benchmark = {
        "granicus": SimpleNamespace(
            candidates=[
                _sample("same", body="CITY COUNCIL", duration=100),
                _sample("same", body="City Council", duration=100),
                _sample("long", body="Planning Commission", duration=30_000),
                _sample("medium", body="Board", duration=9_000),
            ]
        )
    }

    selected = select_locator_samples(benchmark, per_provider=3)["granicus"]

    assert {sample.uid for sample in selected} == {"same", "long", "medium"}
    assert len(selected) == 3
    summary = cohort_summary(benchmark)["providers"]["granicus"]
    assert summary["eligible_feed_rows"] == 4
    assert summary["uid_deduplicated_episodes"] == 3
    assert summary["body_count"] == 3


def test_selector_can_force_a_vtt_fallback_row():
    benchmark = {
        "swagit": SimpleNamespace(
            candidates=[
                _sample("words", body="Council", duration=100),
                _sample("vtt", body="Planning", duration=200, vtt_only=True),
            ]
        )
    }

    selected = select_locator_samples(benchmark, per_provider=1, vtt_per_provider=1)["swagit"]

    assert [sample.uid for sample in selected] == ["vtt"]
    assert cohort_summary(benchmark)["providers"]["swagit"]["timing_sources"] == {
        "words": 1,
        "vtt": 1,
    }


def test_agenda_artifact_classification_distinguishes_placeholders():
    assert classify_agenda_artifact("", candidate_count=0) == "empty"
    assert (
        classify_agenda_artifact("Loading… DocumentViewer.php", candidate_count=0)
        == "viewer-placeholder"
    )
    assert (
        classify_agenda_artifact("This agenda is not currently published.", candidate_count=0)
        == "unpublished-placeholder"
    )
    assert classify_agenda_artifact("A heading", candidate_count=0) == "no-structural-candidates"
    assert (
        classify_agenda_artifact("1. Call to Order", candidate_count=1) == "structural-candidates"
    )


def test_measurement_uses_words_then_vtt_and_reports_source_join():
    sample = _sample("one", body="Council", duration=100)
    vtt = (
        b"WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nCall to order\n\n"
        b"00:00:10.000 --> 00:00:12.000\nBudget\n"
    )
    words = (
        b'{"segments":[{"start":1,"end":3,"text":"Call to order"},'
        b'{"start":10,"end":12,"text":"Budget"}]}'
    )
    agenda = b"1. Call to Order\n2. Budget\n"

    def fetch(url: str) -> bytes:
        return {
            sample.transcript_url: vtt,
            sample.words_url: words,
            sample.agenda_text_url: agenda,
        }[url]

    rows = measure_locator_samples({"granicus": [sample]}, fetch_bytes=fetch)

    assert len(rows) == 1
    row = rows[0]
    assert row.error is None
    assert row.locator_unit_source == "words"
    assert row.locator_unit_count == 2
    assert row.agenda_candidate_count == 2
    assert row.agenda_matched_count == 2
    assert row.agenda_match_pct == 100.0
    assert row.locator_model
    assert row.locator_input_tokens > 0


def test_measurement_retains_timing_and_artifact_sizes_when_agenda_has_no_candidates():
    sample = _sample("empty-agenda", body="Council", duration=100)
    vtt = b"WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nCall to order\n"
    words = b'{"segments":[{"start":1,"end":3,"text":"Call to order"}]}'
    agenda = b"Loading..."

    def fetch(url: str) -> bytes:
        return {
            sample.transcript_url: vtt,
            sample.words_url: words,
            sample.agenda_text_url: agenda,
        }[url]

    row = measure_locator_samples({"granicus": [sample]}, fetch_bytes=fetch)[0]

    assert row.error == "agenda text produced no structural candidates"
    assert row.locator_unit_count == 1
    assert row.locator_unit_source == "words"
    assert row.transcript_bytes == len(vtt)
    assert row.agenda_bytes == len(agenda)
    assert row.agenda_candidate_count == 0
    assert row.locator_input_tokens is None
