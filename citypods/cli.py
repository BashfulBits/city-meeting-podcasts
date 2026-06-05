"""Command-line entry point: ``citypods build`` / ``citypods bodies`` / ``citypods rebuild-audio``."""  # noqa: E501

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
    b.add_argument("--site-config", default="config/site_config.yml")
    b.add_argument("--config-dir", default="config")
    b.add_argument("--output-dir", default="docs")
    b.add_argument("--base-url", help="override Pages base URL")
    b.add_argument(
        "--phase",
        choices=["all", "render"],
        default="all",
        help="'all' (default) runs every stage and renders — a one-shot build for local/preview. "
        "'render' is the fast production phase: cheap stages + render docs/ (deploy this, then "
        "run `citypods enrich`). Heavy audio/chapters already in the store still render.",
    )
    b.add_argument(
        "--chapters-cap",
        type=int,
        default=None,
        help="max chapter pages scraped per source this run (for the PR preview; "
        "unset = bounded only by the wall-clock window, as in production)",
    )

    e = sub.add_parser(
        "enrich",
        help="heavy backfill (chapters + audio) into object storage; no render/deploy. "
        "Run AFTER deploying `build --phase render`; output appears in the next render.",
    )
    e.add_argument("--city", help="enrich only this city slug")
    e.add_argument("--site-config", default="config/site_config.yml")
    e.add_argument("--config-dir", default="config")
    e.add_argument("--output-dir", default="docs")
    e.add_argument("--base-url", help="override Pages base URL")
    e.add_argument(
        "--chapters-cap",
        type=int,
        default=None,
        help="max chapter pages scraped per source this run (unset = bounded only by the "
        "wall-clock window, as in production)",
    )

    d = sub.add_parser("bodies", help="list the meeting bodies in a city's source")
    d.add_argument("slug", help="city slug to inspect")
    d.add_argument("--site-config", default="config/site_config.yml")
    d.add_argument("--config-dir", default="config")

    h = sub.add_parser("doctor", help="run feed-health checks (no GitHub side effects)")
    h.add_argument("--city", help="check only this city slug")
    h.add_argument("--enclosures", action="store_true", help="also HEAD-probe enclosures (slow)")
    h.add_argument("--site-config", default="config/site_config.yml")
    h.add_argument("--config-dir", default="config")

    r = sub.add_parser("report", help="resource cost/time projection report + admin page")
    r.add_argument("--markdown", action="store_true", help="print a Markdown summary to stdout")
    r.add_argument("--site-config", default="config/site_config.yml")
    r.add_argument("--config-dir", default="config")
    r.add_argument("--output-dir", default="docs")

    ra = sub.add_parser(
        "rebuild-audio",
        help="stamp the audio_rebuild nonce on a predicate-selected set of episodes "
        "(case 2: bug in the encode path, fixed in code) or drop corrupt audio objects "
        "(case 1: bad bytes, recipe was right). See design doc §4 for the three blast radii.",
    )
    ra.add_argument("--site-config", default="config/site_config.yml")
    ra.add_argument("--config-dir", default="config")
    ra.add_argument("--output-dir", default="docs")
    ra.add_argument("--base-url", help="base URL (needed for cloud storage drop)")
    ra.add_argument(
        "--encoded-after",
        metavar="ISO_DATE",
        help="select episodes whose audio was encoded on or after this date (inclusive)",
    )
    ra.add_argument(
        "--encoded-before",
        metavar="ISO_DATE",
        help="select episodes whose audio was encoded on or before this date (inclusive)",
    )
    ra.add_argument("--source", metavar="KEY", help="select episodes from this source key")
    ra.add_argument("--body", metavar="NAME", help="select episodes matching this body name")
    ra.add_argument(
        "--uid",
        action="append",
        metavar="UID",
        dest="uids",
        help="select this episode uid (repeatable)",
    )
    ra.add_argument(
        "--reason",
        metavar="TOKEN",
        help="nonce token mixed into audio_spec_hash (required unless --drop-object)",
    )
    ra.add_argument(
        "--drop-object",
        action="store_true",
        help="case 1: delete the audio object from storage and clear the record pointer "
        "so the next build re-encodes the same key. Mutually exclusive with --reason.",
    )
    ra.add_argument(
        "--all",
        action="store_true",
        help="explicitly stamp EVERY episode (required to proceed with --reason when no "
        "--uid/--source/--body/--encoded-* selector is given). Same blast radius as an "
        "AUDIO_PIPELINE_VERSION bump — use deliberately.",
    )
    ra.add_argument(
        "--dry-run", action="store_true", help="print what would be changed; write nothing"
    )

    ab = sub.add_parser(
        "asr-bench",
        help="dev diagnostic: measure ASR model WER and speed on a known episode "
        "(requires [asr] extras and a hosted episode with a stored source transcript).",
    )
    ab.add_argument("--city", required=True, metavar="SLUG", help="city feed slug")
    ab.add_argument("--uid", required=True, metavar="UID", help="episode uid to benchmark")
    ab.add_argument(
        "--models",
        default="base.en,small.en,large-v3-turbo",
        metavar="M1,M2,...",
        help="comma-separated faster-whisper model names to compare "
        "(default: base.en,small.en,large-v3-turbo)",
    )
    ab.add_argument(
        "--cpu-threads", type=int, default=4, metavar="N", help="CPU threads per model (default: 4)"
    )
    ab.add_argument("--site-config", default="config/site_config.yml")
    ab.add_argument("--config-dir", default="config")
    ab.add_argument("--output-dir", default="docs")

    args = parser.parse_args(argv)

    if args.command == "bodies":
        return _bodies(args)

    if args.command == "doctor":
        return _doctor(args)

    if args.command == "report":
        return _report(args)

    if args.command == "build":
        return _run_build(args, phase=args.phase, dry_run=args.dry_run)

    if args.command == "enrich":
        return _run_build(args, phase="enrich", dry_run=False)

    if args.command == "rebuild-audio":
        return _rebuild_audio(args)

    if args.command == "asr-bench":
        return _asr_bench(args)

    return 0


