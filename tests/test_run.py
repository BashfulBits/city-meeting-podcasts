"""Tests for incremental builds: content-hash change detection + state persistence."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

import pytest

from citypods import run
from citypods.models import City, Episode
from citypods.providers import get_provider, register
from citypods.providers.base import ProviderError
from citypods.records import feed_content_hash
from citypods.state import build_fingerprint


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


def test_build_fingerprint_tracks_base_url_and_templates():
    assert build_fingerprint("https://a") != build_fingerprint("https://b")
    assert build_fingerprint("https://a") == build_fingerprint("https://a")


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
    (tmp_path / "site_config.yml").write_text(f"state_dir: {tmp_path / 'state'}\n")
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


def test_changed_content_rebuilds(tmp_path, fake_provider):
    cities = _setup(tmp_path)
    _build(tmp_path, cities)

    fake_provider.episodes.append(_ep("g3", title="Planning Commission"))
    result = _build(tmp_path, cities)
    assert [r.status for r in result] == ["built"]


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
            loudness_profile=None,
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
            loudness_profile=None,
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
        loudness_profile=None,
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
    (tmp_path / "site_config.yml").write_text(f"state_dir: {tmp_path / 'state'}\n")
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
        results = _build_phase(tmp_path, cities_dir, "enrich", ff, shard=(k, n))
        by_shard[k] = sorted(r.slug for r in results)
    # Every slug appears in exactly one shard (disjoint + exhaustive).
    flat = [slug for slugs in by_shard.values() for slug in slugs]
    assert sorted(flat) == ["feed-a", "feed-b"]
    assert len(flat) == len(set(flat))
    # And the partition matches the balanced shard assignment.
    from citypods.config import load_city_configs

    cfg = load_city_configs(cities_dir, {})
    assignment = shard_assignment((source_key(c) for c in cfg), n)
    for c in cfg:
        assert c.slug in by_shard[assignment[source_key(c)]]


def test_enrich_source_scopes_to_one_source(tmp_path, fake_provider):
    from citypods.config import load_city_configs
    from citypods.records import source_key

    cities_dir = _setup_multi(tmp_path)
    key_a = next(source_key(c) for c in load_city_configs(cities_dir, {}) if c.slug == "feed-a")
    results = _build_phase(tmp_path, cities_dir, "enrich", _CountingFfmpeg(), source=key_a)
    assert [r.slug for r in results] == ["feed-a"]


def test_enrich_shard_scopes_state_push_and_skips_reconcile(tmp_path, fake_provider, monkeypatch):
    """A sharded run pushes back ONLY its owned ``sources/<key>/`` records and does not reconcile —
    the H11b scope hooks, so concurrent shards never clobber a sibling's records."""
    from citypods.config import load_city_configs
    from citypods.records import shard_assignment, source_key

    cities_dir = _setup_multi(tmp_path)
    captured = {}

    def _push(_storage, _state_dir, *, only_prefixes=None):
        captured["only_prefixes"] = only_prefixes
        return 0

    def _reconcile(_storage, _state_dir, *, full_run=True, **_k):
        captured["full_run"] = full_run
        return 0

    monkeypatch.setattr(run, "push_state", _push)
    monkeypatch.setattr(run, "reconcile_state", _reconcile)

    _build_phase(tmp_path, cities_dir, "enrich", _CountingFfmpeg(), shard=(0, 2))

    assert captured["full_run"] is False  # a shard never sweeps siblings' records
    cfg = load_city_configs(cities_dir, {})
    assignment = shard_assignment((source_key(c) for c in cfg), 2)
    owned = {f"sources/{source_key(c)}/" for c in cfg if assignment[source_key(c)] == 0}
    assert set(captured["only_prefixes"]) == owned


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


def test_build_rejects_unknown_lane(tmp_path, fake_provider):
    cities = _setup(tmp_path)
    with pytest.raises(ValueError, match="unknown lane"):
        _build_phase(tmp_path, cities, "enrich", _CountingFfmpeg(), lane="bogus")


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
        f"state_dir: {tmp_path / 'state'}\ndefaults:\n  asr_enabled: false\n"
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
