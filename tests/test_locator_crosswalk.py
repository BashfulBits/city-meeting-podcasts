"""Tests for the scoring-only agenda/provider-chapter crosswalk diagnostics."""

from scripts.research.agenda_chapters.audit_locator_crosswalk import (
    _display_ref_hit,
    _pair_features,
)


def test_dotted_display_reference_does_not_match_ordinary_words():
    assert _display_ref_hit("1.", "1. Approval of the contract")
    assert not _display_ref_hit("1.", "Insurance coverage discussion")
    assert _display_ref_hit("II.B", "II. B. Continued case")


def test_source_evidence_can_match_when_generated_title_is_a_summary():
    features = _pair_features(
        "26-1108 Approval of appointing three members to the Committee on the Environment",
        {
            "title": "Appoint three members to the Committee on the Environment",
            "evidence_text": (
                "ID 26-1108 Consider approval of appointing three members to the Committee "
                "on the Environment."
            ),
            "display_ref": "ID 26-1108",
        },
    )

    assert features["identifier_overlap"] == ["26-1108"]
    assert features["score"] >= 0.9
