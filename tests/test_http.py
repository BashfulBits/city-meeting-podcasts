"""Tests for the shared HTTP session retry policy — Retry-After clamping (B2)."""

from __future__ import annotations

from citypods.http import _RETRY, _ClampedRetry


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
