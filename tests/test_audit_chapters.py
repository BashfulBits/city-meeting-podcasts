"""Tests for the read-only materialized-chapter coverage audit (GH#1078)."""

from __future__ import annotations

from types import SimpleNamespace

from citypods.agenda_text import AgendaTitleCandidate
from scripts.research.agenda_chapters.audit_chapters import (
    ProviderBenchmark,
    collect_benchmark_cohort,
    collect_coverage,
    match_agenda_titles,
    measure_benchmark_samples,
    measure_title_candidates,
    render_markdown,
    report_dict,
)


def _record(
    *, source_chapters=None, chapters=None, stage=None, provider=None, published="2026-07-01"
):
    record = {
        "source_chapters": source_chapters,
        "chapters": chapters,
        "published": published,
        "title": "Council meeting",
        "video_url": "https://example.test/watch",
    }
    if stage is not None:
        record["stage_completion"] = {"chapters": {"state": stage}}
    if provider is not None:
        record["sources"] = [{"provider": provider}]
    return record


def _benchmark_record(**overrides):
    record = _record(source_chapters=[{"start": 0}], chapters=[{"start": 0}])
    record.update(
        {
            "audio": {"key": "audio/source/uid.m4a"},
            "body": "City Council",
            "links": {
                "agenda": "https://provider.test/agenda",
                "agenda_text_artifact_key": "documents/source/uid/agenda_text.txt",
                "agenda_text_artifact": "https://objects.test/agenda.txt",
            },
            "transcript": {
                "key": "transcripts/source/uid.vtt",
                "url": "https://objects.test/transcript.vtt",
                "words_key": "transcripts/source/uid.words.json",
                "synced": True,
            },
        }
    )
    record.update(overrides)
    return record


def test_collect_coverage_separates_confirmed_empty_from_legacy(monkeypatch, tmp_path):
    city = SimpleNamespace(slug="city-a", provider="configured", source={}, source_id="source-a")
    records = {
        "empty": _record(stage="complete-empty", published="2026-07-03"),
        "legacy": _record(published="2026-07-02"),
        "source": _record(source_chapters=[{"start": 0}], chapters=[{"start": 0}]),
        "served": _record(chapters=[{"start": 10}], provider="actual"),
    }
    monkeypatch.setattr(
        "scripts.research.agenda_chapters.audit_chapters.load_records", lambda *_args: records
    )

    coverage = collect_coverage([city], tmp_path, sample_size=1)

    configured = coverage["configured"]
    assert (configured.episodes, configured.source_chapters, configured.chapters) == (3, 1, 1)
    assert (
        configured.chapterless,
        configured.attempted_empty,
        configured.legacy_or_unknown,
    ) == (2, 1, 1)
    assert [sample.uid for sample in configured.samples] == ["empty"]
    assert coverage["actual"].episodes == 1
    assert coverage["actual"].chapters == 1


def test_report_rendering_includes_percentage_and_missing_store(monkeypatch, tmp_path):
    city = SimpleNamespace(
        slug="missing-city", provider="civicplus", source={}, source_id="missing"
    )
    monkeypatch.setattr(
        "scripts.research.agenda_chapters.audit_chapters.load_records", lambda *_args: {}
    )

    coverage = collect_coverage([city], tmp_path, sample_size=5)

    assert report_dict(coverage)["providers"]["civicplus"]["chapterless_pct"] == 0.0
    assert "Stage complete-empty" in render_markdown(coverage)
    assert (
        "`civicplus` has no local materialized record store for 1 configured feed(s): "
        "`missing-city`."
    ) in render_markdown(coverage)


def test_collect_benchmark_requires_every_persisted_locator_input(monkeypatch, tmp_path):
    city = SimpleNamespace(slug="city-a", provider="granicus", source={}, source_id="source-a")
    records = {
        "eligible": _benchmark_record(published="2026-07-03"),
        "legacy-audio": _benchmark_record(audio=None, audio_key="audio/source/legacy.m4a"),
        "no-words": _benchmark_record(
            transcript={"key": "transcripts/source/other.vtt", "synced": True}
        ),
        "no-audio": _benchmark_record(audio={}),
        "not-synced": _benchmark_record(
            transcript={
                "key": "transcripts/source/other.vtt",
                "words_key": "transcripts/source/other.words.json",
                "synced": False,
            }
        ),
    }
    monkeypatch.setattr(
        "scripts.research.agenda_chapters.audit_chapters.load_records", lambda *_args: records
    )

    cohort = collect_benchmark_cohort([city], tmp_path, sample_size=5)

    assert cohort["granicus"].episodes == 2
    assert cohort["granicus"].by_body == {"City Council": 2}
    assert cohort["granicus"].samples[0].uid == "eligible"
    report = render_markdown({}, cohort)
    assert "Canonical locator benchmark cohort" in report
    assert "`granicus` | 2 | City Council (2)" in report
    assert report_dict({}, cohort)["benchmark"]["providers"]["granicus"]["episodes"] == 2


