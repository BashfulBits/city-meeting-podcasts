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

    # Clear only the markers whose failure a shipped fix addresses (see the cause histogram that
    # every run prints first), for any task: tag, agenda-item-extract, agenda-chapter-locate,
    # moment-extraction, or 'all':
    python scripts/clear_terminal_failures.py --task agenda-chapter-locate \\
        --last-error-contains "duplicate locator unit" --apply

Clearing a marker only buys three more attempts. A marker whose cause is still present (a model
that keeps returning an unciteable quote, an input no route admits) is exhausted again after
three more paid jobs, so clear by cause, not by task. Every cleared marker is archived under
``state/llm_failure_marker_audits/<date>/`` first.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from typing import Any

from citypods.compute.llm_deferred import (
    DEFERRED_FAILURE_PREFIX,
    _deferred_record_lock,
    _read_json,
    _write_json,
    clear_terminal_failure,
    deferred_failure_key,
)
from citypods.config import load_site_config
from citypods.storage import make_storage

AUDIT_PREFIX = "state/llm_failure_marker_audits/"
_VARIABLE = re.compile(r"[0-9a-fA-F]{8,}|\d+")


def cause_of(marker: Mapping[str, Any]) -> str:
    """A stable, countable description of why a marker exists (ids and numbers collapsed)."""
    error = _VARIABLE.sub("#", str(marker.get("last_error") or ""))[:110]
    detail = marker.get("failure_class") or marker.get("terminal_reason") or ""
    return f"{marker.get('task')} | {marker.get('status')} | {detail} | {error}"


def print_inventory(matched: list[tuple[str, str, dict[str, Any]]]) -> None:
    """Group matching markers by cause so the operator can clear only a fixed cause."""
    causes = Counter(cause_of(data) for _key, _hash, data in matched)
    print("\nMarkers by cause (task | status | class | error):", flush=True)
    for cause, count in causes.most_common():
        print(f"  {count:>5}  {cause}", flush=True)
    print(flush=True)


def clear_with_audit(
    storage: Any,
    recipe_hash: str,
    *,
    expected: Mapping[str, Any] | None = None,
    date: str | None = None,
) -> str:
    """Archive the marker, then clear it, under the record lock. Returns the outcome.

    ``expected`` is the marker as it was when selected. A sweep can replace it between the scan and
    the lock, so the live marker must still equal it or nothing is archived or deleted. Storage
    read failures are outcomes of their own: an unreadable archive is never treated as absent, which
    would overwrite its history.
    """
    day = date or datetime.now(UTC).date().isoformat()
    with _deferred_record_lock(storage, recipe_hash):
        try:
            marker = _read_json(storage, deferred_failure_key(recipe_hash))
        except Exception:
            return "marker-read-failed"
        if not isinstance(marker, Mapping):
            return "missing-marker"
        if expected is not None and dict(marker) != dict(expected):
            return "marker-changed"
        audit_key = f"{AUDIT_PREFIX}{day}/{recipe_hash}.json"
        try:
            existing = _read_json(storage, audit_key)
        except Exception:
            return "audit-read-failed"
        if existing is not None and dict(existing) != dict(marker):
            return "audit-conflict"
        if existing is None:
            _write_json(storage, audit_key, json.dumps(dict(marker), sort_keys=True).encode())
        return "cleared" if clear_terminal_failure(storage, recipe_hash) else "clear-failed"


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
        "--last-error-contains",
        default="",
        help="only markers whose last_error contains this text (clear by cause)",
    )
    parser.add_argument(
        "--max-clear",
        type=int,
        default=300,
        help="refuse --apply when more markers than this match (default: 300)",
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
        try:
            data = _read_json(storage, key)
        except Exception as exc:  # noqa: BLE001 -- unreadable is not the same as absent
            print(f"Could not read the marker for {args.recipe_hash}: {exc}", flush=True)
            return 1
        if not data:
            print(f"No failure marker found for recipe {args.recipe_hash}", flush=True)
            return 0
        task = data.get("task")
        status = data.get("status")
        print(f"Found marker for {args.recipe_hash}: task={task} status={status}", flush=True)
        if args.last_error_contains and args.last_error_contains not in str(
            data.get("last_error") or ""
        ):
            print(
                f"Marker for {args.recipe_hash} does not match --last-error-contains; "
                "nothing to clear.",
                flush=True,
            )
            return 0
        if args.apply:
            outcome = clear_with_audit(storage, args.recipe_hash, expected=data)
            print(f"Marker for {args.recipe_hash}: {outcome}", flush=True)
            return 0 if outcome == "cleared" else 1
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
        if args.last_error_contains and args.last_error_contains not in str(
            data.get("last_error") or ""
        ):
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

    print_inventory(matched)

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

    if len(matched) > args.max_clear:
        print(
            f"Refusing to clear {len(matched)} markers (> --max-clear {args.max_clear}). Narrow "
            "with --last-error-contains or --task, or raise --max-clear deliberately.",
            flush=True,
        )
        return 1
    outcomes = Counter(
        clear_with_audit(storage, r_hash, expected=data) for _key, r_hash, data in matched
    )
    print(f"Cleared {outcomes['cleared']}/{len(matched)} failure markers: {dict(outcomes)}.")
    return 0 if outcomes["cleared"] == len(matched) else 1


if __name__ == "__main__":
    sys.exit(main())
