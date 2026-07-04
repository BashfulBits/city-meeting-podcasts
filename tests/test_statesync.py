"""Tests for durable state sync + reconciliation against the LocalStorage backend."""

from __future__ import annotations

import json
import os
import time

import pytest

from citypods.records import load_records, protected_blocks_for_lane, save_records
from citypods.statesync import (
    STATE_PREFIX,
    fetch_remote_records,
    pull_state,
    push_asr_runtime_log_merged,
    push_records_merged,
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


# --- cross-lane write isolation: foreign-block-preserving push (review/12 §H6) ----------


def _seed_remote(bucket: LocalStorage, src_key: str, episodes: dict) -> None:
    """Write a source's records straight into the bucket's ``state/`` layout (the 'remote')."""
    path = bucket.root / STATE_PREFIX / "sources" / src_key / "episodes.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 2, "episodes": episodes}))


def test_fetch_remote_records_absent_returns_empty_not_none(tmp_path):
    bucket = LocalStorage(root=tmp_path / "bucket", url_prefix="https://x")
    # No remote file yet → {} (first writer, no clobber possible), distinct from None (read error).
    assert fetch_remote_records(bucket, "nope") == {}


def test_fetch_remote_records_reads_existing(tmp_path):
    bucket = LocalStorage(root=tmp_path / "bucket", url_prefix="https://x")
    _seed_remote(bucket, "s", {"u": {"uid": "u", "audio": {"url": "R"}}})
    assert fetch_remote_records(bucket, "s") == {"u": {"uid": "u", "audio": {"url": "R"}}}


def test_push_records_merged_preserves_concurrent_audio(tmp_path):
    """The reported regression: an ASR (transcribe) shard pulled state before an audio run wrote a
    new hosted-audio URL. On push it must preserve the remote's newer audio block while writing its
    own fresh transcript — not re-upload its stale start-of-run audio (cross-lane lost update)."""
    bucket = LocalStorage(root=tmp_path / "bucket", url_prefix="https://x")
    state_dir = tmp_path / "state"
    sk = "src1"
    # Remote (freshest): an audio run hosted audio AFTER this ASR run's pull_state.
    _seed_remote(bucket, sk, {"u1": {"uid": "u1", "audio": {"url": "NEW", "key": "kNEW"}}})
    # Local (this ASR shard): stale audio from its start-of-run snapshot + a fresh transcript.
    save_records(
        state_dir,
        sk,
        {"u1": {"uid": "u1", "audio": {"url": None}, "transcript": {"key": "t1", "synced": True}}},
    )

    pushed = push_records_merged(
        bucket, state_dir, [sk], protected_blocks=protected_blocks_for_lane("transcribe")
    )
    assert pushed == 1

    # What landed in the bucket: audio preserved (not clobbered), transcript written.
    restored = tmp_path / "restored"
    pull_state(bucket, restored)
    final = load_records(restored, sk)["u1"]
    assert final["audio"] == {"url": "NEW", "key": "kNEW"}
    assert final["transcript"]["synced"] is True
    # The local on-disk copy is rewritten to match what was pushed (so a later reuse sees the URL).
    assert load_records(state_dir, sk)["u1"]["audio"]["url"] == "NEW"


def test_push_records_merged_preserves_remote_decoded_plan_from_stale_audio_lane(tmp_path):
    """An audio shard that started from stale state must not overwrite a repaired decoded plan."""
    bucket = LocalStorage(root=tmp_path / "bucket", url_prefix="https://x")
    state_dir = tmp_path / "state"
    sk = "src1"
    _seed_remote(
        bucket,
        sk,
        {
            "u1": {
                "uid": "u1",
                "sources": [{"id": "s0", "duration": 100.0, "duration_basis": "decoded"}],
                "timeline": {"version": "silence:2+swagit-concat:1", "segments": []},
                "audio": {"url": "REMOTE", "key": "decoded-key", "spec_hash": "decoded-spec"},
            }
        },
    )
    save_records(
        state_dir,
        sk,
        {
            "u1": {
                "uid": "u1",
                "sources": [{"id": "s0", "duration": 101.0, "duration_basis": "container"}],
                "timeline": {"version": "silence:2+swagit-concat:1", "segments": []},
                "audio": {"url": "LOCAL", "key": "container-key", "spec_hash": "container-spec"},
            }
        },
    )

    pushed = push_records_merged(
        bucket, state_dir, [sk], protected_blocks=protected_blocks_for_lane("audio")
    )

    assert pushed == 1
    restored = tmp_path / "restored"
    pull_state(bucket, restored)
    final = load_records(restored, sk)["u1"]
    assert final["sources"][0]["duration_basis"] == "decoded"
    assert final["audio"]["key"] == "decoded-key"
    assert load_records(state_dir, sk)["u1"]["sources"][0]["duration_basis"] == "decoded"


def test_push_records_merged_owned_uids_no_sibling_shard_clobber(tmp_path):
    """Two transcribe shards split ONE source per-episode (review/18 §3.2). Each pulled the whole
    source, so each local carries a snapshot-stale transcript for the uid it does not own. Pushing
    with owned_uids must let each shard write only its uid and preserve the sibling's fresh one."""
    bucket = LocalStorage(root=tmp_path / "bucket", url_prefix="https://x")
    sk = "src1"
    _seed_remote(
        bucket,
        sk,
        {"uid1": {"uid": "uid1", "transcript": None}, "uid2": {"uid": "uid2", "transcript": None}},
    )
    protected = protected_blocks_for_lane("transcribe")

    # Shard A owns uid1: writes uid1's transcript; its uid2 copy is the start-of-run snapshot.
    state_a = tmp_path / "state_a"
    save_records(
        state_a,
        sk,
        {
            "uid1": {"uid": "uid1", "transcript": {"key": "A", "synced": True}},
            "uid2": {"uid": "uid2", "transcript": None},
        },
    )
    assert (
        push_records_merged(
            bucket, state_a, [sk], protected_blocks=protected, owned_uids={sk: frozenset({"uid1"})}
        )
        == 1
    )

    # Shard B owns uid2. It pulled BEFORE A's push, so its uid1 transcript is stale (None). On push
    # it re-reads the freshest remote (now carrying A's uid1) and must preserve it.
    state_b = tmp_path / "state_b"
    save_records(
        state_b,
        sk,
        {
            "uid1": {"uid": "uid1", "transcript": None},
            "uid2": {"uid": "uid2", "transcript": {"key": "B", "synced": True}},
        },
    )
    assert (
        push_records_merged(
            bucket, state_b, [sk], protected_blocks=protected, owned_uids={sk: frozenset({"uid2"})}
        )
        == 1
    )

    restored = tmp_path / "restored"
    pull_state(bucket, restored)
    final = load_records(restored, sk)
    assert final["uid1"]["transcript"]["key"] == "A"  # sibling shard not regressed
    assert final["uid2"]["transcript"]["key"] == "B"


def test_push_records_merged_first_write_when_remote_absent(tmp_path):
    bucket = LocalStorage(root=tmp_path / "bucket", url_prefix="https://x")
    state_dir = tmp_path / "state"
    save_records(state_dir, "fresh", {"u": {"uid": "u", "audio": {"url": "A"}}})
    # No remote yet → push the local record whole (no clobber risk).
    assert push_records_merged(bucket, state_dir, ["fresh"], protected_blocks=frozenset()) == 1
    assert bucket.exists(f"{STATE_PREFIX}/sources/fresh/episodes.json")


class _UnreadableBucket(LocalStorage):
    """A backend whose objects list as present but fail to download — a transient read error."""

    def get_file(self, key, local_path) -> bool:
        return False


def test_push_records_merged_skips_unreadable_remote_rather_than_clobber(tmp_path):
    bucket = _UnreadableBucket(root=tmp_path / "bucket", url_prefix="https://x")
    state_dir = tmp_path / "state"
    sk = "src1"
    _seed_remote(bucket, sk, {"u1": {"uid": "u1", "audio": {"url": "NEW"}}})
    save_records(state_dir, sk, {"u1": {"uid": "u1", "audio": {"url": None}}})

    # Remote is present but unreadable → fail safe: skip the push, never clobber the newer remote.
    pushed = push_records_merged(bucket, state_dir, [sk], protected_blocks=frozenset({"audio"}))
    assert pushed == 0
    remote_file = bucket.root / STATE_PREFIX / "sources" / sk / "episodes.json"
    assert json.loads(remote_file.read_text())["episodes"]["u1"]["audio"]["url"] == "NEW"


def _runtime_log(samples):
    return {"version": 1, "samples": samples}


def _runtime_sample(sample_id, finished_at, transcribe_seconds):
    return {
        "id": sample_id,
        "finished_at": finished_at,
        "transcribe_seconds": transcribe_seconds,
        "recording_seconds": 100,
    }


def test_push_asr_runtime_log_merged_unions_remote_and_local_samples(tmp_path):
    bucket = LocalStorage(root=tmp_path / "bucket", url_prefix="https://x")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    remote_file = _tmpfile(
        tmp_path,
        json.dumps(_runtime_log([_runtime_sample("remote", 10, 10)])),
    )
    bucket.put_file(f"{STATE_PREFIX}/asr_runtime_log.json", remote_file, "application/json")
    (state_dir / "asr_runtime_log.json").write_text(
        json.dumps(_runtime_log([_runtime_sample("local", 20, 20)]))
    )

    assert push_asr_runtime_log_merged(bucket, state_dir) == 1

    restored = tmp_path / "restored"
    pull_state(bucket, restored)
    samples = json.loads((restored / "asr_runtime_log.json").read_text())["samples"]
    assert {s["id"] for s in samples} == {"remote", "local"}


def test_push_asr_runtime_log_merged_keeps_newest_100(tmp_path):
    bucket = LocalStorage(root=tmp_path / "bucket", url_prefix="https://x")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    samples = [_runtime_sample(f"s{i}", i, i + 1) for i in range(101)]
    (state_dir / "asr_runtime_log.json").write_text(json.dumps(_runtime_log(samples)))

    assert push_asr_runtime_log_merged(bucket, state_dir) == 1

    data = json.loads((state_dir / "asr_runtime_log.json").read_text())
    assert len(data["samples"]) == 100
    assert data["samples"][0]["id"] == "s1"


def test_push_asr_runtime_log_merged_skips_unreadable_remote(tmp_path):
    bucket = _UnreadableBucket(root=tmp_path / "bucket", url_prefix="https://x")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    remote_file = _tmpfile(
        tmp_path,
        json.dumps(_runtime_log([_runtime_sample("remote", 10, 10)])),
    )
    bucket.put_file(f"{STATE_PREFIX}/asr_runtime_log.json", remote_file, "application/json")
    (state_dir / "asr_runtime_log.json").write_text(
        json.dumps(_runtime_log([_runtime_sample("local", 20, 20)]))
    )

    assert push_asr_runtime_log_merged(bucket, state_dir) == 0

    remote_path = bucket.root / STATE_PREFIX / "asr_runtime_log.json"
    samples = json.loads(remote_path.read_text())["samples"]
    assert {s["id"] for s in samples} == {"remote"}


def _tmpfile(tmp_path, text: str):
    p = tmp_path / "_payload.json"
    p.write_text(text)
    return p


# ── coordination-key exclusion from the bulk sync (H17 PR3) ───────────────────────


class _CASLocal(LocalStorage):
    """A LocalStorage that advertises CAS (like an R2-backed routing storage) so statesync treats
    coordination keys (state/compute_budget.json) as CAS-managed and skips them in the bulk sync."""

    cas_capable = True


def test_push_state_excludes_cas_managed_budget(tmp_path):
    bucket = _CASLocal(root=tmp_path / "bucket", url_prefix="https://x")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "compute_budget.json").write_text('{"month": "2026-06", "backends": {}}')
    save_records(state_dir, "src1", {"u1": {"uid": "u1"}})

    pushed = push_state(bucket, state_dir)
    # The CAS-managed ledger is NOT bulk-pushed (it would clobber the R2 CAS object); records are.
    assert not bucket.exists(f"{STATE_PREFIX}/compute_budget.json")
    assert bucket.exists(f"{STATE_PREFIX}/sources/src1/episodes.json")
    assert pushed == 1


