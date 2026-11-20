"""Offline contract tests for generated agenda-chapter request construction."""

from __future__ import annotations

import json

import pytest

import citypods.chapter_locator as chapter_locator
from citypods.chapter_locator import (
    DEEPSEEK_FREE_CONTEXT_TOKENS,
    DEEPSEEK_FREE_LOCATOR_MODEL,
    DEEPSEEK_V4_FLASH_SAFE_INPUT_TOKENS,
    DEEPSEEK_V41_LOCATOR_MODEL,
    GEMINI_CONTEXT_TOKENS,
    GEMINI_LOCATOR_MODEL,
    LOCATOR_CONTRACT,
    LOCATOR_OUTPUT_TOKEN_RESERVE,
    LOCATOR_OVERFLOW_MAX_INPUT_TOKENS,
    MINIMAX_LOCATOR_MODEL,
    MISTRAL_CONTEXT_TOKENS,
    MISTRAL_LOCATOR_MODEL,
    NEMOTRON_CONTEXT_TOKENS,
    NEMOTRON_LOCATOR_MODEL,
    PRODUCTION_LOCATOR_MODEL,
    DuplicateLocatorUnitError,
    LocatorAgendaItem,
    LocatorInputOverCapError,
    LocatorUnit,
    build_locator_request,
    build_locator_units,
    build_production_locator_request,
    ensure_locator_contract,
    locator_units_from_vtt,
    locator_units_from_words,
    select_locator_model,
    select_locator_models,
    select_production_locator_models,
    validate_locator_response,
    validate_locator_response_partial,
)
from citypods.compute.llm_lanes import lane_for
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

    with pytest.raises(ValueError, match="share a start time"):
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


def _production_inputs(unit_count: int):
    item = LocatorAgendaItem(
        index=0,
        title="Call to order",
        display_ref="1.",
        evidence_text="1. Call to Order",
        locator_cues=("1.",),
    )
    units = [
        LocatorUnit(id=f"u{i}", start=float(i), end=float(i + 1), text="word " * 60)
        for i in range(unit_count)
    ]
    return [item], units


def _production_request(unit_count: int):
    return build_production_locator_request(*_production_inputs(unit_count))


def test_production_locator_pins_small_packets_to_v4_flash_alone():
    request = _production_request(50)

    assert request.models == (PRODUCTION_LOCATOR_MODEL,)
    assert request.model == PRODUCTION_LOCATOR_MODEL
    # The V4 Flash band keeps the full output reserve it has always sent.
    assert request.max_output_tokens == LOCATOR_OUTPUT_TOKEN_RESERVE


def test_overflow_cap_is_derived_from_the_minimax_route():
    # The cap follows config/provider_limits.yml, not a constant: the largest admitted input
    # (hard_input_ceiling, inside the context window less the output reserve) divided by the
    # worst measured provider/estimate ratio. 2026-11-19 probe: 1,034,675 / 1.59 = 650,738.
    from citypods.chapter_locator import (
        locator_max_output_tokens,
        locator_overflow_max_input_tokens,
    )
    from citypods.compute.llm_policy import ROUTE_CANDIDATES, canonical_model

    assert DEEPSEEK_V4_FLASH_SAFE_INPUT_TOKENS == 76_000
    (route,) = ROUTE_CANDIDATES[canonical_model(MINIMAX_LOCATOR_MODEL)]
    room = min(
        route.hard_input_ceiling,
        route.input_context_limit - locator_max_output_tokens(MINIMAX_LOCATOR_MODEL),
    )
    assert LOCATOR_OVERFLOW_MAX_INPUT_TOKENS == locator_overflow_max_input_tokens()
    assert LOCATOR_OVERFLOW_MAX_INPUT_TOKENS == int(room / route.input_token_ratio)
    assert LOCATOR_OVERFLOW_MAX_INPUT_TOKENS == 650_738


