"""Historical Arlington named bodies retain their own subscription and source namespace."""

import copy
import json
from datetime import datetime
from pathlib import Path

import pytest

from citypods.bodies import matches, source_body_filter, source_body_inclusions
from citypods.config import load_city_configs
from citypods.models import Episode
from citypods.records import assign_uids, source_key

ROOT = Path(__file__).resolve().parents[1]
CENSUS = json.loads((ROOT / "tests/fixtures/arlington_coverage/archive.json").read_text())


@pytest.mark.parametrize("slug", ["housing-finance-corporation", "zoning-board-of-adjustment"])
def test_arlington_named_body_complete_provider_label_replay(slug):
    feeds = load_city_configs(ROOT / "config", {})
    owner = next(feed for feed in feeds if feed.slug == f"arlington-tx-{slug}")
    selector = source_body_filter(owner.source)
    selected_guids = []
    for row in CENSUS["labels"]:
        selected = matches(row["body"], selector)
        assert selected == (row["expected_feed"] == owner.slug), row["body"]
        if selected:
            selected_guids.extend(row["provider_guids"])
    assert len(selected_guids) == 1
    persisted_selected = []
    for record in CENSUS["persisted_records"]:
        selected = matches(record["body"], selector)
        assert selected == (record["expected_feed"] == owner.slug), record["body"]
        if selected:
            persisted_selected.append(record["uid"])
    assert len(persisted_selected) == 1
    assert len(CENSUS["persisted_records"]) == CENSUS["persisted_record_count"] == 1515
    all_guids = [guid for row in CENSUS["labels"] for guid in row["provider_guids"]]
    assert len(all_guids) == CENSUS["total_provider_rows"] == 1506
    assert len(set(all_guids)) == CENSUS["unique_provider_guids"] == 1506
    assert source_key(owner) == CENSUS["source_key"]
    assert not source_body_inclusions(owner.source)
    for parent_slug in ["council", "planning-and-zoning-commission"]:
        parent = next(feed for feed in feeds if feed.slug == f"arlington-tx-{parent_slug}")
        assert source_key(parent) == source_key(owner)
        assert parent.podcast_author == owner.podcast_author
        for row in CENSUS["labels"]:
            if row["expected_feed"] == owner.slug:
                assert not matches(row["body"], source_body_filter(parent.source))


def test_arlington_subscription_changes_do_not_change_episode_identity():
    feeds = load_city_configs(ROOT / "config", {})
    parent = next(feed for feed in feeds if feed.slug == "arlington-tx-council")
    for row in CENSUS["positive_episodes"]:
        owner = next(
            feed
            for feed in feeds
            if feed.city_entity == "arlington-tx"
            and matches(row["body"], source_body_filter(feed.source))
        )
        episode = Episode(
            guid=row["provider_guid"],
            title=row["title"],
            published=datetime.fromisoformat(row["published"]),
            video_url=row["links"]["canonical_video"],
            body=row["body"],
        )
        prior = copy.deepcopy(episode)
        assign_uids(parent, [prior])
        assign_uids(owner, [episode])
        assert episode.uid == prior.uid == row["uid"]
        assert episode.guid == row["provider_guid"]
        assert episode.title == row["title"]
        assert episode.body == row["body"]


def test_arlington_named_body_rules_generalize_session_labels_without_topic_capture():
    feeds = load_city_configs(ROOT / "config", {})
    hfc = next(feed for feed in feeds if feed.slug == "arlington-tx-housing-finance-corporation")
    zba = next(feed for feed in feeds if feed.slug == "arlington-tx-zoning-board-of-adjustment")
    for label in [
        "Arlington Housing Finance Corporation Board Meeting",
        "Arlington Housing Finance Corporation Board of Directors Special Meeting",
    ]:
        assert matches(label, source_body_filter(hfc.source))
    for label in ["ZBA Special Session", "Zoning Board of Adjustment Regular Meeting"]:
        assert matches(label, source_body_filter(zba.source))
    for label in [
        "City Council discussion of Arlington Housing Finance Corporation",
        "Planning and Zoning Commission ZBA report",
        "Fort Worth Zoning Board of Adjustment",
        "Housing Advisory Board",
        "Mid-Cities Joint Airport Zoning Board",
        "Empty",
    ]:
        assert not matches(label, source_body_filter(hfc.source))
        assert not matches(label, source_body_filter(zba.source))


@pytest.mark.parametrize("slug", ["housing-finance-corporation", "zoning-board-of-adjustment"])
def test_arlington_remedy_uses_named_owner_and_holds_duplicate_feeds(slug):
    from citypods.audit_remedy import (
        BodyProposal,
        _aggregate_policy_reason,
        _target_feed_is_compatible,
    )

    owner_slug = f"arlington-tx-{slug}"
    path = ROOT / f"config/feeds/{owner_slug}.yml"
    paths = {owner_slug: path}
    label = next(row["body"] for row in CENSUS["labels"] if row["expected_feed"] == owner_slug)
    proposal = BodyProposal(
        source_key=CENSUS["source_key"],
        unexpected_body=label,
        action="union",
        target_feeds=[owner_slug],
        rationale="Verified official named-body identity",
    )
    assert _target_feed_is_compatible(label, path)
    assert not _aggregate_policy_reason(proposal, {owner_slug}, paths)
    proposal.target_feeds = ["arlington-tx-council"]
    assert "requires the owning" in _aggregate_policy_reason(proposal, {owner_slug}, paths)
    proposal.action = "new_feed"
    proposal.new_feed_slug = "arlington-tx-duplicate-session"
    proposal.new_feed_title = label
    assert "forbids" in _aggregate_policy_reason(proposal, {owner_slug}, paths)
    proposal.action = "union"
    proposal.target_feeds = [owner_slug]
    proposal.unexpected_body = "City Council discussion of " + label
    assert not _target_feed_is_compatible(proposal.unexpected_body, path)
    assert _aggregate_policy_reason(proposal, {owner_slug}, paths)
