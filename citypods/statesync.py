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

import hashlib
import json
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from posixpath import normpath

STATE_PREFIX = "state"
MANIFEST_REL_PATH = "catalog/manifest.json"
MANIFEST_KEY = f"{STATE_PREFIX}/{MANIFEST_REL_PATH}"
MANIFEST_VERSION = 1
DIRTY_JOURNAL_NAME = ".dirty.json"
TOMBSTONE_JOURNAL_NAME = ".tombstones.json"

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
_MANIFEST_CAS_ATTEMPTS = 3
_JOURNAL_LOCK = threading.Lock()


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


def _digest(path: Path) -> str:
    if not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_path(state_dir: Path) -> Path:
    return Path(state_dir) / MANIFEST_REL_PATH


def _journal_rel_path(state_dir: Path, raw_path: str | Path, *, label: str) -> str:
    candidate = Path(raw_path)
    if candidate.is_absolute():
        try:
            candidate = candidate.resolve().relative_to(Path(state_dir).resolve())
        except ValueError:
            raise ValueError(f"{label} path escapes state_dir: {raw_path!r}") from None
    rel = normpath(candidate.as_posix())
    if rel in {".", ".."} or rel.startswith("../"):
        raise ValueError(f"{label} path escapes state_dir: {raw_path!r}")
    if Path(rel).suffix not in _SUFFIXES:
        raise ValueError(f"{label} path must be JSON or JSONL: {raw_path!r}")
    if rel in {MANIFEST_REL_PATH, DIRTY_JOURNAL_NAME, TOMBSTONE_JOURNAL_NAME}:
        raise ValueError(f"{label} path is reserved: {raw_path!r}")
    return rel


def _read_journal(path: Path, state_dir: Path, *, label: str) -> set[str]:
    try:
        data = json.loads(path.read_text()) if path.exists() else []
    except (OSError, ValueError, TypeError):
        return set()
    if not isinstance(data, list):
        return set()
    valid: set[str] = set()
    for raw in data:
        if not isinstance(raw, str):
            continue
        try:
            valid.add(_journal_rel_path(state_dir, raw, label=label))
        except ValueError:
            # Journals are optimization hints; discard hand-edited/corrupt entries rather than
            # allowing them to reach a destructive consumer.
            continue
    return valid


def mark_state_dirty(state_dir: Path, *paths: str | Path) -> None:
    """Register exact state paths changed by a writer.

    The journal is deliberately local and disposable: it is an optimization hint, never the
    source of truth. A missing or malformed journal makes ``push_state`` use its safe scan path.
    """
    state_dir = Path(state_dir)
    with _JOURNAL_LOCK:
        state_dir.mkdir(parents=True, exist_ok=True)
        journal = state_dir / DIRTY_JOURNAL_NAME
        dirty = _read_journal(journal, state_dir, label="dirty")
        dirty.update(_journal_rel_path(state_dir, raw, label="dirty") for raw in paths)
        journal.write_text(json.dumps(sorted(dirty), indent=2) + "\n")


def mark_state_deleted(state_dir: Path, *paths: str | Path) -> None:
    """Record explicit state removals; partial local checkouts never imply deletion."""
    state_dir = Path(state_dir)
    with _JOURNAL_LOCK:
        state_dir.mkdir(parents=True, exist_ok=True)
        path = state_dir / TOMBSTONE_JOURNAL_NAME
        tombstones = _read_journal(path, state_dir, label="tombstone")
        tombstones.update(_journal_rel_path(state_dir, raw, label="tombstone") for raw in paths)
        path.write_text(json.dumps(sorted(tombstones), indent=2) + "\n")


def _validate_manifest(manifest: object) -> dict | None:
    if (
        not isinstance(manifest, dict)
        or manifest.get("version") != MANIFEST_VERSION
        or not isinstance(manifest.get("generation"), int)
        or not isinstance(manifest.get("objects"), dict)
        or not isinstance(manifest.get("tombstones", []), list)
        or not all(isinstance(value, dict) for value in manifest["objects"].values())
    ):
        return None
    return manifest


def _load_manifest(storage, state_dir: Path, *, log) -> dict | None:
    """Read and validate the compact remote index, returning None to request full sync."""
    if not _supported(storage):
        return None
    with tempfile.TemporaryDirectory() as td:
        local = Path(td) / "manifest.json"
        try:
            if not storage.get_file(MANIFEST_KEY, local):
                return None
            manifest = json.loads(local.read_text())
        except (OSError, ValueError, TypeError) as exc:
            log(f"state: WARNING invalid remote manifest ({exc}); falling back to full sync")
            return None
    if _validate_manifest(manifest) is None:
        log("state: WARNING incompatible remote manifest; falling back to full sync")
        return None
    return manifest


