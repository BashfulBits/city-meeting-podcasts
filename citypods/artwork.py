"""Generate 1400x1400 podcast cover art per feed.

Colored typographic cover using the city's two brand colors (``City.colors``) — or a
pleasant color derived from the city name when none are set — so covers look at home in
podcast apps rather than stark black/white. (Seal compositing is a later pass; this is
the fallback design, kept colorful by design.)
"""

from __future__ import annotations

import colorsys
import hashlib
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from citypods.models import City

SIZE = 1400
MARGIN = 110
FONT_DIR = Path(__file__).resolve().parent / "assets" / "fonts"


def _font(bold: bool, size: int) -> ImageFont.FreeTypeFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    return ImageFont.truetype(str(FONT_DIR / name), size)


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    v = value.lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    return tuple(int(v[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _derived_colors(seed: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """Deterministic, pleasant primary + accent from a name (stable across runs)."""
    h = int(hashlib.sha1(seed.encode()).hexdigest(), 16)
    hue = (h % 360) / 360.0
    primary = colorsys.hls_to_rgb(hue, 0.34, 0.55)  # deep, saturated
    accent = colorsys.hls_to_rgb((hue + 0.083) % 1.0, 0.62, 0.5)  # lighter, shifted
    to8 = lambda c: tuple(round(x * 255) for x in c)  # noqa: E731
    return to8(primary), to8(accent)


def _resolve_colors(city: City) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    rgb = [_hex_to_rgb(c) for c in city.colors[:2]]
    if len(rgb) == 2:
        return rgb[0], rgb[1]
    if len(rgb) == 1:
        return rgb[0], _derived_colors(city.slug)[1]
    return _derived_colors(city.podcast_author or city.slug)


def _contrast(bg: tuple[int, int, int]) -> tuple[int, int, int]:
    # Relative luminance -> black or white text for legibility.
    lum = (0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]) / 255
    return (20, 20, 20) if lum > 0.6 else (255, 255, 255)


def _wrap(draw, text, font, max_width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=font) <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def render_cover(city: City, dest: Path, wordmark: str = "") -> None:
    primary, accent = _resolve_colors(city)
    fg = _contrast(primary)
    img = Image.new("RGB", (SIZE, SIZE), primary)
    draw = ImageDraw.Draw(img)

    # Accent band across the bottom.
    band_h = 150
    draw.rectangle([0, SIZE - band_h, SIZE, SIZE], fill=accent)

    top_label = (city.podcast_author or "").upper()
    main = city.source.get("body") or city.podcast_title
    max_w = SIZE - 2 * MARGIN

    # Top label (smaller).
    draw.text((MARGIN, MARGIN), top_label, font=_font(False, 50), fill=fg)

    # Main title (bold, wrapped, large — shrink to fit a few lines).
    for size in (150, 130, 110, 92, 78):
        title_font = _font(True, size)
        lines = _wrap(draw, main, title_font, max_w)
        line_h = int(size * 1.12)
        if len(lines) * line_h <= SIZE - band_h - MARGIN - 230:
            break
    y = MARGIN + 110
    for line in lines:
        draw.text((MARGIN, y), line, font=title_font, fill=fg)
        y += line_h

    # Wordmark in the accent band (the deployment's domain/brand; config-driven).
    if wordmark:
        wm_font = _font(True, 46)
        wm_y = SIZE - band_h + (band_h - 46) // 2 - 6
        draw.text((MARGIN, wm_y), wordmark, font=wm_font, fill=_contrast(accent))

    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, "JPEG", quality=88)
