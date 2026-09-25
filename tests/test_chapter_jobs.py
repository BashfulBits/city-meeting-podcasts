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
    # A same-priority pool; Nemotron stays first because it alone is in the recipe hash.
    assert first.inputs["llm_policy"].allowed_models == (
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "tencent/hy3",
        "gemini/gemini-3.1-flash-lite",
    )
    # Repeated as backups only for the Worker's extended retry budget (see site_config.yml).
    assert first.inputs["llm_policy"].backup_models == (
        "tencent/hy3",
        "gemini/gemini-3.1-flash-lite",
    )
    assert first.inputs["llm_policy"].backup_after_attempts == 12
    assert first.inputs["llm_policy"].queue_only is True
    assert first.inputs["llm_policy"].deadline_at is None


def test_agenda_job_requests_the_benchmarked_output_token_budget():
    """The 30-episode benchmark that qualified Nemotron ran at max_tokens=32768 (97% valid JSON);
    without an explicit budget the job fell back to LiteLLMBackend's generic 1024-token default,
    truncating most multi-item agenda extractions mid-JSON."""
    from citypods.chapter_titles import AGENDA_OUTPUT_TOKEN_BUDGET

    job = build_agenda_job(
        episode_uid="e1", agenda_text="1. Approve the budget", agenda_source_hash="sha"
    )
    assert job.inputs["max_tokens"] == AGENDA_OUTPUT_TOKEN_BUDGET
    assert job.inputs["max_tokens"] > 1024


def test_agenda_job_recipe_changes_with_pipeline_version():
    # pipeline_version feeds the recipe hash so a pipeline-version bump (a validation/
    # post-processing behavior change independent of the model) re-queues the catalog exactly
    # like a model change does -- see AgendaChapterCandidatesStage.process()'s currency check.
    first = build_agenda_job(
        episode_uid="e1",
        agenda_text="1. Approve the budget",
        agenda_source_hash="sha",
        pipeline_version="1",
    )
    second = build_agenda_job(
        episode_uid="e1",
        agenda_text="1. Approve the budget",
        agenda_source_hash="sha",
        pipeline_version="2",
    )
    assert first.recipe_hash != second.recipe_hash


def test_finalize_agenda_job_records_the_actually_dispatched_model():
    """AGENDA_PRODUCTION_MODELS (R13) offers same-priority alternates; the artifact must record
    whichever one the scheduler actually reserved (result.model), not the label constant."""
    valid_content = json.dumps(
        {
            "items": [
                {
                    "display_ref": "Item 1",
                    "title": "Call to order",
                    "evidence_quote": "Item 1. Call to order",
                    "line_start": 1,
                    "line_end": 1,
                }
            ]
        }
    )
    result = JobResult(
        task="agenda-item-extract",
        recipe_hash="recipe-agenda-456",
        output={"choices": [{"message": {"content": valid_content}}]},
        model="meta-llama/llama-3.3-70b-instruct",
    )
    artifact = finalize_agenda_job(
        result,
        episode_uid="ep-1",
        agenda_text="Item 1. Call to order",
        agenda_source_hash="agenda-sha-1",
    )
    assert artifact.model == "meta-llama/llama-3.3-70b-instruct"


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
    # select_locator_models() already reserves LOCATOR_OUTPUT_TOKEN_RESERVE tokens of output room
    # when fitting a request into a route's context window; the dispatched request must actually
    # request that much, not the bare 1024-token LiteLLMBackend default.
    from citypods.chapter_locator import LOCATOR_OUTPUT_TOKEN_RESERVE

    assert job.inputs["max_tokens"] == LOCATOR_OUTPUT_TOKEN_RESERVE
    assert job.inputs["max_tokens"] > 1024


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
        pipeline_version="2",
    )
    assert artifact.episode_uid == "ep-1"
    assert artifact.recipe == "recipe-agenda-123"
    # No result.model set (e.g. a legacy/non-policy-driven backend) falls back to the label model.
    assert artifact.model == "nvidia/nemotron-3-ultra-550b-a55b:free"
    assert artifact.pipeline_version == "2"
    assert len(artifact.items) == 2
    assert artifact.items[0].display_ref == "Item 1"
    assert artifact.items[0].title == "Call to order"
    assert artifact.items[0].line_start == 1
    assert artifact.items[0].line_end == 1
    assert artifact.items[0].kind == "substantive_action"
    assert artifact.items[0].source == "strict"
    assert artifact.diagnostics == {
        "source_line_count": 2,
        "recovered_item_count": 0,
        "duplicate_item_count": 0,
        "dropped_item_count": 0,
        "dropped_item_reasons": [],
    }

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