def _manifest_object(rel: str, path: Path) -> dict:
    return {
        "digest": _digest(path),
        "size": path.stat().st_size,
        "category": rel.split("/", 1)[0] or "root",
    }


def _read_local_manifest(state_dir: Path) -> dict:
    path = _manifest_path(state_dir)
    try:
        data = json.loads(path.read_text())
        if data.get("version") == MANIFEST_VERSION and isinstance(data.get("objects"), dict):
            return data
    except (OSError, ValueError, TypeError):
        pass
    return {"version": MANIFEST_VERSION, "generation": 0, "objects": {}, "tombstones": []}


def _write_local_manifest(state_dir: Path, manifest: dict) -> None:
    path = _manifest_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def ensure_remote_manifest(storage, state_dir: Path, *, log=None) -> bool:
    """Seed a complete remote manifest after a full B2 fallback restore.

    ``pull_state`` can restore a complete local snapshot by listing B2 when the R2 manifest is
    absent. A later scoped push must not publish only its owned subset as a new manifest, so the
    ASR reconcile job calls this once after that full restore. The seed indexes local durable files
    without re-uploading them; their canonical bytes are already on B2. Returns whether a manifest
    is present or was successfully seeded. A concurrent seed is harmless: the conditional write
    loses and the caller's subsequent scoped push will re-read the winner.
    """
    if not _supported(storage) or not getattr(storage, "cas_capable", False):
        return False
    emit = log or (lambda msg: print(msg, flush=True))
    state_dir = Path(state_dir)
    try:
        current = storage.get_bytes(MANIFEST_KEY)
    except Exception as exc:  # noqa: BLE001 — leave the B2-list fallback available for retry
        emit(f"state: WARNING could not inspect remote manifest for seed ({exc})")
        return False
    if current is not None:
        return True

    objects = {}
    for path in sorted(state_dir.rglob("*")):
        if not path.is_file() or path.suffix not in _SUFFIXES:
            continue
        rel = path.relative_to(state_dir).as_posix()
        if rel in {MANIFEST_REL_PATH, DIRTY_JOURNAL_NAME, TOMBSTONE_JOURNAL_NAME}:
            continue
        if _is_cas_managed(storage, f"{STATE_PREFIX}/{rel}"):
            continue
        objects[rel] = _manifest_object(rel, path)
    tombstones = _read_journal(state_dir / TOMBSTONE_JOURNAL_NAME, state_dir, label="tombstone")
    manifest = {
        "version": MANIFEST_VERSION,
        "generation": 1,
        "updated_at": datetime.now(UTC).isoformat(),
        "objects": objects,
        "tombstones": sorted(tombstones),
    }
    payload = json.dumps(manifest, indent=2, sort_keys=True).encode()
    try:
        storage.put_cas(MANIFEST_KEY, payload, "application/json", if_none_match="*")
    except Exception as exc:  # noqa: BLE001 — a concurrent seed or transient retry is safe
        emit(f"state: WARNING could not seed remote manifest ({exc})")
        return False
    _write_local_manifest(state_dir, manifest)
    emit(f"state: seeded remote manifest with {len(objects)} object(s)")
    return True


def _full_state_keys(storage):
    return [
        key
        for key, _ in storage.list_objects(f"{STATE_PREFIX}/")
        if (rel := key[len(STATE_PREFIX) + 1 :])
        and rel not in {MANIFEST_REL_PATH, DIRTY_JOURNAL_NAME, TOMBSTONE_JOURNAL_NAME}
        and Path(rel).suffix in _SUFFIXES
        and not _is_cas_managed(storage, key)
    ]


