"""Offline contract tests for generated agenda-chapter request construction."""

from __future__ import annotations

import json

import pytest

from citypods.chapter_locator import (
    DEEPSEEK_FREE_CONTEXT_TOKENS,
    DEEPSEEK_FREE_LOCATOR_MODEL,
    GEMINI_CONTEXT_TOKENS,
    GEMINI_LOCATOR_MODEL,
    LOCATOR_CONTRACT,
    LOCATOR_OUTPUT_TOKEN_RESERVE,
    MISTRAL_CONTEXT_TOKENS,
    MISTRAL_LOCATOR_MODEL,
    NEMOTRON_CONTEXT_TOKENS,
    NEMOTRON_LOCATOR_MODEL,
    LocatorAgendaItem,
    build_locator_request,
    build_locator_units,
    ensure_locator_contract,
    locator_units_from_vtt,
    locator_units_from_words,
    select_locator_model,
    select_locator_models,
    validate_locator_response,
)
from citypods.compute.structured import response_model


def test_word_sidecar_yields_stable_segment_units():
    payload = {
        "schema": "2",
        "basis": "served",
        "segments": [
            {"start": 5, "end": 8.2, "text": " First agenda item. ", "words": []},
            {"start": 8.2, "end": 12, "text": "Discussion begins.", "words": []},
        ],
    }

    units = locator_units_from_words(json.dumps(payload).encode())

    assert [(unit.id, unit.start, unit.end, unit.text) for unit in units] == [
        ("u00001", 5.0, 8.2, "First agenda item."),
        ("u00002", 8.2, 12.0, "Discussion begins."),
    ]


def test_word_sidecar_discards_invalid_segments_without_guessing_timing():
    payload = {"segments": [{"start": -1, "end": 2, "text": "bad"}, {"text": "missing"}]}

    assert locator_units_from_words(json.dumps(payload).encode()) == []


def test_vtt_units_accept_cue_identifier_and_settings():
    vtt = b"""WEBVTT

7
00:01:02.300 --> 00:01:05.000 align:start
Next is Resolution 12.

00:01:05.000 --> 00:01:09.125
Council discussion.
"""

    units = locator_units_from_vtt(vtt)

    assert [(unit.id, unit.start, unit.end, unit.text) for unit in units] == [
        ("u00001", 62.3, 65.0, "Next is Resolution 12."),
        ("u00002", 65.0, 69.125, "Council discussion."),
    ]


def test_builder_prefers_words_then_falls_back_to_vtt():
    words = b'{"segments":[{"start":1,"end":2,"text":"Word sidecar"}]}'
    vtt = b"WEBVTT\n\n00:00:03.000 --> 00:00:04.000\nVTT fallback\n"

    units, source = build_locator_units(words_data=words, vtt_data=vtt)
    assert source == "words"
    assert [unit.text for unit in units] == ["Word sidecar"]

    units, source = build_locator_units(words_data=b"not json", vtt_data=vtt)
    assert source == "vtt"
    assert [unit.text for unit in units] == ["VTT fallback"]


def test_builder_returns_non_admission_for_untimed_or_malformed_data():
    assert build_locator_units(words_data=b"{}", vtt_data=b"WEBVTT\n\nnot a cue") == ([], None)


def test_locator_contract_is_registered_idempotently():
    assert ensure_locator_contract() is ensure_locator_contract()
    assert response_model(LOCATOR_CONTRACT) is ensure_locator_contract()


def test_response_maps_only_supplied_units_and_preserves_reordering():
    units = locator_units_from_vtt(
        b"WEBVTT\n\n00:00:10.000 --> 00:00:12.000\nSecond item\n\n"
        b"00:00:30.000 --> 00:00:34.000\nFirst item\n"
    )
    content = json.dumps(
        {
            "anchors": [
                {
                    "agenda_item_index": 1,
                    "unit_id": "u00001",
                    "transition_quote": "Second item",
                    "confidence": 0.8,
                    "rationale": "The chair announces it.",
                },
                {
                    "agenda_item_index": 0,
                    "unit_id": "u00002",
                    "transition_quote": "First item",
                    "confidence": 0.9,
                    "rationale": "It follows discussion.",
                },
            ]
        }
    )

    anchors = validate_locator_response(content, agenda_item_count=2, units=units)

    assert [anchor.agenda_item_index for anchor in anchors] == [1, 0]
    assert [anchor.unit.start for anchor in anchors] == [10.0, 30.0]


