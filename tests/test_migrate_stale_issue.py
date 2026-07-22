"""Tests for the one-time GH#774 stale-issue migration."""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest.mock as mock
from datetime import UTC, datetime
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

script = SCRIPTS / "migrate_stale_issue.py"
spec = importlib.util.spec_from_file_location("migrate_stale_issue", script)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)
audit = sys.modules["audit_feeds"]

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


def _legacy_issue(*, rows=None, first_seen=None, sub_issues=None):
    rows = rows or {
        "city-a-board": {
            "severity": "warn",
            "count": 1,
            "example": "newest episode is 90d old; typical cadence ~14d",
        }
    }
    first_seen = first_seen or {"city-a-board": "2026-07-01T03:10:59+00:00"}
    state = json.dumps({"check": "stale", "first_seen": first_seen, "rows": rows})
    return {
        "number": 774,
        "title": "[feed-health] stale: 1 feed",
        "body": (
            "<!-- citypods:feed-health -->\n"
            "<!-- citypods:feed-health:key=stale -->\n"
            f"<!-- citypods:feed-health:state\n{state}\n-->"
        ),
        "state": "OPEN",
        "labels": [{"name": "signal:feed-health"}],
        "subIssues": sub_issues or [],
    }


def _contexts():
    return {
        "city-a-board": mod._FeedContext(
            city="city-a",
            feed_config_url=(
                "https://github.com/test/repo/blob/main/config/feeds/city-a-board.yml"
            ),
        )
    }


def test_plan_preserves_first_seen_and_links_feed_yaml():
    plan = mod._migration_plan(_legacy_issue(), contexts=_contexts(), now=NOW)

    assert plan.parent == 774
    assert len(plan.children) == 1
    child = plan.children[0]
    assert child.incident_id == "20260701-1"
    assert "2026-07-01" in child.body
    assert "config/feeds/city-a-board.yml" in child.body
    state = audit._parse_stale_state(child.body)
    assert state["first_seen"] == "2026-07-01T03:10:59+00:00"
    assert state["last_observed"] == NOW.isoformat()
    assert "citypods:feed-health:key=stale" not in plan.parent_body
    assert "citypods:stale-cohort:v1" in plan.parent_body


def test_plan_rejects_rows_without_matching_first_seen():
    issue = _legacy_issue(first_seen={"different-feed": NOW.isoformat()})

    with pytest.raises(RuntimeError, match="legacy state is incomplete"):
        mod._migration_plan(issue, contexts=_contexts(), now=NOW)


def test_apply_creates_and_attaches_children_before_editing_parent():
    plan = mod._migration_plan(_legacy_issue(), contexts=_contexts(), now=NOW)
    calls = []

    def fake_gh(*args, **_kwargs):
        calls.append(args)
        if args[:2] == ("issue", "create"):
            return "https://github.com/test/repo/issues/1001\n"
        return ""

    empty_catalog = audit._StaleCatalog([], {}, {}, {})
    with mock.patch.object(mod, "_open_stale_catalog", return_value=empty_catalog):
        with mock.patch.object(mod, "_gh", side_effect=fake_gh):
            with mock.patch.object(mod, "_attach_sub_issue") as attach:
                mod._apply_plan(
                    plan,
                    github_repo="test/repo",
                    issue=_legacy_issue(),
                    dry_run=False,
                )

    attach.assert_called_once_with(github_repo="test/repo", parent=774, child=1001)
    create_index = next(i for i, call in enumerate(calls) if call[:2] == ("issue", "create"))
    edit_index = next(i for i, call in enumerate(calls) if call[:2] == ("issue", "edit"))
    assert create_index < edit_index


def test_apply_resumes_existing_unattached_child_without_duplicate():
    issue = _legacy_issue()
    plan = mod._migration_plan(issue, contexts=_contexts(), now=NOW)
    child = {
        "number": 1001,
        "check": "stale",
        "slug": "city-a-board",
        "incident_id": "20260701-1",
        "parent": 774,
    }
    catalog = audit._StaleCatalog(
        open_parents=[],
        open_children={("stale", "city-a-board"): child},
        history={("stale", "city-a-board"): [child]},
        children_by_parent={774: [child]},
    )

    with mock.patch.object(mod, "_open_stale_catalog", return_value=catalog):
        with mock.patch.object(mod, "_gh") as gh:
            with mock.patch.object(mod, "_attach_sub_issue") as attach:
                mod._apply_plan(
                    plan,
                    github_repo="test/repo",
                    issue=issue,
                    dry_run=False,
                )

    assert not any(call.args[0][:2] == ("issue", "create") for call in gh.call_args_list)
    attach.assert_called_once_with(github_repo="test/repo", parent=774, child=1001)


def test_apply_does_not_reattach_native_child():
    issue = _legacy_issue(sub_issues={"nodes": [{"number": 1001}], "totalCount": 1})
    plan = mod._migration_plan(issue, contexts=_contexts(), now=NOW)
    child = {
        "number": 1001,
        "check": "stale",
        "slug": "city-a-board",
        "incident_id": "20260701-1",
        "parent": 774,
    }
    catalog = audit._StaleCatalog(
        open_parents=[],
        open_children={("stale", "city-a-board"): child},
        history={("stale", "city-a-board"): [child]},
        children_by_parent={774: [child]},
    )

    with mock.patch.object(mod, "_open_stale_catalog", return_value=catalog):
        with mock.patch.object(mod, "_gh"):
            with mock.patch.object(mod, "_attach_sub_issue") as attach:
                mod._apply_plan(
                    plan,
                    github_repo="test/repo",
                    issue=issue,
                    dry_run=False,
                )

    attach.assert_not_called()
