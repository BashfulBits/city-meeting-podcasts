#!/usr/bin/env python
"""Run one rotating partition of the trusted-audio pointer audit (GH#1024).

The normal audio lane trusts a matching immutable pointer after it has been verified once. This
maintenance pass provides the backstop for deletion, incomplete-upload, and storage-drift cases
without listing every audio prefix. Each run sweeps *all* trusted pointers in one of
``--partitions`` stable hash slices of the catalog (today's slice by default), so the whole
catalog gets one complete sweep every ``--partitions`` runs (32 partitions x daily cron = monthly)
regardless of how large the catalog grows -- there is no per-run item cap. Instead, a wall-clock
budget bounds run time directly: once spent, remaining sources are skipped for this run (not
failed) and get their turn next time this partition comes up. The caller persists only sources
whose audit found a repair, and the next Audio lane sees the cleared completion marker as dirty
work.
"""

from __future__ import annotations

import argparse
import signal
from datetime import UTC, datetime, timedelta
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


class _StopState:
    """Signal-safe stop flag checked between sources."""

    requested = False


def _install_signal_handlers() -> _StopState:
    stop_state = _StopState()

    def _request_stop(_signum, _frame):
        stop_state.requested = True

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)
    return stop_state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--site-config", default="config/site_config.yml")
    parser.add_argument("--output-dir", default=".citypods-state")
    parser.add_argument("--partition", type=int)
    parser.add_argument("--partitions", type=int, default=32)
    parser.add_argument("--max-workers", type=int, default=8)
    parser.add_argument(
        "--run-time-budget-minutes",
        type=float,
        default=300.0,
        help="Wall-clock budget for this run; remaining sources are skipped (not failed) once "
        "spent, and get their turn the next time this partition comes up. Default leaves margin "
        "under GitHub Actions' 6h job limit.",
    )
    parser.add_argument("--pull-state", action="store_true")
    parser.add_argument("--push-state", action="store_true")
    args = parser.parse_args(argv)
    if args.partitions < 1:
        parser.error("--partitions must be positive")
    if args.run_time_budget_minutes <= 0:
        parser.error("--run-time-budget-minutes must be positive")

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

    stop_state = _install_signal_handlers()
    deadline_at = datetime.now(UTC) + timedelta(minutes=args.run_time_budget_minutes)

    cities = load_city_configs(args.config_dir, site_config.get("defaults", {}))
    checked = missing = sources_changed = sources_skipped = 0
    for city in cities:
        if stop_state.requested or datetime.now(UTC) >= deadline_at:
            sources_skipped += 1
            continue
        src = source_key(city)
        try:
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
                max_workers=args.max_workers,
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
        except Exception as exc:  # noqa: BLE001 - one bad source shouldn't sink the whole audit
            print(f"audio integrity: source={src} failed: {exc}", flush=True)

    pushed = push_state(storage, state_dir) if args.push_state and sources_changed else 0
    print(
        f"audio integrity: partition={partition}/{args.partitions} checked={checked} "
        f"missing={missing} sources_changed={sources_changed} sources_skipped={sources_skipped} "
        f"pushed={pushed}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