def test_response_preserves_research_confidence_decomposition_and_alternative():
    units = locator_units_from_vtt(
        b"WEBVTT\n\n00:00:10.000 --> 00:00:12.000\nFirst item\n\n"
        b"00:00:30.000 --> 00:00:34.000\nSecond item\n"
    )
    content = json.dumps(
        {
            "anchors": [
                {
                    "agenda_item_index": 0,
                    "unit_id": "u00001",
                    "transition_quote": "First item",
                    "confidence": 0.72,
                    "item_confidence": 0.9,
                    "boundary_confidence": 0.8,
                    "rationale": "The item number is announced.",
                    "evidence_type": "direct_item_number_or_id",
                    "alternative_agenda_item_index": 1,
                    "alternative_unit_id": "u00002",
                    "uncertainty_reason": "The second item follows shortly afterward.",
                }
            ]
        }
    )

    anchor = validate_locator_response(content, agenda_item_count=2, units=units)[0]

    assert anchor.confidence == 0.72
    assert anchor.item_confidence == 0.9
    assert anchor.boundary_confidence == 0.8
    assert anchor.evidence_type == "direct_item_number_or_id"
    assert anchor.alternative_agenda_item_index == 1
    assert anchor.alternative_unit_id == "u00002"
    assert anchor.uncertainty_reason.startswith("The second item")


@pytest.mark.parametrize(
    "anchors, error",
    [
        ([{"agenda_item_index": 2, "unit_id": "u00001"}], "out of range"),
        (
            [
                {"agenda_item_index": 0, "unit_id": "u00001"},
                {"agenda_item_index": 0, "unit_id": "u00002"},
            ],
            "duplicate agenda",
        ),
        ([{"agenda_item_index": 0, "unit_id": "u99999"}], "unknown locator"),
        (
            [
                {
                    "agenda_item_index": 0,
                    "unit_id": "u00001",
                    "alternative_unit_id": "u99999",
                }
            ],
            "unknown alternative locator",
        ),
    ],
)
def test_response_rejects_request_specific_invalid_anchors(anchors, error):
    units = locator_units_from_vtt(
        b"WEBVTT\n\n00:00:10.000 --> 00:00:12.000\nOne\n\n00:00:30.000 --> 00:00:34.000\nTwo\n"
    )
    payload = {
        "anchors": [
            {
                **anchor,
                "transition_quote": "A sufficient quote",
                "confidence": 0.8,
                "rationale": "Evidence.",
            }
            for anchor in anchors
        ]
    }

    with pytest.raises(ValueError, match=error):
        validate_locator_response(json.dumps(payload), agenda_item_count=2, units=units)


def test_response_rejects_anchors_that_collide_on_one_timestamp():
    units = locator_units_from_vtt(
        b"WEBVTT\n\n00:00:10.000 --> 00:00:12.000\nOne\n\n00:00:10.000 --> 00:00:14.000\nTwo\n"
    )
    payload = {
        "anchors": [
            {
                "agenda_item_index": index,
                "unit_id": unit.id,
                "transition_quote": "A sufficient quote",
                "confidence": 0.8,
                "rationale": "Evidence.",
            }
            for index, unit in enumerate(units)
        ]
    }

    with pytest.raises(ValueError, match="strictly increasing"):
        validate_locator_response(json.dumps(payload), agenda_item_count=2, units=units)


def test_request_contains_full_units_and_selects_mistral_by_default():
    units = locator_units_from_vtt(b"WEBVTT\n\n00:00:10.000 --> 00:00:12.000\nCall to order\n")

    request = build_locator_request(
        [
            LocatorAgendaItem(
                index=0,
                title="Call to order",
                display_ref="1.",
                evidence_text="1. Call to Order",
                locator_cues=("1.", "Call to Order"),
            )
        ],
        units,
    )

    assert request.model == MISTRAL_LOCATOR_MODEL
    assert request.models == (
        MISTRAL_LOCATOR_MODEL,
        DEEPSEEK_FREE_LOCATOR_MODEL,
        NEMOTRON_LOCATOR_MODEL,
    )
    assert request.input_tokens > 0
    material = json.loads(request.messages[1]["content"])
    assert material["agenda_items"] == [
        {
            "index": 0,
            "title": "Call to order",
            "display_ref": "1.",
            "evidence_text": "1. Call to Order",
            "locator_cues": ["1.", "Call to Order"],
        }
    ]
    assert material["transcript_units"] == [
        {"id": "u00001", "start": "00:00:10.000", "end": "00:00:12.000", "text": "Call to order"}
    ]


