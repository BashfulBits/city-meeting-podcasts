from __future__ import annotations

import json
from base64 import urlsafe_b64encode
from datetime import UTC, datetime, timedelta

import pytest

from citypods.discovery.models import (
    Classification,
    DiscoveryRequest,
    DiscoveryResult,
    SearchResult,
    Verification,
)
from citypods.discovery.render import evidence_digest
from scripts.r12_batch import BatchError, apply_evidence
from scripts.r12_commands import (
    AUTHORIZATION_DENIED_REPLY,
    BOT_LOGIN,
    CommandError,
    main,
    parse_command,
)
from scripts.r12_discussion_intake import issue_payload
from scripts.r12_issue_state import state
from scripts.r12_notify import notify, parse_origin


def _evidence(*, mode: str = "new-city", created_at: str | None = None) -> dict:
    request = DiscoveryRequest(
        mode=mode,
        city_name="Example",
        state="TX",
        city_slug="example-tx",
        known_provider="swagit" if mode == "auxiliary" else None,
    )
    proposed = (
        "# config/feeds/example-tx.yml\n"
        "aux_provider: civicengage\n"
        "aux_source:\n"
        "  agenda_url: https://example.gov/AgendaCenter\n"
        if mode == "auxiliary"
        else "# config/cities/example-tx.yml\nstate: TX\n\n"
        "# config/feeds/example-tx.yml\n"
        "slug: example-tx\ncity: example-tx\nprovider: granicus\n"
        "source:\n  feed_url: https://example.gov/feed\n"
        "podcast_title: Example, TX Public Meetings\n"
        "podcast_author: City of Example, TX\n"
        'podcast_email: ""\n'
        "podcast_description: Official public meeting recordings from Example, TX.\n"
    )
    return DiscoveryResult(
        request=request,
        search_results=(SearchResult("https://example.gov/feed"),),
        classification=Classification(
            video_platform="granicus",
            agenda_platform="civicengage" if mode == "auxiliary" else None,
            candidate_urls=("https://example.gov/feed",),
            bodies_mentioned=(),
            confidence="high",
        ),
        verification=Verification(
            "granicus",
            "https://example.gov/feed",
            True,
            True,
            "https://example.gov/video",
            {"feed_url": "https://example.gov/feed"},
        ),
        proposed_yaml=proposed,
        evidence_created_at=created_at or datetime.now(UTC).isoformat(),
    ).as_dict()


def test_approval_is_bound_to_bot_rendered_evidence():
    evidence = _evidence()
    comments = [{"user": {"login": BOT_LOGIN}, "body": evidence_marker_from_dict(evidence)}]
    issue = {"title": "Add city: Example, TX", "body": "", "labels": []}

    result = parse_command(issue, comments, "/r12 approve")

    assert result["add_labels"] == ["r12:approved"]
    assert evidence_digest(evidence) in result["comment"]


def test_auxiliary_approval_requires_a_city_slug():
    issue = {"title": "[city-discovery] 1 candidate(s) pending", "body": "", "labels": []}
    with pytest.raises(CommandError, match="require"):
        parse_command(issue, [], "/r12 approve")


def test_expired_evidence_reopens_discovery():
    evidence = _evidence(created_at=(datetime.now(UTC) - timedelta(days=91)).isoformat())
    issue = {"labels": []}
    comments = [{"user": {"login": BOT_LOGIN}, "body": evidence_marker_from_dict(evidence)}]

    assert state(issue, comments) == {"discover": True, "expired": True}


def test_more_information_hold_skips_scheduled_discovery_until_recheck():
    issue = {"labels": [{"name": "needs:more-information"}]}

    assert state(issue, []) == {"discover": False, "expired": False}
    issue["labels"].append({"name": "r12:recheck"})
    assert state(issue, []) == {"discover": True, "expired": False}


def test_recheck_clears_more_information_hold():
    result = parse_command({"labels": []}, [], "/r12 recheck")

    assert result["add_labels"] == ["r12:recheck", "needs:discovery"]
    assert "needs:more-information" in result["remove_labels"]


