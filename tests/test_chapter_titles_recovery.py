"""Tests for the shadow-only agenda evidence recovery layer."""

from __future__ import annotations

import json

from citypods.chapter_titles import recover_agenda_item_extractor_response


def test_recovery_does_not_hard_fail_on_descriptive_display_label():
    agenda = "A.\nDiscussion of the annual report.\n"
    assessment = recover_agenda_item_extractor_response(
        json.dumps(
            {
                "items": [
                    {
                        "display_ref": "Friendly label",
                        "title": "Annual report discussion",
                        "evidence_quote": "Discussion of the annual report.",
                        "line_start": 2,
                        "line_end": 2,
                    }
                ]
            }
        ),
        agenda_text=agenda,
    )
    assert not assessment.items
    assert len(assessment.recovered) == 1
    recovered = assessment.recovered[0]
    assert recovered.display_ref is None
    assert recovered.original_display_ref == "Friendly label"
    assert recovered.source_evidence == "A.\nDiscussion of the annual report."
    assert recovered.recovery_method == "exact-global+identifier-prefix"


def test_recovery_expands_multiple_hierarchical_prefix_lines():
    agenda = "IX.\nNEW CASE RESIDENTIAL\nc.\nHS-23-134 4328 Burke Road.\n"
    assessment = recover_agenda_item_extractor_response(
        json.dumps(
            {
                "items": [
                    {
                        "display_ref": "IX.c",
                        "title": "New residential case",
                        "evidence_quote": "HS-23-134 4328 Burke Road.",
                        "line_start": 4,
                        "line_end": 4,
                    }
                ]
            }
        ),
        agenda_text=agenda,
    )
    recovered = assessment.recovered[0]
    assert (recovered.line_start, recovered.line_end) == (1, 4)
    assert recovered.display_ref == "IX.c"
    assert "IX.\nNEW CASE RESIDENTIAL\nc." in recovered.source_evidence
    assert recovered.recovery_method == "exact-global+hierarchical-prefix"


def test_recovery_stores_complete_window_for_omitted_parenthetical_text():
    agenda = (
        "Specific Use Permit SUP14-6\n"
        "(Aloft-Arlington by W Hotels)\n"
        "Application for approval of a Specific Use Permit (SUP) for a Boutique Hotel.\n"
    )
    assessment = recover_agenda_item_extractor_response(
        json.dumps(
            {
                "items": [
                    {
                        "display_ref": "SUP14-6",
                        "title": "Specific Use Permit SUP14-6",
                        "evidence_quote": (
                            "Specific Use Permit SUP14-6 Application for approval of a "
                            "Specific Use Permit (SUP) for a Boutique Hotel."
                        ),
                        "line_start": 3,
                        "line_end": 3,
                    }
                ]
            }
        ),
        agenda_text=agenda,
    )
    recovered = assessment.recovered[0]
    assert (recovered.line_start, recovered.line_end) == (1, 3)
    assert recovered.recovery_method == "token-subsequence-window"
    assert "Aloft-Arlington" in recovered.source_evidence


def test_recovery_expands_forward_to_formal_reference_line():
    agenda = (
        "Hold a public hearing and consider the proposed revision.\n"
        "The action applies to the Southeast Denton Area Plan Overlay District.\n"
        "DCA26-0002B.\n"
    )
    assessment = recover_agenda_item_extractor_response(
        json.dumps(
            {
                "items": [
                    {
                        "display_ref": "DCA26-0002B",
                        "title": "Proposed revision to the Denton Development Code",
                        "evidence_quote": (
                            "Hold a public hearing and consider the proposed revision."
                        ),
                        "line_start": 1,
                        "line_end": 1,
                    }
                ]
            }
        ),
        agenda_text=agenda,
    )
    recovered = assessment.recovered[0]
    assert (recovered.line_start, recovered.line_end) == (1, 3)
    assert recovered.display_ref == "DCA26-0002B"
    assert recovered.recovery_method == "exact-global+trailing-reference"
    assert recovered.source_evidence.endswith("DCA26-0002B.")


def test_recovery_does_not_cross_blank_line_for_trailing_reference():
    agenda = "Approve the item.\n\nDCA26-0002B.\n"
    assessment = recover_agenda_item_extractor_response(
        json.dumps(
            {
                "items": [
                    {
                        "display_ref": "DCA26-0002B",
                        "title": "Approve the item",
                        "evidence_quote": "Approve the item.",
                        "line_start": 1,
                        "line_end": 1,
                    }
                ]
            }
        ),
        agenda_text=agenda,
    )
    assert not assessment.recovered
    assert len(assessment.unrecovered) == 1


def test_recovery_leaves_unresolvable_quote_rejected():
    assessment = recover_agenda_item_extractor_response(
        json.dumps(
            {
                "items": [
                    {
                        "display_ref": "ID 26-9999",
                        "title": "Invented item",
                        "evidence_quote": "This sentence is not in the agenda.",
                        "line_start": 1,
                        "line_end": 1,
                    }
                ]
            }
        ),
        agenda_text="ID 26-1000\nApprove the actual item.\n",
    )
    assert not assessment.recovered
    assert len(assessment.unrecovered) == 1