def pull_state(storage, state_dir: Path, *, only_paths=None, log=None) -> int:
    """Download the durable state snapshot from the bucket into ``state_dir`` (bucket wins).
    Returns the number of files restored. No-op for backends without sync support.

    ``only_paths`` (optional): when given, restrict the pull to the listed relative paths (e.g.
    ``["llm_evaluation.json"]``). Useful for commands that only need one file and do not want to
    pay the cost of downloading the full snapshot.

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
    manifest = _load_manifest(storage, state_dir, log=emit)
    if manifest is not None:
        objects = manifest["objects"]
        tombstones = set(manifest.get("tombstones", []))
        keys = [f"{STATE_PREFIX}/{rel}" for rel in objects if rel not in tombstones]
        # A valid manifest is authoritative for remote state. Do not remove local files here:
        # unpushed local writes may be present after an interrupted run. Tombstones are applied
        # only to files that are not locally dirty.
        try:
            dirty = set(json.loads((state_dir / DIRTY_JOURNAL_NAME).read_text()))
        except (OSError, ValueError, TypeError):
            dirty = set()
        for rel in tombstones - dirty:
            (state_dir / rel).unlink(missing_ok=True)
    else:
        keys = _full_state_keys(storage)

    # When only specific files are requested, filter the key list before checking freshness or
    # spawning the thread pool — we don't need to download or even stat any other files.
    if only_paths is not None:
        wanted = {f"{STATE_PREFIX}/{rel}" for rel in only_paths}
        keys = [k for k in keys if k in wanted]

    # Materialize the key list first (a single paginated LIST on fallback), then fan the per-object
    # GETs out
    # across a bounded pool: the listing is cheap and sequential, the downloads are the expensive,
    # parallelizable part. Filtering here keeps the pool doing only real work.
    if manifest is not None:

        def _needs_restore(rel: str) -> bool:
            entry = manifest["objects"].get(rel, {})
            local = state_dir / rel
            if not local.is_file():
                return True
            size = entry.get("size")
            if isinstance(size, int) and local.stat().st_size != size:
                return True
            return _digest(local) != str(entry.get("digest", ""))

        keys = [key for key in keys if _needs_restore(key[len(STATE_PREFIX) + 1 :])]
    if not keys:
        if manifest is not None:
            _write_local_manifest(state_dir, manifest)
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
        restored = sum(1 for got in pool.map(_restore_one, keys) if got)
    if manifest is not None:
        _write_local_manifest(state_dir, manifest)
    return restored


def push_state(storage, state_dir: Path, *, only_prefixes=None, only_paths=None, log=None) -> int:
    """Upload state files under ``state_dir`` to the bucket's ``state/`` prefix.
    Returns the number of durable-scope candidates reconciled. Unchanged candidates may require
    no PUT because the manifest already proves them current. No-op for backends without sync
    support.

    ``only_prefixes`` (the scope hook the H6b shards use): when given, push **only** files whose
    path relative to ``state_dir`` (POSIX form) starts with one of the prefixes. A source-sharded
    ``audio``/``asr`` job pulls the whole prefix for render context but owns only a subset of
    sources, so it must push back **only** the ``source_key``s it owns — e.g.
    ``only_prefixes=["sources/<key>/"]`` — or it would re-upload its now-stale copy of a sibling
    shard's record (the cross-shard clobber, review/12 §H6). ``None`` (default) pushes the whole
    snapshot, which is correct for the single-writer (unsharded enrich) case.

    ``only_paths`` is an exact relative-path scope for callers that know the one file they just
    created. It is mutually exclusive with ``only_prefixes`` and avoids rescanning/re-uploading
    historical files under an append-only directory such as ``run_events/``.

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
    if only_prefixes is not None and only_paths is not None:
        raise ValueError("only_prefixes and only_paths are mutually exclusive")
    explicitly_scoped = only_prefixes is not None or only_paths is not None
    prefixes = tuple(only_prefixes) if only_prefixes is not None else None
    exact_paths = set()
    for raw_path in only_paths or ():
        path_obj = Path(raw_path)
        if path_obj.is_absolute():
            raise ValueError(f"only_paths entry must be relative: {raw_path!r}")
        rel = normpath(path_obj.as_posix())
        if rel in {".", ".."} or rel.startswith("../"):
            raise ValueError(f"only_paths entry is not a valid relative file: {raw_path!r}")
        exact_paths.add(rel)
    exact_paths = frozenset(exact_paths)
    journal_path = state_dir / DIRTY_JOURNAL_NAME
    journal_paths = None
    if only_paths is None and only_prefixes is None and journal_path.exists():
        try:
            journal_paths = sorted(_read_journal(journal_path, state_dir, label="dirty"))
        except (OSError, ValueError, TypeError):
            journal_paths = None
    journal_driven = journal_paths is not None
    if journal_driven and not explicitly_scoped:
        # The journal narrows the upload decision, not candidate discovery: an unjournaled write
        # must still be compared with the manifest rather than being hidden by a warm cache.
        only_paths = None

    if only_paths is not None:
        # Exact-path callers already know the files they created; avoid walking the retained
        # state tree just to rediscover one append-only event.
        candidates = []
        for rel in sorted(exact_paths):
            path = state_dir / rel
            try:
                path.resolve().relative_to(state_dir.resolve())
            except ValueError:
                raise ValueError(f"only_paths entry escapes state_dir: {rel!r}") from None
            if not path.is_file() or path.suffix not in _SUFFIXES:
                continue
            if _is_cas_managed(storage, f"{STATE_PREFIX}/{rel}"):
                continue
            candidates.append(rel)
    else:
        candidates = []
        for path in sorted(state_dir.rglob("*")):
            if not path.is_file() or path.suffix not in _SUFFIXES:
                continue
            rel = path.relative_to(state_dir).as_posix()
            if rel in {MANIFEST_REL_PATH, DIRTY_JOURNAL_NAME, TOMBSTONE_JOURNAL_NAME}:
                continue
            if prefixes is not None and not rel.startswith(prefixes):
                continue
            if _is_cas_managed(storage, f"{STATE_PREFIX}/{rel}"):
                continue
            candidates.append(rel)
    tombstone_path = state_dir / TOMBSTONE_JOURNAL_NAME
    if not candidates and not tombstone_path.exists():
        return 0

    remote_manifest = _load_manifest(storage, state_dir, log=emit)
    manifest = remote_manifest or {
        "version": MANIFEST_VERSION,
        "generation": 0,
        "objects": {},
        "tombstones": [],
    }
    remote_objects = dict(manifest.get("objects", {}))
    remote_tombstones = set(manifest.get("tombstones", []))
    changed = [
        rel
        for rel in candidates
        if remote_manifest is None
        or rel in remote_tombstones
        or remote_objects.get(rel, {}).get("digest") != _digest(state_dir / rel)
    ]
    deleted = _read_journal(tombstone_path, state_dir, label="tombstone")
    if not changed and not deleted:
        _write_local_manifest(state_dir, manifest)
        journal_path.unlink(missing_ok=True)
        # The return value is a durable-scope count used by cleanup callers as a successful
        # persistence signal. It is intentionally not a network-upload count; the manifest is
        # what proves unchanged objects were safely reconciled without PUTs.
        return len(changed) if journal_driven else len(candidates)
    emit(f"state: pushing {len(changed)} file(s) to durable storage")

    def _push_one(rel: str) -> None:
        emit(f"state: pushing {rel}")
        storage.put_file(f"{STATE_PREFIX}/{rel}", state_dir / rel, "application/json")

    if changed:
        workers = min(_STATE_SYNC_MAX_WORKERS, len(changed))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # ``map`` re-raises the first error from any worker (matching the serial loop's
            # contract: an upload failure is not transient-tolerant here the way a restore's is,
            # since a partial push must not be silently reported as complete).
            for _ in pool.map(_push_one, changed):
                pass

    # A scoped writer may have restored state from the B2-list fallback but only owns a subset of
    # files. Never let that subset become a new authoritative manifest; the next full restore must
    # continue using the B2 listing until the run-start reconcile job seeds a complete R2 index.
    if remote_manifest is None and explicitly_scoped:
        emit("state: remote manifest absent; retaining B2-list fallback after scoped push")
        return len(changed) if journal_driven else len(candidates)

    delta_objects = {rel: _manifest_object(rel, state_dir / rel) for rel in changed}

    def _merge_manifest(base: dict) -> dict:
        objects = dict(base.get("objects", {}))
        objects.update(delta_objects)
        tombstones = set(base.get("tombstones", []))
        tombstones.update(deleted)
        for rel in delta_objects:
            tombstones.discard(rel)
        for rel in deleted:
            objects.pop(rel, None)
        return {
            "version": MANIFEST_VERSION,
            "generation": int(base.get("generation", 0)) + 1,
            "updated_at": datetime.now(UTC).isoformat(),
            "objects": objects,
            "tombstones": sorted(tombstones),
        }

    new_manifest = _merge_manifest(manifest)
    payload = json.dumps(new_manifest, indent=2, sort_keys=True).encode()
    cas_done = False
    cas_attempted = False
    if (
        getattr(storage, "cas_capable", False)
        and hasattr(storage, "get_bytes")
        and hasattr(storage, "put_cas")
    ):
        cas_attempted = True
        for attempt in range(_MANIFEST_CAS_ATTEMPTS):
            try:
                current = storage.get_bytes(MANIFEST_KEY)
                if current is None:
                    base = {
                        "version": MANIFEST_VERSION,
                        "generation": 0,
                        "objects": {},
                        "tombstones": [],
                    }
                    etag_kwargs = {"if_none_match": "*"}
                else:
                    raw_manifest = json.loads(current[0])
                    base = _validate_manifest(raw_manifest)
                    if base is None:
                        emit("state: WARNING incompatible CAS manifest; keeping prior manifest")
                        break
                    etag_kwargs = {"if_match": current[1]}
                new_manifest = _merge_manifest(base)
                payload = json.dumps(new_manifest, indent=2, sort_keys=True).encode()
                storage.put_cas(MANIFEST_KEY, payload, "application/json", **etag_kwargs)
                cas_done = True
                break
            except Exception as exc:  # noqa: BLE001 — preserve journals for the next run
                if attempt + 1 < _MANIFEST_CAS_ATTEMPTS:
                    emit(f"state: manifest CAS retry {attempt + 1}: {exc}")
                else:
                    emit(f"state: WARNING manifest CAS failed; keeping prior manifest ({exc})")
    if not cas_done and not cas_attempted:
        with tempfile.NamedTemporaryFile() as manifest_file:
            manifest_file.write(payload)
            manifest_file.flush()
            storage.put_file(MANIFEST_KEY, Path(manifest_file.name), "application/json")
    manifest_published = cas_done or not cas_attempted
    if manifest_published:
        _write_local_manifest(state_dir, new_manifest)
        journal_path.unlink(missing_ok=True)
        if deleted:
            if not hasattr(storage, "delete"):
                emit("state: WARNING storage cannot delete tombstones; retaining journal")
            else:
                failed_deletes = []
                for rel in deleted:
                    try:
                        storage.delete(f"{STATE_PREFIX}/{rel}")
                    except Exception as exc:  # noqa: BLE001 — retry on the next run
                        emit(f"state: WARNING could not delete tombstone {rel}: {exc}")
                        failed_deletes.append(rel)
                if failed_deletes:
                    tombstone_path.write_text(json.dumps(sorted(failed_deletes), indent=2) + "\n")
                else:
                    tombstone_path.unlink(missing_ok=True)
        else:
            tombstone_path.unlink(missing_ok=True)
    return len(changed) if journal_driven else len(candidates)


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
    lane: str | None = None,
    owned_uids: dict[str, frozenset[str]] | None = None,
    maintenance_lease=None,
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
    if maintenance_lease is not None:
        # Chapter reset and chapter-lane writes share one CAS-backed mutex. Check it after all
        # local work is complete and immediately before the remote-preserving merge begins.
        maintenance_lease.assert_held()
    state_dir = Path(state_dir)
    protected = frozenset(protected_blocks)
    emit = log or (lambda msg: print(msg, flush=True))

    def _push_one(sk: str) -> int:
        try:
            remote = fetch_remote_records(storage, sk)
        except Exception as exc:  # noqa: BLE001 — any backend listing error → fail safe (skip)
            if raise_on_transient:
                raise TransientStateSyncError(f"remote read failed for source {sk}: {exc}") from exc
            emit(f"state: WARNING remote read failed for source {sk}: {exc}; skipping push")
            return 0
        if remote is None:
            if raise_on_transient:
                raise TransientStateSyncError(f"remote record for source {sk} unreadable")
            emit(f"state: WARNING remote record for source {sk} unreadable; skipping push")
            return 0
        local = load_records(state_dir, sk)
        # DIAGNOSTIC (agenda-extraction storage-recall investigation -- see the matching
        # checkpoints in run.py's persist_source and stages.py's _store_document/AgendaTextStage):
        # a fresh links[*_artifact_key] mutation is confirmed present at persist_source's
        # "combined" checkpoint (i.e. in the local on-disk episodes.json this reads via
        # load_records above) but was not found in the remote B2 record afterward. This
        # checkpoint confirms whether it's still here just before the remote-preserving merge, and
        # whether merge_preserving_foreign is what drops it. Bounded to genuinely new artifact
        # keys this run. Remove once root-caused.
        _diag_new_artifact_keys: dict[str, dict[str, str]] = {}
        for uid, local_rec in local.items():
            local_links = local_rec.get("links") or {}
            remote_links = (remote.get(uid) or {}).get("links") or {}
            added = {
                k: v
                for k, v in local_links.items()
                if k.endswith("_artifact_key") and remote_links.get(k) != v
            }
            if added:
                _diag_new_artifact_keys[uid] = added
                emit(
                    f"[push_records_merged] checkpoint=local-pre-merge source={sk} uid={uid} "
                    f"new_artifact_keys={added}"
                )
        merged = merge_preserving_foreign(
            remote,
            local,
            protected,
            lane=lane,
            owned_uids=owned_uids.get(sk) if owned_uids is not None else None,
        )
        for uid, added in _diag_new_artifact_keys.items():
            merged_links = (merged.get(uid) or {}).get("links") or {}
            actual = {k: merged_links.get(k) for k in added}
            emit(
                f"[push_records_merged] checkpoint=merged-pre-push source={sk} uid={uid} "
                f"expected={added} actual={actual} match={actual == added}"
            )
        save_records(state_dir, sk, merged)
        storage.put_file(
            f"{STATE_PREFIX}/sources/{sk}/episodes.json",
            records_path(state_dir, sk),
            "application/json",
        )
        # DIAGNOSTIC (agenda-extraction storage-recall investigation): every prior checkpoint
        # (persist_source's "fresh"/"combined", this function's "local-pre-merge"/
        # "merged-pre-push") has matched, in two independent runs, yet the artifact key is still
        # missing from the remote record afterward. The one thing none of them verify is whether
        # the actual upload above landed with the content this run intended -- a stale read, a
        # backend-side overwrite race with a sibling job, or a genuinely bad `put_file` are all
        # still on the table. Re-fetch the object we just wrote, in-process, immediately after the
        # upload, and compare. Remove once root-caused.
        if _diag_new_artifact_keys:
            readback = fetch_remote_records(storage, sk)
            for uid, added in _diag_new_artifact_keys.items():
                readback_links = (readback or {}).get(uid, {}).get("links") or {}
                actual = {k: readback_links.get(k) for k in added}
                emit(
                    f"[push_records_merged] checkpoint=post-push-readback source={sk} uid={uid} "
                    f"expected={added} actual={actual} match={actual == added} "
                    f"readback_is_none={readback is None}"
                )
        return 1

    # Each source has a distinct local file and remote object. Parallelizing them preserves the
    # foreign-block merge for every source while eliminating the long serial B2 tail from a tag
    # checkpoint that touches dozens of sources.
    source_keys = sorted(set(source_keys))
    if not source_keys:
        return 0
    workers = min(_STATE_SYNC_MAX_WORKERS, len(source_keys))
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return sum(pool.map(_push_one, source_keys))
    return _push_one(source_keys[0])


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