def test_overflow_cap_moves_with_the_route_and_is_zero_without_one(monkeypatch):
    import dataclasses

    from citypods import chapter_locator
    from citypods.compute import llm_policy

    key = llm_policy.canonical_model(MINIMAX_LOCATOR_MODEL)
    (route,) = llm_policy.ROUTE_CANDIDATES[key]
    smaller = dataclasses.replace(route, hard_input_ceiling=318_000, input_token_ratio=1.59)
    monkeypatch.setitem(llm_policy.ROUTE_CANDIDATES, key, (smaller,))
    assert chapter_locator.locator_overflow_max_input_tokens() == 200_000
    monkeypatch.setitem(llm_policy.ROUTE_CANDIDATES, key, ())
    assert chapter_locator.locator_overflow_max_input_tokens() == 0


@pytest.mark.parametrize(
    ("input_tokens", "expected"),
    [
        (0, PRODUCTION_LOCATOR_MODEL),
        (DEEPSEEK_V4_FLASH_SAFE_INPUT_TOKENS, PRODUCTION_LOCATOR_MODEL),
        (DEEPSEEK_V4_FLASH_SAFE_INPUT_TOKENS + 1, MINIMAX_LOCATOR_MODEL),
        (LOCATOR_OVERFLOW_MAX_INPUT_TOKENS, MINIMAX_LOCATOR_MODEL),
    ],
)
def test_production_routing_boundaries(input_tokens, expected):
    assert select_production_locator_models(input_tokens) == (expected,)


def test_production_routing_holds_packets_above_the_cap():
    with pytest.raises(LocatorInputOverCapError) as raised:
        select_production_locator_models(LOCATOR_OVERFLOW_MAX_INPUT_TOKENS + 1)
    assert raised.value.input_tokens == LOCATOR_OVERFLOW_MAX_INPUT_TOKENS + 1
    assert raised.value.cap == LOCATOR_OVERFLOW_MAX_INPUT_TOKENS
    with pytest.raises(ValueError, match="non-negative"):
        select_production_locator_models(-1)


def test_raising_the_cap_makes_a_held_packet_eligible(monkeypatch):
    size = LOCATOR_OVERFLOW_MAX_INPUT_TOKENS + 1
    with pytest.raises(LocatorInputOverCapError):
        select_production_locator_models(size)
    monkeypatch.setattr(chapter_locator, "LOCATOR_OVERFLOW_MAX_INPUT_TOKENS", size)
    assert select_production_locator_models(size) == (MINIMAX_LOCATOR_MODEL,)


def test_production_locator_sends_large_packets_to_minimax():
    request = _production_request(1200)

    assert request.input_tokens > DEEPSEEK_V4_FLASH_SAFE_INPUT_TOKENS
    # The same estimate_tokens measure build_locator_request reports.
    assert request.input_tokens == build_locator_request(*_production_inputs(1200)).input_tokens
    assert request.models == (MINIMAX_LOCATOR_MODEL,)
    assert request.model == MINIMAX_LOCATOR_MODEL
    assert set(request.models) <= set(lane_for("chapter-locator").models)
    assert DEEPSEEK_V41_LOCATOR_MODEL not in lane_for("chapter-locator").models


def test_minimax_request_output_fits_its_route():
    from citypods.compute.llm_policy import ROUTE_CANDIDATES

    request = _production_request(1200)
    (route,) = ROUTE_CANDIDATES[MINIMAX_LOCATOR_MODEL]
    assert route.route_id == "nvidia_minimaxai_minimax_m3_1_flash"
    assert route.output_context_limit == 12_800
    assert request.max_output_tokens == route.output_context_limit


def test_production_locator_holds_packets_above_the_cap_unbuilt(monkeypatch):
    items, units = _production_inputs(1200)
    size = build_locator_request(items, units).input_tokens
    monkeypatch.setattr(chapter_locator, "LOCATOR_OVERFLOW_MAX_INPUT_TOKENS", size - 1)
    with pytest.raises(LocatorInputOverCapError) as raised:
        build_production_locator_request(items, units)
    assert raised.value.input_tokens == size