def test_push_state_includes_budget_for_non_cas_backend(tmp_path):
    # A plain B2/local backend (no real CAS) keeps the prior behavior: the ledger rides the sync.
    bucket = LocalStorage(root=tmp_path / "bucket", url_prefix="https://x")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "compute_budget.json").write_text('{"month": "2026-06", "backends": {}}')

    push_state(bucket, state_dir)
    assert bucket.exists(f"{STATE_PREFIX}/compute_budget.json")


def test_pull_state_skips_cas_managed_budget(tmp_path):
    bucket = _CASLocal(root=tmp_path / "bucket", url_prefix="https://x")
    # Seed a budget + a record on the remote.
    import tempfile

    for key, body in (
        (f"{STATE_PREFIX}/compute_budget.json", '{"month": "2026-06", "backends": {}}'),
        (f"{STATE_PREFIX}/sources/src1/episodes.json", '{"episodes": {}}'),
    ):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
            tf.write(body)
            tmp = tf.name
        bucket.put_file(key, tmp, "application/json")

    state_dir = tmp_path / "state"
    restored = pull_state(bucket, state_dir)
    # The CAS-managed ledger is not restored into the local snapshot; the record is.
    assert not (state_dir / "compute_budget.json").exists()
    assert (state_dir / "sources" / "src1" / "episodes.json").exists()
    assert restored == 1


