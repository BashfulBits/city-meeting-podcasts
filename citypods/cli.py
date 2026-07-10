"""Command-line entry point: ``citypods build`` / ``citypods bodies`` / ``citypods rebuild-audio``."""  # noqa: E501

from __future__ import annotations

import argparse
import collections
import json
import sys

from citypods.bodies import is_excluded
from citypods.config import filter_city_configs, load_city_configs, load_site_config
from citypods.providers import get_provider
from citypods.run import build, install_signal_handlers, interrupt_requested


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="citypods", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="build feeds and pages into docs/")
    b.add_argument("--city", help="build only this city entity slug or feed slug")
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
    b.add_argument(
        "--no-refresh",
        action="store_true",
        help="render purely from the record store with NO provider connections (for the PR "
        "preview: verifies the build/render flow against the last-known state without depending "
        "on live provider availability). Only meaningful with --phase render.",
    )

    e = sub.add_parser(
        "enrich",
        help="heavy backfill (chapters + audio) into object storage; no render/deploy. "
        "Run AFTER deploying `build --phase render`; output appears in the next render.",
    )
    e.add_argument("--city", help="enrich only this city entity slug or feed slug")
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
    e.add_argument("--dry-run", action="store_true", help="enrich but write nothing (no uploads)")
    e.add_argument(
        "--source", metavar="KEY", help="enrich only this source_key (H6b shard scoping)"
    )
    e.add_argument(
        "--shard",
        metavar="K/N",
        help="enrich only the sources in shard K of N (0-based), partitioned by "
        "weighted source_key assignment — the H6b sharded audio.yml/asr.yml workflows",
    )
    e.add_argument(
        "--lane",
        choices=["audio", "transcribe", "align"],
        help="work class to run: 'audio' materializes audio only; 'transcribe' runs fresh ASR "
        "only; 'align' runs forced-alignment only. Default runs the full enrich (audio + "
        "transcript). The sharded workflows pin one lane so the two ASR models never co-load.",
    )
    e.add_argument(
        "--shard-plan",
        metavar="PATH",
        help="consume an immutable canonical shard-assignment JSON instead of recomputing "
        "ownership",
    )
    e.add_argument(
        "--state-snapshot-restored",
        action="store_true",
        help="the canonical planner snapshot is already present locally; skip the durable-state "
        "pull",
    )

    d = sub.add_parser("bodies", help="list the meeting bodies in a city's source")
    d.add_argument("slug", help="city slug to inspect")
    d.add_argument("--site-config", default="config/site_config.yml")
    d.add_argument("--config-dir", default="config")

    h = sub.add_parser("doctor", help="run feed-health checks (no GitHub side effects)")
    h.add_argument("--city", help="check only this city entity slug or feed slug")
    h.add_argument("--enclosures", action="store_true", help="also HEAD-probe enclosures (slow)")
    h.add_argument("--site-config", default="config/site_config.yml")
    h.add_argument("--config-dir", default="config")

    r = sub.add_parser("report", help="resource cost/time projection report + admin page")
    r.add_argument("--markdown", action="store_true", help="print a Markdown summary to stdout")
    r.add_argument("--site-config", default="config/site_config.yml")
    r.add_argument("--config-dir", default="config")
    r.add_argument("--output-dir", default="docs")

    h16 = sub.add_parser("h16-report", help="build the GH#353 Audio acceptance report")
    h16.add_argument("--input-dir", required=True, help="downloaded shard evidence directory")
    h16.add_argument("--output-dir", required=True, help="directory for JSON and Markdown reports")
    h16.add_argument("--run-id", required=True, help="GitHub Actions run ID to aggregate")
    h16.add_argument("--site-config", default="config/site_config.yml")
    h16.add_argument("--expected-shards", type=int, default=4)
    h16.add_argument("--markdown", action="store_true", help="also print Markdown to stdout")

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
        "(requires [asr-bench] extras and a hosted episode with a stored source transcript).",
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
    ab.add_argument(
        "--beam-size",
        type=int,
        default=5,
        metavar="N",
        help="Whisper beam-search width (default: 5; lower is faster, often less accurate)",
    )
    ab.add_argument("--site-config", default="config/site_config.yml")
    ab.add_argument("--config-dir", default="config")
    ab.add_argument("--output-dir", default="docs")

    vb = sub.add_parser(
        "validate-build",
        help="validate generated feeds before publishing; exits non-zero on fatal errors",
    )
    vb.add_argument(
        "output_dir",
        nargs="?",
        default="docs",
        help="generated docs directory to validate (default: docs)",
    )
    vb.add_argument(
        "--state-dir",
        default=".citypods-state",
        help="state directory used to detect known-empty feeds (default: .citypods-state)",
    )
    vb.add_argument("--site-config", default="config/site_config.yml")
    vb.add_argument("--config-dir", default="config")

    cp = sub.add_parser(
        "compute", help="external-dispatch maintenance (H14): reconcile leases + free-tier budget"
    )
    cp_sub = cp.add_subparsers(dest="compute_command", required=True)
    cr = cp_sub.add_parser(
        "reconcile",
        help="reap expired dispatch leases (dead worker → re-queue), settle completed jobs, and "
        "roll the monthly free-tier budget — run at asr.yml start",
    )
    cr.add_argument("--site-config", default="config/site_config.yml")
    cr.add_argument("--config-dir", default="config")
    cr.add_argument("--output-dir", default="docs")
    cr.add_argument("--base-url", help="base URL (for resolving cloud storage)")
    cr.add_argument(
        "--dry-run", action="store_true", help="report leased/expired counts; write nothing"
    )
    cps = cp_sub.add_parser(
        "plan-shards",
        help="create one source-atomic shard assignment from the currently restored state snapshot",
    )
    cps.add_argument("--lane", choices=["audio", "transcribe", "align"], required=True)
    cps.add_argument("--shards", type=int, required=True, metavar="N")
    cps.add_argument("--output", required=True, metavar="PATH")
    cps.add_argument("--site-config", default="config/site_config.yml")
    cps.add_argument("--config-dir", default="config")
    cps.add_argument("--output-dir", default="docs")
    crt = cp_sub.add_parser(
        "reclaim-transcript",
        help="re-adopt an ASR artifact already in storage whose record transcript block was "
        "lost (e.g. GH#833) -- never re-transcribes; dry-run unless --write is passed",
    )
    crt.add_argument("--source-key", required=True)
    crt.add_argument("--episode-uid", required=True)
    crt.add_argument("--write", action="store_true", help="actually push the reclaimed record")
    crt.add_argument("--site-config", default="config/site_config.yml")
    crt.add_argument("--config-dir", default="config")
    crt.add_argument("--output-dir", default="docs")
    crt.add_argument("--base-url", help="base URL (for resolving cloud storage)")
    ciw = cp_sub.add_parser(
        "run-internal-worker",
        help="run one pull/claim transcribe worker inside GitHub Actions until the stop budget",
    )
    ciw.add_argument("--site-config", default="config/site_config.yml")
    ciw.add_argument("--config-dir", default="config")
    ciw.add_argument("--output-dir", default="docs")
    ciw.add_argument("--base-url", help="base URL (for resolving cloud storage)")
    ciw.add_argument("--owner", help="override the worker owner identity")
    ciw.add_argument("--max-claims", type=int, metavar="N")
    ciw.add_argument("--max-scan", type=int, metavar="N")

    args = parser.parse_args(argv)

    if args.command == "bodies":
        return _bodies(args)

    if args.command == "doctor":
        return _doctor(args)

    if args.command == "report":
        return _report(args)

    if args.command == "h16-report":
        return _h16_report(args)

    if args.command == "build":
        return _run_build(args, phase=args.phase, dry_run=args.dry_run)

    if args.command == "enrich":
        return _run_build(args, phase="enrich", dry_run=args.dry_run)

    if args.command == "rebuild-audio":
        return _rebuild_audio(args)

    if args.command == "asr-bench":
        return _asr_bench(args)

    if args.command == "validate-build":
        return _validate_build(args)

    if args.command == "compute":
        if args.compute_command == "reconcile":
            return _compute_reconcile(args)
        if args.compute_command == "plan-shards":
            return _compute_plan_shards(args)
        if args.compute_command == "reclaim-transcript":
            return _compute_reclaim_transcript(args)
        if args.compute_command == "run-internal-worker":
            return _compute_run_internal_worker(args)

    return 0


