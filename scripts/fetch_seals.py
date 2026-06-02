#!/usr/bin/env python3
"""One-time (re-runnable) fetch of city seals + brand colors from Wikipedia/Commons.

For each distinct city (grouped by ``podcast_author``):
  1. find a seal image (the city's Wikipedia article images, else a Commons "Seal of …"),
  2. download a PNG render to ``citypods/assets/seals/<author-key>.png`` (committed),
  3. extract two brand colors (from the seal, else the city website favicon),
  4. write ``colors: [...]`` into that city's entity file ``config/cities/<entity>.yml``.

The deploy build then composites the committed seal + uses the committed colors — no
Wikipedia calls or extra system deps at build time. Pure requests + Pillow.

    PYTHONPATH=. python scripts/fetch_seals.py [--force]
"""

from __future__ import annotations

import argparse
import io
import re
from pathlib import Path

from PIL import Image

from citypods.artwork import SEAL_DIR, author_key, dominant_colors
from citypods.config import load_city_configs, load_site_config
from citypods.http import DEFAULT_TIMEOUT, make_session

ROOT = Path(__file__).resolve().parent.parent
COMMONS = "https://commons.wikimedia.org/w/api.php"
WIKIPEDIA = "https://en.wikipedia.org/w/api.php"
STATES = {"TX": "Texas"}


def parse_author(author: str) -> tuple[str, str]:
    a = author.removeprefix("City of ").strip()
    if "," in a:
        name, st = a.rsplit(",", 1)
        return name.strip(), STATES.get(st.strip(), st.strip())
    return a, ""


def _seal_title(session, name, state) -> str | None:
    # 1. images on the city's own Wikipedia article
    for art in (f"{name}, {state}".strip(", "), name):
        r = session.get(
            WIKIPEDIA,
            params={
                "action": "query",
                "titles": art,
                "prop": "images",
                "imlimit": "500",
                "redirects": "1",
                "format": "json",
            },
            timeout=DEFAULT_TIMEOUT,
        ).json()
        for p in r.get("query", {}).get("pages", {}).values():
            for img in p.get("images", []):
                t = img["title"]
                if "seal" in t.lower() and "flag" not in t.lower():
                    return t
    # 2. Commons exact-title guesses
    for title in (
        f"File:Seal of {name}, {state}.svg",
        f"File:Seal of {name}.svg",
        f"File:Seal of {name}, {state}.png",
    ):
        r = session.get(
            COMMONS,
            params={
                "action": "query",
                "titles": title,
                "prop": "imageinfo",
                "iiprop": "url",
                "format": "json",
            },
            timeout=DEFAULT_TIMEOUT,
        ).json()
        pages = r.get("query", {}).get("pages", {})
        if pages and "-1" not in pages:
            return title
    # 3. broadened Commons file search, filtered to plausible municipal seals
    r = session.get(
        COMMONS,
        params={
            "action": "query",
            "generator": "search",
            "gsrsearch": f'"{name}" seal',
            "gsrnamespace": "6",
            "gsrlimit": "20",
            "format": "json",
        },
        timeout=DEFAULT_TIMEOUT,
    ).json()
    deny = (
        "school",
        "university",
        "academy",
        "company",
        " co.",
        "press",
        "lock",
        "crown",
        "cork",
        "marker",
        "railway",
        "railroad",
    )
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())  # noqa: E731
    name_n = norm(name)
    best, best_score = None, 0
    for p in r.get("query", {}).get("pages", {}).values():
        t = p.get("title", "")
        tl = t.lower()
        if "seal" not in tl or name_n not in norm(t) or not tl.endswith((".svg", ".png", ".jpg")):
            continue
        if any(d in tl for d in deny):
            continue
        # State guard: must clearly be the right state (avoids e.g. Arlington County, VA).
        if state and state.lower() not in tl and not re.search(r"(?<![a-z])tx(?![a-z])", tl):
            continue
        score = 2 if f"seal of {name.lower()}" in tl else 1
        if score > best_score:
            best, best_score = t, score
    return best


def _download_seal(session, title) -> Image.Image | None:
    # Query en.wikipedia (federates both local and Commons-hosted files).
    r = session.get(
        WIKIPEDIA,
        params={
            "action": "query",
            "titles": title,
            "prop": "imageinfo",
            "iiprop": "url",
            "iiurlwidth": "1000",
            "format": "json",
        },
        timeout=DEFAULT_TIMEOUT,
    ).json()
    page = next(iter(r.get("query", {}).get("pages", {}).values()), {})
    ii = (page.get("imageinfo") or [{}])[0]
    url = ii.get("thumburl") or ii.get("url")
    if not url:
        return None
    data = session.get(url, timeout=DEFAULT_TIMEOUT).content
    return Image.open(io.BytesIO(data))


def _favicon_colors(session, website) -> list[str]:
    if not website:
        return []
    domain = re.sub(r"^https?://", "", website).split("/")[0]
    try:
        data = session.get(
            f"https://www.google.com/s2/favicons?domain={domain}&sz=128",
            timeout=DEFAULT_TIMEOUT,
        ).content
        return dominant_colors(Image.open(io.BytesIO(data)))
    except Exception:
        return []


def _write_entity_colors(entity_slug: str, colors: list[str]) -> None:
    """Write ``colors:`` into the entity YAML (config/cities/<entity_slug>.yml), where the
    field now lives — shared across every feed for that city."""
    path = ROOT / "config" / "cities" / f"{entity_slug}.yml"
    text = path.read_text()
    line = f"colors: [{', '.join(repr(c) for c in colors)}]".replace("'", '"')
    # Replace an existing inline or block-list ``colors:`` (handles `colors:\n- "#abc"`).
    if re.search(r"^colors:\n(- .*\n)+", text, re.MULTILINE):
        text = re.sub(r"^colors:\n(- .*\n)+", line + "\n", text, flags=re.MULTILINE)
    elif re.search(r"^colors:.*$", text, re.MULTILINE):
        text = re.sub(r"^colors:.*$", line, text, flags=re.MULTILINE)
    else:
        text = text.rstrip("\n") + "\n" + line + "\n"
    path.write_text(text)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="refetch even if seal/colors exist")
    args = ap.parse_args(argv)

    sc = load_site_config(ROOT / "config" / "site_config.yml")
    cities = load_city_configs(ROOT / "config", sc.get("defaults", {}))
    by_author: dict[str, list] = {}
    for c in cities:
        by_author.setdefault(c.podcast_author or c.slug, []).append(c)

    SEAL_DIR.mkdir(parents=True, exist_ok=True)
    with make_session() as session:
        for author, group in by_author.items():
            key = author_key(group[0])
            seal_file = SEAL_DIR / f"{key}.png"
            name, state = parse_author(author)
            colors: list[str] = []

            if args.force or not seal_file.exists():
                title = _seal_title(session, name, state)
                if title:
                    seal = _download_seal(session, title)
                    if seal:
                        seal.save(seal_file, "PNG")
                        print(f"  seal: {author} -> {title}")
            if seal_file.exists():
                colors = dominant_colors(Image.open(seal_file))
            if not colors:
                colors = _favicon_colors(session, group[0].city_website)
            if not colors:
                print(f"  (no seal/colors for {author})")
                continue
            print(f"  colors: {author} -> {colors}")
            # Colors live on the entity now, so write once per city rather than per feed.
            entity_slug = group[0].city_entity
            if entity_slug:
                _write_entity_colors(entity_slug, colors)
            else:
                print(f"  (no city: entity for {author}; skipping colors write)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
