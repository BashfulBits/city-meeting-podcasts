"""Tests for the durable media-availability state machine (H16 PR3, GH#353)."""

from __future__ import annotations

from datetime import UTC, datetime

from citypods.availability import (
    AVAILABLE,
    CONFIRMED_EMPTY,
    DETECTOR_VERSION,
    INVALID,
    MISSING,
    RECOVERED,
    SUSPECTED_EMPTY,
    MediaAvailability,
    Observation,
    classify,
    source_fingerprint,
    with_operator_override,
)
from citypods.models import Episode


def _ep(url="https://city.swagit.com/play/123/media.mp4", duration=3600, media_kind="hls"):
    return Episode(
        guid="g1",
        title="Council",
        published=datetime(2026, 5, 19, 16, 0, tzinfo=UTC),
        video_url=url,
        duration=duration,
        media_kind=media_kind,
    )


def _silent(fp="fp", profile="p1"):
    return Observation(kind="silent", fingerprint=fp, profile=profile)


def _playable(fp="fp", profile="p1"):
    return Observation(kind="playable", fingerprint=fp, profile=profile)


# --- fingerprint ---------------------------------------------------------------------------------


def test_fingerprint_stable_across_signed_query_rotation():
    a = _ep(url="https://city.swagit.com/play/1/media.mp4?X-Amz-Signature=AAA&token=1")
    b = _ep(url="https://city.swagit.com/play/1/media.mp4?X-Amz-Signature=BBB&token=2")
    assert source_fingerprint(a, "p1") == source_fingerprint(b, "p1")


def test_fingerprint_changes_when_media_path_or_duration_changes():
    base = _ep(url="https://city.swagit.com/play/1/media.mp4", duration=3600)
    other_path = _ep(url="https://city.swagit.com/play/2/media.mp4", duration=3600)
    other_dur = _ep(url="https://city.swagit.com/play/1/media.mp4", duration=1800)
    assert source_fingerprint(base, "p1") != source_fingerprint(other_path, "p1")
    assert source_fingerprint(base, "p1") != source_fingerprint(other_dur, "p1")


def test_fingerprint_changes_when_profile_changes():
    ep = _ep()
    assert source_fingerprint(ep, "p1") != source_fingerprint(ep, "p2")


# --- two-fetch confirmation ----------------------------------------------------------------------


def test_first_silent_fetch_is_only_suspected():
    out = classify(None, _silent())
    assert out is not None
    assert out.state == SUSPECTED_EMPTY
    assert out.silent_confirmations == 1
    assert out.is_withheld()
    assert out.detector_version == DETECTOR_VERSION


def test_second_independent_silent_fetch_confirms():
    first = classify(None, _silent())
    second = classify(first, _silent())
    assert second.state == CONFIRMED_EMPTY
    assert second.silent_confirmations == 2
    assert second.is_withheld()


# --- transport never confirms --------------------------------------------------------------------


def test_transport_failure_with_no_prior_yields_no_verdict():
    assert classify(None, Observation(kind="transport_failed")) is None


def test_transport_failure_never_advances_confirmation_or_withholds_good_episode():
    good = classify(None, _playable())
    assert good.state == AVAILABLE
    after = classify(good, Observation(kind="transport_failed"))
    # Known-good stays available; a 403/timeout is not evidence about content.
    assert after.state == AVAILABLE
    assert after.silent_confirmations == 0
    assert not after.is_withheld()


def test_transport_failure_does_not_confirm_a_suspected_verdict():
    suspected = classify(None, _silent())
    after = classify(suspected, Observation(kind="transport_failed"))
    assert after.state == SUSPECTED_EMPTY
    assert after.silent_confirmations == 1  # not advanced toward confirmation


# --- fingerprint-change reset --------------------------------------------------------------------


def test_new_source_bytes_retire_prior_confirmations():
    first = classify(None, _silent(fp="old"))
    second = classify(first, _silent(fp="old"))
    assert second.state == CONFIRMED_EMPTY
    # City swaps the media: a silent observation on new bytes starts over at suspected.
    fresh = classify(second, _silent(fp="new"))
    assert fresh.state == SUSPECTED_EMPTY
    assert fresh.silent_confirmations == 1


