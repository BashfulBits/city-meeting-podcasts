"""Canonical shard plans for heavy workflow matrix jobs.

The ASR workflow plans once from the durable state snapshot restored by its reconcile job, uploads
that snapshot plus this plan as one immutable workflow artifact, and has every matrix shard consume
the same assignment. This prevents sibling jobs from deriving different ownership while durable
state, leases, or future external-GPU capacity change during the run. Audio/align remain
source-atomic; transcribe ownership is per episode with matching cross-source ASR work co-located.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from citypods.asr import asr_initial_prompt, asr_spec_hash
from citypods.models import City
from citypods.records import (
    AUDIO_UNKNOWN_DURATION_WEIGHT_SECONDS,
    estimate_audio_shard_work,
    load_records,
    pending_transcribe_items,
    record_to_episode,
    records_path,
    shard_assignment,
    source_family_key,
    source_key,
    transcript_media_hash,
)

# v2 adds the ``unit`` field: the transcribe lane now plans per ``(source, uid)`` episode rather
# than per source (review/18 §3.1). ``load_shard_plan`` rejects v1; reconcile emits a fresh plan
# every run, so there is no durable v1 artifact to migrate.
SHARD_PLAN_VERSION = 2

# Composite-key separator for episode-unit assignments: ``"<source_key>/<uid>"``. Safe because
# ``source_key`` is a 12-char hex hash and ``uid`` a 16-char hex string — neither contains a slash.
_EPISODE_KEY_SEP = "/"


def _episode_key(src_key: str, uid: str) -> str:
    return f"{src_key}{_EPISODE_KEY_SEP}{uid}"


def _split_episode_key(key: str) -> tuple[str, str]:
    src_key, _, uid = key.rpartition(_EPISODE_KEY_SEP)
    return src_key, uid


@dataclass(frozen=True)
class ShardPlan:
    lane: str
    num_shards: int
    assignment: dict[str, int]
    weights: dict[str, float]
    unit: str = "source"  # "source" (audio/align) | "episode" (transcribe; keys are source/uid)
    snapshot_fingerprint: str | None = None
    version: int = SHARD_PLAN_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "lane": self.lane,
            "unit": self.unit,
            "num_shards": self.num_shards,
            "assignment": dict(sorted(self.assignment.items())),
            "weights": dict(sorted(self.weights.items())),
            "snapshot_fingerprint": self.snapshot_fingerprint,
        }


_LANE_UNITS = {
    "audio": "source",
    "align": "source",
    "transcribe": "episode",
}


def _validate_lane_unit(lane: str, unit: str) -> None:
    expected = _LANE_UNITS.get(lane)
    if expected is None:
        raise ValueError(f"invalid shard-plan lane {lane!r}")
    if unit != expected:
        raise ValueError(
            f"invalid shard-plan unit {unit!r} for lane {lane!r}; expected {expected!r}"
        )


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
    if lane not in _LANE_UNITS:
        raise ValueError(f"unsupported shard-plan lane {lane!r}")
    if num_shards < 1:
        raise ValueError(f"num_shards must be >= 1, got {num_shards}")

    state_dir = Path(state_dir)
    source_city = {source_key(city): city for city in cities}
    max_kbps = int(defaults.get("audio_max_kbps", 96))
    loudness_profile = str(defaults.get("audio_loudness_profile", ""))
    processing_profile = str(defaults.get("audio_processing_profile", ""))
    local_max_hours = float(defaults.get("asr_local_max_duration_hours", 4))

    # transcribe plans per-episode (review/18 §3.1): spread one skewed source across all shards.
    # Each episode is independent GPU work with no per-source coupling, so the source is not the
    # right unit. audio/align stay source-atomic (per-source provider leases / rate limits, §2.3).
    if lane == "transcribe":
        # A stable meeting can appear in more than one configured source/feed view. ASR output is
        # identical when both the stable uid and recipe match, so charge that inference once and
        # co-locate every source-local record on one shard. TranscriptStage's run-local artifact
        # cache then fans the one result back out to each source-scoped object/record without
        # changing the durable blob layout.
        grouped: dict[str, list[tuple[str, float]]] = {}
        for key, city in source_city.items():
            if not records_path(state_dir, key).exists():
                continue
            records = load_records(state_dir, key)
            # CR2-CP-22: memoize per uid within this (key, city) iteration — the recipe is a
            # pure function of (ep, city) so the staleness predicate and the grouping key below
            # would otherwise each hash the same episode independently.
            recipe_cache: dict[str, str] = {}

            def _recipe_hash(ep, *, city=city, recipe_cache=recipe_cache) -> str:
                cached = recipe_cache.get(ep.uid)
                if cached is None:
                    cached = asr_spec_hash(
                        transcript_media_hash(ep),
                        city.asr_model,
                        None,
                        asr_pipeline_version,
                        language=city.asr_language or None,
                        compute_type=city.asr_compute_type,
                        beam_size=city.asr_beam_size,
                        initial_prompt=asr_initial_prompt(
                            city.podcast_author,
                            ep.body,
                            ep.title,
                        ),
                    )
                    recipe_cache[ep.uid] = cached
                return cached

            for uid, weight in pending_transcribe_items(
                state_dir,
                key,
                asr_enabled=city.asr_enabled,
                asr_pipeline_version=asr_pipeline_version,
                local_max_duration_hours=local_max_hours,
                transcript_stale=lambda ep: (
                    bool(ep.transcript_spec_hash) and ep.transcript_spec_hash != _recipe_hash(ep)
                ),
            ):
                ep = record_to_episode(records[uid])
                recipe = _recipe_hash(ep)
                grouped.setdefault(f"{uid}/{recipe}", []).append((_episode_key(key, uid), weight))

        group_weights = {
            work_key: max(weight for _episode, weight in members)
            for work_key, members in grouped.items()
        }
        group_assignment = shard_assignment(group_weights.keys(), num_shards, weights=group_weights)
        assignment: dict[str, int] = {}
        weights: dict[str, float] = {}
        for work_key, members in sorted(grouped.items()):
            owner = group_assignment[work_key]
            # Keep every episode key in the plan so each source-local record is owned and updated.
            # Only the deterministic first member carries the inference weight; aliases are free
            # fan-out writes from the shard-local result cache.
            for index, (episode_key, _weight) in enumerate(sorted(members)):
                assignment[episode_key] = owner
                weights[episode_key] = group_weights[work_key] if index == 0 else 0.0
        return ShardPlan(
            lane=lane,
            num_shards=num_shards,
            assignment=assignment,
            weights=weights,
            unit="episode",
            snapshot_fingerprint=state_snapshot_fingerprint(state_dir),
        )

    def _weight(key: str, city: City) -> float:
        if not records_path(state_dir, key).exists():
            return AUDIO_UNKNOWN_DURATION_WEIGHT_SECONDS if lane == "audio" else 1.0
        if lane == "audio":
            return float(
                estimate_audio_shard_work(
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
    if lane == "audio":
        # Distinct source keys for one entity must share a process so the run-local artifact cache
        # can coalesce meetings exposed by both a combined and a per-board provider view (GH#421).
        family_members: dict[str, list[str]] = {}
        for key, city in source_city.items():
            family_members.setdefault(source_family_key(city), []).append(key)
        family_weights = {
            family: sum(weights[key] for key in members)
            for family, members in family_members.items()
        }
        family_assignment = shard_assignment(family_members, num_shards, weights=family_weights)
        assignment = {
            key: family_assignment[family]
            for family, members in family_members.items()
            for key in members
        }
    else:
        assignment = shard_assignment(source_city, num_shards, weights=weights)
    return ShardPlan(
        lane=lane,
        num_shards=num_shards,
        assignment=assignment,
        weights=weights,
        unit="source",
        snapshot_fingerprint=state_snapshot_fingerprint(state_dir),
    )


def state_snapshot_fingerprint(state_dir: str | Path) -> str:
    """Fingerprint the canonical local planning snapshot used by a shard plan.

    The manifest is the compact canonical input produced by state sync. The fallback keeps local
    development and older snapshots safe by hashing the source record files directly.
    """
    state_dir = Path(state_dir)
    manifest = state_dir / "catalog" / "manifest.json"
    digest = hashlib.sha256()
    if manifest.is_file():
        digest.update(manifest.read_bytes())
        return digest.hexdigest()
    for path in sorted(state_dir.glob("sources/*/episodes.json")):
        digest.update(path.relative_to(state_dir).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def matrix_for_plan(plan: ShardPlan) -> dict[str, list[int]]:
    """Return the dynamic Audio matrix, omitting shards with no actionable work."""
    loads = [0.0] * plan.num_shards
    for key, owner in plan.assignment.items():
        loads[owner] += plan.weights[key]
    return {"shard": [index for index, load in enumerate(loads) if load > 0]}


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
        unit = str(data.get("unit", "source"))
        num_shards = int(data["num_shards"])
        assignment = {str(key): int(value) for key, value in data["assignment"].items()}
        weights = {str(key): float(value) for key, value in data["weights"].items()}
        snapshot_fingerprint = data.get("snapshot_fingerprint")
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"invalid shard plan {path}: {exc}") from exc
    if lane not in {"audio", "transcribe", "align"}:
        raise ValueError(f"invalid shard-plan lane {lane!r}")
    if unit not in {"source", "episode"}:
        raise ValueError(f"invalid shard-plan unit {unit!r}")
    _validate_lane_unit(lane, unit)
    if num_shards < 1:
        raise ValueError(f"invalid shard count {num_shards}")
    if set(assignment) != set(weights):
        raise ValueError("shard-plan assignment and weight key sets differ")
    if any(shard < 0 or shard >= num_shards for shard in assignment.values()):
        raise ValueError("shard-plan assignment contains an out-of-range shard")
    return ShardPlan(
        lane=lane,
        num_shards=num_shards,
        assignment=assignment,
        weights=weights,
        unit=unit,
        snapshot_fingerprint=(str(snapshot_fingerprint) if snapshot_fingerprint else None),
        version=version,
    )


def episodes_for_shard(
    plan: ShardPlan,
    *,
    lane: str,
    shard_index: int,
    num_shards: int,
    expected_sources: set[str],
) -> tuple[set[str], dict[str, frozenset[str]] | None]:
    """Validate a plan against this checkout/config and return one shard's ownership.

    Returns ``(owned_sources, owned_uids)``:
      * ``owned_sources`` — the source keys this shard must load (for an episode-unit plan, every
        source for which this shard owns at least one uid).
      * ``owned_uids`` — for an **episode**-unit (transcribe) plan, ``{source: frozenset(uids)}``
        restricting which uids this shard transcribes/writes; **None** for a **source**-unit
        (audio/align) plan, meaning "own every uid in each owned source" (today's behavior).

    Fail closed rather than silently recomputing: fallback computation in each matrix job would
    recreate the divergent-ownership race this artifact exists to eliminate.
    """
    if plan.lane != lane:
        raise ValueError(f"shard plan is for lane {plan.lane!r}, not {lane!r}")
    _validate_lane_unit(plan.lane, plan.unit)
    if plan.num_shards != num_shards:
        raise ValueError(
            f"shard plan has {plan.num_shards} shards, workflow requested {num_shards}"
        )
    if not (0 <= shard_index < num_shards):
        raise ValueError(f"shard index {shard_index} out of range for {num_shards} shards")

    if plan.unit == "episode":
        # Pending-only plan: every assigned uid's source must be configured, but a caught-up source
        # legitimately has no entries — so check subset (no unknown source), not equality.
        planned_sources = {_split_episode_key(key)[0] for key in plan.assignment}
        extra = sorted(planned_sources - expected_sources)
        if extra:
            raise ValueError(f"shard plan references unconfigured sources (extra={extra})")
        owned_uids: dict[str, set[str]] = {}
        for key, owner in plan.assignment.items():
            if owner != shard_index:
                continue
            src_key, uid = _split_episode_key(key)
            owned_uids.setdefault(src_key, set()).add(uid)
        return set(owned_uids), {src: frozenset(uids) for src, uids in owned_uids.items()}

    planned_sources = set(plan.assignment)
    if planned_sources != expected_sources:
        missing = sorted(expected_sources - planned_sources)
        extra = sorted(planned_sources - expected_sources)
        raise ValueError(
            "shard plan source set does not match configured sources "
            f"(missing={missing}, extra={extra})"
        )
    return {key for key, owner in plan.assignment.items() if owner == shard_index}, None
