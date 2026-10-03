"""Replay every retained and provider row against the verified Pflugerville BOA policy."""

import copy
import json
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from citypods.bodies import filter_by_body, record_matches_body, source_body_filter
from citypods.config import load_city_configs
from citypods.models import Episode
from citypods.records import assign_uids, source_key

ROOT = Path(__file__).resolve().parents[1]
CENSUS = json.loads((ROOT / "tests/fixtures/pflugerville_boa_coverage/archive.json").read_text())
SLUG = "pflugerville-tx-board-of-adjustment"


def _episode(row):
    return Episode(
        guid=row["provider_guid"],
        title=row["title"],
        body=row["body"],
        published=datetime.fromisoformat(row["published"]),
        video_url=row["provider_guid"],
    )


def test_pflugerville_boa_replays_every_retained_and_available_provider_row():
    owner = next(feed for feed in load_city_configs(ROOT / "config", {}) if feed.slug == SLUG)
    selector = source_body_filter(owner.source)
    provider = CENSUS["provider_episodes"]
    retained = CENSUS["persisted_records"]
    for row in provider + retained:
        assert record_matches_body(row, selector) == row["expected_boa"], row
    positives = [row for row in retained if row["expected_boa"]]
    assert len(provider) == CENSUS["provider_rows"] == 205
    assert len(retained) == CENSUS["persisted_rows"] == 948
    assert len(positives) == len({row["provider_guid"] for row in positives}) == 10
    assert len({row["uid"] for row in positives}) == 10
    assert len({row["provider_guid"] for row in provider + retained}) == 832
    assert sum(row["expected_boa"] for row in provider) == 1
    assert {row["uid"] for row in positives} == {row["uid"] for row in CENSUS["positive_episodes"]}
    for label in [
        "Planning and Zoning Commission",
        "City Council",
        "Board of Adjustment Staff Training",
        "Board of Adjustment and Appeals",
    ]:
        assert not record_matches_body({"body": label}, selector)


def test_pflugerville_boa_source_identity_uid_and_provider_order_are_preserved():
    feeds = load_city_configs(ROOT / "config", {})
    owner = next(feed for feed in feeds if feed.slug == SLUG)
    combined = next(feed for feed in feeds if feed.slug == "pflugerville-tx")
    assert source_key(owner) == source_key(combined) == CENSUS["source_key"]
    assert owner.podcast_author == combined.podcast_author
    episodes = [_episode(row) for row in CENSUS["provider_episodes"]]
    original = copy.deepcopy(episodes)
    assign_uids(combined, original)
    assign_uids(owner, episodes)
    assert [(ep.guid, ep.uid) for ep in episodes] == [(ep.guid, ep.uid) for ep in original]
    selected = filter_by_body(episodes, source_body_filter(owner.source))
    assert [ep.guid for ep in selected] == [
        row["provider_guid"] for row in CENSUS["provider_episodes"] if row["expected_boa"]
    ]
    for row in CENSUS["positive_episodes"]:
        prior, current = _episode(row), _episode(row)
        assign_uids(combined, [prior])
        assign_uids(owner, [current])
        assert prior.uid == current.uid == row["uid"]
        clip = parse_qs(urlsplit(row["provider_guid"]).query)["clip_id"][0]
        evidence = row["meeting_details_evidence"]
        assert "Meeting Name: Board of Adjustment" in evidence["source_span"]
        assert f"ID1={clip}&amp;" in evidence["video_node"]
        for slug in [
            "pflugerville-tx-city-council",
            "pflugerville-tx-planning-and-zoning-commission",
        ]:
            other = next(feed for feed in feeds if feed.slug == slug)
            assert not record_matches_body(row, source_body_filter(other.source))


def test_pflugerville_boa_remedy_policy_blocks_duplicate_and_topic_assignment():
    from citypods.audit_remedy import (
        BodyProposal,
        _aggregate_policy_reason,
        _target_feed_is_compatible,
    )

    path = ROOT / f"config/feeds/{SLUG}.yml"
    proposal = BodyProposal(
        source_key=CENSUS["source_key"],
        unexpected_body="Board of Adjustment",
        action="union",
        target_feeds=[SLUG],
        rationale="Official meeting identity and video binding",
    )
    assert _target_feed_is_compatible(proposal.unexpected_body, path)
    assert not _aggregate_policy_reason(proposal, {SLUG}, {SLUG: path})
    proposal.action = "new_feed"
    proposal.new_feed_slug = "pflugerville-tx-another-boa"
    proposal.new_feed_title = "Another BOA"
    assert "forbids" in _aggregate_policy_reason(proposal, {SLUG}, {SLUG: path})
    proposal.action = "union"
    proposal.unexpected_body = "Board of Adjustment Staff Training"
    assert not _target_feed_is_compatible(proposal.unexpected_body, path)
    assert _aggregate_policy_reason(proposal, {SLUG}, {SLUG: path})


def test_pflugerville_boa_evaluation_cases_are_traceable_regressions_not_admission_truth():
    from citypods.remedy_evaluation import compare_results, load_cases

    case_root = ROOT / "evals/remedy/policies/pflugerville-boa"
    dataset = load_cases(case_root / "manifest.json", case_root / "gold.json")
    assert len(dataset.manifest.cases) == 15
    assert dataset.manifest.split == "regression"
    assert all(truth.provenance == "seed" for truth in dataset.gold.cases.values())
    assert {case.recordings[0].uid for case in dataset.manifest.cases[:10]} == {
        row["uid"] for row in CENSUS["positive_episodes"]
    }
    for mode in ("claim_support", "blind_owner"):
        report = compare_results([], dataset.gold, manifest=dataset.manifest, mode=mode)
        assert report.admission_eligible_cases == 0
        assert report.counts["correct"] == report.owner_selection["correct"] == 0
