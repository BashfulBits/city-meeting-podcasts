"""Unit tests for citypods.audit_remedy.

The emphasis is the validation boundary: the model's output is untrusted, so the tests that
matter most are the ones proving a hostile or confused proposal cannot reach the filesystem.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
import yaml
from pydantic import BaseModel, ValidationError

from citypods.audit import collect_unexpected_bodies
from citypods.audit_remedy import (
    DECISION_CONTRACT,
    EVIDENCE_TOKEN_BUDGET,
    REMEDY_CONTRACT,
    BodyDecisions,
    BodyProposal,
    RejectedProposal,
    RemedyOutput,
    RemedyPlan,
    SourceContext,
    _compact_evidence,
    apply_remedy_plan,
    classify_unexpected_bodies,
    ensure_remedy_contract,
    evidence_recipe_hash,
    feed_paths_by_slug,
    format_remedy_markdown,
    gather_unexpected_body_evidence,
    remedy_batches,
    safe_classification_error,
    validate_proposals,
)
from citypods.compute.base import JobResult
from citypods.compute.llm_policy import estimate_tokens
from citypods.compute.structured import register_response_model
from citypods.models import City, Episode


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


def decision_payload(evidence):
    return {
        "proposals": [
            {
                "finding_id": f"f{i}",
                "action": "union",
                "target_feeds": ["test-city-council"],
                "rationale": "Council session",
            }
            for i, _ in enumerate(evidence["unexpected_findings"])
        ]
    }


class ReplyBackend:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.jobs = []

    def run_immediate(self, job):
        self.jobs.append(job)
        return JobResult(
            task=job.task,
            recipe_hash=job.recipe_hash,
            output={"choices": [{"message": {"content": next(self.replies)}}]},
            model="gemini/gemini-3.6-flash",
        )


def test_classify_direct_contract_and_resolves_local_identifiers(evidence):
    payload = decision_payload(evidence)
    payload["proposals"][0].update(action="single_uid_inclusion", all_observed_episodes=True)
    backend = ReplyBackend([f"```json\n{json.dumps(payload)}\n```"])
    out = classify_unexpected_bodies(evidence, backend=backend)
    job = backend.jobs[0]
    policy = job.inputs["llm_policy"]
    assert policy.require_direct and not policy.queue_only and not policy.allow_dispatch_overflow
    assert job.inputs["structured_output"] == DECISION_CONTRACT
    assert job.inputs["timeout"] <= 45
    assert job.inputs["num_retries"] == 0
    assert out.proposals[0].source_key == evidence["source_key"]
    assert out.proposals[0].provider_guids == [
        e["provider_guid"] for e in evidence["unexpected_findings"][0]["episodes"]
    ]


def test_classify_repairs_bad_json_and_evidence_ids_in_same_process(evidence):
    valid = decision_payload(evidence)
    for invalid in ("not json", json.dumps({"proposals": []})):
        backend = ReplyBackend([invalid, json.dumps(valid)])
        result = classify_unexpected_bodies(evidence, backend=backend)
        assert len(result.proposals) == len(evidence["unexpected_findings"])
        assert len(backend.jobs) == 2
        assert "Correct the response" in backend.jobs[-1].inputs["messages"][-1]["content"]


def test_classify_rejects_invented_episode_ids_after_one_repair(evidence):
    payload = decision_payload(evidence)
    payload["proposals"][0].update(action="single_uid_inclusion", episode_ids=["e99999"])
    backend = ReplyBackend([json.dumps(payload)] * 2)
    with pytest.raises(ValueError, match="episode_ids"):
        classify_unexpected_bodies(evidence, backend=backend)
    assert len(backend.jobs) == 2


def test_classify_manual_review_preserves_a_reason_for_every_label(evidence):
    payload = {
        "proposals": [
            {"finding_id": f"f{i}", "action": "manual_review", "rationale": "Owner unclear"}
            for i in range(len(evidence["unexpected_findings"]))
        ]
    }
    result = classify_unexpected_bodies(evidence, backend=ReplyBackend([json.dumps(payload)]))
    assert not result.proposals
    assert len(result.unresolved) == len(evidence["unexpected_findings"])


def test_ensure_remedy_contract():
    model = ensure_remedy_contract()
    assert model is RemedyOutput
    assert ensure_remedy_contract() is RemedyOutput


def test_ensure_remedy_contract_rejects_conflict():
    class IncompatibleModel(BaseModel):
        foo: str

    with pytest.raises(ValueError, match="conflicting structured-output contract"):
        register_response_model(REMEDY_CONTRACT, IncompatibleModel)


def test_classify_does_not_call_backend_after_shared_deadline(evidence):
    backend = ReplyBackend([])
    with pytest.raises(TimeoutError):
        classify_unexpected_bodies(
            evidence, backend=backend, deadline_at=datetime(2020, 1, 1, tzinfo=UTC)
        )
    assert not backend.jobs


def test_wire_schema_requires_action_specific_fields():
    for fields in (
        {"action": "single_uid_inclusion", "target_feeds": ["council"]},
        {"action": "new_feed", "new_feed_slug": "board"},
    ):
        with pytest.raises(ValidationError):
            BodyDecisions.model_validate(
                {"proposals": [{"finding_id": "f0", "rationale": "reason", **fields}]}
            )


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


def _big_evidence(episode_count: int) -> dict:
    return {
        "source_key": "test-source",
        "city": {"slug": "test-city", "name": "Test City"},
        "existing_feeds": [],
        "historical_archive": {"total_archived_count": 0, "known_archived_bodies": []},
        "unexpected_findings": [
            {
                "unexpected_body": "Special Meeting",
                "count": episode_count,
                "date_range": {"earliest": "2020-01-01", "latest": "2026-01-01"},
                "episodes": [
                    {
                        "provider_guid": f"guid-{i}",
                        "published": "2020-01-01",
                        "title": "A" * 200,  # padding so this fixture is realistically oversized
                        "body": "Special Meeting",
                    }
                    for i in range(episode_count)
                ],
            }
        ],
    }


def test_batches_preserve_all_full_evidence_and_bound_model_input():
    import copy

    evidence = _big_evidence(5000)
    evidence["unexpected_findings"] *= 30
    original = copy.deepcopy(evidence)
    batches = list(remedy_batches(evidence))
    assert sum(len(b["unexpected_findings"]) for b in batches) == 30
    assert evidence == original
    for batch in batches:
        compact = _compact_evidence(batch)
        assert estimate_tokens([{"content": json.dumps(compact)}]) <= EVIDENCE_TOKEN_BUDGET
        assert all(len(f["episode_samples"]) <= 6 for f in compact["unexpected_findings"])
        assert all(len(f["episodes"]) == 5000 for f in batch["unexpected_findings"])


def test_single_oversized_finding_is_reported_not_dropped(evidence):
    evidence["unexpected_findings"][0]["unexpected_body"] = "x" * 100000
    batches = list(remedy_batches(evidence))
    assert sum(len(b["unexpected_findings"]) for b in batches) == len(
        evidence["unexpected_findings"]
    )
    backend = ReplyBackend([])
    with pytest.raises(ValueError, match="budget"):
        classify_unexpected_bodies(batches[0], backend=backend)
    assert not backend.jobs


def test_recipe_changes_with_prompt_or_schema_version(evidence, monkeypatch):
    import citypods.audit_remedy as remedy

    before = evidence_recipe_hash(evidence)
    monkeypatch.setattr(remedy, "REMEDY_VERSION", "next-contract")
    assert evidence_recipe_hash(evidence) != before


def test_safe_diagnostics_do_not_echo_invalid_model_text():
    try:
        BodyDecisions.model_validate({"proposals": "secret-provider-text"})
    except ValidationError as exc:
        diagnostic = safe_classification_error(exc)
    assert "secret-provider-text" not in diagnostic
    assert "proposals" in diagnostic and "list_type" in diagnostic
