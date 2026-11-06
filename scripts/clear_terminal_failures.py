#!/usr/bin/env python
"""Clear terminal-failure audit markers so blocked recipe lineages may re-run cleanly.

When a stage fails repeatedly (e.g. hitting a schema validation error before a fix was deployed),
the runner writes a failure marker under ``state/llm_deferred_failures/<recipe_hash>.json``
with status ``exhausted`` or ``structural_blocked``. The engine blocks all future attempts for
that recipe until the failure marker is cleared or a new generation is introduced.

Usage:
    # Dry-run inspect matching tag failure markers:
    python scripts/clear_terminal_failures.py --task tag

    # Apply deletion of tag failure markers from object storage:
    python scripts/clear_terminal_failures.py --task tag --apply

    # Clear a single specific recipe hash:
    python scripts/clear_terminal_failures.py --recipe-hash <hash> --apply
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping

from citypods.compute.llm_deferred import (
    DEFERRED_FAILURE_PREFIX,
    _read_json,
    clear_terminal_failure,
    deferred_failure_key,
)
from citypods.config import load_site_config
from citypods.storage import make_storage


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
        data = _read_json(storage, key)
        if not data:
            print(f"No failure marker found for recipe {args.recipe_hash}")
            return 0
        task = data.get("task")
        status = data.get("status")
        print(f"Found marker for {args.recipe_hash}: task={task} status={status}")
        if args.apply:
            ok = clear_terminal_failure(storage, args.recipe_hash)
            print(f"Cleared marker for {args.recipe_hash}: {ok}")
        else:
            print("Dry-run: pass --apply to delete this marker")
        return 0

    print(
        f"Scanning {DEFERRED_FAILURE_PREFIX} for task='{args.task}' "
        f"statuses={sorted(target_statuses)}..."
    )
    listing = storage.list_objects(DEFERRED_FAILURE_PREFIX)
    examined = 0
    matched: list[tuple[str, str, dict]] = []

    for key, _ in listing:
        examined += 1
        data = _read_json(storage, key)
        if not isinstance(data, Mapping):
            continue
        task = data.get("task")
        status = data.get("status")
        if args.task != "all" and task != args.task:
            continue
        if target_statuses and status not in target_statuses:
            continue
        recipe_hash = data.get("recipe_hash") or key.removeprefix(
            DEFERRED_FAILURE_PREFIX
        ).removesuffix(".json")
        matched.append((key, recipe_hash, dict(data)))

    print(f"Examined {examined} markers, found {len(matched)} matching.")

    if not matched:
        print("Nothing to clear.")
        return 0

    if not args.apply:
        for _key, r_hash, data in matched[:5]:
            err = str(data.get("last_error") or "")[:80]
            print(
                f"  [dry-run] {r_hash} task={data.get('task')} status={data.get('status')}: {err}"
            )
        if len(matched) > 5:
            print(f"  ... and {len(matched) - 5} more")
        print(f"\nDry-run complete. Run with --apply to clear all {len(matched)} failure markers.")
        return 0

    cleared = 0
    for _key, r_hash, _ in matched:
        if clear_terminal_failure(storage, r_hash):
            cleared += 1
    print(f"Successfully cleared {cleared}/{len(matched)} failure markers.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
