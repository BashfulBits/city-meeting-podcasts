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

from pathlib import Path

STATE_PREFIX = "state"
# JSON-typed; everything we persist is text. Kept conservative so a stray binary never syncs.
_SUFFIXES = {".json", ".jsonl"}


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
        if storage.get_file(key, state_dir / rel):
            restored += 1
    return restored


def push_state(storage, state_dir: Path) -> int:
    """Upload every state file under ``state_dir`` to the bucket's ``state/`` prefix.
    Returns the number of files pushed. No-op for backends without sync support."""
    if not _supported(storage) or not hasattr(storage, "put_file"):
        return 0
    state_dir = Path(state_dir)
    if not state_dir.exists():
        return 0
    pushed = 0
    for path in sorted(state_dir.rglob("*")):
        if not path.is_file() or path.suffix not in _SUFFIXES:
            continue
        rel = path.relative_to(state_dir).as_posix()
        storage.put_file(f"{STATE_PREFIX}/{rel}", path, "application/json")
        pushed += 1
    return pushed
