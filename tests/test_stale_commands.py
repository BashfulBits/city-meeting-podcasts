"""Tests for maintainer-only stale-feed lifecycle command handling."""

from __future__ import annotations

import importlib.util
import json
import sys
import textwrap
from datetime import date
from pathlib import Path

import pytest
import yaml

_script = Path(__file__).parent.parent / "scripts" / "stale_commands.py"
_spec = importlib.util.spec_from_file_location("stale_commands", _script)
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)

CommandError = _mod.CommandError
StaleCommand = _mod.StaleCommand
apply_lifecycle = _mod.apply_lifecycle
parse_command = _mod.parse_command
process_event = _mod.process_event

TODAY = date(2026, 7, 22)
WRITE_PERMISSION = {"permission": "write", "role_name": "write"}
VALID_FEED = """\
slug: sample-tx
provider: granicus
source:
  feed_url: https://sample.granicus.com/ViewPublisherRSS.php?view_id=2
podcast_title: Sample Council
podcast_author: City of Sample
podcast_email: ""
podcast_description: Meetings.
state: TX
"""


def _event(
    command: str,
    *,
    association: str = "COLLABORATOR",
    body: str | None = None,
    labels: list[str] | None = None,
    state: str = "open",
    check: str = "stale",
):
    return {
        "issue": {
            "number": 123,
            "state": state,
            "body": body
            or (
                f"<!-- citypods:stale-incident:v1 check={check} slug=sample-tx "
                "incident=20260722-1 parent=100 -->"
            ),
            "labels": [{"name": name} for name in (labels or ["signal:feed-health"])],
        },
        "comment": {
            "body": command,
            "author_association": association,
            "user": {"login": "maintainer-one"},
        },
    }


def _repo(tmp_path: Path, feed: str = VALID_FEED) -> Path:
    config = tmp_path / "config"
    feeds = config / "feeds"
    feeds.mkdir(parents=True)
    (config / "site_config.yml").write_text("defaults: {}\n")
    (feeds / "sample-tx.yml").write_text(textwrap.dedent(feed))
    return tmp_path


def test_parse_pause_requires_future_date_and_preserves_quoted_reason():
    command = parse_command(
        '/stale pause --until 2026-09-15 --reason "summer recess" '
        "--evidence https://example.gov/calendar",
        today=TODAY,
    )
    assert command == StaleCommand(
        status="paused",
        reason="summer recess",
        recheck_after=date(2026, 9, 15),
        evidence_url="https://example.gov/calendar",
    )


@pytest.mark.parametrize(
    "action,status", [("activate", "active"), ("dormant", "dormant"), ("retire", "retired")]
)
def test_parse_indefinite_lifecycle_actions(action, status):
    command = parse_command(f'/stale {action} --reason "irregular body"', today=TODAY)
    assert command.status == status
    assert command.reason == "irregular body"
    assert command.recheck_after is None


@pytest.mark.parametrize(
    "command,match",
    [
        ("/stale pause --reason recess", "requires --until"),
        ('/stale pause --until nope --reason "x"', "valid YYYY-MM-DD"),
        ('/stale pause --until 2026-07-22 --reason "x"', "future date"),
        ('/stale dormant --until 2026-09-01 --reason "x"', "does not accept"),
        ('/stale dormant --reason "x" --reason "y"', "duplicate argument"),
        ("/stale retire", "non-empty value"),
        ('/stale dormant --reason "unterminated', "invalid command quoting"),
        ('/stale dormant --reason "x" extra', "unknown argument"),
    ],
)
def test_parse_rejects_invalid_or_ambiguous_commands(command, match):
    with pytest.raises(CommandError, match=match):
        parse_command(command, today=TODAY)


def test_parse_rejects_unsafe_evidence_url():
    with pytest.raises(CommandError, match="valid HTTPS URL"):
        parse_command(
            '/stale dormant --reason "x" --evidence http://127.0.0.1/private',
            today=TODAY,
        )


@pytest.mark.parametrize("permission", ["none", "read", "triage"])
def test_process_event_rejects_actors_without_write_permission(tmp_path, permission):
    with pytest.raises(ValueError, match="repository write"):
        process_event(
            _event('/stale dormant --reason "x"'),
            repo_root=_repo(tmp_path),
            permission={"permission": permission, "role_name": permission},
            today=TODAY,
        )


