#!/usr/bin/env python
"""Reset partial agenda/chapter state so the standard workflow rebuilds it.

This is a one-time, dry-run-by-default recovery tool for records whose derived agenda artifact
pointer was lost. It deliberately preserves provider-owned links (``agenda``, ``agenda_portal``,
and ``agenda_packet``), audio, transcripts, and the stored artifact objects themselves. The next
Audio run will rediscover the official agenda URL, recreate the derived agenda text artifact, and
the chapter lanes can then submit their normal Mistral and Gemini jobs.

The reset is scoped to records that have partial agenda/chapter state but no
``links["agenda_text_artifact_key"]``. It does not delete deferred LLM registry entries: recipe
idempotency makes an already-submitted request safe to rediscover on the next chapter run.

Usage:
    PYTHONPATH=. python scripts/reset_agenda_chapter_state.py          # dry-run
    PYTHONPATH=. python scripts/reset_agenda_chapter_state.py --apply
    PYTHONPATH=. python scripts/reset_agenda_chapter_state.py \
        --source <source-key> --apply
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from citypods.config import load_city_configs, load_site_config
from citypods.ops.maintenance_leases import (
    AGENDA_CHAPTER_MAINTENANCE_LEASE_KEY,
)
from citypods.ops.maintenance_leases import (
    acquire as acquire_maintenance_lease,
)
from citypods.records import (
    load_records,
    protected_blocks_for_lane,
    save_records,
    source_key,
)
from citypods.state import resolve_state_dir
from citypods.statesync import pull_state, push_records_merged
from citypods.storage import make_storage

_DERIVED_AGENDA_LINKS = (
    "agenda_text_artifact",
    "agenda_text_artifact_key",
    "agenda_backup_artifact",
    "agenda_backup_artifact_key",
)
_RESET_STAGE_NAMES = frozenset(
    {"agenda_text", "chapter_agenda", "chapter_locator", "generated_chapters"}
)


def _record_uid(rec: dict, fallback: str) -> str:
    return str(rec.get("uid") or fallback)


def _has_partial_agenda_state(rec: dict) -> bool:
    agenda = rec.get("agenda_text") or {}
    backup = rec.get("agenda_backup") or {}
    candidates = rec.get("generated_agenda_candidates") or {}
    return bool(
        agenda
        or backup
        or candidates
        or rec.get("generated_chapters")
        or rec.get("generated_chapters_spec_hash")
    )


def needs_reset(rec: dict) -> bool:
    """Return whether a record is in the known lost-pointer cohort."""
    if not isinstance(rec, dict):
        return False
    links = rec.get("links") or {}
    return not links.get("agenda_text_artifact_key") and _has_partial_agenda_state(rec)


def reset_record(rec: dict) -> dict:
    """Clear derived agenda/chapter state while retaining official links and unrelated artifacts."""
    rec.pop("agenda_text", None)
    rec.pop("agenda_backup", None)
    rec.pop("generated_agenda_candidates", None)
    rec.pop("generated_chapters", None)
    rec.pop("generated_chapters_spec_hash", None)

    status = rec.get("stage_completion")
    if isinstance(status, dict):
        remaining = dict(status)
        for name in _RESET_STAGE_NAMES:
            if name in remaining:
                # Scoped merges are additive for stage markers; an explicit null is the tombstone
                # that prevents a remote completion marker from being resurrected.
                remaining[name] = None
        if remaining:
            rec["stage_completion"] = remaining
        else:
            rec.pop("stage_completion", None)

    # The scoped audio push can explicitly clear a link only when the local record carries that
    # key. Keep tombstones for keys that existed locally so a stale derived URL cannot survive the
    # remote-preserving merge. Provider-owned links are never touched.
    links = dict(rec.get("links") or {})
    for key in _DERIVED_AGENDA_LINKS:
        if key in links:
            links[key] = None
    if links:
        rec["links"] = links
    else:
        rec.pop("links", None)
    return rec


def _clean_filter(values: list[str], *, lower: bool = False) -> set[str] | None:
    cleaned = {value.strip().lower() if lower else value.strip() for value in values}
    cleaned.discard("")
    return cleaned or None


def select_sources(
    state_dir: Path,
    source_to_provider: dict[str, str],
    *,
    providers: set[str] | None,
    sources: set[str] | None,
) -> list[str]:
    selected = []
    for path in sorted(Path(state_dir).glob("sources/*/episodes.json")):
        key = path.parent.name
        if sources is not None and key not in sources:
            continue
        if providers is not None and source_to_provider.get(key) not in providers:
            continue
        selected.append(key)
    return selected


def plan_resets(
    state_dir: Path,
    source_keys: list[str],
    *,
    uids: set[str] | None = None,
    max_records: int = 25_000,
) -> dict[str, list[str]]:
    """Return deterministic source→UID reset targets without mutating records."""
    if max_records < 1:
        raise ValueError("--max-records must be >= 1")
    planned: dict[str, list[str]] = {}
    total = 0
    for key in source_keys:
        records = load_records(state_dir, key)
        matches = []
        for rec_key, rec in sorted(records.items()):
            uid = _record_uid(rec, str(rec_key))
            if uids is not None and uid not in uids:
                continue
            if not needs_reset(rec):
                continue
            if total >= max_records:
                break
            matches.append(uid)
            total += 1
        if matches:
            planned[key] = matches
        if total >= max_records:
            break
    return planned


def reset_agenda_chapter_state(
    state_dir: Path,
    planned: dict[str, list[str]],
    *,
    apply: bool = False,
    storage=None,
    maintenance_lease=None,
) -> dict:
    """Reset planned records and, when requested, push both owned blocks safely."""
    touched = sorted(planned)

    def apply_reset_snapshot(*, persist: bool) -> int:
        changed = 0
        for key in touched:
            records = load_records(state_dir, key)
            target_uids = set(planned[key])
            for rec_key, rec in records.items():
                if _record_uid(rec, str(rec_key)) in target_uids:
                    reset_record(rec)
                    changed += 1
            if persist:
                save_records(state_dir, key, records)
        return changed

    reset_count = apply_reset_snapshot(persist=apply)

    pushed = {"chapter": 0, "audio": 0}
    if apply and storage is not None and touched:
        owned_uids = {key: frozenset(planned[key]) for key in touched}
        # Clear chapter-owned blocks first, then audio-owned agenda blocks. Each pass preserves
        # the sibling lane's current state and writes only the selected UIDs.
        for lane in ("chapter", "audio"):
            # The preceding scoped merge may restore the sibling lane from remote state. Reapply
            # the reset snapshot so the next lane push also carries the intended tombstones.
            apply_reset_snapshot(persist=True)
            pushed[lane] = push_records_merged(
                storage,
                state_dir,
                touched,
                protected_blocks=protected_blocks_for_lane(lane),
                lane=lane,
                owned_uids=owned_uids,
                maintenance_lease=maintenance_lease,
            )
    return {"reset": reset_count, "touched_sources": touched, "pushed": pushed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", action="append", default=[])
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--uid", action="append", default=[])
    parser.add_argument("--max-records", type=int, default=25_000)
    parser.add_argument("--apply", action="store_true", help="actually mutate durable state")
    parser.add_argument("--site-config", default="config/site_config.yml")
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--output-dir", default="docs")
    args = parser.parse_args(argv)

    site_config = load_site_config(args.site_config)
    cities = load_city_configs(args.config_dir, site_config.get("defaults", {}))
    source_to_provider = {source_key(city): city.provider for city in cities}
    storage = make_storage(site_config, "https://example.invalid", args.output_dir)
    state_dir = resolve_state_dir(site_config, args.output_dir)
    if args.apply and storage is None:
        print("--apply requires a configured storage backend", file=sys.stderr)
        return 1
    if storage is not None:
        print(f"state: pulled {pull_state(storage, state_dir)} file(s)")

    selected = select_sources(
        state_dir,
        source_to_provider,
        providers=_clean_filter(args.provider, lower=True),
        sources=_clean_filter(args.source),
    )
    if not selected:
        print("no sources matched the supplied filters", file=sys.stderr)
        return 1
    try:
        planned = plan_resets(
            state_dir,
            selected,
            uids=_clean_filter(args.uid),
            max_records=args.max_records,
        )
    except ValueError as exc:
        parser.error(str(exc))
    count = sum(len(uids) for uids in planned.values())
    if not count:
        print("no partial agenda records without agenda_text_artifact_key matched")
        return 1
    print(f"scanned {len(selected)} source(s); matching records: {count}")
    for key, uids in planned.items():
        print(f"  {key} ({source_to_provider.get(key, '?')}): {len(uids)} record(s)")
    if not args.apply:
        print("dry-run: re-run with --apply to clear and push these records")
        return 0

    lease_owner = os.environ.get("CITYPODS_MAINTENANCE_LEASE_OWNER") or (
        f"github-actions:{os.environ.get('GITHUB_WORKFLOW', 'manual-reset')}"
        f":{os.environ.get('GITHUB_RUN_ID', 'local')}"
    )
    maintenance_lease = acquire_maintenance_lease(
        storage,
        owner=lease_owner,
        key=AGENDA_CHAPTER_MAINTENANCE_LEASE_KEY,
    )
    try:
        summary = reset_agenda_chapter_state(
            state_dir,
            planned,
            apply=True,
            storage=storage,
            maintenance_lease=maintenance_lease,
        )
    finally:
        maintenance_lease.release()
    expected_sources = len(summary["touched_sources"])
    if (
        summary["pushed"]["chapter"] != expected_sources
        or summary["pushed"]["audio"] != expected_sources
    ):
        print(f"error: scoped push incomplete: {summary['pushed']}", file=sys.stderr)
        return 1
    print(
        f"reset {summary['reset']} record(s); pushed chapter={summary['pushed']['chapter']} "
        f"audio={summary['pushed']['audio']} source file(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
