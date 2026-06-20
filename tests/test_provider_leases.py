"""Tests for cross-shard provider leases."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from citypods.http import StopRequested
from citypods.provider_leases import (
    DistributedProviderLeasePool,
    ProviderLeaseRule,
)
from citypods.storage.local import LocalStorage


def _pool(
    store: LocalStorage,
    *,
    ttl_seconds: float = 60,
    log=None,
) -> DistributedProviderLeasePool:
    pool = DistributedProviderLeasePool(prefix="test-provider-leases")
    pool.configure(
        store,
        {
            "granicus.com": {
                "slots": 1,
                "ttl_seconds": ttl_seconds,
                "poll_seconds": 0.01,
                "settle_seconds": 0.01,
            }
        },
        log=log,
    )
    return pool


def test_distributed_provider_lease_blocks_second_pool_until_release(tmp_path):
    store = LocalStorage(root=tmp_path / "bucket", url_prefix="https://cdn")
    first = _pool(store)
    second = _pool(store)
    acquired: list[str] = []

    def _waiter():
        with second.slots(["https://archive-video.granicus.com/x.mp4"]):
            acquired.append("second")

    with first.slots(["https://archive-video.granicus.com/x.mp4"]):
        t = threading.Thread(target=_waiter)
        t.start()
        time.sleep(0.05)
        assert acquired == []

    t.join(timeout=1)
    assert acquired == ["second"]


def test_distributed_provider_lease_ignores_unconfigured_domains(tmp_path):
    store = LocalStorage(root=tmp_path / "bucket", url_prefix="https://cdn")
    pool = _pool(store)

    with pool.slots(["https://example.com/x.mp4"]):
        pass

    assert list(store.list_objects("test-provider-leases/")) == []


def test_distributed_provider_lease_uses_basic_object_storage_api(tmp_path):
    """B2 does not implement conditional PutObject headers; leases use upload/list/delete only."""

    class BasicStore:
        name = "basic"

        def __init__(self, inner: LocalStorage) -> None:
            self.inner = inner

        def put_file(self, key, local_path, content_type):
            return self.inner.put_file(key, local_path, content_type)

        def list_objects(self, prefix=""):
            return self.inner.list_objects(prefix)

        def delete(self, key):
            self.inner.delete(key)

    store = BasicStore(LocalStorage(root=tmp_path / "bucket", url_prefix="https://cdn"))
    pool = DistributedProviderLeasePool(prefix="test-provider-leases")
    pool.configure(
        store,
        {
            "granicus.com": {
                "slots": 1,
                "ttl_seconds": 60,
                "poll_seconds": 0.01,
                "settle_seconds": 0,
            }
        },
    )

    with pool.slots(["https://archive-video.granicus.com/x.mp4"]):
        keys = [key for key, _ in store.list_objects("test-provider-leases/")]
        assert len(keys) == 1

    assert list(store.list_objects("test-provider-leases/")) == []


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


def test_active_lease_renews_past_ttl_and_blocks_waiter(tmp_path):
    store = LocalStorage(root=tmp_path / "bucket", url_prefix="https://cdn")
    first = _pool(store, ttl_seconds=0.15)
    second = _pool(store, ttl_seconds=0.15)
    acquired = threading.Event()

    def _waiter():
        with second.slots(["https://archive-video.granicus.com/x.mp4"]):
            acquired.set()

    with first.slots(["https://archive-video.granicus.com/x.mp4"]):
        thread = threading.Thread(target=_waiter)
        thread.start()
        time.sleep(0.35)  # more than two TTLs; renewal must retain the first slot
        assert not acquired.is_set()
        telemetry = first.telemetry()["granicus.com"]
        assert telemetry["lease_renewals"] >= 2

    thread.join(timeout=1)
    assert acquired.is_set()


def test_payload_expiry_reaps_stale_lease_even_when_object_mtime_is_fresh(tmp_path):
    store = LocalStorage(root=tmp_path / "bucket", url_prefix="https://cdn")
    pool = _pool(store)
    key = "test-provider-leases/granicus.com/0000000000000000000-dead.json"
    payload = tmp_path / "lease.json"
    payload.write_text(
        json.dumps(
            {
                "owner": "dead-run:123",
                "domain": "granicus.com",
                "expires_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
            }
        )
    )
    store.put_file(key, payload, "application/json")

    with pool.slots(["https://archive-video.granicus.com/x.mp4"]):
        keys = [candidate for candidate, _ in store.list_objects("test-provider-leases/")]
        assert key not in keys

    assert pool.telemetry()["granicus.com"]["stale_leases_reaped"] == 1


def test_stale_reap_log_includes_run_job_shard_and_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "999")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("GITHUB_JOB", "audio")
    store = LocalStorage(root=tmp_path / "bucket", url_prefix="https://cdn")
    messages: list[str] = []
    pool = DistributedProviderLeasePool(prefix="test-provider-leases")
    pool.configure(
        store,
        {
            "granicus.com": {
                "slots": 1,
                "ttl_seconds": 60,
                "poll_seconds": 0.01,
                "settle_seconds": 0,
            }
        },
        log=lambda msg, **_kw: messages.append(msg),
        shard="2/4",
    )
    key = "test-provider-leases/granicus.com/0000000000000000000-dead.json"
    payload = tmp_path / "lease.json"
    payload.write_text(
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
        )
    )
    store.put_file(key, payload, "application/json")

    with pool.slots(["https://archive-video.granicus.com/x.mp4"]):
        pass

    reaped = [m for m in messages if "stale reaped" in m]
    assert len(reaped) == 1
    assert "owner=dead-run:123" in reaped[0]
    assert "run_id=111" in reaped[0]
    assert "job=audio" in reaped[0]
    assert "shard=0/4" in reaped[0]
    assert "state=acquired" in reaped[0]


def test_stale_reap_log_falls_back_concisely_for_legacy_payload(tmp_path):
    store = LocalStorage(root=tmp_path / "bucket", url_prefix="https://cdn")
    messages: list[str] = []
    pool = DistributedProviderLeasePool(prefix="test-provider-leases")
    pool.configure(
        store,
        {
            "granicus.com": {
                "slots": 1,
                "ttl_seconds": 60,
                "poll_seconds": 0.01,
                "settle_seconds": 0,
            }
        },
        log=lambda msg, **_kw: messages.append(msg),
    )
    # Simulate an unreadable/legacy payload (body is not valid JSON) with a stale modification
    # time, so only the mtime+TTL fallback applies (no metadata available).
    key = "test-provider-leases/granicus.com/0000000000000000000-old.json"
    object_path = tmp_path / "bucket" / key
    object_path.parent.mkdir(parents=True, exist_ok=True)
    object_path.write_text("not json", encoding="utf-8")
    stale_modified = datetime.now(UTC) - timedelta(hours=1)
    os.utime(object_path, (stale_modified.timestamp(), stale_modified.timestamp()))

    with pool.slots(["https://archive-video.granicus.com/x.mp4"]):
        pass

    reaped = [m for m in messages if "stale reaped" in m]
    assert len(reaped) == 1
    assert reaped[0] == "[enrich] provider lease stale reaped domain=granicus.com"


def test_acquire_failure_removes_waiting_candidate(tmp_path):
    class FailingListStore:
        name = "failing-list"

        def __init__(self, inner: LocalStorage) -> None:
            self.inner = inner

        def put_file(self, key: str, local_path: Path, content_type: str):
            return self.inner.put_file(key, local_path, content_type)

        def list_objects(self, prefix=""):
            raise RuntimeError("list failed")

        def delete(self, key: str):
            self.inner.delete(key)

    inner = LocalStorage(root=tmp_path / "bucket", url_prefix="https://cdn")
    pool = DistributedProviderLeasePool(prefix="test-provider-leases")
    pool.configure(
        FailingListStore(inner),
        {
            "granicus.com": {
                "slots": 1,
                "ttl_seconds": 60,
                "poll_seconds": 0.01,
                "settle_seconds": 0,
            }
        },
    )

    with pytest.raises(RuntimeError, match="list failed"):
        with pool.slots(["https://archive-video.granicus.com/x.mp4"]):
            pass

    assert list(inner.list_objects("test-provider-leases/")) == []


def test_stop_firing_raises_instead_of_blocking_out_the_lease_wait(tmp_path):
    """A waiter polling for a held lease yields ``StopRequested`` once the run's wall-clock budget
    expires, instead of blocking until the holder releases (which TTL alone could make minutes)."""
    store = LocalStorage(root=tmp_path / "bucket", url_prefix="https://cdn")
    first = _pool(store)
    second = _pool(store)
    holder_released = threading.Event()

    def hold():
        with first.slots(["https://archive-video.granicus.com/x.mp4"]):
            holder_released.wait(timeout=2.0)

    t = threading.Thread(target=hold)
    t.start()
    try:
        with pytest.raises(StopRequested):
            with second.slots(["https://archive-video.granicus.com/x.mp4"], stop=lambda: True):
                pass
    finally:
        holder_released.set()
        t.join()

    # The aborted waiter's own candidate key must be cleaned up — only the holder's lease key
    # (now released too) may remain.
    assert list(store.list_objects("test-provider-leases/")) == []


def test_stop_never_firing_still_acquires_lease(tmp_path):
    store = LocalStorage(root=tmp_path / "bucket", url_prefix="https://cdn")
    pool = _pool(store)
    with pool.slots(["https://archive-video.granicus.com/x.mp4"], stop=lambda: False):
        pass  # acquired without raising


def test_current_waiting_counts_empty_when_idle(tmp_path):
    store = LocalStorage(root=tmp_path / "bucket", url_prefix="https://cdn")
    pool = _pool(store)
    assert pool.current_waiting_counts() == {}


def test_current_waiting_counts_reflects_live_queue_depth(tmp_path):
    store = LocalStorage(root=tmp_path / "bucket", url_prefix="https://cdn")
    first = _pool(store)
    second = _pool(store)
    waiting_seen = threading.Event()

    def _waiter():
        with second.slots(["https://archive-video.granicus.com/x.mp4"]):
            pass

    with first.slots(["https://archive-video.granicus.com/x.mp4"]):
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
