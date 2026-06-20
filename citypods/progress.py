"""Process-wide registry of long-running per-thread work, for stall diagnostics.

The H6b sharded audio/ASR workflows have intermittently shown a shard stuck for the whole run
with no further log output (#audio-workflow-review). The existing heartbeat prints CPU/memory
snapshots on an interval but says nothing about *what* is actually running, so a stuck shard is
indistinguishable from a slow-but-healthy one until the runner is killed.

Workers register what they're doing (source/uid/phase) before starting a potentially long
operation and clear it when done. ``citypods.run``'s heartbeat reads a snapshot each tick so a
stalled shard shows which episode/source/phase every thread has been stuck on, and for how long.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class ProgressEntry:
    source: str
    uid: str
    phase: str
    started: float  # time.monotonic() at entry

    def elapsed(self) -> float:
        return time.monotonic() - self.started


class ProgressRegistry:
    """Thread-safe map of thread ident -> the entry that thread is currently working on."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[int, ProgressEntry] = {}

    def start(self, *, source: str, uid: str, phase: str) -> ProgressEntry | None:
        """Mark the calling thread as working on ``(source, uid, phase)``.

        Returns the thread's *previous* entry (or ``None``), to pass to :meth:`finish` so nested
        calls restore the outer entry rather than clearing it.
        """
        entry = ProgressEntry(source=source, uid=uid, phase=phase, started=time.monotonic())
        ident = threading.get_ident()
        with self._lock:
            previous = self._entries.get(ident)
            self._entries[ident] = entry
        return previous

    def finish(self, previous: ProgressEntry | None) -> None:
        """Restore the calling thread's previous entry (or clear it if there was none)."""
        ident = threading.get_ident()
        with self._lock:
            if previous is None:
                self._entries.pop(ident, None)
            else:
                self._entries[ident] = previous

    @contextmanager
    def track(self, *, source: str, uid: str, phase: str) -> Iterator[None]:
        previous = self.start(source=source, uid=uid, phase=phase)
        try:
            yield
        finally:
            self.finish(previous)

    def snapshot(self) -> list[ProgressEntry]:
        """All active entries, longest-running first."""
        with self._lock:
            entries = list(self._entries.values())
        return sorted(entries, key=lambda e: e.started)

    def longest_elapsed(self) -> float:
        entries = self.snapshot()
        return max((e.elapsed() for e in entries), default=0.0)


def format_snapshot(entries: list[ProgressEntry], *, limit: int = 8) -> str:
    """Human-readable summary of the longest-running active entries, for log lines."""
    if not entries:
        return "no tracked work active"
    longest_first = sorted(entries, key=lambda e: e.elapsed(), reverse=True)
    shown = ", ".join(
        f"{e.phase} source={e.source} uid={e.uid} elapsed={e.elapsed():.0f}s"
        for e in longest_first[:limit]
    )
    extra = len(longest_first) - limit
    if extra > 0:
        shown += f" (+{extra} more)"
    return shown


# One registry for the whole process; workers in citypods/media.py and citypods/stages.py
# register their current phase, and citypods.run's heartbeat reads it each tick.
PROGRESS = ProgressRegistry()
