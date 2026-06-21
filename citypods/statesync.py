"""Durable build state: make the object bucket the source of truth, not ``actions/cache``.

The record store (``state/sources/<src>/episodes.json``) and the change-detection cache hold
**derived, expensive-to-recompute** data — hosted-audio provenance today, and transcripts /
summaries soon. Previously these lived only in ``.citypods-state`` restored via
``actions/cache``, which GitHub silently evicts after ~7 days of no hits or when the 10 GB
repo-cache limit is reached. On eviction that derived state is simply gone: re-encoding and
(future) re-transcribing cost real money.

This module mirrors ``state_dir`` to/from a ``state/`` prefix in the **same bucket** that hosts
the audio. The bucket is durable and already configured, so:

  * ``pull_state`` (build start) overwrites the local copy from the bucket — the bucket is
    canonical, so a cache miss/eviction self-heals on the next run.
  * ``push_state`` (build end) uploads every state file back.

``actions/cache`` then becomes a pure latency optimization (skip the download), never a
correctness dependency. Backends without ``get_file``/``list_objects`` (none today) or the
local dev backend simply no-op, keeping their on-disk ``.citypods-state``.
"""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

STATE_PREFIX = "state"
# JSON-typed; everything we persist is text. Kept conservative so a stray binary never syncs.
_SUFFIXES = {".json", ".jsonl"}


def _is_cas_managed(storage, key: str) -> bool:
    """True if ``key`` is a coordination object this ``storage`` manages by compare-and-swap.

    Such keys are read/written by CAS (review/17 §5), so the bulk state sync must skip them on both
    legs: ``pull_state`` would shadow the CAS object with a stale local copy, and ``push_state``
    would clobber it with a non-conditional ``put_file``. The storage is the authority: a
    ``RoutingStorage`` answers from its own configured prefixes (which can differ from the module
    constant). A non-routing CAS backend (e.g. a plain ``r2``) has no per-instance prefix list, so
    fall back to the module table. A non-CAS backend (plain B2 / local) keeps bulk-syncing the file
    as before (the local-file fallback path)."""
    predicate = getattr(storage, "is_cas_managed_key", None)
    if callable(predicate):
        return bool(predicate(key))
    if not getattr(storage, "cas_capable", False):
        return False
    from citypods.storage.routing import COORDINATION_PREFIXES

    return bool(COORDINATION_PREFIXES) and key.startswith(COORDINATION_PREFIXES)


# Mirror gc_audio's safety floor: a remote state object with no local counterpart is only
# reaped once it is older than this, so an object written by a build that hasn't yet produced
# its local copy isn't deleted out from under it.
RECONCILE_MIN_AGE_DAYS = 7.0


def _supported(storage) -> bool:
    return storage is not None and hasattr(storage, "get_file") and hasattr(storage, "list_objects")


def pull_state(storage, state_dir: Path) -> int:
    """Download the durable state snapshot from the bucket into ``state_dir`` (bucket wins).
    Returns the number of files restored. No-op for backends without sync support."""
    if not _supported(storage):
        return 0
    state_dir = Path(state_dir)
    restored = 0
    for key, _ in storage.list_objects(f"{STATE_PREFIX}/"):
        rel = key[len(STATE_PREFIX) + 1 :]
        if not rel or Path(rel).suffix not in _SUFFIXES:
            continue
        if _is_cas_managed(storage, key):  # CAS-managed on R2; not part of the bulk snapshot
            continue
        if storage.get_file(key, state_dir / rel):
            restored += 1
    return restored


def push_state(storage, state_dir: Path, *, only_prefixes=None) -> int:
    """Upload state files under ``state_dir`` to the bucket's ``state/`` prefix.
    Returns the number of files pushed. No-op for backends without sync support.

    ``only_prefixes`` (the scope hook the H6b shards use): when given, push **only** files whose
    path relative to ``state_dir`` (POSIX form) starts with one of the prefixes. A source-sharded
    ``audio``/``asr`` job pulls the whole prefix for render context but owns only a subset of
    sources, so it must push back **only** the ``source_key``s it owns — e.g.
    ``only_prefixes=["sources/<key>/"]`` — or it would re-upload its now-stale copy of a sibling
    shard's record (the cross-shard clobber, review/12 §H6). ``None`` (default) pushes the whole
    snapshot, which is correct for the single-writer (unsharded enrich) case."""
    if not _supported(storage) or not hasattr(storage, "put_file"):
        return 0
    state_dir = Path(state_dir)
    if not state_dir.exists():
        return 0
    prefixes = tuple(only_prefixes) if only_prefixes is not None else None
    pushed = 0
    for path in sorted(state_dir.rglob("*")):
        if not path.is_file() or path.suffix not in _SUFFIXES:
            continue
        rel = path.relative_to(state_dir).as_posix()
        if prefixes is not None and not rel.startswith(prefixes):
            continue
        if _is_cas_managed(storage, f"{STATE_PREFIX}/{rel}"):  # CAS-managed on R2; never bulk-push
            continue
        storage.put_file(f"{STATE_PREFIX}/{rel}", path, "application/json")
        pushed += 1
    return pushed


