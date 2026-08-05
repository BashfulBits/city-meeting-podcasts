"""Tests for the direct, source-anchored agenda-item extraction experiment."""

from __future__ import annotations

import json

import pytest

from citypods.chapter_titles import (
    AGENDA_ITEM_EXTRACTOR_CONTRACT,
    TITLE_EQUIVALENCE_CONTRACT,
    TitleEquivalenceMatch,
    assess_agenda_item_extractor_response,
    build_agenda_item_extraction_request,
    build_title_equivalence_request,
    ensure_agenda_item_extractor_contract,
    ensure_title_equivalence_contract,
    outline_adds_title_evidence,
    validate_agenda_item_extractor_response,
    validate_title_equivalence_response,
)
from citypods.compute.structured import response_model

AGENDA = """AGENDA
1. CALL TO ORDER
2. CONSENT AGENDA
A. ID 26-1108 Consider approval of appointing three members to the Committee on the Environment.
Attachment: Resolution
B. ID 26-1109 Consider approval of appointing three members to the Mobility Committee.
3. PUBLIC HEARINGS
"""


def test_direct_request_contains_numbered_full_source_lines_and_no_candidate_gate():
    request = build_agenda_item_extraction_request(agenda_text=AGENDA)

    assert request.model == "mistral/mistral-large-latest"
    assert request.source_line_count == 7
    assert '"line":4' in request.messages[1]["content"]
    assert '"candidates"' not in request.messages[1]["content"]
    assert "display_ref" in request.messages[0]["content"]
    assert "section heading" in request.messages[0]["content"]
    assert "Public Hearings" in request.messages[0]["content"]
    assert "Consent Agenda" in request.messages[0]["content"]
    assert "constituent items" in request.messages[0]["content"]
    assert "JSON object" in request.messages[0]["content"]
    assert "exactly one top-level key" in request.messages[0]["content"]
    assert request.input_tokens > 0
    with pytest.raises(ValueError, match="non-empty"):
        build_agenda_item_extraction_request(agenda_text="")


def test_direct_request_records_an_explicit_benchmark_model():
    request = build_agenda_item_extraction_request(
        agenda_text=AGENDA, model="mistral/mistral-medium-2508"
    )

    assert request.model == "mistral/mistral-medium-2508"


def test_direct_request_supplies_soft_hints_without_turning_them_into_a_gate():
    request = build_agenda_item_extraction_request(
        agenda_text=AGENDA,
        candidate_hints=[{"line_start": 4, "line_end": 4, "priority": "high"}],
    )

    assert '"candidate_hints"' in request.messages[1]["content"]
    assert "soft recall cues" in request.messages[0]["content"]
    assert "do not omit" in request.messages[0]["content"]
    with pytest.raises(ValueError, match="bounded"):
        build_agenda_item_extraction_request(
            agenda_text=AGENDA,
            candidate_hints=[{"line_start": 8, "line_end": 8, "priority": "high"}],
        )


def test_hierarchy_prompt_preserves_consent_composite_and_excludes_backup_materials():
    request = build_agenda_item_extraction_request(
        agenda_text=AGENDA, prompt_variant="hierarchy-first"
    )

    prompt = request.messages[0]["content"]
    assert "Consent Agenda is one composite action" in prompt
    assert "attachment lists, backup links" in prompt
    with pytest.raises(ValueError, match="unknown"):
        build_agenda_item_extraction_request(agenda_text=AGENDA, prompt_variant="unknown")


def test_format_aware_guard_compares_full_source_text_not_parser_candidates():
    assert not outline_adds_title_evidence(agenda_text=AGENDA, outline_text=AGENDA)
    assert outline_adds_title_evidence(
        agenda_text=AGENDA,
        outline_text="## CONSENT AGENDA\nID 26-1108\nID 26-1109\n",
    )


def test_extractor_contract_is_idempotent_and_accepts_line_bounded_verbatim_items():
    assert ensure_agenda_item_extractor_contract() is ensure_agenda_item_extractor_contract()
    assert response_model(AGENDA_ITEM_EXTRACTOR_CONTRACT) is ensure_agenda_item_extractor_contract()

    items = validate_agenda_item_extractor_response(
        json.dumps(
            {
                "items": [
                    {
                        "display_ref": "ID 26-1108",
                        "title": "Appoint environment committee members",
                        "evidence_quote": (
                            "Consider approval of appointing three members to the "
                            "Committee on the Environment."
                        ),
                        "line_start": 4,
                        "line_end": 4,
                    },
                    {
                        "display_ref": "ID 26-1109",
                        "title": "Appoint mobility committee members",
                        "evidence_quote": (
                            "Consider approval of appointing three members to the Mobility "
                            "Committee."
                        ),
                        "line_start": 6,
                        "line_end": 6,
                    },
                ]
            }
        ),
        agenda_text=AGENDA,
    )

    assert [item.display_ref for item in items] == ["ID 26-1108", "ID 26-1109"]
    assert items[0].line_start == items[0].line_end == 4


