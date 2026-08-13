from __future__ import annotations

from datetime import UTC, datetime

from citypods.models import Episode
from citypods.tags import (
    TAG_PROMPT_VERSION,
    chapter_id,
    chapter_tag_inputs,
    episode_tag_inputs,
    llm_tag_suggestions,
    load_taxonomy,
    merge_tag_sources,
    rollup_tags,
    rule_phrase_audit,
    tag_episode,
    tag_input_fingerprint,
    taxonomy_from_dict,
)
from citypods.timeline import Segment, Timeline


def test_seed_taxonomy_is_flat_and_contains_the_approved_unique_topics():
    taxonomy = load_taxonomy()
    ids = {tag.id for tag in taxonomy.tags}
    assert len(ids) == len(taxonomy.tags)
    assert {
        "street-trees-green-infrastructure",
        "third-places-public-life",
        "incremental-development",
        "historic-preservation",
        "public-art-culture",
        "neighborhood-engagement",
        "community-wealth-local-ownership",
    } <= ids
    assert "downtown-incremental-development" not in ids
    # rezoning was split out of zoning-reform (individual-property cases were firing on the
    # code-wide "zoning-reform" tag -- see GH #1057/#1062/#1072/#1076) and must be its own id.
    assert "rezoning" in ids


def test_zoning_case_matches_rezoning_not_zoning_reform():
    """Rule-path regression guard for the real production false positives (GH #1057/#1072/#1076):
    individual-property zoning actions must match the new "rezoning" tag, not "zoning-reform"."""
    taxonomy = load_taxonomy()
    for text in (
        "III.A Zoning Case PD20-25",
        "Application for a Specific Use Permit SUP20-6",
        "Replat of Odom Addition",
        "Variance Request for a rear setback",
    ):
        tags = {tag["id"] for tag in tag_episode(text, "", taxonomy)}
        assert "rezoning" in tags, text
        assert "zoning-reform" not in tags, text


def test_zoning_code_amendment_matches_zoning_reform_not_rezoning():
    taxonomy = load_taxonomy()
    text = "Proposed zoning code amendment to allow ADUs citywide"
    tags = {tag["id"] for tag in tag_episode(text, "", taxonomy)}
    assert "zoning-reform" in tags
    assert "rezoning" not in tags


def test_neighborhood_engagement_no_longer_matches_bare_public_meeting():
    """Regression guard: "public meeting" was the most generic, most boilerplate-prone keyword in
    neighborhood-engagement's include list (present on nearly every agenda header) and was
    removed. See also the structural preamble-stripping test in test_episode_tag_inputs_*."""
    taxonomy = load_taxonomy()
    tags = tag_episode("Notice of Public Meeting", "", taxonomy)
    assert all(tag["id"] != "neighborhood-engagement" for tag in tags)


def test_rules_are_explainable_and_preserve_agenda_transcript_location():
    taxonomy = taxonomy_from_dict(
        {
            "version": 1,
            "source_refs": {"example": "https://example.test"},
            "tags": [
                {
                    "id": "street-trees",
                    "label": "Street trees",
                    "description": "Trees",
                    "source_refs": ["example"],
                    "rules": {"include": ["street trees"]},
                }
            ],
        }
    )
    tags = tag_episode("Street trees on Main Street", "", taxonomy)
    assert tags == [
        {
            "id": "street-trees",
            "source": "rule",
            "confidence": 1.0,
            "evidence": [{"where": "agenda", "span": "Street trees"}],
        }
    ]


def test_rule_metadata_preserves_authored_phrase_and_matched_text():
    taxonomy = taxonomy_from_dict(
        {
            "version": 1,
            "source_refs": {"example": "https://example.test"},
            "tags": [
                {
                    "id": "housing",
                    "source_refs": ["example"],
                    "rules": {"include": ["housing supply"]},
                }
            ],
        }
    )
    tags = tag_episode("Housing   Supply", "", taxonomy, include_rule_metadata=True)
    assert tags[0]["rule_patterns"] == ["housing supply"]
    assert tags[0]["rule_match_texts"] == ["Housing   Supply"]