def test_request_can_carry_research_only_unit_provenance():
    units = locator_units_from_vtt(b"WEBVTT\n\n00:00:10.000 --> 00:00:12.000\nCall to order\n")
    request = build_locator_request(
        [LocatorAgendaItem(index=0, title="Call to order", evidence_text="1. Call to order")],
        units,
        unit_annotations={"u00001": {"candidate_for": [[0, {"L": 1}, "L"]]}},
    )

    material = json.loads(request.messages[1]["content"])
    assert material["transcript_units"][0]["retrieval_provenance"]["candidate_for"][0] == [
        0,
        {"L": 1},
        "L",
    ]
    assert "retrieval_provenance" in request.messages[0]["content"]


def test_context_routing_bands_escalate_through_every_free_tier_before_gemini():
    # Band 1: fits Mistral, the free DeepSeek V4 Flash tier, and Nemotron 3 Ultra alike.
    assert select_locator_models(DEEPSEEK_FREE_CONTEXT_TOKENS - LOCATOR_OUTPUT_TOKEN_RESERVE) == (
        MISTRAL_LOCATOR_MODEL,
        DEEPSEEK_FREE_LOCATOR_MODEL,
        NEMOTRON_LOCATOR_MODEL,
    )
    # Band 2: over the free DeepSeek tier's budget but still within Mistral's and Nemotron's.
    assert select_locator_models(
        DEEPSEEK_FREE_CONTEXT_TOKENS - LOCATOR_OUTPUT_TOKEN_RESERVE + 1
    ) == (MISTRAL_LOCATOR_MODEL, NEMOTRON_LOCATOR_MODEL)
    assert select_locator_models(MISTRAL_CONTEXT_TOKENS - LOCATOR_OUTPUT_TOKEN_RESERVE) == (
        MISTRAL_LOCATOR_MODEL,
        NEMOTRON_LOCATOR_MODEL,
    )
    # Band 3: over Mistral's budget too -- only the huge free Nemotron tier still fits.
    assert select_locator_models(MISTRAL_CONTEXT_TOKENS - LOCATOR_OUTPUT_TOKEN_RESERVE + 1) == (
        NEMOTRON_LOCATOR_MODEL,
    )
    assert select_locator_models(NEMOTRON_CONTEXT_TOKENS - LOCATOR_OUTPUT_TOKEN_RESERVE) == (
        NEMOTRON_LOCATOR_MODEL,
    )
    # Band 4: over every free tier's budget -- last-resort escalation to Gemini.
    assert select_locator_models(NEMOTRON_CONTEXT_TOKENS - LOCATOR_OUTPUT_TOKEN_RESERVE + 1) == (
        GEMINI_LOCATOR_MODEL,
    )
    with pytest.raises(ValueError, match="Gemini context"):
        select_locator_models(GEMINI_CONTEXT_TOKENS - LOCATOR_OUTPUT_TOKEN_RESERVE + 1)


def test_select_locator_model_returns_the_primary_band_candidate():
    """Backward-compatible single-model selector: first entry of select_locator_models."""
    assert (
        select_locator_model(MISTRAL_CONTEXT_TOKENS - LOCATOR_OUTPUT_TOKEN_RESERVE)
        == MISTRAL_LOCATOR_MODEL
    )
    assert (
        select_locator_model(MISTRAL_CONTEXT_TOKENS - LOCATOR_OUTPUT_TOKEN_RESERVE + 1)
        == NEMOTRON_LOCATOR_MODEL
    )
    assert (
        select_locator_model(NEMOTRON_CONTEXT_TOKENS - LOCATOR_OUTPUT_TOKEN_RESERVE + 1)
        == GEMINI_LOCATOR_MODEL
    )
    with pytest.raises(ValueError, match="Gemini context"):
        select_locator_model(GEMINI_CONTEXT_TOKENS - LOCATOR_OUTPUT_TOKEN_RESERVE + 1)


def test_request_rejects_ambiguous_or_empty_inputs():
    unit = locator_units_from_vtt(b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nText\n")[0]
    with pytest.raises(ValueError, match="at least one agenda"):
        build_locator_request([], [unit])
    with pytest.raises(ValueError, match="unit IDs"):
        build_locator_request(
            [LocatorAgendaItem(index=0, title="Item", evidence_text="1. Item")], [unit, unit]
        )
    with pytest.raises(ValueError, match="evidence text"):
        build_locator_request([LocatorAgendaItem(index=0, title="Item", evidence_text="")], [unit])
