#!/usr/bin/env python
"""Pull only episode record stores needed for the generated-chapter research dataset.

This is deliberately narrower than ``statesync.pull_state``: it reads configured
``state/sources/*/episodes.json`` objects from B2 and writes a disposable local research snapshot.
It never uploads, deletes, or changes durable state.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from citypods.config import load_city_configs, load_site_config
from citypods.records import source_key
from citypods.storage.s3 import b2_from_env


@dataclass(frozen=True)
class PullResult:
    requested: int
    restored: int
    missing: int


def configured_record_keys(cities) -> tuple[str, ...]:
    """Return deterministic B2 keys for every currently configured source record store."""
    return tuple(sorted({f"state/sources/{source_key(city)}/episodes.json" for city in cities}))


def pull_records(
    storage, *, keys: tuple[str, ...], output_state_dir: Path, dry_run: bool
) -> PullResult:
    """Restore only listed record stores; absent sources are ordinary and remain local omissions."""
    restored = 0
    missing = 0
    for key in keys:
        destination = output_state_dir / key.removeprefix("state/")
        if dry_run:
            print(f"would restore {key}")
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if storage.get_file(key, destination):
            restored += 1
            print(f"restored {key}")
        else:
            missing += 1
            print(f"missing {key}")
    return PullResult(requested=len(keys), restored=restored, missing=missing)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-state-dir", type=Path, required=True)
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--site-config", default="config/site_config.yml")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    site = load_site_config(args.site_config)
    cities = load_city_configs(args.config_dir, site.get("defaults", {}))
    storage = b2_from_env()
    if storage is None:
        raise RuntimeError("B2 storage is not configured")
    result = pull_records(
        storage,
        keys=configured_record_keys(cities),
        output_state_dir=args.output_state_dir,
        dry_run=args.dry_run,
    )
    print(
        f"alignment-records: requested={result.requested} restored={result.restored} "
        f"missing={result.missing} dry_run={args.dry_run}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