def test_rule_phrase_audit_surfaces_include_and_exclude_hits():
    taxonomy = taxonomy_from_dict(
        {
            "version": 1,
            "source_refs": {"example": "https://example.test"},
            "tags": [
                {
                    "id": "housing",
                    "source_refs": ["example"],
                    "rules": {
                        "include": ["housing supply"],
                        "exclude": ["not housing supply"],
                    },
                }
            ],
        }
    )
    observations = rule_phrase_audit("Housing supply discussion; not housing supply", "", taxonomy)
    assert {(item["kind"], item["pattern"]) for item in observations} == {
        ("include", "housing supply"),
        ("exclude", "not housing supply"),
    }
    assert all(item["match_count"] >= 1 for item in observations)
    assert all(len(item["match_texts"]) <= 3 for item in observations)


def test_prelabeler_excerpt_keeps_transcript_order_across_evidence():
    from citypods.tags import _prelabel_source_excerpt

    chapter = {
        "chapter_id": "ch-1",
        "title": "Housing",
        "agenda_text": "",
        "transcript_text": "early item\nlate item",
        "transcript_segments": [
            {"start": 0.0, "end": 1.0, "text": "early item"},
            {"start": 90.0, "end": 91.0, "text": "late item"},
        ],
    }
    excerpt = _prelabel_source_excerpt(
        {
            "evidence": [
                {"where": "transcript", "quote": "late item", "start": 90.0, "end": 91.0},
                {"where": "transcript", "quote": "early item", "start": 0.0, "end": 1.0},
            ]
        },
        chapter,
        fallback_agenda="",
        fallback_transcript="",
    )
    assert excerpt.index("early item") < excerpt.index("late item")


def test_prelabeler_excerpt_centers_tail_evidence():
    import json

    from citypods.compute.base import JobResult
    from citypods.tags import llm_prelabel_candidates

    captured = {}
    taxonomy = taxonomy_from_dict(
        {
            "version": 1,
            "source_refs": {"example": "https://example.test"},
            "tags": [
                {
                    "id": "housing",
                    "source_refs": ["example"],
                    "rules": {"include": ["housing"]},
                }
            ],
        }
    )
    candidate = {
        "candidate_id": "subject-1",
        "id": "housing",
        "source_kind": "llm",
        "scope": "chapter",
        "chapter_id": "ch-1",
        "evidence": [{"where": "transcript", "quote": "target evidence"}],
        "explanation": "The chapter discusses housing.",
    }
    transcript = "prefix " * 3000 + " target evidence " + "suffix " * 100

    class Backend:
        def run_inference(self, job):
            captured["messages"] = job.inputs["messages"]
            return JobResult(
                task=job.task,
                recipe_hash=job.recipe_hash,
                output={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "assessments": [
                                            {
                                                "candidate_id": "subject-1",
                                                "decision": "likely_correct",
                                                "confidence": 0.9,
                                                "reason": "supported",
                                                "evidence_supported": True,
                                            }
                                        ]
                                    }
                                )
                            }
                        }
                    ]
                },
            )

    result, pending, _ = llm_prelabel_candidates(
        Backend(),
        candidates=[candidate],
        taxonomy=taxonomy,
        chapters=[
            {
                "chapter_id": "ch-1",
                "title": "Housing",
                "agenda_text": "housing agenda",
                "transcript_text": transcript,
                "transcript_segments": [],
            }
        ],
        recipe_hash="recipe",
        model="reviewer",
    )
    assert not pending
    assert result["subject-1"]["prelabeler_decision"] == "likely_correct"
    payload = json.loads(captured["messages"][1]["content"])
    excerpt = payload["candidates"][0]["source_excerpt"]
    assert "target evidence" in excerpt


def test_exclude_terms_suppress_a_match_found_in_a_different_source():
    """The exclude check must run against the combined agenda+transcript text, not each source
    independently -- otherwise an exclude term present only in the agenda (e.g. "school zoning")
    fails to suppress an include match found only in the transcript."""
    taxonomy = taxonomy_from_dict(
        {
            "version": 1,
            "source_refs": {"example": "https://example.test"},
            "tags": [
                {
                    "id": "zoning-reform",
                    "source_refs": ["example"],
                    "rules": {"include": ["zoning"], "exclude": ["school zoning"]},
                }
            ],
        }
    )
    tags = tag_episode(
        "School Zoning Boundary Adjustment", "the current zoning code applies here", taxonomy
    )
    assert tags == []


