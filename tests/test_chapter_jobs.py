import json

import pytest

from citypods.chapter_artifacts import AgendaCandidate, AgendaCandidatesArtifact
from citypods.chapter_jobs import (
    build_agenda_job,
    build_locator_job,
    finalize_agenda_job,
    finalize_locator_job,
)
from citypods.chapter_locator import LocatorUnit
from citypods.compute.base import JobResult


def test_agenda_job_is_pinned_and_idempotent():
    first = build_agenda_job(
        episode_uid="e1", agenda_text="1. Approve the budget", agenda_source_hash="sha"
    )
    second = build_agenda_job(
        episode_uid="e1", agenda_text="1. Approve the budget", agenda_source_hash="sha"
    )
    assert first.task == "agenda-item-extract"
    assert first.recipe_hash == second.recipe_hash
    assert first.inputs["structured_output"] == "agenda-chapter-item-extract"
    assert first.inputs["llm_policy"].allowed_models == ("mistral/mistral-medium-2508",)
    assert first.inputs["llm_policy"].queue_only is True
    assert first.inputs["llm_policy"].deadline_at is None


def test_agenda_job_recipe_changes_with_candidate_hints():
    first = build_agenda_job(
        episode_uid="e1", agenda_text="1. Approve the budget", agenda_source_hash="sha"
    )
    second = build_agenda_job(
        episode_uid="e1",
        agenda_text="1. Approve the budget",
        agenda_source_hash="sha",
        candidate_hints=[{"line_start": 1, "line_end": 1, "priority": "high"}],
    )
    third = build_agenda_job(
        episode_uid="e1",
        agenda_text="1. Approve the budget",
        agenda_source_hash="sha",
        candidate_hints=[{"line_start": 1, "line_end": 1, "priority": "low"}],
    )
    assert first.recipe_hash != second.recipe_hash
    assert second.recipe_hash != third.recipe_hash


def test_locator_job_keeps_all_units_and_uses_gemini_lite():
    agenda = AgendaCandidatesArtifact(
        episode_uid="e1",
        source_hash="agenda-sha",
        model="mistral/mistral-medium-2508",
        prompt_version="agenda-flow",
        recipe="agenda-recipe",
        items=(
            AgendaCandidate(
                index=0,
                title="Approve the budget",
                kind="substantive_action",
                line_start=1,
                line_end=1,
                evidence_text="ID 1 Approve the budget",
                locator_cues=("ID 1", "Approve the budget"),
                display_ref="ID 1",
            ),
        ),
    )
    job = build_locator_job(
        episode_uid="e1",
        agenda=agenda,
        transcript_hash="transcript-sha",
        units=[LocatorUnit(id="u00001", start=1, end=2, text="ID 1")],
    )
    assert job.task == "agenda-chapter-locate"
    assert job.inputs["llm_policy"].allowed_models == ("gemini/gemini-3.5-flash-lite",)
    assert job.inputs["llm_policy"].queue_only is True
    assert job.inputs["llm_policy"].deadline_at is None
    material = json.loads(job.inputs["messages"][1]["content"])
    assert len(material["transcript_units"]) == 1


def test_locator_job_recipe_changes_with_unit_annotations():
    agenda = AgendaCandidatesArtifact(
        episode_uid="e1",
        source_hash="agenda-sha",
        model="mistral/mistral-medium-2508",
        prompt_version="agenda-flow",
        recipe="agenda-recipe",
        items=(
            AgendaCandidate(
                index=0,
                title="Approve the budget",
                kind="substantive_action",
                line_start=1,
                line_end=1,
                evidence_text="ID 1 Approve the budget",
                locator_cues=("ID 1", "Approve the budget"),
                display_ref="ID 1",
            ),
        ),
    )
    units = [LocatorUnit(id="u00001", start=1.0, end=2.0, text="ID 1")]
    first = build_locator_job(
        episode_uid="e1", agenda=agenda, transcript_hash="transcript-sha", units=units
    )
    second = build_locator_job(
        episode_uid="e1",
        agenda=agenda,
        transcript_hash="transcript-sha",
        units=units,
        unit_annotations={"u00001": {"score": 0.95}},
    )
    third = build_locator_job(
        episode_uid="e1",
        agenda=agenda,
        transcript_hash="transcript-sha",
        units=units,
        unit_annotations={"u00001": {"score": 0.50}},
    )
    assert first.recipe_hash != second.recipe_hash
    assert second.recipe_hash != third.recipe_hash


