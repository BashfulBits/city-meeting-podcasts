"""Tests for the durable media-availability state machine (H16 PR3, GH#353)."""

from __future__ import annotations

from datetime import UTC, datetime

from citypods.availability import (
    AVAILABLE,
    CONFIRMED_EMPTY,
    CONFIRMED_PARTIAL,
    DETECTOR_VERSION,
    INVALID,
    MISSING,
    RECOVERED,
    SUSPECTED_EMPTY,
    SUSPECTED_PARTIAL,
    MediaAvailability,
    Observation,
    classify,
    content_seconds,
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


def _partial(fp="fp", profile="p1"):
    return Observation(kind="partial", fingerprint=fp, profile=profile)


# --- content_seconds -------------------------------------------------------------------------


def test_content_seconds_subtracts_non_overlapping_silence():
    assert content_seconds([(0, 10), (20, 30)], 100) == 80.0


def test_content_seconds_merges_overlapping_silence_spans():
    # CR2-CP-04: two overlapping silence spans must not each count their overlap separately.
    # [0,20] and [10,30] overlap on [10,20]; merged, silence is [0,30] = 30s, not 20+20=40s.
    assert content_seconds([(0, 20), (10, 30)], 100) == 70.0


def test_content_seconds_merges_touching_silence_spans():
    assert content_seconds([(0, 10), (10, 20)], 100) == 80.0


def test_content_seconds_handles_unsorted_input():
    assert content_seconds([(20, 30), (0, 10)], 100) == 80.0


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


def test_clearing_override_with_no_prior_verdict_raises_instead_of_fabricating_available():
    # CR2-CP-02: with_operator_override(None, None, ...) used to manufacture a fabricated
    # AVAILABLE verdict instead of treating a no-op clear-with-nothing-to-clear as invalid.
    import pytest

    with pytest.raises(ValueError, match="cannot clear"):
        with_operator_override(None, None)


def test_operator_override_reset_on_source_fingerprint_change():
    # CR2-CP-03: classify() resets `base` (and so must reset operator_override/operator_reason
    # too) when the source fingerprint changes -- a stale override on old media bytes must not
    # silently carry forward onto a swapped recording.
    confirmed = classify(classify(None, _silent(fp="fp1")), _silent(fp="fp1"))
    overridden = with_operator_override(confirmed, AVAILABLE, "verified good")
    assert overridden.operator_override == AVAILABLE

    # New source bytes (different fingerprint) arrive as a fresh silent observation.
    after_swap = classify(overridden, _silent(fp="fp2"))
    assert after_swap.operator_override is None
    assert after_swap.operator_reason == ""


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


def test_confirm_partial_lifecycle_through_records_and_feed():
    """GH#851's analogous loop: two short-source fetches confirm-partial + publish with a
    disclaimer (record round-tripping in between, enclosure kept unlike withheld), then a
    full-length fetch recovers and clears the disclaimer."""
    from citypods.feeds import enclosure_url, episode_notes_html
    from citypods.records import episode_to_record, record_to_episode

    ep = _ep(media_kind="direct")
    fp = source_fingerprint(ep, "p1")

    # Run 1: short → suspected (NOT withheld), persists across a record round-trip.
    ep.media_availability = classify(None, _partial(fp=fp))
    ep = record_to_episode(episode_to_record(ep))
    assert ep.media_availability.state == SUSPECTED_PARTIAL
    assert ep.media_availability.partial_confirmations == 1
    assert enclosure_url(ep, "video") == ep.video_url  # still published
    assert "incomplete" not in episode_notes_html(ep)  # not yet confirmed — no disclaimer

    # Run 2: short again on an independent fetch → confirmed, still published, now badged.
    ep.media_availability = classify(ep.media_availability, _partial(fp=fp))
    ep = record_to_episode(episode_to_record(ep))
    assert ep.media_availability.state == CONFIRMED_PARTIAL
    assert ep.media_availability.partial_confirmations == 2
    assert enclosure_url(ep, "video") == ep.video_url
    assert "incomplete" in episode_notes_html(ep)

    # Run 3: the city posts the complete recording → recovers, disclaimer clears.
    ep.media_availability = classify(ep.media_availability, _playable(fp=fp))
    assert ep.media_availability.state == AVAILABLE
    assert "incomplete" not in episode_notes_html(ep)


def test_is_confirmed_dead_covers_durable_states_not_suspected():
    from citypods.availability import INVALID

    assert MediaAvailability(state=CONFIRMED_EMPTY).is_confirmed_dead()
    assert MediaAvailability(state=MISSING).is_confirmed_dead()
    assert MediaAvailability(state=INVALID).is_confirmed_dead()
    # suspected_empty is withheld but not yet confirmed dead → stays on the exponential ramp so it
    # can reach its second silent confirmation quickly (GH#795).
    suspected = MediaAvailability(state=SUSPECTED_EMPTY)
    assert suspected.is_withheld() and not suspected.is_confirmed_dead()
    assert not MediaAvailability(state=AVAILABLE).is_confirmed_dead()
    # An operator override drives this gate like every other one.
    overridden = MediaAvailability(state=CONFIRMED_EMPTY, operator_override=AVAILABLE)
    assert not overridden.is_confirmed_dead()


# --- GH#851: partial/short-source lifecycle -------------------------------------------------------


def test_first_partial_fetch_is_only_suspected_and_not_withheld():
    out = classify(None, _partial())
    assert out is not None
    assert out.state == SUSPECTED_PARTIAL
    assert out.partial_confirmations == 1
    assert out.silent_confirmations == 0
    assert out.is_suspected_partial()
    assert not out.is_confirmed_partial()
    # Real, playable content — must never be excluded from the feed like empty/dead media.
    assert not out.is_withheld()


def test_second_independent_partial_fetch_confirms():
    first = classify(None, _partial())
    second = classify(first, _partial())
    assert second.state == CONFIRMED_PARTIAL
    assert second.partial_confirmations == 2
    assert second.is_confirmed_partial()
    assert not second.is_withheld()


def test_partial_and_silent_confirmations_do_not_conflate():
    # A silent observation must not advance (or be advanced by) partial confirmations, and
    # vice versa — they are evidence for different verdicts and share no counter.
    silent_then_partial = classify(classify(None, _silent()), _partial())
    assert silent_then_partial.state == SUSPECTED_PARTIAL
    assert silent_then_partial.partial_confirmations == 1
    assert silent_then_partial.silent_confirmations == 0

    partial_then_silent = classify(classify(None, _partial()), _silent())
    assert partial_then_silent.state == SUSPECTED_EMPTY
    assert partial_then_silent.silent_confirmations == 1
    assert partial_then_silent.partial_confirmations == 0


def test_playable_after_suspected_partial_recovers_without_confirming():
    # A transient short fetch that then comes back full-length must not accumulate toward
    # confirmation — this is the "transient recovers" acceptance criterion (GH#851).
    suspected = classify(None, _partial())
    recovered = classify(suspected, _playable())
    assert recovered.state == AVAILABLE  # not RECOVERED: suspected_partial was never withheld
    assert recovered.partial_confirmations == 0


def test_transport_failure_never_advances_partial_confirmation():
    suspected = classify(None, _partial())
    after = classify(suspected, Observation(kind="transport_failed"))
    assert after.state == SUSPECTED_PARTIAL  # unchanged
    assert after.partial_confirmations == 1  # not advanced


def test_new_source_bytes_retire_prior_partial_confirmations():
    ep_fp_a = classify(None, _partial(fp="a"))
    retired = classify(ep_fp_a, _partial(fp="b"))
    assert retired.state == SUSPECTED_PARTIAL  # restarted, not confirmed
    assert retired.partial_confirmations == 1


def test_is_confirmed_partial_respects_operator_override():
    confirmed = MediaAvailability(state=CONFIRMED_PARTIAL)
    assert confirmed.is_confirmed_partial()
    overridden = MediaAvailability(state=CONFIRMED_PARTIAL, operator_override=AVAILABLE)
    assert not overridden.is_confirmed_partial()


def test_partial_states_are_valid_operator_overrides():
    from citypods.availability import OVERRIDE_STATES

    assert SUSPECTED_PARTIAL in OVERRIDE_STATES
    assert CONFIRMED_PARTIAL in OVERRIDE_STATES


def test_partial_states_excluded_from_withheld_and_confirmed_dead():
    from citypods.availability import CONFIRMED_DEAD_STATES, WITHHELD_STATES

    assert SUSPECTED_PARTIAL not in WITHHELD_STATES
    assert CONFIRMED_PARTIAL not in WITHHELD_STATES
    assert CONFIRMED_PARTIAL not in CONFIRMED_DEAD_STATES


def test_confirmed_partial_enclosure_stays_published():
    """Unlike withheld media, a confirmed-partial episode keeps its enclosure (GH#851: publish
    with a disclaimer, don't exclude)."""
    from citypods.feeds import enclosure_url

    ep = _ep(media_kind="direct")
    ep.hosted_audio_url = "https://cdn.example.com/hosted.m4a"
    ep.media_availability = MediaAvailability(state=CONFIRMED_PARTIAL)
    assert enclosure_url(ep, "audio") == ep.hosted_audio_url
