from __future__ import annotations

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