def test_finalize_agenda_job_valid_and_invalid_responses():
    agenda_text = "Item 1. Call to order\nItem 2. Public hearing on rezoning"
    valid_content = json.dumps(
        {
            "items": [
                {
                    "display_ref": "Item 1",
                    "title": "Call to order",
                    "evidence_quote": "Item 1. Call to order",
                    "line_start": 1,
                    "line_end": 1,
                },
                {
                    "display_ref": "Item 2",
                    "title": "Public hearing on rezoning",
                    "evidence_quote": "Item 2. Public hearing on rezoning",
                    "line_start": 2,
                    "line_end": 2,
                },
            ]
        }
    )
    result = JobResult(
        task="agenda-item-extract",
        recipe_hash="recipe-agenda-123",
        output={"choices": [{"message": {"content": valid_content}}]},
    )
    artifact = finalize_agenda_job(
        result,
        episode_uid="ep-1",
        agenda_text=agenda_text,
        agenda_source_hash="agenda-sha-1",
    )
    assert artifact.episode_uid == "ep-1"
    assert artifact.recipe == "recipe-agenda-123"
    assert len(artifact.items) == 2
    assert artifact.items[0].display_ref == "Item 1"
    assert artifact.items[0].title == "Call to order"
    assert artifact.items[0].line_start == 1
    assert artifact.items[0].line_end == 1
    assert artifact.items[0].kind == "substantive_action"
    assert artifact.diagnostics == {"source_line_count": 2}

    # Malformed JSON raises ValueError
    invalid_json_result = JobResult(
        task="agenda-item-extract",
        recipe_hash="r",
        output={"choices": [{"message": {"content": "not-json"}}]},
    )
    with pytest.raises(ValueError):
        finalize_agenda_job(
            invalid_json_result,
            episode_uid="ep-1",
            agenda_text=agenda_text,
            agenda_source_hash="agenda-sha-1",
        )

    # Line start/end out of range raises ValueError
    out_of_range_result = JobResult(
        task="agenda-item-extract",
        recipe_hash="r",
        output={
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "items": [
                                    {
                                        "title": "Out of range",
                                        "evidence_quote": "No evidence",
                                        "line_start": 99,
                                        "line_end": 100,
                                    }
                                ]
                            }
                        )
                    }
                }
            ]
        },
    )
    with pytest.raises(ValueError):
        finalize_agenda_job(
            out_of_range_result,
            episode_uid="ep-1",
            agenda_text=agenda_text,
            agenda_source_hash="agenda-sha-1",
        )


def test_finalize_locator_job_valid_and_invalid_responses():
    agenda = AgendaCandidatesArtifact(
        episode_uid="ep-1",
        source_hash="agenda-sha",
        model="mistral/mistral-medium-2508",
        prompt_version="agenda-flow",
        recipe="agenda-recipe-1",
        items=(
            AgendaCandidate(
                index=0,
                title="Call to order",
                kind="substantive_action",
                line_start=1,
                line_end=1,
                evidence_text="Item 1. Call to order",
                locator_cues=("Item 1", "Call to order"),
                display_ref="Item 1",
            ),
            AgendaCandidate(
                index=1,
                title="Public hearing",
                kind="substantive_action",
                line_start=2,
                line_end=2,
                evidence_text="Item 2. Public hearing",
                locator_cues=("Item 2", "Public hearing"),
                display_ref="Item 2",
            ),
        ),
    )
    units = [
        LocatorUnit(id="u00001", start=10.0, end=20.0, text="Meeting called to order"),
        LocatorUnit(id="u00002", start=50.0, end=75.0, text="Opening the public hearing"),
    ]
    valid_content = json.dumps(
        {
            "anchors": [
                {
                    "agenda_item_index": 0,
                    "unit_id": "u00001",
                    "transition_quote": "Meeting called to order",
                    "confidence": 0.95,
                    "rationale": "Chair calls meeting to order",
                },
                {
                    "agenda_item_index": 1,
                    "unit_id": "u00002",
                    "transition_quote": "Opening the public hearing",
                    "confidence": 0.90,
                    "rationale": "Hearing opens",
                },
            ]
        }
    )
    result = JobResult(
        task="agenda-chapter-locate",
        recipe_hash="boundary-recipe-456",
        output={"choices": [{"message": {"content": valid_content}}]},
    )
    boundary = finalize_locator_job(
        result,
        episode_uid="ep-1",
        agenda=agenda,
        transcript_hash="trans-sha",
        units=units,
    )
    assert boundary.episode_uid == "ep-1"
    assert boundary.agenda_recipe == "agenda-recipe-1"
    assert boundary.recipe == "boundary-recipe-456"
    assert len(boundary.anchors) == 2
    assert boundary.anchors[0]["unit_id"] == "u00001"
    assert boundary.anchors[0]["start"] == 10.0
    assert boundary.anchors[0]["basis"] == "served"
    assert boundary.anchors[1]["unit_id"] == "u00002"
    assert boundary.anchors[1]["start"] == 50.0
    assert boundary.diagnostics == {"unit_count": 2, "agenda_item_count": 2}

    # Unknown unit ID raises ValueError
    unknown_unit_result = JobResult(
        task="agenda-chapter-locate",
        recipe_hash="r",
        output={
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "anchors": [
                                    {
                                        "agenda_item_index": 0,
                                        "unit_id": "u99999",
                                        "transition_quote": "bad quote",
                                        "confidence": 0.5,
                                        "rationale": "bad",
                                    }
                                ]
                            }
                        )
                    }
                }
            ]
        },
    )
    with pytest.raises(ValueError, match="unknown locator unit"):
        finalize_locator_job(
            unknown_unit_result,
            episode_uid="ep-1",
            agenda=agenda,
            transcript_hash="trans-sha",
            units=units,
        )

    # Non-monotonic / duplicate unit raises ValueError
    duplicate_unit_result = JobResult(
        task="agenda-chapter-locate",
        recipe_hash="r",
        output={
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "anchors": [
                                    {
                                        "agenda_item_index": 0,
                                        "unit_id": "u00001",
                                        "transition_quote": "Meeting called to order",
                                        "confidence": 0.95,
                                        "rationale": "Chair calls meeting to order",
                                    },
                                    {
                                        "agenda_item_index": 1,
                                        "unit_id": "u00001",
                                        "transition_quote": "Meeting called to order",
                                        "confidence": 0.95,
                                        "rationale": "Chair calls meeting to order",
                                    },
                                ]
                            }
                        )
                    }
                }
            ]
        },
    )
    with pytest.raises(ValueError, match="duplicate locator unit"):
        finalize_locator_job(
            duplicate_unit_result,
            episode_uid="ep-1",
            agenda=agenda,
            transcript_hash="trans-sha",
            units=units,
        )
