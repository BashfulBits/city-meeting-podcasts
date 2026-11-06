"""Tests for scripts/clear_terminal_failures.py."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import clear_terminal_failures as ctf  # noqa: E402

from citypods.compute.llm_deferred import _write_json  # noqa: E402
from citypods.storage.base import StorageReadUnavailable  # noqa: E402
from tests._cas_fake import MemStorage  # noqa: E402


def test_clear_terminal_failures_dry_run_and_apply(monkeypatch):
    storage = MemStorage()
    marker_tag = {
        "recipe_hash": "r-tag",
        "task": "tag",
        "status": "exhausted",
        "last_error": "validation failure",
    }
    marker_agenda = {
        "recipe_hash": "r-agenda",
        "task": "agenda-item-extract",
        "status": "exhausted",
        "last_error": "context too large",
    }
    _write_json(storage, ctf.deferred_failure_key("r-tag"), json.dumps(marker_tag).encode())
    _write_json(storage, ctf.deferred_failure_key("r-agenda"), json.dumps(marker_agenda).encode())

    monkeypatch.setattr(ctf, "load_site_config", lambda _path: {})
    monkeypatch.setattr(ctf, "make_storage", lambda _cfg, _base, _out: storage)

    # Dry-run: nothing deleted
    ret = ctf.main(["--task", "tag"])
    assert ret == 0
    assert storage.exists(ctf.deferred_failure_key("r-tag"))
    assert storage.exists(ctf.deferred_failure_key("r-agenda"))

    # Apply tag: deletes r-tag only
    ret = ctf.main(["--task", "tag", "--apply"])
    assert ret == 0
    assert not storage.exists(ctf.deferred_failure_key("r-tag"))
    assert storage.exists(ctf.deferred_failure_key("r-agenda"))

    # Specific recipe hash apply
    ret = ctf.main(["--recipe-hash", "r-agenda", "--apply"])
    assert ret == 0
    assert not storage.exists(ctf.deferred_failure_key("r-agenda"))


def test_clear_terminal_failures_days_filtering(monkeypatch):
    storage = MemStorage()
    now = datetime.now(UTC)
    old_date = now - timedelta(days=20)
    recent_date = now - timedelta(days=2)

    marker_old = {
        "recipe_hash": "r-old",
        "task": "tag",
        "status": "exhausted",
        "last_error": "old failure",
    }
    marker_new = {
        "recipe_hash": "r-new",
        "task": "tag",
        "status": "exhausted",
        "last_error": "recent failure",
    }
    _write_json(storage, ctf.deferred_failure_key("r-old"), json.dumps(marker_old).encode())
    _write_json(storage, ctf.deferred_failure_key("r-new"), json.dumps(marker_new).encode())

    def mock_list_objects(_prefix=""):
        yield ctf.deferred_failure_key("r-old"), old_date
        yield ctf.deferred_failure_key("r-new"), recent_date

    storage.list_objects = mock_list_objects

    monkeypatch.setattr(ctf, "load_site_config", lambda _path: {})
    monkeypatch.setattr(ctf, "make_storage", lambda _cfg, _base, _out: storage)

    # With --days 7 --apply: only r-new should be deleted
    ret = ctf.main(["--task", "tag", "--days", "7", "--apply"])
    assert ret == 0
    assert storage.exists(ctf.deferred_failure_key("r-old"))
    assert not storage.exists(ctf.deferred_failure_key("r-new"))


def test_clear_terminal_failures_resilient_to_storage_read_error(monkeypatch):
    storage = MemStorage()
    marker_good = {
        "recipe_hash": "r-good",
        "task": "tag",
        "status": "exhausted",
        "last_error": "validation failure",
    }
    _write_json(storage, ctf.deferred_failure_key("r-bad"), b"not valid json")
    _write_json(storage, ctf.deferred_failure_key("r-good"), json.dumps(marker_good).encode())

    real_read = ctf._read_json

    def flaky_read(store, key):
        if "r-bad" in key:
            raise StorageReadUnavailable(key, RuntimeError("simulated network failure"))
        return real_read(store, key)

    monkeypatch.setattr(ctf, "_read_json", flaky_read)
    monkeypatch.setattr(ctf, "load_site_config", lambda _path: {})
    monkeypatch.setattr(ctf, "make_storage", lambda _cfg, _base, _out: storage)

    # Dry-run and apply shouldn't crash on r-bad
    ret = ctf.main(["--task", "tag", "--apply"])
    assert ret == 0
    assert not storage.exists(ctf.deferred_failure_key("r-good"))
