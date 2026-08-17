"""Tests for the `/remedy` issue-comment command handler."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_script = Path(__file__).parent.parent / "scripts" / "remedy_commands.py"
_spec = importlib.util.spec_from_file_location("remedy_commands", _script)
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)

process_event = _mod.process_event
UNEXPECTED_BODY_MARKER = _mod.UNEXPECTED_BODY_MARKER

WRITE = {"permission": "write", "role_name": "write"}
READ = {"permission": "read", "role_name": "read"}
NONE = {"permission": "none"}


def _event(number: int = 1231, body: str | None = None) -> dict:
    return {
        "issue": {"number": number, "body": body if body is not None else UNEXPECTED_BODY_MARKER}
    }


def test_accepted_for_a_writer_on_the_real_unexpected_body_issue():
    result = process_event(_event(), WRITE)
    assert result == {
        "accepted": True,
        "issue_number": 1231,
        "comment": (
            "🔄 Re-running automated remedy for issue #1231. I'll classify this issue's "
            "current findings and post the result here, with a link to the PR if one is "
            "opened."
        ),
    }


@pytest.mark.parametrize("permission", ["maintain", "admin", "push"])
def test_accepted_for_every_write_or_higher_role(permission):
    assert process_event(_event(), {"permission": permission, "role_name": permission})["accepted"]


def test_rejected_for_read_only_access():
    result = process_event(_event(), READ)
    assert result["accepted"] is False
    assert "write" in result["comment"].lower()


def test_rejected_for_no_collaborator_relationship():
    assert process_event(_event(), NONE)["accepted"] is False


def test_rejected_when_the_issue_is_not_the_unexpected_body_issue():
    result = process_event(_event(body="just a random issue"), WRITE)
    assert result["accepted"] is False
    assert "unexpected-body" in result["comment"]


def test_rejected_for_a_different_checks_marker():
    """A per-slug (or any other check's) marker must not satisfy the unexpected-body match."""
    other = "<!-- citypods:feed-health:key=meetings-url-dead::somecity -->"
    assert process_event(_event(body=other), WRITE)["accepted"] is False


def test_permission_denial_is_checked_before_the_marker():
    """Fail on the actor's own permission first -- never leak which issues carry the marker to
    someone who isn't authorized to act on any of them."""
    result = process_event(_event(body="unrelated issue, no marker"), READ)
    assert result["accepted"] is False
    assert "write" in result["comment"].lower()


def test_missing_body_does_not_crash():
    result = process_event({"issue": {"number": 5}}, WRITE)
    assert result == {
        "accepted": False,
        "issue_number": 5,
        "comment": (
            "❌ `/remedy` only runs on the feed-health audit's own consolidated "
            "`unexpected-body` issue."
        ),
    }


def test_issue_number_is_preserved_even_on_rejection():
    assert process_event(_event(number=999, body="x"), WRITE)["issue_number"] == 999