class _FlakyKeyError(Exception):
    """Stand-in for a real backend's connectivity-level error (timeout, dropped connection)."""


class _FlakyBucket(LocalStorage):
    """A backend where one specific key raises on every ``get_file`` — simulating the Backblaze
    B2 object that reproducibly failed with ``RetriesExceededError`` in build 452-455 (GH#455)."""

    flaky_key: str | None = None
    flaky_error: type[BaseException] = _FlakyKeyError

    def get_file(self, key, local_path) -> bool:
        if key == self.flaky_key:
            raise self.flaky_error("connection reset by peer")
        return super().get_file(key, local_path)


def test_pull_state_skips_key_with_transient_download_error(tmp_path, monkeypatch):
    from citypods.storage import s3 as s3_module

    monkeypatch.setattr(s3_module, "transient_download_errors", lambda: (_FlakyKeyError,))

    bucket = _FlakyBucket(root=tmp_path / "bucket", url_prefix="https://x")
    bucket.flaky_key = f"{STATE_PREFIX}/sources/bad/episodes.json"
    _seed_remote(bucket, "bad", {"u": {"uid": "u"}})
    _seed_remote(bucket, "good", {"u": {"uid": "u"}})

    state_dir = tmp_path / "state"
    warnings = []
    restored = pull_state(bucket, state_dir, log=warnings.append)

    # The flaky key is skipped (not raised through) and the rest of the snapshot still restores.
    assert restored == 1
    assert (state_dir / "sources" / "good" / "episodes.json").exists()
    assert not (state_dir / "sources" / "bad" / "episodes.json").exists()
    assert any("bad" in w for w in warnings)


