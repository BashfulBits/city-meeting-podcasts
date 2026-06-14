"""Offline unit tests for citypods.contracts helpers (the live integration runs under -m live)."""

from __future__ import annotations

from citypods.contracts import _is_spa_seek_url


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
