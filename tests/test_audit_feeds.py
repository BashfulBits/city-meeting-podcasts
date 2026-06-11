"""Tests for scripts/audit_feeds.py reconcile logic (no real GitHub calls)."""

from __future__ import annotations

import importlib.util
import unittest.mock as mock
from pathlib import Path

import pytest

from citypods.audit import WARN, Finding

# Load audit_feeds from scripts/ without making it a package.
_script = Path(__file__).parent.parent / "scripts" / "audit_feeds.py"
_spec = importlib.util.spec_from_file_location("audit_feeds", _script)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

reconcile = _mod.reconcile
_title = _mod._title
_body = _mod._body
_state_comment = _mod._state_comment


def _finding(slug="testcity", check="test-check", sev=WARN, msg="something is wrong"):
    return Finding(slug, check, sev, msg)


def _existing_issue(finding: Finding, *, number: int = 1, body_override: str | None = None):
    f = finding
    title = _title(f.slug, f.check)
    body = body_override if body_override is not None else _body(f.message, f.severity, f.check)
    return {title: {"number": number, "title": title, "body": body}}


# ---------------------------------------------------------------------------
# _state_comment
# ---------------------------------------------------------------------------


def test_state_comment_contains_severity_and_message():
    f = _finding(sev=WARN, msg="pipeline may be stalled")
    comment = _state_comment(f)
    assert "warn" in comment
    assert "pipeline may be stalled" in comment
    assert "Audit update" in comment


# ---------------------------------------------------------------------------
# reconcile — dry_run (no GitHub calls, uses empty existing dict)
# ---------------------------------------------------------------------------


def test_reconcile_dry_run_create_printed(capsys):
    f = _finding()
    reconcile([f], dry_run=True)
    out = capsys.readouterr().out
    assert "CREATE" in out
    assert _title(f.slug, f.check) in out


def test_reconcile_dry_run_close_printed(capsys):
    # dry_run uses empty existing, so close is never triggered through the normal path.
    # The summary line should show 0 findings.
    reconcile([], dry_run=True)
    out = capsys.readouterr().out
    assert "0 active finding(s)" in out


# ---------------------------------------------------------------------------
# reconcile — live paths (mocked _gh + _open_issues)
# ---------------------------------------------------------------------------


def _run_reconcile(findings, existing_issues):
    """Run reconcile with _gh and _open_issues both mocked; return list of gh call arg tuples."""
    calls: list[tuple] = []
    with mock.patch.object(_mod, "_open_issues", return_value=existing_issues):
        with mock.patch.object(_mod, "_gh", side_effect=lambda *a, **kw: calls.append(a) or ""):
            reconcile(findings, dry_run=False)
    return calls


def test_reconcile_creates_new_issue():
    f = _finding()
    calls = _run_reconcile([f], existing_issues={})
    create_calls = [a for a in calls if len(a) >= 2 and a[1] == "create"]
    assert create_calls, "expected an issue create call"


def test_reconcile_no_action_when_body_unchanged():
    f = _finding()
    existing = _existing_issue(f, number=10)
    calls = _run_reconcile([f], existing_issues=existing)
    edit_calls = [a for a in calls if len(a) >= 2 and a[1] == "edit"]
    comment_calls = [a for a in calls if len(a) >= 2 and a[1] == "comment"]
    assert not edit_calls, "body unchanged → no edit"
    assert not comment_calls, "body unchanged → no comment"


def test_reconcile_update_edits_body_and_posts_comment():
    f_old = _finding(msg="old message")
    f_new = _finding(msg="new message")  # same slug/check, different message
    existing = _existing_issue(f_old, number=42)
    calls = _run_reconcile([f_new], existing_issues=existing)
    edit_calls = [a for a in calls if len(a) >= 2 and a[1] == "edit"]
    comment_calls = [a for a in calls if len(a) >= 2 and a[1] == "comment"]
    assert edit_calls, "state changed → body should be updated"
    assert comment_calls, "state changed → comment should be posted"
    # The comment should be on the same issue number
    assert "42" in comment_calls[0]


def test_reconcile_closes_resolved_issue():
    existing = {
        "[feed-health] somecity: some-check": {
            "number": 99,
            "title": "[feed-health] somecity: some-check",
            "body": "x",
        }
    }
    calls = _run_reconcile(findings=[], existing_issues=existing)
    close_calls = [a for a in calls if len(a) >= 2 and a[1] == "close"]
    assert close_calls, "resolved issue should be closed"
    assert "99" in close_calls[0]


def test_reconcile_does_not_autoclose_needs_human_verification():
    existing = {
        "[feed-health] somecity: meetings-url-dead": {
            "number": 77,
            "title": "[feed-health] somecity: meetings-url-dead",
            "body": "x",
        }
    }
    calls = _run_reconcile(findings=[], existing_issues=existing)
    close_calls = [a for a in calls if len(a) >= 2 and a[1] == "close"]
    assert not close_calls, "meetings-url issues must not auto-close"
