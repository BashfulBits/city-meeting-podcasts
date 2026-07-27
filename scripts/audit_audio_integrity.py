#!/usr/bin/env python
"""Run one bounded partition of the trusted-audio pointer audit (GH#1024).

The normal audio lane trusts a matching immutable pointer after it has been verified once. This
maintenance pass provides the bounded backstop for deletion, incomplete-upload, and storage-drift
cases without listing every audio prefix. The caller persists only sources whose audit found a
repair, and the next Audio lane sees the cleared completion marker as dirty work.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from citypods.config import load_city_configs, load_site_config
from citypods.media import audit_verified_audio
from citypods.records import (
    episode_to_record,
    load_records,
    record_to_episode,
    save_records,
    source_key,
)
from citypods.state import resolve_state_dir
from citypods.statesync import pull_state, push_state
from citypods.storage import make_storage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--site-config", default="config/site_config.yml")
    parser.add_argument("--output-dir", default=".citypods-state")
    parser.add_argument("--partition", type=int)
    parser.add_argument("--partitions", type=int, default=32)
    parser.add_argument("--max-items", type=int, default=100)
    parser.add_argument("--pull-state", action="store_true")
    parser.add_argument("--push-state", action="store_true")
    args = parser.parse_args(argv)
    if args.partitions < 1 or args.max_items < 0:
        parser.error("--partitions must be positive and --max-items must be non-negative")

    site_config = load_site_config(args.site_config)
    state_dir = resolve_state_dir(site_config, args.output_dir)
    storage = make_storage(site_config, "https://example.invalid", Path(args.output_dir))
    if storage is None:
        print("audio integrity: storage unavailable; nothing to audit")
        return 0
    if args.pull_state:
        pull_state(storage, state_dir)

    partition = (
        args.partition
        if args.partition is not None
        else datetime.now(UTC).toordinal() % args.partitions
    )
    if not 0 <= partition < args.partitions:
        parser.error("--partition must be within [0, --partitions)")

    cities = load_city_configs(args.config_dir, site_config.get("defaults", {}))
    checked = missing = sources_changed = 0
    for city in cities:
        src = source_key(city)
        raw = load_records(state_dir, src)
        if not raw:
            continue
        episodes = [record_to_episode(record) for record in raw.values()]
        before = {ep.uid: ep.audio_verification.copy() for ep in episodes if ep.uid}
        source_checked, source_missing = audit_verified_audio(
            episodes,
            storage,
            partition=partition,
            partitions=args.partitions,
            max_items=args.max_items,
        )
        checked += source_checked
        missing += source_missing
        if any(before.get(ep.uid) != ep.audio_verification for ep in episodes if ep.uid):
            save_records(
                state_dir,
                src,
                {ep.uid: episode_to_record(ep) for ep in episodes if ep.uid},
            )
            sources_changed += 1

    pushed = push_state(storage, state_dir) if args.push_state and sources_changed else 0
    print(
        f"audio integrity: partition={partition}/{args.partitions} checked={checked} "
        f"missing={missing} sources_changed={sources_changed} pushed={pushed}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