def test_llm_suggestions_add_without_replacing_rule_provenance():
    rules = [
        {
            "id": "street-trees",
            "source": "rule",
            "confidence": 1.0,
            "evidence": [{"where": "agenda", "span": "street trees"}],
        }
    ]
    llm = [
        {
            "id": "street-trees",
            "source": "llm",
            "confidence": 0.82,
            "explanation": "The agenda discusses trees.",
            "evidence": [{"where": "transcript", "span": "trees"}],
        },
        {
            "id": "new-tag",
            "source": "llm",
            "confidence": 0.8,
            "evidence": [{"where": "transcript", "span": "new"}],
        },
    ]
    merged = merge_tag_sources(rules, llm)
    assert [tag["id"] for tag in merged] == ["street-trees", "new-tag"]
    assert merged[0]["source"] == "rule"
    assert merged[0]["explanation"] == "The agenda discusses trees."


def test_rollup_keeps_the_highest_llm_confidence_across_scopes():
    """A tag can be suggested at episode scope and again at chapter scope with a different LLM
    confidence. The rolled-up episode facet must keep the highest of the two, not whichever
    occurrence happened to be merged first -- an earlier, lower-confidence occurrence must not
    permanently shadow a later, better-supported one for the same tag id."""
    taxonomy = taxonomy_from_dict(
        {
            "version": 1,
            "source_refs": {"x": "https://example.test"},
            "tags": [{"id": "housing", "source_refs": ["x"], "rules": {"include": ["housing"]}}],
        }
    )
    episode_tags = [{"id": "housing", "source": "llm", "confidence": 0.4, "evidence": []}]
    chapter_annotations = [
        {
            "chapter_id": "ch-1",
            "tags": [{"id": "housing", "source": "llm", "confidence": 0.91, "evidence": []}],
        }
    ]
    rolled = rollup_tags(episode_tags, chapter_annotations, taxonomy)
    assert rolled[0]["id"] == "housing"
    assert rolled[0]["confidence"] == 0.91


def test_chapter_ids_use_source_data_and_rollup_is_taxonomy_ordered():
    ep = Episode(
        "g1",
        "Meeting",
        datetime(2026, 1, 1, tzinfo=UTC),
        "https://example.test/video",
        source_chapters=[
            {"start": 10, "title": "Tree ordinance"},
            {"start": 40, "title": "Housing plan"},
        ],
        chapters=[
            {"start": 8, "title": "Tree ordinance"},
            {"start": 35, "title": "Housing plan"},
        ],
    )
    inputs = chapter_tag_inputs(ep)
    assert [item["chapter_id"] for item in inputs] == [
        chapter_id(ep, ep.chapters[0], 0),
        chapter_id(ep, ep.chapters[1], 1),
    ]
    taxonomy = taxonomy_from_dict(
        {
            "version": 1,
            "source_refs": {"x": "https://example.test"},
            "tags": [
                {"id": "housing", "source_refs": ["x"], "rules": {"include": ["housing"]}},
                {"id": "trees", "source_refs": ["x"], "rules": {"include": ["tree"]}},
            ],
        }
    )
    annotations = [
        {"chapter_id": inputs[1]["chapter_id"], "tags": [{"id": "housing", "source": "rule"}]},
        {"chapter_id": inputs[0]["chapter_id"], "tags": [{"id": "trees", "source": "rule"}]},
    ]
    assert [tag["id"] for tag in rollup_tags([], annotations, taxonomy)] == ["housing", "trees"]


def test_chapter_id_survives_a_snapped_chapter():
    """A chapter start in a cut span snaps to the next served boundary, so served position no
    longer lines up with source position. chapter_id() must resolve each served chapter's identity
    from its true source position (chapters.py's source_index stamp), not served-list position."""
    ep = Episode(
        "g1",
        "Meeting",
        datetime(2026, 1, 1, tzinfo=UTC),
        "https://example.test/video",
        source_chapters=[
            {"start": 0, "title": "Call to order"},
            {"start": 450, "title": "Snapped item (falls in a cut span)"},
            {"start": 700, "title": "Housing plan"},
        ],
        timeline=Timeline(
            version="silence-v1",
            segments=(
                Segment(
                    served_start=0,
                    served_end=300,
                    kind="source",
                    source_id="s0",
                    source_start=0,
                    source_end=300,
                ),
                Segment(
                    served_start=300,
                    served_end=3300,
                    kind="source",
                    source_id="s0",
                    source_start=600,
                    source_end=3600,
                ),
            ),
        ),
    )
    inputs = chapter_tag_inputs(ep)
    assert len(inputs) == 3  # the middle chapter snaps to the next kept boundary
    assert inputs[0]["title"] == "Call to order"
    assert inputs[1]["title"] == "Snapped item (falls in a cut span)"
    assert inputs[2]["title"] == "Housing plan"
    assert inputs[1]["chapter_id"] == chapter_id(ep, ep.source_chapters[1], 1)
    assert inputs[2]["chapter_id"] == chapter_id(ep, ep.source_chapters[2], 2)


