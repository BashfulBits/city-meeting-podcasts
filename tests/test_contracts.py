"""Offline unit tests for citypods.contracts helpers (the live integration runs under -m live)."""

from __future__ import annotations

from datetime import UTC, datetime

from citypods.contracts import _is_spa_seek_url, _media_fetch_detail, _safe_url, check_city
from citypods.models import Episode


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


def test_check_city_unregistered_provider_returns_a_result_not_raises():
    # CR2-SC-03: get_provider() used to run before any try block, so an unregistered provider
    # name raised ProviderError straight out of check_city, aborting the caller's whole scan
    # (--all in scripts/check_endpoints.py) instead of reporting this one city as a failure.
    results = check_city("some-city", "not-a-real-provider", {})
    assert len(results) == 1
    assert results[0].endpoint == "list"
    assert results[0].ok is False
    assert "not-a-real-provider" in results[0].detail