def test_pull_state_propagates_non_transient_error(tmp_path, monkeypatch):
    """Only the transient set is swallowed — a real (operator) error still surfaces, so a
    regression that made ``pull_state`` catch everything wouldn't slip past this suite."""
    from citypods.storage import s3 as s3_module

    monkeypatch.setattr(s3_module, "transient_download_errors", lambda: (_FlakyKeyError,))

    bucket = _FlakyBucket(root=tmp_path / "bucket", url_prefix="https://x")
    bucket.flaky_key = f"{STATE_PREFIX}/sources/bad/episodes.json"
    bucket.flaky_error = RuntimeError
    _seed_remote(bucket, "bad", {"u": {"uid": "u"}})

    with pytest.raises(RuntimeError):
        pull_state(bucket, tmp_path / "state")


def test_reconcile_state_skips_cas_managed_budget(tmp_path):
    bucket = _CASLocal(root=tmp_path / "bucket", url_prefix="https://x")
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    budget = _tmpfile(tmp_path, '{"month": "2026-06", "backends": {}}')
    stale = _tmpfile(tmp_path, '{"episodes": {}}')
    bucket.put_file(f"{STATE_PREFIX}/compute_budget.json", budget, "application/json")
    bucket.put_file(f"{STATE_PREFIX}/sources/stale/episodes.json", stale, "application/json")
    _age(bucket, f"{STATE_PREFIX}/compute_budget.json", days=30)
    _age(bucket, f"{STATE_PREFIX}/sources/stale/episodes.json", days=30)

    assert reconcile_state(bucket, state_dir) == 1
    assert bucket.exists(f"{STATE_PREFIX}/compute_budget.json")
    assert not bucket.exists(f"{STATE_PREFIX}/sources/stale/episodes.json")


def test_cas_managed_uses_router_instance_prefixes_not_global(tmp_path):
    # A RoutingStorage built with CUSTOM coordination_prefixes must drive the bulk-sync exclusion
    # from its own config, not the module-level COORDINATION_PREFIXES (which could diverge).
    from citypods.statesync import _is_cas_managed
    from citypods.storage.routing import COORDINATION_PREFIXES, RoutingStorage

    class _CASPrimary(LocalStorage):
        cas_capable = True

    primary = LocalStorage(root=tmp_path / "b2", url_prefix="https://b2")
    coord = _CASPrimary(root=tmp_path / "r2", url_prefix="https://r2")
    router = RoutingStorage(
        primary=primary,
        coordination=coord,
        coordination_prefixes=("state/custom_coord.json",),
    )
    # The router's own prefix is CAS-managed; the global default budget key is NOT (this router
    # doesn't manage it) — proving statesync consults the instance, not the constant.
    assert _is_cas_managed(router, "state/custom_coord.json") is True
    assert _is_cas_managed(router, "state/compute_budget.json") is False
    assert "state/compute_budget.json" in COORDINATION_PREFIXES  # would be excluded if using global