def test_agenda_text_survives_a_snapped_chapter():
    """Same desync test_chapter_id_survives_a_snapped_chapter guards against, but for
    agenda_item_context()'s lookup: that dict is keyed by SOURCE chapter_index (R3's manifest),
    not served-list position, so the agenda-text lookup in chapter_tag_inputs() needs the same
    source_index resolution chapter_id() already has -- otherwise the surviving chapters could
    receive agenda evidence from the wrong source item instead of their own."""

    class Storage:
        def exists(self, key):
            return key == "agenda-backup-key"

        def get_file(self, key, path):
            path.write_text(
                '{"items": ['
                '{"chapter_index": 0, "item_text": "Call to order text"},'
                '{"chapter_index": 1, "item_text": "Snapped item text"},'
                '{"chapter_index": 2, "item_text": "Housing plan text"}'
                "]}"
            )
            return True

    ep = Episode(
        "g1",
        "Meeting",
        datetime(2026, 1, 1, tzinfo=UTC),
        "https://example.test/video",
        source_chapters=[
            {"start": 0, "title": "Call to order"},
            {"start": 450, "title": "Snapped item (falls in a cut span)"},
            {"start": 700, "title": "Housing plan"},
        ],
        links={"agenda_backup_artifact_key": "agenda-backup-key"},
        agenda_text_quality={"status": "accepted", "eligibility": "agenda"},
        timeline=Timeline(
            version="silence-v1",
            segments=(
                Segment(
                    served_start=0,
                    served_end=300,
                    kind="source",
                    source_id="s0",
                    source_start=0,
                    source_end=300,
                ),
                Segment(
                    served_start=300,
                    served_end=3300,
                    kind="source",
                    source_id="s0",
                    source_start=600,
                    source_end=3600,
                ),
            ),
        ),
    )
    inputs = chapter_tag_inputs(ep, Storage())
    assert len(inputs) == 3  # the middle chapter snaps to the next kept boundary
    assert inputs[0]["agenda_text"] == "Call to order text"
    assert inputs[1]["agenda_text"] == "Snapped item text"
    assert inputs[2]["agenda_text"] == "Housing plan text"


def test_transcript_windows_are_the_reliable_chapter_association():
    class Storage:
        def exists(self, key):
            return key == "transcript"

        def get_file(self, key, path):
            path.write_text(
                '{"segments":[{"start":10,"text":"street trees"},'
                '{"start":120,"text":"housing supply"}]}'
            )
            return True

    ep = Episode(
        "g1",
        "Meeting",
        datetime(2026, 1, 1, tzinfo=UTC),
        "https://example.test/video",
        chapters=[
            {"start": 0, "title": "First item"},
            {"start": 100, "title": "Second item"},
        ],
        transcript_key="transcript",
        transcript_format="json",
    )
    inputs = chapter_tag_inputs(ep, Storage())
    assert inputs[0]["transcript_text"] == "street trees"
    assert inputs[1]["transcript_text"] == "housing supply"


def test_llm_evidence_is_a_quoted_region_with_transcript_timing_and_document_link():
    from citypods.compute.base import JobResult

    taxonomy = taxonomy_from_dict(
        {
            "version": 1,
            "source_refs": {"x": "https://example.test"},
            "tags": [{"id": "housing", "source_refs": ["x"], "rules": {"include": ["housing"]}}],
        }
    )

    class Backend:
        def run_inference(self, job):
            return JobResult(
                task=job.task,
                recipe_hash=job.recipe_hash,
                output={
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    '{"tags":[{"id":"housing","chapter_id":"ch-1",'
                                    '"confidence":0.82,'
                                    '"explanation":"The item discusses housing.","evidence":['
                                    '{"where":"transcript","quote":"housing supply"},'
                                    '{"where":"agenda","quote":"housing plan",'
                                    '"document_url":"https://example.test/agenda",'
                                    '"document_locator":"Item 4"}]}]}'
                                )
                            }
                        }
                    ]
                },
            )

    episode, chapters = (
        "Housing plan",
        [
            {
                "chapter_id": "ch-1",
                "title": "Housing plan",
                "agenda_text": "housing plan",
                "transcript_text": "housing supply",
                "transcript_segments": [{"start": 10.0, "end": 13.0, "text": "housing supply"}],
            }
        ],
    )
    tags, chapter_tags, dispatched, _resolved_model = llm_tag_suggestions(
        Backend(),
        taxonomy=taxonomy,
        agenda_item_titles=episode,
        agenda_text="housing plan",
        transcript_text="housing supply",
        recipe_hash="recipe",
        chapter_inputs=chapters,
        agenda_documents=[{"title": "Agenda", "url": "https://example.test/agenda"}],
    )
    assert not dispatched
    assert tags == []
    assert chapter_tags["ch-1"][0]["evidence"] == [
        {
            "where": "transcript",
            "quote": "housing supply",
            "chapter_id": "ch-1",
            "start": 10.0,
            "end": 13.0,
        },
        {
            "where": "agenda",
            "quote": "housing plan",
            "chapter_id": "ch-1",
            "document_url": "https://example.test/agenda",
            "document_locator": "Item 4",
        },
    ]
    assert TAG_PROMPT_VERSION == "3"


