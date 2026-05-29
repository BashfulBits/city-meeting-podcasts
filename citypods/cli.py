"""Command-line entry point: ``citypods build [--city SLUG] [--dry-run]``."""

from __future__ import annotations

import argparse
import sys

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

    args = parser.parse_args(argv)

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


if __name__ == "__main__":
    sys.exit(main())
