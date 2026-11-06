"""Tests for scripts/clear_terminal_failures.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import clear_terminal_failures as ctf  # noqa: E402

from citypods.compute.llm_deferred import _write_json  # noqa: E402
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
