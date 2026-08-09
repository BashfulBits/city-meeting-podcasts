from citypods.chapter_artifacts import (
    AgendaCandidate,
    AgendaCandidatesArtifact,
    BoundaryResultArtifact,
    artifact_key,
    recipe_hash,
)


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