def test_transcript_region_does_not_span_the_whole_episode_on_a_common_word():
    """The old heuristic OR-matched only the quote's first/last word against each segment, so a
    common word like "the" could pull in unrelated segments from anywhere in the transcript and
    yield a bogus, episode-spanning timestamp range. The fix must trace the quote's own
    contiguous match back to only the segments it actually spans."""
    from citypods.tags import _transcript_region

    segments = [
        {"start": 0.0, "end": 2.0, "text": "the meeting is called to order"},
        {"start": 100.0, "end": 103.0, "text": "the new zoning plan is approved"},
        {"start": 500.0, "end": 502.0, "text": "the session is adjourned"},
    ]
    start, end = _transcript_region("the new zoning plan is approved", segments)
    assert (start, end) == (100.0, 103.0)


def _fp_episode():
    return Episode(
        "g1",
        "Meeting",
        datetime(2026, 1, 1, tzinfo=UTC),
        "https://example.test/video",
        links={"agenda_text_artifact_key": "documents/x/1/agenda_text-aaa"},
        transcript_key="transcripts/x/1-asr-bbb.vtt",
        transcript_format="vtt",
    )


def _fp_taxonomy():
    return taxonomy_from_dict(
        {
            "version": 1,
            "source_refs": {"x": "https://example.test"},
            "tags": [{"id": "housing", "source_refs": ["x"], "rules": {"include": ["housing"]}}],
        }
    )


def test_tag_input_fingerprint_is_stable_for_unrelated_field_changes():
    """The fingerprint must depend only on tagging inputs (content-addressed keys, chapter
    boundaries, taxonomy/tagger/LLM config, admission policy) -- not on anything else about the
    episode -- so an unrelated field changing (e.g. a fresh provider title on every fetch) doesn't
    force a needless re-tag."""
    ep = _fp_episode()
    taxonomy = _fp_taxonomy()
    before = tag_input_fingerprint(ep, taxonomy, llm_enabled=False)
    ep.title = "A different title entirely"
    ep.body = "different body text"
    after = tag_input_fingerprint(ep, taxonomy, llm_enabled=False)
    assert before == after


def test_tag_input_fingerprint_changes_with_agenda_artifact_key():
    ep = _fp_episode()
    taxonomy = _fp_taxonomy()
    before = tag_input_fingerprint(ep, taxonomy, llm_enabled=False)
    ep.links = {"agenda_text_artifact_key": "documents/x/1/agenda_text-different"}
    after = tag_input_fingerprint(ep, taxonomy, llm_enabled=False)
    assert before != after


def test_tag_input_fingerprint_changes_with_transcript_key():
    ep = _fp_episode()
    taxonomy = _fp_taxonomy()
    before = tag_input_fingerprint(ep, taxonomy, llm_enabled=False)
    ep.transcript_key = "transcripts/x/1-asr-different.vtt"
    after = tag_input_fingerprint(ep, taxonomy, llm_enabled=False)
    assert before != after


def test_tag_input_fingerprint_changes_with_chapter_boundaries():
    ep = _fp_episode()
    ep.source_chapters = [{"start": 10, "title": "Tree ordinance"}]
    ep.chapters = [{"start": 10, "title": "Tree ordinance"}]
    taxonomy = _fp_taxonomy()
    before = tag_input_fingerprint(ep, taxonomy, llm_enabled=False)
    ep.source_chapters = [{"start": 20, "title": "Tree ordinance"}]
    ep.chapters = [{"start": 20, "title": "Tree ordinance"}]
    after = tag_input_fingerprint(ep, taxonomy, llm_enabled=False)
    assert before != after


