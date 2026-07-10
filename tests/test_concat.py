"""Tests for citypods/concat.py: SwagitConcatPlanner (#122)."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from citypods.concat import SwagitConcatPlanner, _probe_duration_url
from citypods.http import StopRequested
from citypods.security import SecurityError
from citypods.timeline import timeline_digest

SEG_URL_0 = "https://swagit-video.granicus.com/archive/2014/01/21/616.h264.mp4"
SEG_URL_1 = "https://swagit-video.granicus.com/archive/2014/01/21/617.h264.mp4"
SEG_OBJS = [(SEG_URL_0, "Call to Order"), (SEG_URL_1, "Item 1")]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(ffmpeg_binary="ffmpeg", timeout=None):
    ctx = MagicMock()
    ctx.ffmpeg.binary = ffmpeg_binary
    ctx.ffmpeg.timeout_seconds = timeout
    return ctx


def _make_city(provider="swagit"):
    city = MagicMock()
    city.provider = provider
    city.source = {
        "list_url": "https://dallastx.new.swagit.com/views/default",
        "body": "City Council",
    }
    return city


def _make_ep(media_kind="hls"):
    ep = MagicMock()
    ep.media_kind = media_kind
    ep.sources = []
    ep.chapters = []
    ep.chapters_basis = "source:s0"
    ep.links = {"canonical_video": "https://dallastx.new.swagit.com/videos/201667"}
    return ep


def _make_provider(seg_objs=SEG_OBJS):
    provider = MagicMock()
    provider.fetch_segment_objects.return_value = seg_objs
    return provider


# ---------------------------------------------------------------------------
# Guard conditions
# ---------------------------------------------------------------------------


class TestGuards:
    def test_skips_non_swagit_provider(self):
        planner = SwagitConcatPlanner()
        city = _make_city(provider="civicplus")
        result = planner.plan(_make_provider(), city, _make_ep(), _make_ctx(), None)
        assert result is None

    def test_skips_non_hls_episode(self):
        planner = SwagitConcatPlanner()
        result = planner.plan(
            _make_provider(), _make_city(), _make_ep(media_kind="direct"), _make_ctx(), None
        )
        assert result is None

    def test_skips_when_ffmpeg_not_installed(self):
        planner = SwagitConcatPlanner()
        with patch("citypods.concat.shutil.which", return_value=None):
            result = planner.plan(_make_provider(), _make_city(), _make_ep(), _make_ctx(), None)
        assert result is None

    def test_skips_modern_meeting(self):
        """fetch_segment_objects returns None → modern keyed meeting → pass through."""
        planner = SwagitConcatPlanner()
        provider = _make_provider(seg_objs=None)
        with (
            patch("citypods.concat.shutil.which", return_value="ffmpeg"),
            patch("citypods.concat._probe_duration_url", return_value=1800.0),
        ):
            result = planner.plan(provider, _make_city(), _make_ep(), _make_ctx(), None)
        assert result is None

    def test_skips_single_segment(self):
        """One segment → single-segment keyless → resolve_media_url handles it."""
        planner = SwagitConcatPlanner()
        provider = _make_provider(seg_objs=[(SEG_URL_0, "Full Meeting")])
        with (
            patch("citypods.concat.shutil.which", return_value="ffmpeg"),
            patch("citypods.concat._probe_duration_url", return_value=3600.0),
        ):
            result = planner.plan(provider, _make_city(), _make_ep(), _make_ctx(), None)
        assert result is None

    def test_defers_when_probe_fails(self):
        """If any segment duration probe returns None, planner returns None (defer)."""
        planner = SwagitConcatPlanner()
        with (
            patch("citypods.concat.shutil.which", return_value="ffmpeg"),
            patch("citypods.concat._probe_duration_url", return_value=None),
        ):
            result = planner.plan(_make_provider(), _make_city(), _make_ep(), _make_ctx(), None)
        assert result is None

    def test_probe_failure_records_segment_index(self):
        """The failure is recorded against the specific segment that failed to probe."""
        planner = SwagitConcatPlanner()
        ep = _make_ep()
        # First segment probes fine, second fails → code should identify segment 1.
        with (
            patch("citypods.concat.shutil.which", return_value="ffmpeg"),
            patch("citypods.concat._probe_duration_url", side_effect=[1800.0, None]),
            patch("citypods.concat.record_materialize_failure") as mock_record,
        ):
            result = planner.plan(_make_provider(), _make_city(), ep, _make_ctx(), None)
        assert result is None
        mock_record.assert_called_once_with(ep, "concat-probe:s1")

    def test_defers_when_provider_raises(self):
        """Network failure in fetch_segment_objects → return None (defer, not error)."""
        from citypods.providers.base import ProviderError

        planner = SwagitConcatPlanner()
        provider = MagicMock()
        provider.fetch_segment_objects.side_effect = ProviderError("network down")
        with patch("citypods.concat.shutil.which", return_value="ffmpeg"):
            result = planner.plan(provider, _make_city(), _make_ep(), _make_ctx(), None)
        assert result is None

    def test_defers_without_recording_failure_when_probe_stop_requested(self):
        """The run's wall-clock budget expiring mid-probe is not a source failure: no backoff
        should be recorded for it (#120-style false penalty)."""
        planner = SwagitConcatPlanner()
        ep = _make_ep()
        with (
            patch("citypods.concat.shutil.which", return_value="ffmpeg"),
            patch("citypods.concat._probe_duration_url", side_effect=StopRequested("stopped")),
            patch("citypods.concat.record_materialize_failure") as mock_record,
        ):
            result = planner.plan(_make_provider(), _make_city(), ep, _make_ctx(), None)
        assert result is None
        mock_record.assert_not_called()

    def test_probe_called_with_ctx_stop(self):
        planner = SwagitConcatPlanner()
        ctx = _make_ctx()
        ctx.stop = lambda: False
        with (
            patch("citypods.concat.shutil.which", return_value="ffmpeg"),
            patch("citypods.concat._probe_duration_url", return_value=1800.0) as mock_probe,
        ):
            planner.plan(_make_provider(), _make_city(), _make_ep(), ctx, None)
        assert mock_probe.call_args.kwargs["stop"] is ctx.stop

    def test_provider_raises_records_fetch_failure(self):
        from citypods.providers.base import ProviderError

        planner = SwagitConcatPlanner()
        provider = MagicMock()
        ep = _make_ep()
        provider.fetch_segment_objects.side_effect = ProviderError("network down")
        with (
            patch("citypods.concat.shutil.which", return_value="ffmpeg"),
            patch("citypods.concat.record_materialize_failure") as mock_record,
        ):
            result = planner.plan(provider, _make_city(), ep, _make_ctx(), None)
        assert result is None
        mock_record.assert_called_once_with(ep, "concat-fetch")


# ---------------------------------------------------------------------------
# Timeline construction
# ---------------------------------------------------------------------------


class TestTimelineConstruction:
    def _run(self, seg_objs=SEG_OBJS, durations=(1800.0, 2700.0)):
        planner = SwagitConcatPlanner()
        ep = _make_ep()
        dur_iter = iter(durations)
        with (
            patch("citypods.concat.shutil.which", return_value="ffmpeg"),
            patch(
                "citypods.concat._probe_duration_url", side_effect=lambda *a, **kw: next(dur_iter)
            ),
        ):
            tl = planner.plan(_make_provider(seg_objs), _make_city(), ep, _make_ctx(), None)
        return tl, ep

    def test_returns_non_identity_timeline(self):
        tl, _ = self._run()
        assert tl is not None
        assert timeline_digest(tl) != ""  # non-identity → will encode via filter_complex

    def test_timeline_has_correct_segment_count(self):
        tl, _ = self._run()
        assert len(tl.segments) == 2

    def test_served_offsets_are_cumulative(self):
        tl, _ = self._run(durations=(1800.0, 2700.0))
        s0, s1 = tl.segments
        assert s0.served_start == 0.0
        assert s0.served_end == 1800.0
        assert s1.served_start == 1800.0
        assert s1.served_end == 4500.0

    def test_each_source_spans_full_duration(self):
        """source_start=0, source_end=duration for each segment (full copy, no trim)."""
        tl, _ = self._run(durations=(1800.0, 2700.0))
        for seg, dur in zip(tl.segments, (1800.0, 2700.0), strict=True):
            assert seg.source_start == 0.0
            assert seg.source_end == dur

    def test_source_ids_are_sequential(self):
        tl, _ = self._run()
        assert [s.source_id for s in tl.segments] == ["s0", "s1"]

    def test_ep_sources_populated(self):
        _, ep = self._run()
        assert len(ep.sources) == 2
        assert ep.sources[0].id == "s0"
        assert ep.sources[0].ref == SEG_URL_0
        assert ep.sources[0].provider == "swagit"
        assert ep.sources[0].media_kind == "direct"
        assert ep.sources[0].duration == 1800.0
        assert ep.sources[1].id == "s1"
        assert ep.sources[1].ref == SEG_URL_1

    def test_ep_sources_watch_url_from_links(self):
        _, ep = self._run()
        assert ep.sources[0].watch_url == "https://dallastx.new.swagit.com/videos/201667"


# ---------------------------------------------------------------------------
# Chapter construction
# ---------------------------------------------------------------------------


class TestChapterConstruction:
    def _run(self, seg_objs=SEG_OBJS, durations=(1800.0, 2700.0)):
        planner = SwagitConcatPlanner()
        ep = _make_ep()
        dur_iter = iter(durations)
        with (
            patch("citypods.concat.shutil.which", return_value="ffmpeg"),
            patch(
                "citypods.concat._probe_duration_url", side_effect=lambda *a, **kw: next(dur_iter)
            ),
        ):
            planner.plan(_make_provider(seg_objs), _make_city(), ep, _make_ctx(), None)
        return ep

    def test_chapters_set_from_segment_titles(self):
        ep = self._run()
        assert [c["title"] for c in ep.chapters] == ["Call to Order", "Item 1"]

    def test_chapters_at_cumulative_served_offsets(self):
        ep = self._run(durations=(1800.0, 2700.0))
        assert ep.chapters[0]["start"] == 0.0
        assert ep.chapters[0]["end"] == 1800.0
        assert ep.chapters[1]["start"] == 1800.0
        assert ep.chapters[1]["end"] == 4500.0

    def test_chapters_basis_marked_served(self):
        ep = self._run()
        assert ep.chapters_basis == "served"

    def test_no_chapters_when_titles_empty(self):
        """Segments with empty titles produce no chapters."""
        seg_objs = [(SEG_URL_0, ""), (SEG_URL_1, "")]
        planner = SwagitConcatPlanner()
        ep = _make_ep()
        dur_iter = iter([1800.0, 2700.0])
        with (
            patch("citypods.concat.shutil.which", return_value="ffmpeg"),
            patch(
                "citypods.concat._probe_duration_url", side_effect=lambda *a, **kw: next(dur_iter)
            ),
        ):
            planner.plan(_make_provider(seg_objs), _make_city(), ep, _make_ctx(), None)
        # ep.chapters should not have been overwritten (stays as MagicMock default or [])
        assert ep.chapters_basis != "served"


# ---------------------------------------------------------------------------
# _probe_duration_url (unit)
# ---------------------------------------------------------------------------


class TestProbeDurationUrl:
    @pytest.fixture(autouse=True)
    def _bypass_ssrf_gate(self):
        """These tests exercise the ffprobe-invocation/output-parsing logic in isolation; the
        SSRF gate (validate_source_url) has its own dedicated tests below."""
        with patch("citypods.concat.validate_source_url"):
            yield

    def test_acquires_host_slot_before_distributed_lease(self):
        """Match the global media lock order so concat probes cannot deadlock source-cache work."""
        events = []

        @contextmanager
        def held(name):
            """Record context-manager acquisition and release order."""
            events.append(f"enter:{name}")
            try:
                yield
            finally:
                events.append(f"exit:{name}")

        def host_slot(*_args, **_kwargs):
            """Return the recorded process-local host slot."""
            return held("host")

        def distributed_slots(*_args, **_kwargs):
            """Return the recorded cross-shard distributed lease."""
            return held("distributed")

        host_limiter = MagicMock()
        host_limiter.slot.side_effect = host_slot
        lease_pool = MagicMock()
        lease_pool.slots.side_effect = distributed_slots

        with (
            patch("citypods.concat.HOST_LIMITER", host_limiter),
            patch("citypods.concat.DISTRIBUTED_PROVIDER_LEASES", lease_pool),
            patch("citypods.concat.subprocess.run") as mock_run,
        ):
            mock_run.return_value.stdout = "3600\n"
            assert _probe_duration_url(SEG_URL_0) == 3600.0

        assert events == [
            "enter:host",
            "enter:distributed",
            "exit:distributed",
            "exit:host",
        ]

    def test_returns_float_on_success(self):
        with patch("citypods.concat.subprocess.run") as mock_run:
            mock_run.return_value.stdout = "3600.123456\n"
            result = _probe_duration_url("http://example.com/file.mp4")
        assert result == pytest.approx(3600.123456)

    def test_probe_sends_browser_user_agent(self):
        from citypods.http import USER_AGENT

        with patch("citypods.concat.subprocess.run") as mock_run:
            mock_run.return_value.stdout = "3600\n"
            _probe_duration_url("https://swagit-video.granicus.com/archive/x.mp4")

        argv = mock_run.call_args.args[0]
        assert "-user_agent" in argv
        assert argv[argv.index("-user_agent") + 1] == USER_AGENT

    def test_returns_none_on_empty_output(self):
        with patch("citypods.concat.subprocess.run") as mock_run:
            mock_run.return_value.stdout = ""
            result = _probe_duration_url("http://example.com/file.mp4")
        assert result is None

    def test_returns_none_on_subprocess_error(self):
        import subprocess

        err = subprocess.CalledProcessError(1, "ffprobe")
        with patch("citypods.concat.subprocess.run", side_effect=err):
            result = _probe_duration_url("http://example.com/file.mp4")
        assert result is None

    def test_returns_none_on_timeout(self):
        import subprocess

        err = subprocess.TimeoutExpired("ffprobe", 30)
        with patch("citypods.concat.subprocess.run", side_effect=err):
            result = _probe_duration_url("http://example.com/file.mp4", timeout=30)
        assert result is None

    def test_uses_ffprobe_binary_from_ffmpeg_path(self):
        """When ffmpeg_binary is 'ffmpeg', ffprobe binary is 'ffprobe'."""
        with patch("citypods.concat.subprocess.run") as mock_run:
            mock_run.return_value.stdout = "100.0"
            _probe_duration_url("http://x.com/f.mp4", ffprobe="ffprobe")
            cmd = mock_run.call_args[0][0]
        assert cmd[0] == "ffprobe"

    def test_stop_firing_raises_before_running_ffprobe(self):
        """A stop predicate that's already true must abort a queued host-rate-limit wait (and
        never call ffprobe) rather than block until the holder releases the slot."""
        import threading

        from citypods.http import HOST_LIMITER

        url = "https://archive-video.granicus.com/x.mp4"
        HOST_LIMITER.configure({"granicus.com": 1})
        holder_released = threading.Event()

        def hold():
            with HOST_LIMITER.slot(url):
                holder_released.wait(timeout=2.0)

        t = threading.Thread(target=hold)
        t.start()
        try:
            with patch("citypods.concat.subprocess.run") as mock_run:
                with pytest.raises(StopRequested):
                    _probe_duration_url(url, stop=lambda: True)
            mock_run.assert_not_called()
        finally:
            holder_released.set()
            t.join()
            HOST_LIMITER.configure({})


class TestProbeDurationUrlSsrfGate:
    """MR-CP-01/C3: this was previously the one ffprobe call in the tree with no SSRF gate at
    all — url is a page-scraped Swagit dfile URL, not implicitly trusted."""

    def test_validates_url_before_running_ffprobe(self):
        with (
            patch("citypods.concat.validate_source_url") as mock_validate,
            patch("citypods.concat.subprocess.run") as mock_run,
        ):
            mock_run.return_value.stdout = "3600\n"
            assert _probe_duration_url(SEG_URL_0) == 3600.0
        mock_validate.assert_called_once_with(SEG_URL_0, resolve=True)

    def test_blocked_url_returns_none_without_running_ffprobe(self):
        with (
            patch("citypods.concat.validate_source_url", side_effect=SecurityError("blocked")),
            patch("citypods.concat.subprocess.run") as mock_run,
        ):
            result = _probe_duration_url("https://169.254.169.254/x.mp4")
        assert result is None
        mock_run.assert_not_called()
