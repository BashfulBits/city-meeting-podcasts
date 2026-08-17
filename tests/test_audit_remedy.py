"""Unit tests for citypods.audit_remedy.

The emphasis is the validation boundary: the model's output is untrusted, so the tests that
matter most are the ones proving a hostile or confused proposal cannot reach the filesystem.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
import yaml
from pydantic import ValidationError

from citypods.audit import collect_unexpected_bodies
from citypods.audit_remedy import (
    BodyProposal,
    RejectedProposal,
    RemedyOutput,
    RemedyPlan,
    SourceContext,
    apply_remedy_plan,
    classify_unexpected_bodies,
    evidence_recipe_hash,
    feed_paths_by_slug,
    format_remedy_markdown,
    gather_unexpected_body_evidence,
    validate_proposals,
)
from citypods.compute.base import JobHandle, JobResult
from citypods.models import City, Episode
from tests._cas_fake import MemStorage


def make_city(slug, body, **source_extra):
    return City(
        slug=slug,
        city_entity="test-city-tx",
        provider="granicus",
        source={"feed_url": "https://test.example/feed", "body": body, **source_extra},
        podcast_title=f"Test City: {body}",
        podcast_description="Test description",
        podcast_author="City of Test, TX",
        podcast_email="",
    )


def make_episode(guid, title, body, day):
    return Episode(
        guid=guid,
        title=title,
        published=datetime(2026, 5, day, 18, 0, tzinfo=UTC),
        body=body,
        video_url=f"https://test.example/{guid}",
    )


@pytest.fixture
def repo(tmp_path):
    """A miniature repo with two feeds on one source."""
    feeds = tmp_path / "config" / "feeds"
    feeds.mkdir(parents=True)
    (tmp_path / "config" / "cities").mkdir(parents=True)
    (tmp_path / "config" / "cities" / "test-city-tx.yml").write_text(
        yaml.safe_dump(
            {
                "name": "Test City",
                "state": "TX",
                "city_website": "https://testcity.gov",
                "meetings_url": "https://testcity.gov/meetings",
            }
        ),
        encoding="utf-8",
    )
    (feeds / "test-city-council.yml").write_text(
        "slug: test-city-council\n"
        "city: test-city-tx\n"
        "provider: granicus\n"
        "source:\n"
        "  # Council reads the main view.\n"
        "  feed_url: https://test.example/feed\n"
        '  body: "City Council"\n'
        "  body_any:\n"
        '    - "Work Session"\n'
        'podcast_title: "Test City: Council"\n'
        'podcast_author: "City of Test, TX"\n'
        'podcast_email: ""\n'
        'podcast_description: "Meetings."\n',
        encoding="utf-8",
    )
    (feeds / "test-city-library-board.yml").write_text(
        "slug: test-city-library-board\n"
        "city: test-city-tx\n"
        "provider: granicus\n"
        "source:\n"
        "  feed_url: https://test.example/feed\n"
        '  body: "Library Board"\n'
        'podcast_title: "Test City: Library Board"\n'
        'podcast_author: "City of Test, TX"\n'
        'podcast_email: ""\n'
        'podcast_description: "Meetings."\n',
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def evidence(repo):
    council = make_city("test-city-council", "City Council", body_any=["Work Session"])
    library = make_city("test-city-library-board", "Library Board")
    episodes = [
        make_episode("guid-101", "Special Meeting - Budget", "Special Meeting", 10),
        make_episode("guid-102", "Special Meeting - Hearing", "Special Meeting", 15),
        make_episode("guid-103", "TIRZ 4-1-26", "TIRZ", 20),
    ]
    rows = collect_unexpected_bodies(episodes, {}, related_cities=[council, library])
    return gather_unexpected_body_evidence(
        source_key="test-source",
        city_slug="test-city-tx",
        unexpected_rows=rows,
        related_cities=[council, library],
        records={"guid-1": {"body": "City Council", "title": "Regular Meeting"}},
        repo_root=repo,
    )


def proposal(**overrides):
    base = {
        "source_key": "test-source",
        "unexpected_body": "Special Meeting",
        "action": "union",
        "target_feeds": ["test-city-council"],
        "rationale": "Recurring council session",
    }
    return BodyProposal(**{**base, **overrides})


# --- evidence -----------------------------------------------------------------------------


def test_evidence_describes_the_audits_own_rows(evidence):
    assert evidence["source_key"] == "test-source"
    assert evidence["city"]["name"] == "Test City"
    assert {feed["slug"] for feed in evidence["existing_feeds"]} == {
        "test-city-council",
        "test-city-library-board",
    }
    labels = {f["unexpected_body"]: f for f in evidence["unexpected_findings"]}
    assert set(labels) == {"Special Meeting", "TIRZ"}
    assert labels["Special Meeting"]["count"] == 2
    assert labels["Special Meeting"]["date_range"]["earliest"].startswith("2026-05-10")
    assert labels["Special Meeting"]["date_range"]["latest"].startswith("2026-05-15")
    assert {ep["provider_guid"] for ep in labels["Special Meeting"]["episodes"]} == {
        "guid-101",
        "guid-102",
    }


def test_evidence_bounds_open_ended_supporting_lists(repo):
    council = make_city("test-city-council", "City Council")
    records = {f"g{i}": {"body": f"Body {i}", "title": f"T{i}"} for i in range(200)}
    rows = collect_unexpected_bodies(
        [make_episode("x", "One Off", "Brand New Board", 1)], {}, related_cities=[council]
    )
    ev = gather_unexpected_body_evidence(
        source_key="s",
        city_slug="test-city-tx",
        unexpected_rows=rows,
        related_cities=[council],
        records=records,
        repo_root=repo,
    )
    archive = ev["historical_archive"]
    assert len(archive["known_archived_bodies"]) == 60
    assert archive["known_archived_bodies_truncated"] is True
    assert archive["total_archived_count"] == 200
    assert len(archive["sample_past_titles"]) == 10


def test_recipe_hash_is_content_addressed_not_time_based(evidence):
    assert evidence_recipe_hash(evidence) == evidence_recipe_hash(dict(evidence))
    other = {**evidence, "source_key": "different"}
    assert evidence_recipe_hash(evidence) != evidence_recipe_hash(other)


# --- validation boundary ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bad", "expected_reason"),
    [
        ({"unexpected_body": "Never Observed"}, "was not observed"),
        ({"source_key": "other-source"}, "does not match this bundle"),
        ({"target_feeds": ["some-other-citys-feed"]}, "not configured on this source"),
        ({"target_feeds": []}, "requires at least one target feed"),
        (
            {"action": "single_uid_inclusion", "provider_guids": []},
            "requires provider_guids",
        ),
        (
            {"action": "single_uid_inclusion", "provider_guids": ["guid-999"]},
            "were not observed for this label",
        ),
        (
            {"action": "new_feed", "new_feed_slug": "../../etc/passwd"},
            "not a well-formed slug",
        ),
        ({"action": "new_feed", "new_feed_slug": ""}, "not a well-formed slug"),
        (
            {"action": "new_feed", "new_feed_slug": "test-city-council"},
            "already exists",
        ),
        (
            {"action": "new_feed", "new_feed_slug": "ok-slug", "new_feed_title": ""},
            "requires new_feed_title",
        ),
    ],
)
def test_unverifiable_proposals_are_rejected(bad, expected_reason, evidence, repo):
    plan = validate_proposals(
        RemedyOutput(proposals=[proposal(**bad)]), evidence, feed_paths_by_slug(repo)
    )
    assert plan.accepted == []
    assert len(plan.rejected) == 1
    assert expected_reason in plan.rejected[0].reason


def test_well_formed_proposals_are_accepted(evidence, repo):
    remedy = RemedyOutput(
        proposals=[
            proposal(),
            proposal(
                unexpected_body="TIRZ",
                action="single_uid_inclusion",
                provider_guids=["guid-103"],
            ),
        ]
    )
    plan = validate_proposals(remedy, evidence, feed_paths_by_slug(repo))
    assert len(plan.accepted) == 2
    assert plan.rejected == []


def test_proposals_for_removed_feed_files_are_rejected(evidence, repo):
    """Evidence can outlive a feed rename or removal between audit and remediation."""
    paths = feed_paths_by_slug(repo)
    paths.pop("test-city-council")
    plan = validate_proposals(RemedyOutput(proposals=[proposal()]), evidence, paths)
    assert plan.accepted == []
    assert "have no file under config/feeds" in plan.rejected[0].reason


def test_model_cannot_supply_a_file_path():
    """The schema has no path field at all -- the strongest form of the guarantee."""
    assert "file_path" not in BodyProposal.model_fields
    with pytest.raises(ValidationError):
        BodyProposal(
            source_key="s",
            unexpected_body="b",
            action="union",
            target_feeds=["x"],
            rationale="r",
            file_path="../../evil.yml",
        )


# --- applier ------------------------------------------------------------------------------


def test_union_appends_to_the_resolved_feed_and_keeps_comments(evidence, repo):
    plan = RemedyPlan(accepted=[proposal()])
    modified = apply_remedy_plan(
        plan,
        feed_paths=feed_paths_by_slug(repo),
        source_context=SourceContext.from_city(make_city("test-city-council", "City Council")),
        repo_root=repo,
    )
    path = repo / "config" / "feeds" / "test-city-council.yml"
    assert modified == [path]
    text = path.read_text(encoding="utf-8")
    assert yaml.safe_load(text)["source"]["body_any"] == ["Work Session", "Special Meeting"]
    assert "# Council reads the main view." in text


def test_single_uid_inclusion_pins_only_the_observed_guids(evidence, repo):
    plan = RemedyPlan(
        accepted=[
            proposal(
                unexpected_body="TIRZ",
                action="single_uid_inclusion",
                provider_guids=["guid-103"],
            )
        ]
    )
    apply_remedy_plan(
        plan,
        feed_paths=feed_paths_by_slug(repo),
        source_context=SourceContext.from_city(make_city("test-city-council", "City Council")),
        repo_root=repo,
    )
    data = yaml.safe_load(
        (repo / "config" / "feeds" / "test-city-council.yml").read_text(encoding="utf-8")
    )
    assert data["source"]["body_includes"] == [{"provider_guid": "guid-103", "body": "TIRZ"}]


def test_apply_is_idempotent(evidence, repo):
    plan = RemedyPlan(accepted=[proposal()])
    context = SourceContext.from_city(make_city("test-city-council", "City Council"))
    paths = feed_paths_by_slug(repo)
    first = apply_remedy_plan(plan, feed_paths=paths, source_context=context, repo_root=repo)
    second = apply_remedy_plan(plan, feed_paths=paths, source_context=context, repo_root=repo)
    assert first and second == []
    data = yaml.safe_load(
        (repo / "config" / "feeds" / "test-city-council.yml").read_text(encoding="utf-8")
    )
    assert data["source"]["body_any"].count("Special Meeting") == 1


def test_new_feed_copies_the_sibling_transport_so_the_source_key_matches(evidence, repo):
    plan = RemedyPlan(
        accepted=[
            proposal(
                unexpected_body="TIRZ",
                action="new_feed",
                target_feeds=[],
                new_feed_slug="test-city-tirz-board",
                new_feed_title="Test City: TIRZ Board",
                new_feed_description="TIRZ Board meetings.",
            )
        ]
    )
    council = make_city("test-city-council", "City Council", body_any=["Work Session"])
    apply_remedy_plan(
        plan,
        feed_paths=feed_paths_by_slug(repo),
        source_context=SourceContext.from_city(council),
        repo_root=repo,
    )
    created = repo / "config" / "feeds" / "test-city-tirz-board.yml"
    data = yaml.safe_load(created.read_text(encoding="utf-8"))
    assert data["slug"] == "test-city-tirz-board"
    assert data["source"]["feed_url"] == "https://test.example/feed"
    # The sibling's selectors must not leak onto the new feed.
    assert data["source"]["body"] == "TIRZ"
    assert "body_any" not in data["source"]
    assert "Rationale:" in created.read_text(encoding="utf-8")


def test_apply_never_writes_outside_config_feeds(evidence, repo):
    """A rejected traversal slug produces no file anywhere."""
    plan = validate_proposals(
        RemedyOutput(
            proposals=[
                proposal(action="new_feed", new_feed_slug="../../../tmp/evil", target_feeds=[])
            ]
        ),
        evidence,
        feed_paths_by_slug(repo),
    )
    written = apply_remedy_plan(
        plan,
        feed_paths=feed_paths_by_slug(repo),
        source_context=SourceContext.from_city(make_city("test-city-council", "City Council")),
        repo_root=repo,
    )
    assert written == []
    assert sorted(p.name for p in (repo / "config" / "feeds").glob("*.yml")) == [
        "test-city-council.yml",
        "test-city-library-board.yml",
    ]


# --- classification -----------------------------------------------------------------------


def test_classify_parses_a_fenced_response(evidence):
    payload = {
        "proposals": [
            {
                "source_key": "test-source",
                "unexpected_body": "Special Meeting",
                "action": "union",
                "target_feeds": ["test-city-council"],
                "rationale": "Recurring special session",
            }
        ]
    }

    class FakeBackend:
        def run_inference(self, job):
            assert job.recipe_hash == evidence_recipe_hash(evidence)
            return JobResult(
                task=job.task,
                recipe_hash=job.recipe_hash,
                output={
                    "choices": [{"message": {"content": f"```json\n{json.dumps(payload)}\n```"}}]
                },
                model="gemini/gemini-3.6-flash",
            )

    out = classify_unexpected_bodies(evidence, storage=MemStorage(), backend=FakeBackend())
    assert out.proposals[0].action == "union"


def test_classify_raises_when_the_request_is_deferred(evidence):
    class DeferringBackend:
        def run_inference(self, job):
            return JobHandle(
                backend="litellm",
                task=job.task,
                recipe_hash=job.recipe_hash,
                ref="queued-remedy",
            )

    with pytest.raises(RuntimeError, match="deferred"):
        classify_unexpected_bodies(evidence, storage=MemStorage(), backend=DeferringBackend())


def test_classify_raises_on_empty_content(evidence):
    class EmptyBackend:
        def run_inference(self, job):
            return JobResult(
                task=job.task,
                recipe_hash=job.recipe_hash,
                output={"choices": [{"message": {"content": "   "}}]},
                model="gemini/gemini-3.6-flash",
            )

    with pytest.raises(ValueError, match="empty"):
        classify_unexpected_bodies(evidence, storage=MemStorage(), backend=EmptyBackend())


def test_remedy_models_exist_in_the_route_catalog():
    """An unqualified model name silently matches no route and defers every request."""
    from citypods.audit_remedy import REMEDY_MODELS
    from citypods.compute.llm_policy import ROUTE_CANDIDATES

    assert set(REMEDY_MODELS) <= set(ROUTE_CANDIDATES)


# --- reporting ----------------------------------------------------------------------------


def test_report_lists_accepted_and_rejected(evidence, repo):
    remedy = RemedyOutput(proposals=[proposal(), proposal(unexpected_body="Not Observed")])
    plan = validate_proposals(remedy, evidence, feed_paths_by_slug(repo))
    table = format_remedy_markdown(plan, evidence)
    assert "Special Meeting" in table
    assert "**union**" in table
    assert "Rejected proposals" in table
    assert "was not observed" in table


def test_report_handles_a_fully_rejected_plan(evidence, repo):
    plan = validate_proposals(
        RemedyOutput(proposals=[proposal(unexpected_body="Not Observed")]),
        evidence,
        feed_paths_by_slug(repo),
    )
    assert "_(none accepted)_" in format_remedy_markdown(plan, evidence)


def test_report_escapes_model_text_that_could_break_a_markdown_table(evidence):
    plan = RemedyPlan(
        accepted=[
            proposal(
                unexpected_body="Special | Meeting\n[misleading](https://example.invalid)",
                target_feeds=["test-city-council|other"],
                rationale="line one\r\nline two | `not code`",
            )
        ],
        rejected=[
            # This bypasses validation deliberately: it exercises the reporting boundary itself.
            # A rejected model proposal may include any arbitrary string.
            RejectedProposal(
                proposal=proposal(unexpected_body="Rejected|\n[label](https://example.invalid)"),
                reason="bad | reason\nwith another line",
            )
        ],
    )
    table = format_remedy_markdown(plan, evidence)
    assert "Special \\| Meeting \\[misleading\\](https://example.invalid)" in table
    assert "test-city-council\\|other" in table
    assert "line one  line two \\| \\`not code\\`" in table
    assert "Rejected\\| \\[label\\](https://example.invalid)" in table
    assert "bad \\| reason with another line" in table


def test_feed_paths_by_slug_skips_templates(repo):
    (repo / "config" / "feeds" / "_template.yml").write_text("slug: example\n", encoding="utf-8")
    assert set(feed_paths_by_slug(repo)) == {
        "test-city-council",
        "test-city-library-board",
    }
