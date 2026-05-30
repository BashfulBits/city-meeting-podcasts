"""Command-line entry point: ``citypods build`` / ``citypods bodies``."""

from __future__ import annotations

import argparse
import collections
import sys

from citypods.config import load_city_configs, load_site_config
from citypods.providers import get_provider
from citypods.run import build


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="citypods", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="build feeds and pages into docs/")
    b.add_argument("--city", help="build only this city slug")
    b.add_argument("--dry-run", action="store_true", help="fetch but write nothing")
    b.add_argument("--site-config", default="site_config.yml")
    b.add_argument("--cities-dir", default="cities")
    b.add_argument("--output-dir", default="docs")
    b.add_argument("--base-url", help="override Pages base URL")

    d = sub.add_parser("bodies", help="list the meeting bodies in a city's source")
    d.add_argument("slug", help="city slug to inspect")
    d.add_argument("--site-config", default="site_config.yml")
    d.add_argument("--cities-dir", default="cities")

    args = parser.parse_args(argv)

    if args.command == "bodies":
        return _bodies(args)

    if args.command == "build":
        results = build(
            site_config_path=args.site_config,
            cities_dir=args.cities_dir,
            output_dir=args.output_dir,
            base_url=args.base_url,
            only_slug=args.city,
            dry_run=args.dry_run,
        )
        built = sum(r.status == "built" for r in results)
        skipped = sum(r.status == "skipped" for r in results)
        errors = [r for r in results if r.status == "error"]
        for r in results:
            mark = {"built": "✓", "skipped": "·", "error": "✗"}[r.status]
            extra = f" ({r.episode_count} eps)" if r.status == "built" else ""
            if r.status == "error":
                extra = f" — {r.detail}"
            print(f"  {mark} {r.slug}{extra}")
        suffix = " (dry run)" if args.dry_run else ""
        print(f"\n{built} built, {skipped} skipped, {len(errors)} errors{suffix}")
        return 1 if errors else 0

    return 0


def _bodies(args) -> int:
    site_config = load_site_config(args.site_config)
    cities = load_city_configs(args.cities_dir, site_config.get("defaults", {}))
    city = next((c for c in cities if c.slug == args.slug), None)
    if city is None:
        print(f"no city with slug {args.slug!r}")
        return 1

    episodes = get_provider(city.provider).fetch_episodes(city.source)
    counts: dict[str, int] = collections.Counter(e.body or "(unknown)" for e in episodes)
    latest: dict[str, object] = {}
    for e in episodes:
        key = e.body or "(unknown)"
        if key not in latest or e.published > latest[key]:
            latest[key] = e.published

    print(f"{len(episodes)} meetings across {len(counts)} bodies in {args.slug}:\n")
    print(f"  {'count':>5}  {'latest':<12}  body")
    for body, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {n:>5}  {latest[body].date()!s:<12}  {body}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
