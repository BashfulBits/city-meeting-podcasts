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
        '{"sources": [{"source_key": "granicus:fake-source", "unexpected_findings": '
        '[{"unexpected_body": "Council", "episodes": []}]}]}',
        encoding="utf-8",
    )
    output_report = tmp_path / "report.md"

    monkeypatch.setattr(_mod, "load_site_config", lambda path: {})
    monkeypatch.setattr(_mod, "make_storage", lambda cfg, url, out: None)
    monkeypatch.setattr(_mod, "load_city_configs", lambda path, reg: [])
    monkeypatch.setattr(_mod, "feed_paths_by_slug", lambda root: {})

    def fake_classify(bundle, storage=None, **kwargs):
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
    assert code == 1
    assert output_report.exists()
    content = output_report.read_text(encoding="utf-8")
    assert "#### `granicus:fake-source`" in content
    assert "> Classification failed: ConnectionError" in content
    assert "API Gateway returned" not in content


def test_partial_failure_still_verifies_opens_pr_and_reports_failure(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from citypods.audit_remedy import BodyProposal, RemedyOutput, RemedyPlan

    bundle = {
        "source_key": "test",
        "city": {"slug": "test-city"},
        "existing_feeds": [{"slug": "council"}],
        "unexpected_findings": [{"unexpected_body": "Special", "episodes": []}],
    }
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({"sources": [bundle, bundle]}))
    monkeypatch.setattr(_mod, "load_site_config", lambda path: {})
    monkeypatch.setattr(_mod, "make_storage", lambda *args: None)
    monkeypatch.setattr(_mod, "load_city_configs", lambda *args: [SimpleNamespace(slug="council")])
    monkeypatch.setattr(_mod, "feed_paths_by_slug", lambda *args: {})
    calls = []
    proposal = BodyProposal(
        source_key="test",
        unexpected_body="Special",
        action="union",
        target_feeds=["council"],
        rationale="Council special session",
    )

    def classify(*args, **kwargs):
        calls.append("classify")
        if len(calls) == 1:
            raise TimeoutError()
        return RemedyOutput(proposals=[proposal], model="test-model")

    monkeypatch.setattr(_mod, "classify_unexpected_bodies", classify)
    monkeypatch.setattr(_mod, "validate_proposals", lambda *args: RemedyPlan(accepted=[proposal]))
    monkeypatch.setattr(_mod.SourceContext, "from_city", lambda *args: None)
    monkeypatch.setattr(_mod, "apply_remedy_plan", lambda *args, **kwargs: [tmp_path / "feed.yml"])
    monkeypatch.setattr(_mod, "verify_remedy_mutations", lambda **kwargs: (True, "passed"))
    monkeypatch.setattr(_mod, "_open_pull_request", lambda *args: "https://example.test/pr/1")
    comments = []
    monkeypatch.setattr(
        _mod, "_post_final_comment", lambda *args, **kwargs: comments.append(kwargs)
    )
    assert (
        _mod.main(
            [
                "--evidence-file",
                str(evidence),
                "--repo-root",
                str(tmp_path),
                "--output",
                str(tmp_path / "report.md"),
                "--issue",
                "1231",
                "--apply",
            ]
        )
        == 1
    )
    assert comments[0]["pr_url"] == "https://example.test/pr/1"
    assert comments[0]["failed_total"] == 1
    assert "Verification: passed" in comments[0]["report_md"]


def test_empty_evidence_posts_a_terminal_report(tmp_path, monkeypatch):
    evidence = tmp_path / "empty.json"
    evidence.write_text('{"sources": []}')
    comments = []
    monkeypatch.setattr(
        _mod, "_post_final_comment", lambda *args, **kwargs: comments.append(kwargs)
    )
    assert _mod.main(["--evidence-file", str(evidence), "--issue", "1231"]) == 0
    assert "no unexpected labels" in comments[0]["report_md"]
