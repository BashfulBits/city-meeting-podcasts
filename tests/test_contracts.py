"""Offline unit tests for citypods.contracts helpers (the live integration runs under -m live)."""

from __future__ import annotations

from datetime import UTC, datetime

import requests

from citypods.contracts import _is_spa_seek_url, _media_fetch_detail, _safe_url, check_city
from citypods.models import Episode
from citypods.providers.base import ProviderError


def test_spa_seek_url_true_for_path_timestamp():
    # Swagit's player route: /play/{id}/{seconds} — a client-side route the server 404s on a HEAD.
    assert _is_spa_seek_url("https://addisontx.new.swagit.com/play/390531/30") is True
    assert _is_spa_seek_url("https://x.swagit.com/play/1/0") is True


def test_spa_seek_url_false_for_query_param_anchor():
    # Granicus deep-links seek via a query param and ARE server-resolvable — not an SPA route.
    granicus = "https://arlingtontx.granicus.com/MediaPlayer.php?view_id=2&clip_id=5&starttime=30"
    assert _is_spa_seek_url(granicus) is False


def test_spa_seek_url_false_when_last_segment_not_numeric():
    # Only ever called on a generated deeplink, which always ends in a numeric timestamp; a
    # non-numeric tail (or none) is not an SPA seek route.
    assert _is_spa_seek_url("https://x.swagit.com/videos/clip-abc") is False
    assert _is_spa_seek_url("https://x.granicus.com/MediaPlayer.php") is False


# --- presigned-URL redaction (CR2-CP-28/MR-CP-04) ---------------------------------------


def test_safe_url_strips_query_string():
    presigned = (
        "https://s3.amazonaws.com/bucket/key.mp4?AWSAccessKeyId=AKIA&Signature=abc&Expires=1"
    )
    assert _safe_url(presigned) == "https://s3.amazonaws.com/bucket/key.mp4?<redacted>"


def test_safe_url_leaves_query_less_url_unchanged():
    assert _safe_url("https://example.com/path") == "https://example.com/path"


def test_media_fetch_detail_uses_safe_url():
    presigned = "https://cdn.example/a.mp4?Signature=topsecret"
    detail = _media_fetch_detail(resolved_url=presigned, size=0, seconds=3.0, ok=False, logs=[])
    assert "topsecret" not in detail
    assert "url=https://cdn.example/a.mp4?<redacted>" in detail


class _FakeProvider:
    name = "fake"

    def fetch_episodes(self, source):
        return [
            Episode(
                guid="1",
                title="Meeting",
                published=datetime(2026, 1, 1, tzinfo=UTC),
                video_url="https://cdn.example/a.mp4",
            )
        ]

    def resolve_media_url(self, episode, source):
        return "https://cdn.example/a.mp4?AWSAccessKeyId=AKIA&Signature=topsecret&Expires=1"

    def fetch_view_counts(self, source):
        return []  # Uncapped archive-backed providers have no cap data to report.


def test_check_city_media_check_redacts_presigned_query(monkeypatch):
    monkeypatch.setattr("citypods.contracts.get_provider", lambda name: _FakeProvider())
    monkeypatch.setattr("shutil.which", lambda _name: None)  # skip the media-fetch sub-check
    results = check_city("fake-city", "fake", {})
    media = next(r for r in results if r.endpoint == "media")
    assert "topsecret" not in media.detail
    assert media.detail == "https://cdn.example/a.mp4?<redacted>"


def test_check_city_accepts_empty_view_counts_for_uncapped_provider(monkeypatch):
    monkeypatch.setattr("citypods.contracts.get_provider", lambda name: _FakeProvider())
    monkeypatch.setattr("shutil.which", lambda _name: None)  # skip the media-fetch sub-check

    results = check_city("fake-city", "fake", {})

    view_counts = next(r for r in results if r.endpoint == "view_counts")
    assert view_counts.ok is True
    assert view_counts.detail == "[]"


def test_check_city_confirms_a_transient_listing_failure(monkeypatch):
    calls = []
    pauses = []

    class _FlakyProvider(_FakeProvider):
        def fetch_episodes(self, source):
            del source
            calls.append(None)
            if len(calls) == 1:
                try:
                    raise requests.ReadTimeout("CivicClerk read timed out")
                except requests.ReadTimeout as exc:
                    raise ProviderError("GET events failed") from exc
            return super().fetch_episodes({})

    monkeypatch.setattr("citypods.contracts.get_provider", lambda name: _FlakyProvider())
    monkeypatch.setattr("citypods.contracts.time.sleep", pauses.append)
    monkeypatch.setattr("shutil.which", lambda _name: None)

    results = check_city("fake-city", "fake", {})

    listing = next(r for r in results if r.endpoint == "list")
    assert listing.ok is True
    assert listing.detail == "1 episodes (after 1 transient retry)"
    assert len(calls) == 2
    assert pauses == [1.0]


def test_check_city_does_not_retry_a_non_transient_listing_failure(monkeypatch):
    calls = []

    class _BrokenProvider(_FakeProvider):
        def fetch_episodes(self, source):
            del source
            calls.append(None)
            raise ProviderError("invalid CivicClerk JSON")

    monkeypatch.setattr("citypods.contracts.get_provider", lambda name: _BrokenProvider())

    results = check_city("fake-city", "fake", {})

    listing = next(r for r in results if r.endpoint == "list")
    assert listing.ok is False
    assert len(calls) == 1


def test_check_city_bounds_repeated_transient_listing_failures(monkeypatch):
    calls = []
    pauses = []

    class _UnavailableProvider(_FakeProvider):
        def fetch_episodes(self, source):
            del source
            calls.append(None)
            try:
                raise requests.ReadTimeout("CivicClerk read timed out")
            except requests.ReadTimeout as exc:
                raise ProviderError("GET events failed") from exc

    monkeypatch.setattr("citypods.contracts.get_provider", lambda name: _UnavailableProvider())
    monkeypatch.setattr("citypods.contracts.time.sleep", pauses.append)

    results = check_city("fake-city", "fake", {})

    listing = next(r for r in results if r.endpoint == "list")
    assert listing.ok is False
    assert len(calls) == 2
    assert pauses == [1.0]


def test_check_city_unregistered_provider_returns_a_result_not_raises():
    # CR2-SC-03: get_provider() used to run before any try block, so an unregistered provider
    # name raised ProviderError straight out of check_city, aborting the caller's whole scan
    # (--all in scripts/check_endpoints.py) instead of reporting this one city as a failure.
    results = check_city("some-city", "not-a-real-provider", {})
    assert len(results) == 1
    assert results[0].endpoint == "list"
    assert results[0].ok is False
    assert "not-a-real-provider" in results[0].detail


def test_check_city_media_fetch_exception_includes_ffmpeg_log(monkeypatch):
    monkeypatch.setattr("citypods.contracts.get_provider", lambda name: _FakeProvider())
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/ffmpeg")

    def _failing_download(_url, _dest, *, log=None, **_kwargs):
        if log is not None:
            log("ffmpeg probe event failure details")
        raise RuntimeError("ffmpeg crash")

    monkeypatch.setattr("citypods.media._download_audio", _failing_download)
    results = check_city("fake-city", "fake", {})
    fetch = next(r for r in results if r.endpoint == "media-fetch")
    assert fetch.ok is False
    assert "RuntimeError('ffmpeg crash')" in fetch.detail
    assert "ffmpeg=ffmpeg probe event failure details" in fetch.detail
