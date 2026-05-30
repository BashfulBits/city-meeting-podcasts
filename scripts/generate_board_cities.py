#!/usr/bin/env python3
"""Generate one per-board city YAML per meeting body of an existing "base" city.

Reads a template city (for provider/source/metadata), discovers its bodies, and writes a
``cities/<base-slug>-<body>.yml`` for each body that:
  - has >= site_config.defaults.min_meetings_per_body meetings in the recency window, and
  - met within --recency-months (default 12), and
  - is NOT matched by the city's body_exclude denylist.

Variants are merged by stripping a trailing " - subtype" / ": subtype" (e.g. the three
"Planning and Zoning Commission - ..." and "Board of Adjustments: Panel A/B/C" collapse).

Example:
    python scripts/generate_board_cities.py denton-tx --base-slug denton-tx \\
        --title-prefix "Denton" --write
"""

from __future__ import annotations

import argparse
import collections
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from citypods.bodies import canonical_body as canonical
from citypods.bodies import is_excluded
from citypods.config import load_city_configs, load_site_config
from citypods.media import _source_key
from citypods.providers import get_provider

ROOT = Path(__file__).resolve().parent.parent


def slugify(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("template_city", help="existing city slug to use as source/metadata template")
    ap.add_argument("--base-slug", required=True, help="slug prefix for generated feeds")
    ap.add_argument("--title-prefix", required=True, help="podcast title prefix, e.g. 'Denton'")
    ap.add_argument("--recency-months", type=int, default=12)
    ap.add_argument("--site-config", default=str(ROOT / "site_config.yml"))
    ap.add_argument("--cities-dir", default=str(ROOT / "cities"))
    ap.add_argument("--write", action="store_true", help="write files (default: dry run)")
    args = ap.parse_args(argv)

    site_config = load_site_config(args.site_config)
    defaults = site_config.get("defaults", {})
    min_meetings = int(defaults.get("min_meetings_per_body", 3))
    cities = load_city_configs(args.cities_dir, defaults)
    tmpl = next((c for c in cities if c.slug == args.template_city), None)
    if tmpl is None:
        print(f"no city with slug {args.template_city!r}")
        return 1

    episodes = get_provider(tmpl.provider).fetch_episodes(tmpl.source)
    cutoff = datetime.now(UTC) - timedelta(days=30 * args.recency_months)
    # Count only meetings within the recency window, so "active" bodies are selected
    # (sources like Swagit return the entire historical archive).
    counts: collections.Counter[str] = collections.Counter()
    for e in episodes:
        if e.body and e.published >= cutoff:
            counts[canonical(e.body)] += 1

    # Skip bodies an existing feed already covers (same source + body), so curated/single-body
    # feeds (e.g. an existing council feed) are preserved and generation is idempotent.
    covered = {
        (_source_key(c), canonical(c.source["body"])) for c in cities if c.source.get("body")
    }
    selected = sorted(
        b
        for b, n in counts.items()
        if n >= min_meetings
        and not is_excluded(b, tmpl.body_exclude)
        and (_source_key(tmpl), b) not in covered
    )
    print(
        f"{args.template_city}: {len(selected)} feeds (>= {min_meetings} meetings, "
        f"last {args.recency_months}mo, denylist {tmpl.body_exclude or '(none)'}):"
    )
    cities_dir = Path(args.cities_dir)
    for body in selected:
        slug = f"{args.base_slug}-{slugify(body)}"
        path = cities_dir / f"{slug}.yml"
        action = "WRITE" if args.write else "plan"
        print(f"  {action}: {path.name}  (body={body!r}, {counts[body]} mtgs)")
        if args.write:
            path.write_text(_render(tmpl, slug, body, args.title_prefix))
    return 0


def _render(tmpl, slug: str, body: str, title_prefix: str) -> str:
    src = {k: v for k, v in tmpl.source.items() if k != "body"}
    src_lines = "\n".join(f"  {k}: {v}" for k, v in src.items())
    return (
        f"slug: {slug}\n"
        f"provider: {tmpl.provider}\n"
        f"source:\n{src_lines}\n"
        f'  body: "{body}"\n'
        f'podcast_title: "{title_prefix}: {body}"\n'
        f'podcast_author: "{tmpl.podcast_author}"\n'
        f'podcast_email: "{tmpl.podcast_email}"\n'
        f'podcast_description: "{body} meetings for {title_prefix}."\n'
        f"state: {tmpl.state}\n"
        + (f"city_website: {tmpl.city_website}\n" if tmpl.city_website else "")
        + (f"max_episodes: {tmpl.max_episodes}\n" if tmpl.max_episodes != 50 else "")
        + (f"extract_audio: {str(tmpl.extract_audio).lower()}\n" if tmpl.extract_audio else "")
    )


if __name__ == "__main__":
    raise SystemExit(main())