def _parse_shard(spec: str | None) -> tuple[int, int] | None:
    """Parse a ``--shard K/N`` spec into ``(k, n)``; ``None`` for no sharding."""
    if not spec:
        return None
    try:
        k_str, n_str = spec.split("/", 1)
        k, n = int(k_str), int(n_str)
    except ValueError as exc:
        raise SystemExit(f"--shard must be K/N (e.g. 0/4), got {spec!r}") from exc
    if not (n >= 1 and 0 <= k < n):
        raise SystemExit(f"--shard {spec} out of range: need N>=1 and 0<=K<N")
    return (k, n)


def _run_build(args, *, phase: str, dry_run: bool) -> int:
    # GH#377: convert SIGTERM (GitHub cancel, lost-comms) into the existing graceful-stop path so
    # in-flight workers defer and the build still persists records + writes a run-history entry on
    # the way out, instead of the process dying mid-queue with everything since the last persist
    # lost. Installed only here at the CLI entry, never in importable library code.
    install_signal_handlers()
    results = build(
        site_config_path=args.site_config,
        config_dir=args.config_dir,
        output_dir=args.output_dir,
        base_url=args.base_url,
        only_slug=args.city,
        dry_run=dry_run,
        chapters_cap=args.chapters_cap,
        phase=phase,
        shard=_parse_shard(getattr(args, "shard", None)),
        source=getattr(args, "source", None),
        lane=getattr(args, "lane", None),
        no_refresh=getattr(args, "no_refresh", False),
        shard_plan_path=getattr(args, "shard_plan", None),
        state_snapshot_restored=getattr(args, "state_snapshot_restored", False),
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
    if errors:
        return 1
    # GH#377: a run cut short by SIGTERM persisted what it could, but it did NOT finish the backlog.
    # Surface it as the conventional 128+SIGTERM(15) code so `continue-on-error` / log readers don't
    # mistake a killed run for a clean success. A graceful wall-clock/superseded yield is *not* an
    # interrupt and still returns 0.
    if interrupt_requested():
        print("interrupted by signal — partial run persisted; exiting 143")
        return 143
    return 0


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


def _h16_report(args) -> int:
    from pathlib import Path

    from citypods.h16_report import build_h16_report, to_markdown, write_h16_report

    report = build_h16_report(
        Path(args.input_dir),
        run_id=args.run_id,
        site_config_path=Path(args.site_config),
        expected_shards=args.expected_shards,
    )
    write_h16_report(report, Path(args.output_dir))
    if args.markdown:
        print(to_markdown(report), end="")
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
        beam_size=args.beam_size,
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
        cities = filter_city_configs(cities, args.city)
        if not cities:
            print(f"no feed or city entity with slug {args.city!r}")
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

    # Guard the foot-gun: stamping the nonce, or dropping object pointers, with no selector
    # would hit the ENTIRE catalog — the blunt blast radius the nonce is meant to be the
    # scalpel against. Require an explicit --all to do that on purpose.
    has_selector = bool(target_uids or target_source or target_body or after or before)
    if not has_selector and not args.all:
        if drop:
            print(
                "error: --drop-object with no selector would clear every audio object pointer. "
                "Pass a selector (--uid/--source/--body/--encoded-after/--encoded-before), "
                "or --all to deliberately rebuild the whole catalog."
            )
            return 1
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


def _compute_reconcile(args) -> int:
    """Reap expired dispatch leases (dead worker → re-queue), settle completed jobs, and roll the
    monthly free-tier budget. Run at ``asr.yml`` start so a crashed worker's slot/budget is
    reclaimed before the run dispatches more work (H14a)."""
    from datetime import UTC, datetime
    from pathlib import Path

    from citypods.compute import reconcile_compute
    from citypods.compute.budget import load_budget, load_budget_cas, storage_supports_cas
    from citypods.ops.workqueue import (
        is_leased,
        load_manifest,
        rebuild_manifest_from_state,
        save_manifest,
    )
    from citypods.state import resolve_state_dir
    from citypods.statesync import pull_state, push_state
    from citypods.storage import make_storage

    site_config = load_site_config(args.site_config)
    output_dir = Path(args.output_dir)
    state_dir = resolve_state_dir(site_config, output_dir)
    base_url = getattr(args, "base_url", None) or site_config.get("base_url", "")
    storage = make_storage(site_config, base_url, output_dir)
    # The Stage-2 work-lease reaper stays dormant until external pull workers (H14b/H14c) claim
    # against the ledger; until then sweeping it is pointless backlog-scaled GETs (review/18 §4.2).
    # Lives under `defaults:` in site_config.yml (sibling to `compute_backend`), not the document
    # root — GH#706 §6(b) found this read at the root silently defaulted to False in production
    # for the life of the flag, so the sweep never actually ran despite the config saying `true`.
    sweep_work_leases = bool(
        (site_config.get("defaults") or {}).get("work_lease_reaper_enabled", False)
    )

    if args.dry_run:
        # Predict what a real run would reap WITHOUT touching durable or real local state. A real
        # reconcile pulls the durable snapshot first, so mirror it: pull into a throwaway dir and
        # read the manifest there; read the budget from R2 (CAS) when available, else from the same
        # pulled snapshot. Keeps the manifest and budget views consistent (both durable) instead of
        # mixing a durable budget with a possibly-stale local work.json.
        import tempfile

        from citypods.compute.dispatch import _asr_artifact_present
        from citypods.ops.work_leases import reap as reap_work_leases

        now = datetime.now(UTC)
        cas = storage_supports_cas(storage)
        lease_preview = {"completed": 0, "requeued": 0, "in_flight": 0}
        with tempfile.TemporaryDirectory() as td:
            snapshot = Path(td)
            pull_state(storage, snapshot)
            manifest = load_manifest(snapshot)
            leased = [wi for wi in manifest if wi.lease_owner]
            budget = load_budget_cas(storage)[0] if cas else load_budget(snapshot)
            if cas and sweep_work_leases:
                # Read-only preview of the Stage-2 work-lease sweep the real reconcile would do, so
                # the dry-run output matches it (not just legacy work.json leases). Gated by the
                # same flag, so the preview reflects whether the reaper is actually enabled.
                candidates = [
                    (wi.source_key, wi.episode_uid)
                    for wi in manifest
                    if wi.work_class == "transcript-asr" and wi.state != "done"
                ]
                lease_preview = reap_work_leases(
                    storage,
                    candidates,
                    artifact_present=lambda s, u: _asr_artifact_present(storage, s, u),
                    now=now,
                    dry_run=True,
                )
        expired = sum(1 for wi in leased if not is_leased(wi, now=now))
        reservations = sum(led.inflight_count for led in budget.backends.values())
        print(
            f"compute reconcile (dry run): {len(leased)} leased "
            f"({expired} expired → would reap), {reservations} budget reservation(s); "
            f"work-leases would: {lease_preview['requeued']} requeue, "
            f"{lease_preview['completed']} settle ({lease_preview['in_flight']} in-flight)"
        )
        return 0

    # Operate on the durable (bucket) state: pull the snapshot, reconcile, push back only the
    # files reconcile owns (so it never clobbers records). On a CAS backend the budget ledger is
    # written directly by reconcile via CAS and push_state skips it (CAS-managed); on a non-CAS
    # backend (plain B2 / local) it rides this bulk push as before. No-op for a sync-less backend.
    pull_state(storage, state_dir)
    cities = load_city_configs(args.config_dir, site_config.get("defaults", {}))
    save_manifest(
        state_dir,
        rebuild_manifest_from_state(cities, site_config=site_config, state_dir=state_dir),
    )
    summary = reconcile_compute(state_dir, storage, sweep_work_leases=sweep_work_leases)
    push_state(storage, state_dir, only_prefixes=["work.json", "compute_budget.json"])
    leases = summary.get("leases", {})
    lease_note = (
        f"; work-leases: {leases.get('requeued', 0)} requeued, "
        f"{leases.get('completed', 0)} settled, {leases.get('in_flight', 0)} in-flight"
        if leases
        else ""
    )
    print(
        f"compute reconcile: {summary['reaped']} reaped, {summary['settled']} settled, "
        f"{summary['in_flight']} in-flight{lease_note}"
    )
    return 0


def _compute_run_internal_worker(args) -> int:
    from citypods.compute.external_worker import run_internal_worker

    install_signal_handlers()
    summary = run_internal_worker(
        owner=args.owner,
        max_claims=args.max_claims,
        max_scan=args.max_scan,
        site_config_path=args.site_config,
        config_dir=args.config_dir,
        output_dir=args.output_dir,
        base_url=args.base_url,
    )
    print(json.dumps(summary, indent=2))
    if summary.get("failed"):
        return 1
    if interrupt_requested():
        print("interrupted by signal — partial run persisted; exiting 143")
        return 143
    return 0


def _compute_reclaim_transcript(args) -> int:
    """Re-adopt an ASR artifact already uploaded to storage whose record ``transcript`` block was
    lost — e.g. GH#833's owned-block-merge bug, where a better remote plan silently dropped a
    just-written transcript even though the VTT/words artifact had already landed. This never
    re-transcribes: it recomputes the SAME recipe hash the original worker used
    (``_asr_recipe_hash``, deterministic from the current city config + episode fields) and
    re-attaches the existing keys if they're present. Read-only unless ``--write`` is passed."""
    from pathlib import Path

    from citypods.records import (
        episode_to_record,
        load_records,
        protected_blocks_for_lane,
        record_to_episode,
        save_records,
        source_key,
    )
    from citypods.stages import (
        _adopt_asr_keys,
        _asr_object_key,
        _asr_recipe_hash,
        _asr_words_object_key,
    )
    from citypods.state import resolve_state_dir
    from citypods.statesync import pull_state, push_records_merged
    from citypods.storage import make_storage

    site_config = load_site_config(args.site_config)
    cities = load_city_configs(args.config_dir, site_config.get("defaults", {}))
    output_dir = Path(args.output_dir)
    base_url = getattr(args, "base_url", None) or site_config.get("base_url", "")
    storage = make_storage(site_config, base_url, output_dir)
    if storage is None:
        print("error: storage is not configured")
        return 1

    city = next((c for c in cities if source_key(c) == args.source_key), None)
    if city is None:
        print(f"error: no configured city has source_key={args.source_key!r}")
        return 1

    state_dir = resolve_state_dir(site_config, output_dir)
    pull_state(storage, state_dir)
    records = load_records(state_dir, args.source_key)
    rec = records.get(args.episode_uid)
    if rec is None:
        print(f"error: no record for {args.source_key}/{args.episode_uid}")
        return 1

    ep = record_to_episode(rec)
    if ep.transcript_key and ep.transcript_words_key:
        print(
            f"nothing to do: {args.source_key}/{args.episode_uid} already has a transcript "
            f"(key={ep.transcript_key})"
        )
        return 0

    recipe = _asr_recipe_hash(city, ep, None)
    asr_key = _asr_object_key(args.source_key, args.episode_uid, recipe)
    words_key = _asr_words_object_key(args.source_key, args.episode_uid, recipe)
    if not (storage.exists(asr_key) and storage.exists(words_key)):
        print(
            f"error: no existing ASR artifact at recipe={recipe} for "
            f"{args.source_key}/{args.episode_uid} (asr_key={asr_key}) -- nothing to reclaim; "
            "this episode needs to be re-transcribed, not reclaimed"
        )
        return 1

    print(
        f"found existing artifact for {args.source_key}/{args.episode_uid}: "
        f"recipe={recipe} asr_key={asr_key} words_key={words_key}"
    )
    if not args.write:
        print("dry run (pass --write to actually reclaim): would adopt keys and push the record")
        return 0

    _adopt_asr_keys(ep, storage, asr_key, words_key, recipe)
    records[args.episode_uid] = episode_to_record(ep)
    save_records(state_dir, args.source_key, records)
    pushed = push_records_merged(
        storage,
        state_dir,
        [args.source_key],
        protected_blocks=protected_blocks_for_lane("transcribe"),
        owned_uids={args.source_key: frozenset({args.episode_uid})},
    )
    if pushed != 1:
        print(f"error: failed to push reclaimed record for {args.source_key}/{args.episode_uid}")
        return 1
    print(f"reclaimed transcript for {args.source_key}/{args.episode_uid}")
    return 0


def _compute_plan_shards(args) -> int:
    """Write one deterministic shard plan from the state already restored by reconcile."""
    from pathlib import Path

    from citypods.sharding import create_shard_plan, save_shard_plan
    from citypods.stages import ASR_PIPELINE_VERSION
    from citypods.state import resolve_state_dir

    if args.shards < 1:
        raise SystemExit("--shards must be >= 1")
    site_config = load_site_config(args.site_config)
    defaults = site_config.get("defaults", {})
    cities = load_city_configs(args.config_dir, defaults)
    state_dir = resolve_state_dir(site_config, Path(args.output_dir))
    plan = create_shard_plan(
        cities,
        state_dir,
        lane=args.lane,
        num_shards=args.shards,
        defaults=defaults,
        asr_pipeline_version=ASR_PIPELINE_VERSION,
    )
    save_shard_plan(args.output, plan)
    loads = [0.0] * plan.num_shards
    for key, owner in plan.assignment.items():
        loads[owner] += plan.weights[key]
    unit_label = "episode" if plan.unit == "episode" else "source"
    print(
        f"compute plan-shards: wrote {len(plan.assignment)} {unit_label}(s) across "
        f"{plan.num_shards} {plan.lane} shard(s) to {args.output}; "
        f"loads={','.join(f'{load:.1f}' for load in loads)}"
    )
    return 0


def _validate_build(args) -> int:
    from pathlib import Path

    from citypods.records import load_records, source_key
    from citypods.validate import validate_build

    output_dir = Path(args.output_dir)
    state_dir = Path(args.state_dir)
    site_config = load_site_config(args.site_config)
    cities = load_city_configs(args.config_dir, site_config.get("defaults", {}))

    known_empty: set[str] = set()
    if state_dir.exists():
        for city in cities:
            key = source_key(city)
            records = load_records(state_dir, key)
            body = city.source.get("body")
            hosted = sum(
                1
                for r in records.values()
                if (not body or r.get("body") == body) and (r.get("audio") or {}).get("url")
            )
            if hosted == 0:
                known_empty.add(city.slug)

    fatals, warnings = validate_build(output_dir, known_empty)

    for msg in warnings:
        print(f"WARN  {msg}")
    for msg in fatals:
        print(f"ERROR {msg}")

    if fatals:
        print(f"\nvalidate-build: {len(fatals)} fatal error(s), {len(warnings)} warning(s) — FAIL")
        return 1
    if warnings:
        print(f"\nvalidate-build: 0 fatal errors, {len(warnings)} warning(s) — PASS")
    else:
        print(f"validate-build: {sum(1 for _ in output_dir.rglob('*.xml'))} feed(s) valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
