"""Append-only log of objects reclaimed by the storage-reclaim policy (GH#496).

Every delete the reclaim tooling performs is recorded here so the resurrection watchdog
(``scripts/check_reclaim_resurrection.py``) can answer one question each run: *did a live record
come to reference a key we deleted, while it is still restorable from the B2 version window?* If so,
it escalates a HIGH-priority issue in time to restore the prior version before B2 purges it.

Modeled on ``run_history.jsonl`` (``citypods/run.py``): one JSON object per line, in the durable
state dir (``state/reclaim-log.jsonl``) so it syncs to the bucket via ``statesync`` (``.jsonl``
``state/`` is whitelisted). Unlike run_history's fixed line cap, this log is **pruned by age**: the
only entries with live safety value are those whose ``recover_by`` is still ahead, so we retain
those plus a margin and drop the rest. A fixed count cap could wrongly truncate a still-recoverable
entry if a single run deletes thousands of objects; age pruning cannot.

Entry fields:
  * ``key`` — the storage key that was deleted.
  * ``backend`` — ``"b2"`` (recoverable within the version window) or ``"r2"`` (permanent; R2 has no
    version history — logged for audit only, never recoverable).
  * ``deleted_at`` — ISO-8601 UTC timestamp of the delete.
  * ``reason`` — short tag, e.g. ``"orphan-auto"``, ``"orphan-manual"``, ``"scratch"``.
  * ``run_url`` — the Actions run that deleted it (for the recovery issue), or ``None``.
  * ``recover_by`` — ISO-8601 deadline (``deleted_at`` + B2 retention window) after which a deleted
    version is purged and unrecoverable; ``None`` for R2 (already unrecoverable).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from citypods.ops.reclaim import B2_RETENTION_DAYS_DEFAULT

RECLAIM_LOG_NAME = "reclaim-log.jsonl"


@dataclass(frozen=True)
class DeletionEntry:
    key: str
    backend: str
    deleted_at: str
    reason: str
    run_url: str | None = None
    recover_by: str | None = None

    def recover_by_dt(self) -> datetime | None:
        return _parse_dt(self.recover_by)

    def deleted_at_dt(self) -> datetime | None:
        return _parse_dt(self.deleted_at)


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def make_entry(
    key: str,
    *,
    backend: str,
    reason: str,
    run_url: str | None = None,
    retention_days: int = B2_RETENTION_DAYS_DEFAULT,
    now: datetime | None = None,
) -> DeletionEntry:
    """Build a :class:`DeletionEntry`, computing ``recover_by`` from the B2 retention window. R2
    deletes are permanent (no version history), so their ``recover_by`` is ``None``."""
    now = now or datetime.now(UTC)
    recover_by = None
    if backend == "b2":
        recover_by = (now + timedelta(days=retention_days)).isoformat()
    return DeletionEntry(
        key=key,
        backend=backend,
        deleted_at=now.isoformat(),
        reason=reason,
        run_url=run_url,
        recover_by=recover_by,
    )


def _log_path(state_dir: Path) -> Path:
    return Path(state_dir) / RECLAIM_LOG_NAME


def load(state_dir: Path) -> list[DeletionEntry]:
    """Every entry in the log, oldest-first. Malformed lines are skipped, not fatal."""
    path = _log_path(state_dir)
    if not path.exists():
        return []
    entries: list[DeletionEntry] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and "key" in data:
            entries.append(
                DeletionEntry(
                    key=data["key"],
                    backend=data.get("backend", ""),
                    deleted_at=data.get("deleted_at", ""),
                    reason=data.get("reason", ""),
                    run_url=data.get("run_url"),
                    recover_by=data.get("recover_by"),
                )
            )
    return entries


def load_recoverable(state_dir: Path, *, now: datetime | None = None) -> list[DeletionEntry]:
    """Entries whose ``recover_by`` is still in the future — i.e. the deleted version can still be
    restored from B2. These are the only entries the watchdog acts on (R2 entries, with no
    ``recover_by``, are never recoverable and are excluded)."""
    now = now or datetime.now(UTC)
    out = []
    for e in load(state_dir):
        rb = e.recover_by_dt()
        if rb is not None and rb > now:
            out.append(e)
    return out


def _keep(entry: DeletionEntry, now: datetime, retention_days: int) -> bool:
    """Prune-by-age predicate. Keep an entry until well past any recovery value:
      * recoverable (B2) entries: keep until ``recover_by`` + one extra retention window (margin);
      * permanent (R2/no-recover_by) entries: keep for 2× the retention window from ``deleted_at``.
    Dropping an over-age entry loses only stale audit history, never live recovery safety."""
    margin = timedelta(days=retention_days)
    rb = entry.recover_by_dt()
    if rb is not None:
        return now <= rb + margin
    da = entry.deleted_at_dt()
    if da is None:
        return True  # can't age an unparseable timestamp — keep rather than silently drop
    return now <= da + 2 * margin


def append_deletions(
    state_dir: Path,
    entries: list[DeletionEntry],
    *,
    retention_days: int = B2_RETENTION_DAYS_DEFAULT,
    now: datetime | None = None,
) -> Path:
    """Append ``entries`` to the log, then prune stale entries by age. Returns the log path.

    Appending + pruning in one pass keeps the synced file bounded without a separate maintenance.
    A no-entry call still prunes, so the log self-trims on every reclaim run."""
    now = now or datetime.now(UTC)
    path = _log_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    kept = [e for e in load(state_dir) if _keep(e, now, retention_days)]
    kept.extend(entries)
    path.write_text(
        "\n".join(json.dumps(asdict(e)) for e in kept) + ("\n" if kept else ""),
        encoding="utf-8",
    )
    return path
