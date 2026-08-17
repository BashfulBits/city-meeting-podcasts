"""Tests for the unexpected-body remedy command-line workflow."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_script = Path(__file__).parent.parent / "scripts" / "remedy_unexpected_bodies.py"
_spec = importlib.util.spec_from_file_location("remedy_unexpected_bodies", _script)
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)


def test_issue_number_accepts_only_ascii_decimal_values():
    assert _mod.parse_args(["--evidence-file", "evidence.json", "--issue", "123"]).issue == "123"
    with pytest.raises(SystemExit):
        _mod.parse_args(["--evidence-file", "evidence.json", "--issue", "--repo"])
    with pytest.raises(SystemExit):
        _mod.parse_args(["--evidence-file", "evidence.json", "--issue", "١٢٣"])


def test_checkout_remedy_branch_reuses_a_remote_digest_branch(tmp_path, monkeypatch):
    commands: list[list[str]] = []
    branch = "fix/12-unexpected-feed-bodies-digest"

    def fake_run(command, *, cwd, check=True):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0 if command[1] == "fetch" else 1, "", "")

    monkeypatch.setattr(_mod, "_run", fake_run)
    _mod._checkout_remedy_branch(branch, tmp_path)

    assert commands == [
        ["gh", "auth", "setup-git"],
        ["git", "fetch", "origin", f"{branch}:{branch}"],
        ["git", "rev-parse", "--verify", branch],
        ["git", "checkout", branch],
    ]


def test_checkout_remedy_branch_creates_a_branch_when_no_remote_exists(tmp_path, monkeypatch):
    commands: list[list[str]] = []
    branch = "fix/12-unexpected-feed-bodies-digest"

    def fake_run(command, *, cwd, check=True):
        commands.append(command)
        return subprocess.CompletedProcess(command, 1, "", "")

    monkeypatch.setattr(_mod, "_run", fake_run)
    _mod._checkout_remedy_branch(branch, tmp_path)

    assert commands[-1] == ["git", "checkout", "-b", branch]
