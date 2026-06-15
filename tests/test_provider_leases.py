"""Tests for cross-shard provider leases."""

from __future__ import annotations

import threading
import time

from citypods.provider_leases import DistributedProviderLeasePool
from citypods.storage.local import LocalStorage


def _pool(store: LocalStorage) -> DistributedProviderLeasePool:
    pool = DistributedProviderLeasePool(prefix="test-provider-leases")
    pool.configure(
        store,
        {
            "granicus.com": {
                "slots": 1,
                "ttl_seconds": 60,
                "poll_seconds": 0.01,
                "settle_seconds": 0.01,
            }
        },
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