def test_tag_input_fingerprint_changes_with_taxonomy_version():
    ep = _fp_episode()
    before = tag_input_fingerprint(ep, _fp_taxonomy(), llm_enabled=False)
    bumped = taxonomy_from_dict(
        {
            "version": 2,
            "source_refs": {"x": "https://example.test"},
            "tags": [{"id": "housing", "source_refs": ["x"], "rules": {"include": ["housing"]}}],
        }
    )
    after = tag_input_fingerprint(ep, bumped, llm_enabled=False)
    assert before != after


def test_tag_input_fingerprint_changes_with_llm_config():
    ep = _fp_episode()
    taxonomy = _fp_taxonomy()
    disabled = tag_input_fingerprint(ep, taxonomy, llm_enabled=False)
    enabled = tag_input_fingerprint(
        ep, taxonomy, llm_enabled=True, llm_route="litellm:gemini/gemini-3-flash-preview"
    )
    other_route = tag_input_fingerprint(
        ep, taxonomy, llm_enabled=True, llm_route="litellm:gemini/other-model"
    )
    other_admission = tag_input_fingerprint(
        ep,
        taxonomy,
        llm_enabled=True,
        llm_route="litellm:gemini/gemini-3-flash-preview",
        admission_policy="policy-2",
    )
    assert len({disabled, enabled, other_route, other_admission}) == 4


def test_episode_tag_inputs_strips_preamble_and_includes_backup_text():
    """End-to-end (production-shaped, not hand-constructed) validation of 4c/4d: preamble text
    before the first chapter title is dropped from agenda_text, and backup/attachment document
    text from the agenda_backup manifest is folded in."""

    class Storage:
        def exists(self, key):
            return key in ("agenda-key", "backup-key")

        def get_file(self, key, path):
            if key == "agenda-key":
                path.write_text(
                    "Notice of public meeting. Members of the public who wish to speak "
                    "can call 555-1234.\n\nI. Call to Order\nII. Zoning Case PD20-25"
                )
            elif key == "backup-key":
                import json

                path.write_text(
                    json.dumps(
                        {
                            "links": [
                                {
                                    "url": "https://example.test/staff-report.pdf",
                                    "chapter_index": 1,
                                    "text": "Staff Report - Zoning Case PD20-25 details",
                                }
                            ]
                        }
                    )
                )
            return True

    ep = Episode(
        "g1",
        "Meeting",
        datetime(2026, 1, 1, tzinfo=UTC),
        "https://example.test/video",
        source_chapters=[
            {"start": 0, "title": "I. Call to Order"},
            {"start": 100, "title": "II. Zoning Case PD20-25"},
        ],
        links={
            "agenda_text_artifact_key": "agenda-key",
            "agenda_backup_artifact_key": "backup-key",
        },
        agenda_text_quality={"status": "accepted", "eligibility": "agenda"},
    )
    titles, agenda_text, _transcript = episode_tag_inputs(ep, Storage())
    assert "555-1234" not in agenda_text
    assert "I. Call to Order" in titles
    assert "Staff Report - Zoning Case PD20-25 details" in agenda_text
    # CodeRabbit regression: the retained agenda_text must stay verbatim (original casing/
    # whitespace), not the lowercase/whitespace-collapsed copy resolve_chapter_spans() uses
    # internally -- it's what rule-tag evidence spans and LLM "exact quote" evidence are drawn
    # from.
    assert "I. Call to Order" in agenda_text
    assert "i. call to order" not in agenda_text


def test_episode_tag_inputs_excludes_rejected_and_notice_agendas():
    class Storage:
        def exists(self, key):
            return key == "agenda-key"

        def get_file(self, key, path):
            path.write_text("1. A visible agenda item")
            return True

    for quality in (
        {"status": "rejected", "eligibility": "unknown"},
        {"status": "accepted", "eligibility": "notice"},
    ):
        ep = Episode(
            "g1",
            "Meeting",
            datetime(2026, 1, 1, tzinfo=UTC),
            "https://example.test/video",
            links={"agenda_text_artifact_key": "agenda-key"},
        )
        ep.agenda_text_quality = quality
        _titles, agenda_text, _transcript = episode_tag_inputs(ep, Storage())
        assert agenda_text == ""