def fetch_remote_records(storage, src_key: str) -> dict | None:
    """Fetch + parse the CURRENT remote ``sources/<src_key>/episodes.json`` episodes dict — the
    freshest durable copy, which a sibling lane may have written since this run's ``pull_state``.

    Returns ``{}`` when the remote file does not exist yet (this run is the first to write the
    source — no clobber is possible), the parsed ``{uid: record}`` mapping on success, or ``None``
    when the object exists but cannot be read/parsed. The caller treats ``None`` as "skip this
    source's push": a transient read failure must never license pushing a possibly-stale whole
    record over a sibling's newer one. A backend *listing* error propagates to the caller (also
    treated as skip). No-op (``None``) for backends without sync support."""
    if not _supported(storage):
        return None
    key = f"{STATE_PREFIX}/sources/{src_key}/episodes.json"
    # Existence via list_objects (not get_file): get_file swallows backend errors as "absent",
    # which would let a transient failure masquerade as a first write and clobber the remote.
    present = any(k == key for k, _ in storage.list_objects(f"{STATE_PREFIX}/sources/{src_key}/"))
    if not present:
        return {}
    with tempfile.TemporaryDirectory() as td:
        dest = Path(td) / "episodes.json"
        if not storage.get_file(key, dest):
            return None  # listed but unreadable → transient; skip rather than clobber
        try:
            data = json.loads(dest.read_text())
        except (OSError, ValueError):
            return None
    return data.get("episodes", {}) if isinstance(data, dict) else {}


def push_records_merged(
    storage,
    state_dir: Path,
    source_keys,
    *,
    protected_blocks,
    owned_uids: dict[str, frozenset[str]] | None = None,
    log=None,
) -> int:
    """Foreign-block-preserving scoped push of owned ``sources/<key>/episodes.json`` files.

    A scoped lane run (audio / transcribe / align) owns only its own derived-artifact block, but a
    *different* lane's run touching the SAME source file at an overlapping read→write window must
    not regress the block it doesn't own (the cross-lane lost update: an ASR run finishing after an
    audio run re-uploads its start-of-run audio block, erasing freshly hosted audio — review/12
    §H6). For each owned source this re-reads the CURRENT remote record, preserves
    ``protected_blocks`` from it over the local copy (``records.merge_preserving_foreign``), writes
    the merge back to ``state_dir`` (so the on-disk and pushed copies match), then uploads it.
    Returns the number of source files pushed.

    Fail-safe: if a source's remote record exists but cannot be re-read (transient backend error),
    that source is SKIPPED — never push a possibly-stale whole record over a sibling's newer one.
    The owned artifact is re-pushed next run; because artifacts are content-addressed, the re-credit
    is cheap (review/12 §H6 recovery note). No-op for backends without sync support.

    ``protected_blocks`` empty (a full/unknown lane that owns every artifact) degrades to a plain
    per-source push that still preserves remote-only uids — never less safe than the legacy push.

    ``owned_uids`` (review/18 §3.2): for a per-episode-sharded transcribe run, ``{source: uids}``
    restricting which uids this push may write an artifact block for, so sibling shards on one
    source never regress each other's fresh transcripts. ``None`` (audio/align/source-atomic/full
    run) owns every uid in ``local`` — byte-for-byte the prior behavior."""
    from citypods.records import (
        load_records,
        merge_preserving_foreign,
        records_path,
        save_records,
    )

    if not _supported(storage) or not hasattr(storage, "put_file"):
        return 0
    state_dir = Path(state_dir)
    protected = frozenset(protected_blocks)
    emit = log or (lambda msg: print(msg, flush=True))
    pushed = 0
    for sk in sorted(set(source_keys)):
        try:
            remote = fetch_remote_records(storage, sk)
        except Exception as exc:  # noqa: BLE001 — any backend listing error → fail safe (skip)
            emit(f"state: WARNING remote read failed for source {sk}: {exc}; skipping push")
            continue
        if remote is None:
            emit(f"state: WARNING remote record for source {sk} unreadable; skipping push")
            continue
        local = load_records(state_dir, sk)
        merged = merge_preserving_foreign(
            remote,
            local,
            protected,
            owned_uids=owned_uids.get(sk) if owned_uids is not None else None,
        )
        save_records(state_dir, sk, merged)
        storage.put_file(
            f"{STATE_PREFIX}/sources/{sk}/episodes.json",
            records_path(state_dir, sk),
            "application/json",
        )
        pushed += 1
    return pushed


def _fetch_remote_json(storage, rel: str) -> dict | None:
    key = f"{STATE_PREFIX}/{rel}"
    present = any(k == key for k, _ in storage.list_objects(f"{STATE_PREFIX}/"))
    if not present:
        return {}
    with tempfile.TemporaryDirectory() as td:
        dest = Path(td) / Path(rel).name
        if not storage.get_file(key, dest):
            return None
        try:
            data = json.loads(dest.read_text())
        except (OSError, ValueError):
            return None
    return data if isinstance(data, dict) else {}


