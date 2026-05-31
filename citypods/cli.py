"""Command-line entry point: ``citypods build`` / ``citypods bodies``."""

from __future__ import annotations

import argparse
import collections
import json
import sys

from citypods.bodies import is_excluded
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

    h = sub.add_parser("doctor", help="run feed-health checks (no GitHub side effects)")
    h.add_argument("--city", help="check only this city slug")
    h.add_argument("--enclosures", action="store_true", help="also HEAD-probe enclosures (slow)")
    h.add_argument("--site-config", default="site_config.yml")
    h.add_argument("--cities-dir", default="cities")

    r = sub.add_parser("report", help="resource cost/time projection report + admin page")
    r.add_argument("--markdown", action="store_true", help="print a Markdown summary to stdout")
    r.add_argument("--site-config", default="site_config.yml")
    r.add_argument("--cities-dir", default="cities")
    r.add_argument("--output-dir", default="docs")

    args = parser.parse_args(argv)

    if args.command == "bodies":
        return _bodies(args)

    if args.command == "doctor":
        return _doctor(args)

    if args.command == "report":
        return _report(args)

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


def _report(args) -> int:
    from pathlib import Path

    from citypods.report import build_report, to_admin_html, to_markdown
    from citypods.state import resolve_state_dir

    site_config = load_site_config(args.site_config)
    cities = load_city_configs(args.cities_dir, site_config.get("defaults", {}))
    output_dir = Path(args.output_dir)
    state_dir = resolve_state_dir(site_config, output_dir)

    report = build_report(cities, site_config=site_config, state_dir=state_dir)

    admin = output_dir / "admin"
    admin.mkdir(parents=True, exist_ok=True)
    (admin / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    (admin / "index.html").write_text(to_admin_html(report))

    md = to_markdown(report)
    if args.markdown:
        print(md)
    else:
        c = report["current"]
        print(
            f"{report['generated_for_feeds']} feeds · {c['storage_gb']:.1f} GB "
            f"(${c['monthly_cost_usd']:.2f}/mo) · {c['per_run_throughput']}/run · "
            f"backfill {c['full_backfill_days']:.0f}d → wrote {admin}/index.html + report.json"
        )
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

    print(f"{len(episodes)} meetings across {len(counts)} bodies in {args.slug}:")
    print(f"(denylist: {city.body_exclude or '(none)'} — ✗ = excluded from feeds)\n")
    print(f"  {'':1} {'count':>5}  {'latest':<12}  body")
    for body, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        mark = "✗" if is_excluded(body, city.body_exclude) else "✓"
        print(f"  {mark} {n:>5}  {latest[body].date()!s:<12}  {body}")
    return 0


def _doctor(args) -> int:
    from citypods.audit import audit_all

    site_config = load_site_config(args.site_config)
    cities = load_city_configs(args.cities_dir, site_config.get("defaults", {}))
    if args.city:
        cities = [c for c in cities if c.slug == args.city]
        if not cities:
            print(f"no city with slug {args.city!r}")
            return 1

    findings = audit_all(cities, site_config=site_config, check_enclosures_net=args.enclosures)
    if not findings:
        print(f"✓ no issues across {len(cities)} feed(s)")
        return 0

    icon = {"error": "✗", "warn": "⚠"}
    for f in sorted(findings, key=lambda f: (f.severity != "error", f.slug, f.check)):
        print(f"  {icon.get(f.severity, '?')} {f.slug} [{f.check}] {f.message}")
    errors = sum(f.severity == "error" for f in findings)
    print(f"\n{len(findings)} finding(s): {errors} error(s), {len(findings) - errors} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
