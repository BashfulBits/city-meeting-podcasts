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


def test_chapter_id_survives_a_dropped_chapter():
    """remap() drops any chapter whose start falls in a cut span, so the served list's index no
    longer lines up with its position in source_chapters. chapter_id() must resolve each served
    chapter's identity from its true source position (chapters.py's source_index stamp), not
    from the served-list position — otherwise a later chapter picks up an earlier, dropped
    chapter's title/start and gets the wrong "stable" id."""
    ep = Episode(
        "g1",
        "Meeting",
        datetime(2026, 1, 1, tzinfo=UTC),
        "https://example.test/video",
        source_chapters=[
            {"start": 0, "title": "Call to order"},
            {"start": 450, "title": "Dropped item (falls in a cut span)"},
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
    assert len(inputs) == 2  # the middle chapter was dropped
    assert inputs[0]["title"] == "Call to order"
    assert inputs[1]["title"] == "Housing plan"
    # The served chapter at index 1 must be identified using source_chapters[2] ("Housing
    # plan"), not source_chapters[1] ("Dropped item") -- the bug this guards against.
    assert inputs[1]["chapter_id"] == chapter_id(ep, ep.source_chapters[2], 2)
    assert inputs[1]["chapter_id"] != chapter_id(ep, ep.source_chapters[1], 1)


def test_agenda_text_survives_a_dropped_chapter():
    """Same desync test_chapter_id_survives_a_dropped_chapter guards against, but for
    agenda_item_context()'s lookup: that dict is keyed by SOURCE chapter_index (R3's manifest),
    not served-list position, so the agenda-text lookup in chapter_tag_inputs() needs the same
    source_index resolution chapter_id() already has -- otherwise the surviving chapter after
    "Housing plan" gets the dropped "Dropped item" chapter's agenda text instead of its own."""

    class Storage:
        def exists(self, key):
            return key == "agenda-backup-key"

        def get_file(self, key, path):
            path.write_text(
                '{"items": ['
                '{"chapter_index": 0, "item_text": "Call to order text"},'
                '{"chapter_index": 1, "item_text": "WRONG: dropped item text"},'
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
            {"start": 450, "title": "Dropped item (falls in a cut span)"},
            {"start": 700, "title": "Housing plan"},
        ],
        links={"agenda_backup_artifact_key": "agenda-backup-key"},
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
    assert len(inputs) == 2  # the middle chapter was dropped
    assert inputs[0]["agenda_text"] == "Call to order text"
    assert inputs[1]["agenda_text"] == "Housing plan text"


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
