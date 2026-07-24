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
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

STATE_PREFIX = "state"

# The durable snapshot is a large fan-out of small JSON files (one ``episodes.json`` per source,
# per-source calendar/run-event sidecars, etc. — thousands of objects on a mature deploy: 3,554 as
# of the `tag` lane's last observed run). Each ``get_file``/``put_file`` is a separate, latency-
# bound round trip to the bucket, so transferring them one at a time is dominated by round-trip
# latency, not bandwidth. A serial *restore* of ~3.5k objects took ~11 minutes at build start —
# nearly half the `tag` lane's 25-minute job budget, and it runs *before* the wall-clock ``stop()``
# window even opens, so a slow restore pushed that lane's graceful-yield deadline past GitHub's
# hard job timeout and the run was cancelled outright with no candidates produced. A bounded worker
# pool overlaps the latency of independent transfers (each reads/writes its own distinct
# ``state_dir/rel`` path, so there is no shared mutable state to guard) and collapses that restore
# to well under a minute. The symmetric *push* at the end of a run paid the identical serial cost
# uncaught for longer: it wasn't logged at all until a later fix added per-file visibility, at
# which point a real run was caught pushing only 1,503 of 3,554 files (42%) in the ~9 minutes of
# tail budget it had left before GitHub's hard timeout killed it mid-upload. Same fix, same pool,
# both directions.
_STATE_SYNC_MAX_WORKERS = 16


class TransientStateSyncError(RuntimeError):
    """A transient state read failure that should requeue its owning work item."""


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


def pull_state(storage, state_dir: Path, *, log=None) -> int:
    """Download the durable state snapshot from the bucket into ``state_dir`` (bucket wins).
    Returns the number of files restored. No-op for backends without sync support.

    A single key that keeps failing with a transient storage read error (timeout, dropped
    connection, transient S3 response, or known botocore parser failure — see
    ``storage.s3.is_transient_storage_error``) is logged and skipped rather
    than aborting the whole restore: render "must always finish so the deploy isn't gated"
    (citypods/run.py), and a skipped file just keeps its existing local copy — the bucket is
    canonical, so it self-heals on the next run that can reach it. A non-transient error
    (e.g. a real 403) still propagates; that is an operator problem, not a blip to paper over.
    """
    if not _supported(storage):
        return 0
    from citypods.storage.s3 import is_transient_storage_error

    emit = log or (lambda msg: print(msg, flush=True))
    state_dir = Path(state_dir)

    # Materialize the key list first (a single paginated LIST), then fan the per-object GETs out
    # across a bounded pool: the listing is cheap and sequential, the downloads are the expensive,
    # parallelizable part. Filtering here keeps the pool doing only real work.
    keys = [
        key
        for key, _ in storage.list_objects(f"{STATE_PREFIX}/")
        if (rel := key[len(STATE_PREFIX) + 1 :])
        and Path(rel).suffix in _SUFFIXES
        # CAS-managed on R2; not part of the bulk snapshot.
        and not _is_cas_managed(storage, key)
    ]
    if not keys:
        return 0

    def _restore_one(key: str) -> bool:
        rel = key[len(STATE_PREFIX) + 1 :]
        try:
            return bool(storage.get_file(key, state_dir / rel))
        except Exception as exc:
            if not is_transient_storage_error(exc):
                raise
            # A single key that keeps failing transiently keeps its existing local copy (the
            # bucket is canonical, so it self-heals next run) rather than aborting the whole
            # restore — same fail-soft contract as the original serial loop.
            emit(f"state: WARNING transient error restoring {key} ({exc}); keeping local copy")
            return False

    workers = min(_STATE_SYNC_MAX_WORKERS, len(keys))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        # ``map`` re-raises the first non-transient error from any worker (propagating a real 403
        # to the caller, exactly as the serial loop did), and otherwise yields one bool per key.
        return sum(1 for got in pool.map(_restore_one, keys) if got)