def test_llm_tag_suggestions_admits_material_larger_than_one_tpm_minute():
    """TPM is an average throughput rate, not a maximum request size."""

    from citypods.compute.base import JobResult

    class Config:
        model = "gemini/gemini-3.1-flash-lite"
        additional_models = ()

    class Storage:
        cas_capable = True

    class Backend:
        storage = Storage()
        config = Config()

        def run_inference(self, job):
            return JobResult(
                task=job.task,
                recipe_hash=job.recipe_hash,
                output={"choices": [{"message": {"content": '{"tags": []}'}}]},
            )

    taxonomy = taxonomy_from_dict(
        {
            "version": 1,
            "source_refs": {"x": "https://example.test"},
            "tags": [{"id": "housing", "source_refs": ["x"], "rules": {"include": ["housing"]}}],
        }
    )
    huge_agenda_text = "housing " * 200_000  # far past one minute of gemini's 250k TPM
    tags, chapter_tags, dispatched, resolved_model = llm_tag_suggestions(
        Backend(),
        taxonomy=taxonomy,
        agenda_item_titles="Housing plan",
        agenda_text=huge_agenda_text,
        transcript_text="housing supply",
        recipe_hash="recipe",
    )
    assert tags == []
    assert chapter_tags == {}
    assert dispatched is True
    assert resolved_model == "payload-too-large"


def test_llm_tag_suggestions_dispatches_when_material_fits_one_tpm_capped_route():
    """A payload that fits within the configured provider context can dispatch normally."""
    from citypods.compute.base import JobResult

    class Config:
        model = "gemini/gemini-3.1-flash-lite"
        additional_models = ()

    class Storage:
        cas_capable = True

    class Backend:
        storage = Storage()
        config = Config()

        def run_inference(self, job):
            assert "llm_policy" in job.inputs
            return JobResult(
                task=job.task,
                recipe_hash=job.recipe_hash,
                output={"choices": [{"message": {"content": '{"tags":[]}'}}]},
            )

    taxonomy = taxonomy_from_dict(
        {
            "version": 1,
            "source_refs": {"x": "https://example.test"},
            "tags": [{"id": "housing", "source_refs": ["x"], "rules": {"include": ["housing"]}}],
        }
    )
    tags, chapter_tags, dispatched, _resolved_model = llm_tag_suggestions(
        Backend(),
        taxonomy=taxonomy,
        agenda_item_titles="Housing plan",
        agenda_text="housing plan",
        transcript_text="housing supply",
        recipe_hash="recipe",
    )
    assert tags == []
    assert chapter_tags == {}
    assert dispatched is False


def test_chapter_tagger_batches_large_meetings_and_excludes_episode_backup_context():
    from citypods.compute.base import JobResult

    class Config:
        model = "gemini/gemini-3.1-flash-lite"
        additional_models = ()

    class Storage:
        cas_capable = True

    class Backend:
        storage = Storage()
        config = Config()

        def __init__(self):
            self.jobs = []

        def run_inference(self, job):
            self.jobs.append(job)
            return JobResult(
                task=job.task,
                recipe_hash=job.recipe_hash,
                output={"choices": [{"message": {"content": '{"tags":[]}'}}]},
                model="gemini/gemini-3.1-flash-lite",
            )

    taxonomy = taxonomy_from_dict(
        {
            "version": 1,
            "source_refs": {"x": "https://example.test"},
            "tags": [{"id": "housing", "source_refs": ["x"], "rules": {"include": ["housing"]}}],
        }
    )
    chapters = [
        {
            "chapter_id": f"c{index}",
            "title": f"Chapter {index}",
            "agenda_text": "mapped agenda evidence",
            "transcript_text": "housing " * 5_000,
            "transcript_segments": [],
        }
        for index in range(40)
    ]
    backend = Backend()
    _tags, chapter_tags, pending, resolved_model = llm_tag_suggestions(
        backend,
        taxonomy=taxonomy,
        agenda_item_titles="unrelated episode titles",
        agenda_text="UNMAPPED BACKUP SHOULD NOT REACH THE CHAPTER TAGGER " * 10_000,
        transcript_text="unrelated transcript",
        recipe_hash="large-meeting",
        chapter_inputs=chapters,
    )

    assert pending is False
    assert chapter_tags == {}
    assert resolved_model == "gemini/gemini-3.1-flash-lite"
    assert len(backend.jobs) > 1
    assert len({job.recipe_hash for job in backend.jobs}) == len(backend.jobs)
    assert all(
        "UNMAPPED BACKUP" not in job.inputs["messages"][1]["content"] for job in backend.jobs
    )


