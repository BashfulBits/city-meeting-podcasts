"""Tests for the Stage-2 pull-based work-lease ledger (H17 PR4 / review/18 §4).

A small in-memory CAS bucket stands in for R2 and counts ops by billing class, so the cost
discipline (review/18 §4.6 — ≈1 Class-A per *claimed* item, no failed-claim writes, no listing) is
asserted directly, not just assumed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from citypods.ops import work_leases as wl
from citypods.storage import CASConflict

NOW = datetime(2026, 6, 14, 12, 0, tzinfo=UTC)


class _MemCAS:
    """In-memory CAS store with op counters. ``class_a`` = writes (put_cas); ``class_b`` = reads."""

    cas_capable = True

    def __init__(self):
        self.objs: dict[str, bytes] = {}
        self.etags: dict[str, str] = {}
        self._n = 0
        self.class_a = 0
        self.class_b = 0

    def _bump(self, key: str) -> str:
        self._n += 1
        self.etags[key] = f'"e{self._n}"'
        return self.etags[key]

    def get_bytes(self, key):
        self.class_b += 1
        if key not in self.objs:
            return None
        return self.objs[key], self.etags[key]

    def put_cas(self, key, data, content_type, *, if_none_match=None, if_match=None):
        self.class_a += 1
        exists = key in self.objs
        if if_none_match == "*" and exists:
            raise CASConflict(key)
        if if_match is not None and (not exists or self.etags.get(key) != if_match):
            raise CASConflict(key)
        self.objs[key] = data
        return "mem://" + key, self._bump(key)

    # listing is deliberately NOT implemented: the ledger must never list (review/18 §4.6 lever 1)
    def list_objects(self, prefix=""):
        raise AssertionError("the lease ledger must never list the R2 prefix")


def test_lease_key_is_derived_per_item():
    assert wl.lease_key("src1", "uid1") == "work-leases/src1/uid1.json"


def test_claim_creates_lease_when_absent():
    bucket = _MemCAS()
    held = wl.claim(bucket, "s1", "u1", owner="w1", ttl_seconds=600, pipeline_version="v3", now=NOW)
    assert held is not None
    assert held.state == "leased" and held.owner == "w1" and held.attempts == 1
    assert held.pipeline_version == "v3"
    assert held.lease_expiry == NOW + timedelta(seconds=600)
    # 1 read-before-claim (Class-B) + 1 claim write (Class-A).
    assert (bucket.class_a, bucket.class_b) == (1, 1)


def test_claim_skips_held_unexpired_lease_without_a_write():
    bucket = _MemCAS()
    wl.claim(bucket, "s1", "u1", owner="w1", ttl_seconds=600, now=NOW)
    a0 = bucket.class_a
    # A second worker finds it leased+unexpired → returns None WITHOUT spending a Class-A write.
    assert wl.claim(bucket, "s1", "u1", owner="w2", ttl_seconds=600, now=NOW) is None
    assert bucket.class_a == a0  # no failed-claim write (cost lever 2)


def test_claim_reclaims_expired_lease_and_bumps_attempts():
    bucket = _MemCAS()
    wl.claim(bucket, "s1", "u1", owner="w1", ttl_seconds=600, now=NOW - timedelta(hours=1))
    held = wl.claim(bucket, "s1", "u1", owner="w2", ttl_seconds=600, now=NOW)
    assert held is not None and held.owner == "w2" and held.attempts == 2


def test_claim_returns_none_on_cas_conflict():
    # Simulate a sibling winning between our read and our write.
    bucket = _MemCAS()
    real = bucket.put_cas

    def conflict_once(key, data, ct, *, if_none_match=None, if_match=None):
        bucket.put_cas = real  # only the first attempt conflicts
        raise CASConflict(key)

    bucket.put_cas = conflict_once
    assert wl.claim(bucket, "s1", "u1", owner="w1", ttl_seconds=600, now=NOW) is None


def test_terminal_states_are_not_claimable():
    bucket = _MemCAS()
    wl.claim(bucket, "s1", "u1", owner="w1", ttl_seconds=600, pipeline_version="v1", now=NOW)
    assert wl.release(bucket, "s1", "u1", owner="w1", state="failed", now=NOW) is True
    # A terminal lease remains closed for its same recipe version.
    assert (
        wl.claim(bucket, "s1", "u1", owner="w2", ttl_seconds=600, pipeline_version="v1", now=NOW)
        is None
    )


@pytest.mark.parametrize("state", ["done", "failed"])
def test_version_bump_reopens_terminal_lease(state):
    bucket = _MemCAS()
    wl.claim(bucket, "s1", "u1", owner="w1", ttl_seconds=600, pipeline_version="v1", now=NOW)
    assert wl.release(bucket, "s1", "u1", owner="w1", state=state, now=NOW) is True

    held = wl.claim(bucket, "s1", "u1", owner="w2", ttl_seconds=600, pipeline_version="v2", now=NOW)

    assert held is not None
    assert held.state == "leased" and held.owner == "w2"
    assert held.pipeline_version == "v2" and held.attempts == 2


def test_version_bump_does_not_steal_active_lease():
    bucket = _MemCAS()
    wl.claim(bucket, "s1", "u1", owner="w1", ttl_seconds=600, pipeline_version="v1", now=NOW)

    assert (
        wl.claim(bucket, "s1", "u1", owner="w2", ttl_seconds=600, pipeline_version="v2", now=NOW)
        is None
    )


def test_requeue_failed_reopens_only_failed_leases():
    bucket = _MemCAS()
    wl.claim(bucket, "s1", "failed", owner="w1", ttl_seconds=600, now=NOW)
    wl.release(bucket, "s1", "failed", owner="w1", state="failed", now=NOW)
    wl.claim(bucket, "s1", "queued", owner="w2", ttl_seconds=600, now=NOW)

    summary = wl.requeue_failed(bucket, [("s1", "failed"), ("s1", "queued")], now=NOW)

    assert summary == {"scanned": 2, "requeued": 1, "skipped": 1, "conflicts": 0}
    reopened, _ = wl.read_lease(bucket, "s1", "failed")
    assert reopened.state == "queued" and reopened.owner == ""
    assert wl.claim(bucket, "s1", "failed", owner="w3", ttl_seconds=600, now=NOW) is not None
    still_held, _ = wl.read_lease(bucket, "s1", "queued")
    assert still_held.state == "leased" and still_held.owner == "w2"


def test_requeue_failed_dry_run_is_read_only():
    bucket = _MemCAS()
    wl.claim(bucket, "s1", "u1", owner="w1", ttl_seconds=600, now=NOW)
    wl.release(bucket, "s1", "u1", owner="w1", state="failed", now=NOW)
    writes = bucket.class_a

    summary = wl.requeue_failed(bucket, [("s1", "u1")], now=NOW, dry_run=True)

    assert summary["requeued"] == 1
    assert bucket.class_a == writes
    assert wl.read_lease(bucket, "s1", "u1")[0].state == "failed"


def test_renew_extends_only_for_holder():
    bucket = _MemCAS()
    wl.claim(bucket, "s1", "u1", owner="w1", ttl_seconds=600, now=NOW)
    later = NOW + timedelta(seconds=300)
    renewed = wl.renew(bucket, "s1", "u1", owner="w1", ttl_seconds=600, now=later)
    assert renewed is not None and renewed.lease_expiry == later + timedelta(seconds=600)
    # A non-holder cannot renew.
    assert wl.renew(bucket, "s1", "u1", owner="intruder", ttl_seconds=600, now=later) is None


def test_read_lease_treats_malformed_schema_as_claimable():
    # Valid JSON, invalid schema (attempts not an int) must not crash the worker — treat as
    # claimable (None) but keep the ETag so a claim cleanly replaces it.
    bucket = _MemCAS()
    key = wl.lease_key("s1", "u1")
    bucket.objs[key] = b'{"attempts": "not-an-int"}'
    bucket._n += 1
    bucket.etags[key] = f'"e{bucket._n}"'
    lease, etag = wl.read_lease(bucket, "s1", "u1")
    assert lease is None and etag is not None
    held = wl.claim(bucket, "s1", "u1", owner="w1", ttl_seconds=300, now=NOW)
    assert held is not None and held.state == "leased"


def test_renew_and_release_refuse_after_expiry():
    # Once expired we no longer hold the lease — the reaper owns it. A stale worker must not extend
    # dead work (renew) or terminally settle it (release); leave it for the reaper to requeue.
    bucket = _MemCAS()
    wl.claim(bucket, "s1", "u1", owner="w1", ttl_seconds=600, now=NOW)
    past_expiry = NOW + timedelta(hours=1)
    assert wl.renew(bucket, "s1", "u1", owner="w1", ttl_seconds=600, now=past_expiry) is None
    assert wl.release(bucket, "s1", "u1", owner="w1", state="failed", now=past_expiry) is False
    assert wl.read_lease(bucket, "s1", "u1")[0].state == "leased"  # untouched → reaper requeues


def test_reap_dry_run_previews_without_writing():
    bucket = _MemCAS()
    # expired claim
    wl.claim(bucket, "s1", "u1", owner="w1", ttl_seconds=600, now=NOW - timedelta(hours=1))
    a0 = bucket.class_a
    summary = wl.reap(
        bucket, [("s1", "u1")], artifact_present=lambda s, u: False, now=NOW, dry_run=True
    )
    assert summary == {"completed": 0, "requeued": 1, "in_flight": 0}
    assert bucket.class_a == a0  # read-only: no write
    assert wl.read_lease(bucket, "s1", "u1")[0].state == "leased"  # unchanged


def test_reap_dry_run_does_not_invoke_callbacks():
    # A dry run must stay entirely read-only, including the on_completed/on_requeued callbacks —
    # those are real CAS mutations (budget settle/release) in production.
    bucket = _MemCAS()
    wl.claim(bucket, "s1", "u1", owner="w1", ttl_seconds=600, now=NOW - timedelta(hours=1))
    a0 = bucket.class_a
    calls: list[str] = []
    summary = wl.reap(
        bucket,
        [("s1", "u1")],
        artifact_present=lambda s, u: False,
        on_completed=calls.append,
        on_requeued=calls.append,
        now=NOW,
        dry_run=True,
    )
    assert summary["requeued"] == 1
    assert calls == []
    assert bucket.class_a == a0


def test_release_rejects_non_terminal_state():
    # release must only settle to a terminal state; a "leased"/"queued" release would write a
    # non-terminal object (e.g. leased with no expiry) that can wedge the item.
    bucket = _MemCAS()
    wl.claim(bucket, "s1", "u1", owner="w1", ttl_seconds=300, now=NOW)
    with pytest.raises(ValueError, match="terminal"):
        wl.release(bucket, "s1", "u1", owner="w1", state="leased", now=NOW)


def test_release_requires_ownership():
    bucket = _MemCAS()
    wl.claim(bucket, "s1", "u1", owner="w1", ttl_seconds=600, now=NOW)
    assert wl.release(bucket, "s1", "u1", owner="intruder") is False
    assert wl.release(bucket, "s1", "u1", owner="w1", state="done", now=NOW) is True
    lease, _ = wl.read_lease(bucket, "s1", "u1")
    assert lease.state == "done" and lease.lease_expiry is None


def test_abandon_returns_own_fresh_claim_to_queue():
    bucket = _MemCAS()
    wl.claim(bucket, "s1", "u1", owner="w1", ttl_seconds=600, now=NOW)
    assert wl.abandon(bucket, "s1", "u1", owner="w1", now=NOW) is True
    lease, _ = wl.read_lease(bucket, "s1", "u1")
    assert lease.state == "queued"
    assert lease.owner == ""
    assert lease.lease_expiry is None


def test_abandon_refuses_non_holder_and_expired_claim():
    bucket = _MemCAS()
    wl.claim(bucket, "s1", "u1", owner="w1", ttl_seconds=600, now=NOW)
    assert wl.abandon(bucket, "s1", "u1", owner="w2", now=NOW) is False
    assert wl.abandon(bucket, "s1", "u1", owner="w1", now=NOW + timedelta(hours=1)) is False
    lease, _ = wl.read_lease(bucket, "s1", "u1")
    assert lease.state == "leased" and lease.owner == "w1"


def test_reap_settles_completed_requeues_expired_leaves_running():
    bucket = _MemCAS()
    # u1: leased + artifact present → completed
    wl.claim(bucket, "s1", "u1", owner="w1", ttl_seconds=600, now=NOW)
    # u2: leased + expired + no artifact → requeued
    wl.claim(bucket, "s1", "u2", owner="w2", ttl_seconds=600, now=NOW - timedelta(hours=1))
    # u3: leased + unexpired + no artifact → left running
    wl.claim(bucket, "s1", "u3", owner="w3", ttl_seconds=600, now=NOW)
    present = {("s1", "u1")}
    summary = wl.reap(
        bucket,
        [("s1", "u1"), ("s1", "u2"), ("s1", "u3")],
        artifact_present=lambda s, u: (s, u) in present,
        now=NOW,
    )
    assert summary == {"completed": 1, "requeued": 1, "in_flight": 1}
    assert wl.read_lease(bucket, "s1", "u1")[0].state == "done"
    requeued = wl.read_lease(bucket, "s1", "u2")[0]
    assert requeued.state == "queued" and requeued.owner == ""
    assert wl.read_lease(bucket, "s1", "u3")[0].state == "leased"


def test_reap_calls_callbacks_after_successful_write():
    bucket = _MemCAS()
    wl.claim(bucket, "s1", "done", owner="modal:done", ttl_seconds=600, now=NOW)
    wl.claim(
        bucket,
        "s1",
        "dead",
        owner="modal:dead",
        ttl_seconds=600,
        now=NOW - timedelta(hours=1),
    )
    completed: list[str] = []
    requeued: list[str] = []

    summary = wl.reap(
        bucket,
        [("s1", "done"), ("s1", "dead")],
        artifact_present=lambda _s, u: u == "done",
        on_completed=completed.append,
        on_requeued=requeued.append,
        now=NOW,
    )

    assert summary == {"completed": 1, "requeued": 1, "in_flight": 0}
    assert completed == ["modal:done"]
    assert requeued == ["modal:dead"]


def test_scan_offset_differs_by_worker():
    # Different workers start at different indices so they don't all collide on the newest item.
    offsets = {wl.scan_offset(f"worker-{i}", 4) for i in range(8)}
    assert len(offsets) > 1
    assert wl.scan_offset("anything", 0) == 0  # empty candidate set


def test_ordered_candidates_is_a_rotation_not_a_resort():
    # Public, generic over candidate shape: any worker composing its own loop on top of the
    # claim primitives (rather than calling run_claim_loop) should call this instead of
    # re-deriving the scan_offset modulo-rotation itself.
    candidates = ["a", "b", "c", "d"]
    offset = wl.scan_offset("worker-x", len(candidates))
    rotated = wl.ordered_candidates(candidates, "worker-x")
    assert rotated == candidates[offset:] + candidates[:offset]
    assert sorted(rotated) == sorted(candidates)  # same set, just rotated


def test_ordered_candidates_empty_is_noop():
    assert wl.ordered_candidates([], "worker-x") == []


def test_ordered_candidates_works_on_richer_objects_not_just_tuples():
    # Generic over T: a worker building its loop on top of full WorkItem objects (not just
    # (source_key, uid) tuples) can call this directly, same as run_claim_loop does internally.
    items = [{"uid": "a"}, {"uid": "b"}, {"uid": "c"}]
    rotated = wl.ordered_candidates(items, "worker-x")
    assert {it["uid"] for it in rotated} == {"a", "b", "c"}


def test_run_claim_loop_claims_then_infers_completion_without_done_write():
    bucket = _MemCAS()
    candidates = [("s1", f"u{i}") for i in range(4)]
    transcribed: list[tuple[str, str]] = []

    def transcribe(src, uid):
        transcribed.append((src, uid))

    summary = wl.run_claim_loop(
        bucket,
        candidates,
        owner="w1",
        transcribe=transcribe,
        ttl_seconds=600,
        pipeline_version="v3",
        now_fn=lambda: NOW,
    )
    assert summary == {"claimed": 4, "completed": 4, "failed": 0}
    assert set(transcribed) == set(candidates)
    # Completion is inferred (no done write) → every lease stays "leased" for the reaper to settle.
    assert all(wl.read_lease(bucket, *c)[0].state == "leased" for c in candidates)
    # ≈1 Class-A per claimed item (no done write, no failed-claim writes).
    assert bucket.class_a == 4


def test_run_claim_loop_marks_failed_on_inference_error():
    bucket = _MemCAS()
    candidates = [("s1", "u1")]

    def boom(src, uid):
        raise RuntimeError("gpu exploded")

    summary = wl.run_claim_loop(
        bucket, candidates, owner="w1", transcribe=boom, ttl_seconds=600, now_fn=lambda: NOW
    )
    assert summary == {"claimed": 1, "completed": 0, "failed": 1}
    assert wl.read_lease(bucket, "s1", "u1")[0].state == "failed"


def test_run_claim_loop_two_workers_split_work_no_double_claim():
    bucket = _MemCAS()
    candidates = [("s1", f"u{i}") for i in range(6)]
    done_a: list = []
    done_b: list = []
    wl.run_claim_loop(
        bucket,
        candidates,
        owner="A",
        transcribe=lambda s, u: done_a.append(u),
        ttl_seconds=600,
        now_fn=lambda: NOW,
    )
    wl.run_claim_loop(
        bucket,
        candidates,
        owner="B",
        transcribe=lambda s, u: done_b.append(u),
        ttl_seconds=600,
        now_fn=lambda: NOW,
    )
    # A claimed everything (leased); B finds them all held+unexpired → claims nothing (no overlap).
    assert len(done_a) == 6 and done_b == []


def test_run_claim_loop_respects_max_claims():
    bucket = _MemCAS()
    candidates = [("s1", f"u{i}") for i in range(10)]
    summary = wl.run_claim_loop(
        bucket,
        candidates,
        owner="w1",
        transcribe=lambda s, u: None,
        ttl_seconds=600,
        max_claims=3,
        now_fn=lambda: NOW,
    )
    assert summary["claimed"] == 3


def test_claim_with_update_index_adds_active_entry():
    bucket = _MemCAS()
    held = wl.claim(bucket, "s1", "u1", owner="w1", ttl_seconds=600, now=NOW, update_index=True)
    assert held is not None
    n = wl.index_bucket_for("s1", "u1")
    entries, _ = wl._load_index_bucket(bucket, n)
    assert entries[wl._entry_id("s1", "u1")]["lease_expiry"] == held.lease_expiry.isoformat()


def test_claim_without_update_index_leaves_index_untouched():
    bucket = _MemCAS()
    wl.claim(bucket, "s1", "u1", owner="w1", ttl_seconds=600, now=NOW)
    n = wl.index_bucket_for("s1", "u1")
    entries, etag = wl._load_index_bucket(bucket, n)
    assert entries == {} and etag is None  # no bucket object was ever written


def test_index_write_failure_never_fails_the_claim():
    # Acceptance criterion 7: the lease object stays claim authority precisely because the index
    # is allowed to fail. A storage double whose put_cas always raises on index keys proves the
    # claim still lands even though every index write attempt fails.
    class _IndexHostile(_MemCAS):
        def put_cas(self, key, data, content_type, **kw):
            if key.startswith(wl.INDEX_PREFIX):
                raise CASConflict(key)
            return super().put_cas(key, data, content_type, **kw)

    bucket = _IndexHostile()
    # _mutate_index_bucket retries with real backoff by default; every attempt fails here, so swap
    # in a no-op sleep for the duration of this test to keep it fast.
    orig_sleep = wl._time.sleep
    wl._time.sleep = lambda *a, **k: None
    try:
        held = wl.claim(
            bucket,
            "s1",
            "u1",
            owner="w1",
            ttl_seconds=600,
            now=NOW,
            update_index=True,
        )
    finally:
        wl._time.sleep = orig_sleep
    assert held is not None  # the lease landed despite the index being unwritable
    assert wl.read_lease(bucket, "s1", "u1")[0].state == "leased"
    assert wl._load_index_bucket(bucket, wl.index_bucket_for("s1", "u1"))[0] == {}


def test_renew_with_update_index_refreshes_expiry():
    bucket = _MemCAS()
    wl.claim(bucket, "s1", "u1", owner="w1", ttl_seconds=600, now=NOW, update_index=True)
    later = NOW + timedelta(seconds=300)
    renewed = wl.renew(
        bucket, "s1", "u1", owner="w1", ttl_seconds=600, now=later, update_index=True
    )
    n = wl.index_bucket_for("s1", "u1")
    entries, _ = wl._load_index_bucket(bucket, n)
    assert entries[wl._entry_id("s1", "u1")]["lease_expiry"] == renewed.lease_expiry.isoformat()


def test_release_with_update_index_drops_entry():
    bucket = _MemCAS()
    wl.claim(bucket, "s1", "u1", owner="w1", ttl_seconds=600, now=NOW, update_index=True)
    assert wl.release(bucket, "s1", "u1", owner="w1", state="done", now=NOW, update_index=True)
    n = wl.index_bucket_for("s1", "u1")
    entries, _ = wl._load_index_bucket(bucket, n)
    assert entries == {}


def test_abandon_with_update_index_drops_entry():
    bucket = _MemCAS()
    wl.claim(bucket, "s1", "u1", owner="w1", ttl_seconds=600, now=NOW, update_index=True)
    assert wl.abandon(bucket, "s1", "u1", owner="w1", now=NOW, update_index=True)
    n = wl.index_bucket_for("s1", "u1")
    entries, _ = wl._load_index_bucket(bucket, n)
    assert entries == {}


def test_reap_indexed_zero_active_reads_only_bounded_bucket_objects():
    bucket = _MemCAS()
    summary = wl.reap_indexed(bucket, artifact_present=lambda s, u: False, now=NOW)
    assert summary == {
        "completed": 0,
        "requeued": 0,
        "in_flight": 0,
        "indexed_buckets_read": wl.INDEX_BUCKET_COUNT,
        "integrity_checked": 0,
    }
    # No candidate lease keys were ever read — only the (empty) index buckets.
    assert bucket.class_b == wl.INDEX_BUCKET_COUNT
    assert bucket.class_a == 0


def test_reap_indexed_settles_completed_requeues_expired_leaves_running():
    bucket = _MemCAS()
    wl.claim(bucket, "s1", "u1", owner="w1", ttl_seconds=600, now=NOW, update_index=True)
    wl.claim(
        bucket,
        "s1",
        "u2",
        owner="w2",
        ttl_seconds=600,
        now=NOW - timedelta(hours=1),
        update_index=True,
    )
    wl.claim(bucket, "s1", "u3", owner="w3", ttl_seconds=600, now=NOW, update_index=True)
    present = {("s1", "u1")}
    summary = wl.reap_indexed(bucket, artifact_present=lambda s, u: (s, u) in present, now=NOW)
    assert summary["completed"] == 1
    assert summary["requeued"] == 1
    assert summary["in_flight"] == 1
    assert wl.read_lease(bucket, "s1", "u1")[0].state == "done"
    assert wl.read_lease(bucket, "s1", "u2")[0].state == "queued"
    assert wl.read_lease(bucket, "s1", "u3")[0].state == "leased"
    # settled items are dropped from the index; the still-running one stays indexed.
    n1, n2, n3 = (wl.index_bucket_for("s1", u) for u in ("u1", "u2", "u3"))
    assert wl._entry_id("s1", "u1") not in wl._load_index_bucket(bucket, n1)[0]
    assert wl._entry_id("s1", "u2") not in wl._load_index_bucket(bucket, n2)[0]
    assert wl._entry_id("s1", "u3") in wl._load_index_bucket(bucket, n3)[0]


def test_reap_indexed_prunes_multiple_settled_entries_in_one_bucket_write():
    # Several settled/drifted entries landing in the same bucket must cost one index CAS write,
    # not one per entry — the whole point of this sweep is cutting reconcile's op cost.
    bucket = _MemCAS()
    wl.claim(bucket, "s1", "u1", owner="w1", ttl_seconds=600, now=NOW)
    wl.claim(bucket, "s1", "u2", owner="w2", ttl_seconds=600, now=NOW - timedelta(hours=1))
    # Force both into the same (only) bucket so a per-entry write would show up as 2 index writes.
    wl._index_upsert(bucket, "s1", "u1", lease_expiry=NOW + timedelta(seconds=600), bucket_count=1)
    wl._index_upsert(bucket, "s1", "u2", lease_expiry=NOW - timedelta(seconds=1), bucket_count=1)
    a0 = bucket.class_a

    summary = wl.reap_indexed(
        bucket, artifact_present=lambda s, u: s == "s1" and u == "u1", bucket_count=1, now=NOW
    )

    assert summary["completed"] == 1 and summary["requeued"] == 1
    # 2 lease-settle writes + exactly 1 index-bucket prune write (not 2).
    assert bucket.class_a - a0 == 3
    assert wl._load_index_bucket(bucket, 0)[0] == {}


def test_integrity_partition_for_advances_every_minute_not_every_day():
    t0 = datetime(2026, 6, 14, 12, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=1)
    t_same_minute = t0 + timedelta(seconds=30)
    assert wl.integrity_partition_for(t0) != wl.integrity_partition_for(t1)
    assert wl.integrity_partition_for(t0) == wl.integrity_partition_for(t_same_minute)


def test_reap_indexed_calls_callbacks():
    bucket = _MemCAS()
    wl.claim(bucket, "s1", "done", owner="modal:done", ttl_seconds=600, now=NOW, update_index=True)
    wl.claim(
        bucket,
        "s1",
        "dead",
        owner="modal:dead",
        ttl_seconds=600,
        now=NOW - timedelta(hours=1),
        update_index=True,
    )
    completed: list[str] = []
    requeued: list[str] = []
    summary = wl.reap_indexed(
        bucket,
        artifact_present=lambda _s, u: u == "done",
        on_completed=completed.append,
        on_requeued=requeued.append,
        now=NOW,
    )
    assert summary["completed"] == 1 and summary["requeued"] == 1
    assert completed == ["modal:done"]
    assert requeued == ["modal:dead"]


def test_reap_indexed_dry_run_previews_without_writing_lease_or_index():
    bucket = _MemCAS()
    wl.claim(
        bucket,
        "s1",
        "u1",
        owner="w1",
        ttl_seconds=600,
        now=NOW - timedelta(hours=1),
        update_index=True,
    )
    a0 = bucket.class_a
    summary = wl.reap_indexed(bucket, artifact_present=lambda s, u: False, now=NOW, dry_run=True)
    assert summary["requeued"] == 1
    assert bucket.class_a == a0  # read-only: no lease write, no index write
    assert wl.read_lease(bucket, "s1", "u1")[0].state == "leased"  # unchanged
    n = wl.index_bucket_for("s1", "u1")
    assert wl._entry_id("s1", "u1") in wl._load_index_bucket(bucket, n)[0]  # still indexed


def test_reap_indexed_dry_run_does_not_invoke_callbacks():
    bucket = _MemCAS()
    wl.claim(
        bucket,
        "s1",
        "u1",
        owner="w1",
        ttl_seconds=600,
        now=NOW - timedelta(hours=1),
        update_index=True,
    )
    a0 = bucket.class_a
    calls: list[str] = []
    summary = wl.reap_indexed(
        bucket,
        artifact_present=lambda s, u: False,
        on_completed=calls.append,
        on_requeued=calls.append,
        now=NOW,
        dry_run=True,
    )
    assert summary["requeued"] == 1
    assert calls == []
    assert bucket.class_a == a0


def test_reap_indexed_drops_drifted_entries_not_actually_leased():
    # An entry can point at a lease that was already settled by a plain (non-indexed) release —
    # the index must self-heal rather than re-report it forever.
    bucket = _MemCAS()
    wl.claim(bucket, "s1", "u1", owner="w1", ttl_seconds=600, now=NOW, update_index=True)
    wl.release(bucket, "s1", "u1", owner="w1", state="done", now=NOW)  # no update_index
    n = wl.index_bucket_for("s1", "u1")
    assert wl._entry_id("s1", "u1") in wl._load_index_bucket(bucket, n)[0]  # stale entry present
    summary = wl.reap_indexed(bucket, artifact_present=lambda s, u: False, now=NOW)
    assert summary == {
        "completed": 0,
        "requeued": 0,
        "in_flight": 0,
        "indexed_buckets_read": wl.INDEX_BUCKET_COUNT,
        "integrity_checked": 0,
    }
    assert wl._entry_id("s1", "u1") not in wl._load_index_bucket(bucket, n)[0]


def test_reap_indexed_integrity_sweep_recovers_unindexed_active_lease():
    # Simulate a crash between the lease write and the index write: claim without update_index.
    bucket = _MemCAS()
    wl.claim(bucket, "s1", "u1", owner="w1", ttl_seconds=600, now=NOW)
    n = wl.index_bucket_for("s1", "u1")
    assert wl._load_index_bucket(bucket, n)[0] == {}  # not indexed
    summary = wl.reap_indexed(
        bucket,
        artifact_present=lambda s, u: False,
        now=NOW,
        integrity_candidates=[("s1", "u1")],
        integrity_partition=n,
    )
    assert summary["in_flight"] == 1
    assert summary["integrity_checked"] == 1
    # Repaired into the index so the next sweep finds it directly.
    assert wl._entry_id("s1", "u1") in wl._load_index_bucket(bucket, n)[0]


def test_reap_indexed_integrity_sweep_only_checks_matching_partition():
    bucket = _MemCAS()
    wl.claim(bucket, "s1", "u1", owner="w1", ttl_seconds=600, now=NOW)
    n = wl.index_bucket_for("s1", "u1")
    other_partition = (n + 1) % wl.INDEX_BUCKET_COUNT
    summary = wl.reap_indexed(
        bucket,
        artifact_present=lambda s, u: False,
        now=NOW,
        integrity_candidates=[("s1", "u1")],
        integrity_partition=other_partition,
    )
    assert summary["integrity_checked"] == 0
    assert wl._load_index_bucket(bucket, n)[0] == {}  # left un-repaired this run


def test_index_bucket_for_is_stable_and_bounded():
    n = wl.index_bucket_for("s1", "u1", bucket_count=8)
    assert 0 <= n < 8
    assert wl.index_bucket_for("s1", "u1", bucket_count=8) == n  # deterministic


def test_run_claim_loop_empty_candidates_is_noop():
    bucket = _MemCAS()
    summary = wl.run_claim_loop(
        bucket, [], owner="w1", transcribe=lambda s, u: None, ttl_seconds=600
    )
    assert summary == {"claimed": 0, "completed": 0, "failed": 0}
    assert (bucket.class_a, bucket.class_b) == (0, 0)