def push_state(storage, state_dir: Path, *, only_prefixes=None, log=None) -> int:
    """Upload state files under ``state_dir`` to the bucket's ``state/`` prefix.
    Returns the number of files pushed. No-op for backends without sync support.

    ``only_prefixes`` (the scope hook the H6b shards use): when given, push **only** files whose
    path relative to ``state_dir`` (POSIX form) starts with one of the prefixes. A source-sharded
    ``audio``/``asr`` job pulls the whole prefix for render context but owns only a subset of
    sources, so it must push back **only** the ``source_key``s it owns — e.g.
    ``only_prefixes=["sources/<key>/"]`` — or it would re-upload its now-stale copy of a sibling
    shard's record (the cross-shard clobber, review/12 §H6). ``None`` (default) pushes the whole
    snapshot, which is correct for the single-writer (unsharded enrich) case.

    Logs a start count and one line per file (``log``, default ``print(..., flush=True)``): this
    is the run's last write before it's considered durably persisted, and it used to be a silent
    black box — a slow or stuck upload here was indistinguishable from any other cause of a run
    running out of wall-clock budget (issue: the tag lane's finalization tail gave no visibility
    into what it was doing before GitHub's hard job timeout cancelled it mid-persist).

    Uploads run across a bounded worker pool (mirroring ``pull_state``'s parallel restore, same
    ``_STATE_SYNC_MAX_WORKERS``): a real `tag` lane run was caught pushing only 1,503 of 3,554
    files serially in the ~9 minutes of tail budget it had before GitHub's hard job timeout killed
    it mid-upload — the identical latency-bound-not-bandwidth-bound cost ``pull_state`` was already
    fixed on the download side. Each upload writes its own distinct remote key, so there is no
    shared mutable state to guard."""
    if not _supported(storage) or not hasattr(storage, "put_file"):
        return 0
    emit = log or (lambda msg: print(msg, flush=True))
    state_dir = Path(state_dir)
    if not state_dir.exists():
        return 0
    prefixes = tuple(only_prefixes) if only_prefixes is not None else None
    candidates = []
    for path in sorted(state_dir.rglob("*")):
        if not path.is_file() or path.suffix not in _SUFFIXES:
            continue
        rel = path.relative_to(state_dir).as_posix()
        if prefixes is not None and not rel.startswith(prefixes):
            continue
        if _is_cas_managed(storage, f"{STATE_PREFIX}/{rel}"):  # CAS-managed on R2; never bulk-push
            continue
        candidates.append(rel)
    if not candidates:
        return 0
    emit(f"state: pushing {len(candidates)} file(s) to durable storage")

    def _push_one(rel: str) -> bool:
        emit(f"state: pushing {rel}")
        storage.put_file(f"{STATE_PREFIX}/{rel}", state_dir / rel, "application/json")
        return True

    workers = min(_STATE_SYNC_MAX_WORKERS, len(candidates))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        # ``map`` re-raises the first error from any worker (matching the serial loop's contract:
        # an upload failure is not transient-tolerant here the way a restore's is, since a partial
        # push must not be silently reported as complete).
        return sum(1 for ok in pool.map(_push_one, candidates) if ok)


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
    raise_on_transient: bool = False,
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
            if raise_on_transient:
                raise TransientStateSyncError(f"remote read failed for source {sk}: {exc}") from exc
            emit(f"state: WARNING remote read failed for source {sk}: {exc}; skipping push")
            continue
        if remote is None:
            if raise_on_transient:
                raise TransientStateSyncError(f"remote record for source {sk} unreadable")
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


def push_calendar_records_merged(storage, state_dir: Path, source_keys, *, log=None) -> int:
    """Append-only, race-safe scoped push for ``sources/<key>/calendar.json``.

    Calendar metadata is not owned by an audio/transcript artifact lane, but a
    scoped run still refreshes its primary source and may discover additional
    companion rows. Re-read the remote catalog and union official links before
    upload so one lane cannot erase another lane's concurrently discovered
    history. Sources with no local calendar file are skipped.
    """
    from citypods.records import (
        calendar_records_path,
        load_calendar_records,
        merge_calendar_records,
        save_calendar_records,
    )

    if not _supported(storage) or not hasattr(storage, "put_file"):
        return 0
    state_dir = Path(state_dir)
    emit = log or (lambda msg: print(msg, flush=True))
    pushed = 0
    for sk in sorted(set(source_keys)):
        local_path = calendar_records_path(state_dir, sk)
        if not local_path.exists():
            continue
        key = f"{STATE_PREFIX}/sources/{sk}/calendar.json"
        try:
            prefix = f"{STATE_PREFIX}/sources/{sk}/"
            present = any(k == key for k, _ in storage.list_objects(prefix))
        except Exception as exc:  # noqa: BLE001 — fail safe on backend listing failure
            emit(
                f"state: WARNING remote calendar read failed for source {sk}: {exc}; skipping push"
            )
            continue
        remote = {}
        if present:
            with tempfile.TemporaryDirectory() as td:
                dest = Path(td) / "calendar.json"
                if not storage.get_file(key, dest):
                    emit(
                        f"state: WARNING remote calendar for source {sk} unreadable; skipping push"
                    )
                    continue
                try:
                    data = json.loads(dest.read_text())
                except (OSError, ValueError):
                    emit(f"state: WARNING remote calendar for source {sk} invalid; skipping push")
                    continue
                raw = data.get("records", {}) if isinstance(data, dict) else {}
                if not isinstance(raw, dict):
                    emit(f"state: WARNING remote calendar for source {sk} invalid; skipping push")
                    continue
                # Reuse the record-store parser by writing the validated envelope
                # to a tiny temporary state tree; it is the single compatibility
                # reader for calendar schema evolution.
                remote_dir = Path(td) / "state"
                remote_file = calendar_records_path(remote_dir, sk)
                remote_file.parent.mkdir(parents=True, exist_ok=True)
                remote_file.write_text(json.dumps(data))
                remote = load_calendar_records(remote_dir, sk)
        local = load_calendar_records(state_dir, sk)
        merged = merge_calendar_records(remote, local.values())
        save_calendar_records(state_dir, sk, merged)
        storage.put_file(key, local_path, "application/json")
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


