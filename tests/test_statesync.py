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


def test_push_state_only_prefixes_scopes_to_owned_sources(tmp_path):
    """H6b scope hook: a source-sharded job pulls the whole prefix for render context but must push
    back ONLY the source_keys it owns, or it re-uploads its stale copy of a sibling shard's record
    (the cross-shard clobber). ``only_prefixes`` restricts the push to the owned paths."""
    bucket = LocalStorage(root=tmp_path / "bucket", url_prefix="https://x")
    state_dir = tmp_path / "state"
    for key in ("mine", "theirs"):
        (state_dir / "sources" / key).mkdir(parents=True)
        (state_dir / "sources" / key / "episodes.json").write_text("{}")
    # A non-source state file (run history) must also be excluded by a sources-only scope.
    (state_dir / "run_summary.json").write_text("{}")

    pushed = push_state(bucket, state_dir, only_prefixes=["sources/mine/"])

    assert pushed == 1
    assert bucket.exists(f"{STATE_PREFIX}/sources/mine/episodes.json")
    assert not bucket.exists(f"{STATE_PREFIX}/sources/theirs/episodes.json")
    assert not bucket.exists(f"{STATE_PREFIX}/run_summary.json")

    # The default (no scope) still pushes everything — the single-writer / full-run case.
    assert push_state(bucket, state_dir) == 3
    assert bucket.exists(f"{STATE_PREFIX}/sources/theirs/episodes.json")


def test_scoped_run_event_push_does_not_delete_prior_events(tmp_path):
    """Scoped shard pushes are upload-only, so later ASR events do not erase audio events."""
    bucket = LocalStorage(root=tmp_path / "bucket", url_prefix="https://x")
    state_dir = tmp_path / "state"
    events = state_dir / "run_events"
    events.mkdir(parents=True)

    old_event = _tmpfile(tmp_path, '{"lane":"audio"}')
    bucket.put_file(f"{STATE_PREFIX}/run_events/audio.json", old_event, "application/json")

    (events / "asr.json").write_text('{"lane":"transcribe"}')
    pushed = push_state(bucket, state_dir, only_prefixes=["run_events/"])

    assert pushed == 1
    assert bucket.exists(f"{STATE_PREFIX}/run_events/audio.json")
    assert bucket.exists(f"{STATE_PREFIX}/run_events/asr.json")


def test_reconcile_state_full_run_guard(tmp_path):
    """A source-sharded job owns only a subset, so reconcile (which reaps every remote object with
    no local counterpart) would delete its siblings' records. ``full_run=False`` makes it a no-op;
    the periodic full run does the sweep."""
    bucket = LocalStorage(root=tmp_path / "bucket", url_prefix="https://x")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    bucket.put_file(
        f"{STATE_PREFIX}/sources/stale/episodes.json",
        _tmpfile(tmp_path, "{}"),
        "application/json",
    )
    _age(bucket, f"{STATE_PREFIX}/sources/stale/episodes.json", days=30)

    # A shard must not sweep — the orphan survives.
    assert reconcile_state(bucket, state_dir, full_run=False) == 0
    assert bucket.exists(f"{STATE_PREFIX}/sources/stale/episodes.json")

    # The full run reaps it as before.
    assert reconcile_state(bucket, state_dir, full_run=True) == 1
    assert not bucket.exists(f"{STATE_PREFIX}/sources/stale/episodes.json")


def _tmpfile(tmp_path, text: str):
    p = tmp_path / "_payload.json"
    p.write_text(text)
    return p
