"""Tests for citypods/silence.py: silence detection + SilencePlanner (#111)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from citypods.http import StopRequested
from citypods.silence import (
    SilencePlanner,
    _parse_ffmpeg_duration,
    build_silence_timeline,
    detect_silences,
    is_degenerate_served_duration,
    parse_silences,
)
from citypods.timeline import Segment, Timeline, timeline_digest

# ---------------------------------------------------------------------------
# parse_silences
# ---------------------------------------------------------------------------


class TestParseSilences:
    def test_empty_stderr(self):
        assert parse_silences("") == []

    def test_single_silence(self):
        stderr = (
            "[silencedetect @ 0x1] silence_start: 0\n"
            "[silencedetect @ 0x1] silence_end: 2.5 | silence_duration: 2.5\n"
        )
        assert parse_silences(stderr) == [(0.0, 2.5)]

    def test_multiple_silences(self):
        stderr = (
            "[silencedetect @ 0x1] silence_start: 0\n"
            "[silencedetect @ 0x1] silence_end: 3.0 | silence_duration: 3.0\n"
            "[silencedetect @ 0x1] silence_start: 600.5\n"
            "[silencedetect @ 0x1] silence_end: 900.0 | silence_duration: 299.5\n"
        )
        assert parse_silences(stderr) == [(0.0, 3.0), (600.5, 900.0)]

    def test_ignores_unmatched_end(self):
        # silence_end with no preceding silence_start is ignored
        stderr = "[silencedetect @ 0x1] silence_end: 2.5 | silence_duration: 2.5\n"
        assert parse_silences(stderr) == []

    def test_trailing_start_without_end(self):
        # silence_start with no matching end (still recording) is dropped
        stderr = (
            "[silencedetect @ 0x1] silence_start: 0\n"
            "[silencedetect @ 0x1] silence_end: 2.0 | silence_duration: 2.0\n"
            "[silencedetect @ 0x1] silence_start: 7200.0\n"
        )
        assert parse_silences(stderr) == [(0.0, 2.0)]


# ---------------------------------------------------------------------------
# _parse_ffmpeg_duration
# ---------------------------------------------------------------------------


class TestParseFfmpegDuration:
    def test_standard_format(self):
        stderr = "  Duration: 02:15:30.45, start: 0.000, bitrate: 128 kb/s"
        assert _parse_ffmpeg_duration(stderr) == pytest.approx(2 * 3600 + 15 * 60 + 30.45)

    def test_no_duration(self):
        assert _parse_ffmpeg_duration("no duration here") is None

    def test_zero_duration(self):
        stderr = "Duration: 00:00:00.00"
        assert _parse_ffmpeg_duration(stderr) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# build_silence_timeline
# ---------------------------------------------------------------------------


class TestBuildSilenceTimeline:
    def test_no_silences_returns_none(self):
        assert build_silence_timeline("s0", 3600.0, []) is None

    def test_leading_silence_below_threshold_not_cut(self):
        # 0.5s leading silence, threshold is 1.0
        result = build_silence_timeline("s0", 3600.0, [(0.0, 0.5)], lead_trail_min=1.0)
        assert result is None

    def test_leading_silence_removed(self):
        # 3s leading silence at start, threshold 1.0
        result = build_silence_timeline("s0", 3600.0, [(0.0, 3.0)], lead_trail_min=1.0)
        assert result is not None
        assert len(result.segments) == 1
        seg = result.segments[0]
        assert seg.source_start == pytest.approx(3.0)
        assert seg.source_end == pytest.approx(3600.0)
        assert seg.served_start == pytest.approx(0.0)
        assert seg.served_end == pytest.approx(3597.0)

    def test_trailing_silence_removed(self):
        result = build_silence_timeline("s0", 3600.0, [(3598.0, 3600.0)], lead_trail_min=1.0)
        assert result is not None
        seg = result.segments[0]
        assert seg.source_start == pytest.approx(0.0)
        assert seg.source_end == pytest.approx(3598.0)

    def test_mid_silence_below_threshold_not_cut(self):
        # 5s mid-meeting gap, threshold 10s
        result = build_silence_timeline("s0", 3600.0, [(1800.0, 1805.0)], mid_min=10.0)
        assert result is None

    def test_mid_silence_removed(self):
        # 300s executive session in the middle
        result = build_silence_timeline(
            "s0", 3600.0, [(1800.0, 2100.0)], lead_trail_min=1.0, mid_min=10.0
        )
        assert result is not None
        assert len(result.segments) == 2
        seg0, seg1 = result.segments
        assert seg0.source_start == pytest.approx(0.0)
        assert seg0.source_end == pytest.approx(1800.0)
        assert seg0.served_start == pytest.approx(0.0)
        assert seg0.served_end == pytest.approx(1800.0)
        assert seg1.source_start == pytest.approx(2100.0)
        assert seg1.source_end == pytest.approx(3600.0)
        assert seg1.served_start == pytest.approx(1800.0)
        assert seg1.served_end == pytest.approx(3300.0)

    def test_multiple_cuts_served_time_packed(self):
        # leading 2s + mid 60s: served time should be left-packed with no gaps
        silences = [(0.0, 2.0), (100.0, 160.0)]
        result = build_silence_timeline("s0", 200.0, silences, lead_trail_min=1.0, mid_min=10.0)
        assert result is not None
        assert len(result.segments) == 2
        seg0, seg1 = result.segments
        assert seg0.served_start == pytest.approx(0.0)
        assert seg0.served_end == pytest.approx(98.0)  # 100 - 2 = 98s of audio
        assert seg1.served_start == pytest.approx(98.0)
        assert seg1.served_end == pytest.approx(138.0)  # 200 - 160 = 40s of audio

    def test_source_id_set_on_all_segments(self):
        result = build_silence_timeline(
            "my-source",
            3600.0,
            [(0.0, 5.0), (1800.0, 2100.0)],
            lead_trail_min=1.0,
            mid_min=10.0,
        )
        assert result is not None
        for seg in result.segments:
            assert seg.source_id == "my-source"

    def test_identity_timeline_digest_is_empty(self):
        # A Timeline where kept spans cover the whole source is identity → digest ""
        # (only a single full-pass segment with no offset qualifies as identity)
        result = build_silence_timeline("s0", 3600.0, [], lead_trail_min=1.0)
        assert result is None  # nothing to cut → caller stamps identity

    def test_non_identity_digest_non_empty(self):
        result = build_silence_timeline("s0", 3600.0, [(0.0, 5.0)], lead_trail_min=1.0)
        assert result is not None
        assert timeline_digest(result) != ""


# ---------------------------------------------------------------------------
# detect_silences (mocked subprocess)
# ---------------------------------------------------------------------------


class TestDetectSilences:
    def test_parses_silences_and_duration(self):
        fake_stderr = (
            "  Duration: 01:00:00.00, start: 0.0, bitrate: 128 kb/s\n"
            "[silencedetect @ 0x1] silence_start: 0\n"
            "[silencedetect @ 0x1] silence_end: 2.0 | silence_duration: 2.0\n"
        )
        with patch("citypods.silence.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stderr=fake_stderr, returncode=0)
            silences, duration = detect_silences("http://example.com/video.mp4")
        assert silences == [(0.0, 2.0)]
        assert duration == pytest.approx(3600.0)

    def test_returns_empty_on_subprocess_error(self):
        with patch("citypods.silence.subprocess.run") as mock_run:
            mock_run.side_effect = OSError("ffmpeg not found")
            silences, duration = detect_silences("http://example.com/video.mp4")
        assert silences == []
        assert duration is None

    def test_ffmpeg_command_includes_silencedetect(self):
        with patch("citypods.silence.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stderr="", returncode=0)
            detect_silences("http://x.com/v.mp4", noise_db=-50.0, min_duration_s=2.0)
        cmd = mock_run.call_args[0][0]
        assert "silencedetect=noise=-50.0dB:d=2.0" in " ".join(cmd)
        assert "-vn" in cmd

    def test_threads_arg_pins_ffmpeg_thread_count(self):
        """Mirrors CommandFfmpeg's -threads pinning so a silencedetect pass doesn't default to
        'all cores' alongside NativeWorkGate-budgeted encodes."""
        with patch("citypods.silence.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stderr="", returncode=0)
            detect_silences("http://x.com/v.mp4", threads=2)
        cmd = mock_run.call_args[0][0]
        assert cmd[cmd.index("-threads") + 1] == "2"

    def test_no_threads_arg_when_threads_is_none(self):
        with patch("citypods.silence.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stderr="", returncode=0)
            detect_silences("http://x.com/v.mp4")
        cmd = mock_run.call_args[0][0]
        assert "-threads" not in cmd


# ---------------------------------------------------------------------------
# SilencePlanner
# ---------------------------------------------------------------------------


def _make_ctx(
    trim_silence=True,
    noise_db=-40.0,
    lead_trail=1.0,
    mid=10.0,
    min_served_seconds=5.0,
    min_served_fraction=0.02,
):
    ctx = MagicMock()
    ctx.trim_silence = trim_silence
    ctx.silence_noise_db = noise_db
    ctx.silence_lead_trail_min_s = lead_trail
    ctx.silence_mid_min_s = mid
    ctx.silence_min_served_seconds = min_served_seconds
    ctx.silence_min_served_fraction = min_served_fraction
    ctx.ffmpeg.binary = "ffmpeg"
    ctx.ffmpeg.timeout_seconds = None
    return ctx


def _make_city(provider="swagit", extract_audio=False, extra=None):
    city = MagicMock()
    city.provider = provider
    city.extract_audio = extract_audio
    city.extra = extra or {}
    city.source = {"list_url": "http://example.com/views/default", "body": "City Council"}
    return city


def _make_episode(media_kind="hls", duration=3600, sources=None, video_url="http://x.com/v/1"):
    ep = MagicMock()
    ep.media_kind = media_kind
    ep.duration = duration
    ep.sources = sources or []
    ep.video_url = video_url
    ep.links = {}
    ep.audio_key = None
    ep.hosted_audio_url = None
    # H16 PR3: the planner now reads these to fingerprint and stamp the availability verdict.
    ep.audio_url = None
    ep.resolved_audio_url.return_value = video_url
    ep.media_availability = None
    return ep


class TestSilencePlanner:
    def test_returns_none_when_disabled(self):
        planner = SilencePlanner()
        ctx = _make_ctx(trim_silence=False)
        result = planner.plan(MagicMock(), _make_city(), _make_episode(), ctx, None)
        assert result is None

    def test_returns_none_for_direct_not_extract_audio(self):
        planner = SilencePlanner()
        ctx = _make_ctx()
        ep = _make_episode(media_kind="direct")
        result = planner.plan(MagicMock(), _make_city(extract_audio=False), ep, ctx, None)
        assert result is None

    def test_returns_none_for_multi_source_concat_episode(self):
        """SwagitConcatPlanner owns multi-source episodes; SilencePlanner must skip them."""
        from unittest.mock import MagicMock as MM

        planner = SilencePlanner()
        ctx = _make_ctx()
        src = MM()
        ep = _make_episode(sources=[src, src])  # len > 1
        result = planner.plan(MagicMock(), _make_city(), ep, ctx, None)
        assert result is None

    def test_runs_for_direct_with_extract_audio(self):
        planner = SilencePlanner()
        ctx = _make_ctx()
        ep = _make_episode(media_kind="direct", duration=3600)
        provider = MagicMock()
        provider.resolve_media_url.return_value = "http://x.com/video.mp4"
        with (
            patch("citypods.silence.shutil.which", return_value="ffmpeg"),
            patch("citypods.silence.detect_silences") as mock_detect,
        ):
            mock_detect.return_value = ([], 3600.0)
            result = planner.plan(provider, _make_city(extract_audio=True), ep, ctx, None)
        # No silence → identity timeline
        assert result is not None
        assert timeline_digest(result) == ""

    def test_per_feed_opt_out_via_extra(self):
        planner = SilencePlanner()
        ctx = _make_ctx(trim_silence=True)
        city = _make_city(extra={"trim_silence": False})
        result = planner.plan(MagicMock(), city, _make_episode(), ctx, None)
        assert result is None

    def test_returns_identity_when_no_silence_to_cut(self):
        planner = SilencePlanner()
        ctx = _make_ctx()
        provider = MagicMock()
        provider.resolve_media_url.return_value = "http://x.com/video.mp4"
        with (
            patch("citypods.silence.shutil.which", return_value="ffmpeg"),
            patch("citypods.silence.detect_silences") as mock_detect,
        ):
            mock_detect.return_value = ([], 3600.0)
            result = planner.plan(provider, _make_city(), _make_episode(duration=3600), ctx, None)
        assert result is not None
        assert timeline_digest(result) == ""  # identity → no re-encode

    def test_returns_trimmed_timeline_when_silence_found(self):
        planner = SilencePlanner()
        ctx = _make_ctx()
        provider = MagicMock()
        provider.resolve_media_url.return_value = "http://x.com/video.mp4"
        with (
            patch("citypods.silence.shutil.which", return_value="ffmpeg"),
            patch("citypods.silence.detect_silences") as mock_detect,
        ):
            mock_detect.return_value = ([(0.0, 5.0)], 3600.0)
            result = planner.plan(provider, _make_city(), _make_episode(duration=3600), ctx, None)
        assert result is not None
        assert timeline_digest(result) != ""  # non-identity → will re-encode
        assert len(result.segments) == 1
        assert result.segments[0].source_start == pytest.approx(5.0)

    def test_detect_silences_called_with_native_work_gate_threads(self):
        """The detect_silences call is pinned to CommandFfmpeg's configured thread count, the same
        budget AudioStage's encodes respect, so a planner pass can't oversubscribe the CPU."""
        planner = SilencePlanner()
        ctx = _make_ctx()
        ctx.ffmpeg.threads = 2
        provider = MagicMock()
        provider.resolve_media_url.return_value = "http://x.com/video.mp4"
        with (
            patch("citypods.silence.shutil.which", return_value="ffmpeg"),
            patch("citypods.silence.detect_silences") as mock_detect,
        ):
            mock_detect.return_value = ([], 3600.0)
            planner.plan(provider, _make_city(), _make_episode(duration=3600), ctx, None)
        assert mock_detect.call_args.kwargs["threads"] == 2

    def test_defers_when_native_work_gate_denies_admission(self):
        """A NativeWorkGate that denies admission (budget/queue pressure) must defer the planner
        pass — not run silencedetect unbounded and not raise."""
        planner = SilencePlanner()
        ctx = _make_ctx()
        ctx.native_work_gate.acquire.return_value = False
        provider = MagicMock()
        provider.resolve_media_url.return_value = "http://x.com/video.mp4"
        with (
            patch("citypods.silence.shutil.which", return_value="ffmpeg"),
            patch("citypods.silence.detect_silences") as mock_detect,
        ):
            result = planner.plan(provider, _make_city(), _make_episode(duration=3600), ctx, None)
        assert result is None
        mock_detect.assert_not_called()
        ctx.native_work_gate.release.assert_not_called()

    def test_releases_native_work_gate_after_detect_silences(self):
        planner = SilencePlanner()
        ctx = _make_ctx()
        ctx.native_work_gate.acquire.return_value = True
        provider = MagicMock()
        provider.resolve_media_url.return_value = "http://x.com/video.mp4"
        with (
            patch("citypods.silence.shutil.which", return_value="ffmpeg"),
            patch("citypods.silence.detect_silences", return_value=([], 3600.0)),
        ):
            planner.plan(provider, _make_city(), _make_episode(duration=3600), ctx, None)
        ctx.native_work_gate.release.assert_called_once_with(kind="audio")

    def test_defers_without_recording_failure_when_source_cache_stop_requested(self):
        """The run's wall-clock budget expiring while queued on the source cache's per-uid lock is
        not a source failure: defer cleanly, don't raise out of the planner."""
        planner = SilencePlanner()
        ctx = _make_ctx()
        ctx.source_cache.get_or_fetch.side_effect = StopRequested("stopped")
        provider = MagicMock()
        provider.resolve_media_url.return_value = "http://x.com/video.mp4"
        with patch("citypods.silence.shutil.which", return_value="ffmpeg"):
            result = planner.plan(provider, _make_city(), _make_episode(duration=3600), ctx, None)
        assert result is None

    def test_returns_none_when_provider_error(self):
        from citypods.providers.base import ProviderError

        planner = SilencePlanner()
        ctx = _make_ctx()
        provider = MagicMock()
        provider.resolve_media_url.side_effect = ProviderError("network error")
        with patch("citypods.silence.shutil.which", return_value="ffmpeg"):
            result = planner.plan(provider, _make_city(), _make_episode(), ctx, None)
        assert result is None

    def test_returns_none_when_no_duration_available(self):
        planner = SilencePlanner()
        ctx = _make_ctx()
        provider = MagicMock()
        provider.resolve_media_url.return_value = "http://x.com/video.mp4"
        ep = _make_episode(duration=None)  # no duration in record
        with (
            patch("citypods.silence.shutil.which", return_value="ffmpeg"),
            patch("citypods.silence.detect_silences") as mock_detect,
        ):
            mock_detect.return_value = ([], None)  # also no duration from ffmpeg
            result = planner.plan(provider, _make_city(), ep, ctx, None)
        assert result is None

    def test_uses_ep_duration_as_fallback(self):
        planner = SilencePlanner()
        ctx = _make_ctx()
        provider = MagicMock()
        provider.resolve_media_url.return_value = "http://x.com/video.mp4"
        ep = _make_episode(duration=7200)  # ep.duration set, ffmpeg doesn't return duration
        with (
            patch("citypods.silence.shutil.which", return_value="ffmpeg"),
            patch("citypods.silence.detect_silences") as mock_detect,
        ):
            mock_detect.return_value = ([], None)  # no duration from ffmpeg
            result = planner.plan(provider, _make_city(), ep, ctx, None)
        # No silence, ep.duration used → identity
        assert result is not None
        assert timeline_digest(result) == ""

    def test_returns_none_when_ffmpeg_not_installed(self):
        planner = SilencePlanner()
        ctx = _make_ctx()
        provider = MagicMock()
        with patch("citypods.silence.shutil.which", return_value=None):
            result = planner.plan(provider, _make_city(), _make_episode(), ctx, None)
        assert result is None
        provider.resolve_media_url.assert_not_called()  # no network call when ffmpeg absent

    def test_degenerate_timeline_with_no_prior_falls_back_to_identity(self):
        """A near-total-silence detection (bad probe) with no prior timeline → identity, not
        a near-empty served timeline that would get hosted."""
        planner = SilencePlanner()
        ctx = _make_ctx()
        provider = MagicMock()
        provider.resolve_media_url.return_value = "http://x.com/video.mp4"
        with (
            patch("citypods.silence.shutil.which", return_value="ffmpeg"),
            patch("citypods.silence.detect_silences") as mock_detect,
        ):
            # Almost the entire (claimed) 3600s source flagged silent → 0.01s kept.
            mock_detect.return_value = ([(0.0, 3599.99)], 3600.0)
            result = planner.plan(provider, _make_city(), _make_episode(duration=3600), ctx, None)
        assert result is not None
        assert timeline_digest(result) == ""  # identity, not the degenerate trim

    def test_degenerate_timeline_with_prior_preserves_prior(self):
        """When a (good) prior timeline already exists, a degenerate re-detection must not
        downgrade it — return None so TimelineStage leaves ``current`` untouched."""
        planner = SilencePlanner()
        ctx = _make_ctx()
        provider = MagicMock()
        provider.resolve_media_url.return_value = "http://x.com/video.mp4"
        prior = Timeline(
            version="silence:1",
            segments=(
                Segment(
                    served_start=0.0,
                    served_end=3597.0,
                    kind="source",
                    source_id="s0",
                    source_start=3.0,
                    source_end=3600.0,
                ),
            ),
        )
        with (
            patch("citypods.silence.shutil.which", return_value="ffmpeg"),
            patch("citypods.silence.detect_silences") as mock_detect,
        ):
            mock_detect.return_value = ([(0.0, 3599.99)], 3600.0)
            result = planner.plan(provider, _make_city(), _make_episode(duration=3600), ctx, prior)
        assert result is None