def test_extractor_tolerates_pdf_layout_spacing_without_accepting_a_paraphrase():
    layout_text = "A. ID 26-2000 Consult with the City  ’s attorneys about I -35E.\n"
    items = validate_agenda_item_extractor_response(
        json.dumps(
            {
                "items": [
                    {
                        "display_ref": "ID 26-2000",
                        "title": "Attorney consultation about I-35E",
                        "evidence_quote": "Consult with the City’s attorneys about I-35E.",
                        "line_start": 1,
                        "line_end": 1,
                    }
                ]
            }
        ),
        agenda_text=layout_text,
    )
    assert items[0].display_ref == "ID 26-2000"


def test_extractor_tolerates_pdf_page_furniture_between_visual_item_lines():
    layout_text = (
        "ID 26-2001 Consider approval of an appointment to the\n"
        "Page 6 Printed on 7/15/2026 City Council Meeting Agenda July 21, 2026\n"
        "Committee on the Environment.\n"
    )
    items = validate_agenda_item_extractor_response(
        json.dumps(
            {
                "items": [
                    {
                        "display_ref": "ID 26-2001",
                        "title": "Environment committee appointment",
                        "evidence_quote": (
                            "ID 26-2001 Consider approval of an appointment to the "
                            "Committee on the Environment."
                        ),
                        "line_start": 1,
                        "line_end": 3,
                    }
                ]
            }
        ),
        agenda_text=layout_text,
    )
    assert items[0].display_ref == "ID 26-2001"


def test_extractor_tolerates_number_letter_reference_spacing_from_layout_extraction():
    items = validate_agenda_item_extractor_response(
        json.dumps(
            {
                "items": [
                    {
                        "display_ref": "2.A",
                        "title": "Approve the agreement",
                        "evidence_quote": "2. A Consider approval of the agreement.",
                        "line_start": 1,
                        "line_end": 1,
                    }
                ]
            }
        ),
        agenda_text="2. A Consider approval of the agreement.\n",
    )
    assert items[0].display_ref == "2.A"


def test_extractor_expands_prefix_before_validating_display_ref():
    agenda_text = "ID 26-2002\nConsider approval of an appointment to the Committee on Parks.\n"
    items = validate_agenda_item_extractor_response(
        json.dumps(
            {
                "items": [
                    {
                        "display_ref": "ID 26-2002",
                        "title": "Parks committee appointment",
                        "evidence_quote": (
                            "Consider approval of an appointment to the Committee on Parks."
                        ),
                        "line_start": 2,
                        "line_end": 2,
                    }
                ]
            }
        ),
        agenda_text=agenda_text,
    )

    assert (items[0].line_start, items[0].line_end) == (1, 2)
    assert items[0].display_ref == "ID 26-2002"
    assert items[0].evidence_span_repaired


def test_extractor_derives_display_ref_after_prefix_expansion_when_model_omits_it():
    agenda_text = "B.\nApprove the revised mobility agreement.\n"
    items = validate_agenda_item_extractor_response(
        json.dumps(
            {
                "items": [
                    {
                        "display_ref": None,
                        "title": "Approve revised mobility agreement",
                        "evidence_quote": "Approve the revised mobility agreement.",
                        "line_start": 2,
                        "line_end": 2,
                    }
                ]
            }
        ),
        agenda_text=agenda_text,
    )

    assert (items[0].line_start, items[0].line_end) == (1, 2)
    assert items[0].display_ref == "B."
    assert items[0].evidence_span_repaired


def test_extractor_rejects_display_ref_only_after_expanded_span_is_checked():
    agenda_text = "ID 26-2003\nApprove the revised mobility agreement.\n"
    with pytest.raises(ValueError, match="display reference is absent"):
        validate_agenda_item_extractor_response(
            json.dumps(
                {
                    "items": [
                        {
                            "display_ref": "ID 26-9999",
                            "title": "Approve revised mobility agreement",
                            "evidence_quote": "Approve the revised mobility agreement.",
                            "line_start": 2,
                            "line_end": 2,
                        }
                    ]
                }
            ),
            agenda_text=agenda_text,
        )


