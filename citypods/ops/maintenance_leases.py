"""CAS-backed maintenance mutexes for workflows that repair shared durable state.

The lease is deliberately a single coordination object, not a per-record lock. A short-lived
manual repair must exclude the chapter lanes for the whole read/modify/push window; a point-in-time
Actions status check cannot provide that guarantee. Expiry is the crash-recovery path, and the
lease is routed to the R2 CAS control plane rather than the B2 record store.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from citypods.storage import CASConflict

MAINTENANCE_LEASE_PREFIX = "maintenance-leases/"
CHAPTER_AGENDA_MAINTENANCE_LEASE_KEY = f"{MAINTENANCE_LEASE_PREFIX}chapter-agenda.json"
CHAPTER_LOCATOR_MAINTENANCE_LEASE_KEY = f"{MAINTENANCE_LEASE_PREFIX}chapter-locator.json"
AGENDA_CHAPTER_RESET_MAINTENANCE_LEASE_KEYS = (
    CHAPTER_AGENDA_MAINTENANCE_LEASE_KEY,
    CHAPTER_LOCATOR_MAINTENANCE_LEASE_KEY,
)
AGENDA_CHAPTER_MAINTENANCE_LEASE_KEY = f"{MAINTENANCE_LEASE_PREFIX}agenda-chapter-reset.json"
MAINTENANCE_LEASE_SCHEMA_VERSION = 1
MAINTENANCE_LEASE_TTL_SECONDS = 6 * 60 * 60


class MaintenanceLeaseBusy(RuntimeError):
    """Raised when another live workflow owns the requested maintenance lease."""


class MaintenanceLeaseUnavailable(RuntimeError):
    """Raised when the storage backend cannot provide real compare-and-swap."""


def _now() -> datetime:
    return datetime.now(UTC)


def _encode(*, owner: str, token: str, expires_at: datetime, state: str = "held") -> bytes:
    return (
        json.dumps(
            {
                "schema_version": MAINTENANCE_LEASE_SCHEMA_VERSION,
                "state": state,
                "owner": owner,
                "token": token,
                "expires_at": expires_at.isoformat(),
            },
            sort_keys=True,
        )
        + "\n"
    ).encode()


def _active_owner(data: bytes, now: datetime) -> str | None:
    try:
        payload = json.loads(data)
        if payload.get("state") != "held":
            return None
        expires_at = datetime.fromisoformat(str(payload["expires_at"]))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    return str(payload.get("owner") or "unknown") if expires_at > now else None


def _assert_cas(storage) -> None:
    if not getattr(storage, "cas_capable", False):
        raise MaintenanceLeaseUnavailable(
            "agenda/chapter maintenance requires a CAS-capable R2 coordination backend"
        )


@dataclass
class MaintenanceLease:
    """An owned maintenance lease that can be checked, renewed, and released safely."""

    storage: object
    key: str
    owner: str
    token: str
    etag: str
    ttl_seconds: float

    def _read_owned(self) -> tuple[dict, str]:
        got = self.storage.get_bytes(self.key)
        if got is None:
            raise MaintenanceLeaseUnavailable(f"maintenance lease disappeared: {self.key}")
        data, etag = got
        try:
            payload = json.loads(data)
            expires_at = datetime.fromisoformat(str(payload["expires_at"]))
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise MaintenanceLeaseUnavailable(f"maintenance lease is corrupt: {self.key}") from exc
        now = _now()
        if (
            payload.get("state") != "held"
            or payload.get("owner") != self.owner
            or payload.get("token") != self.token
            or expires_at <= now
        ):
            raise MaintenanceLeaseBusy(f"maintenance lease is no longer held by {self.owner}")
        return payload, etag

    def renew(self) -> None:
        """Extend the lease and fail closed if another owner replaced it."""
        _payload, etag = self._read_owned()
        expires_at = _now() + timedelta(seconds=self.ttl_seconds)
        _url, self.etag = self.storage.put_cas(
            self.key,
            _encode(owner=self.owner, token=self.token, expires_at=expires_at),
            "application/json",
            if_match=etag,
        )

    def assert_held(self) -> None:
        """Verify ownership immediately before a workflow writes shared durable state."""
        _payload, etag = self._read_owned()
        self.etag = etag

    def release(self) -> None:
        """Publish an expired tombstone without deleting a newer owner's lease."""
        try:
            _payload, etag = self._read_owned()
        except (MaintenanceLeaseBusy, MaintenanceLeaseUnavailable):
            return
        try:
            _url, self.etag = self.storage.put_cas(
                self.key,
                _encode(
                    owner=self.owner,
                    token=self.token,
                    expires_at=_now(),
                    state="released",
                ),
                "application/json",
                if_match=etag,
            )
        except CASConflict:
            # A newer owner won after our read; never overwrite its lease during cleanup.
            return

    def __enter__(self) -> MaintenanceLease:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


