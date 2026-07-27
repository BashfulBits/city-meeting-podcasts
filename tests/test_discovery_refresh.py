from datetime import UTC, datetime, timedelta

from citypods.discovery.refresh import (
    dirty_uids,
    episode_input_fingerprint,
    next_poll_at,
    refresh_due,
)
from citypods.models import ChangeToken, City, Episode
from citypods.providers import _REGISTRY, register
from citypods.records import episode_to_record, save_records, source_key
from citypods.run import SourcePipeline
from citypods.stages import StageContext


def _episode(*, guid="provider-id", title="Council"):
    return Episode(
        guid=guid,
        uid="stable-uid",
        title=title,
        published=datetime(2026, 7, 24, tzinfo=UTC),
        video_url="https://cdn.example/video.mp4?token=old",
    )


def test_input_fingerprint_ignores_provider_guid_and_expiring_media_query():
    first = _episode()
    second = _episode(guid="migrated-id")
    second.video_url = "https://cdn.example/video.mp4?token=new"
    assert episode_input_fingerprint(first) == episode_input_fingerprint(second)


def test_input_fingerprint_ignores_common_presigned_query_shapes():
    first = _episode()
    first.video_url = (
        "https://cdn.example/video.mp4?X-Amz-Algorithm=one&X-Amz-Signature=old"
        "&Policy=old&Key-Pair-Id=old&hdnea=old&keep=1"
    )
    second = _episode()
    second.video_url = (
        "https://cdn.example/video.mp4?X-Amz-Algorithm=two&X-Amz-Signature=new"
        "&Policy=new&Key-Pair-Id=new&hdnea=new&keep=1"
    )
    assert episode_input_fingerprint(first) == episode_input_fingerprint(second)


def test_input_fingerprint_normalizes_naive_published_as_utc():
    naive = _episode()
    naive.published = datetime(2026, 7, 24)
    aware = _episode()
    assert episode_input_fingerprint(naive) == episode_input_fingerprint(aware)


def test_input_fingerprint_marks_official_metadata_edits_dirty():
    first = _episode()
    second = _episode(title="Corrected title")
    assert dirty_uids(
        {"stable-uid": episode_input_fingerprint(first)},
        {"stable-uid": episode_input_fingerprint(second)},
    ) == {"stable-uid": "input_changed"}


def test_refresh_due_honors_ttl_but_forces_full_refresh():
    now = datetime(2026, 7, 24, tzinfo=UTC)
    recent = {
        "last_success": now.isoformat(),
        "next_poll_at": (now + timedelta(hours=4)).isoformat(),
    }
    assert not refresh_due(recent, ttl_hours=6, full_refresh_days=7, now=now + timedelta(hours=1))
    old = {
        "last_success": (now - timedelta(days=8)).isoformat(),
        "next_poll_at": recent["next_poll_at"],
    }
    assert refresh_due(old, ttl_hours=24, full_refresh_days=7, now=now)
    validator_recent = {
        "last_success": now.isoformat(),
        "last_full_refresh": (now - timedelta(days=8)).isoformat(),
        "next_poll_at": (now + timedelta(hours=4)).isoformat(),
    }
    assert refresh_due(validator_recent, ttl_hours=24, full_refresh_days=7, now=now)


def test_next_poll_is_optional_for_compatibility_default():
    assert next_poll_at(ttl_hours=0) is None


def test_source_pipeline_skips_full_fetch_for_unchanged_validator(tmp_path):
    class _Provider:
        name = "refresh-test"
        capabilities = frozenset()

        def __init__(self):
            self.probes = 0
            self.fetches = 0

        def validate(self, source):
            pass

        def detect_change(self, source):
            self.probes += 1
            return ChangeToken(etag='"same"')

        def fetch_episodes(self, source):
            self.fetches += 1
            episode = _episode()
            episode.description = "Official description"
            episode.audio_url = "https://cdn.example/audio.mp3"
            return [episode]

        def resolve_media_url(self, episode, source):
            return episode.video_url

        def video_deeplink(self, ref, t_seconds):
            return None

    provider = _Provider()
    register(provider)
    try:
        city = City(
            slug="refresh-city",
            provider=provider.name,
            source={"feed_url": "https://example.test/feed"},
            podcast_title="Refresh",
            podcast_author="Test",
            podcast_email="",
            podcast_description="",
        )
        state_dir = tmp_path / "state"
        state = {}
        pipeline = SourcePipeline(
            state_dir=state_dir,
            stages=[],
            ctx=StageContext(storage=None, ffmpeg=None, max_kbps=96, dry_run=False, lane=None),
            full_artifact_episodes=2000,
            metadata_retention_episodes=10000,
            refresh_state=state,
        )
        key = source_key(city)
        _, episodes, _, _ = pipeline.fetch_merge(city, key)
        save_records(state_dir, key, {ep.uid: episode_to_record(ep) for ep in episodes})
        _, unchanged, _, _ = pipeline.fetch_merge(city, key)

        assert provider.probes == 2
        assert provider.fetches == 1
        assert [ep.uid for ep in unchanged] == [episodes[0].uid]
        assert pipeline.dirty_uids(key) == frozenset()
        assert state[key]["dirty_episode_count"] == 0
    finally:
        _REGISTRY.pop(provider.name, None)
