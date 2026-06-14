#!/usr/bin/env python
"""Clear the audio materializations produced by one Actions run, so they re-materialize cleanly.

Use this to undo a bad `Audio` run wholesale: the first sharded run (before #39's per-host cap +
truncation guard) hosted truncated/empty audio (e.g. 258-byte Swagit stubs). Rather than guess which
episodes are bad, this nukes **everything that run materialized** — each `audio encode done` line in
its log — and resets those records so the next `audio.yml` re-encodes them correctly.

What it does, per episode the run hosted (matched by `source` + `uid` from the run log):
  * clears the record's audio block (key/url/spec_hash/bytes/encode_time/duration_served) and resets
    the #120 backoff (attempts/last_attempt/error) → the next run sees "not hosted" and re-encodes;
  * optionally deletes the B2 object for that audio key (``--delete-objects``). Optional because a
    re-encode overwrites the same content-addressed key anyway; deleting just leaves no stale stub.

Safe by default:
  * **dry-run unless ``--apply``** — prints exactly what it would clear/delete and changes nothing;
  * pulls the durable state first and pushes back only the affected ``sources/<key>/`` prefixes
    (never reconciles, so it can't sweep a sibling shard's records);
  * refuses to run if the run log yields no materializations (so a no-op can't look like success);
  * only ever touches audio blocks — transcripts and the durable ``state/`` snapshot are untouched.

Usage:
    # Inspect what run 27497034389 materialized (no changes):
    PYTHONPATH=. python scripts/clear_run_materializations.py 27497034389

    # Actually clear the records and delete the B2 objects (needs B2_* + GITHUB_TOKEN/gh auth):
    PYTHONPATH=. python scripts/clear_run_materializations.py 27497034389 --apply --delete-objects

    # Offline (feed a saved log instead of calling gh):
    PYTHONPATH=. python scripts/clear_run_materializations.py --log-file run.log --apply
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

from citypods.config import load_site_config
from citypods.records import load_records, save_records
from citypods.state import resolve_state_dir
from citypods.statesync import pull_state, push_state
from citypods.storage import make_storage

# Matches the per-episode host line emitted by citypods/media.py, e.g.:
#   [enrich] audio encode done slug=... provider=swagit source=3b7588310856 uid=186781db3d142caf ...
_DONE_RE = re.compile(r"audio encode done\b.*?\bsource=(?P<source>\w+)\b.*?\buid=(?P<uid>\w+)\b")

# Audio-block fields cleared to force a clean re-materialization (mirrors episode_to_record).
_CLEARED_AUDIO = {
    "key": None,
    "url": None,
    "spec_hash": None,
    "bytes": None,
    "encode_time": None,
    "duration_served": None,
    "attempts": 0,
    "last_attempt": None,
    "error": None,
}


def parse_materialized(log_text: str) -> dict[str, set[str]]:
    """Map ``source_key -> {uid, ...}`` for every ``audio encode done`` line in a run log.

    Pure (no I/O) so it is trivially unit-testable from a fixture."""
    out: dict[str, set[str]] = {}
    for m in _DONE_RE.finditer(log_text):
        out.setdefault(m.group("source"), set()).add(m.group("uid"))
    return out


def _fetch_run_log(run_id: str) -> str:
    """The full (all-shards) log for an Actions run, via the `gh` CLI."""
    proc = subprocess.run(  # noqa: S603,S607 - run_id is the only input; gh is a trusted tool
        ["gh", "run", "view", run_id, "--log"],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def clear_materializations(
    state_dir: Path,
    affected: dict[str, set[str]],
    *,
    storage=None,
    delete_objects: bool = False,
    apply: bool = False,
) -> dict:
    """Clear the audio block of every ``(source, uid)`` in ``affected`` that has hosted audio.

    Returns a summary dict: ``cleared`` (count), ``deleted`` (count), ``touched_sources`` (set), and
    ``object_keys`` (the B2 keys that were/would be deleted). ``apply=False`` writes nothing.
    """
    cleared = 0
    deleted = 0
    touched: set[str] = set()
    object_keys: list[str] = []
    for source_key, uids in sorted(affected.items()):
        records = load_records(state_dir, source_key)
        if not records:
            continue
        changed = False
        for uid in sorted(uids):
            rec = records.get(uid)
            if not isinstance(rec, dict):
                continue
            audio = rec.get("audio") or {}
            if not audio.get("url") and not audio.get("key"):
                continue  # nothing hosted for this episode — skip
            key = audio.get("key")
            if key:
                object_keys.append(key)
                if delete_objects and apply and storage is not None:
                    storage.delete(key)
                    deleted += 1
            rec["audio"] = {**audio, **_CLEARED_AUDIO}
            cleared += 1
            changed = True
        if changed:
            touched.add(source_key)
            if apply:
                save_records(state_dir, source_key, records)
    return {
        "cleared": cleared,
        "deleted": deleted,
        "touched_sources": touched,
        "object_keys": object_keys,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "run_id", nargs="?", help="Actions run ID/number to clear (omit with --log-file)"
    )
    ap.add_argument("--log-file", help="read the run log from a file instead of calling `gh`")
    ap.add_argument("--apply", action="store_true", help="actually mutate state (default: dry-run)")
    ap.add_argument(
        "--delete-objects",
        action="store_true",
        help="also delete the B2 audio objects (else just reset records; re-encode overwrites)",
    )
    ap.add_argument("--site-config", default="config/site_config.yml")
    ap.add_argument("--output-dir", default="docs")
    args = ap.parse_args(argv)

    if not args.run_id and not args.log_file:
        ap.error("provide a run_id (uses gh) or --log-file")

    log_text = Path(args.log_file).read_text() if args.log_file else _fetch_run_log(args.run_id)
    affected = parse_materialized(log_text)
    total = sum(len(v) for v in affected.values())
    if not total:
        print("no 'audio encode done' lines found in the run log; nothing to clear")
        return 1
    print(f"run materialized {total} episode(s) across {len(affected)} source(s):")
    for source_key, uids in sorted(affected.items()):
        print(f"  {source_key}: {len(uids)} episode(s)")

    site_config = load_site_config(args.site_config)
    storage = make_storage(site_config, "https://example.invalid", args.output_dir)
    state_dir = resolve_state_dir(site_config, args.output_dir)

    if args.delete_objects and (storage is None or not hasattr(storage, "delete")):
        print("--delete-objects requested but no storage backend with delete support is configured")
        return 1

    # Pull regardless of --apply: it's read-only (downloads the durable records into the local
    # cache) and a dry-run must see the real records to report what it would clear.
    if storage is not None:
        pulled = pull_state(storage, state_dir)
        print(f"state: pulled {pulled} file(s) from durable storage")

    summary = clear_materializations(
        state_dir,
        affected,
        storage=storage,
        delete_objects=args.delete_objects,
        apply=args.apply,
    )

    if args.apply and storage is not None and summary["touched_sources"]:
        prefixes = [f"sources/{sk}/" for sk in sorted(summary["touched_sources"])]
        pushed = push_state(storage, state_dir, only_prefixes=prefixes)
        print(f"state: pushed {pushed} file(s) back (scoped to {len(prefixes)} source(s))")

    verb = "cleared" if args.apply else "would clear (dry-run)"
    del_verb = "deleted" if (args.apply and args.delete_objects) else "would delete (dry-run)"
    print(
        f"\n{summary['cleared']} record(s) {verb}; "
        f"{len(summary['object_keys'])} object(s) {del_verb}"
        + ("" if args.apply else "  — re-run with --apply to make changes")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
