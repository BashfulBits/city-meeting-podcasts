#!/usr/bin/env python
"""Clear terminal-failure audit markers so blocked recipe lineages may re-run cleanly.

When a stage fails repeatedly (e.g. hitting a schema validation error before a fix was deployed),
the runner writes a failure marker under ``state/llm_deferred_failures/<recipe_hash>.json``
with status ``exhausted`` or ``structural_blocked``. The engine blocks all future attempts for
that recipe until the failure marker is cleared or a new generation is introduced.

Usage:
    # Dry-run inspect matching tag failure markers from the last 7 days:
    python scripts/clear_terminal_failures.py --task tag

    # Scan all historical markers without time cutoff:
    python scripts/clear_terminal_failures.py --task tag --days 0

    # Apply deletion of tag failure markers from object storage:
    python scripts/clear_terminal_failures.py --task tag --apply

    # Clear a single specific recipe hash:
    python scripts/clear_terminal_failures.py --recipe-hash <hash> --apply
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from typing import Any

from citypods.compute.llm_deferred import (
    DEFERRED_FAILURE_PREFIX,
    _read_json,
    clear_terminal_failure,
    deferred_failure_key,
)
from citypods.config import load_site_config
from citypods.storage import make_storage


def _safe_read_json(storage: Any, key: str) -> Mapping[str, Any] | None:
    """Read a JSON object safely, catching transient storage or parse errors."""
    try:
        return _read_json(storage, key)
    except Exception:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="clear_terminal_failures",
        description="Clear terminal failure markers for re-trying blocked recipes.",
    )
    parser.add_argument("--site-config", default="config/site_config.yml")
    parser.add_argument(
        "--task",
        default="tag",
        help="task name to clear (e.g. 'tag', 'agenda-chapter-locate', 'all'; default: 'tag')",
    )
    parser.add_argument(
        "--recipe-hash",
        help="clear only this exact recipe hash",
    )
    parser.add_argument(
        "--status",
        default="exhausted,structural_blocked",
        help="comma-separated failure statuses to clear (default: 'exhausted,structural_blocked')",
    )
    parser.add_argument(
        "--days",
        type=float,
        default=7.0,
        help="only scan markers modified in the last N days (0 = scan all history; default: 7.0)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=8,
        help="concurrent worker threads for reading markers (default: 8)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually delete matching failure markers from storage (dry run by default)",
    )

    args = parser.parse_args(argv)
    site_config = load_site_config(args.site_config)
    storage = make_storage(site_config, "", "docs")

    target_statuses = {s.strip() for s in args.status.split(",") if s.strip()}

    if args.recipe_hash:
        key = deferred_failure_key(args.recipe_hash)
        data = _safe_read_json(storage, key)
        if not data:
            print(f"No failure marker found for recipe {args.recipe_hash}", flush=True)
            return 0
        task = data.get("task")
        status = data.get("status")
        print(f"Found marker for {args.recipe_hash}: task={task} status={status}", flush=True)
        if args.apply:
            ok = clear_terminal_failure(storage, args.recipe_hash)
            print(f"Cleared marker for {args.recipe_hash}: {ok}", flush=True)
        else:
            print("Dry-run: pass --apply to delete this marker", flush=True)
        return 0

    print(
        f"Scanning {DEFERRED_FAILURE_PREFIX} for task='{args.task}' "
        f"statuses={sorted(target_statuses)} (days={args.days:g})...",
        flush=True,
    )
    listing = storage.list_objects(DEFERRED_FAILURE_PREFIX)
    cutoff = datetime.now(UTC) - timedelta(days=args.days) if args.days and args.days > 0 else None

    candidates: list[str] = []
    total_listed = 0
    for key, last_modified in listing:
        total_listed += 1
        if cutoff is not None and isinstance(last_modified, datetime):
            ts = last_modified if last_modified.tzinfo else last_modified.replace(tzinfo=UTC)
            if ts < cutoff:
                continue
        candidates.append(key)

    if cutoff is not None:
        print(
            f"Listed {total_listed} markers in storage; {len(candidates)} modified within "
            f"the last {args.days:g} days.",
            flush=True,
        )
    else:
        print(f"Listed {len(candidates)} markers in storage.", flush=True)

    if not candidates:
        print("Nothing to inspect.", flush=True)
        return 0

    matched: list[tuple[str, str, dict[str, Any]]] = []
    checked = 0

    def _check_key(key: str) -> tuple[str, str, dict[str, Any]] | None:
        data = _safe_read_json(storage, key)
        if not isinstance(data, Mapping):
            return None
        task = data.get("task")
        status = data.get("status")
        if args.task != "all" and task != args.task:
            return None
        if target_statuses and status not in target_statuses:
            return None
        recipe_hash = data.get("recipe_hash") or key.removeprefix(
            DEFERRED_FAILURE_PREFIX
        ).removesuffix(".json")
        return (key, recipe_hash, dict(data))

    concurrency = max(1, args.concurrency)
    if concurrency == 1 or len(candidates) <= 1:
        for key in candidates:
            res = _check_key(key)
            if res:
                matched.append(res)
            checked += 1
            if checked % 100 == 0 or checked == len(candidates):
                print(f"  [progress] Checked {checked}/{len(candidates)} markers...", flush=True)
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(_check_key, k) for k in candidates]
            for fut in as_completed(futures):
                res = fut.result()
                if res:
                    matched.append(res)
                checked += 1
                if checked % 100 == 0 or checked == len(candidates):
                    print(
                        f"  [progress] Checked {checked}/{len(candidates)} markers...",
                        flush=True,
                    )

    print(
        f"Inspected {len(candidates)} candidate markers, found {len(matched)} matching.",
        flush=True,
    )

    if not matched:
        print("Nothing to clear.", flush=True)
        return 0

    if not args.apply:
        for _key, r_hash, data in matched[:5]:
            err = str(data.get("last_error") or "")[:80]
            print(
                f"  [dry-run] {r_hash} task={data.get('task')} status={data.get('status')}: {err}",
                flush=True,
            )
        if len(matched) > 5:
            print(f"  ... and {len(matched) - 5} more", flush=True)
        print(
            f"\nDry-run complete. Run with --apply to clear all {len(matched)} failure markers.",
            flush=True,
        )
        return 0

    cleared = 0
    for _key, r_hash, _ in matched:
        if clear_terminal_failure(storage, r_hash):
            cleared += 1
    print(f"Successfully cleared {cleared}/{len(matched)} failure markers.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
