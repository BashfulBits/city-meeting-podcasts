"""Tests for incremental builds: content-hash change detection + state persistence."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

import pytest

from citypods import run
from citypods.compute.base import InferenceJob, JobHandle
from citypods.compute.llm import BatchDispatchOutcome, LLMBackendError
from citypods.models import AgendaRecord, CalendarIndex, City, Episode
from citypods.providers import get_provider, register
from citypods.providers.base import ProviderError
from citypods.records import episode_to_record, feed_content_hash, meeting_page_hash
from citypods.stages import StageContext, StageStats
from citypods.state import build_fingerprint
from citypods.timeline import Segment, Timeline


def _ep(guid="g1", title="City Council", hosted=None):
    return Episode(
        guid=guid,
        uid=f"uid-{guid}",
        title=title,
        published=datetime(2026, 5, 1, tzinfo=UTC),
        video_url=f"https://x/{guid}.mp4",
        duration=600,
        body="City Council",
        hosted_audio_url=hosted,
    )


# --- pure content-hash / fingerprint units ---------------------------------------------


def test_content_hash_stable_and_order_independent():
    fp = "fp0"
    a = [_ep("g1"), _ep("g2")]
    b = [_ep("g2"), _ep("g1")]  # reversed
    assert feed_content_hash(a, fp) == feed_content_hash(b, fp)


def test_content_hash_changes_with_episodes_and_fingerprint():
    base = feed_content_hash([_ep("g1")], "fp0")
    assert base != feed_content_hash([_ep("g1", title="Different")], "fp0")
    assert base != feed_content_hash([_ep("g1")], "fp1")  # fingerprint bust
    # A newly-hosted enclosure changes the hash so the feed re-renders.
    assert base != feed_content_hash([_ep("g1", hosted="https://cdn/g1.m4a")], "fp0")


def test_meeting_page_hash_tracks_transcript_and_availability_render_inputs():
    from citypods.availability import CONFIRMED_EMPTY, MediaAvailability

    ep = _ep()
    base = meeting_page_hash(ep)

    ep.transcript_format = "vtt"
    assert meeting_page_hash(ep) != base

    ep.media_availability = MediaAvailability(
        state=CONFIRMED_EMPTY,
        reason="empty file",
        last_check="2026-07-01T00:00:00+00:00",
        recovered_at=None,
    )
    changed = meeting_page_hash(ep)
    assert changed != base

    ep.media_availability = MediaAvailability(
        state=CONFIRMED_EMPTY,
        reason="updated operator note",
        last_check="2026-07-02T00:00:00+00:00",
        recovered_at="2026-07-03T00:00:00+00:00",
    )
    assert meeting_page_hash(ep) != changed


def test_build_fingerprint_tracks_base_url_and_templates():
    assert build_fingerprint("https://a") != build_fingerprint("https://b")
    assert build_fingerprint("https://a") == build_fingerprint("https://a")


def test_refresh_health_summary_reports_canonical_and_oldest_source_age():
    now = datetime(2026, 7, 25, 12, tzinfo=UTC)
    state = {
        "fresh": {
            "last_success": (now - timedelta(hours=2)).isoformat(),
            "next_poll_at": (now + timedelta(hours=2)).isoformat(),
        },
        "stale": {
            "last_success": (now - timedelta(hours=26)).isoformat(),
            "next_poll_at": (now - timedelta(hours=1)).isoformat(),
            "last_error": "provider unavailable",
        },
        "legacy-naive": {
            "last_success": (now - timedelta(hours=3)).isoformat(),
            "next_poll_at": (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S"),
        },
        "uninitialized": {},
    }

    summary = run._refresh_health_summary(state, now=now)

    assert summary == {
        "sources": 4,
        "sources_with_success": 3,
        "sources_with_errors": 1,
        "sources_due": 2,
        "canonical_state_age_seconds": 7200.0,
        "oldest_source_refresh_age_seconds": 93600.0,
    }


def test_print_refresh_health_exposes_no_refresh_and_source_age(capsys):
    now = datetime(2026, 7, 25, 12, tzinfo=UTC)
    state = {"source": {"last_success": (now - timedelta(hours=4)).isoformat()}}

    # The helper uses the current clock for log output; assert its stable contract rather than
    # depending on a wall-clock value in the age text.
    run._print_refresh_health(state, phase="render", no_refresh=True)

    output = capsys.readouterr().out
    assert "canonical state: phase=render mode=no-refresh" in output
    assert "sources=1 successful=1" in output
    assert "canonical_age=" in output and "oldest_source_refresh_age=" in output


def test_build_closes_compute_backend_when_impl_raises(monkeypatch):
    class _Backend:
        closed = False

        def close(self):
            self.closed = True

    backend = _Backend()

    def _fail(**kwargs):
        kwargs["_compute_backend_holder"].append(backend)
        raise RuntimeError("boom")

    monkeypatch.setattr(run, "_build_impl", _fail)

    with pytest.raises(RuntimeError, match="boom"):
        run.build()
    assert backend.closed is True


def test_chapter_batch_replay_uses_real_outcomes_and_records_submission_errors():
    agenda = _ep("agenda")
    agenda.generated_agenda_candidates = {
        "status": "pending",
        "recipe": "agenda-recipe",
        "job_ref": "batch-pending:agenda-recipe",
    }
    failed = _ep("failed")
    failed.generated_agenda_candidates = {
        "status": "pending",
        "recipe": "failed-recipe",
        "job_ref": "batch-pending:failed-recipe",
    }
    outcomes = [
        BatchDispatchOutcome(
            job=InferenceJob(task="agenda-item-extract", recipe_hash="agenda-recipe"),
            result=JobHandle(
                task="agenda-item-extract",
                recipe_hash="agenda-recipe",
                backend="llm-dispatch-v2",
                ref="real-agenda-job",
            ),
        ),
        BatchDispatchOutcome(
            job=InferenceJob(task="agenda-item-extract", recipe_hash="failed-recipe"),
            result=RuntimeError("ingress unavailable"),
        ),
    ]

    replay, errors = run._chapter_batch_replay_items(
        [("source", agenda), ("source", failed)], "chapter_agenda", outcomes
    )

    assert replay == [("source", agenda)]
    assert errors == ["uid-failed: agenda chapter extraction: ingress unavailable"]
    assert failed.generated_agenda_candidates["status"] == "error"
    assert failed.generated_agenda_candidates["error"].endswith("ingress unavailable")
    assert "job_ref" not in failed.generated_agenda_candidates


def test_chapter_batch_replay_records_locator_submission_errors():
    failed = _ep("locator-failed")
    failed.generated_agenda_candidates = {
        "locator_status": "pending",
        "locator_recipe": "locator-recipe",
        "locator_job_ref": "batch-pending:locator-recipe",
    }
    outcomes = [
        BatchDispatchOutcome(
            job=InferenceJob(task="agenda-chapter-locate", recipe_hash="locator-recipe"),
            result=RuntimeError("ingress unavailable"),
        )
    ]

    replay, errors = run._chapter_batch_replay_items(
        [("source", failed)], "chapter_locator", outcomes
    )

    assert replay == []
    assert errors == ["uid-locator-failed: chapter locator: ingress unavailable"]
    assert failed.generated_agenda_candidates["locator_status"] == "error"
    assert failed.generated_agenda_candidates["locator_error"].endswith("ingress unavailable")
    assert "locator_job_ref" not in failed.generated_agenda_candidates


def test_chapter_build_claims_and_releases_maintenance_lease(monkeypatch, tmp_path):
    from tests._cas_fake import MemCAS

    storage = MemCAS()
    captured = {}
    monkeypatch.setenv("CITYPODS_MAINTENANCE_LEASE_KEY", "maintenance/test.json")
    monkeypatch.setenv("CITYPODS_MAINTENANCE_LEASE_OWNER", "test-owner")
    monkeypatch.setattr(run, "load_site_config", lambda path: {"defaults": {}})
    monkeypatch.setattr(run, "make_storage", lambda *args: storage)
    monkeypatch.setattr(run, "_build_impl", lambda **kwargs: captured.update(kwargs) or [])

    assert run.build(lane="chapter-agenda", output_dir=tmp_path) == []
    assert captured["maintenance_lease"] is not None
    payload, _etag = storage.get_bytes("maintenance/test.json")
    assert json.loads(payload)["state"] == "released"


def test_chapter_build_claims_and_releases_composite_maintenance_lease(monkeypatch, tmp_path):
    from tests._cas_fake import MemCAS

    storage = MemCAS()
    captured = {}
    monkeypatch.setenv(
        "CITYPODS_MAINTENANCE_LEASE_KEY", "maintenance/test-a.json, maintenance/test-b.json"
    )
    monkeypatch.setenv("CITYPODS_MAINTENANCE_LEASE_OWNER", "test-owner")
    monkeypatch.setattr(run, "load_site_config", lambda path: {"defaults": {}})
    monkeypatch.setattr(run, "make_storage", lambda *args: storage)
    monkeypatch.setattr(run, "_build_impl", lambda **kwargs: captured.update(kwargs) or [])

    assert run.build(lane="chapter", output_dir=tmp_path) == []
    assert captured["maintenance_lease"] is not None
    assert captured["maintenance_lease"].keys == (
        "maintenance/test-a.json",
        "maintenance/test-b.json",
    )
    for key in ("maintenance/test-a.json", "maintenance/test-b.json"):
        payload, _etag = storage.get_bytes(key)
        assert json.loads(payload)["state"] == "released"


def test_normalize_episode_durations_prefers_probe_without_listing(monkeypatch):
    ep = _ep("g-probe", hosted="https://cdn/g-probe.m4a")
    ep.audio_key = "audio/src/g-probe.m4a"
    warnings = []

    class _Storage:
        def get_range(self, key, start, end):
            raise AssertionError("range probe should be monkeypatched before use")

        def list_objects(self, prefix):
            raise AssertionError("normalization must not list storage for duration lookup")

    monkeypatch.setattr(
        run,
        "probe_hosted_audio_duration_seconds",
        lambda storage, key, *, ffmpeg_binary="ffmpeg": (1800.0, "stream-sample"),
    )

    stats = run._normalize_episode_durations_for_dispatch(
        "src",
        [ep],
        storage=_Storage(),
        ffmpeg_binary="ffmpeg",
        allow_probe=True,
        log=warnings.append,
    )

    assert ep.served_duration_seconds == pytest.approx(1800.0)
    assert ep.audio_duration_served == pytest.approx(1800.0)
    assert stats == run.DurationNormalizationStats(normalized_from_probe=1)
    assert any("duration_normalized_from_probe" in msg for msg in warnings)


def test_normalize_episode_durations_leaves_timeline_only_episode_missing(monkeypatch):
    ep = _ep("g-fallback")
    ep.timeline = Timeline(
        version="silence:2",
        segments=(
            Segment(
                served_start=0.0,
                served_end=600.0,
                kind="source",
                source_id="s0",
                source_start=0.0,
                source_end=600.0,
            ),
        ),
    )
    warnings = []
    monkeypatch.setattr(
        run,
        "probe_hosted_audio_duration_seconds",
        lambda storage, key, *, ffmpeg_binary="ffmpeg": (None, "header-unavailable"),
    )

    stats = run._normalize_episode_durations_for_dispatch(
        "src",
        [ep],
        storage=None,
        ffmpeg_binary="ffmpeg",
        allow_probe=False,
        log=warnings.append,
    )

    assert ep.served_duration_seconds is None
    assert ep.audio_duration_served is None
    assert stats == run.DurationNormalizationStats(missing_after_normalization=1)
    assert any("duration_missing_after_normalization" in msg for msg in warnings)


def test_normalize_episode_durations_warns_when_still_missing(monkeypatch):
    ep = _ep("g-missing", hosted="https://cdn/g-missing.m4a")
    ep.audio_key = "audio/src/g-missing.m4a"
    warnings = []
    monkeypatch.setattr(
        run,
        "probe_hosted_audio_duration_seconds",
        lambda storage, key, *, ffmpeg_binary="ffmpeg": (None, "no-duration-metadata"),
    )

    stats = run._normalize_episode_durations_for_dispatch(
        "src",
        [ep],
        storage=object(),
        ffmpeg_binary="ffmpeg",
        allow_probe=True,
        log=warnings.append,
    )

    assert ep.served_duration_seconds is None
    assert ep.audio_duration_served is None
    assert stats == run.DurationNormalizationStats(probe_failed=1, missing_after_normalization=1)
    assert any("duration_probe_failed" in msg for msg in warnings)
    assert any("duration_missing_after_normalization" in msg for msg in warnings)


def test_normalize_episode_durations_stops_before_probe(monkeypatch):
    ep = _ep("g-stop", hosted="https://cdn/g-stop.m4a")
    ep.audio_key = "audio/src/g-stop.m4a"

    def _probe(*args, **kwargs):
        raise AssertionError("probe should not run after stop")

    monkeypatch.setattr(run, "probe_hosted_audio_duration_seconds", _probe)

    stats = run._normalize_episode_durations_for_dispatch(
        "src",
        [ep],
        storage=object(),
        ffmpeg_binary="ffmpeg",
        allow_probe=True,
        stop=lambda: True,
        log=lambda msg: None,
    )

    assert stats == run.DurationNormalizationStats()
    assert ep.served_duration_seconds is None
    assert ep.audio_duration_served is None


def test_normalize_episode_durations_probe_exception_leaves_missing(monkeypatch):
    ep = _ep("g-probe-error", hosted="https://cdn/g-probe-error.m4a")
    ep.audio_key = "audio/src/g-probe-error.m4a"
    ep.timeline = Timeline(
        version="silence:2",
        segments=(
            Segment(
                served_start=0.0,
                served_end=600.0,
                kind="source",
                source_id="s0",
                source_start=0.0,
                source_end=600.0,
            ),
        ),
    )
    warnings = []

    def _probe(*args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(run, "probe_hosted_audio_duration_seconds", _probe)

    stats = run._normalize_episode_durations_for_dispatch(
        "src",
        [ep],
        storage=object(),
        ffmpeg_binary="ffmpeg",
        allow_probe=True,
        log=warnings.append,
    )

    assert stats == run.DurationNormalizationStats(probe_failed=1, missing_after_normalization=1)
    assert ep.served_duration_seconds is None
    assert ep.audio_duration_served is None
    assert any("duration_probe_failed" in msg for msg in warnings)
    assert any("duration_missing_after_normalization" in msg for msg in warnings)


# --- end-to-end incremental build via a fake provider ----------------------------------


class _FakeProvider:
    name = "faketest"

    def __init__(self):
        self.episodes = [_ep("g1"), _ep("g2")]
        self.fetches = 0
        self.error: ProviderError | None = None

    def validate(self, source):
        pass

    def detect_change(self, source):
        return None  # no HTTP validator -> exercises the content-hash path

    def fetch_episodes(self, source):
        self.fetches += 1
        if self.error:
            raise self.error
        return list(self.episodes)

    def resolve_media_url(self, episode, source):
        return episode.video_url


@pytest.fixture
def fake_provider():
    provider = _FakeProvider()
    register(provider)
    try:
        yield provider
    finally:
        from citypods.providers import _REGISTRY

        _REGISTRY.pop("faketest", None)


def _setup(tmp_path):
    config_dir = tmp_path / "config"
    feeds = config_dir / "feeds"
    feeds.mkdir(parents=True)
    (feeds / "fake-city.yml").write_text(
        "slug: fake-city\n"
        "provider: faketest\n"
        "source: {feed_url: 'https://x'}\n"
        'podcast_title: "Fake City"\n'
        'podcast_author: "City of Fake"\n'
        'podcast_email: ""\n'
        'podcast_description: "desc"\n'
    )
    # source_media_max_bytes: 0 disables the #497 preflight guard for these orchestration tests —
    # they use a synthetic, deliberately non-resolvable host (https://x) with a fake ffmpeg double
    # to avoid real network/subprocess calls, not to exercise the guard itself (covered directly in
    # test_media.py/test_http.py). Without this, validate_source_url's DNS resolution failure would
    # now surface as a SecurityError before the fake ffmpeg is ever reached.
    (tmp_path / "site_config.yml").write_text(
        f"state_dir: {tmp_path / 'state'}\ndefaults:\n  source_media_max_bytes: 0\n"
    )
    return config_dir


def _build(tmp_path, cities):
    return run.build(
        site_config_path=tmp_path / "site_config.yml",
        config_dir=cities,
        output_dir=tmp_path / "docs",
        base_url="https://example.test",
    )


def test_unchanged_city_is_skipped_on_second_build(tmp_path, fake_provider):
    assert get_provider("faketest") is fake_provider
    cities = _setup(tmp_path)

    first = _build(tmp_path, cities)
    assert [r.status for r in first] == ["built"]

    second = _build(tmp_path, cities)
    assert [r.status for r in second] == ["skipped"]
    # State persisted outside docs/ so it survives a wiped output tree.
    assert (tmp_path / "state" / "feed_etags.json").exists()


def test_render_writes_static_search_outputs(tmp_path, fake_provider):
    cities = _setup(tmp_path)
    _build(tmp_path, cities)
    assert (tmp_path / "docs" / "search" / "index.html").exists()
    assert (tmp_path / "docs" / "data" / "search" / "manifest.json").exists()
    assert (tmp_path / "docs" / "assets" / "minisearch-7.1.2.js").exists()
    assert (tmp_path / "docs" / "assets" / "LICENSES" / "minisearch-7.1.2.txt").exists()


def test_render_writes_city_request_page_when_public_form_configured(tmp_path, fake_provider):
    cities = _setup(tmp_path)
    with (tmp_path / "site_config.yml").open("a") as config:
        config.write(
            "city_request_form:\n"
            "  formspark_action: https://submit-form.com/public-form-id\n"
            "  turnstile_site_key: public-site-key\n"
        )
    _build(tmp_path, cities)
    request_page = tmp_path / "docs" / "request-a-city" / "index.html"
    assert request_page.exists()
    assert "https://submit-form.com/public-form-id" in request_page.read_text()
    assert "/request-a-city/" in (tmp_path / "docs" / "index.html").read_text()


def test_render_does_not_advertise_an_incomplete_static_search_index(
    tmp_path, fake_provider, monkeypatch
):
    cities = _setup(tmp_path)

    def defer_search(*args, **kwargs):
        assert kwargs["stop"] is not None
        return None

    monkeypatch.setattr(run, "build_search_index", defer_search)
    _build(tmp_path, cities)

    assert "/search/" not in (tmp_path / "docs" / "index.html").read_text()
    assert not (tmp_path / "docs" / "search" / "index.html").exists()


def test_archived_meeting_page_updates_even_when_feed_window_is_unchanged(tmp_path, fake_provider):
    cities = _setup(tmp_path)
    with (tmp_path / "site_config.yml").open("a") as config:
        config.write("  max_episodes: 1\n")
    _build(tmp_path, cities)

    old_page = tmp_path / "docs" / "fake-city" / fake_provider.episodes[1].uid / "index.html"
    assert old_page.exists()
    fake_provider.episodes[1].title = "Corrected archival title"

    result = _build(tmp_path, cities)
    assert [r.status for r in result] == ["built"]
    assert "Corrected archival title" in old_page.read_text()


def test_changed_content_rebuilds(tmp_path, fake_provider):
    cities = _setup(tmp_path)
    _build(tmp_path, cities)

    fake_provider.episodes.append(_ep("g3", title="Planning Commission"))
    result = _build(tmp_path, cities)
    assert [r.status for r in result] == ["built"]


def test_auxiliary_source_enriches_existing_episode_without_creating_one(tmp_path, fake_provider):
    class _AuxiliaryProvider:
        name = "faketestaux"
        capabilities = frozenset()

        def validate(self, source):
            pass

        def detect_change(self, source):
            return None

        def fetch_episodes(self, source):
            return [
                Episode(
                    guid="aux-g1",
                    title="Agenda row",
                    published=datetime(2026, 5, 1, tzinfo=UTC),
                    video_url="https://x/aux.mp4",
                    body="City Council",
                    links={"agenda_portal": "https://agenda.example/meeting/1"},
                )
            ]

        def resolve_media_url(self, episode, source):
            return episode.video_url

        def video_deeplink(self, ref, t_seconds):
            return None

    auxiliary = _AuxiliaryProvider()
    register(auxiliary)
    try:
        cities = _setup(tmp_path)
        config = cities / "feeds" / "fake-city.yml"
        config.write_text(
            config.read_text()
            + "aux_provider: faketestaux\n"
            + "aux_source: {feed_url: 'https://agenda.example'}\n"
        )

        result = _build(tmp_path, cities)

        assert result[0].episode_count == len(fake_provider.episodes)
        (records_file,) = (tmp_path / "state" / "sources").glob("*/episodes.json")
        records = json.loads(records_file.read_text())["episodes"]
        assert len(records) == len(fake_provider.episodes)
        assert records[fake_provider.episodes[0].uid]["links"]["agenda_portal"] == (
            "https://agenda.example/meeting/1"
        )
    finally:
        from citypods.providers import _REGISTRY

        _REGISTRY.pop(auxiliary.name, None)


def test_calendar_companion_backfills_video_and_persists_no_video_meetings(tmp_path, fake_provider):
    class _CalendarCompanion:
        name = "faketestcalendar"
        capabilities = frozenset()

        def validate(self, source):
            pass

        def fetch_calendar_index(self, source):
            return CalendarIndex(
                records=[
                    AgendaRecord(
                        body="City Council",
                        title="Archive meeting calendar metadata",
                        published=datetime(2026, 5, 1, tzinfo=UTC),
                        links={"agenda": "https://calendar.example/archive.pdf"},
                        video_guid="archive",
                        video_url="https://calendar.example/archive.mp4",
                    ),
                    AgendaRecord(
                        body="Planning Commission",
                        title="Calendar-only recording",
                        published=datetime(2026, 5, 2, tzinfo=UTC),
                        links={"agenda": "https://calendar.example/planning.pdf"},
                        video_guid="calendar-recording",
                        video_url="https://calendar.example/planning.mp4",
                    ),
                    AgendaRecord(
                        body="Library Board",
                        title="Calendar-only meeting",
                        published=datetime(2026, 5, 3, tzinfo=UTC),
                        links={"agenda": "https://calendar.example/library.pdf"},
                    ),
                ]
            )

    companion = _CalendarCompanion()
    register(companion)
    try:
        fake_provider.episodes = [
            Episode(
                guid="archive",
                title="Archive meeting",
                published=datetime(2026, 5, 1, tzinfo=UTC),
                video_url="https://archive.example/meeting.mp4",
                body="City Council",
            )
        ]
        cities = _setup(tmp_path)
        config = cities / "feeds" / "fake-city.yml"
        config.write_text(
            config.read_text()
            + "aux_provider: faketestcalendar\n"
            + "aux_source: {calendar_url: 'https://calendar.example'}\n"
        )

        result = _build(tmp_path, cities)

        assert result[0].episode_count == 2
        source_dir = next((tmp_path / "state" / "sources").iterdir())
        episodes = json.loads((source_dir / "episodes.json").read_text())["episodes"]
        assert len(episodes) == 2
        assert all(record["title"] != "Calendar-only meeting" for record in episodes.values())
        assert (
            next(record for record in episodes.values() if record["provider_guid"] == "archive")[
                "links"
            ]["agenda"]
            == "https://calendar.example/archive.pdf"
        )

        calendar = json.loads((source_dir / "calendar.json").read_text())["records"]
        assert len(calendar) == 3
        assert any(record["title"] == "Calendar-only meeting" for record in calendar.values())
        archive = (tmp_path / "docs" / "fake-city" / "archive" / "index.html").read_text()
        assert "Calendar-only meetings" in archive
        assert "Calendar-only meeting" in archive
        assert "Calendar-only recording" in archive
        assert "Calendar-only recording" not in archive.split("Calendar-only meetings", 1)[1]
    finally:
        from citypods.providers import _REGISTRY

        _REGISTRY.pop(companion.name, None)


def test_calendar_companion_matches_same_day_sessions_by_uid(tmp_path, fake_provider):
    class _SameDayCalendar:
        name = "faketestsamedaycalendar"
        capabilities = frozenset()

        def validate(self, source):
            pass

        def fetch_calendar_index(self, source):
            return CalendarIndex(
                records=[
                    AgendaRecord(
                        body="City Council",
                        title="Morning session",
                        published=datetime(2026, 5, 1, 9, tzinfo=UTC),
                        links={"agenda": "https://calendar.example/morning.pdf"},
                    ),
                    AgendaRecord(
                        body="City Council",
                        title="Evening session",
                        published=datetime(2026, 5, 1, 18, tzinfo=UTC),
                        links={"agenda": "https://calendar.example/evening.pdf"},
                    ),
                ]
            )

    companion = _SameDayCalendar()
    register(companion)
    try:
        fake_provider.episodes = [
            _ep("morning"),
            _ep("evening"),
        ]
        fake_provider.episodes[0].published = datetime(2026, 5, 1, 9, tzinfo=UTC)
        fake_provider.episodes[1].published = datetime(2026, 5, 1, 18, tzinfo=UTC)
        cities = _setup(tmp_path)
        config = cities / "feeds" / "fake-city.yml"
        config.write_text(
            config.read_text()
            + "aux_provider: faketestsamedaycalendar\n"
            + "aux_source: {calendar_url: 'https://calendar.example'}\n"
        )

        _build(tmp_path, cities)

        source_dir = next((tmp_path / "state" / "sources").iterdir())
        records = json.loads((source_dir / "episodes.json").read_text())["episodes"].values()
        by_guid = {record["provider_guid"]: record for record in records}
        assert by_guid["morning"]["links"]["agenda"].endswith("morning.pdf")
        assert by_guid["evening"]["links"]["agenda"].endswith("evening.pdf")
    finally:
        from citypods.providers import _REGISTRY

        _REGISTRY.pop(companion.name, None)


def test_calendar_companion_failure_keeps_primary_archive_available(tmp_path, fake_provider):
    class _FailingCalendarCompanion:
        name = "failingcalendar"
        capabilities = frozenset()

        def validate(self, source):
            pass

        def fetch_calendar_index(self, source):
            raise ProviderError("calendar temporarily unavailable")

    companion = _FailingCalendarCompanion()
    register(companion)
    try:
        cities = _setup(tmp_path)
        config = cities / "feeds" / "fake-city.yml"
        config.write_text(
            config.read_text()
            + "aux_provider: failingcalendar\n"
            + "aux_source: {calendar_url: 'https://calendar.example'}\n"
        )

        result = _build(tmp_path, cities)

        assert result[0].status == "built"
        assert result[0].episode_count == len(fake_provider.episodes)
    finally:
        from citypods.providers import _REGISTRY

        _REGISTRY.pop(companion.name, None)


def test_calendar_companion_crash_keeps_primary_archive_available(tmp_path, fake_provider):
    class _CrashingCalendarCompanion:
        name = "crashingcalendar"
        capabilities = frozenset()

        def validate(self, source):
            pass

        def fetch_calendar_index(self, source):
            raise RuntimeError("unexpected calendar parser failure")

    companion = _CrashingCalendarCompanion()
    register(companion)
    try:
        cities = _setup(tmp_path)
        config = cities / "feeds" / "fake-city.yml"
        config.write_text(
            config.read_text()
            + "aux_provider: crashingcalendar\n"
            + "aux_source: {calendar_url: 'https://calendar.example'}\n"
        )

        result = _build(tmp_path, cities)

        assert result[0].status == "built"
        assert result[0].episode_count == len(fake_provider.episodes)
    finally:
        from citypods.providers import _REGISTRY

        _REGISTRY.pop(companion.name, None)


def test_missing_outputs_force_rebuild_even_if_hash_matches(tmp_path, fake_provider):
    cities = _setup(tmp_path)
    _build(tmp_path, cities)

    # Simulate the CI case where docs/ is wiped but state (cache) survives.
    import shutil

    shutil.rmtree(tmp_path / "docs")
    result = _build(tmp_path, cities)
    assert [r.status for r in result] == ["built"]


def test_window_shift_keeps_dropped_episode_in_archive_and_feed(tmp_path, fake_provider):
    """Issue #109: a meeting that leaves the provider's window must stay in our records + feed,
    and its audio must remain referenced (so orphan-GC won't reap it)."""
    import json

    from citypods.records import referenced_audio_keys

    # Distinct publish dates so each meeting gets a stable uid across runs (same date+body would
    # make assign_uids' sequence numbering depend on which episodes the window currently shows).
    def ep(guid, day, **kw):
        e = _ep(guid, **kw)
        e.published = datetime(2026, 5, day, tzinfo=UTC)
        return e

    # g1 has hosted audio (a content-addressed key); the build will persist it.
    g1 = ep("g1", 1, hosted="https://cdn/g1.m4a")
    g1.audio_key = "faketest/src/uid-g1-spec.m4a"
    fake_provider.episodes = [g1, ep("g2", 8)]
    cities = _setup(tmp_path)
    _build(tmp_path, cities)

    # The provider window shifts: g1 drops off, g3 appears.
    fake_provider.episodes = [ep("g2", 8), ep("g3", 15, title="Planning Commission")]
    _build(tmp_path, cities)

    # (assign_uids derives the real uid from author+body+date, so match on the stable audio key.)
    state = tmp_path / "state"
    (records_file,) = state.glob("sources/*/episodes.json")
    episodes = json.loads(records_file.read_text())["episodes"]
    audio_keys = {rec["audio"]["key"] for rec in episodes.values()}
    # g1 survived the window shift with its audio intact (3 meetings now archived: g1, g2, g3)...
    assert g1.audio_key in audio_keys
    assert len(episodes) == 3
    # ...so the orphan GC still sees its audio as referenced.
    assert g1.audio_key in referenced_audio_keys(state)
    # ...and the rendered feed still serves the dropped episode's enclosure.
    feed = (tmp_path / "docs" / "fake-city" / "audio_feed.xml").read_text()
    assert "https://cdn/g1.m4a" in feed
    assert feed.count("<item>") == 3


def test_write_chapter_sidecars_writes_and_prunes(tmp_path):
    import json
    from datetime import UTC, datetime

    from citypods.models import City, Episode
    from citypods.run import _write_chapter_sidecars

    city = City(
        slug="x-tx",
        provider="granicus",
        source={"feed_url": "u"},
        podcast_title="X",
        podcast_author="A",
        podcast_email="",
        podcast_description="d",
    )

    def ep(uid, chapters):
        return Episode(
            guid=uid,
            uid=uid,
            title="t",
            published=datetime(2026, 1, 1, tzinfo=UTC),
            video_url="v",
            media_kind="direct",
            chapters=chapters,
        )

    city_dir = tmp_path / "x-tx"
    city_dir.mkdir()
    (city_dir / "chapters").mkdir()
    (city_dir / "chapters" / "stale.json").write_text("{}")  # leftover -> should be pruned

    eps = [ep("u1", [{"start": 0, "title": "Intro"}]), ep("u2", [])]  # u2 has no chapters
    _write_chapter_sidecars(city_dir, city, eps, "https://e.test")

    doc = json.loads((city_dir / "chapters" / "u1.json").read_text())
    assert doc["chapters"] == [{"startTime": 0, "title": "Intro"}]
    assert not (city_dir / "chapters" / "u2.json").exists()  # no chapters -> no file
    assert not (city_dir / "chapters" / "stale.json").exists()  # pruned


def test_build_writes_run_history_and_summary(tmp_path, fake_provider):
    import json as _json

    cities = _setup(tmp_path)
    _build(tmp_path, cities)
    state = tmp_path / "state"
    summary = _json.loads((state / "run_summary.json").read_text())
    assert summary["cities"] >= 1
    assert "stages" in summary and "materialized" in summary
    hist = (state / "run_history.jsonl").read_text().strip().splitlines()
    assert len(hist) == 1
    # a second build appends, not overwrites
    _build(tmp_path, cities)
    hist2 = (state / "run_history.jsonl").read_text().strip().splitlines()
    assert len(hist2) == 2
    assert all(_json.loads(line)["schema_version"] for line in hist2)


def test_run_history_counts_planner_and_audio_throttles_once(tmp_path):
    base = {
        "ran": 0,
        "encoded": 0,
        "credited": 0,
        "aligned": 0,
        "transcribed": 0,
        "reused": 0,
        "backlog": 0,
        "seconds": 0.0,
        "bytes": 0,
        "errors": 0,
        "rate_limited": 0,
    }
    timeline = {**base, "rate_limited": 2, "errors": 2}
    audio = {**base, "rate_limited": 1, "errors": 1}

    run._record_run_history(
        tmp_path,
        [],
        {"timeline": timeline, "audio": audio},
        scope={"phase": "enrich", "lane": "audio"},
    )

    summary = json.loads((tmp_path / "run_summary.json").read_text())
    assert summary["audio_rate_limited_403s"] == 3


def test_run_history_records_h16_identity_summary(tmp_path):
    identity = {
        "checked": 12,
        "mismatches": 1,
        "artifact_checked": 8,
        "mismatch_categories": {"audio_key": 1},
    }

    run._record_run_history(
        tmp_path,
        [],
        {},
        h16_identity=identity,
        scope={"phase": "enrich", "lane": "audio"},
    )

    summary = json.loads((tmp_path / "run_summary.json").read_text())
    assert summary["h16_identity"] == identity


def test_run_history_records_logical_run_id_and_defer_reasons(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/repo")
    transcript = {
        "ran": 0,
        "encoded": 0,
        "credited": 0,
        "aligned": 0,
        "transcribed": 0,
        "reused": 0,
        "backlog": 3,
        "seconds": 0.0,
        "bytes": 0,
        "errors": 0,
        "rate_limited": 0,
        "defer_reasons": {"insufficient-budget": 2, "timeout-backoff": 1},
    }

    run._record_run_history(
        tmp_path,
        [],
        {"transcript": transcript},
        scope={"phase": "enrich", "lane": "transcribe", "shard": "0/4", "scoped": True},
    )

    summary = json.loads((tmp_path / "run_summary.json").read_text())
    assert summary["logical_run_id"] == "github:12345:enrich:transcribe"
    assert summary["stages"]["transcript"]["defer_reasons"] == {
        "insufficient-budget": 2,
        "timeout-backoff": 1,
    }


def test_build_writes_work_manifest(tmp_path, fake_provider):
    """H5: an enrich/all run persists the derived work manifest to state/work.json."""
    from citypods.ops.workqueue import load_manifest

    cities = _setup(tmp_path)
    _build(tmp_path, cities)
    work = tmp_path / "state" / "work.json"
    assert work.exists()
    data = json.loads(work.read_text())
    assert data["version"] == 1 and isinstance(data["items"], list)
    assert isinstance(load_manifest(tmp_path / "state"), list)  # round-trips


def test_build_with_backlog_policy_orders_and_succeeds(tmp_path, fake_provider):
    """H5: a configured policy exercises the city-ordering path; the build still succeeds."""
    cities = _setup(tmp_path)
    (tmp_path / "site_config.yml").write_text(
        f"state_dir: {tmp_path / 'state'}\n"
        "backlog_priority:\n"
        "  - recency: {order: desc, within_days: 30}\n"
    )
    result = _build(tmp_path, cities)
    assert [r.status for r in result] == ["built"]


def test_build_logs_audio_stage_activity_and_errors(tmp_path, fake_provider, capsys):
    """The per-stage run summary must surface audio activity (and sample errors) to stdout, so a
    re-host that triggers but fails downstream is visible rather than hiding behind the
    feed-level "0 errors" line (issue #116 follow-up)."""
    import subprocess

    for ep in fake_provider.episodes:
        ep.media_kind = "hls"  # forces re-hosting via materialize_audio

    class _FailingFfmpeg:
        def extract_audio(
            self,
            timeline,
            sources_by_id,
            dest,
            chapters=None,
            *,
            sources=None,
            loudness_profile=None,
            processing_profile=None,
            asset_resolver=None,
        ):
            raise subprocess.CalledProcessError(1, "ffmpeg")

    cities = _setup(tmp_path)
    run.build(
        site_config_path=tmp_path / "site_config.yml",
        config_dir=cities,
        output_dir=tmp_path / "docs",
        base_url="https://example.test",
        ffmpeg=_FailingFfmpeg(),
    )
    out = capsys.readouterr().out
    assert "audio:" in out and "errors" in out
    assert "! " in out  # a sample error message was surfaced


def test_build_logs_audio_hosted_count(tmp_path, fake_provider, capsys):
    """A successful re-host reports a non-zero ``ran`` count in the stage summary."""

    class _OkFfmpeg:
        def extract_audio(
            self,
            timeline,
            sources_by_id,
            dest,
            chapters=None,
            *,
            sources=None,
            loudness_profile=None,
            processing_profile=None,
            asset_resolver=None,
        ):
            dest.write_bytes(
                b"fake-m4a" * 1024
            )  # >_MIN_PLAUSIBLE_AUDIO_BYTES (#39 truncation guard)

    for ep in fake_provider.episodes:
        ep.media_kind = "hls"
    cities = _setup(tmp_path)
    run.build(
        site_config_path=tmp_path / "site_config.yml",
        config_dir=cities,
        output_dir=tmp_path / "docs",
        base_url="https://example.test",
        ffmpeg=_OkFfmpeg(),
    )
    out = capsys.readouterr().out
    # two HLS episodes, both newly encoded (not credited from storage), none reused, no errors
    assert "audio: 2 ran (2 encoded, 0 credited), 0 reused" in out and "0 errors" in out


def test_enrich_logs_source_stage_and_heartbeat(tmp_path, fake_provider, capsys, monkeypatch):
    """CI enrich logs should leave queue/source breadcrumbs plus resource snapshots. The H5 PR3
    global queue replaces the per-source/per-stage breadcrumbs with queue + pass-level lines
    (per-stage detail moves to the end-of-run summary; per-encode logs stay in media.py)."""

    cities = _setup(tmp_path)
    (tmp_path / "site_config.yml").write_text(
        f"state_dir: {tmp_path / 'state'}\ndefaults:\n  asr_enabled: false\n"
    )
    monkeypatch.setenv("CITYPODS_HEARTBEAT_SECONDS", "999")

    run.build(
        site_config_path=tmp_path / "site_config.yml",
        config_dir=cities,
        output_dir=tmp_path / "docs",
        base_url="https://example.test",
        phase="enrich",
    )
    out = capsys.readouterr().out
    assert "[enrich] heartbeat start" in out and "[enrich] heartbeat stop" in out
    assert "[enrich] source fetched slug=fake-city provider=faketest" in out
    assert "[enrich] global queue:" in out
    assert "[enrich] audio pass:" in out and "[enrich] audio pass done" in out


def test_bounded_runner_refills_without_eager_submission(monkeypatch):
    """The audio queue keeps a rolling window instead of submitting the whole backlog."""
    submitted: list[int] = []

    class _Pool:
        def submit(self, fn, item):
            submitted.append(item)
            return _ImmediateFuture(fn(item))

    class _ImmediateFuture:
        def __init__(self, value):
            self.value = value

        def result(self):
            return self.value

    def _one_done(pending, *, return_when):
        future = next(iter(pending))
        return {future}, pending - {future}

    monkeypatch.setattr(run, "wait", _one_done)
    run._run_bounded(_Pool(), lambda item: item, range(5), max_pending=2)
    assert submitted == [0, 1, 2, 3, 4]


def test_bounded_runner_calls_on_progress_once_per_drain_cycle(monkeypatch):
    """`on_progress` (the tag lane's mid-run checkpoint hook) must fire from the runner's own
    thread after every drain cycle -- never skipped, never concurrent with itself -- so a
    periodic durable-storage push actually happens during a long-running pass instead of only at
    the very end."""
    calls: list[int] = []

    class _Pool:
        def submit(self, fn, item):
            return _ImmediateFuture(fn(item))

    class _ImmediateFuture:
        def __init__(self, value):
            self.value = value

        def result(self):
            return self.value

    def _one_done(pending, *, return_when):
        future = next(iter(pending))
        return {future}, pending - {future}

    monkeypatch.setattr(run, "wait", _one_done)
    run._run_bounded(
        _Pool(),
        lambda item: item,
        range(5),
        max_pending=2,
        on_progress=lambda: calls.append(1),
    )
    assert len(calls) == 5


def test_heartbeat_tick_prints_active_work_snapshot(tmp_path, capsys):
    # CR2-TS-10: call _tick() directly (the sibling stall-dump test's established pattern)
    # instead of racing the background thread's own interval_seconds timing.
    progress_entry = run.PROGRESS.start(source="dallas-tx", uid="ep1", phase="audio-encode")
    try:
        hb = run._ResourceHeartbeat(
            enabled=True, label="enrich", root=tmp_path, interval_seconds=999
        )
        hb._tick()
        out = capsys.readouterr().out
        assert "[enrich] active work:" in out
        assert "audio-encode" in out and "dallas-tx" in out and "ep1" in out
    finally:
        run.PROGRESS.finish(progress_entry)


def test_heartbeat_dumps_stalled_threads_once_per_cooldown(tmp_path, capsys, monkeypatch):
    progress_entry = run.PROGRESS.start(source="dallas-tx", uid="ep1", phase="audio-encode")
    dump_calls = []
    monkeypatch.setattr(run.faulthandler, "dump_traceback", lambda **kw: dump_calls.append(kw))
    try:
        hb = run._ResourceHeartbeat(
            enabled=True,
            label="enrich",
            root=tmp_path,
            interval_seconds=999,
            stall_dump_seconds=0.001,
        )
        time.sleep(0.01)  # ensure the tracked entry is older than stall_dump_seconds
        hb._maybe_dump_stalled_threads()
        captured = capsys.readouterr()
        assert "dumping all thread stacks" in captured.out
        assert len(dump_calls) == 1
        assert hb._last_stall_dump is not None
        # Second call within the cooldown window must not dump again.
        prior = hb._last_stall_dump
        hb._maybe_dump_stalled_threads()
        assert hb._last_stall_dump == prior
        assert len(dump_calls) == 1
    finally:
        run.PROGRESS.finish(progress_entry)


def test_heartbeat_no_dump_when_stall_dump_disabled(tmp_path, capsys):
    progress_entry = run.PROGRESS.start(source="dallas-tx", uid="ep1", phase="audio-encode")
    try:
        hb = run._ResourceHeartbeat(
            enabled=True,
            label="enrich",
            root=tmp_path,
            interval_seconds=999,
            stall_dump_seconds=0.0,  # 0 == disabled
        )
        hb._maybe_dump_stalled_threads()
        assert hb._last_stall_dump is None
        assert "dumping all thread stacks" not in capsys.readouterr().out
    finally:
        run.PROGRESS.finish(progress_entry)


def test_heartbeat_tick_prints_gate_state_when_active(tmp_path, capsys):
    gate = run.NativeWorkGate(max_audio_active=3, poll_seconds=0.01)
    assert gate.acquire(kind="audio", label="x") is True
    try:
        hb = run._ResourceHeartbeat(
            enabled=True,
            label="enrich",
            root=tmp_path,
            interval_seconds=0.02,
            native_work_gate=gate,
        )
        with hb:
            time.sleep(0.06)
        out = capsys.readouterr().out
        assert "[enrich] gate: audio_active=1/3" in out
    finally:
        gate.release(kind="audio")


def test_heartbeat_tick_suppresses_gate_line_when_idle(tmp_path, capsys):
    gate = run.NativeWorkGate(max_audio_active=3, poll_seconds=0.01)
    hb = run._ResourceHeartbeat(
        enabled=True,
        label="enrich",
        root=tmp_path,
        interval_seconds=0.02,
        native_work_gate=gate,
    )
    with hb:
        time.sleep(0.06)
    out = capsys.readouterr().out
    assert "[enrich] gate:" not in out


def test_heartbeat_tick_prints_lease_waiting_state(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        run.DISTRIBUTED_PROVIDER_LEASES,
        "current_waiting_counts",
        lambda: {"granicus.com": 2},
    )
    monkeypatch.setattr(
        run.DISTRIBUTED_PROVIDER_LEASES,
        "telemetry",
        lambda: {"granicus.com": {"lease_wait_seconds": 12.3, "lease_renewals": 4}},
    )
    hb = run._ResourceHeartbeat(enabled=True, label="enrich", root=tmp_path, interval_seconds=0.02)
    with hb:
        time.sleep(0.06)
    out = capsys.readouterr().out
    assert "[enrich] leases: granicus.com waiting=2 cum_wait=12.3s renewals=4" in out


def test_heartbeat_no_dump_when_nothing_tracked(tmp_path):
    hb = run._ResourceHeartbeat(
        enabled=True, label="enrich", root=tmp_path, interval_seconds=999, stall_dump_seconds=1.0
    )
    hb._maybe_dump_stalled_threads()
    assert hb._last_stall_dump is None


def test_stop_signal_fires_on_deadline():
    from citypods.run import StopSignal

    # deadline already in the past -> stop immediately, regardless of supersession.
    assert StopSignal(deadline=time.monotonic() - 1)() is True
    # no deadline, no superseded check -> never stops.
    assert StopSignal()() is False


def test_stop_signal_latches_superseded_and_throttles_polling():
    from citypods.run import StopSignal

    calls = {"n": 0}

    def superseded():
        calls["n"] += 1
        return calls["n"] >= 2  # becomes True on the 2nd poll

    s = StopSignal(superseded=superseded, poll_interval=0)  # poll every call
    assert s() is False  # 1st poll: not yet
    assert s() is True  # 2nd poll: superseded
    assert s() is True and calls["n"] == 2  # latched -> no further polling


# --- GH#377: mid-run checkpoint + graceful SIGTERM shutdown ----------------------------


def test_signal_handler_flips_stop_and_marks_interrupt():
    """The SIGTERM handler latches the process-wide interrupt so any live StopSignal returns True
    (in-flight workers defer immediately) and the run is recorded as interrupted. Invoked directly
    — no real signal needed."""
    assert run.interrupt_requested() is False
    stop = run.StopSignal()
    assert stop() is False
    try:
        run._signal_stop_handler(15, None)
        assert run.interrupt_requested() is True
        assert stop() is True
        assert stop.fired_reason == "termination signal received"
    finally:
        run._INTERRUPT.clear()


def test_install_signal_handlers_registers_sigterm():
    import signal

    prev = signal.getsignal(signal.SIGTERM)
    try:
        run.install_signal_handlers()
        assert run.interrupt_requested() is False
        signal.raise_signal(signal.SIGTERM)  # delivered synchronously to the main thread
        assert run.interrupt_requested() is True
    finally:
        run._INTERRUPT.clear()
        signal.signal(signal.SIGTERM, prev)


def test_record_run_history_marks_interrupted(tmp_path):
    run._record_run_history(tmp_path, [], {}, scope={"phase": "enrich"}, interrupted=True)
    summary = json.loads((tmp_path / "run_summary.json").read_text())
    assert summary["interrupted"] is True
    assert summary["outcome"] == "interrupted"
    hist = (tmp_path / "run_history.jsonl").read_text().strip().splitlines()
    assert json.loads(hist[-1])["outcome"] == "interrupted"


def test_record_run_history_marks_completed_by_default(tmp_path):
    run._record_run_history(tmp_path, [], {}, scope={"phase": "enrich"})
    summary = json.loads((tmp_path / "run_summary.json").read_text())
    assert summary["interrupted"] is False
    assert summary["outcome"] == "completed"


def test_global_queue_persists_after_audio_pass_and_again_after_transcript(tmp_path, monkeypatch):
    """GH#377 incremental persistence: a source is saved once the audio pass drains (before the
    decoupled transcript pass even starts) and again at the end, shrinking the window a mid-run
    kill could lose."""
    city = City(
        slug="c",
        provider="granicus",
        source={"feed_url": "https://x.granicus.com/f"},
        podcast_title="C",
        podcast_author="A",
        podcast_email="",
        podcast_description="",
        extract_audio=True,
    )
    ep = _ep("e1")
    ep.hosted_audio_url = "https://cdn/e1.m4a"  # so the transcript pass includes it
    episodes = [ep]
    events: list[str] = []

    class _AudioStage:
        name = "audio"

    class _TranscriptStage:
        name = "transcript"

    class _Pipeline:
        def __init__(self):
            self.ctx = StageContext(
                storage=None, ffmpeg=None, max_kbps=96, dry_run=False, lane=None
            )
            self.stages = [_AudioStage(), _TranscriptStage()]

        def fetch_merge(self, _city, _key):
            return object(), episodes, {}, 0

        def accumulate_stats(self, _stats):
            pass

        def persist_source(self, _key, _eps, _persisted, *, notes):
            events.append("persist")

    def _run_stages(_p, _c, batch, stages, _ctx, *, quiet):
        events.append(f"{stages[0].name}:{batch[0].uid}")
        return [StageStats(stages[0].name, ran=1)]

    monkeypatch.setattr(run, "run_stages", _run_stages)

    run._run_enrich_global_queue(_Pipeline(), [city], source_cache=None, max_workers=1, policy=None)

    assert events == ["audio:uid-e1", "persist", "transcript:uid-e1", "persist"]


def test_global_queue_serializes_r7_private_ledger_stages(monkeypatch):
    """R7 ledger work follows every per-episode transcript stage and checkpoints per source."""
    city = City(
        slug="c",
        provider="granicus",
        source={"feed_url": "https://x.granicus.com/f"},
        podcast_title="C",
        podcast_author="A",
        podcast_email="",
        podcast_description="",
        extract_audio=True,
    )
    episodes = [_ep("e1"), _ep("e2")]
    for episode in episodes:
        episode.hosted_audio_url = f"https://cdn/{episode.uid}.m4a"
    events: list[str] = []

    class _Stage:
        def __init__(self, name):
            self.name = name

    class _Pipeline:
        def __init__(self):
            self.ctx = StageContext(
                storage=None, ffmpeg=None, max_kbps=96, dry_run=False, lane=None
            )
            self.stages = [
                _Stage("transcript"),
                _Stage("native_diarize"),
                _Stage("speaker_identity"),
            ]

        def fetch_merge(self, _city, _key):
            return object(), episodes, {}, 0

        def accumulate_stats(self, _stats):
            pass

        def persist_source(self, _key, _eps, _persisted, *, notes):
            events.append("persist")

    def _run_stages(_provider, _city, batch, stages, _ctx, *, quiet):
        stage_names = ",".join(stage.name for stage in stages)
        episode_uids = ",".join(ep.uid for ep in batch)
        events.append(f"{stage_names}:{episode_uids}")
        return [StageStats(stage.name, ran=len(batch)) for stage in stages]

    clock = {"now": 0.0}

    def _with_elapsed_ledger_run(*args, **kwargs):
        result = _run_stages(*args, **kwargs)
        if args[3][0].name == "native_diarize":
            clock["now"] = 181.0
        return result

    monkeypatch.setattr(run, "run_stages", _with_elapsed_ledger_run)
    monkeypatch.setattr(run.time, "monotonic", lambda: clock["now"])
    run._run_enrich_global_queue(
        _Pipeline(),
        [city],
        source_cache=None,
        max_workers=1,
        policy=None,
        mid_run_checkpoint=lambda: events.append("checkpoint"),
    )
    assert events == [
        "transcript:uid-e1",
        "transcript:uid-e2",
        "native_diarize,speaker_identity:uid-e1,uid-e2",
        "persist",
        "checkpoint",
        "persist",
    ]


def test_global_queue_mid_run_checkpoint_fires_on_interval_during_tags_only_pass(
    tmp_path, monkeypatch
):
    """The `tag` lane runs only a tags-only pass (no audio pass, so no free mid-run persist
    boundary -- see the previous test). `mid_run_checkpoint` must fire periodically during that
    pass once enough wall-clock time has elapsed, calling a local persist first, so a run that's
    hard-killed mid-pass for some other reason doesn't still lose the *entire* run's tag work back
    to whatever the previous run last pushed."""
    city = City(
        slug="c",
        provider="granicus",
        source={"feed_url": "https://x.granicus.com/f"},
        podcast_title="C",
        podcast_author="A",
        podcast_email="",
        podcast_description="",
        extract_audio=True,
    )
    episodes = [_ep("e1"), _ep("e2"), _ep("e3")]  # none hosted -> all land in the tags-only pass
    events: list[str] = []

    class _TagsStage:
        name = "tags"

    class _Pipeline:
        def __init__(self):
            self.ctx = StageContext(
                storage=None, ffmpeg=None, max_kbps=96, dry_run=False, lane=None
            )
            self.stages = [_TagsStage()]

        def fetch_merge(self, _city, _key):
            return object(), episodes, {}, 0

        def accumulate_stats(self, _stats):
            pass

        def persist_source(self, _key, _eps, _persisted, *, notes):
            events.append("persist")

    def _run_stages(_p, _c, batch, stages, _ctx, *, quiet):
        events.append(f"{stages[0].name}:{batch[0].uid}")
        return [StageStats(stages[0].name, ran=1)]

    monkeypatch.setattr(run, "run_stages", _run_stages)

    # Advances 100s per call: the checkpoint's own baseline read consumes the first tick, then
    # the interval (180s) is crossed on the SECOND check (t=200) but not the first (t=100) or
    # third (t=300 - 200 = 100 < 180) -- so the checkpoint must fire exactly once, after the
    # second item, not on every drain cycle and not only at the very end.
    ticks = iter(range(0, 1000, 100))
    monkeypatch.setattr(run.time, "monotonic", lambda: next(ticks))

    checkpoint_calls: list[int] = []
    run._run_enrich_global_queue(
        _Pipeline(),
        [city],
        source_cache=None,
        max_workers=1,
        policy=None,
        mid_run_checkpoint=lambda: checkpoint_calls.append(1),
    )

    assert len(checkpoint_calls) == 1
    assert events == [
        "tags:uid-e1",
        "tags:uid-e2",
        "persist",  # the mid-run checkpoint's local persist, ahead of the caller's durable push
        "tags:uid-e3",
        "persist",  # the unconditional end-of-pass persist every run gets
    ]


def test_global_queue_runs_document_stages_once_per_complete_source(tmp_path, monkeypatch):
    """Agenda-derived minutes inheritance needs the full source archive, before audio work."""
    city = City(
        slug="c",
        provider="granicus",
        source={"feed_url": "https://x.granicus.com/f"},
        podcast_title="C",
        podcast_author="A",
        podcast_email="",
        podcast_description="",
        extract_audio=True,
    )
    episodes = [_ep("e1"), _ep("e2")]
    events: list[str] = []

    class _Stage:
        def __init__(self, name):
            self.name = name

    class _Pipeline:
        def __init__(self):
            self.ctx = StageContext(
                storage=None, ffmpeg=None, max_kbps=96, dry_run=False, lane=None
            )
            self.stages = [
                _Stage("links"),
                _Stage("agenda_text"),
                _Stage("minutes_text"),
                _Stage("audio"),
            ]

        def fetch_merge(self, _city, _key):
            return object(), episodes, {}, 0

        def accumulate_stats(self, _stats):
            pass

        def persist_source(self, _key, _eps, _persisted, *, notes):
            events.append("persist")

    def _run_stages(_p, _c, batch, stages, _ctx, *, quiet):
        events.append(
            f"{','.join(stage.name for stage in stages)}:{','.join(ep.uid for ep in batch)}"
        )
        return [StageStats(stage.name, ran=1) for stage in stages]

    monkeypatch.setattr(run, "run_stages", _run_stages)

    run._run_enrich_global_queue(_Pipeline(), [city], source_cache=None, max_workers=1, policy=None)

    assert events[0] == "links,agenda_text,minutes_text:uid-e1,uid-e2"
    assert events[1:3] == ["audio:uid-e1", "audio:uid-e2"]


def _retention_city(*, max_episodes=6000):
    return City(
        slug="retention-city",
        provider="granicus",
        source={"feed_url": "https://x.granicus.com/f"},
        podcast_title="Retention",
        podcast_author="A",
        podcast_email="",
        podcast_description="",
        max_episodes=max_episodes,
        full_artifact_episodes=max_episodes,
        extract_audio=True,
    )


def _retention_episodes(count):
    newest = datetime(2026, 7, 24, tzinfo=UTC)
    episodes = []
    for index in range(count):
        ep = _ep(f"archive-{index}")
        ep.published = newest - timedelta(minutes=index)
        episodes.append(ep)
    return episodes


def test_retention_projection_suppresses_full_granicus_overflow_before_planning(capsys):
    """GH#1025: a 5,480-row fetch with a 5,000-row source cap must expose only the exact
    post-persistence survivors to the global heavy-work queue, deterministically on every run."""
    city = _retention_city()
    episodes = _retention_episodes(5480)

    class _Pipeline:
        full_artifact_episodes = 5000
        metadata_retention_episodes = 5000
        max_archive_age_years = 1000

    persisted = {ep.uid: episode_to_record(ep) for ep in episodes[:5000]}
    first = run._retained_working_set(_Pipeline(), city, episodes, episodes, persisted)
    second = run._retained_working_set(_Pipeline(), city, episodes, episodes, persisted)

    assert len(first) == len(second) == 5000
    assert [ep.uid for ep in first] == [ep.uid for ep in second]
    assert {ep.uid for ep in first} == {ep.uid for ep in episodes[:5000]}
    prepared = {
        "source": {
            "city": city,
            "episodes": episodes,
            "retained_episodes": first,
        }
    }
    planned = run._order_global_candidates(prepared, policy=None)
    assert len(planned) == 5000
    assert not ({ep.uid for _, ep in planned} & {ep.uid for ep in episodes[5000:]})
    output = capsys.readouterr().out
    assert output.count("suppressed_rows=480") == 2


def test_retention_projection_handles_cap_changes_repairs_and_missing_audio():
    """Cap changes use the same deterministic policy as persistence. Repair markers outside the
    retained archive cannot force work, while a retained row lacking its audio pointer still can."""
    city = _retention_city(max_episodes=10)
    episodes = _retention_episodes(6)
    persisted = {ep.uid: episode_to_record(ep) for ep in episodes}
    persisted[episodes[-1].uid]["integrity"] = {"repair_audio": True}
    episodes[0].hosted_audio_url = None

    class _Pipeline:
        full_artifact_episodes = 3
        metadata_retention_episodes = 3
        max_archive_age_years = 1000

    retained = run._retained_working_set(_Pipeline(), city, episodes, episodes, persisted)
    assert [ep.uid for ep in retained] == [ep.uid for ep in episodes[:3]]
    assert retained[0].hosted_audio_url is None
    assert episodes[-1].uid not in {ep.uid for ep in retained}

    _Pipeline.full_artifact_episodes = 5
    _Pipeline.metadata_retention_episodes = 5
    raised = run._retained_working_set(_Pipeline(), city, episodes, episodes, persisted)
    assert [ep.uid for ep in raised] == [ep.uid for ep in episodes[:5]]

    _Pipeline.full_artifact_episodes = 2
    _Pipeline.metadata_retention_episodes = 2
    lowered = run._retained_working_set(_Pipeline(), city, episodes, episodes, persisted)
    assert [ep.uid for ep in lowered] == [ep.uid for ep in episodes[:2]]


def test_global_queue_never_runs_stages_for_rows_final_persistence_prunes(monkeypatch):
    """The retention boundary applies to source and per-item stages on repeated runs.

    Final persistence still receives every fresh observation, keeping append→prune authoritative.
    """
    city = _retention_city(max_episodes=10)
    episodes = _retention_episodes(4)
    stage_batches = []
    persisted_batches = []

    class _Stage:
        def __init__(self, name):
            self.name = name

    class _Pipeline:
        full_artifact_episodes = 2
        metadata_retention_episodes = 2
        max_archive_age_years = 1000

        def __init__(self):
            self.ctx = StageContext(
                storage=None, ffmpeg=None, max_kbps=96, dry_run=False, lane=None
            )
            self.stages = [_Stage("links"), _Stage("audio")]

        def fetch_merge(self, _city, _key):
            records = {ep.uid: episode_to_record(ep) for ep in episodes[:2]}
            return object(), episodes, records, 0

        def accumulate_stats(self, _stats):
            pass

        def persist_source(self, _key, batch, _persisted, *, notes):
            persisted_batches.append([ep.uid for ep in batch])

    def _run_stages(_provider, _city, batch, stages, _ctx, *, quiet):
        stage_batches.append(([stage.name for stage in stages], [ep.uid for ep in batch]))
        return [StageStats(stage.name, ran=len(batch)) for stage in stages]

    monkeypatch.setattr(run, "run_stages", _run_stages)
    pipeline = _Pipeline()
    for _ in range(2):
        run._run_enrich_global_queue(
            pipeline,
            [city],
            source_cache=None,
            max_workers=1,
            policy=None,
        )

    retained_uids = [ep.uid for ep in episodes[:2]]
    suppressed_uids = {ep.uid for ep in episodes[2:]}
    assert stage_batches == [
        (["links"], retained_uids),
        (["audio"], [retained_uids[0]]),
        (["audio"], [retained_uids[1]]),
        (["links"], retained_uids),
        (["audio"], [retained_uids[0]]),
        (["audio"], [retained_uids[1]]),
    ]
    assert all(not (set(batch) & suppressed_uids) for _, batch in stage_batches)
    # Each run checkpoints after audio and again at completion; both writes keep the full fresh
    # observation set so the single shared persistence boundary remains authoritative.
    assert persisted_batches == [[ep.uid for ep in episodes]] * 4


def test_repeat_persist_source_is_idempotent(tmp_path):
    """A source flushed early (incremental persist) and then again at end-of-run must not corrupt
    or duplicate records, and must not double-append notes to the caller's list (GH#377)."""
    from citypods.records import load_records

    state_dir = tmp_path / "state"
    ctx = StageContext(storage=None, ffmpeg=None, max_kbps=96, dry_run=False, lane=None)
    pipeline = run.SourcePipeline(
        state_dir=state_dir,
        stages=[],
        ctx=ctx,
        full_artifact_episodes=2000,
        metadata_retention_episodes=10000,
    )
    episodes = [_ep("a"), _ep("b")]
    notes: list[str] = []

    pipeline.persist_source("granicus/src", episodes, {}, notes=notes)
    first = load_records(state_dir, "granicus/src")
    pipeline.persist_source("granicus/src", episodes, {}, notes=notes)
    second = load_records(state_dir, "granicus/src")

    assert set(first) == set(second) == {"uid-a", "uid-b"}
    assert first == second  # byte-for-byte stable across the repeat persist
    assert notes == []  # caller's list untouched by repeat persists


def _set_actions_env(monkeypatch, run_number="63"):
    """The standard GITHUB_* vars _newer_run_queued reads, with a realistic GITHUB_WORKFLOW_REF
    (the ``@refs/heads/main`` suffix is the part the old parse tripped on)."""
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_RUN_ID", "100")
    monkeypatch.setenv("GITHUB_RUN_NUMBER", run_number)
    monkeypatch.setenv(
        "GITHUB_WORKFLOW_REF", "owner/repo/.github/workflows/deploy.yml@refs/heads/main"
    )


def _fake_actions_api(monkeypatch, payload, captured=None):
    """Patch urllib.request.urlopen to serve ``payload`` as the Actions API response, recording the
    requested URL into ``captured`` (a dict) when given."""
    import urllib.request

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(payload).encode()

    def _urlopen(req, timeout=0):
        if captured is not None:
            captured["url"] = req.full_url
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)


def test_newer_run_queued_uses_workflow_filename_not_branch(monkeypatch):
    # Regression guard for issue #63: GITHUB_WORKFLOW_REF's "@refs/heads/main" suffix must not be
    # mistaken for the workflow filename — querying /workflows/main/runs 404s and graceful yield
    # silently never fires (the run rides to its wall-clock deadline with newer builds queued).
    _set_actions_env(monkeypatch)
    captured: dict = {}
    _fake_actions_api(
        monkeypatch,
        {
            "workflow_runs": [
                {"id": 999, "run_number": 64, "status": "queued", "event": "push"},
                {
                    "id": 100,
                    "run_number": 63,
                    "status": "in_progress",
                    "event": "schedule",
                },  # this run
            ]
        },
        captured,
    )
    assert run._newer_run_queued() == "push"
    assert "/actions/workflows/deploy.yml/runs" in captured["url"]
    assert "/workflows/main/runs" not in captured["url"]


def test_newer_run_queued_false_for_older_or_completed(monkeypatch):
    _set_actions_env(monkeypatch)
    _fake_actions_api(
        monkeypatch,
        {
            "workflow_runs": [
                {
                    "id": 999,
                    "run_number": 64,
                    "status": "completed",
                    "event": "push",
                },  # newer but finished
                {
                    "id": 998,
                    "run_number": 62,
                    "status": "queued",
                    "event": "push",
                },  # queued but older
                {
                    "id": 100,
                    "run_number": 63,
                    "status": "in_progress",
                    "event": "schedule",
                },  # this run
            ]
        },
    )
    assert run._newer_run_queued() is None


# --- ffmpeg threads auto-calc -------------------------------------------------------


def test_ffmpeg_threads_autocalc_divides_by_native_audio_max_active(
    tmp_path, fake_provider, monkeypatch
):
    """Auto-calc must use native_audio_max_active as the divisor, not max_encodes_per_source.

    Bug: the old code divided by max_encodes_per_source (default 1), so clearing
    audio_ffmpeg_threads with native_audio_max_active=4 would give 4×4=16 threads on 4 cores.
    """
    monkeypatch.setattr("os.cpu_count", lambda: 4)
    config_dir = _setup(tmp_path)
    (tmp_path / "site_config.yml").write_text(
        f"state_dir: {tmp_path / 'state'}\ndefaults:\n  native_audio_max_active: 4\n"
        # No audio_ffmpeg_threads — triggers auto-calc path
    )

    captured: dict = {}
    original = run.CommandFfmpeg

    class _CapturingFfmpeg(original):
        def __init__(self, **kwargs):
            captured["threads"] = kwargs.get("threads")
            super().__init__(**kwargs)

    monkeypatch.setattr(run, "CommandFfmpeg", _CapturingFfmpeg)

    run.build(
        site_config_path=tmp_path / "site_config.yml",
        config_dir=config_dir,
        output_dir=tmp_path / "docs",
        base_url="https://example.test",
        dry_run=True,
    )

    assert captured["threads"] == 1  # 4 CPUs // 4 active encodes = 1 thread per encode


def test_build_closes_owned_ffmpeg_when_process_city_raises(tmp_path, fake_provider, monkeypatch):
    # M9/CR2-CP-37: an exception from a _process_city future used to skip ffmpeg.close()
    # entirely (it sat after the processing block, not in a finally), leaking the owned
    # ffmpeg process pool.
    config_dir = _setup(tmp_path)

    closed = []
    original = run.CommandFfmpeg

    class _CapturingFfmpeg(original):
        def close(self):
            closed.append(True)
            super().close()

    monkeypatch.setattr(run, "CommandFfmpeg", _CapturingFfmpeg)

    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(run, "_process_city", _boom)

    with pytest.raises(RuntimeError, match="boom"):
        run.build(
            site_config_path=tmp_path / "site_config.yml",
            config_dir=config_dir,
            output_dir=tmp_path / "docs",
            base_url="https://example.test",
        )

    assert closed == [True]


def test_newer_run_queued_detects_newer_scheduled_runs(monkeypatch):
    _set_actions_env(monkeypatch)
    _fake_actions_api(
        monkeypatch,
        {
            "workflow_runs": [
                {"id": 999, "run_number": 64, "status": "queued", "event": "schedule"},
                {"id": 100, "run_number": 63, "status": "in_progress", "event": "push"},
            ]
        },
    )
    assert run._newer_run_queued() == "schedule"


def test_newer_run_queued_logs_once_on_error(monkeypatch, capsys):
    # Silent fail-open is what hid the 404 for three fix attempts; assert it now says so — once.
    import urllib.error
    import urllib.request

    _set_actions_env(monkeypatch)

    def _boom(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    run._newer_run_queued._warned = False  # reset the module-level once-guard
    assert run._newer_run_queued() is None
    assert run._newer_run_queued() is None  # second failure stays silent
    assert capsys.readouterr().out.count("graceful-yield check failed") == 1


def test_stop_signal_records_fired_reason():
    s = run.StopSignal(deadline=time.monotonic() - 1)
    assert s() is True
    assert s.fired_reason == "wall-clock window spent"

    s2 = run.StopSignal(superseded=lambda: "push", poll_interval=0)
    assert s2() is True
    assert s2.fired_reason == "newer build queued behind this run"
    assert s2.should_exit_immediately() is True

    s_schedule = run.StopSignal(superseded=lambda: "schedule", poll_interval=0)
    assert s_schedule() is True
    assert s_schedule.fired_reason == "newer build queued behind this run"
    assert s_schedule.should_exit_immediately() is False

    s3 = run.StopSignal(superseded=lambda: None, poll_interval=0)
    assert s3() is False
    assert s3.should_exit_immediately() is False

    assert run.StopSignal().fired_reason is None  # never fired


def test_build_wires_a_stop_signal_when_time_bounded(tmp_path, fake_provider, monkeypatch):
    import citypods.run as run_mod

    captured = {}
    real_ctx = run_mod.StageContext

    def spy_ctx(*a, **kw):
        captured["stop"] = kw.get("stop")
        return real_ctx(*a, **kw)

    monkeypatch.setattr(run_mod, "StageContext", spy_ctx)
    cities = _setup(tmp_path)
    (tmp_path / "site_config.yml").write_text(
        f"state_dir: {tmp_path / 'state'}\n"
        "defaults:\n"
        "  audio_storage_backend: local\n"
        "  run_time_budget_minutes: 300\n"
    )
    _build(tmp_path, cities)
    assert isinstance(captured["stop"], run_mod.StopSignal)
    assert captured["stop"]() is False  # window not yet spent, not superseded


def test_asr_lane_uses_start_cutoff_and_backstop(tmp_path, fake_provider, monkeypatch):
    import citypods.run as run_mod

    captured = {}
    real_ctx = run_mod.StageContext

    def spy_ctx(*a, **kw):
        captured["asr_start_deadline"] = kw.get("asr_start_deadline")
        captured["asr_deadline"] = kw.get("asr_deadline")
        captured["asr_runtime_log_path"] = kw.get("asr_runtime_log_path")
        captured["asr_local_max_duration_hours"] = kw.get("asr_local_max_duration_hours")
        return real_ctx(*a, **kw)

    monkeypatch.setattr(run_mod, "StageContext", spy_ctx)
    monkeypatch.setattr(run_mod.time, "monotonic", lambda: 1000.0)
    cities = _setup(tmp_path)
    (tmp_path / "site_config.yml").write_text(
        f"state_dir: {tmp_path / 'state'}\n"
        "defaults:\n"
        "  audio_storage_backend: local\n"
        "  asr_enabled: false\n"
        "  run_time_budget_minutes: 240\n"
        "  budget_safety: 0.85\n"
        "  asr_start_cutoff_minutes: 285\n"
        "  asr_backstop_minutes: 350\n"
        "  asr_local_max_duration_hours: 3.5\n"
    )

    run.build(
        site_config_path=tmp_path / "site_config.yml",
        config_dir=cities,
        output_dir=tmp_path / "docs",
        base_url="https://example.test",
        phase="enrich",
        lane="transcribe",
    )

    assert captured["asr_start_deadline"] == pytest.approx(1000 + 285 * 60)
    assert captured["asr_deadline"] == pytest.approx(1000 + 350 * 60)
    assert captured["asr_runtime_log_path"].name == "asr_runtime_log.json"
    assert captured["asr_local_max_duration_hours"] == 3.5


def test_chapters_cap_defaults_unbounded_and_is_overridable(tmp_path, fake_provider, monkeypatch):
    import citypods.run as run_mod

    captured = {}
    real_ctx = run_mod.StageContext

    def spy_ctx(*a, **kw):
        captured["chapters_per_source"] = kw.get("chapters_per_source")
        return real_ctx(*a, **kw)

    monkeypatch.setattr(run_mod, "StageContext", spy_ctx)
    cities = _setup(tmp_path)
    (tmp_path / "site_config.yml").write_text(f"state_dir: {tmp_path / 'state'}\n")

    _build(tmp_path, cities)  # no cap -> production: effectively unbounded
    assert captured["chapters_per_source"] >= 10_000

    run.build(  # --chapters-cap (preview) flows through
        site_config_path=tmp_path / "site_config.yml",
        config_dir=cities,
        output_dir=tmp_path / "docs",
        base_url="https://example.test",
        chapters_cap=40,
    )
    assert captured["chapters_per_source"] == 40


# --- render/enrich phase split --------------------------------------------------------


class _CountingFfmpeg:
    def __init__(self):
        self.calls = 0

    def extract_audio(
        self,
        timeline,
        sources_by_id,
        dest,
        chapters=None,
        *,
        sources=None,
        loudness_profile=None,
        processing_profile=None,
        asset_resolver=None,
    ):
        self.calls += 1
        dest.write_bytes(b"fake-m4a" * 1024)  # >_MIN_PLAUSIBLE_AUDIO_BYTES (#39 truncation guard)


def _build_phase(tmp_path, cities, phase, ffmpeg, **kw):
    return run.build(
        site_config_path=tmp_path / "site_config.yml",
        config_dir=cities,
        output_dir=tmp_path / "docs",
        base_url="https://example.test",
        ffmpeg=ffmpeg,
        phase=phase,
        **kw,
    )


def _setup_multi(tmp_path):
    """Two feeds with distinct sources (so distinct ``source_key``s / shards), one provider."""
    config_dir = tmp_path / "config"
    feeds = config_dir / "feeds"
    feeds.mkdir(parents=True)
    for slug, url in (("feed-a", "https://a"), ("feed-b", "https://b")):
        (feeds / f"{slug}.yml").write_text(
            f"slug: {slug}\n"
            "provider: faketest\n"
            f"source: {{feed_url: '{url}'}}\n"
            f'podcast_title: "{slug}"\n'
            'podcast_author: "City of Fake"\n'
            'podcast_email: ""\n'
            'podcast_description: "desc"\n'
        )
    # See _setup(): disables the #497 preflight guard for these synthetic non-resolvable hosts.
    (tmp_path / "site_config.yml").write_text(
        f"state_dir: {tmp_path / 'state'}\ndefaults:\n  source_media_max_bytes: 0\n"
    )
    return config_dir


def test_render_phase_renders_without_encoding(tmp_path, fake_provider):
    for ep in fake_provider.episodes:
        ep.media_kind = "hls"  # would be re-hosted if the audio stage ran
    cities = _setup(tmp_path)
    ff = _CountingFfmpeg()
    _build_phase(tmp_path, cities, "render", ff)
    assert ff.calls == 0  # the cheap render phase never encodes
    assert (tmp_path / "docs" / "index.html").exists()  # but the site IS rendered
    assert (tmp_path / "docs" / "fake-city" / "index.html").exists()


def test_render_phase_persists_no_records(tmp_path, fake_provider, monkeypatch):
    """H11b: render writes ONLY docs/. It must not call ``save_records`` / ``push_state`` /
    ``reconcile_state`` — the separate enrich workflow is the sole record writer, so a stale
    render push can't clobber audio/transcripts it wrote (the record-write race, review/12 §H6)."""
    cities = _setup(tmp_path)
    ff = _CountingFfmpeg()
    calls = {"save_records": 0, "push_state": 0, "reconcile_state": 0}

    def _spy(name, retval=None):
        def _f(*_a, **_k):
            calls[name] += 1
            return retval

        return _f

    monkeypatch.setattr(run, "save_records", _spy("save_records"))
    monkeypatch.setattr(run, "push_state", _spy("push_state", 0))
    monkeypatch.setattr(run, "reconcile_state", _spy("reconcile_state", 0))

    _build_phase(tmp_path, cities, "render", ff)

    assert calls == {"save_records": 0, "push_state": 0, "reconcile_state": 0}
    # No record store is written to disk either (only docs/ is produced).
    assert not (tmp_path / "state" / "sources").exists()
    assert (tmp_path / "docs" / "index.html").exists()


def test_enrich_phase_persists_records(tmp_path, fake_provider, monkeypatch):
    """The counterpart to the render gate: the heavy phase IS the record writer, so it must still
    call ``save_records`` + ``push_state`` (guards the gate against over-broad suppression)."""
    cities = _setup(tmp_path)
    ff = _CountingFfmpeg()
    calls = {"save_records": 0, "push_state": 0}

    def _bump(name, retval=None):
        def _f(*_a, **_k):
            calls[name] += 1
            return retval

        return _f

    monkeypatch.setattr(run, "save_records", _bump("save_records"))
    monkeypatch.setattr(run, "push_state", _bump("push_state", 0))

    _build_phase(tmp_path, cities, "enrich", ff)

    assert calls["save_records"] >= 1
    assert calls["push_state"] >= 1


def test_enrich_phase_encodes_without_rendering(tmp_path, fake_provider):
    for ep in fake_provider.episodes:
        ep.media_kind = "hls"
    cities = _setup(tmp_path)
    ff = _CountingFfmpeg()
    _build_phase(tmp_path, cities, "enrich", ff)
    assert ff.calls == 2  # both HLS episodes encoded
    # No site render: the heavy phase only backfills + persists.
    assert not (tmp_path / "docs" / "index.html").exists()
    assert not (tmp_path / "docs" / "fake-city").exists()
    # It DOES record run history (the cost-bearing phase calibrates the projection).
    assert (tmp_path / "state" / "run_history.jsonl").exists()


def test_enrich_output_surfaces_in_next_render_via_records(tmp_path, fake_provider):
    """The phases hand off through the record store: audio encoded by enrich must appear in a later
    render even though render never encodes. Clearing the in-memory episodes first forces the audio
    to come back via merge_persisted (the fake provider otherwise reuses the same objects)."""
    for ep in fake_provider.episodes:
        ep.media_kind = "hls"
    cities = _setup(tmp_path)
    ff = _CountingFfmpeg()

    # 1) First render: no audio hosted yet -> HLS episodes omit their enclosure (no audio feed).
    _build_phase(tmp_path, cities, "render", ff)
    assert not (tmp_path / "docs" / "fake-city" / "audio_feed.xml").exists()

    # 2) Enrich: encode + persist hosted audio onto the records.
    _build_phase(tmp_path, cities, "enrich", ff)
    assert ff.calls == 2

    # 3) Wipe the in-memory audio so the next render can only succeed via the persisted records.
    for ep in fake_provider.episodes:
        ep.hosted_audio_url = ep.audio_key = ep.audio_spec_hash = None
    _build_phase(tmp_path, cities, "render", ff)
    assert ff.calls == 2  # render still didn't encode
    feed = (tmp_path / "docs" / "fake-city" / "audio_feed.xml").read_text()
    assert "<enclosure" in feed  # audio is back, carried by the record store


def test_render_phase_uses_persisted_archive_when_provider_fetch_fails(tmp_path, fake_provider):
    cities = _setup(tmp_path)
    ff = _CountingFfmpeg()

    # Seed the record store via the enrich phase (post-H11b the enrich workflow is the sole record
    # writer; render no longer persists). In production these records are restored from the bucket.
    enriched = _build_phase(tmp_path, cities, "enrich", ff)
    assert [r.status for r in enriched] == ["built"]
    assert (tmp_path / "state" / "sources").exists()  # records persisted by enrich

    # A transient provider outage during the fast render phase: render must publish the last
    # known-good archive from the record store instead of failing the whole Pages deploy.
    fake_provider.error = ProviderError("GET https://x failed: timed out")

    second = _build_phase(tmp_path, cities, "render", ff)

    assert [r.status for r in second] == ["built"]
    assert "stale provider fetch failed" in second[0].detail
    assert (tmp_path / "docs" / "fake-city" / "index.html").exists()
    feed = (tmp_path / "docs" / "fake-city" / "video_feed.xml").read_text()
    assert "City Council" in feed


# --- H6b: sharded / lane-pinned enrich -------------------------------------------------


def test_enrich_shards_partition_sources_disjoint_and_exhaustive(tmp_path, fake_provider):
    """The acceptance: across shards each configured source is enriched by exactly one shard, so
    two concurrent shards never touch the same record file."""
    from citypods.records import shard_assignment, source_key

    cities_dir = _setup_multi(tmp_path)
    ff = _CountingFfmpeg()
    n = 2
    by_shard = {}
    for k in range(n):
        # Each shard gets its own state dir, mirroring the real workflow where every matrix job
        # restores the same pre-run state cache into an isolated runner — a sibling shard's
        # mid-run writes (e.g. finishing an encode) must never be visible to another shard's
        # weight computation within the same workflow execution.
        (tmp_path / "site_config.yml").write_text(f"state_dir: {tmp_path / f'state-{k}'}\n")
        results = _build_phase(tmp_path, cities_dir, "enrich", ff, shard=(k, n))
        by_shard[k] = sorted(r.slug for r in results)
    # Every slug appears in exactly one shard (disjoint + exhaustive).
    flat = [slug for slugs in by_shard.values() for slug in slugs]
    assert sorted(flat) == ["feed-a", "feed-b"]
    assert len(flat) == len(set(flat))
    # And the partition matches the source-atomic shard assignment.
    from citypods.config import load_city_configs

    cfg = load_city_configs(cities_dir, {})
    assignment = shard_assignment((source_key(c) for c in cfg), n)
    for c in cfg:
        assert c.slug in by_shard[assignment[source_key(c)]]


def test_enrich_consumes_canonical_plan_and_skips_duplicate_state_restore(
    tmp_path, fake_provider, monkeypatch
):
    from citypods.config import load_city_configs
    from citypods.records import source_key
    from citypods.sharding import ShardPlan, save_shard_plan

    cities_dir = _setup_multi(tmp_path)
    cfg = load_city_configs(cities_dir, {})
    keys = {city.slug: source_key(city) for city in cfg}
    plan_keys = {
        slug: [f"{key}/{ep.uid}" for ep in fake_provider.episodes] for slug, key in keys.items()
    }
    plan_path = tmp_path / "asr-plan.json"
    save_shard_plan(
        plan_path,
        ShardPlan(
            lane="transcribe",
            num_shards=2,
            assignment={
                **{key: 1 for key in plan_keys["feed-a"]},
                **{key: 0 for key in plan_keys["feed-b"]},
            },
            weights={
                **{key: 10 for key in plan_keys["feed-a"]},
                **{key: 1 for key in plan_keys["feed-b"]},
            },
            unit="episode",
        ),
    )
    pulls = []
    monkeypatch.setattr(run, "pull_state", lambda *_a, **_k: pulls.append(True))

    results = _build_phase(
        tmp_path,
        cities_dir,
        "enrich",
        _CountingFfmpeg(),
        shard=(0, 2),
        lane="transcribe",
        shard_plan_path=plan_path,
        state_snapshot_restored=True,
    )

    assert [result.slug for result in results] == ["feed-b"]
    assert pulls == []


def test_enrich_source_scopes_to_one_source(tmp_path, fake_provider):
    from citypods.config import load_city_configs
    from citypods.records import source_key

    cities_dir = _setup_multi(tmp_path)
    key_a = next(source_key(c) for c in load_city_configs(cities_dir, {}) if c.slug == "feed-a")
    results = _build_phase(tmp_path, cities_dir, "enrich", _CountingFfmpeg(), source=key_a)
    assert [r.slug for r in results] == ["feed-a"]


def test_enrich_source_and_shard_run_history_keeps_source_filter(tmp_path, fake_provider):
    # Regression: the shard block must not shadow the ``source`` parameter with the plan-origin
    # description, or combining --source and --shard corrupts run_history's recorded source.
    import json

    from citypods.config import load_city_configs
    from citypods.records import source_key

    cities_dir = _setup_multi(tmp_path)
    key_a = next(source_key(c) for c in load_city_configs(cities_dir, {}) if c.slug == "feed-a")
    _build_phase(tmp_path, cities_dir, "enrich", _CountingFfmpeg(), source=key_a, shard=(0, 1))
    hist = (tmp_path / "state" / "run_history.jsonl").read_text().strip().splitlines()
    entry = json.loads(hist[-1])
    assert entry["source"] == key_a  # the --source filter, not "computed in-process"


def test_enrich_shard_scopes_state_push_and_skips_reconcile(tmp_path, fake_provider, monkeypatch):
    """A sharded run pushes back ONLY its owned ``sources/<key>/`` records (via the foreign-block-
    preserving ``push_records_merged``) and does not reconcile — the H6b/H11b scope hooks, so
    concurrent shards (and lanes) never clobber a sibling's records."""
    from citypods.config import load_city_configs
    from citypods.records import shard_assignment, source_key

    cities_dir = _setup_multi(tmp_path)
    captured = {}

    def _push_merged(
        _storage,
        _state_dir,
        source_keys,
        *,
        protected_blocks,
        lane=None,
        owned_uids=None,
        log=None,
    ):
        captured["owned"] = sorted(set(source_keys))
        captured["protected"] = protected_blocks
        captured["owned_uids"] = owned_uids
        return len(captured["owned"])

    def _push(_storage, _state_dir, *, only_prefixes=None, only_paths=None, log=None):
        captured["only_prefixes"] = only_prefixes
        captured["only_paths"] = only_paths  # the exact current run event
        return 0

    def _push_asr_log(_storage, _state_dir, *, rel_path, log=None):
        captured["asr_runtime_log"] = rel_path
        return 0

    def _reconcile(_storage, _state_dir, *, full_run=True, **_k):
        captured["full_run"] = full_run
        return 0

    monkeypatch.setattr(run, "push_records_merged", _push_merged)
    monkeypatch.setattr(run, "push_state", _push)
    monkeypatch.setattr(run, "push_asr_runtime_log_merged", _push_asr_log)
    monkeypatch.setattr(run, "reconcile_state", _reconcile)

    _build_phase(tmp_path, cities_dir, "enrich", _CountingFfmpeg(), shard=(0, 2))

    assert captured["full_run"] is False  # a shard never sweeps siblings' records
    cfg = load_city_configs(cities_dir, {})
    assignment = shard_assignment((source_key(c) for c in cfg), 2)
    owned = sorted({source_key(c) for c in cfg if assignment[source_key(c)] == 0})
    assert captured["owned"] == owned  # records pushed only for owned sources
    assert captured["owned_uids"] is None  # audio is source-atomic → own every uid (review/18 §2.3)
    assert captured["only_prefixes"] is None
    assert len(captured["only_paths"]) == 1
    assert captured["only_paths"][0].startswith("run_events/")
    assert captured["asr_runtime_log"] == "asr_runtime_log.json"
    events = list((tmp_path / "state" / "run_events").glob("*.json"))
    assert len(events) == 1
    assert json.loads(events[0].read_text())["scoped"] is True


def test_enrich_lane_threads_protected_blocks_into_push(tmp_path, fake_provider, monkeypatch):
    """A transcribe-lane shard must hand ``push_records_merged`` the ``audio`` block to preserve, so
    a late-finishing ASR run can't erase a concurrent audio run's hosted audio (review/12 §H6)."""
    for ep in fake_provider.episodes:
        ep.media_kind = "hls"
    cities_dir = _setup_multi(tmp_path)
    captured = {}

    def _push_merged(
        _storage,
        _state_dir,
        source_keys,
        *,
        protected_blocks,
        lane=None,
        owned_uids=None,
        log=None,
    ):
        captured["protected"] = protected_blocks
        captured["owned_uids"] = owned_uids
        return 0

    monkeypatch.setattr(run, "push_records_merged", _push_merged)
    monkeypatch.setattr(run, "push_state", lambda *a, **k: 0)
    monkeypatch.setattr(run, "reconcile_state", lambda *a, **k: 0)

    _build_phase(tmp_path, cities_dir, "enrich", _CountingFfmpeg(), shard=(0, 2), lane="transcribe")
    # The transcribe lane preserves the artifact blocks it does not write: hosted audio,
    # audio-derived media availability, and diarize-owned speakers.
    assert captured["protected"] == frozenset(
        {
            "audio",
            "speakers",
            "media_availability",
            "integrity",
            "agenda_text",
            "agenda_backup",
            "minutes_text",
            "minutes_votes",
            "minutes_roster",
            "tags",
            "chapter_tags",
            "llm_tag_candidates",
            "tags_llm_call_attempts",
            "tags_llm_recipe_hash",
            "tags_spec_hash",
            "tags_input_fingerprint",
            "generated_agenda_candidates",
            "generated_chapters",
            "generated_chapters_spec_hash",
            "moments",
        }
    )
    # transcribe plans per-episode → push receives a per-source owned-uid map, not None (§3.2).
    assert isinstance(captured["owned_uids"], dict)


def test_unsharded_enrich_pushes_everything_and_reconciles(tmp_path, fake_provider, monkeypatch):
    """The full (unsharded) run keeps the whole-snapshot push + the reconcile sweep."""
    cities_dir = _setup_multi(tmp_path)
    captured = {}

    def _push(*_a, only_prefixes=None, **_k):
        captured["op"] = only_prefixes
        return 0

    def _reconcile(*_a, full_run=True, **_k):
        captured["fr"] = full_run
        return 0

    monkeypatch.setattr(run, "push_state", _push)
    monkeypatch.setattr(run, "reconcile_state", _reconcile)
    _build_phase(tmp_path, cities_dir, "enrich", _CountingFfmpeg())
    assert captured["op"] is None  # whole-snapshot push
    assert captured["fr"] is True  # full reconcile sweep


def test_lane_audio_runs_audio_pass_only(tmp_path, fake_provider):
    for ep in fake_provider.episodes:
        ep.media_kind = "hls"
    cities = _setup(tmp_path)
    ff = _CountingFfmpeg()
    _build_phase(tmp_path, cities, "enrich", ff, lane="audio")
    assert ff.calls == 2  # the audio lane encodes


@pytest.mark.parametrize("lane", ["transcribe", "align"])
def test_transcript_lanes_skip_the_audio_pass(tmp_path, fake_provider, lane):
    """``--lane transcribe``/``align`` run ONLY the transcript pass — the audio pass is skipped, so
    no encoding happens (the acceptance's '--lane align runs only the transcript pass')."""
    for ep in fake_provider.episodes:
        ep.media_kind = "hls"
    cities = _setup(tmp_path)
    ff = _CountingFfmpeg()
    _build_phase(tmp_path, cities, "enrich", ff, lane=lane)
    assert ff.calls == 0  # audio pass skipped; only the transcript pass would run


def test_transcript_lane_provider_fetch_errors_use_persisted_archive(
    tmp_path, fake_provider, capsys
):
    """ASR shard workers should use already-hosted audio when provider refresh fails."""
    for ep in fake_provider.episodes:
        ep.media_kind = "hls"
    cities = _setup(tmp_path)
    ff = _CountingFfmpeg()
    first = _build_phase(tmp_path, cities, "enrich", ff, lane="audio")
    assert [r.status for r in first] == ["built"]
    assert ff.calls == 2

    fake_provider.error = ProviderError("GET https://x failed: timed out")

    results = _build_phase(tmp_path, cities, "enrich", ff, lane="transcribe")

    out = capsys.readouterr().out
    assert [r.status for r in results] == ["built"]
    assert "[enrich] source stale" in out
    assert "[enrich] transcript pass: 2 item(s) with audio" in out


def test_transcript_lane_provider_fetch_errors_defer_without_archive(tmp_path, fake_provider):
    """A first-ever ASR run with no records still completes cleanly and defers the source."""
    cities = _setup(tmp_path)
    fake_provider.error = ProviderError("GET https://x failed: timed out")

    results = _build_phase(tmp_path, cities, "enrich", _CountingFfmpeg(), lane="transcribe")

    assert [r.status for r in results] == ["skipped"]
    assert "transcript lane deferred" in results[0].detail


def test_audio_lane_provider_fetch_errors_remain_failed(tmp_path, fake_provider):
    """The audio/full enrich lanes still surface provider fetch failures as run errors."""
    cities = _setup(tmp_path)
    fake_provider.error = ProviderError("GET https://x failed: timed out")

    results = _build_phase(tmp_path, cities, "enrich", _CountingFfmpeg(), lane="audio")

    assert [r.status for r in results] == ["error"]


def test_audio_lane_transport_fetch_errors_defer(tmp_path, fake_provider):
    """An exhausted requests transport retry is recoverable, not a permanent provider failure."""
    import requests

    cities = _setup(tmp_path)
    cause = requests.exceptions.ReadTimeout("read timed out")
    fake_provider.error = ProviderError("GET https://x failed: read timed out")
    fake_provider.error.__cause__ = cause

    results = _build_phase(tmp_path, cities, "enrich", _CountingFfmpeg(), lane="audio")

    assert [r.status for r in results] == ["skipped"]
    assert "provider fetch deferred" in results[0].detail


def test_build_rejects_unknown_lane(tmp_path, fake_provider):
    cities = _setup(tmp_path)
    with pytest.raises(ValueError, match="unknown lane"):
        _build_phase(tmp_path, cities, "enrich", _CountingFfmpeg(), lane="bogus")


def test_tag_lane_is_accepted(tmp_path, fake_provider):
    """`--lane tag` is what the daily tag.yml workflow runs (`enrich --lane tag`, no --source/
    --shard). It was already fully wired into the CLI's --lane choices, LANE_STAGES, and
    _LANE_OWNED_BLOCKS, but _build_impl's own whitelist still only accepted audio/transcribe/
    align, so every scheduled run failed immediately with "unknown lane 'tag'" before TagsStage
    ever ran."""
    cities = _setup(tmp_path)

    results = _build_phase(tmp_path, cities, "enrich", _CountingFfmpeg(), lane="tag")

    assert [r.status for r in results] == ["built"]


# --- H5 PR3: global two-pass enrich queue ----------------------------------------------

_NOW = datetime(2026, 6, 12, tzinfo=UTC)


def _dated_ep(guid, days_ago):
    return Episode(
        guid=guid,
        uid=f"uid-{guid}",
        title="Meeting",
        published=_NOW - timedelta(days=days_ago),
        video_url=f"https://x/{guid}.mp4",
        media_kind="hls",
        body="City Council",
    )


def _bare_city(slug):
    return City(
        slug=slug,
        provider="faketest",
        source={},
        podcast_title="t",
        podcast_author="a",
        podcast_email="",
        podcast_description="d",
    )


def test_order_global_candidates_newest_everywhere_first():
    """The core PR3 deliverable: candidates are ordered newest-first ACROSS sources, not grouped
    per source (which is all the per-source pool + within-source ordering could give)."""
    from citypods.ops.workqueue import BacklogPolicy
    from citypods.run import _order_global_candidates

    prepared = {
        "srcA": {
            "city": _bare_city("a-tx"),
            "episodes": [_dated_ep("a_old", 10), _dated_ep("a_new", 2)],
        },
        "srcB": {
            "city": _bare_city("b-tx"),
            "episodes": [_dated_ep("b_mid", 5), _dated_ep("b_newest", 1)],
        },
    }
    policy = BacklogPolicy.from_site_config({"backlog_priority": [{"recency": "desc"}]}, now=_NOW)
    order = [ep.guid for _, ep in _order_global_candidates(prepared, policy)]
    assert order == ["b_newest", "a_new", "b_mid", "a_old"]


def test_order_global_candidates_identity_without_policy():
    """No policy → per-source materialized sets concatenated in source order; no cross mix."""
    from citypods.run import _order_global_candidates

    prepared = {
        "srcA": {
            "city": _bare_city("a-tx"),
            "episodes": [_dated_ep("a_old", 10), _dated_ep("a_new", 2)],
        },
        "srcB": {"city": _bare_city("b-tx"), "episodes": [_dated_ep("b_newest", 1)]},
    }
    keys_order = [key for key, _ in _order_global_candidates(prepared, None)]
    assert keys_order == ["srcA", "srcA", "srcB"]  # all of A (newest-first per body), then B


def test_enrich_phase_two_pass_and_manifest(tmp_path, fake_provider, capsys):
    """The enrich phase runs the on-runner AUDIO pass then the decoupled TRANSCRIPT pass, and
    persists the work manifest."""
    for ep in fake_provider.episodes:
        ep.media_kind = "hls"
    cities = _setup(tmp_path)
    (tmp_path / "site_config.yml").write_text(
        f"state_dir: {tmp_path / 'state'}\n"
        "defaults:\n  asr_enabled: false\n  source_media_max_bytes: 0\n"
    )
    ff = _CountingFfmpeg()
    _build_phase(tmp_path, cities, "enrich", ff)
    out = capsys.readouterr().out
    assert "[enrich] audio pass:" in out and "[enrich] audio pass done" in out
    # Transcript is a SEPARATE pass over episodes that now have hosted audio (decoupled).
    assert "[enrich] transcript pass: 2 item(s) with audio" in out
    assert (tmp_path / "state" / "work.json").exists()


def test_no_refresh_renders_from_records_without_any_fetch(tmp_path, fake_provider):
    """--no-refresh (PR preview) renders from the record store with ZERO provider connections."""
    cities = _setup(tmp_path)
    _build(tmp_path, cities)  # normal build persists records
    assert fake_provider.fetches >= 1
    fetches_before = fake_provider.fetches

    # Fresh output dir so it must re-render (the first build's docs/ would otherwise hash-skip).
    # state_dir is fixed by site_config, so records persist across output dirs.
    results = run.build(
        site_config_path=tmp_path / "site_config.yml",
        config_dir=cities,
        output_dir=tmp_path / "docs2",
        base_url="https://example.test",
        phase="render",
        no_refresh=True,
    )
    assert fake_provider.fetches == fetches_before  # no new provider fetch
    assert [r.status for r in results] == ["built"]
    feed = (tmp_path / "docs2" / "fake-city" / "audio_feed.xml").read_text()
    assert feed.count("<item>") == 2  # rendered g1 + g2 from records, not a live fetch


def test_no_refresh_empty_store_is_not_an_error(tmp_path, fake_provider):
    """An empty record store renders an empty feed (not an error) and still makes no connection."""
    cities = _setup(tmp_path)  # no prior build → no records
    results = run.build(
        site_config_path=tmp_path / "site_config.yml",
        config_dir=cities,
        output_dir=tmp_path / "docs",
        base_url="https://example.test",
        phase="render",
        no_refresh=True,
    )
    assert fake_provider.fetches == 0  # never touched the provider
    assert [r.status for r in results] != ["error"]


def test_render_phase_default_still_refreshes_from_provider(tmp_path, fake_provider):
    """Production deploy path (`--phase render`, no `--no-refresh`) is unchanged: it still does a
    live provider refresh. Guards against no_refresh accidentally leaking to deploy."""
    cities = _setup(tmp_path)
    results = run.build(
        site_config_path=tmp_path / "site_config.yml",
        config_dir=cities,
        output_dir=tmp_path / "docs",
        base_url="https://example.test",
        phase="render",  # no_refresh defaults False — exactly what deploy.yml runs
    )
    assert fake_provider.fetches >= 1  # fetched live, as production does
    assert [r.status for r in results] == ["built"]


def test_tag_lane_pre_filters_candidate_episodes(tmp_path, monkeypatch):
    """In the tag lane, _run_enrich_global_queue filters candidate_episodes to only items that
    actually need tags, while retained_episodes remains intact."""
    from citypods.run import SourcePipeline, _run_enrich_global_queue
    from citypods.stages import StageContext
    from citypods.tags import load_taxonomy, tag_input_fingerprint

    taxonomy_file = tmp_path / "taxonomy.yml"
    taxonomy_file.write_text(
        "version: 1\nreviewed_at: '2026-01-01'\nsource_refs: {x: 'https://example.test'}\n"
        "tags:\n  - id: housing\n    label: Housing\n    description: desc\n    group: land-use\n"
        "    source_refs: [x]\n    rules: {include: [housing]}\n"
    )
    taxonomy = load_taxonomy(taxonomy_file)

    ep_clean = Episode(
        guid="clean",
        uid="uid-clean",
        title="Clean Meeting",
        published=_NOW,
        video_url="https://x/clean.mp4",
        media_kind="hls",
        body="Council",
    )
    fp = tag_input_fingerprint(ep_clean, taxonomy, llm_enabled=False)
    ep_clean.tags_input_fingerprint = fp
    ep_clean.tags_spec_hash = "hash-clean"

    ep_dirty = Episode(
        guid="dirty",
        uid="uid-dirty",
        title="Dirty Meeting",
        published=_NOW,
        video_url="https://x/dirty.mp4",
        media_kind="hls",
        body="Council",
    )

    city = _bare_city("test-city")

    class _FakeProvider:
        def fetch_episodes(self):
            return [ep_clean, ep_dirty]

    class _CountingStage:
        name = "tags"

        def __init__(self):
            self.processed = []

        def process(self, provider, city, episodes, ctx):
            self.processed.extend(episodes)
            from citypods.stages import StageStats

            return StageStats(self.name)

    tag_stage = _CountingStage()
    ctx = StageContext(
        storage=None,
        ffmpeg=None,
        max_kbps=96,
        dry_run=True,
        lane="tag",
        taxonomy_path=taxonomy_file,
    )
    pipeline = SourcePipeline(
        state_dir=tmp_path / "state",
        stages=[tag_stage],
        ctx=ctx,
        full_artifact_episodes=2000,
        metadata_retention_episodes=10000,
    )
    pipeline.fetch_merge = lambda city, key: (_FakeProvider(), [ep_clean, ep_dirty], {}, 0)
    pipeline.persist_source = lambda key, eps, persisted, notes=None: None

    results = _run_enrich_global_queue(
        pipeline,
        [city],
        source_cache=None,
        max_workers=1,
        policy=None,
    )
    assert len(results) == 1
    # Only ep_dirty should have entered the global candidate queue and been processed!
    assert tag_stage.processed == [ep_dirty]


def test_tag_batch_submission_failure_replaces_provisional_defer_with_error():
    """A rejected batched tag job must not be persisted as a pending worker handle."""
    from citypods.run import _record_tag_batch_submission_failures

    ep = _ep("batch-failure")
    ep.tags_llm_call_attempts = [
        {
            "purpose": "topic-tags:tagger",
            "recipe_hash": "parent-recipe",
            "job_recipe_hashes": ["parent-recipe-tag-batch-0"],
            "status": "deferred",
            "reason": "",
        },
        {
            "purpose": "topic-tags:prelabeler",
            "recipe_hash": "prelabeler-recipe",
            "job_recipe_hashes": ["prelabeler-recipe-tag-batch-0"],
            "status": "deferred",
            "reason": "",
        },
    ]
    failures = [
        BatchDispatchOutcome(
            job=InferenceJob(task="tag", recipe_hash="parent-recipe-tag-batch-0"),
            result=LLMBackendError("idempotency conflict"),
        ),
        BatchDispatchOutcome(
            job=InferenceJob(task="tag", recipe_hash="prelabeler-recipe-tag-batch-0"),
            result=LLMBackendError("ingress unavailable"),
        ),
    ]

    assert (
        _record_tag_batch_submission_failures(
            {"source": {"episodes": [ep], "persist_episodes": [ep]}}, failures
        )
        == 2
    )
    tagger, prelabeler = ep.tags_llm_call_attempts
    assert tagger["status"] == "error"
    assert tagger["reason"] == "batch submission failed: idempotency conflict"
    assert tagger["batch_submission_errors"] == [
        {"recipe_hash": "parent-recipe-tag-batch-0", "reason": "idempotency conflict"}
    ]
    assert prelabeler["status"] == "error"
    assert prelabeler["reason"] == "batch submission failed: ingress unavailable"
    assert prelabeler["batch_submission_errors"] == [
        {"recipe_hash": "prelabeler-recipe-tag-batch-0", "reason": "ingress unavailable"}
    ]