def test_extractor_repairs_a_uniquely_exact_nearby_evidence_span():
    items = validate_agenda_item_extractor_response(
        json.dumps(
            {
                "items": [
                    {
                        "display_ref": "2.A",
                        "title": "Approve the agreement",
                        "evidence_quote": "2. A. Consider approval of the agreement.",
                        "line_start": 2,
                        "line_end": 3,
                    }
                ]
            }
        ),
        agenda_text="2.\nA. Consider approval of the agreement.\n\n",
    )
    assert (items[0].line_start, items[0].line_end) == (1, 2)
    assert items[0].evidence_span_repaired


@pytest.mark.parametrize(
    ("item", "message"),
    [
        (
            {
                "display_ref": "ID 26-1108",
                "title": "Environment committee appointments",
                "evidence_quote": "invented",
                "line_start": 4,
                "line_end": 4,
            },
            "evidence quote is absent",
        ),
        (
            {
                "display_ref": "ID 26-1108",
                "title": "Environment committee appointments",
                "evidence_quote": (
                    "Consider approval of appointing three members to the Committee on the "
                    "Environment."
                ),
                "line_start": 4,
                "line_end": 8,
            },
            "outside",
        ),
        (
            {
                "display_ref": "ID 26-1108",
                "title": "Environment committee appointments",
                "evidence_quote": (
                    "Consider approval of appointing three members to the Committee on the "
                    "Environment."
                ),
                "line_start": 4,
                "line_end": 4,
            },
            "duplicate",
        ),
    ],
)
def test_extractor_rejects_nonverbatim_or_invalid_source_evidence(item: dict, message: str):
    items = [item, item] if message == "duplicate" else [item]
    with pytest.raises(ValueError, match=message):
        validate_agenda_item_extractor_response(json.dumps({"items": items}), agenda_text=AGENDA)


def test_evidence_assessment_retains_valid_items_and_reports_invalid_ones():
    assessment = assess_agenda_item_extractor_response(
        json.dumps(
            {
                "items": [
                    {
                        "display_ref": "ID 26-1108",
                        "title": "Environment committee appointments",
                        "evidence_quote": (
                            "Consider approval of appointing three members to the "
                            "Committee on the Environment."
                        ),
                        "line_start": 4,
                        "line_end": 4,
                    },
                    {
                        "display_ref": "ID 26-1109",
                        "title": "Mobility committee appointments",
                        "evidence_quote": "invented",
                        "line_start": 6,
                        "line_end": 6,
                    },
                ]
            }
        ),
        agenda_text=AGENDA,
    )
    assert [item.display_ref for item in assessment.items] == ["ID 26-1108"]
    assert assessment.rejected[0].display_ref == "ID 26-1109"
    assert "evidence quote" in assessment.rejected[0].reason


def test_title_equivalence_contract_requires_one_to_one_action_matches():
    assert ensure_title_equivalence_contract() is ensure_title_equivalence_contract()
    assert response_model(TITLE_EQUIVALENCE_CONTRACT) is ensure_title_equivalence_contract()
    request = build_title_equivalence_request(
        canonical_titles=["CONSENT AGENDA", "Approval of bridge agreement"],
        generated_titles=["Approve bridge agreement"],
    )
    assert request.model == "mistral/mistral-large-latest"
    assert "semantic equivalence" in request.messages[0]["content"]
    assert "zero-based" in request.messages[0]["content"]
    result = validate_title_equivalence_response(
        json.dumps(
            {
                "canonical_action_indices": [1],
                "matches": [{"generated_index": 0, "canonical_index": 1}],
            }
        ),
        canonical_count=2,
        generated_count=1,
    )
    assert result.canonical_action_indices == (1,)
    assert result.matches[0].canonical_index == 1


def test_title_equivalence_accepts_only_unambiguously_one_based_judge_indexes():
    result = validate_title_equivalence_response(
        json.dumps(
            {
                "canonical_action_indices": [2],
                "matches": [{"generated_index": 1, "canonical_index": 2}],
            }
        ),
        canonical_count=2,
        generated_count=1,
    )
    assert result.canonical_action_indices == (1,)
    assert result.matches == (TitleEquivalenceMatch(generated_index=0, canonical_index=1),)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {"canonical_action_indices": [1, 1], "matches": []},
            "duplicate canonical",
        ),
        (
            {
                "canonical_action_indices": [0],
                "matches": [{"generated_index": 0, "canonical_index": 1}],
            },
            "non-action",
        ),
        (
            {
                "canonical_action_indices": [0],
                "matches": [
                    {"generated_index": 0, "canonical_index": 0},
                    {"generated_index": 0, "canonical_index": 0},
                ],
            },
            "one-to-one",
        ),
    ],
)
def test_title_equivalence_rejects_invalid_judge_output(payload: dict, message: str):
    with pytest.raises(ValueError, match=message):
        validate_title_equivalence_response(
            json.dumps(payload), canonical_count=2, generated_count=1
        )
