#!/usr/bin/env python3
"""Generate one per-board city YAML per meeting body of an existing "base" city.

Reads a template city (for provider/source/metadata), discovers its bodies, and writes a
``config/feeds/<base-slug>-<body>.yml`` for each body that:
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

from citypods.bodies import body_key, is_excluded
from citypods.bodies import canonical_body as canonical
from citypods.config import load_city_configs, load_site_config
from citypods.providers import get_provider
from citypods.records import source_key

ROOT = Path(__file__).resolve().parent.parent


def slugify(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("template_city", help="existing city slug to use as source/metadata template")
    ap.add_argument("--base-slug", required=True, help="slug prefix for generated feeds")
    ap.add_argument("--title-prefix", required=True, help="podcast title prefix, e.g. 'Denton'")
    ap.add_argument("--recency-months", type=int, default=12)
    ap.add_argument("--site-config", default=str(ROOT / "config" / "site_config.yml"))
    ap.add_argument("--config-dir", default=str(ROOT / "config"))
    ap.add_argument("--write", action="store_true", help="write files (default: dry run)")
    args = ap.parse_args(argv)

    site_config = load_site_config(args.site_config)
    defaults = site_config.get("defaults", {})
    min_meetings = int(defaults.get("min_meetings_per_body", 3))
    cities = load_city_configs(args.config_dir, defaults)
    tmpl = next((c for c in cities if c.slug == args.template_city), None)
    if tmpl is None:
        print(f"no city with slug {args.template_city!r}")
        return 1
    if args.recency_months <= 0:
        print(f"--recency-months must be positive, got {args.recency_months}")
        return 1

    episodes = get_provider(tmpl.provider).fetch_episodes(tmpl.source)
    cutoff = datetime.now(UTC) - timedelta(days=30 * args.recency_months)
    # Group by normalized body_key (merges spelling/case variants across views), counting
    # only meetings within the recency window; the display name is the most-common spelling.
    counts: collections.Counter[str] = collections.Counter()
    spellings: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for e in episodes:
        if e.body and e.published >= cutoff:
            disp = canonical(e.body)
            k = body_key(disp)
            counts[k] += 1
            spellings[k][disp] += 1

    # Skip bodies an existing feed already covers (same source + body key), so curated /
    # single-body feeds are preserved and generation is idempotent.
    covered = {(source_key(c), body_key(c.source["body"])) for c in cities if c.source.get("body")}
    selected_keys = sorted(
        k
        for k, n in counts.items()
        if n >= min_meetings
        and not is_excluded(k, tmpl.body_exclude)
        and (source_key(tmpl), k) not in covered
    )
    selected = sorted(spellings[k].most_common(1)[0][0] for k in selected_keys)
    print(
        f"{args.template_city}: {len(selected)} feeds (>= {min_meetings} meetings, "
        f"last {args.recency_months}mo, denylist {tmpl.body_exclude or '(none)'}):"
    )
    feeds_dir = Path(args.config_dir) / "feeds"
    if args.write:
        feeds_dir.mkdir(parents=True, exist_ok=True)
    for body in selected:
        slug = f"{args.base_slug}-{slugify(body)}"
        path = feeds_dir / f"{slug}.yml"
        if path.exists():
            print(f"  skip (exists): {path.name}  (body={body!r})")
            continue
        action = "WRITE" if args.write else "plan"
        print(f"  {action}: {path.name}  (body={body!r}, {counts[body_key(body)]} mtgs)")
        if args.write:
            path.write_text(_render(tmpl, slug, body, args.title_prefix))
    return 0


def _render(tmpl, slug: str, body: str, title_prefix: str) -> str:
    src = {k: v for k, v in tmpl.source.items() if k != "body"}
    src_lines = "\n".join(f"  {k}: {v}" for k, v in src.items())
    return (
        f"slug: {slug}\n"
        + (f"city: {tmpl.city_entity}\n" if tmpl.city_entity else "")
        + f"provider: {tmpl.provider}\n"
        f"source:\n{src_lines}\n"
        f'  body: "{body}"\n'
        f'podcast_title: "{title_prefix}: {body}"\n'
        f'podcast_author: "{tmpl.podcast_author}"\n'
        f'podcast_email: "{tmpl.podcast_email}"\n'
        f'podcast_description: "{body} meetings for {title_prefix}."\n'
        + (f"max_episodes: {tmpl.max_episodes}\n" if tmpl.max_episodes != 50 else "")
        + (f"extract_audio: {str(tmpl.extract_audio).lower()}\n" if tmpl.extract_audio else "")
    )


if __name__ == "__main__":
    raise SystemExit(main())
