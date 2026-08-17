"""Tests for the CAS-backed workflow maintenance mutex."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import citypods.ops.maintenance_leases as maintenance_leases
from citypods.ops.maintenance_leases import (
    MaintenanceLeaseBusy,
    acquire,
)
from tests._cas_fake import MemCAS

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


def test_acquire_blocks_a_second_owner_until_release():
    store = MemCAS()
    first = acquire(store, owner="chapter-agenda", now=NOW)

    with pytest.raises(MaintenanceLeaseBusy, match="held by chapter-agenda"):
        acquire(store, owner="reset", now=NOW)

    first.release()
    second = acquire(store, owner="reset", now=NOW)
    assert second.owner == "reset"


def test_expired_lease_can_be_reclaimed(monkeypatch):
    store = MemCAS()
    first = acquire(store, owner="stale", now=NOW, ttl_seconds=60)
    replacement_now = NOW + timedelta(seconds=61)

    second = acquire(
        store,
        owner="reset",
        now=replacement_now,
        ttl_seconds=60,
    )

    assert second.owner == "reset"
    first.release()  # Must not overwrite the replacement owner.
    monkeypatch.setattr(maintenance_leases, "_now", lambda: replacement_now)
    second.assert_held()


def test_non_cas_storage_is_rejected():
    with pytest.raises(RuntimeError, match="CAS-capable"):
        acquire(object(), owner="reset", now=NOW)
