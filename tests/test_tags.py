from __future__ import annotations

from datetime import UTC, datetime

from citypods.models import Episode
from citypods.tags import (
    TAG_PROMPT_VERSION,
    chapter_id,
    chapter_tag_inputs,
    llm_tag_suggestions,
    load_taxonomy,
    merge_tag_sources,
    rollup_tags,
    tag_episode,
    taxonomy_from_dict,
)


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
                                    '{"tags":[{"id":"housing","confidence":0.82,'
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
    tags, chapter_tags, dispatched = llm_tag_suggestions(
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
    assert chapter_tags == {}
    assert tags[0]["evidence"] == [
        {"where": "transcript", "quote": "housing supply", "start": 10.0, "end": 13.0},
        {
            "where": "agenda",
            "quote": "housing plan",
            "document_url": "https://example.test/agenda",
            "document_locator": "Item 4",
        },
    ]
    assert TAG_PROMPT_VERSION == "2"
