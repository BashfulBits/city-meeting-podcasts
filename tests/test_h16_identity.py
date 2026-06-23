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


def test_coalesced_sibling_source_artifact_is_valid_identity(tmp_path):
    city = _city()
    ep = _episode()
    storage = LocalStorage(root=tmp_path / "bucket", url_prefix="https://cdn.example")
    tracker = _tracker(storage)
    tracker.capture(city, [ep])

    spec = audio_spec_hash(ep, max_kbps=96)
    key = f"granicus/deadbeefcafe/{ep.uid}-{spec}.m4a"
    ep.audio_spec_hash = spec
    ep.audio_key = key
    ep.hosted_audio_url = storage.public_url(key)
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


def test_legacy_artifact_reuse_is_not_a_mismatch(tmp_path):
    # GH#353 / Audio run 54: a migrated legacy artifact is reused as-is by materialize_audio
    # (its ``legacy_ok`` path) — the record keeps ``audio_spec_hash == "legacy"`` and its
    # legacy key/url, which deliberately do NOT match the content-addressed recompute. The run
    # that first backfills the served duration trips the artifact-changed branch; that must not
    # be reported as an identity mismatch.
    city = _city()
    ep = _episode()
    storage = LocalStorage(root=tmp_path / "bucket", url_prefix="https://cdn.example")
    ep.audio_spec_hash = "legacy"
    ep.audio_key = "granicus/example-tx/stable-uid.m4a"
    ep.hosted_audio_url = "https://cdn.example/granicus/example-tx/stable-uid.m4a"
    ep.audio_duration_served = None  # not yet backfilled at capture
    tracker = _tracker(storage)
    tracker.capture(city, [ep])

    # The reuse pass backfills the served duration this run (one-shot); key/spec/url stay legacy.
    ep.audio_duration_served = 3599.5
    tracker.verify(source_key(city), [ep])

    assert tracker.summary() == {
        "checked": 1,
        "mismatches": 0,
        "artifact_checked": 1,
        "mismatch_categories": {},
    }


def test_upload_failure_retaining_prior_artifact_is_not_a_mismatch(tmp_path):
    # GH#353 / Audio runs 54 & 56 (the real root cause): an episode's recipe changed this run and
    # its re-encode probed a new served duration, then the upload failed transiently (B2
    # ServiceUnavailable). materialize_audio left the record pointing at the prior, valid artifact
    # (old spec) while the new probed duration had already been written. The duration change trips
    # the artifact-changed branch, but key/spec/url were not rewritten this run, so the divergence
    # from the freshly-recomputed expected spec is a pending re-encode, not corruption — and must
    # not be reported as a mismatch.
    city = _city()
    ep = _episode()
    storage = LocalStorage(root=tmp_path / "bucket", url_prefix="https://cdn.example")
    # Prior run's artifact, current at capture (recipe == artifact spec).
    spec, key, url = _expected(city, ep, storage)
    ep.audio_spec_hash = spec
    ep.audio_key = key
    ep.hosted_audio_url = url
    ep.audio_duration_served = 3600.0
    tracker = _tracker(storage)
    tracker.capture(city, [ep])

    # This run: a pre-audio stage changed the recipe (so expected spec now differs), the re-encode
    # probed a new served duration, but the upload failed so the artifact pointer stayed put.
    ep.chapters = [{"start": 0, "title": "Call to order"}]
    ep.audio_duration_served = 3599.2
    tracker.verify(source_key(city), [ep])

    # Sanity: the recipe really did change, so a naive content-addressed recompute would diverge.
    assert audio_spec_hash(ep, max_kbps=96) != spec
    assert tracker.summary()["mismatches"] == 0


def test_legacy_marker_with_changed_key_is_still_a_mismatch(tmp_path):
    # The exemption covers only artifacts UNCHANGED across the media chain. A record whose key/url
    # actually changed this run WAS (re)materialized, so its content-addressed identity is checked:
    # a ``legacy`` spec paired with a changed key/url is reported, not silently exempted.
    city = _city()
    ep = _episode()
    storage = LocalStorage(root=tmp_path / "bucket", url_prefix="https://cdn.example")
    ep.audio_spec_hash = "legacy"
    ep.audio_key = "granicus/example-tx/stable-uid.m4a"
    ep.hosted_audio_url = "https://cdn.example/granicus/example-tx/stable-uid.m4a"
    ep.audio_duration_served = 10.0
    tracker = _tracker(storage)
    tracker.capture(city, [ep])

    # Key/url drift away from the captured legacy values while the spec marker stays "legacy".
    ep.audio_key = "granicus/example-tx/stable-uid-deadbeef.m4a"
    ep.hosted_audio_url = "https://cdn.example/granicus/example-tx/stable-uid-deadbeef.m4a"
    tracker.verify(source_key(city), [ep])

    summary = tracker.summary()
    assert summary["mismatches"] == 1
    assert {"audio_key", "audio_url"} <= set(summary["mismatch_categories"])


def test_non_granicus_records_are_not_reported(tmp_path):
    city = _city()
    city.provider = "swagit"
    storage = LocalStorage(root=tmp_path / "bucket", url_prefix="https://cdn.example")
    tracker = _tracker(storage)

    tracker.capture(city, [_episode()])
    tracker.verify(source_key(city), [_episode()])

    assert tracker.summary()["checked"] == 0
