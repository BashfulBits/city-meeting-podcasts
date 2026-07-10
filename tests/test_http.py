"""Tests for the shared HTTP session: Retry-After clamping (B2) + per-host rate limiting (#39)."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
import requests

from citypods.http import (
    _RETRY,
    GuardedHTTPAdapter,
    HostRateLimiter,
    StopRequested,
    _ClampedRetry,
    preflight_media_size,
)


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

        ts = [threading.Thread(target=work, daemon=True) for _ in range(threads)]
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

        t = threading.Thread(target=work, daemon=True)
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

        t = threading.Thread(target=hold, daemon=True)
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

        t = threading.Thread(target=hold_swagit, daemon=True)
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
            ts = [threading.Thread(target=work, daemon=True) for _ in range(8)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            assert peak[0] == 2
        finally:
            http_mod.HOST_LIMITER.configure({})  # reset the process-global singleton

    def test_slot_held_across_the_buffered_content_read_not_just_send(self, monkeypatch):
        """CR2-CP-10/H4: the host slot used to be released right after ``super().send()``, before
        the non-streaming path's buffered ``response.content`` read — the actual body transfer.
        Concurrent buffered downloads to the same host must still serialize to the cap."""
        from citypods import http as http_mod

        monkeypatch.setattr(http_mod, "validate_source_url", lambda *a, **k: None)
        http_mod.HOST_LIMITER.configure({"granicus.com": 2})

        active = [0]
        peak = [0]
        lock = threading.Lock()

        def fake_stream(amt=2**16, **kwargs):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.02)
            with lock:
                active[0] -= 1
            yield b"x" * 10

        def fake_send(self, request, **kwargs):
            resp = requests.Response()
            resp.status_code = 200
            resp.raw = SimpleNamespace(close=lambda: None, stream=lambda *a, **k: fake_stream())
            return resp

        monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", fake_send)
        adapter = GuardedHTTPAdapter()

        def work():
            adapter.send(
                SimpleNamespace(url="https://archive-video.granicus.com/x.mp4", method="GET")
            )

        try:
            ts = [threading.Thread(target=work, daemon=True) for _ in range(8)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            assert peak[0] == 2
        finally:
            http_mod.HOST_LIMITER.configure({})  # reset the process-global singleton


class TestGuardedAdapterTimeoutBackstop:
    """CR2-CP-09/M2: DEFAULT_TIMEOUT was defined but never applied — a caller that forgot
    timeout= would hang forever with no adapter-level backstop."""

    def test_applies_default_timeout_when_caller_omits_it(self, monkeypatch):
        from citypods import http as http_mod

        monkeypatch.setattr(http_mod, "validate_source_url", lambda *a, **k: None)
        captured = {}

        def fake_send(self, request, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(headers={})

        monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", fake_send)
        adapter = GuardedHTTPAdapter()
        adapter.send(SimpleNamespace(url="https://archive-video.granicus.com/x.mp4"))
        assert captured["timeout"] == http_mod.DEFAULT_TIMEOUT

    def test_preserves_explicit_timeout(self, monkeypatch):
        from citypods import http as http_mod

        monkeypatch.setattr(http_mod, "validate_source_url", lambda *a, **k: None)
        captured = {}

        def fake_send(self, request, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(headers={})

        monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", fake_send)
        adapter = GuardedHTTPAdapter()
        adapter.send(SimpleNamespace(url="https://archive-video.granicus.com/x.mp4"), timeout=5)
        assert captured["timeout"] == 5


class TestGuardedAdapterHeadExemption:
    """HEAD never transfers a body, so a big declared Content-Length isn't an over-buffering
    risk the way it is for a plain buffered GET (issue #497's media preflight needs HEAD on a
    legitimately multi-GB media URL to work at all)."""

    def test_head_with_oversized_content_length_does_not_raise(self, monkeypatch):
        from citypods import http as http_mod

        monkeypatch.setattr(http_mod, "validate_source_url", lambda *a, **k: None)

        def fake_send(self, request, **kwargs):
            return SimpleNamespace(headers={"Content-Length": str(500 * 1024 * 1024)})

        monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", fake_send)
        adapter = GuardedHTTPAdapter()
        request = SimpleNamespace(url="https://archive-video.granicus.com/x.mp4", method="HEAD")
        response = adapter.send(request)
        assert response.headers["Content-Length"] == str(500 * 1024 * 1024)

    def test_streamed_get_with_oversized_content_length_does_not_raise(self, monkeypatch):
        """A caller that explicitly streams (e.g. the ranged-GET preflight fallback) and never
        reads the body takes on the responsibility itself; only the buffered-by-default path
        needs the eager reject."""
        from citypods import http as http_mod

        monkeypatch.setattr(http_mod, "validate_source_url", lambda *a, **k: None)

        def fake_send(self, request, **kwargs):
            return SimpleNamespace(headers={"Content-Length": str(500 * 1024 * 1024)})

        monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", fake_send)
        adapter = GuardedHTTPAdapter()
        request = SimpleNamespace(url="https://archive-video.granicus.com/x.mp4", method="GET")
        response = adapter.send(request, stream=True)
        assert response.headers["Content-Length"] == str(500 * 1024 * 1024)

    def test_buffered_get_with_oversized_content_length_still_raises(self, monkeypatch):
        """Unchanged existing behavior: a normal (non-streamed) GET is still eager-rejected."""
        from citypods import http as http_mod
        from citypods.security import SecurityError

        monkeypatch.setattr(http_mod, "validate_source_url", lambda *a, **k: None)

        def fake_send(self, request, **kwargs):
            return SimpleNamespace(
                headers={"Content-Length": str(500 * 1024 * 1024)}, close=lambda: None
            )

        monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", fake_send)
        adapter = GuardedHTTPAdapter()
        request = SimpleNamespace(url="https://archive-video.granicus.com/x.mp4", method="GET")
        with pytest.raises(SecurityError):
            adapter.send(request)


class TestPreflightMediaSize:
    """Issue #497: HEAD-then-ranged-GET-fallback size check ahead of a direct ffmpeg fetch."""

    def _session(
        self,
        monkeypatch,
        head_headers=None,
        head_status=200,
        range_headers=None,
        range_status=206,
        validate=lambda *a, **k: None,
    ):
        from citypods import http as http_mod

        monkeypatch.setattr(http_mod, "validate_source_url", validate)

        def _fake_response(status_code, headers):
            resp = requests.Response()
            resp.status_code = status_code
            resp.headers.update(headers or {})
            # A bodyless response (HEAD, or a 206 this test never reads past headers for): give
            # ``.raw`` just enough surface for requests' auto-buffer-into-``.content`` path
            # (stream_requested=False for HEAD) to resolve to an empty body instead of crashing.
            resp.raw = SimpleNamespace(close=lambda: None, stream=lambda *a, **k: iter(()))
            return resp

        def fake_send(self, request, **kwargs):
            if request.method == "HEAD":
                return _fake_response(head_status, head_headers)
            return _fake_response(range_status, range_headers)

        monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", fake_send)
        return http_mod.make_session()

    def test_known_ok_from_head_content_length(self, monkeypatch):
        session = self._session(monkeypatch, head_headers={"Content-Length": "1000"})
        result = preflight_media_size(
            "https://archive-video.granicus.com/x.mp4", 2000, session=session
        )
        assert result.status == "known_ok"
        assert result.content_length == 1000

    def test_known_too_large_from_head_content_length(self, monkeypatch):
        session = self._session(monkeypatch, head_headers={"Content-Length": "5000"})
        result = preflight_media_size(
            "https://archive-video.granicus.com/x.mp4", 2000, session=session
        )
        assert result.status == "known_too_large"
        assert result.content_length == 5000

    def test_falls_back_to_ranged_get_when_head_omits_length(self, monkeypatch):
        session = self._session(
            monkeypatch,
            head_headers={},
            range_headers={"Content-Range": "bytes 0-0/9999"},
        )
        result = preflight_media_size(
            "https://archive-video.granicus.com/x.mp4", 2000, session=session
        )
        assert result.status == "known_too_large"
        assert result.content_length == 9999

    def test_unknown_when_neither_head_nor_range_discloses_a_size(self, monkeypatch):
        session = self._session(monkeypatch, head_headers={}, range_headers={})
        result = preflight_media_size(
            "https://archive-video.granicus.com/x.mp4", 2000, session=session
        )
        assert result.status == "unknown"
        assert result.content_length is None

    def test_error_response_content_length_is_not_trusted(self, monkeypatch):
        """CodeRabbit #800: a 403/404/5xx error page can carry its own small Content-Length —
        that must never be read as the real resource's size (a HEAD-disallowing CDN answering
        with a small error body would otherwise look "known_ok" for an actually huge source)."""
        session = self._session(
            monkeypatch,
            head_status=403,
            head_headers={"Content-Length": "50"},
            range_status=403,
            range_headers={"Content-Length": "50"},
        )
        result = preflight_media_size(
            "https://archive-video.granicus.com/x.mp4", 2000, session=session
        )
        assert result.status == "unknown"
        assert result.content_length is None

    def test_error_head_falls_back_to_a_successful_ranged_get(self, monkeypatch):
        """A HEAD that fails (ignored, so no size) can still resolve via a *successful* ranged
        GET — only the error response's own headers are untrusted, not the whole preflight."""
        session = self._session(
            monkeypatch,
            head_status=405,
            head_headers={"Content-Length": "50"},
            range_status=206,
            range_headers={"Content-Range": "bytes 0-0/9999"},
        )
        result = preflight_media_size(
            "https://archive-video.granicus.com/x.mp4", 2000, session=session
        )
        assert result.status == "known_too_large"
        assert result.content_length == 9999

    def test_security_error_propagates_instead_of_becoming_unknown(self, monkeypatch):
        """CodeRabbit #800: an SSRF/host-allowlist rejection must abort the fetch, not be treated
        as an inconclusive size that lets the caller proceed to hand the same URL to ffmpeg."""
        from citypods.security import SecurityError

        def _reject(*a, **k):
            raise SecurityError("blocked: resolves to a private IP")

        session = self._session(monkeypatch, validate=_reject)
        with pytest.raises(SecurityError):
            preflight_media_size("https://archive-video.granicus.com/x.mp4", 2000, session=session)