def _run_build(args, *, phase: str, dry_run: bool) -> int:
    results = build(
        site_config_path=args.site_config,
        config_dir=args.config_dir,
        output_dir=args.output_dir,
        base_url=args.base_url,
        only_slug=args.city,
        dry_run=dry_run,
        chapters_cap=args.chapters_cap,
        phase=phase,
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
    suffix = " (dry run)" if dry_run else ""
    print(f"\n{built} built, {skipped} skipped, {len(errors)} errors{suffix}")
    return 1 if errors else 0


def _report(args) -> int:
    from pathlib import Path

    from citypods.report import (
        build_report,
        build_status,
        to_admin_html,
        to_markdown,
        to_status_html,
    )
    from citypods.state import resolve_state_dir

    site_config = load_site_config(args.site_config)
    cities = load_city_configs(args.config_dir, site_config.get("defaults", {}))
    output_dir = Path(args.output_dir)
    state_dir = resolve_state_dir(site_config, output_dir)

    report = build_report(cities, site_config=site_config, state_dir=state_dir)

    admin = output_dir / "admin"
    admin.mkdir(parents=True, exist_ok=True)
    (admin / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    (admin / "index.html").write_text(to_admin_html(report))

    status = build_status(cities, site_config=site_config, state_dir=state_dir)
    admin_status = admin / "status"
    admin_status.mkdir(parents=True, exist_ok=True)
    (admin_status / "index.html").write_text(to_status_html(status))
    (admin_status / "status.json").write_text(json.dumps(status, indent=2) + "\n")

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


def _asr_bench(args) -> int:
    from citypods.bench import run_bench

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    return run_bench(
        city_slug=args.city,
        episode_uid=args.uid,
        models=models,
        site_config_path=args.site_config,
        config_dir=args.config_dir,
        output_dir=args.output_dir,
        cpu_threads=args.cpu_threads,
    )


def _bodies(args) -> int:
    site_config = load_site_config(args.site_config)
    cities = load_city_configs(args.config_dir, site_config.get("defaults", {}))
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
    cities = load_city_configs(args.config_dir, site_config.get("defaults", {}))
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


def _rebuild_audio(args) -> int:
    """Stamp the audio_rebuild nonce on a predicate-selected set (case 2: encode-path bug)
    or drop corrupt audio objects (case 1: bad bytes, correct recipe).

    The three blast radii (design doc §4):
      1. --drop-object: delete the object + clear the audio pointer → same key re-encodes.
      2. --reason TOKEN: stamp the nonce → new spec hash → new key → stamped-only re-encodes.
      3. AUDIO_PIPELINE_VERSION bump (not this command): re-encodes the whole catalog.
    """
    from datetime import UTC, datetime
    from pathlib import Path

    from citypods.records import load_records, save_records, source_key
    from citypods.state import resolve_state_dir
    from citypods.storage import make_storage

    drop = args.drop_object
    reason = args.reason or ""

    if not drop and not reason:
        print("error: --reason TOKEN is required (or use --drop-object for case 1)")
        return 1
    if drop and reason:
        print("error: --drop-object and --reason are mutually exclusive")
        return 1

    site_config = load_site_config(args.site_config)
    cities = load_city_configs(args.config_dir, site_config.get("defaults", {}))
    output_dir = Path(args.output_dir)
    state_dir = resolve_state_dir(site_config, output_dir)

    storage = None
    if drop:
        base_url = getattr(args, "base_url", None) or site_config.get("base_url", "")
        storage = make_storage(site_config, base_url, output_dir)

    # Parse date bounds (inclusive, either optional → open-ended)
    after: datetime | None = None
    before: datetime | None = None
    try:
        if args.encoded_after:
            after = datetime.fromisoformat(args.encoded_after).replace(tzinfo=UTC)
        if args.encoded_before:
            before = datetime.fromisoformat(args.encoded_before).replace(tzinfo=UTC)
    except ValueError as exc:
        print(f"error: invalid date: {exc}")
        return 1

    target_uids: set[str] = set(args.uids or [])
    target_source = args.source
    target_body = args.body

    # Guard the foot-gun: stamping the nonce with no selector would queue the ENTIRE catalog
    # for re-encode — the blunt blast radius the nonce is meant to be the scalpel against.
    # Require an explicit --all to do that on purpose. (Dropping objects is always scoped by a
    # selector in practice, but an unscoped --drop-object is harmless: it only clears pointers
    # for objects that exist, and is itself gated by the matched set.)
    has_selector = bool(target_uids or target_source or target_body or after or before)
    if not drop and not has_selector and not args.all:
        print(
            "error: --reason with no selector would stamp every episode for re-encode. "
            "Pass a selector (--uid/--source/--body/--encoded-after/--encoded-before), "
            "or --all to deliberately rebuild the whole catalog."
        )
        return 1

    total_matched = 0
    total_sources = 0

    for city in cities:
        key = source_key(city)
        if target_source and key != target_source:
            continue
        records = load_records(state_dir, key)
        if not records:
            continue

        changed: dict[str, dict] = {}
        for uid, rec in records.items():
            # uid filter
            if target_uids and uid not in target_uids:
                continue
            # body filter
            if target_body and (rec.get("body") or "").lower() != target_body.lower():
                continue
            # date range filter (records without encode_time are excluded from date-range selects)
            audio = rec.get("audio") or {}
            encode_time_str = audio.get("encode_time")
            if after or before:
                if not encode_time_str:
                    continue  # unknown encode time — exclude conservatively
                try:
                    encode_time = datetime.fromisoformat(encode_time_str).replace(tzinfo=UTC)
                except ValueError:
                    continue
                if after and encode_time < after:
                    continue
                if before and encode_time > before:
                    continue

            total_matched += 1
            rec = dict(rec)  # shallow copy before mutation

            if drop:
                obj_key = audio.get("key")
                if obj_key and storage and hasattr(storage, "delete"):
                    if not args.dry_run:
                        try:
                            storage.delete(obj_key)
                        except Exception as exc:  # noqa: BLE001
                            print(f"  warn: could not delete {obj_key}: {exc}")
                    else:
                        print(f"  [dry-run] would delete object {obj_key}")
                # Clear the audio pointer so next build re-encodes the same key.
                rec["audio"] = {
                    k: v
                    for k, v in audio.items()
                    if k not in ("key", "url", "spec_hash", "encode_time", "duration_served")
                }
                rec["audio"]["attempts"] = 0
                rec["audio"]["last_attempt"] = None
                rec["audio"]["error"] = None
            else:
                # Stamp the nonce: changes audio_spec_hash → new object key → re-encode.
                new_audio = dict(audio)
                new_audio["rebuild"] = reason
                rec["audio"] = new_audio

            changed[uid] = rec

        if changed:
            total_sources += 1
            merged = {**records, **changed}
            if not args.dry_run:
                save_records(state_dir, key, merged)
            action = "drop-object" if drop else f"nonce={reason!r}"
            print(
                f"  {'[dry-run] ' if args.dry_run else ''}"
                f"{city.slug} ({key}): {len(changed)} episode(s) [{action}]"
            )

    suffix = " (dry run)" if args.dry_run else ""
    print(f"\n{total_matched} episode(s) across {total_sources} source(s) updated{suffix}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