class TestSilencePlannerAvailability:
    """H16 PR3: the silence decode pass also stamps the durable media-availability verdict."""

    @staticmethod
    def _plan(detect_return, *, ep=None, prior_timeline=None):
        planner = SilencePlanner()
        ctx = _make_ctx()
        provider = MagicMock()
        provider.resolve_media_url.return_value = "http://x.com/video.mp4"
        ep = ep or _make_episode(duration=3600)
        with (
            patch("citypods.silence.shutil.which", return_value="ffmpeg"),
            patch("citypods.silence.detect_silences") as mock_detect,
        ):
            mock_detect.return_value = detect_return
            planner.plan(provider, _make_city(), ep, ctx, prior_timeline)
        return ep.media_availability

    def test_real_audio_is_available(self):
        from citypods.availability import AVAILABLE

        av = self._plan(([(0.0, 5.0)], 3600.0))
        assert av is not None and av.state == AVAILABLE and not av.is_withheld()

    def test_near_total_silence_is_suspected_then_confirmed(self):
        from citypods.availability import CONFIRMED_EMPTY, SUSPECTED_EMPTY

        ep = _make_episode(duration=3600)
        first = self._plan(([(0.0, 3599.99)], 3600.0), ep=ep)
        assert first.state == SUSPECTED_EMPTY and first.is_withheld()
        ep.media_availability = first  # carry the verdict into the next independent run
        second = self._plan(([(0.0, 3599.99)], 3600.0), ep=ep)
        assert second.state == CONFIRMED_EMPTY and second.silent_confirmations == 2

    def test_missing_probe_duration_is_transport_not_silence(self):
        from citypods.availability import AVAILABLE

        ep = _make_episode(duration=3600)
        ep.media_availability = self._plan(([(0.0, 5.0)], 3600.0), ep=ep)
        assert ep.media_availability.state == AVAILABLE
        # A later run whose fetch fails to decode (no probe duration) must NOT flip it to empty.
        after = self._plan(([], None), ep=ep)
        assert after.state == AVAILABLE and not after.is_withheld()

    def test_dead_media_is_classified_missing(self):
        from citypods.availability import MISSING
        from citypods.providers.base import MEDIA_DEAD, MediaUnavailable

        planner = SilencePlanner()
        ctx = _make_ctx()
        provider = MagicMock()
        provider.resolve_media_url.side_effect = MediaUnavailable("gone", code=MEDIA_DEAD)
        ep = _make_episode(duration=3600)
        with patch("citypods.silence.shutil.which", return_value="ffmpeg"):
            planner.plan(provider, _make_city(), ep, ctx, None)
        assert ep.media_availability is not None
        assert ep.media_availability.state == MISSING and ep.media_availability.is_withheld()


