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


def test_lease_held_by_a_finished_actions_run_is_taken_over(monkeypatch):
    monkeypatch.setattr(maintenance_leases, "_now", lambda: NOW)
    store = MemCAS()
    acquire(store, owner="github-actions:Chapter boundary locator:111", now=NOW)
    monkeypatch.setattr(maintenance_leases, "_owner_run_finished", lambda owner: True)
    taken = acquire(store, owner="github-actions:Chapter boundary locator:222", now=NOW)
    assert taken.owner.endswith(":222")


def test_lease_held_by_a_still_running_run_stays_busy(monkeypatch):
    monkeypatch.setattr(maintenance_leases, "_now", lambda: NOW)
    store = MemCAS()
    acquire(store, owner="github-actions:Chapter boundary locator:111", now=NOW)
    monkeypatch.setattr(maintenance_leases, "_owner_run_finished", lambda owner: False)
    with pytest.raises(MaintenanceLeaseBusy):
        acquire(store, owner="github-actions:Chapter boundary locator:222", now=NOW)


@pytest.mark.parametrize(
    "owner,env,expected",
    [
        ("chapter-agenda", {"GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": "o/r"}, False),
        ("github-actions:wf:abc", {"GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": "o/r"}, False),
        ("github-actions:wf:12", {}, False),
        (
            "github-actions:wf:12",
            {"GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "12"},
            False,
        ),
    ],
)
def test_owner_run_finished_fails_closed_without_certainty(monkeypatch, owner, env, expected):
    for name in ("GITHUB_TOKEN", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        maintenance_leases.urllib.request, "urlopen", lambda *a, **k: pytest.fail("no request")
    )
    assert maintenance_leases._owner_run_finished(owner) is expected


@pytest.mark.parametrize(
    "status,expected", [("completed", True), ("in_progress", False), ("queued", False)]
)
def test_owner_run_finished_reads_the_run_status(monkeypatch, status, expected):
    import io
    import json as _json

    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_RUN_ID", "999")
    seen = {}

    def fake(request, timeout):
        seen["url"] = request.full_url
        return io.StringIO(_json.dumps({"status": status}))

    monkeypatch.setattr(maintenance_leases.urllib.request, "urlopen", fake)
    assert (
        maintenance_leases._owner_run_finished("github-actions:Chapter boundary locator:111")
        is expected
    )
    assert seen["url"] == "https://api.github.com/repos/o/r/actions/runs/111"


def test_owner_run_finished_treats_an_api_error_as_still_held(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")

    def boom(request, timeout):
        raise OSError("network down")

    monkeypatch.setattr(maintenance_leases.urllib.request, "urlopen", boom)
    assert maintenance_leases._owner_run_finished("github-actions:wf:111") is False