# --- recovery ------------------------------------------------------------------------------------


def test_playable_after_withheld_recovers():
    suspected = classify(None, _silent())
    assert suspected.is_withheld()
    recovered = classify(suspected, _playable())
    assert recovered.state == RECOVERED
    assert not recovered.is_withheld()
    assert recovered.recovered_at is not None


def test_playable_from_clean_state_is_available_not_recovered():
    out = classify(None, _playable())
    assert out.state == AVAILABLE
    assert out.recovered_at is None


# --- dead / missing ------------------------------------------------------------------------------


def test_dead_media_is_invalid_and_withheld():
    out = classify(None, Observation(kind="dead", fingerprint="fp"))
    assert out.state == INVALID
    assert out.is_withheld()


def test_absent_source_is_missing_and_withheld():
    out = classify(None, Observation(kind="missing", fingerprint="fp"))
    assert out.state == MISSING
    assert out.is_withheld()


# --- operator override ---------------------------------------------------------------------------


def test_operator_override_supersedes_auto_state_for_gating():
    confirmed = classify(classify(None, _silent()), _silent())
    assert confirmed.state == CONFIRMED_EMPTY
    cleared = with_operator_override(confirmed, AVAILABLE, "manually verified good")
    assert cleared.state == CONFIRMED_EMPTY  # auto-state preserved for the record
    assert cleared.effective_state() == AVAILABLE
    assert not cleared.is_withheld()


def test_operator_override_survives_a_later_detector_run():
    confirmed = classify(classify(None, _silent()), _silent())
    cleared = with_operator_override(confirmed, AVAILABLE, "verified")
    # A subsequent silent observation must not erase the operator's call.
    after = classify(cleared, _silent())
    assert after.operator_override == AVAILABLE
    assert not after.is_withheld()


def test_operator_can_force_withhold_an_unexamined_episode():
    out = with_operator_override(None, INVALID, "known bad source")
    assert out.effective_state() == INVALID
    assert out.is_withheld()


def test_invalid_override_state_rejected():
    import pytest

    with pytest.raises(ValueError):
        with_operator_override(None, "bogus", "x")


def test_dataclass_is_frozen():
    import dataclasses

    import pytest

    out = MediaAvailability(state=AVAILABLE)
    with pytest.raises(dataclasses.FrozenInstanceError):
        out.state = MISSING  # type: ignore[misc]


# --- end-to-end lifecycle ------------------------------------------------------------------------


def test_confirm_then_recover_lifecycle_through_records_and_feed():
    """The full PR3 loop: two silent fetches confirm-empty + withhold from both feeds (record
    round-tripping in between), then a playable fetch recovers and re-enables the enclosure."""
    from citypods.feeds import enclosure_url
    from citypods.records import episode_to_record, record_to_episode

    ep = _ep(media_kind="direct")  # direct so a video enclosure exists when not withheld
    fp = source_fingerprint(ep, "p1")

    # Run 1: silent → suspected (withheld), persists across a record round-trip.
    ep.media_availability = classify(None, _silent(fp=fp))
    ep = record_to_episode(episode_to_record(ep))
    assert ep.media_availability.state == SUSPECTED_EMPTY
    assert enclosure_url(ep, "audio") is None and enclosure_url(ep, "video") is None

    # Run 2: silent again on an independent fetch → confirmed, still withheld.
    ep.media_availability = classify(ep.media_availability, _silent(fp=fp))
    assert ep.media_availability.state == CONFIRMED_EMPTY
    assert enclosure_url(ep, "video") is None

    # Run 3: the city supplies real audio → recovered, enclosure re-enabled.
    ep.media_availability = classify(ep.media_availability, _playable(fp=fp))
    assert ep.media_availability.state == RECOVERED
    assert enclosure_url(ep, "video") == ep.video_url