def push_refresh_state_merged(
    storage,
    state_dir: Path,
    *,
    owned_source_keys=None,
    rel_path: str = "source_refresh.json",
    log=None,
) -> int:
    """Merge-upload the per-source refresh ledger without scoped-shard clobbering.

    A source/shard run owns only the refresh entries for the sources it processed. Re-read the
    durable ledger, overlay those entries, and upload the merged snapshot. The unscoped path
    overlays every locally loaded entry, preserving the existing full-run behavior.
    """
    if not _supported(storage) or not hasattr(storage, "put_file"):
        return 0
    state_dir = Path(state_dir)
    local_path = state_dir / rel_path
    if not local_path.exists():
        return 0
    emit = log or (lambda msg: print(msg, flush=True))
    try:
        local_data = json.loads(local_path.read_text())
    except (OSError, ValueError) as exc:
        emit(f"state: WARNING local refresh ledger unreadable: {exc}; skipping push")
        return 0
    if not isinstance(local_data, dict):
        emit("state: WARNING local refresh ledger is not an object; skipping push")
        return 0
    try:
        remote_data = _fetch_remote_json(storage, rel_path)
    except Exception as exc:  # noqa: BLE001 — never clobber a durable ledger on read failure
        emit(f"state: WARNING remote refresh ledger read failed: {exc}; skipping push")
        return 0
    if remote_data is None:
        emit("state: WARNING remote refresh ledger unreadable; skipping push")
        return 0
    merged = dict(remote_data)
    owned = set(owned_source_keys) if owned_source_keys is not None else set(local_data)
    for key in owned:
        if key in local_data:
            merged[key] = local_data[key]
    local_path.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n")
    storage.put_file(f"{STATE_PREFIX}/{rel_path}", local_path, "application/json")
    return 1


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
