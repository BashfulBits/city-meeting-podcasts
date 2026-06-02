#!/usr/bin/env python
"""Garbage-collect orphaned audio objects from storage.

Audio is content-addressed (the key embeds the audio spec), so when an episode's audio is
regenerated — e.g. chapters added or a bitrate-policy bump — the old object is left behind,
no longer referenced by any record. This sweep deletes those orphans.

Safe by default:
  * dry-run unless ``--apply`` is given;
  * only deletes objects older than ``--min-age-days`` (default 7) so an object written by a
    build that hasn't yet persisted its record isn't reaped out from under it;
  * only considers keys under the audio prefix the pipeline writes.

Usage:
    PYTHONPATH=. python scripts/gc_audio.py [--apply] [--min-age-days N]
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta

from citypods.config import load_site_config
from citypods.records import referenced_audio_keys
from citypods.state import resolve_state_dir
from citypods.statesync import STATE_PREFIX
from citypods.storage import make_storage


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="actually delete (default: dry-run)")
    ap.add_argument("--min-age-days", type=float, default=7.0, help="skip objects newer than this")
    ap.add_argument("--prefix", default="", help="limit to keys under this storage prefix")
    ap.add_argument("--site-config", default="config/site_config.yml")
    ap.add_argument("--output-dir", default="docs")
    args = ap.parse_args(argv)

    site_config = load_site_config(args.site_config)
    storage = make_storage(site_config, "https://example.invalid", args.output_dir)
    if storage is None or not hasattr(storage, "list_objects"):
        print("no storage backend with GC support configured; nothing to do")
        return 0

    state_dir = resolve_state_dir(site_config, args.output_dir)
    referenced = referenced_audio_keys(state_dir)
    if not referenced:
        print("no records found; refusing to GC (would look like everything is orphaned)")
        return 1

    cutoff = datetime.now(UTC) - timedelta(days=args.min_age_days)
    deleted = kept_young = 0
    for key, last_modified in storage.list_objects(args.prefix):
        if key in referenced or key.startswith(f"{STATE_PREFIX}/"):
            continue  # never reap the durable state snapshot
        if last_modified is not None and last_modified > cutoff:
            kept_young += 1
            continue
        if args.apply:
            storage.delete(key)
            print(f"DELETED  {key}")
        else:
            print(f"ORPHAN   {key}")
        deleted += 1

    action = "deleted" if args.apply else "orphaned (dry-run)"
    print(
        f"\n{len(referenced)} referenced; {deleted} {action}; "
        f"{kept_young} skipped (younger than {args.min_age_days}d)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