def test_process_event_requires_trusted_generated_issue_marker(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(CommandError, match="not a generated feed-health"):
        process_event(
            _event('/stale dormant --reason "x"', labels=["type:operations"]),
            repo_root=repo,
            permission=WRITE_PERMISSION,
            today=TODAY,
        )
    with pytest.raises(CommandError, match="no valid stale-incident marker"):
        process_event(
            _event('/stale dormant --reason "x"', body="ordinary issue"),
            repo_root=repo,
            permission=WRITE_PERMISSION,
            today=TODAY,
        )


def test_apply_lifecycle_inserts_block_without_rewriting_unrelated_yaml(tmp_path):
    path = tmp_path / "feed.yml"
    path.write_text(VALID_FEED)

    changed = apply_lifecycle(
        path,
        StaleCommand("paused", "Documented recess", date(2026, 9, 15), None),
    )

    text = path.read_text()
    assert changed is True
    assert "source:\n  feed_url:" in text
    assert "lifecycle:\n  status: paused\n  recheck_after: 2026-09-15" in text
    assert text.index("lifecycle:") < text.index("podcast_title:")
    assert yaml.safe_load(text)["lifecycle"]["reason"] == "Documented recess"


def test_apply_lifecycle_replaces_existing_block_and_is_idempotent(tmp_path):
    path = tmp_path / "feed.yml"
    path.write_text(
        VALID_FEED.replace(
            "podcast_title:",
            "lifecycle:\n  status: dormant\n  reason: old reason\npodcast_title:",
        )
    )
    command = StaleCommand("retired", "Body dissolved", None, "https://example.gov/minutes")

    assert apply_lifecycle(path, command) is True
    assert apply_lifecycle(path, command) is False
    data = yaml.safe_load(path.read_text())
    assert data["lifecycle"] == {
        "status": "retired",
        "reason": "Body dissolved",
        "evidence_url": "https://example.gov/minutes",
    }
    assert path.read_text().count("lifecycle:") == 1


def test_activate_removes_dormant_lifecycle_block(tmp_path):
    path = tmp_path / "feed.yml"
    path.write_text(
        VALID_FEED.replace(
            "podcast_title:",
            "lifecycle:\n  status: dormant\n  reason: irregular body\npodcast_title:",
        )
    )

    assert apply_lifecycle(path, StaleCommand("active", "regular meetings resumed")) is True
    assert "lifecycle:" not in path.read_text()
    assert yaml.safe_load(path.read_text())["podcast_title"] == "Sample Council"


def test_process_event_edits_exact_feed_and_emits_idempotent_pr_plan(tmp_path):
    repo = _repo(tmp_path)

    result = process_event(
        _event(
            '/stale pause --until 2026-10-01 --reason "fall recess" '
            "--evidence https://example.gov/calendar"
        ),
        repo_root=repo,
        permission=WRITE_PERMISSION,
        today=TODAY,
    )

    assert result["config_path"] == "config/feeds/sample-tx.yml"
    assert result["branch"] == "chore/stale-123-lifecycle"
    assert result["status"] == "paused"
    assert "incident #123" in result["pr_body"]
    assert "remains open" in result["pr_body"]
    lifecycle = yaml.safe_load((repo / result["config_path"]).read_text())["lifecycle"]
    assert lifecycle == {
        "status": "paused",
        "recheck_after": date(2026, 10, 1),
        "reason": "fall recess",
        "evidence_url": "https://example.gov/calendar",
    }


def test_dormant_resumed_activate_emits_pr_that_removes_lifecycle(tmp_path):
    repo = _repo(
        tmp_path,
        VALID_FEED.replace(
            "podcast_title:",
            "lifecycle:\n  status: dormant\n  reason: irregular body\npodcast_title:",
        ),
    )

    result = process_event(
        _event(
            '/stale activate --reason "regular meetings resumed"',
            check="dormant-resumed",
        ),
        repo_root=repo,
        permission=WRITE_PERMISSION,
        today=TODAY,
    )

    assert result["status"] == "active"
    assert "lifecycle:" not in (repo / result["config_path"]).read_text()
    assert "Requested lifecycle: `active`" in result["pr_body"]


def test_activate_is_rejected_on_an_ordinary_stale_incident(tmp_path):
    with pytest.raises(CommandError, match="dormant-resumed"):
        process_event(
            _event('/stale activate --reason "not applicable"'),
            repo_root=_repo(tmp_path),
            permission=WRITE_PERMISSION,
            today=TODAY,
        )


def test_cli_rejection_emits_safe_issue_feedback(tmp_path):
    event_path = tmp_path / "event.json"
    permission_path = tmp_path / "permission.json"
    out = tmp_path / "result.json"
    event_path.write_text(json.dumps(_event("/stale retire", association="CONTRIBUTOR")))
    permission_path.write_text(json.dumps({"permission": "read", "role_name": "read"}))

    code = _mod.main(
        [
            "--event",
            str(event_path),
            "--permission",
            str(permission_path),
            "--repo-root",
            str(tmp_path),
            "--out",
            str(out),
        ]
    )

    assert code == 2
    result = json.loads(out.read_text())
    assert result["ok"] is False
    assert result["issue_number"] == 123
    assert "repository write" in result["comment"]


def test_cli_yaml_parser_failure_still_emits_rejection_feedback(tmp_path):
    repo = _repo(tmp_path)
    (repo / "config" / "site_config.yml").write_text("defaults: [\n")
    event_path = tmp_path / "event.json"
    permission_path = tmp_path / "permission.json"
    out = tmp_path / "result.json"
    event_path.write_text(
        json.dumps(_event('/stale dormant --reason "irregular meeting schedule"'))
    )
    permission_path.write_text(json.dumps(WRITE_PERMISSION))

    code = _mod.main(
        [
            "--event",
            str(event_path),
            "--permission",
            str(permission_path),
            "--repo-root",
            str(repo),
            "--out",
            str(out),
        ]
    )

    assert code == 2
    result = json.loads(out.read_text())
    assert result["ok"] is False
    assert result["issue_number"] == 123
    assert "command rejected" in result["comment"]