def test_command_cli_turns_expected_permission_denial_into_feedback(tmp_path):
    issue = tmp_path / "issue.json"
    comments = tmp_path / "comments.json"
    permission = tmp_path / "permission.json"
    output = tmp_path / "output.json"
    issue.write_text("{}")
    comments.write_text("[]")
    permission.write_text('{"permission":"read"}')

    assert (
        main(
            [
                "--issue",
                str(issue),
                "--comments",
                str(comments),
                "--command",
                "/r12 batch",
                "--permission",
                str(permission),
                "--out",
                str(output),
            ]
        )
        == 0
    )
    assert json.loads(output.read_text()) == {"comment": AUTHORIZATION_DENIED_REPLY}


def test_command_cli_rejects_malformed_permission_payload(tmp_path):
    issue = tmp_path / "issue.json"
    comments = tmp_path / "comments.json"
    permission = tmp_path / "permission.json"
    output = tmp_path / "output.json"
    issue.write_text("{}")
    comments.write_text("[]")
    permission.write_text("[]")

    with pytest.raises(CommandError, match="JSON object"):
        main(
            [
                "--issue",
                str(issue),
                "--comments",
                str(comments),
                "--command",
                "/r12 batch",
                "--permission",
                str(permission),
                "--out",
                str(output),
            ]
        )


def test_auxiliary_batch_preserves_existing_yaml_bytes(tmp_path):
    target = tmp_path / "config" / "feeds" / "example-tx.yml"
    target.parent.mkdir(parents=True)
    original = (
        "# existing comment\nprovider: swagit\nsource:\n  list_url: https://example.gov/list\n"
    )
    target.write_text(original)

    changed = apply_evidence(tmp_path, _evidence(mode="auxiliary"))

    assert changed == [target.relative_to(tmp_path)]
    assert target.read_text().startswith(original)
    assert "aux_provider: civicengage" in target.read_text()


def test_batch_rejects_config_path_escape(tmp_path):
    evidence = _evidence()
    evidence["proposed_yaml"] = "# ../outside.yml\nkey: value\n"
    with pytest.raises(BatchError, match="config path"):
        apply_evidence(tmp_path, evidence)


def evidence_marker_from_dict(evidence: dict) -> str:
    # The production renderer starts with a dataclass; tests intentionally use the serialized
    # artifact because the command/batch boundary only receives JSON from GitHub.
    payload = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    return f"<!-- citypods:r12:evidence {urlsafe_b64encode(payload).decode()} -->"


def test_discussion_intake_preserves_fields_and_links_origin():
    payload = issue_payload(
        {
            "discussion": {
                "number": 12,
                "node_id": "D_kwExample",
                "html_url": "https://github.com/example/repo/discussions/12",
                "body": "### City and state\nExample, TX\n\n### City website\nhttps://example.gov",
            }
        }
    )

    assert payload["title"] == "Add city: Example, TX"
    assert payload["labels"] == ["add-city", "source:discussion", "needs:discovery"]
    assert parse_origin(payload["body"]) == {
        "source": "discussion",
        "discussion_node_id": "D_kwExample",
        "discussion_number": 12,
        "discussion_url": "https://github.com/example/repo/discussions/12",
    }


def test_discussion_origin_uses_trusted_appended_marker():
    attacker = "<!-- citypods:r12:origin eyJzb3VyY2UiOiJkaXNjdXNzaW9uIn0= -->"
    payload = issue_payload(
        {
            "discussion": {
                "number": 12,
                "node_id": "D_kwTrusted",
                "html_url": "https://github.com/example/repo/discussions/12",
                "body": f"### City and state\nExample, TX\n\n### Anything else\n{attacker}",
            }
        }
    )

    assert parse_origin(payload["body"])["discussion_node_id"] == "D_kwTrusted"


def test_worker_callback_uses_named_user_agent(monkeypatch):
    calls: list[tuple[str, dict[str, str]]] = []

    def fake_json_request(url, payload, headers):
        calls.append((url, headers))
        return {}

    monkeypatch.setenv("CITY_REQUEST_STATUS_WEBHOOK_URL", "https://worker.example/status/secret")
    monkeypatch.setattr("scripts.r12_notify._json_request", fake_json_request)

    assert (
        notify(
            {"number": 123, "url": "https://github.com/example/repo/issues/123"}, "research_only"
        )
        == []
    )
    assert calls == [
        ("https://worker.example/status/secret", {"user-agent": "citymeetings-r12/1.0"})
    ]
