"""Run-local transcript spill must retain exact snapshots and replacement semantics."""

import os
import sqlite3

import pytest

from citypods.judging.tasks import EpisodeTexts
from citypods.judging.text_cache import TextCache


def test_eviction_restores_snapshot_and_origin_replacement():
    cache = TextCache(memory_entries=1)
    directory = cache._directory.name
    one = EpisodeTexts(
        segments=[{"start": 1.0, "end": 2.0, "text": "first"}],
        chapters=[("chapter", "title", 0, None)],
        definitions={"tag": "definition"},
        identity="v1",
    )
    two = EpisodeTexts(segments=[{"start": 3, "end": 4, "text": "second"}], identity="v2")
    newer = EpisodeTexts(segments=[{"start": 1, "end": 2, "text": "origin"}], identity="v3")
    try:
        cache.put(1, one)
        cache.put(2, two)
        assert len(cache.memory) == 1
        assert cache.get(1) == one
        assert cache.get(2) == two  # Key 1 is back on disk when the origin replaces it.
        cache.put(1, newer)
        assert cache.get(2) == two
        assert cache.get(1) == newer
        assert cache.get(3) is None
        assert cache.spills > 0 and cache.rehydrates > 0 and cache.spill_bytes > 0
    finally:
        cache.close()
    assert not os.path.exists(directory)


def test_constructor_cleans_up_after_database_error(monkeypatch, tmp_path):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))

    def fail(_path):
        raise sqlite3.OperationalError("disk unavailable")

    monkeypatch.setattr("sqlite3.connect", fail)
    with pytest.raises(sqlite3.OperationalError, match="disk unavailable"):
        TextCache()
    assert list(tmp_path.iterdir()) == []
