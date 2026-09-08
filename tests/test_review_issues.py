from __future__ import annotations

import json
import subprocess

import pytest

from citypods.review_issues import (
    SAFE_BODY_LIMIT_BYTES,
    PublicationStatus,
    append_bounded_envelope,
    append_envelope,
    bounded_body,
    checked_decisions,
    decode_envelope,
    publication_summary,
    render_decision_block,
    require_one_decision,
)


def test_envelope_uses_the_last_marker_and_preserves_the_publisher_identity():
    body = append_envelope(
        "untrusted text <!-- citypods-review: bad -->",
        family="h16",
        candidate_id="episode:fp:1:confirmed_empty",
    )

    assert decode_envelope(body) == {
        "v": 1,
        "family": "h16",
        "candidate_id": "episode:fp:1:confirmed_empty",
        "surface": "child",
    }


def test_bounded_body_is_utf8_safe_and_leaves_a_visible_artifact_hint():
    body, truncated = bounded_body("😀" * 40, limit=100)

    assert truncated is True
    assert len(body.encode("utf-8")) <= 100
    assert "workflow run artifact" in body


def test_review_choice_requires_exactly_one_checkbox():
    body = render_decision_block(("Confirm empty", "Restore media")).replace(
        "- [ ] Confirm", "- [x] Confirm"
    )
    assert require_one_decision(body, ("Confirm empty", "Restore media")) == "Confirm empty"
    with pytest.raises(ValueError, match="exactly one"):
        require_one_decision(
            body.replace("- [ ] Restore", "- [x] Restore"), ("Confirm empty", "Restore media")
        )


def test_review_choices_ignore_untrusted_checkbox_text_outside_the_decision_block():
    body = "Provider output:\n- [x] Confirm empty\n\n" + render_decision_block(
        ("Confirm empty", "Restore media")
    )

    assert checked_decisions(body, ("Confirm empty", "Restore media")) == ()


def test_bounded_envelope_survives_utf8_truncation():
    body, truncated = append_bounded_envelope(
        "😀" * 20_000,
        family="h16",
        candidate_id="candidate",
        limit=SAFE_BODY_LIMIT_BYTES,
    )

    assert truncated is True
    assert len(body.encode("utf-8")) <= SAFE_BODY_LIMIT_BYTES
    assert decode_envelope(body)["candidate_id"] == "candidate"


def test_publication_summary_distinguishes_blocked_from_empty_work():
    assert '"status": "no_candidates"' in publication_summary(
        status=PublicationStatus.NO_CANDIDATES, selected=0, published=0
    )
    assert '"status": "blocked"' in publication_summary(
        status=PublicationStatus.BLOCKED,
        selected=0,
        published=0,
        reasons=("dispatch capacity exhausted",),
    )


@pytest.mark.parametrize(
    ("stderr", "reason"),
    [
        ("ValueError: choose exactly one decision", "no_decision"),
        (
            "ValueError: H16 candidate differs from durable media availability field state",
            "stale_candidate",
        ),
    ],
)
def test_shared_review_resolver_turns_non_actionable_adapter_errors_into_skips(
    tmp_path, monkeypatch, capsys, stderr, reason
):
    from scripts import resolve_review_issue

    body_file = tmp_path / "issue.md"
    body_file.write_text(
        append_envelope("", family="h16", candidate_id="candidate"), encoding="utf-8"
    )
    monkeypatch.setattr(
        resolve_review_issue.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", stderr),
    )

    assert (
        resolve_review_issue.main(
            [
                "--issue-number",
                "123",
                "--actor",
                "test",
                "--body-file",
                str(body_file),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["reason"] == reason
