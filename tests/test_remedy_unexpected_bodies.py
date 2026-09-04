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


def test_main_handles_classification_failure_gracefully(tmp_path, monkeypatch):
    evidence_file = tmp_path / "evidence.json"
    evidence_file.write_text(
        '{"sources": [{"source_key": "granicus:fake-source", "unexpected_findings": []}]}',
        encoding="utf-8",
    )
    output_report = tmp_path / "report.md"

    monkeypatch.setattr(_mod, "load_site_config", lambda path: {})
    monkeypatch.setattr(_mod, "make_storage", lambda cfg, url, out: None)
    monkeypatch.setattr(_mod, "load_city_configs", lambda path, reg: [])
    monkeypatch.setattr(_mod, "feed_paths_by_slug", lambda root: {})

    def fake_classify(bundle, storage=None):
        raise ConnectionError("API Gateway returned 404: Not Found")

    monkeypatch.setattr(_mod, "classify_unexpected_bodies", fake_classify)

    code = _mod.main(
        [
            "--evidence-file",
            str(evidence_file),
            "--output",
            str(output_report),
            "--repo-root",
            str(tmp_path),
        ]
    )
    assert code == 0
    assert output_report.exists()
    content = output_report.read_text(encoding="utf-8")
    assert "#### `granicus:fake-source`" in content
    assert "> Classification failed (ConnectionError)." in content
