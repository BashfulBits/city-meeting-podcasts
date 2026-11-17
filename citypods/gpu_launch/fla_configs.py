"""Prebaked ``flash-linear-attention`` autotune configs from a Triton autotune cache.

fla can load a tuned config per kernel from ``$FLA_CONFIG_DIR/<kernel>.json`` instead of
benchmarking every candidate (``FLA_CACHE_MODE=fuzzy`` also applies a config tuned for one numeric
key value, such as a different ``NB`` or batch row count, to any other). A miss then costs one
compile rather than an autotune sweep of 4-30 compiles plus benchmarks. This module derives those
files from the ``*.autotune.json`` results a warm-up run leaves in the Triton cache, so they ship
inside the same pinned archive.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SUFFIX = ".autotune.json"
DIRNAME = "_fla_configs"
REQUIRED = ("kwargs", "num_warps", "num_stages", "num_ctas")


def _canonical(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def key_hash(key: list[Any]) -> str:
    """fla's lookup hash: md5 of the canonical JSON of the autotune key."""
    return hashlib.md5(_canonical(key).encode()).hexdigest()  # noqa: S324 - fla's own scheme


def best_config(document: Mapping[str, Any]) -> dict[str, Any]:
    """The config with the lowest mean benchmark time; ties keep the earliest candidate."""
    candidates = document["configs_timings"]
    if not candidates:
        raise ValueError("autotune record has no timed configs")
    config, _ = min(candidates, key=lambda c: statistics.fmean(c[1]))
    if any(name not in config for name in REQUIRED):
        raise ValueError("autotune config lacks required fields")
    return config


def configs_from_triton_cache(
    cache_dir: Path, *, triton_version: str | None = None
) -> dict[str, dict[str, Any]]:
    """Group every autotune record by kernel into an fla ``<kernel>.json`` document."""
    entries: dict[str, dict[str, Any]] = {}
    for path in sorted(Path(cache_dir).rglob(f"*{SUFFIX}")):
        kernel = path.name[: -len(SUFFIX)]
        document = json.loads(path.read_text())
        key = document["key"]
        entries.setdefault(kernel, {})[key_hash(key)] = {
            "autotune_key": key,
            "config": best_config(document),
        }
    result = {}
    for kernel, table in sorted(entries.items()):
        votes = Counter(_canonical(entry["config"]) for entry in table.values())
        top = max(votes.values())
        default = min(c for c, n in votes.items() if n == top)  # deterministic tie-break
        result[kernel] = {
            "kernel_name": kernel,
            "triton_version": triton_version,
            "autotune_entries": dict(sorted(table.items())),
            "default_config": json.loads(default),
        }
    return result


def write_configs(configs: Mapping[str, Mapping[str, Any]], directory: Path) -> int:
    """Write one deterministic JSON file per kernel; returns the file count."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for kernel, document in configs.items():
        (directory / f"{kernel}.json").write_text(
            json.dumps(document, sort_keys=True, indent=1) + "\n"
        )
    return len(configs)
