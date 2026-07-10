"""Tests for cross-process provider leases (H17 PR6: per-slot CAS objects).

A small **thread-safe** in-memory CAS bucket stands in for R2. The pool now needs real
compare-and-swap (``get_bytes``/``put_cas``/``delete`` + ``cas_capable``); it never lists the slot
prefix, so the fake's ``list_objects`` raises if the pool ever calls it.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from citypods.http import StopRequested
from citypods.provider_leases import (
    DistributedProviderLeasePool,
    ProviderLeaseRule,
)
from tests._cas_fake import MemCAS

URL = "https://archive-video.granicus.com/x.mp4"


def _pool(
    store: MemCAS,
    *,
    slots: int = 1,
    ttl_seconds: float = 60,
    log=None,
    shard: str | None = None,
) -> DistributedProviderLeasePool:
    pool = DistributedProviderLeasePool(prefix="test-provider-leases")
    pool.configure(
        store,
        {
            "granicus.com": {
                "slots": slots,
                "ttl_seconds": ttl_seconds,
                "poll_seconds": 0.01,
            }
        },
        log=log,
        shard=shard,
    )
    return pool


def _held_count(store: MemCAS, prefix: str = "test-provider-leases/") -> int:
    """Count slots currently HELD. Release is an atomic CAS tombstone write (no delete), so a freed
    slot persists as an expired ``state == 'released'`` object — present but not held."""
    held = 0
    for key in store.keys(prefix):
        try:
            if json.loads(store.objs[key]).get("state") != "released":
                held += 1
        except (ValueError, TypeError):
            held += 1  # unreadable payload — treat as occupying the slot
    return held


def test_distributed_provider_lease_blocks_second_pool_until_release():
    store = MemCAS()
    first = _pool(store)
    second = _pool(store)
    acquired: list[str] = []

    def _waiter():
        with second.slots([URL]):
            acquired.append("second")

    with first.slots([URL]):
        t = threading.Thread(target=_waiter)
        t.start()
        time.sleep(0.05)
        assert acquired == []

    t.join(timeout=1)
    assert acquired == ["second"]
    assert _held_count(store) == 0  # both released (tombstoned) — no slot held


def test_two_slots_admit_two_holders_and_block_the_third():
    store = MemCAS()
    holders = [_pool(store, slots=2) for _ in range(2)]
    third = _pool(store, slots=2)
    third_in = threading.Event()

    cm0, cm1 = holders[0].slots([URL]), holders[1].slots([URL])
    cm0.__enter__()
    cm1.__enter__()
    try:
        assert _held_count(store) == 2

        def _waiter():
            with third.slots([URL]):
                third_in.set()

        t = threading.Thread(target=_waiter)
        t.start()
        time.sleep(0.05)
        assert not third_in.is_set()  # both slots full
        cm1.__exit__(None, None, None)  # free one slot
        t.join(timeout=1)
        assert third_in.is_set()
    finally:
        cm0.__exit__(None, None, None)


def test_distributed_provider_lease_ignores_unconfigured_domains():
    store = MemCAS()
    pool = _pool(store)
    with pool.slots(["https://example.com/x.mp4"]):
        pass
    assert store.keys("test-provider-leases/") == []


def test_acquire_falls_back_to_noop_lease_when_rules_cleared_mid_wait():
    # CR2-CP-36: a concurrent configure() disabling leases clears `_storage` and `_rules`
    # together. `_acquire` used to look up `_rules[domain]` unconditionally, so a domain that
    # existed when the caller entered `slots()` but is gone by the time `_acquire` runs would
    # KeyError instead of returning the same no-op lease the storage-disabled path returns.
    store = MemCAS()
    pool = _pool(store)
    pool._storage = None
    pool._rules = {}

    lease = pool._acquire("granicus.com")

    assert lease.key == ""
    assert lease.rule is None


def test_pool_disabled_when_backend_lacks_cas():
    """Without CAS (b2-only / local dev) the distributed layer is a no-op; only the in-process
    HostRateLimiter applies, so ``slots()`` neither blocks nor writes anything."""

    class NonCasStore:
        name = "noncas"
        cas_capable = False

        def put_file(self, key, local_path, content_type):
            raise AssertionError("disabled pool must not write")

        def list_objects(self, prefix=""):
            return []

        def delete(self, key):
            raise AssertionError("disabled pool must not delete")

    messages: list[str] = []
    pool = DistributedProviderLeasePool(prefix="test-provider-leases")
    pool.configure(
        NonCasStore(), {"granicus.com": {"slots": 1}}, log=lambda m, **_k: messages.append(m)
    )

    with pool.slots([URL]):
        pass  # no-op; nothing raised

    assert any("disabled" in m and "CAS" in m for m in messages)


def test_renew_interval_is_capped_so_lowering_ttl_costs_no_renewal_traffic():
    """GH#378: the production Granicus TTL drop 3600→900 is free because the renewal cadence is
    clamped at 60s for any ttl >= 180 (180/3). If this cap ever drops below 300s a future TTL
    reduction would start increasing renewal traffic — this guards that reasoning."""

    def interval(ttl: float) -> float:
        rule = ProviderLeaseRule(slots=2, ttl_seconds=ttl)
        return DistributedProviderLeasePool._renew_interval(rule)

    assert interval(3600) == 60.0
    assert interval(900) == 60.0
    assert interval(3600) == interval(900)  # unchanged renewal cadence across the reduction


def test_active_lease_renews_past_ttl_and_blocks_waiter():
    store = MemCAS()
    first = _pool(store, ttl_seconds=0.15)
    second = _pool(store, ttl_seconds=0.15)
    acquired = threading.Event()

    def _waiter():
        with second.slots([URL]):
            acquired.set()

    with first.slots([URL]):
        thread = threading.Thread(target=_waiter)
        thread.start()
        time.sleep(0.35)  # more than two TTLs; renewal must retain the first slot
        assert not acquired.is_set()
        telemetry = first.telemetry()["granicus.com"]
        assert telemetry["lease_renewals"] >= 2

    thread.join(timeout=1)
    assert acquired.is_set()


def test_expired_slot_is_reclaimed_and_counted():
    store = MemCAS()
    pool = _pool(store)
    key = "test-provider-leases/granicus.com/slot-0.json"
    store.seed(
        key,
        json.dumps(
            {
                "owner": "dead-run:123",
                "domain": "granicus.com",
                "expires_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
            }
        ).encode(),
    )

    with pool.slots([URL]):
        # The expired slot was reclaimed (still exactly one slot object, now ours).
        assert store.keys("test-provider-leases/granicus.com/") == [key]

    assert pool.telemetry()["granicus.com"]["stale_leases_reaped"] == 1
    assert _held_count(store) == 0  # released (tombstoned)


def test_release_does_not_delete_a_slot_another_worker_reclaimed():
    # If this holder over-ran its TTL *without* observing a CAS conflict (a renewal network error or
    # a stalled renew thread) and another worker reclaimed the slot, release must NOT delete the new
    # holder's live lease — that would silently break the distributed cap.
    store = MemCAS()
    pool = _pool(store)
    slot0 = "test-provider-leases/granicus.com/slot-0.json"
    lease = pool._acquire("granicus.com")  # holds slot-0 at our ETag
    assert lease.key == slot0

    # Simulate another worker reclaiming the slot: a new payload under a different ETag.
    store.seed(slot0, b'{"owner": "other", "domain": "granicus.com", "state": "acquired"}')
    reclaimer_etag = store.etags[slot0]
    assert reclaimer_etag != lease.etag

    lease.release()  # stops renewal, then ownership-checked release

    assert store.keys("test-provider-leases/") == [slot0]  # the reclaimer's lease survived
    assert store.etags[slot0] == reclaimer_etag  # untouched — we did not delete it


def test_stale_reap_log_includes_run_job_shard_and_owner(monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "999")
    store = MemCAS()
    messages: list[str] = []
    pool = _pool(store, log=lambda msg, **_kw: messages.append(msg), shard="2/4")
    key = "test-provider-leases/granicus.com/slot-0.json"
    store.seed(
        key,
        json.dumps(
            {
                "owner": "dead-run:123",
                "domain": "granicus.com",
                "state": "acquired",
                "github_run_id": "111",
                "github_job": "audio",
                "github_shard": "0/4",
                "expires_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
            }
        ).encode(),
    )

    with pool.slots([URL]):
        pass

    reaped = [m for m in messages if "stale reaped" in m]
    assert len(reaped) == 1
    assert "owner=dead-run:123" in reaped[0]
    assert "run_id=111" in reaped[0]
    assert "job=audio" in reaped[0]
    assert "shard=0/4" in reaped[0]
    assert "state=acquired" in reaped[0]


def test_stale_reap_log_falls_back_concisely_for_unreadable_payload():
    store = MemCAS()
    messages: list[str] = []
    pool = _pool(store, log=lambda msg, **_kw: messages.append(msg))
    # An unreadable payload (not valid JSON) is treated as expired so a corrupt slot can't wedge the
    # domain — but there is no metadata to enrich the log line.
    store.seed("test-provider-leases/granicus.com/slot-0.json", b"not json")

    with pool.slots([URL]):
        pass

    reaped = [m for m in messages if "stale reaped" in m]
    assert len(reaped) == 1
    assert reaped[0] == "[enrich] provider lease stale reaped domain=granicus.com"


def test_acquire_uncontended_costs_one_read_and_one_write():
    store = MemCAS()
    pool = _pool(store, ttl_seconds=3600)  # 60s renew cadence: no renewal during this brief hold
    cm = pool.slots([URL])
    cm.__enter__()
    # read-before-claim (Class-B) + a single claim write (Class-A); no listing.
    assert (store.class_a, store.class_b) == (1, 1)
    cm.__exit__(None, None, None)
    assert store.class_a == 2  # release delete


def test_get_bytes_error_propagates_and_writes_nothing():
    class FailingReadStore(MemCAS):
        def get_bytes(self, key):
            raise RuntimeError("read failed")

    store = FailingReadStore()
    pool = _pool(store)
    with pytest.raises(RuntimeError, match="read failed"):
        with pool.slots([URL]):
            pass
    assert store.keys("test-provider-leases/") == []


def test_stop_firing_raises_instead_of_blocking_out_the_lease_wait():
    """A waiter polling for a held slot yields ``StopRequested`` once the run's wall-clock budget
    expires, instead of blocking until the holder releases. The stop must fire *after* contention is
    established (one full sweep finds the slot held), not on the pre-poll check — otherwise the test
    would never exercise the blocked path."""
    store = MemCAS()
    first = _pool(store)
    second = _pool(store)
    holder_acquired = threading.Event()
    holder_released = threading.Event()

    def hold():
        with first.slots([URL]):
            holder_acquired.set()
            holder_released.wait(timeout=2.0)

    t = threading.Thread(target=hold)
    t.start()
    try:
        assert holder_acquired.wait(timeout=1)  # the slot is genuinely held before we contend
        calls = {"n": 0}

        def stop() -> bool:
            calls["n"] += 1
            return calls["n"] > 1  # let one sweep observe the held slot, then fire

        with pytest.raises(StopRequested):
            with second.slots([URL], stop=stop):
                pass
        assert calls["n"] >= 2  # proves we polled the held slot before yielding

    finally:
        holder_released.set()
        t.join()

    assert _held_count(store) == 0  # holder released; waiter wrote nothing


def test_stop_never_firing_still_acquires_lease():
    store = MemCAS()
    pool = _pool(store)
    with pool.slots([URL], stop=lambda: False):
        pass  # acquired without raising


def test_current_waiting_counts_empty_when_idle():
    store = MemCAS()
    pool = _pool(store)
    assert pool.current_waiting_counts() == {}


def test_current_waiting_counts_reflects_live_queue_depth():
    store = MemCAS()
    first = _pool(store)
    second = _pool(store)
    waiting_seen = threading.Event()

    def _waiter():
        with second.slots([URL]):
            pass

    with first.slots([URL]):
        t = threading.Thread(target=_waiter)
        t.start()
        for _ in range(200):
            if second.current_waiting_counts().get("granicus.com", 0) == 1:
                waiting_seen.set()
                break
            time.sleep(0.01)
        assert waiting_seen.is_set()

    t.join(timeout=2)
    assert second.current_waiting_counts() == {}
