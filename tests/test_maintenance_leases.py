"""Tests for the CAS-backed workflow maintenance mutex."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import citypods.ops.maintenance_leases as maintenance_leases
from citypods.ops.maintenance_leases import (
    AGENDA_CHAPTER_RESET_MAINTENANCE_LEASE_KEYS,
    CHAPTER_AGENDA_MAINTENANCE_LEASE_KEY,
    CHAPTER_LOCATOR_MAINTENANCE_LEASE_KEY,
    CompositeMaintenanceLease,
    MaintenanceLeaseBusy,
    acquire,
    acquire_all,
)
from tests._cas_fake import MemCAS

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


def test_acquire_blocks_a_second_owner_until_release(monkeypatch):
    # release() (via MaintenanceLease._read_owned()) checks liveness against the real wall clock,
    # not the `now=` passed to acquire() -- unlike acquire() itself, it has no injectable clock.
    # Freeze it to NOW so the lease's own TTL window (NOW .. NOW+6h here) can't drift out from under
    # a real-time run long after this file was written, which would otherwise make release() treat
    # its own lease as already expired and silently no-op (the same guard that protects a newer
    # owner's lease from being clobbered during cleanup -- see the CASConflict handling below it).
    monkeypatch.setattr(maintenance_leases, "_now", lambda: NOW)

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


def test_composite_lease_claims_and_releases_all_keys(monkeypatch):
    monkeypatch.setattr(maintenance_leases, "_now", lambda: NOW)
    store = MemCAS()

    composite = acquire_all(
        store,
        owner="reset-owner",
        keys=AGENDA_CHAPTER_RESET_MAINTENANCE_LEASE_KEYS,
        now=NOW,
    )
    assert isinstance(composite, CompositeMaintenanceLease)
    assert composite.owner == "reset-owner"
    assert composite.keys == AGENDA_CHAPTER_RESET_MAINTENANCE_LEASE_KEYS

    composite.assert_held()
    composite.renew()

    # Individual keys cannot be claimed by another owner while composite lease is held
    for key in AGENDA_CHAPTER_RESET_MAINTENANCE_LEASE_KEYS:
        with pytest.raises(MaintenanceLeaseBusy, match="held by reset-owner"):
            acquire(store, owner="other", key=key, now=NOW)

    composite.release()

    # All keys are freed after composite release
    for key in AGENDA_CHAPTER_RESET_MAINTENANCE_LEASE_KEYS:
        lease = acquire(store, owner="other", key=key, now=NOW)
        assert lease.owner == "other"
        lease.release()


def test_composite_lease_rolls_back_already_claimed_keys_on_busy(monkeypatch):
    monkeypatch.setattr(maintenance_leases, "_now", lambda: NOW)
    store = MemCAS()

    # Pre-claim locator key so that the second key in composite claim fails
    locator_lease = acquire(
        store,
        owner="locator-job",
        key=CHAPTER_LOCATOR_MAINTENANCE_LEASE_KEY,
        now=NOW,
    )

    # Attempting to claim both keys fails on the second key
    with pytest.raises(MaintenanceLeaseBusy, match="held by locator-job"):
        acquire(
            store,
            owner="reset-job",
            key=AGENDA_CHAPTER_RESET_MAINTENANCE_LEASE_KEYS,
            now=NOW,
        )

    # The first key (chapter-agenda) should have been rolled back and released
    agenda_lease = acquire(
        store,
        owner="agenda-job",
        key=CHAPTER_AGENDA_MAINTENANCE_LEASE_KEY,
        now=NOW,
    )
    assert agenda_lease.owner == "agenda-job"

    agenda_lease.release()
    locator_lease.release()


def test_chapter_lanes_can_run_concurrently_with_separate_leases(monkeypatch):
    monkeypatch.setattr(maintenance_leases, "_now", lambda: NOW)
    store = MemCAS()

    agenda_lease = acquire(
        store,
        owner="chapter-agenda-job",
        key=CHAPTER_AGENDA_MAINTENANCE_LEASE_KEY,
        now=NOW,
    )
    locator_lease = acquire(
        store,
        owner="chapter-locator-job",
        key=CHAPTER_LOCATOR_MAINTENANCE_LEASE_KEY,
        now=NOW,
    )

    agenda_lease.assert_held()
    locator_lease.assert_held()

    agenda_lease.release()
    locator_lease.release()


def test_non_cas_storage_is_rejected():
    with pytest.raises(RuntimeError, match="CAS-capable"):
        acquire(object(), owner="reset", now=NOW)