def test_measurement_is_bounded_stratified_and_uses_shared_token_estimate(monkeypatch, tmp_path):
    city = SimpleNamespace(slug="city-a", provider="granicus", source={}, source_id="source-a")
    records = {
        "new-council": _benchmark_record(body="Council", published="2026-07-03"),
        "planning": _benchmark_record(body="Planning", published="2026-07-02"),
        "old-council": _benchmark_record(body="Council", published="2026-07-01"),
    }
    monkeypatch.setattr(
        "scripts.research.agenda_chapters.audit_chapters.load_records", lambda *_args: records
    )
    cohort = collect_benchmark_cohort([city], tmp_path, sample_size=1)
    fetched = []

    def fetch(url):
        fetched.append(url)
        return b"agenda words" if url.endswith("agenda.txt") else b"WEBVTT\n\nTranscript words"

    measured = measure_benchmark_samples(cohort, sample_size=2, fetch_bytes=fetch)

    assert [row.uid for row in measured] == ["new-council", "planning"]
    assert len(fetched) == 4
    assert all(row.combined_tokens and row.combined_tokens > 0 for row in measured)
    assert isinstance(cohort["granicus"], ProviderBenchmark)


def test_measurement_can_target_longest_durable_inputs(monkeypatch, tmp_path):
    city = SimpleNamespace(slug="city-a", provider="granicus", source={}, source_id="source-a")
    records = {
        "short": _benchmark_record(audio={"key": "audio/short.m4a", "duration_served": 30}),
        "long": _benchmark_record(audio={"key": "audio/long.m4a", "duration_served": 3600}),
    }
    monkeypatch.setattr(
        "scripts.research.agenda_chapters.audit_chapters.load_records", lambda *_args: records
    )
    cohort = collect_benchmark_cohort([city], tmp_path, sample_size=1)

    measured = measure_benchmark_samples(
        cohort, sample_size=1, selection="longest", fetch_bytes=lambda _url: b"text"
    )

    assert [row.uid for row in measured] == ["long"]


def test_title_matching_is_one_to_one_and_requires_high_similarity():
    matches = match_agenda_titles(
        ["Consent Agenda", "Public Hearing"],
        [
            AgendaTitleCandidate("2. CONSENT AGENDA", 1),
            AgendaTitleCandidate("PUBLIC HEARINGS", 2),
            AgendaTitleCandidate("Consent agenda", 3),
        ],
    )

    assert matches == [1.0, 1.0]
    assert match_agenda_titles(["Budget"], [AgendaTitleCandidate("Parks", 1)]) == []


def test_title_benchmark_fetches_only_main_agenda_and_reports_candidate_gap(monkeypatch, tmp_path):
    city = SimpleNamespace(slug="city-a", provider="granicus", source={}, source_id="source-a")
    record = _benchmark_record(
        source_chapters=[{"title": "CALL TO ORDER"}, {"title": "Consent Agenda"}]
    )
    monkeypatch.setattr(
        "scripts.research.agenda_chapters.audit_chapters.load_records",
        lambda *_args: {"eligible": record},
    )
    cohort = collect_benchmark_cohort([city], tmp_path, sample_size=1)
    fetched = []

    def fetch(url):
        fetched.append(url)
        return b"AGENDA\nCALL TO ORDER\n2. CONSENT AGENDA\nEXTRA HEADING\n"

    measured = measure_title_candidates(cohort, sample_size=1, fetch_bytes=fetch)

    assert fetched == ["https://objects.test/agenda.txt"]
    assert measured[0].canonical_count == 2
    assert measured[0].candidate_count == 3
    assert measured[0].matched_count == 2
    assert measured[0].match_pct == 100.0