def test_finalize_agenda_job_merges_recovered_items_with_valid_ones():
    """The GH#1078 recovery-shadow layer (recover_agenda_item_extractor_response) is wired into
    production finalize_agenda_job: a strictly-valid item and a recoverable one (a display_ref
    that doesn't literally validate but whose evidence a source-only search confirms) must both
    appear in the artifact, with the recovered one tagged source="recovery"."""
    agenda_text = "Item 1. Call to order\nA.\nDiscussion of the annual report."
    content = json.dumps(
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
                    "display_ref": "Friendly label",
                    "title": "Annual report discussion",
                    "evidence_quote": "Discussion of the annual report.",
                    "line_start": 3,
                    "line_end": 3,
                },
            ]
        }
    )
    result = JobResult(
        task="agenda-item-extract",
        recipe_hash="recipe-agenda-recovery",
        output={"choices": [{"message": {"content": content}}]},
    )
    artifact = finalize_agenda_job(
        result,
        episode_uid="ep-1",
        agenda_text=agenda_text,
        agenda_source_hash="agenda-sha-1",
    )
    assert len(artifact.items) == 2
    assert artifact.diagnostics["recovered_item_count"] == 1
    strict_item = next(item for item in artifact.items if item.source == "strict")
    recovered_item = next(item for item in artifact.items if item.source == "recovery")
    assert strict_item.title == "Call to order"
    assert recovered_item.title == "Annual report discussion"
    assert recovered_item.display_ref is None  # "Friendly label" doesn't survive recovery
    # Indices stay unique and contiguous across both groups (AgendaCandidatesArtifact requires it).
    assert {item.index for item in artifact.items} == {0, 1}


