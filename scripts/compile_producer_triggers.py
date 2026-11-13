#!/usr/bin/env python3
"""Compile ``config/producer_triggers.yml`` into the v2 Worker's producer trigger table.

Writes ``workers/llm-dispatch-v2/src/producer_triggers.json``. Deterministic, offline and runnable
with nothing but PyYAML installed -- the same contract and ``--check`` drift gate as
``scripts/compile_dispatch_tuning.py`` (``llm-dispatch-v2-worker-deploy.yml``).

review/58 WP6 part B: the Worker's per-minute cron fires ``repository_dispatch`` for the LLM
producer workflows on the minutes listed here. The compiled table expands every entry to an
explicit list of UTC hours so the Worker only compares the tick's minute and hour.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
INPUT_YAML = REPO_ROOT / "config" / "producer_triggers.yml"
TUNING_YAML = REPO_ROOT / "config" / "dispatch_tuning.yml"
OUTPUT_JSON = REPO_ROOT / "workers" / "llm-dispatch-v2" / "src" / "producer_triggers.json"

# GitHub accepts any string up to 100 characters; keep it to a plain slug that is safe to log.
_EVENT_TYPE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_WORKFLOW = re.compile(r"^[a-z0-9][a-z0-9-]*\.yml$")
_ENTRY_KEYS = {
    "event_type",
    "workflow",
    "minute",
    "hours",
    "every_n_hours",
    "hour_offset",
    "schedule_guard_minutes",
}
# Worker-side ceilings: a tick's subrequest and connection budget is shared with dispatch.
MAX_TRIGGERS_PER_TICK_CEILING = 3
MAX_TIMEOUT_SECONDS = 10


def _display(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _int(path: Path, where: str, value: object, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise SystemExit(f"{path}: {where} must be an integer in [{low}, {high}], got {value!r}")
    return value


def cleanup_interval_minutes(path: Path = TUNING_YAML) -> int:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return int(raw.get("CLEANUP_INTERVAL_MINUTES", 10))


def _hours(path: Path, where: str, entry: dict) -> list[int]:
    has_list = "hours" in entry
    has_every = "every_n_hours" in entry
    if has_list == has_every:
        raise SystemExit(f"{path}: {where} needs exactly one of 'hours' or 'every_n_hours'")
    if has_list:
        if "hour_offset" in entry:
            raise SystemExit(f"{path}: {where} 'hour_offset' only applies to 'every_n_hours'")
        raw = entry["hours"]
        if not isinstance(raw, list) or not raw:
            raise SystemExit(f"{path}: {where} 'hours' must be a non-empty list")
        hours = [_int(path, f"{where} hours[]", h, 0, 23) for h in raw]
        if len(set(hours)) != len(hours):
            raise SystemExit(f"{path}: {where} 'hours' repeats an hour")
        return sorted(hours)
    every = _int(path, f"{where} every_n_hours", entry["every_n_hours"], 1, 24)
    if 24 % every:
        raise SystemExit(f"{path}: {where} every_n_hours must divide 24, got {every}")
    offset = _int(path, f"{where} hour_offset", entry.get("hour_offset", 0), 0, every - 1)
    return list(range(offset, 24, every))


def _min_gap_minutes(hours: list[int]) -> int:
    """Smallest gap between consecutive firings, wrapping past midnight."""
    if len(hours) == 1:
        return 24 * 60
    wrapped = [*hours, hours[0] + 24]
    return min(b - a for a, b in zip(wrapped, wrapped[1:], strict=False)) * 60


def load_triggers(path: Path = INPUT_YAML, *, cleanup_interval: int | None = None) -> dict:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise SystemExit(f"{path} must be a mapping")
    unknown = set(raw) - {"max_triggers_per_tick", "timeout_seconds", "triggers"}
    if unknown:
        raise SystemExit(f"{path}: unknown top-level keys {sorted(unknown)}")
    cap = _int(
        path,
        "max_triggers_per_tick",
        raw.get("max_triggers_per_tick"),
        1,
        MAX_TRIGGERS_PER_TICK_CEILING,
    )
    timeout = _int(path, "timeout_seconds", raw.get("timeout_seconds"), 1, MAX_TIMEOUT_SECONDS)
    interval = cleanup_interval if cleanup_interval is not None else cleanup_interval_minutes()
    entries = raw.get("triggers")
    if not isinstance(entries, list) or not entries:
        raise SystemExit(f"{path}: 'triggers' must be a non-empty list")

    compiled: list[dict] = []
    seen: set[str] = set()
    per_minute: dict[tuple[int, int], list[str]] = {}
    for index, entry in enumerate(entries):
        where = f"triggers[{index}]"
        if not isinstance(entry, dict):
            raise SystemExit(f"{path}: {where} must be a mapping")
        extra = set(entry) - _ENTRY_KEYS
        if extra:
            raise SystemExit(f"{path}: {where} has unknown keys {sorted(extra)}")
        event_type = entry.get("event_type")
        if not isinstance(event_type, str) or not _EVENT_TYPE.match(event_type):
            raise SystemExit(f"{path}: {where} event_type {event_type!r} is not a plain slug")
        if event_type in seen:
            raise SystemExit(f"{path}: {where} repeats event_type {event_type}")
        seen.add(event_type)
        workflow = entry.get("workflow")
        if not isinstance(workflow, str) or not _WORKFLOW.match(workflow):
            raise SystemExit(f"{path}: {where} workflow {workflow!r} is not a .yml file name")
        minute = _int(path, f"{where} minute", entry.get("minute"), 0, 59)
        if interval > 0 and minute % interval == 0:
            raise SystemExit(
                f"{path}: {where} minute {minute} is a cleanup minute "
                f"(CLEANUP_INTERVAL_MINUTES {interval}); pick another"
            )
        hours = _hours(path, where, entry)
        gap = _min_gap_minutes(hours)
        # Validated only: each workflow's guard step carries it (tests/test_workflows.py).
        _int(
            path, f"{where} schedule_guard_minutes", entry.get("schedule_guard_minutes"), 1, gap - 1
        )
        for hour in hours:
            per_minute.setdefault((hour, minute), []).append(event_type)
        compiled.append(
            {"event_type": event_type, "workflow": workflow, "minute": minute, "hours": hours}
        )

    for (hour, minute), names in sorted(per_minute.items()):
        if len(names) > cap:
            raise SystemExit(
                f"{path}: {len(names)} triggers fall on {hour:02d}:{minute:02d} UTC "
                f"({', '.join(names)}), above max_triggers_per_tick {cap}"
            )
    return {"max_triggers_per_tick": cap, "timeout_seconds": timeout, "triggers": compiled}


def render(table: dict) -> str:
    compiled = {
        "_generated_by": "scripts/compile_producer_triggers.py",
        "_source": "config/producer_triggers.yml",
        "_readme": (
            "Do not edit by hand. review/58 WP6 part B: the Worker cron fires repository_dispatch "
            "for each entry whose minute and UTC hour match the tick's scheduledTime."
        ),
        **table,
    }
    return json.dumps(compiled, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="Exit non-zero if the committed JSON is stale."
    )
    args = parser.parse_args(argv)
    table = load_triggers()
    rendered = render(table)
    if args.check:
        current = OUTPUT_JSON.read_text(encoding="utf-8") if OUTPUT_JSON.exists() else ""
        if current != rendered:
            print(
                f"::error::{_display(OUTPUT_JSON)} is out of date -- recompile with "
                "'python scripts/compile_producer_triggers.py' and commit the YAML and JSON "
                "together.",
                file=sys.stderr,
            )
            return 1
        print(f"{_display(OUTPUT_JSON)} is up to date ({len(table['triggers'])} triggers)")
        return 0
    OUTPUT_JSON.write_text(rendered, encoding="utf-8")
    print(f"wrote {_display(OUTPUT_JSON)}: {len(table['triggers'])} triggers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
