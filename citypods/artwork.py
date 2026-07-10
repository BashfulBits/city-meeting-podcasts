"""Generate 1400x1400 podcast cover art per feed.

If a committed city seal exists (``assets/seals/<author-key>.png``, fetched once by
``scripts/fetch_seals.py``), it's composited on a clean card with a colored band naming
the city + board (so each feed differs). Otherwise a colored typographic card is used.
Both use the city's two brand colors (``City.colors``), or a pleasant color derived from
the name — never stark black/white.
"""

from __future__ import annotations

import colorsys
import hashlib
import re
from functools import cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from citypods.models import City

SIZE = 1400
MARGIN = 110
FONT_DIR = Path(__file__).resolve().parent / "assets" / "fonts"
SEAL_DIR = Path(__file__).resolve().parent / "assets" / "seals"


def author_key(city: City) -> str:
    """Stable per-city key (seals are shared by all of a city's feeds)."""
    slug = re.sub(r"[^a-z0-9]+", "-", (city.podcast_author or "").lower())
    return re.sub(r"-+", "-", slug).strip("-")


def seal_path(city: City) -> Path | None:
    p = SEAL_DIR / f"{author_key(city)}.png"
    return p if p.exists() else None


@cache
def _font(bold: bool, size: int) -> ImageFont.FreeTypeFont:
    # CR2-CP-16: _draw_band's text-fitting loop re-requests the same (bold, size) combination
    # across renders; cache the loaded TTF instead of re-reading it from disk every call.
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


def _draw_band(draw, primary, accent, city, wordmark, band_h):
    """Bottom band: city label + board name + wordmark, naming this specific feed."""
    fg = _contrast(primary)
    top = SIZE - band_h
    draw.rectangle([0, top, SIZE, SIZE], fill=primary)
    draw.rectangle([0, top, SIZE, top + 10], fill=accent)  # accent hairline
    y = top + 40
    draw.text((MARGIN, y), (city.podcast_author or "").upper(), font=_font(False, 44), fill=fg)
    y += 64
    main = city.source.get("body") or city.podcast_title
    for size in (84, 72, 60, 50):
        f = _font(True, size)
        lines = _wrap(draw, main, f, SIZE - 2 * MARGIN)
        if len(lines) * int(size * 1.1) <= band_h - 150:
            break
    for line in lines:
        draw.text((MARGIN, y), line, font=f, fill=fg)
        y += int(size * 1.1)
    if wordmark:
        draw.text((MARGIN, SIZE - 64), wordmark, font=_font(True, 38), fill=fg)


def _save(img: Image.Image, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, "JPEG", quality=88)


def _render_with_seal(city, dest, wordmark, seal_file):
    primary, accent = _resolve_colors(city)
    img = Image.new("RGB", (SIZE, SIZE), (255, 255, 255))
    seal = Image.open(seal_file).convert("RGBA")
    canvas = Image.new("RGBA", seal.size, (255, 255, 255, 255))
    seal = Image.alpha_composite(canvas, seal).convert("RGB")
    band_h = 430
    region = SIZE - band_h
    box = int(min(region, SIZE) * 0.74)
    seal.thumbnail((box, box))
    img.paste(seal, ((SIZE - seal.width) // 2, (region - seal.height) // 2))
    _draw_band(ImageDraw.Draw(img), primary, accent, city, wordmark, band_h)
    _save(img, dest)


def _render_typographic(city, dest, wordmark):
    primary, accent = _resolve_colors(city)
    fg = _contrast(primary)
    img = Image.new("RGB", (SIZE, SIZE), primary)
    draw = ImageDraw.Draw(img)
    band_h = 150
    draw.rectangle([0, SIZE - band_h, SIZE, SIZE], fill=accent)
    draw.text((MARGIN, MARGIN), (city.podcast_author or "").upper(), font=_font(False, 50), fill=fg)
    main = city.source.get("body") or city.podcast_title
    for size in (150, 130, 110, 92, 78):
        title_font = _font(True, size)
        lines = _wrap(draw, main, title_font, SIZE - 2 * MARGIN)
        line_h = int(size * 1.12)
        if len(lines) * line_h <= SIZE - band_h - MARGIN - 230:
            break
    y = MARGIN + 110
    for line in lines:
        draw.text((MARGIN, y), line, font=title_font, fill=fg)
        y += line_h
    if wordmark:
        wm_y = SIZE - band_h + (band_h - 46) // 2 - 6
        draw.text((MARGIN, wm_y), wordmark, font=_font(True, 46), fill=_contrast(accent))
    _save(img, dest)


def render_cover(city: City, dest: Path, wordmark: str = "") -> None:
    seal_file = seal_path(city)
    if seal_file:
        _render_with_seal(city, dest, wordmark, seal_file)
    else:
        _render_typographic(city, dest, wordmark)


def dominant_colors(image: Image.Image, n: int = 2) -> list[str]:
    """Extract up to ``n`` representative brand hex colors (skip near-white/black/gray)."""
    im = image.convert("RGBA")
    bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
    im = Image.alpha_composite(bg, im).convert("RGB").resize((120, 120))
    quant = im.quantize(colors=12).convert("RGB")
    picked: list[tuple[int, int, int]] = []
    for _count, (r, g, b) in sorted(quant.getcolors(120 * 120) or [], reverse=True):
        total, spread = r + g + b, max(r, g, b) - min(r, g, b)
        if total > 710 or total < 70 or spread < 28:  # skip white/black/gray
            continue
        # Require the next color to be visibly distinct from those already chosen.
        if any(abs(r - pr) + abs(g - pg) + abs(b - pb) < 120 for pr, pg, pb in picked):
            continue
        picked.append((r, g, b))
        if len(picked) >= n:
            break
    return [f"#{r:02x}{g:02x}{b:02x}" for r, g, b in picked]
