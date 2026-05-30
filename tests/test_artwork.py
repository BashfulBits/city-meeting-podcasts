"""Tests for cover-art generation."""

from __future__ import annotations

from PIL import Image

from citypods.artwork import _contrast, _hex_to_rgb, _resolve_colors, render_cover
from citypods.models import City


def _city(colors=None):
    return City(
        slug="dallas-tx-city-council",
        provider="swagit",
        source={"list_url": "x", "body": "City Council"},
        podcast_title="Dallas: City Council",
        podcast_author="City of Dallas, TX",
        podcast_email="",
        podcast_description="d",
        state="TX",
        colors=colors or [],
    )


def test_render_cover_is_1400_jpeg(tmp_path):
    dest = tmp_path / "art.jpg"
    render_cover(_city(), dest)
    img = Image.open(dest)
    assert img.size == (1400, 1400)
    assert img.format == "JPEG"


def test_hex_parsing_and_contrast():
    assert _hex_to_rgb("#ffffff") == (255, 255, 255)
    assert _hex_to_rgb("#fff") == (255, 255, 255)
    assert _hex_to_rgb("002b5c") == (0, 43, 92)
    assert _contrast((255, 255, 255)) == (20, 20, 20)  # dark text on light bg
    assert _contrast((10, 10, 10)) == (255, 255, 255)  # light text on dark bg


def test_resolve_colors_uses_config_then_derives():
    primary, accent = _resolve_colors(_city(colors=["#002b5c", "#b8860b"]))
    assert primary == (0, 43, 92) and accent == (184, 134, 11)
    # No colors -> deterministic (stable, non-mono) derivation.
    a = _resolve_colors(_city())
    b = _resolve_colors(_city())
    assert a == b and a[0] != (0, 0, 0)