def _normalize_asr_runtime_samples(samples: list[dict], *, max_samples: int) -> list[dict]:
    by_id: dict[str, tuple[int, dict]] = {}
    for idx, sample in enumerate(samples):
        transcribe_seconds = float(sample.get("transcribe_seconds", 0) or 0)
        recording_seconds = float(sample.get("recording_seconds", 0) or 0)
        if transcribe_seconds <= 0 or recording_seconds <= 0:
            continue
        finished_at = float(sample.get("finished_at", sample.get("ts", 0)) or 0)
        sample_id = str(sample.get("id") or sample.get("sample_id") or f"legacy-{idx}")
        by_id[sample_id] = (
            idx,
            {
                "id": sample_id,
                "finished_at": finished_at,
                "transcribe_seconds": transcribe_seconds,
                "recording_seconds": recording_seconds,
            },
        )
    ordered = [
        sample
        for _idx, sample in sorted(
            by_id.values(),
            key=lambda item: (float(item[1].get("finished_at", 0) or 0), item[0]),
        )
    ]
    return ordered[-max_samples:]


def push_asr_runtime_log_merged(
    storage,
    state_dir: Path,
    *,
    rel_path: str = "asr_runtime_log.json",
    max_samples: int = 100,
    log=None,
) -> int:
    """Merge-upload the shared ASR runtime telemetry log.

    Scoped ASR shards all update this single file, so a plain ``push_state`` would reintroduce a
    last-writer-wins shared-state race. This fetches the current remote log, unions samples by id,
    keeps the newest ``max_samples``, writes the merged file locally, and uploads it.
    """
    if not _supported(storage) or not hasattr(storage, "put_file"):
        return 0
    state_dir = Path(state_dir)
    local_path = state_dir / rel_path
    if not local_path.exists():
        return 0
    emit = log or (lambda msg: print(msg, flush=True))
    try:
        remote_data = _fetch_remote_json(storage, rel_path)
    except Exception as exc:  # noqa: BLE001 - listing/read errors must not clobber remote telemetry
        emit(f"state: WARNING remote ASR runtime log read failed: {exc}; skipping push")
        return 0
    if remote_data is None:
        emit("state: WARNING remote ASR runtime log unreadable; skipping push")
        return 0
    try:
        local_data = json.loads(local_path.read_text())
    except (OSError, ValueError) as exc:
        emit(f"state: WARNING local ASR runtime log unreadable: {exc}; skipping push")
        return 0
    samples = _normalize_asr_runtime_samples(
        list(remote_data.get("samples", [])) + list(local_data.get("samples", [])),
        max_samples=max_samples,
    )
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text(json.dumps({"version": 1, "samples": samples}, indent=2) + "\n")
    storage.put_file(f"{STATE_PREFIX}/{rel_path}", local_path, "application/json")
    return 1


def reconcile_state(
    storage, state_dir: Path, *, min_age_days: float = RECONCILE_MIN_AGE_DAYS, full_run: bool = True
) -> int:
    """Delete remote ``state/`` objects that no longer have a local counterpart (age-guarded).

    ``push_state`` only ever *uploads*. When a city's ``source`` is edited its ``source_key``
    changes (see ``records.source_key``), so the old ``state/sources/<old_key>/episodes.json``
    would otherwise linger in the bucket forever — growing without bound and, via
    ``referenced_audio_keys``, pinning now-orphaned audio so ``gc_audio`` can't reclaim it.
    Run after ``push_state`` to sweep those stale records. Returns the number deleted. No-op
    for backends without ``list_objects``/``delete``.

    Only safe on a **full, unsharded run** (``full_run=True``, the default): it reaps *every*
    remote object lacking a local counterpart, so a source-sharded ``audio``/``asr`` job — which
    pulls the whole prefix for render context but owns only a subset of sources — would delete its
    siblings' records. ``full_run=False`` (a shard) makes it a no-op; the periodic full enrich run
    does the sweep (H6b)."""
    if not full_run:
        return 0
    if not _supported(storage) or not hasattr(storage, "delete"):
        return 0
    state_dir = Path(state_dir)
    cutoff = datetime.now(UTC) - timedelta(days=min_age_days)
    deleted = 0
    for key, last_modified in storage.list_objects(f"{STATE_PREFIX}/"):
        rel = key[len(STATE_PREFIX) + 1 :]
        if not rel or Path(rel).suffix not in _SUFFIXES:
            continue  # only manage the JSON snapshot push_state writes
        if (state_dir / rel).exists():
            continue  # still has a local counterpart — canonical, keep it
        if last_modified is not None and last_modified > cutoff:
            continue  # too young to be safely reaped (just-written, not yet local)
        storage.delete(key)
        deleted += 1
    return deleted
