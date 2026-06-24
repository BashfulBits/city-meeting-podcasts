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
    wl.claim(bucket, "s1", "u1", owner="w1", ttl_seconds=600, now=NOW)
    assert wl.release(bucket, "s1", "u1", owner="w1", state="failed", now=NOW) is True
    # failed is terminal → not auto-reclaimed.
    assert wl.claim(bucket, "s1", "u1", owner="w2", ttl_seconds=600, now=NOW) is None


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


def test_scan_offset_differs_by_worker():
    # Different workers start at different indices so they don't all collide on the newest item.
    offsets = {wl._scan_offset(f"worker-{i}", 4) for i in range(8)}
    assert len(offsets) > 1
    assert wl._scan_offset("anything", 0) == 0  # empty candidate set


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


def test_run_claim_loop_empty_candidates_is_noop():
    bucket = _MemCAS()
    summary = wl.run_claim_loop(
        bucket, [], owner="w1", transcribe=lambda s, u: None, ttl_seconds=600
    )
    assert summary == {"claimed": 0, "completed": 0, "failed": 0}
    assert (bucket.class_a, bucket.class_b) == (0, 0)
