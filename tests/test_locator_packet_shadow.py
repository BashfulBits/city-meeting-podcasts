"""Tests for the read-only locator packet shadow response repairs."""

from citypods.chapter_locator import LocatorRequest
from scripts.research.agenda_chapters.run_locator_packet_shadow import (
    _duplicate_unit_ids,
    _duplicate_unit_repair_request,
)


def test_duplicate_unit_repair_keeps_contract_and_adds_one_instruction():
    request = LocatorRequest(
        messages=(
            {"role": "system", "content": "Locate starts."},
            {"role": "user", "content": "{}"},
        ),
        model="mistral/mistral-large-latest",
        input_tokens=10,
    )

    repaired = _duplicate_unit_repair_request(request)

    assert repaired.model == request.model
    assert repaired.messages[:2] == request.messages
    assert len(repaired.messages) == 3
    assert "at most one agenda item" in repaired.messages[-1]["content"]
    assert repaired.input_tokens > request.input_tokens


def test_duplicate_unit_repair_names_conflicting_ids():
    content = '{"unit_id":"u00001"},{"unit_id":"u00001"},{"unit_id":"u00002"}'

    assert _duplicate_unit_ids(content) == ("u00001",)
