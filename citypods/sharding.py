"""Canonical source-atomic shard plans for heavy workflow matrix jobs.

The ASR workflow plans once from the durable state snapshot restored by its reconcile job, uploads
that snapshot plus this plan as one immutable workflow artifact, and has every matrix shard consume
the same assignment. This prevents sibling jobs from deriving different ownership while durable
state, leases, or future external-GPU capacity change during the run.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from citypods.models import City
from citypods.records import (
    estimate_transcribe_shard_work,
    pending_audio_work,
    records_path,
    shard_assignment,
    source_key,
)

SHARD_PLAN_VERSION = 1


@dataclass(frozen=True)
class ShardPlan:
    lane: str
    num_shards: int
    assignment: dict[str, int]
    weights: dict[str, float]
    version: int = SHARD_PLAN_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "lane": self.lane,
            "num_shards": self.num_shards,
            "assignment": dict(sorted(self.assignment.items())),
            "weights": dict(sorted(self.weights.items())),
        }


def create_shard_plan(
    cities: Sequence[City],
    state_dir: str | Path,
    *,
    lane: str,
    num_shards: int,
    defaults: Mapping[str, Any],
    asr_pipeline_version: str,
) -> ShardPlan:
    """Create one deterministic source ownership plan from one restored state snapshot."""
    if lane not in {"audio", "transcribe", "align"}:
        raise ValueError(f"unsupported shard-plan lane {lane!r}")
    if num_shards < 1:
        raise ValueError(f"num_shards must be >= 1, got {num_shards}")

    state_dir = Path(state_dir)
    source_city = {source_key(city): city for city in cities}
    max_kbps = int(defaults.get("audio_max_kbps", 96))
    loudness_profile = str(defaults.get("audio_loudness_profile", ""))
    processing_profile = str(defaults.get("audio_processing_profile", ""))
    local_max_hours = float(defaults.get("asr_local_max_duration_hours", 4))

    def _weight(key: str, city: City) -> float:
        if not records_path(state_dir, key).exists():
            return 1.0
        if lane == "transcribe":
            return estimate_transcribe_shard_work(
                state_dir,
                key,
                asr_enabled=city.asr_enabled,
                asr_pipeline_version=asr_pipeline_version,
                local_max_duration_hours=local_max_hours,
            ).shard_weight()
        if lane == "audio":
            return float(
                pending_audio_work(
                    state_dir,
                    key,
                    extract_audio=city.extract_audio,
                    max_kbps=max_kbps,
                    loudness_profile=loudness_profile,
                    processing_profile=processing_profile,
                )
            )
        # The align lane is currently unscheduled. Preserve deterministic source-count balancing
        # until its trust/routing policy can provide an actionable per-source estimate.
        return 1.0

    weights = {key: _weight(key, city) for key, city in source_city.items()}
    assignment = shard_assignment(source_city, num_shards, weights=weights)
    return ShardPlan(
        lane=lane,
        num_shards=num_shards,
        assignment=assignment,
        weights=weights,
    )


def save_shard_plan(path: str | Path, plan: ShardPlan) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan.to_dict(), indent=2, sort_keys=True) + "\n")


def load_shard_plan(path: str | Path) -> ShardPlan:
    path = Path(path)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError(f"unable to read shard plan {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"shard plan {path} must contain a JSON object")
    version = data.get("version")
    if version != SHARD_PLAN_VERSION:
        raise ValueError(
            f"unsupported shard plan version {version!r}; expected {SHARD_PLAN_VERSION}"
        )
    try:
        lane = str(data["lane"])
        num_shards = int(data["num_shards"])
        assignment = {str(key): int(value) for key, value in data["assignment"].items()}
        weights = {str(key): float(value) for key, value in data["weights"].items()}
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"invalid shard plan {path}: {exc}") from exc
    if lane not in {"audio", "transcribe", "align"}:
        raise ValueError(f"invalid shard-plan lane {lane!r}")
    if num_shards < 1:
        raise ValueError(f"invalid shard count {num_shards}")
    if set(assignment) != set(weights):
        raise ValueError("shard-plan assignment and weight source sets differ")
    if any(shard < 0 or shard >= num_shards for shard in assignment.values()):
        raise ValueError("shard-plan assignment contains an out-of-range shard")
    return ShardPlan(
        lane=lane,
        num_shards=num_shards,
        assignment=assignment,
        weights=weights,
        version=version,
    )


def sources_for_shard(
    plan: ShardPlan,
    *,
    lane: str,
    shard_index: int,
    num_shards: int,
    expected_sources: set[str],
) -> set[str]:
    """Validate a plan against this checkout/config and return one shard's owned source keys.

    Fail closed rather than silently recomputing: fallback computation in each matrix job would
    recreate the divergent-ownership race this artifact exists to eliminate.
    """
    if plan.lane != lane:
        raise ValueError(f"shard plan is for lane {plan.lane!r}, not {lane!r}")
    if plan.num_shards != num_shards:
        raise ValueError(
            f"shard plan has {plan.num_shards} shards, workflow requested {num_shards}"
        )
    if not (0 <= shard_index < num_shards):
        raise ValueError(f"shard index {shard_index} out of range for {num_shards} shards")
    planned_sources = set(plan.assignment)
    if planned_sources != expected_sources:
        missing = sorted(expected_sources - planned_sources)
        extra = sorted(planned_sources - expected_sources)
        raise ValueError(
            "shard plan source set does not match configured sources "
            f"(missing={missing}, extra={extra})"
        )
    return {key for key, owner in plan.assignment.items() if owner == shard_index}
