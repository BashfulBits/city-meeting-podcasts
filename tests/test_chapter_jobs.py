import json

from citypods.chapter_artifacts import AgendaCandidate, AgendaCandidatesArtifact
from citypods.chapter_jobs import build_agenda_job, build_locator_job
from citypods.chapter_locator import LocatorUnit


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
    material = json.loads(job.inputs["messages"][1]["content"])
    assert len(material["transcript_units"]) == 1
