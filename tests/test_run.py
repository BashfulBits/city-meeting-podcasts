"""Tests for incremental builds: content-hash change detection + state persistence."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime

import pytest

from citypods import run
from citypods.models import Episode
from citypods.providers import get_provider, register
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

    def validate(self, source):
        pass

    def detect_change(self, source):
        return None  # no HTTP validator -> exercises the content-hash path

    def fetch_episodes(self, source):
        self.fetches += 1
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


def test_build_logs_audio_stage_activity_and_errors(tmp_path, fake_provider, capsys):
    """The per-stage run summary must surface audio activity (and sample errors) to stdout, so a
    re-host that triggers but fails downstream is visible rather than hiding behind the
    feed-level "0 errors" line (issue #116 follow-up)."""
    import subprocess

    for ep in fake_provider.episodes:
        ep.media_kind = "hls"  # forces re-hosting via materialize_audio

    class _FailingFfmpeg:
        def extract_audio(self, source_url, dest, chapters=None):
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
        def extract_audio(self, source_url, dest, chapters=None):
            dest.write_bytes(b"fake-m4a")

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
                {"id": 999, "run_number": 64, "status": "queued"},
                {"id": 100, "run_number": 63, "status": "in_progress"},  # this run
            ]
        },
        captured,
    )
    assert run._newer_run_queued() is True
    assert "/actions/workflows/deploy.yml/runs" in captured["url"]
    assert "/workflows/main/runs" not in captured["url"]


def test_newer_run_queued_false_for_older_or_completed(monkeypatch):
    _set_actions_env(monkeypatch)
    _fake_actions_api(
        monkeypatch,
        {
            "workflow_runs": [
                {"id": 999, "run_number": 64, "status": "completed"},  # newer but finished
                {"id": 998, "run_number": 62, "status": "queued"},  # queued but older
                {"id": 100, "run_number": 63, "status": "in_progress"},  # this run
            ]
        },
    )
    assert run._newer_run_queued() is False


def test_newer_run_queued_logs_once_on_error(monkeypatch, capsys):
    # Silent fail-open is what hid the 404 for three fix attempts; assert it now says so — once.
    import urllib.error
    import urllib.request

    _set_actions_env(monkeypatch)

    def _boom(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    run._newer_run_queued._warned = False  # reset the module-level once-guard
    assert run._newer_run_queued() is False
    assert run._newer_run_queued() is False  # second failure stays silent
    assert capsys.readouterr().out.count("graceful-yield check failed") == 1


def test_stop_signal_records_fired_reason():
    s = run.StopSignal(deadline=time.monotonic() - 1)
    assert s() is True
    assert s.fired_reason == "wall-clock window spent"

    s2 = run.StopSignal(superseded=lambda: True, poll_interval=0)
    assert s2() is True
    assert s2.fired_reason == "newer build queued behind this run"

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