def test_minimax_route_admits_a_cap_sized_packet():
    """Every Python-visible size limit on the route holds a packet at the cap.

    The Worker scales the chars/4 estimate by the route's ratio and needs the scaled input plus
    the job's max_tokens inside input_context_limit, and the scaled input inside
    hard_input_ceiling (workers/llm-dispatch-v2/test/locator_overflow_route.test.js asserts the
    same through the Worker's own functions).
    """
    from citypods.compute.llm_policy import ROUTE_CANDIDATES, route_input_tokens

    (route,) = ROUTE_CANDIDATES[MINIMAX_LOCATOR_MODEL]
    scaled = route_input_tokens(LOCATOR_OVERFLOW_MAX_INPUT_TOKENS, route)
    assert route.input_token_ratio == pytest.approx(1.59)
    assert route.hard_input_ceiling == 1_034_675  # 2026-11-19 probe
    assert route.input_context_limit == 1_048_576  # NVIDIA's reported window
    assert scaled <= route.hard_input_ceiling
    assert scaled + route.output_context_limit <= route.input_context_limit


def test_minimax_route_shapes_the_locator_contract_as_json_schema():
    from citypods.compute.llm_policy import ROUTE_CANDIDATES
    from citypods.compute.structured_shaping import shape_for_route

    (route,) = ROUTE_CANDIDATES[MINIMAX_LOCATOR_MODEL]
    contract = ensure_locator_contract()
    request = _production_request(50)
    messages = [dict(message) for message in request.messages]
    shaped, response_format = shape_for_route(
        messages,
        name=contract.__name__,
        schema=contract.model_json_schema(),
        route=route,
    )
    assert shaped == messages
    assert response_format == {
        "type": "json_schema",
        "json_schema": {"name": contract.__name__, "schema": contract.model_json_schema()},
    }


def _anchor(index: int, unit_id: str, quote: str = "quote here") -> dict:
    return {
        "agenda_item_index": index,
        "unit_id": unit_id,
        "transition_quote": quote,
        "confidence": 0.9,
        "rationale": "because",
    }


_THREE_UNITS = (
    b"WEBVTT\n\n00:00:00.000 --> 00:00:05.000\nCall to order\n\n"
    b"00:00:10.000 --> 00:00:15.000\nItem two\n\n00:00:20.000 --> 00:00:25.000\nItem three\n"
)


def test_strict_validation_raises_a_typed_error_naming_the_collision():
    units = locator_units_from_vtt(_THREE_UNITS)
    content = json.dumps({"anchors": [_anchor(0, "u00001"), _anchor(1, "u00001")]})

    with pytest.raises(DuplicateLocatorUnitError, match="duplicate locator unit: u00001") as info:
        validate_locator_response(content, agenda_item_count=3, units=units)

    assert info.value.unit_id == "u00001"
    assert info.value.agenda_item_indices == (0, 1)


def test_partial_validation_sets_colliding_items_aside_and_keeps_the_rest():
    units = locator_units_from_vtt(_THREE_UNITS)
    content = json.dumps(
        {"anchors": [_anchor(0, "u00001"), _anchor(1, "u00001"), _anchor(2, "u00003")]}
    )

    result = validate_locator_response_partial(content, agenda_item_count=3, units=units)

    assert [anchor.agenda_item_index for anchor in result.anchors] == [2]
    assert len(result.conflicts) == 1
    assert result.conflicts[0].unit.id == "u00001"
    assert result.conflicts[0].agenda_item_indices == (0, 1)


def test_partial_validation_without_collisions_matches_strict():
    units = locator_units_from_vtt(_THREE_UNITS)
    content = json.dumps({"anchors": [_anchor(0, "u00001"), _anchor(2, "u00003")]})

    partial = validate_locator_response_partial(content, agenda_item_count=3, units=units)

    assert partial.conflicts == ()
    assert partial.anchors == validate_locator_response(content, agenda_item_count=3, units=units)


def test_partial_validation_still_rejects_other_defects():
    units = locator_units_from_vtt(_THREE_UNITS)
    content = json.dumps({"anchors": [_anchor(0, "u00001"), _anchor(0, "u00003")]})

    with pytest.raises(ValueError, match="duplicate agenda item index"):
        validate_locator_response_partial(content, agenda_item_count=3, units=units)