def push_transcript_quality_log_merged(
    storage,
    state_dir: Path,
    *,
    rel_path: str = "transcript_quality_log.json",
    max_events: int = 200,
    log=None,
) -> int:
    """Merge-upload the shared H15 raw evaluation log.

    Unlike rollups, the raw log is intentionally capped. Merge by event id, keep the newest
    ``max_events``, and never let a transient remote read degrade into a last-writer-wins push.
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
    except Exception as exc:  # noqa: BLE001
        emit(f"state: WARNING remote transcript-quality log read failed: {exc}; skipping push")
        return 0
    if remote_data is None:
        emit("state: WARNING remote transcript-quality log unreadable; skipping push")
        return 0
    try:
        local_data = json.loads(local_path.read_text())
    except (OSError, ValueError) as exc:
        emit(f"state: WARNING local transcript-quality log unreadable: {exc}; skipping push")
        return 0
    from citypods.transcript_quality import _normalize_events

    events = _normalize_events(
        list(remote_data.get("events", [])) + list(local_data.get("events", [])),
        max_events=max_events,
    )
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text(json.dumps({"version": 1, "events": events}, indent=2) + "\n")
    storage.put_file(f"{STATE_PREFIX}/{rel_path}", local_path, "application/json")
    return 1


def push_calibration_trend_merged(
    storage,
    state_dir: Path,
    *,
    rel_path: str = "transcript_quality_calibration_trend.json",
    max_entries: int = 52,
    log=None,
) -> int:
    """Merge-upload the H15 Layer 3 calibration trend log.

    Like the raw evaluation log, this is intentionally capped — merge by run_at, keep the
    newest ``max_entries``, and never let a transient remote read degrade into a
    last-writer-wins push.
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
    except Exception as exc:  # noqa: BLE001
        emit(f"state: WARNING remote calibration trend read failed: {exc}; skipping push")
        return 0
    if remote_data is None:
        emit("state: WARNING remote calibration trend unreadable; skipping push")
        return 0
    try:
        local_data = json.loads(local_path.read_text())
    except (OSError, ValueError) as exc:
        emit(f"state: WARNING local calibration trend unreadable: {exc}; skipping push")
        return 0
    by_run_at: dict[str, dict] = {}
    for entry in list(remote_data.get("runs", [])) + list(local_data.get("runs", [])):
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("run_at") or "")
        if key:
            by_run_at[key] = entry
    runs = [by_run_at[key] for key in sorted(by_run_at)][-max_entries:]
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text(json.dumps({"version": 1, "runs": runs}, indent=2) + "\n")
    storage.put_file(f"{STATE_PREFIX}/{rel_path}", local_path, "application/json")
    return 1


def push_transcript_quality_rollups_merged(
    storage,
    state_dir: Path,
    *,
    rel_path: str = "transcript_quality_rollups.json",
    log=None,
) -> int:
    """Merge-upload the stable H15 body/source rollups.

    Rollups are not pruned: rows merge by ``(source_key, body_key)`` and evidence merges by
    ``sample_id``, preserving the durable one-row-per-body/source ledger used for routing.
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
    except Exception as exc:  # noqa: BLE001
        emit(f"state: WARNING remote transcript-quality rollups read failed: {exc}; skipping push")
        return 0
    if remote_data is None:
        emit("state: WARNING remote transcript-quality rollups unreadable; skipping push")
        return 0
    try:
        local_data = json.loads(local_path.read_text())
    except (OSError, ValueError) as exc:
        emit(f"state: WARNING local transcript-quality rollups unreadable: {exc}; skipping push")
        return 0
    from citypods.transcript_quality import _normalize_rollups

    rows = _normalize_rollups(list(remote_data.get("rows", [])) + list(local_data.get("rows", [])))
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text(json.dumps({"version": 1, "rows": rows}, indent=2) + "\n")
    storage.put_file(f"{STATE_PREFIX}/{rel_path}", local_path, "application/json")
    return 1


def reconcile_state(
    storage,
    state_dir: Path,
    *,
    min_age_days: float = RECONCILE_MIN_AGE_DAYS,
    full_run: bool = True,
    log=None,
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
    emit = log or (lambda msg: print(msg, flush=True))
    state_dir = Path(state_dir)
    cutoff = datetime.now(UTC) - timedelta(days=min_age_days)
    emit("state: reconciling remote state (listing objects)")
    deleted = 0
    for key, last_modified in storage.list_objects(f"{STATE_PREFIX}/"):
        rel = key[len(STATE_PREFIX) + 1 :]
        if not rel or Path(rel).suffix not in _SUFFIXES:
            continue  # only manage the JSON snapshot push_state writes
        if _is_cas_managed(storage, key):
            continue  # CAS-managed on R2; never bulk-reconcile/delete
        if (state_dir / rel).exists():
            continue  # still has a local counterpart — canonical, keep it
        if last_modified is not None and last_modified > cutoff:
            continue  # too young to be safely reaped (just-written, not yet local)
        emit(f"state: reclaiming stale {key}")
        storage.delete(key)
        deleted += 1
    return deleted
