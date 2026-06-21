from __future__ import annotations

from datetime import UTC, datetime

from citypods.h16_identity import H16IdentityTracker
from citypods.models import City, Episode
from citypods.records import audio_object_key, audio_spec_hash, source_key
from citypods.storage.local import LocalStorage


def _city() -> City:
    return City(
        slug="example-tx",
        provider="granicus",
        source={"feed_url": "https://example.granicus.com/rss"},
        podcast_title="Example",
        podcast_author="Example",
        podcast_email="podcast@example.com",
        podcast_description="Meetings",
    )


def _episode() -> Episode:
    return Episode(
        guid="clip-123",
        uid="stable-uid",
        title="Council",
        published=datetime(2026, 6, 21, tzinfo=UTC),
        video_url="https://archive-video.granicus.com/example/example_clip-123.mp4",
        duration=3600,
        links={"canonical_video": "https://example.granicus.com/MediaPlayer.php?clip_id=123"},
    )


def _tracker(storage: LocalStorage) -> H16IdentityTracker:
    return H16IdentityTracker(
        storage=storage,
        max_kbps=96,
        loudness_profile="",
        processing_profile="",
        enabled=True,
    )


def _expected(city: City, ep: Episode, storage: LocalStorage) -> tuple[str, str, str]:
    spec = audio_spec_hash(ep, max_kbps=96)
    key = audio_object_key(city, ep, spec)
    return spec, key, storage.public_url(key)


def test_current_artifact_and_record_identity_pass(tmp_path):
    city = _city()
    ep = _episode()
    storage = LocalStorage(root=tmp_path / "bucket", url_prefix="https://cdn.example")
    spec, key, url = _expected(city, ep, storage)
    ep.audio_spec_hash = spec
    ep.audio_key = key
    ep.hosted_audio_url = url
    ep.audio_duration_served = 3599.5
    tracker = _tracker(storage)

    tracker.capture(city, [ep])
    tracker.verify(source_key(city), [ep])

    assert tracker.summary() == {
        "checked": 1,
        "mismatches": 0,
        "artifact_checked": 1,
        "mismatch_categories": {},
    }


def test_new_artifact_must_use_deterministic_identity(tmp_path):
    city = _city()
    ep = _episode()
    storage = LocalStorage(root=tmp_path / "bucket", url_prefix="https://cdn.example")
    tracker = _tracker(storage)
    tracker.capture(city, [ep])

    spec, key, url = _expected(city, ep, storage)
    ep.audio_spec_hash = spec
    ep.audio_key = key
    ep.hosted_audio_url = url
    ep.audio_duration_served = 3600.0
    tracker.verify(source_key(city), [ep])

    assert tracker.summary()["mismatches"] == 0
    assert tracker.summary()["artifact_checked"] == 1


def test_deferred_recipe_change_does_not_claim_artifact_validation(tmp_path):
    city = _city()
    ep = _episode()
    storage = LocalStorage(root=tmp_path / "bucket", url_prefix="https://cdn.example")
    spec, key, url = _expected(city, ep, storage)
    ep.audio_spec_hash = spec
    ep.audio_key = key
    ep.hosted_audio_url = url
    ep.audio_duration_served = 3600.0
    tracker = _tracker(storage)
    tracker.capture(city, [ep])

    # A pre-audio stage changed the byte recipe, but the encode was deferred by the run budget.
    ep.chapters = [{"start": 0, "title": "Call to order"}]
    tracker.verify(source_key(city), [ep])

    assert tracker.summary()["mismatches"] == 0
    assert tracker.summary()["artifact_checked"] == 0


def test_identity_drift_reports_bounded_categories(tmp_path):
    city = _city()
    ep = _episode()
    storage = LocalStorage(root=tmp_path / "bucket", url_prefix="https://cdn.example")
    tracker = _tracker(storage)
    tracker.capture(city, [ep])

    ep.uid = "changed"
    ep.guid = "changed-guid"
    ep.video_url = "https://archive-video.granicus.com/example/changed.mp4"
    ep.duration = 12
    ep.audio_spec_hash = "wrong"
    ep.audio_key = "wrong.m4a"
    ep.hosted_audio_url = "https://cdn.example/wrong.m4a"
    tracker.verify(source_key(city), [ep])

    summary = tracker.summary()
    assert summary["checked"] == 1
    assert summary["mismatches"] == 1
    assert set(summary["mismatch_categories"]) >= {
        "uid",
        "provider_guid",
        "official_url",
        "source_url",
        "source_duration",
        "audio_spec_hash",
        "audio_key",
        "audio_url",
        "served_duration",
    }


def test_non_granicus_records_are_not_reported(tmp_path):
    city = _city()
    city.provider = "swagit"
    storage = LocalStorage(root=tmp_path / "bucket", url_prefix="https://cdn.example")
    tracker = _tracker(storage)

    tracker.capture(city, [_episode()])
    tracker.verify(source_key(city), [_episode()])

    assert tracker.summary()["checked"] == 0
