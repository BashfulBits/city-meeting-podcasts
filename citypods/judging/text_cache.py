"""Bounded run-local transcript cache with a private, temporary spill file."""

from __future__ import annotations

import pickle
import sqlite3
import tempfile
from collections import OrderedDict

from citypods.judging.tasks import EpisodeTexts


class TextCache:
    """Keep a few parsed episodes in memory; preserve evicted snapshots on local disk."""

    def __init__(self, memory_entries: int = 8):
        if memory_entries < 1:
            raise ValueError("memory_entries must be positive")
        self.limit = memory_entries
        self.memory: OrderedDict[int, EpisodeTexts] = OrderedDict()
        self._directory = tempfile.TemporaryDirectory(prefix="citypods-judge-")
        try:
            self._db = sqlite3.connect(f"{self._directory.name}/texts.sqlite")
            try:
                self._db.execute(
                    "CREATE TABLE texts (key INTEGER PRIMARY KEY, value BLOB NOT NULL)"
                )
            except BaseException:
                self._db.close()
                raise
        except BaseException:
            self._directory.cleanup()
            raise
        self.spills = 0
        self.rehydrates = 0
        self.spill_bytes = 0

    def close(self) -> None:
        try:
            self._db.close()
        finally:
            self._directory.cleanup()

    def get(self, key: int) -> EpisodeTexts | None:
        if key in self.memory:
            self.memory.move_to_end(key)
            return self.memory[key]
        row = self._db.execute("SELECT value FROM texts WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        # The database is private, created in this process and never populated from a remote blob.
        value = pickle.loads(row[0])  # noqa: S301
        self.rehydrates += 1
        self._put_memory(key, value)
        return value

    def put(self, key: int, value: EpisodeTexts) -> None:
        # Remove any previous snapshot before publishing its replacement in memory.
        self._db.execute("DELETE FROM texts WHERE key = ?", (key,))
        self._put_memory(key, value)

    def _put_memory(self, key: int, value: EpisodeTexts) -> None:
        self.memory[key] = value
        self.memory.move_to_end(key)
        while len(self.memory) > self.limit:
            old_key, old_value = self.memory.popitem(last=False)
            blob = pickle.dumps(old_value, protocol=pickle.HIGHEST_PROTOCOL)
            self._db.execute("INSERT OR REPLACE INTO texts VALUES (?, ?)", (old_key, blob))
            self.spills += 1
            self.spill_bytes += len(blob)
