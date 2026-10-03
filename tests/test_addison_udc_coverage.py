"""Replay retained and available Addison recordings against the UDC committee policy."""

import copy
import json
from datetime import datetime
from pathlib import Path

from citypods.bodies import matches, record_matches_body, source_body_filter, source_body_inclusions
from citypods.config import load_city_configs
from citypods.models import Episode
from citypods.records import assign_uids, source_key

ROOT = Path(__file__).resolve().parents[1]
CENSUS = json.loads((ROOT / "tests/fixtures/addison_udc_coverage/archive.json").read_text())
SLUG = "addison-tx-unified-development-code-advisory-committee"


def test_addison_udc_covers_every_available_committee_recording_and_no_other_label():
    owner = next(feed for feed in load_city_configs(ROOT / "config", {}) if feed.slug == SLUG)
    selector = source_body_filter(owner.source)
    observed = []
    for row in CENSUS["provider_labels"]:
        selected = matches(row["body"], selector)
        assert selected == row["expected_udc"], row["body"]
        if selected:
            observed.extend(row["provider_guids"])
    stored = []
    for record in CENSUS["persisted_records"]:
        selected = record_matches_body(record, selector)
        assert selected == record["expected_udc"], record["body"]
        if selected:
            stored.append(record)
    assert len(observed) == len(stored) == 3
    assert len(set(observed)) == len({record["provider_guid"] for record in stored}) == 3
    assert set(observed) == {record["provider_guid"] for record in stored}
    assert sum(len(row["provider_guids"]) for row in CENSUS["provider_labels"]) == 819
    assert len(CENSUS["persisted_records"]) == CENSUS["persisted_rows"] == 822
    assert CENSUS["unique_union_provider_guids"] == 819
    assert not matches("Unified Development Code (UDC) Advisory Committee Open House", selector)
    assert not matches("Council Discussion of Unified Development Code", selector)
    assert not matches("UDC Advisory Committee Staff Training", selector)


def test_addison_udc_preserves_existing_source_namespace_and_stored_episode_uids():
    feeds = load_city_configs(ROOT / "config", {})
    owner = next(feed for feed in feeds if feed.slug == SLUG)
    council = next(feed for feed in feeds if feed.slug == "addison-tx-city-council")
    assert source_key(owner) == source_key(council) == CENSUS["source_key"]
    assert owner.podcast_author == council.podcast_author
    assert not source_body_inclusions(owner.source)
    persisted = [
        record
        for record in CENSUS["persisted_records"]
        if record_matches_body(record, source_body_filter(owner.source))
    ]
    assert len(persisted) == len({record["provider_guid"] for record in persisted})
    persisted_uids = {record["provider_guid"]: record["uid"] for record in persisted}
    assert set(persisted_uids) == {row["provider_guid"] for row in CENSUS["positive_episodes"]}
    for row in CENSUS["positive_episodes"]:
        episode = Episode(
            guid=row["provider_guid"],
            title=row["title"],
            body=row["body"],
            published=datetime.fromisoformat(row["published"]),
            video_url=row["links"]["canonical_video"],
        )
        prior = copy.deepcopy(episode)
        assign_uids(council, [prior])
        assign_uids(owner, [episode])
        assert episode.uid == prior.uid == row["uid"] == persisted_uids[row["provider_guid"]]
        for slug in [
            "addison-tx-city-council",
            "addison-tx-planning-and-zoning-commission",
            "addison-tx-town-meetings",
        ]:
            parent = next(feed for feed in feeds if feed.slug == slug)
            assert not record_matches_body(
                row, source_body_filter(parent.source), source_body_inclusions(parent.source)
            )


def test_addison_udc_remedy_policy_blocks_duplicate_feed_and_topic_ownership():
    from citypods.audit_remedy import (
        BodyProposal,
        _aggregate_policy_reason,
        _target_feed_is_compatible,
    )

    path = ROOT / f"config/feeds/{SLUG}.yml"
    label = CENSUS["positive_episodes"][0]["body"]
    proposal = BodyProposal(
        source_key=CENSUS["source_key"],
        unexpected_body=label,
        action="union",
        target_feeds=[SLUG],
        rationale="Official committee identity",
    )
    assert _target_feed_is_compatible(label, path)
    assert not _aggregate_policy_reason(proposal, {SLUG}, {SLUG: path})
    proposal.action = "new_feed"
    proposal.new_feed_slug = "addison-tx-another-udc-title"
    proposal.new_feed_title = label
    assert "forbids" in _aggregate_policy_reason(proposal, {SLUG}, {SLUG: path})
    proposal.action = "union"
    proposal.unexpected_body = label + " Open House"
    assert not _target_feed_is_compatible(proposal.unexpected_body, path)
    assert _aggregate_policy_reason(proposal, {SLUG}, {SLUG: path})