def test_finalize_agenda_job_still_raises_on_a_genuinely_unrecoverable_item():
    """An item whose evidence_quote is nowhere in the source text is not a recovery-layer miss --
    it's fabricated, and finalize_agenda_job must still fail it (preserving today's
    fail-and-retry-later behavior), not silently drop it."""
    agenda_text = "ID 26-1000\nApprove the actual item."
    content = json.dumps(
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
    )
    result = JobResult(
        task="agenda-item-extract",
        recipe_hash="recipe-agenda-unrecoverable",
        output={"choices": [{"message": {"content": content}}]},
    )
    with pytest.raises(ValueError):
        finalize_agenda_job(
            result,
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


def _agenda_result(items):
    return JobResult(
        task="agenda-item-extract",
        recipe_hash="recipe",
        output=json.dumps({"items": items}),
        model="nvidia/nemotron-3-ultra-550b-a55b:free",
    )


def _finalize(agenda_text, items):
    return finalize_agenda_job(
        _agenda_result(items),
        episode_uid="e1",
        agenda_text=agenda_text,
        agenda_source_hash="sha",
    )


def _numbered_agenda(count):
    return "\n".join(
        f"{n}. Consider approval of contract number {1000 + n}" for n in range(1, count + 1)
    )


def _numbered_item(n, **overrides):
    return {
        "display_ref": f"{n}.",
        "title": f"Contract {1000 + n}",
        "evidence_quote": f"Consider approval of contract number {1000 + n}",
        "line_start": n,
        "line_end": n,
        **overrides,
    }


def _outline_item(display_ref):
    return {
        "display_ref": display_ref,
        "title": "Outdoor burning",
        "evidence_quote": "Outdoor burning in the county",
        "line_start": 2,
        "line_end": 2,
    }


def test_a_composed_outline_reference_the_agenda_confirms_is_kept():
    # The line reads `A.` under section `4.`; the model's `4.A` is the item's real position --
    # and how providers name the chapter ("Item 4A") -- so it is kept, not rejected.
    agenda = "4. Items from the Commissioners Court\n  A. Outdoor burning in the county\n"
    artifact = _finalize(agenda, [_outline_item("4.A")])
    assert [item.display_ref for item in artifact.items] == ["4.A"]
    assert artifact.items[0].source == "recovery"


def test_an_outline_reference_the_agenda_contradicts_falls_back_to_the_source_label():
    agenda = "4. Items from the Commissioners Court\n  A. Outdoor burning in the county\n"
    artifact = _finalize(agenda, [_outline_item("7.A")])  # no `7.` anywhere above
    assert [item.display_ref for item in artifact.items] == ["A."]


def test_quote_marks_do_not_decide_whether_a_quote_is_grounded():
    agenda = (
        'Review of cases on Today\u2019s Agenda\nInstall 8" round wooden columns\n'
        'Rezone from: "MU-1" Low Intensity Mixed Use\n'
    )
    artifact = _finalize(
        agenda,
        [
            {
                "title": "Case review",
                "evidence_quote": "Review of cases on Today's Agenda",
                "line_start": 1,
                "line_end": 1,
            },
            {
                "title": "Columns",
                "evidence_quote": "Install 8 round wooden columns",
                "line_start": 2,
                "line_end": 2,
            },
            {
                # A closing single quote next to a word must not glue the words together.
                "title": "Rezoning",
                "evidence_quote": "Rezone from: 'MU-1' Low Intensity Mixed Use",
                "line_start": 3,
                "line_end": 3,
            },
        ],
    )
    assert len(artifact.items) == 3
    assert artifact.diagnostics["dropped_item_count"] == 0


def test_a_repeated_item_is_dropped_not_fatal():
    artifact = _finalize(
        _numbered_agenda(3), [_numbered_item(1), _numbered_item(1), _numbered_item(2)]
    )
    assert len(artifact.items) == 2
    assert artifact.diagnostics["duplicate_item_count"] == 1


def test_a_few_ungrounded_items_are_dropped_but_a_mostly_ungrounded_reply_still_fails():
    agenda = _numbered_agenda(20)
    stitched = {"evidence_quote": "Consider approval of contract number 9999 (Commissioner X)"}
    one_bad = [_numbered_item(n) for n in range(1, 20)] + [_numbered_item(20, **stitched)]
    artifact = _finalize(agenda, one_bad)
    assert len(artifact.items) == 19
    assert artifact.diagnostics["dropped_item_count"] == 1
    assert artifact.diagnostics["dropped_item_reasons"] == [
        "agenda item evidence quote is absent from cited lines"
    ]

    three_bad = [_numbered_item(n) for n in range(1, 18)] + [
        _numbered_item(n, **stitched) for n in (18, 19, 20)
    ]
    with pytest.raises(ValueError, match="absent from cited lines"):
        _finalize(agenda, three_bad)


def test_an_omitted_word_internal_apostrophe_still_grounds_the_quote():
    agenda = "Review of cases on Today’s Agenda\nThe City  ’s attorneys report\n"
    artifact = _finalize(
        agenda,
        [
            {
                "title": "Cases",
                "evidence_quote": "Review of cases on Todays Agenda",
                "line_start": 1,
                "line_end": 1,
            },
            {
                "title": "Attorneys",
                "evidence_quote": "The City's attorneys report",
                "line_start": 2,
                "line_end": 2,
            },
        ],
    )
    assert len(artifact.items) == 2


def test_a_quote_that_is_only_quote_marks_grounds_nothing():
    # Normalizes to "", which would be "in" any line: untrusted output must not ground a title so.
    with pytest.raises(ValueError):
        _finalize(
            "1. Consider approval of contract number 1001\n",
            [_numbered_item(1, evidence_quote='"')],
        )


def test_repeated_items_do_not_dilute_the_dropped_share():
    agenda = _numbered_agenda(2)
    stitched = {"evidence_quote": "Consider approval of contract number 9999"}
    padded = [_numbered_item(1)] * 9 + [_numbered_item(2, **stitched)]
    # 1 ungrounded of 2 distinct items is 50%, whatever the padding.
    with pytest.raises(ValueError, match="absent from cited lines"):
        _finalize(agenda, padded)


def test_an_outline_reference_is_not_confirmed_across_a_peer_section():
    agenda = (
        "3. Consent items\n  A. Approve minutes\n  B. Approve contract\n"
        "4. Discussion items\n  A. Outdoor burning in the county\n"
    )
    item = {
        "display_ref": "3.A",
        "title": "Outdoor burning",
        "evidence_quote": "Outdoor burning in the county",
        "line_start": 5,
        "line_end": 5,
    }
    artifact = _finalize(agenda, [item])
    # `4.` closes section 3, so `3.A` is contradicted and the source's own label is used.
    assert [i.display_ref for i in artifact.items] == ["A."]
    assert [i.display_ref for i in _finalize(agenda, [{**item, "display_ref": "4.A"}]).items] == [
        "4.A"
    ]