def test_chapter_tagger_preserves_evidence_beyond_legacy_cutoffs():
    """Chapter batching must retain, or explicitly defer, all mapped evidence."""
    from citypods.compute.base import JobResult

    class Config:
        model = "gemini/gemini-3.1-flash-lite"
        additional_models = ()

    class Storage:
        cas_capable = True

    class Backend:
        storage = Storage()
        config = Config()

        def __init__(self):
            self.job = None

        def run_inference(self, job):
            self.job = job
            return JobResult(
                task=job.task,
                recipe_hash=job.recipe_hash,
                output={"choices": [{"message": {"content": '{"tags":[]}'}}]},
            )

    taxonomy = taxonomy_from_dict(
        {
            "version": 1,
            "source_refs": {"x": "https://example.test"},
            "tags": [{"id": "housing", "source_refs": ["x"], "rules": {"include": ["housing"]}}],
        }
    )
    backend = Backend()
    llm_tag_suggestions(
        backend,
        taxonomy=taxonomy,
        agenda_item_titles="",
        agenda_text="",
        transcript_text="",
        recipe_hash="complete-chapter",
        chapter_inputs=[
            {
                "chapter_id": "c1",
                "title": "Chapter",
                "agenda_text": "a" * 20_000 + " AGENDA-TAIL",
                "transcript_text": "t" * 30_000 + " TRANSCRIPT-TAIL",
                "transcript_segments": [
                    {"start": index, "end": index + 1, "text": "segment"} for index in range(200)
                ]
                + [{"start": 201, "end": 202, "text": "x" * 1_200 + " SEGMENT-TAIL"}],
            }
        ],
    )

    content = backend.job.inputs["messages"][1]["content"]
    assert "AGENDA-TAIL" in content
    assert "TRANSCRIPT-TAIL" in content
    assert "SEGMENT-TAIL" in content


def test_chapter_tagger_admits_a_batch_that_fits_an_additional_allowed_route(monkeypatch):
    """A smaller primary route must not reject work that an allowed fallback can accept."""
    from citypods.compute import llm_policy
    from citypods.compute.base import JobResult
    from citypods.compute.llm_policy import LLMRoute, PricingPolicy, QuotaPolicy

    primary = "test/primary"
    fallback = "test/fallback"
    primary_route = LLMRoute(
        model=primary,
        transport="llm-dispatch",
        free=True,
        quota=QuotaPolicy(tpm=10_000),
        pricing=PricingPolicy(),
        input_context_limit=10_000,
        output_context_limit=1_024,
    )
    fallback_route = LLMRoute(
        model=fallback,
        transport="llm-dispatch",
        free=True,
        quota=QuotaPolicy(tpm=100_000),
        pricing=PricingPolicy(),
        input_context_limit=100_000,
        output_context_limit=1_024,
    )
    monkeypatch.setitem(llm_policy.ROUTES, primary, primary_route)
    monkeypatch.setitem(llm_policy.ROUTES, fallback, fallback_route)
    monkeypatch.setitem(llm_policy.ROUTE_CANDIDATES, primary, (primary_route,))
    monkeypatch.setitem(llm_policy.ROUTE_CANDIDATES, fallback, (fallback_route,))

    class Config:
        model = primary
        additional_models = (fallback,)

    class Storage:
        cas_capable = True

    class Backend:
        storage = Storage()
        config = Config()

        def run_inference(self, job):
            assert job.inputs["llm_policy"].allowed_models == (primary, fallback)
            return JobResult(
                task=job.task,
                recipe_hash=job.recipe_hash,
                output={"choices": [{"message": {"content": '{"tags":[]}'}}]},
                model=fallback,
            )

    taxonomy = taxonomy_from_dict(
        {
            "version": 1,
            "source_refs": {"x": "https://example.test"},
            "tags": [{"id": "housing", "source_refs": ["x"], "rules": {"include": ["housing"]}}],
        }
    )
    _tags, chapter_tags, pending, resolved_model = llm_tag_suggestions(
        Backend(),
        taxonomy=taxonomy,
        agenda_item_titles="",
        agenda_text="",
        transcript_text="",
        recipe_hash="fallback-route",
        chapter_inputs=[
            {
                "chapter_id": "c1",
                "title": "Chapter",
                "agenda_text": "housing " * 5_000,
                "transcript_text": "",
                "transcript_segments": [],
            }
        ],
    )

    assert chapter_tags == {}
    assert pending is False
    assert resolved_model == fallback
