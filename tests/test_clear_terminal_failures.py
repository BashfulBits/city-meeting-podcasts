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


def _seed(storage, recipe, task, error, status="exhausted"):
    marker = {"recipe_hash": recipe, "task": task, "status": status, "last_error": error}
    _write_json(storage, ctf.deferred_failure_key(recipe), json.dumps(marker).encode())
    return marker


def test_clear_by_cause_clears_only_the_matching_marker_and_archives_it(monkeypatch, capsys):
    storage = MemStorage()
    keep = _seed(storage, "r-keep", "agenda-chapter-locate", "quote is absent from cited lines")
    gone = _seed(storage, "r-gone", "agenda-chapter-locate", "duplicate locator unit: u00001")
    monkeypatch.setattr(ctf, "load_site_config", lambda _path: {})
    monkeypatch.setattr(ctf, "make_storage", lambda _cfg, _base, _out: storage)

    ret = ctf.main(
        [
            "--task",
            "agenda-chapter-locate",
            "--last-error-contains",
            "duplicate locator unit",
            "--apply",
        ]
    )

    assert ret == 0
    assert storage.exists(ctf.deferred_failure_key("r-keep"))
    assert not storage.exists(ctf.deferred_failure_key("r-gone"))
    audit_key = f"{ctf.AUDIT_PREFIX}{datetime.now(UTC).date().isoformat()}/r-gone.json"
    audit = ctf._safe_read_json(storage, audit_key)
    assert audit == gone
    assert keep["recipe_hash"] == "r-keep"
    assert "Markers by cause" in capsys.readouterr().out


def test_dry_run_prints_a_cause_histogram_with_ids_collapsed(monkeypatch, capsys):
    storage = MemStorage()
    _seed(storage, "r1", "moment-extraction", "job 1234abcd5678 failed permanently")
    _seed(storage, "r2", "moment-extraction", "job 99ffee001122 failed permanently")
    monkeypatch.setattr(ctf, "load_site_config", lambda _path: {})
    monkeypatch.setattr(ctf, "make_storage", lambda _cfg, _base, _out: storage)

    assert ctf.main(["--task", "moment-extraction"]) == 0

    out = capsys.readouterr().out
    assert "2  moment-extraction | exhausted |  | job # failed permanently" in out
    assert storage.exists(ctf.deferred_failure_key("r1"))


def test_apply_refuses_more_markers_than_max_clear(monkeypatch):
    storage = MemStorage()
    for index in range(3):
        _seed(storage, f"r{index}", "tag", "boom")
    monkeypatch.setattr(ctf, "load_site_config", lambda _path: {})
    monkeypatch.setattr(ctf, "make_storage", lambda _cfg, _base, _out: storage)

    assert ctf.main(["--task", "tag", "--apply", "--max-clear", "2"]) == 1
    assert all(storage.exists(ctf.deferred_failure_key(f"r{index}")) for index in range(3))


def _args(monkeypatch, storage):
    monkeypatch.setattr(ctf, "load_site_config", lambda _path: {})
    monkeypatch.setattr(ctf, "make_storage", lambda _cfg, _base, _out: storage)


def test_single_hash_honours_the_cause_filter(monkeypatch):
    storage = MemStorage()
    _seed(storage, "r1", "agenda-chapter-locate", "quote absent")
    _args(monkeypatch, storage)

    ret = ctf.main(
        ["--recipe-hash", "r1", "--last-error-contains", "duplicate locator unit", "--apply"]
    )

    assert ret == 0
    assert storage.exists(ctf.deferred_failure_key("r1"))


def test_single_hash_fails_loudly_when_it_cannot_clear(monkeypatch):
    storage = MemStorage()
    _seed(storage, "r1", "tag", "boom")
    day = datetime.now(UTC).date().isoformat()
    # A different archive already exists for today: clearing must not overwrite it.
    _write_json(storage, f"{ctf.AUDIT_PREFIX}{day}/r1.json", json.dumps({"other": 1}).encode())
    _args(monkeypatch, storage)

    assert ctf.main(["--recipe-hash", "r1", "--apply"]) == 1
    assert storage.exists(ctf.deferred_failure_key("r1"))


def test_an_unreadable_archive_is_not_treated_as_absent(monkeypatch):
    storage = MemStorage()
    marker = _seed(storage, "r1", "tag", "boom")
    real_read = ctf._read_json

    def flaky(store, key):
        if key.startswith(ctf.AUDIT_PREFIX):
            raise StorageReadUnavailable("archive offline")
        return real_read(store, key)

    monkeypatch.setattr(ctf, "_read_json", flaky)

    assert ctf.clear_with_audit(storage, "r1", expected=marker) == "audit-read-failed"
    assert storage.exists(ctf.deferred_failure_key("r1"))


def test_a_marker_replaced_after_the_scan_is_not_cleared(monkeypatch):
    storage = MemStorage()
    scanned = _seed(storage, "r1", "tag", "duplicate locator unit")
    _seed(storage, "r1", "tag", "a different failure since the scan", status="retryable")

    assert ctf.clear_with_audit(storage, "r1", expected=scanned) == "marker-changed"
    assert storage.exists(ctf.deferred_failure_key("r1"))