# ---------------------------------------------------------------------------
# is_degenerate_served_duration
# ---------------------------------------------------------------------------


class TestIsDegenerateServedDuration:
    def test_normal_trim_not_degenerate(self):
        tl = build_silence_timeline("s0", 3600.0, [(0.0, 5.0)], lead_trail_min=1.0)
        assert tl is not None
        assert not is_degenerate_served_duration(tl, 3600.0)

    def test_near_total_wipeout_is_degenerate(self):
        tl = build_silence_timeline("s0", 3600.0, [(0.0, 3599.99)], lead_trail_min=1.0)
        assert tl is not None
        assert is_degenerate_served_duration(tl, 3600.0)

    def test_short_meeting_near_absolute_floor_not_degenerate(self):
        # A genuinely short (20s) meeting with a 2s trim: 18s kept clears the 5s absolute floor
        # even though it's a small fraction of a typical meeting length.
        tl = build_silence_timeline("s0", 20.0, [(0.0, 2.0)], lead_trail_min=1.0)
        assert tl is not None
        assert not is_degenerate_served_duration(tl, 20.0, min_seconds=5.0, min_fraction=0.02)

    def test_custom_thresholds_respected(self):
        tl = build_silence_timeline("s0", 100.0, [(0.0, 90.0)], lead_trail_min=1.0)
        assert tl is not None  # 10s kept of 100s
        assert is_degenerate_served_duration(tl, 100.0, min_seconds=1.0, min_fraction=0.5)
        assert not is_degenerate_served_duration(tl, 100.0, min_seconds=1.0, min_fraction=0.05)
