"""Tests for durable state sync + reconciliation against the LocalStorage backend."""

from __future__ import annotations

import os
import time

from citypods.statesync import (
    STATE_PREFIX,
    pull_state,
    push_state,
    reconcile_state,
)
from citypods.storage.local import LocalStorage


def _age(store: LocalStorage, key: str, days: float) -> None:
    """Backdate a remote object's mtime so the age guard treats it as old."""
    path = store._path(key)
    old = time.time() - days * 86400
    os.utime(path, (old, old))


def test_push_then_reconcile_reclaims_orphans_keeps_current(tmp_path):
    bucket = LocalStorage(root=tmp_path / "bucket", url_prefix="https://x")
    state_dir = tmp_path / "state"

    # A current local record + a stale remote-only one (e.g. left by an old source_key).
    (state_dir / "sources" / "current").mkdir(parents=True)
    (state_dir / "sources" / "current" / "episodes.json").write_text("{}")
    bucket.put_file(
        f"{STATE_PREFIX}/sources/stale/episodes.json",
        _tmpfile(tmp_path, "{}"),
        "application/json",
    )
    _age(bucket, f"{STATE_PREFIX}/sources/stale/episodes.json", days=30)

    assert push_state(bucket, state_dir) == 1
    assert reconcile_state(bucket, state_dir) == 1

    # Stale remote-only object is gone; the current one survives the sweep...
    assert not bucket.exists(f"{STATE_PREFIX}/sources/stale/episodes.json")
    assert bucket.exists(f"{STATE_PREFIX}/sources/current/episodes.json")
    # ...and pull no longer restores the stale record.
    restored = pull_state(bucket, tmp_path / "restored")
    assert (tmp_path / "restored" / "sources" / "current" / "episodes.json").exists()
    assert not (tmp_path / "restored" / "sources" / "stale" / "episodes.json").exists()
    assert restored == 1


def test_reconcile_age_guard_keeps_young_orphans(tmp_path):
    bucket = LocalStorage(root=tmp_path / "bucket", url_prefix="https://x")
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    # Remote-only object with no local counterpart, but freshly written: must be kept,
    # mirroring gc_audio's floor (a build may not have written its local copy yet).
    bucket.put_file(
        f"{STATE_PREFIX}/sources/fresh/episodes.json",
        _tmpfile(tmp_path, "{}"),
        "application/json",
    )

    assert reconcile_state(bucket, state_dir, min_age_days=7.0) == 0
    assert bucket.exists(f"{STATE_PREFIX}/sources/fresh/episodes.json")

    # Once it ages past the floor it becomes reclaimable.
    _age(bucket, f"{STATE_PREFIX}/sources/fresh/episodes.json", days=10)
    assert reconcile_state(bucket, state_dir, min_age_days=7.0) == 1
    assert not bucket.exists(f"{STATE_PREFIX}/sources/fresh/episodes.json")


def _tmpfile(tmp_path, text: str):
    p = tmp_path / "_payload.json"
    p.write_text(text)
    return p