@dataclass
class CompositeMaintenanceLease:
    """A collection of owned maintenance leases coordinated as a single transaction."""

    leases: tuple[MaintenanceLease, ...]

    @property
    def owner(self) -> str:
        return self.leases[0].owner if self.leases else ""

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(lease.key for lease in self.leases)

    def renew(self) -> None:
        """Extend all leases and fail closed if any lease was replaced."""
        for lease in self.leases:
            lease.renew()

    def assert_held(self) -> None:
        """Verify ownership across all leases before durable writes."""
        for lease in self.leases:
            lease.assert_held()

    def release(self) -> None:
        """Release all held leases in reverse order."""
        for lease in reversed(self.leases):
            lease.release()

    def __enter__(self) -> CompositeMaintenanceLease:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


def _acquire_single(
    storage,
    *,
    owner: str,
    key: str,
    ttl_seconds: float = MAINTENANCE_LEASE_TTL_SECONDS,
    now: datetime | None = None,
    max_attempts: int = 8,
) -> MaintenanceLease:
    """Atomically claim a single maintenance lease or fail closed."""
    _assert_cas(storage)
    if not owner.strip():
        raise ValueError("maintenance lease owner must not be empty")
    if not key.strip():
        raise ValueError("maintenance lease key must not be empty")
    if ttl_seconds <= 0:
        raise ValueError("maintenance lease TTL must be positive")
    now = now or _now()
    token = uuid.uuid4().hex
    for _attempt in range(max_attempts):
        got = storage.get_bytes(key)
        if got is None:
            etag_arg = {"if_none_match": "*"}
        else:
            data, etag = got
            active = _active_owner(data, now)
            if active is not None:
                raise MaintenanceLeaseBusy(f"maintenance lease {key} is held by {active}")
            etag_arg = {"if_match": etag}
        expires_at = now + timedelta(seconds=ttl_seconds)
        try:
            _url, new_etag = storage.put_cas(
                key,
                _encode(owner=owner, token=token, expires_at=expires_at),
                "application/json",
                **etag_arg,
            )
            return MaintenanceLease(storage, key, owner, token, new_etag, ttl_seconds)
        except CASConflict:
            continue
    raise MaintenanceLeaseBusy(f"could not claim maintenance lease {key} after CAS retries")


def acquire(
    storage,
    *,
    owner: str,
    key: str | Sequence[str] = AGENDA_CHAPTER_MAINTENANCE_LEASE_KEY,
    ttl_seconds: float = MAINTENANCE_LEASE_TTL_SECONDS,
    now: datetime | None = None,
    max_attempts: int = 8,
) -> MaintenanceLease | CompositeMaintenanceLease:
    """Atomically claim one or more maintenance leases, or fail closed.

    When multiple keys are provided, already-acquired leases are automatically released
    if any subsequent key cannot be claimed, avoiding leaked partial locks.
    """
    if isinstance(key, (list, tuple, set, frozenset)):
        keys = tuple(key)
        if not keys:
            raise ValueError("maintenance lease keys must not be empty")
        acquired: list[MaintenanceLease] = []
        try:
            for k in keys:
                acquired.append(
                    _acquire_single(
                        storage,
                        owner=owner,
                        key=k,
                        ttl_seconds=ttl_seconds,
                        now=now,
                        max_attempts=max_attempts,
                    )
                )
            return CompositeMaintenanceLease(tuple(acquired))
        except Exception:
            for lease in reversed(acquired):
                lease.release()
            raise

    return _acquire_single(
        storage,
        owner=owner,
        key=key,
        ttl_seconds=ttl_seconds,
        now=now,
        max_attempts=max_attempts,
    )


def acquire_all(
    storage,
    *,
    owner: str,
    keys: Sequence[str] = AGENDA_CHAPTER_RESET_MAINTENANCE_LEASE_KEYS,
    ttl_seconds: float = MAINTENANCE_LEASE_TTL_SECONDS,
    now: datetime | None = None,
    max_attempts: int = 8,
) -> CompositeMaintenanceLease:
    """Atomically claim a sequence of maintenance leases or fail closed."""
    lease = acquire(
        storage,
        owner=owner,
        key=keys,
        ttl_seconds=ttl_seconds,
        now=now,
        max_attempts=max_attempts,
    )
    assert isinstance(lease, CompositeMaintenanceLease)
    return lease
