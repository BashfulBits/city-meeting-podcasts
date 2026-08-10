import pytest

from citypods.chapter_artifacts import (
    AgendaCandidate,
    AgendaCandidatesArtifact,
    BoundaryResultArtifact,
    LocatorUnitArtifact,
    artifact_key,
    recipe_hash,
)

# Short recipes (< 12 chars) are kept as-is; long ones are truncated to 12 chars.
_LONG_RECIPE = "a" * 64  # sha256 hex length


def test_locator_unit_artifact_round_trip():
    unit = LocatorUnitArtifact(id="u00001", start=12.5, end=24.0, text="Hello world")
    restored = LocatorUnitArtifact.from_dict(unit.to_dict())
    assert restored == unit


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (float("nan"), 10.0),
        (10.0, float("nan")),
        (float("nan"), float("nan")),
        (float("inf"), 10.0),
        (10.0, float("inf")),
        (float("-inf"), 10.0),
        (10.0, float("-inf")),
        (-1.0, 10.0),
        (15.0, 10.0),
    ],
)
def test_locator_unit_artifact_rejects_non_finite_and_invalid_timestamps(start: float, end: float):
    with pytest.raises(ValueError, match="invalid locator unit"):
        LocatorUnitArtifact(id="u00001", start=start, end=end, text="Sample text")


@pytest.mark.parametrize(
    "payload",
    [
        {"id": "u00001", "start": "nan", "end": 10.0, "text": "Sample text"},
        {"id": "u00001", "start": 10.0, "end": "inf", "text": "Sample text"},
        {"id": "u00001", "start": "-inf", "end": 10.0, "text": "Sample text"},
        {"id": "u00001", "start": "invalid", "end": 10.0, "text": "Sample text"},
        {"id": "", "start": 0.0, "end": 10.0, "text": "Sample text"},
        {"id": "u00001", "start": 0.0, "end": 10.0, "text": ""},
    ],
)
def test_locator_unit_artifact_from_dict_rejects_invalid_payloads(payload: dict):
    with pytest.raises(ValueError):
        LocatorUnitArtifact.from_dict(payload)


def test_artifact_key_truncates_long_recipe_to_12_chars():
    key = artifact_key("agenda/candidates", "episode/1", _LONG_RECIPE)
    assert key == "state/generated_chapters/agenda-candidates/episode-1-aaaaaaaaaaaa.json"


def test_recipe_hash_is_order_independent_for_mapping_parts():
    assert recipe_hash(episode_uid="e1", source_hash="a") == recipe_hash(
        source_hash="a", episode_uid="e1"
    )


def test_agenda_artifact_round_trip_preserves_source_evidence_and_cues():
    item = AgendaCandidate(
        index=0,
        title="Approve the budget",
        kind="substantive_action",
        line_start=4,
        line_end=6,
        evidence_text="ID 24-01\nApprove the budget",
        locator_cues=("ID 24-01", "Approve the budget"),
        display_ref="ID 24-01",
    )
    artifact = AgendaCandidatesArtifact(
        episode_uid="episode-1",
        source_hash="agenda-sha",
        model="mistral/mistral-medium-2508",
        prompt_version="agenda-flow",
        recipe="recipe-1",
        items=(item,),
    )
    restored = AgendaCandidatesArtifact.from_dict(artifact.to_dict())
    assert restored == artifact


def test_artifact_key_is_content_addressed_and_safe():
    assert artifact_key("agenda/candidates", "episode/1", "abc") == (
        "state/generated_chapters/agenda-candidates/episode-1-abc.json"
    )


def test_boundary_artifact_round_trip_keeps_diagnostics():
    artifact = BoundaryResultArtifact(
        episode_uid="episode-1",
        agenda_recipe="agenda-1",
        transcript_hash="transcript-1",
        model="gemini/gemini-3.5-flash-lite",
        prompt_version="locator-v1",
        recipe="boundary-1",
        anchors=({"agenda_item_index": 0, "unit_id": "u00001", "start": 12.0},),
        diagnostics={"hint_mode": "none"},
    )
    assert BoundaryResultArtifact.from_dict(artifact.to_dict()) == artifact
