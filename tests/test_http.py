"""Tests for the shared HTTP session: Retry-After clamping (B2) + per-host rate limiting (#39)."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
import requests

from citypods.http import _RETRY, GuardedHTTPAdapter, HostRateLimiter, StopRequested, _ClampedRetry


class _Resp:
    """Minimal stand-in for a urllib3 response carrying a Retry-After header."""

    def __init__(self, retry_after):
        self.headers = {} if retry_after is None else {"Retry-After": retry_after}
        self.status = 429


class TestRetryAfterClamp:
    def test_long_retry_after_is_clamped(self):
        # A hostile/misconfigured Retry-After: 3600 must not stall the build for an hour.
        assert _RETRY.get_retry_after(_Resp("3600")) == _ClampedRetry.MAX_RETRY_AFTER_SECONDS

    def test_value_at_cap_is_unchanged(self):
        cap = _ClampedRetry.MAX_RETRY_AFTER_SECONDS
        assert _RETRY.get_retry_after(_Resp(str(cap))) == cap

    def test_short_retry_after_is_honored(self):
        assert _RETRY.get_retry_after(_Resp("5")) == 5

    def test_no_header_returns_none(self):
        assert _RETRY.get_retry_after(_Resp(None)) is None

    def test_policy_actually_consults_retry_after(self):
        # If this were False the clamp would be dead code (urllib3 skips get_retry_after).
        assert _RETRY.respect_retry_after_header is True

    def test_clamp_survives_copy(self):
        # urllib3 copies the Retry via new() during the loop; the subclass + cap must persist,
        # or a long Retry-After on a *later* attempt would hang the build.
        nxt = _RETRY.new()
        assert isinstance(nxt, _ClampedRetry)
        assert nxt.get_retry_after(_Resp("3600")) == _ClampedRetry.MAX_RETRY_AFTER_SECONDS


class TestHostRateLimiter:
    """#39: a per-registrable-domain concurrency cap shared by the requests session and the ffmpeg
    fetch paths, so a sharded burst never opens more than the configured number of simultaneous
    connections to one provider tenant."""

    @staticmethod
    def _burst(limiter, urls, threads):
        """Run ``threads`` workers that each hold ``limiter.slots(urls)`` briefly; return the peak
        observed concurrency inside the slot.

        A barrier holds every worker at the starting line until all ``threads`` have been
        created, so they all contend for the slot at once instead of trickling in over the
        20ms hold window -- making the peak deterministic rather than load/GIL-dependent."""
        active = [0]
        peak = [0]
        lock = threading.Lock()
        barrier = threading.Barrier(threads)

        def work():
            barrier.wait()
            with limiter.slots(urls):
                with lock:
                    active[0] += 1
                    peak[0] = max(peak[0], active[0])
                time.sleep(0.02)
                with lock:
                    active[0] -= 1

        ts = [threading.Thread(target=work) for _ in range(threads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        return peak[0]

    def test_concurrent_requests_to_one_host_serialize_to_the_cap(self):
        lim = HostRateLimiter()
        lim.configure({"granicus.com": 2})
        peak = self._burst(lim, ["https://archive-video.granicus.com/x.mp4"], threads=8)
        assert peak == 2  # never exceeds the configured cap under an 8-way burst

    def test_cap_of_one_fully_serializes(self):
        lim = HostRateLimiter()
        lim.configure({"swagit.com": 1})
        assert self._burst(lim, ["https://v.swagit.com/a.mp4"], threads=6) == 1

    def test_unconfigured_host_is_unlimited(self):
        lim = HostRateLimiter()
        lim.configure({"granicus.com": 1})
        # example.com has no configured cap → the no-op slot must let every worker run
        # concurrently. The barrier in _burst makes this deterministic: asserting the full
        # thread count (not just >= 2) actually proves no cap is applied, including a
        # regression that accidentally defaults unconfigured hosts to a small cap.
        threads = 5
        assert self._burst(lim, ["https://example.com/a.mp4"], threads=threads) == threads

    def test_registrable_domain_matches_any_subdomain(self):
        lim = HostRateLimiter()
        lim.configure({"granicus.com": 1})
        # archive-video.* and the bare apex both resolve to the granicus.com cap.
        assert lim._key_for("archive-video.granicus.com") == "granicus.com"
        assert lim._key_for("granicus.com") == "granicus.com"
        # A look-alike must NOT match (suffix is dotted).
        assert lim._key_for("evilgranicus.com") is None

    def test_longest_suffix_match_wins(self):
        lim = HostRateLimiter()
        lim.configure({"granicus.com": 5, "archive.granicus.com": 1})
        assert lim._key_for("x.archive.granicus.com") == "archive.granicus.com"

    def test_slots_dedupes_same_domain_so_one_call_takes_one_slot(self):
        # Two URLs on the same domain must consume ONE slot, not two — else a cap-1 multi-source
        # ffmpeg render would deadlock against itself.
        lim = HostRateLimiter()
        lim.configure({"granicus.com": 1})
        done = []

        def work():
            with lim.slots(["https://a.granicus.com/1.mp4", "https://b.granicus.com/2.mp4"]):
                done.append(True)

        t = threading.Thread(target=work)
        t.start()
        t.join(timeout=2.0)
        assert done == [True]  # completed without self-deadlock

    def test_local_paths_and_empty_are_no_ops(self):
        lim = HostRateLimiter()
        lim.configure({"granicus.com": 1})
        with lim.slots(["/tmp/cached.m4a", ""]):  # no host → no slot held
            pass
        with lim.slots([]):
            pass

    def test_nonpositive_caps_are_ignored(self):
        lim = HostRateLimiter()
        lim.configure({"granicus.com": 0, "swagit.com": -3})
        assert lim._key_for("x.granicus.com") is None
        assert lim._key_for("x.swagit.com") is None


class TestHostRateLimiterStopAware:
    """A queued slot wait yields ``StopRequested`` once the run's wall-clock budget expires,
    instead of blocking out the full queue."""

    def test_stop_firing_raises_instead_of_blocking(self):
        lim = HostRateLimiter()
        lim.configure({"granicus.com": 1})
        # Hold the only slot on a background thread so the foreground wait queues.
        holder_released = threading.Event()

        def hold():
            with lim.slots(["https://archive-video.granicus.com/x.mp4"]):
                holder_released.wait(timeout=2.0)

        t = threading.Thread(target=hold)
        t.start()
        try:
            with pytest.raises(StopRequested):
                with lim.slots(
                    ["https://archive-video.granicus.com/x.mp4"],
                    stop=lambda: True,
                    poll_seconds=0.01,
                ):
                    pass
        finally:
            holder_released.set()
            t.join()

    def test_stop_never_firing_still_acquires(self):
        lim = HostRateLimiter()
        lim.configure({"granicus.com": 1})
        with lim.slots(
            ["https://archive-video.granicus.com/x.mp4"], stop=lambda: False, poll_seconds=0.01
        ):
            pass  # acquired without raising

    def test_stop_does_not_leak_partially_acquired_slots(self):
        """Two distinct hosts, second blocked: stopping must release the first before raising, or
        the held semaphore would starve every other waiter on that host forever."""
        lim = HostRateLimiter()
        lim.configure({"granicus.com": 1, "swagit.com": 1})
        holder_released = threading.Event()

        def hold_swagit():
            with lim.slots(["https://v.swagit.com/a.mp4"]):
                holder_released.wait(timeout=2.0)

        t = threading.Thread(target=hold_swagit)
        t.start()
        try:
            with pytest.raises(StopRequested):
                with lim.slots(
                    ["https://archive-video.granicus.com/x.mp4", "https://v.swagit.com/a.mp4"],
                    stop=lambda: True,
                    poll_seconds=0.01,
                ):
                    pass
            # The granicus.com slot acquired before the swagit.com block must have been released.
            with lim.slots(["https://archive-video.granicus.com/x.mp4"]):
                pass
        finally:
            holder_released.set()
            t.join()


class Test403RetriedAsRateLimit:
    """#39 lifts the bespoke Granicus 403 retry into the shared layer: a 403 (provider throttle, not
    auth, since media bytes never go through requests) is now a retryable status."""

    def test_403_is_in_the_status_forcelist(self):
        assert 403 in _RETRY.status_forcelist

    def test_retry_after_clamp_is_preserved(self):
        # The B2 clamp must survive the #39 forcelist change.
        assert _RETRY.respect_retry_after_header is True
        assert _RETRY.get_retry_after(_Resp("3600")) == _ClampedRetry.MAX_RETRY_AFTER_SECONDS

    def test_only_idempotent_methods_retry(self):
        assert _RETRY.allowed_methods == frozenset({"GET", "HEAD"})


class TestGuardedAdapterHoldsHostSlot:
    def test_send_acquires_the_host_slot(self, monkeypatch):
        """The adapter holds the per-host slot for the network round-trip — concurrent sends to one
        host serialize to the cap (validated end-to-end through GuardedHTTPAdapter.send)."""
        from citypods import http as http_mod

        monkeypatch.setattr(http_mod, "validate_source_url", lambda *a, **k: None)
        http_mod.HOST_LIMITER.configure({"granicus.com": 2})

        active = [0]
        peak = [0]
        lock = threading.Lock()

        def fake_super_send(self, request, **kwargs):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.02)
            with lock:
                active[0] -= 1
            return SimpleNamespace(headers={})

        monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", fake_super_send)
        adapter = GuardedHTTPAdapter()

        def work():
            adapter.send(SimpleNamespace(url="https://archive-video.granicus.com/x.mp4"))

        try:
            ts = [threading.Thread(target=work) for _ in range(8)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            assert peak[0] == 2
        finally:
            http_mod.HOST_LIMITER.configure({})  # reset the process-global singleton
