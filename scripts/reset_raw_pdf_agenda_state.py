#!/usr/bin/env python
"""Reset agenda/chapter state for episodes confirmed to hold raw, undecoded PDF bytes as their
``agenda_text_artifact`` (the ``_extract_pdf`` ``ImportError``-fallback bug fixed in
``citypods/agenda_text.py``; see ``scripts/audit_raw_pdf_agenda_artifacts.py``, its read-only
survey companion).

Unlike ``scripts/reset_agenda_chapter_state.py`` (which targets records with a *lost* artifact
pointer), these records have a perfectly present ``links["agenda_text_artifact_key"]`` -- it just
points at corrupted content, so nothing about the record's own shape identifies it as bad. The
reset target list therefore comes from a survey manifest (``--hits-file``, the JSON
``audit_raw_pdf_agenda_artifacts.py --json`` produces) rather than a scan of current record state.
Each target is re-checked against current state before being touched -- a record whose
``agenda_text_artifact_key`` no longer matches what the survey saw has already been reprocessed
(by another run, or a previous invocation of this script) and is left alone.

Reuses ``reset_agenda_chapter_state``'s ``reset_record``/scoped-push mechanics unchanged: clearing
the same derived-agenda fields makes ``AgendaTextStage`` treat the episode as unprocessed and
re-derive it -- under the now-fixed extractor -- on the next run.

Usage:
    PYTHONPATH=. python scripts/reset_raw_pdf_agenda_state.py --hits-file hits.json  # dry-run
    PYTHONPATH=. python scripts/reset_raw_pdf_agenda_state.py --hits-file hits.json --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from citypods.config import load_city_configs, load_site_config
from citypods.ops.maintenance_leases import (
    AGENDA_CHAPTER_RESET_MAINTENANCE_LEASE_KEYS,
)
from citypods.ops.maintenance_leases import (
    acquire as acquire_maintenance_lease,
)
from citypods.records import load_records, records_path, source_key
from citypods.state import resolve_state_dir
from citypods.statesync import STATE_PREFIX
from citypods.storage import make_storage
from scripts.reset_agenda_chapter_state import reset_agenda_chapter_state


def sync_targeted_sources(storage, state_dir: Path, source_keys: list[str]) -> int:
    """Pull only the specific ``state/sources/<key>/episodes.json`` files this run needs.

    A full ``pull_state`` syncs the entire canonical snapshot (every city's episodes, chapters,
    LLM-dispatch registries, ...) -- overkill and needlessly heavy on B2 for a run touching a
    couple dozen sources, and exactly the kind of unscoped bulk access that caused problems
    earlier in this investigation. Each file here is small (one source's episode records), so a
    plain ``get_file`` per source is cheap and the natural unit of retry on a transient failure.
    """
    synced = 0
    for sk in sorted(set(source_keys)):
        local_path = records_path(state_dir, sk)
        if storage.get_file(f"{STATE_PREFIX}/sources/{sk}/episodes.json", local_path):
            synced += 1
    return synced


def load_hit_targets(hits_file: Path, slug_to_source: dict[str, str]) -> dict[str, dict[str, str]]:
    """Return ``{source_key: {uid: expected_agenda_text_artifact_key}}`` from a survey manifest."""
    data = json.loads(hits_file.read_text())
    targets: dict[str, dict[str, str]] = {}
    for hit in data:
        key = hit["key"]
        for ep in hit["episodes"]:
            slug, uid = ep["slug"], ep["uid"]
            src = slug_to_source.get(slug)
            if src is None:
                print(f"warning: unknown city slug {slug!r} (uid {uid}); skipping", file=sys.stderr)
                continue
            targets.setdefault(src, {})[uid] = key
    return targets


def _record_uid(rec: dict, fallback: str) -> str:
    return str(rec.get("uid") or fallback)


def plan_resets(state_dir: Path, targets: dict[str, dict[str, str]]) -> dict[str, list[str]]:
    """Re-check each survey hit against current record state; only a still-corrupt record plans."""
    planned: dict[str, list[str]] = {}
    for src, uid_to_key in targets.items():
        records = load_records(state_dir, src)
        by_uid = {_record_uid(rec, str(rec_key)): rec for rec_key, rec in records.items()}
        matches = []
        for uid, expected_key in uid_to_key.items():
            rec = by_uid.get(uid)
            if rec is None:
                print(f"skip {src}/{uid}: no longer in records", file=sys.stderr)
                continue
            current_key = (rec.get("links") or {}).get("agenda_text_artifact_key")
            if current_key != expected_key:
                print(
                    f"skip {src}/{uid}: agenda_text_artifact_key changed since the survey "
                    f"(already reprocessed) -- was {expected_key!r}, now {current_key!r}",
                    file=sys.stderr,
                )
                continue
            matches.append(uid)
        if matches:
            planned[src] = matches
    return planned


def apply_planned(
    state_dir: Path,
    planned: dict[str, list[str]],
    *,
    storage,
    maintenance_lease,
    sequential: bool,
) -> tuple[int, list[str]]:
    """Apply the reset; return (records reset, source keys that failed to push).

    Default: one ``reset_agenda_chapter_state`` call covering every source (its own
    ``push_records_merged`` launches up to 16 concurrent uploads). ``sequential=True`` instead
    pushes one source at a time, isolating a failure to that source alone -- the rest already
    pushed are not retried, and the failed source(s) are reported so a rerun (safe:
    ``plan_resets`` re-verifies current state first) only needs to retry what's left. Slower, but
    the safer default for a cohort that mixes normal-sized and very large (80-120MB)
    ``episodes.json`` files under one concurrent batch.
    """
    if not sequential:
        summary = reset_agenda_chapter_state(
            state_dir, planned, apply=True, storage=storage, maintenance_lease=maintenance_lease
        )
        expected = len(summary["touched_sources"])
        ok = summary["pushed"]["chapter"] == expected and summary["pushed"]["audio"] == expected
        return summary["reset"], ([] if ok else list(summary["touched_sources"]))

    total_reset = 0
    failed: list[str] = []
    for src in sorted(planned):
        one_source = {src: planned[src]}
        try:
            summary = reset_agenda_chapter_state(
                state_dir,
                one_source,
                apply=True,
                storage=storage,
                maintenance_lease=maintenance_lease,
            )
        except Exception as exc:  # noqa: BLE001 - one source's failure must not stop the rest
            print(f"error: {src}: {exc}", file=sys.stderr)
            failed.append(src)
            continue
        expected = len(summary["touched_sources"])
        if summary["pushed"]["chapter"] != expected or summary["pushed"]["audio"] != expected:
            print(f"error: {src}: scoped push incomplete: {summary['pushed']}", file=sys.stderr)
            failed.append(src)
            continue
        total_reset += summary["reset"]
        print(f"  {src}: reset {summary['reset']} record(s), pushed")
    return total_reset, failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hits-file",
        type=Path,
        required=True,
        help="JSON manifest from `audit_raw_pdf_agenda_artifacts.py --json`.",
    )
    parser.add_argument("--apply", action="store_true", help="actually mutate durable state")
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        help=(
            "Restrict to one source key (repeatable). Pushing every source at once launches up "
            "to 16 concurrent uploads (citypods.statesync._STATE_SYNC_MAX_WORKERS) -- fine for "
            "normal-sized episodes.json files, but a mixed batch that includes a very large one "
            "(some sources here are 100MB+) is more prone to connection failures under that "
            "burst. Pass this to push a small group (or one source) at a time instead."
        ),
    )
    parser.add_argument(
        "--sequential",
        action="store_true",
        help=(
            "Push one source at a time instead of one call covering every source (which "
            "launches up to 16 concurrent uploads -- fine normally, but this cohort mixes "
            "normal-sized and very large, 80-120MB, episodes.json files under one batch). A "
            "failed source is reported and skipped rather than aborting the run; a rerun safely "
            "retries only what's left, since plan_resets() re-verifies current state first."
        ),
    )
    parser.add_argument("--site-config", default="config/site_config.yml")
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--output-dir", default="docs")
    args = parser.parse_args(argv)

    site_config = load_site_config(args.site_config)
    cities = load_city_configs(args.config_dir, site_config.get("defaults", {}))
    slug_to_source = {city.slug: source_key(city) for city in cities}
    storage = make_storage(site_config, "https://example.invalid", args.output_dir)
    state_dir = resolve_state_dir(site_config, args.output_dir)
    if args.apply and storage is None:
        print("--apply requires a configured storage backend", file=sys.stderr)
        return 1

    targets = load_hit_targets(args.hits_file, slug_to_source)
    if args.source:
        wanted = set(args.source)
        targets = {sk: uids for sk, uids in targets.items() if sk in wanted}
        missing = wanted - set(targets)
        if missing:
            print(
                f"warning: --source value(s) not in the hits file: {sorted(missing)}",
                file=sys.stderr,
            )
    if storage is not None:
        synced = sync_targeted_sources(storage, state_dir, list(targets))
        print(f"state: synced {synced} source file(s)")

    planned = plan_resets(state_dir, targets)
    count = sum(len(uids) for uids in planned.values())
    if not count:
        print("no surveyed hits still match current record state -- nothing to reset")
        return 1
    print(f"{len(targets)} source(s) in the survey; still-corrupt records: {count}")
    for key, uids in planned.items():
        print(f"  {key}: {len(uids)} record(s)")
    if not args.apply:
        print("dry-run: re-run with --apply to clear and push these records")
        return 0

    lease_owner = os.environ.get("CITYPODS_MAINTENANCE_LEASE_OWNER") or (
        f"github-actions:{os.environ.get('GITHUB_WORKFLOW', 'manual-reset')}"
        f":{os.environ.get('GITHUB_RUN_ID', 'local')}"
    )
    raw_lease_key = os.environ.get("CITYPODS_MAINTENANCE_LEASE_KEY")
    target_keys: str | tuple[str, ...]
    if raw_lease_key:
        target_keys = (
            tuple(k.strip() for k in raw_lease_key.split(",") if k.strip())
            if "," in raw_lease_key
            else raw_lease_key.strip()
        )
    else:
        target_keys = AGENDA_CHAPTER_RESET_MAINTENANCE_LEASE_KEYS
    maintenance_lease = acquire_maintenance_lease(storage, owner=lease_owner, key=target_keys)
    try:
        reset_count, failed = apply_planned(
            state_dir,
            planned,
            storage=storage,
            maintenance_lease=maintenance_lease,
            sequential=args.sequential,
        )
    finally:
        maintenance_lease.release()
    if failed:
        print(f"error: {len(failed)} source(s) failed to push: {sorted(failed)}", file=sys.stderr)
        print(
            f"reset {reset_count} record(s) before the failure(s); rerun to retry "
            "(already-pushed sources are skipped automatically)",
            file=sys.stderr,
        )
        return 1
    print(f"reset {reset_count} record(s) across {len(planned)} source(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
