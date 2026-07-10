"""Tests for scripts/availability_digest.py (the CLI script, distinct from the
citypods.availability_digest library module tests/test_availability_digest.py already covers)."""

from __future__ import annotations

import time

from citypods.availability_digest import Candidate
from citypods.models import City
from citypods.providers import register
from scripts import availability_digest as script


def _candidate(source_key="src"):
    return Candidate(
        source_key=source_key,
        uid="u1",
        title="t",
        state="confirmed_empty",
        reason="silence",
        detector_version="1",
        source_fingerprint="fp",
        profile="noise=-40dB",
        last_check=None,
        video_url="https://x/u1.mp4",
        canonical_url="https://city.gov/watch/u1",
        duration=3600,
    )


class _SlowProvider:
    name = "slow-test-provider"

    def resolve_media_url(self, episode, source):
        time.sleep(2.0)
        return "https://x/u1.mp4"


def _city(slug="x-tx"):
    return City(
        slug=slug,
        provider="slow-test-provider",
        source={"feed_url": "https://x/feed"},
        podcast_title="X",
        podcast_author="City of X",
        podcast_email="",
        podcast_description="d",
    )


def test_resolve_source_url_does_not_block_past_timeout(monkeypatch):
    # CodeRabbit (PR #877): _resolve_source_url used `with ThreadPoolExecutor(...) as pool:`,
    # whose __exit__ calls shutdown(wait=True) unconditionally — so even after future.result()
    # timed out, the function still blocked until the slow provider call finished, defeating the
    # whole point of the timeout. It must now return promptly instead.
    provider = _SlowProvider()
    register(provider)
    try:
        city = _city()
        started = time.monotonic()
        result = script._resolve_source_url(_candidate(), {"src": city}, timeout=0.05)
        elapsed = time.monotonic() - started
    finally:
        from citypods.providers import _REGISTRY

        _REGISTRY.pop("slow-test-provider", None)

    assert result is None  # timed out -> no URL
    assert elapsed < 1.0  # must not have waited for the 2s provider call to finish
