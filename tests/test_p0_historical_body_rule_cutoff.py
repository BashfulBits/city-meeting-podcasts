"""Protect the maintainer-approved P0 body-rule cutoff and its historical matches.

The evidence CSVs are a frozen source/title baseline. These tests ensure that rows selected for a
reusable feed rule match that feed by source and body label, while rows explicitly marked
"not to pursue" stay outside the newly selected body rules. No UID is used as a routing selector.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

from citypods.bodies import body_key, matches, source_body_filter
from citypods.config import load_city_configs, load_site_config
from citypods.records import source_key

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "review" / "evidence"
APPROVED_RULE_ACTIONS = {
    "approved-body-rule-local",
    "approved-new-body-rule-local",
    "selected-public-information",
    "selected-curated-series",
    "selected-dallas-bond-family",
}


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _feed_map() -> dict[str, object]:
    return {
        feed.slug: feed
        for feed in load_city_configs(
            ROOT / "config", load_site_config(ROOT / "config/site_config.yml")
        )
    }


def _matches_feed(body: str, feed: object) -> bool:
    if any(body_key(term) in body_key(body) for term in feed.body_exclude):
        return False
    return matches(body, source_body_filter(feed.source))


def test_historical_body_rule_rows_match_their_source_scoped_feed() -> None:
    """Every selected source/record label has a reusable selector in the named feed."""
    dispositions = _read_csv(EVIDENCE / "p0-final-disposition-2026-10-06.csv")
    replay = _read_csv(EVIDENCE / "historical-selector-replay-2026-10-06.csv")
    replay_by_id: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in replay:
        replay_by_id[row["inventory_id"]].append(row)
    feeds = _feed_map()

    approved = [
        row for row in dispositions if row["maintainer_final_action"] in APPROVED_RULE_ACTIONS
    ]
    assert approved
    for row in approved:
        feed = feeds[row["maintainer_final_destination"]]
        source_rows = replay_by_id[row["id"]]
        assert source_rows, row["id"]
        for source_row in source_rows:
            assert source_key(feed) == source_row["source_key"]
            assert _matches_feed(source_row["record_body"], feed), (
                row["id"],
                source_row["uid"],
                row["maintainer_final_destination"],
                source_row["record_body"],
            )


def test_not_pursued_rows_are_not_captured_by_new_body_rules() -> None:
    """The cutoff does not silently route the low-count remainder by substring accident."""
    dispositions = _read_csv(EVIDENCE / "p0-final-disposition-2026-10-06.csv")
    replay = _read_csv(EVIDENCE / "historical-selector-replay-2026-10-06.csv")
    replay_by_id: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in replay:
        replay_by_id[row["inventory_id"]].append(row)
    feeds = _feed_map()
    approved_feeds = {
        slug: feed
        for slug, feed in feeds.items()
        if any(
            row["maintainer_final_destination"] == slug
            and row["maintainer_final_action"] in APPROVED_RULE_ACTIONS
            for row in dispositions
        )
    }

    not_pursued = [
        row
        for row in dispositions
        if row["maintainer_final_action"] == "not-to-pursue-maintainer-directed"
    ]
    assert len(not_pursued) == 162
    for row in not_pursued:
        source_rows = replay_by_id[row["id"]]
        assert source_rows, row["id"]
        for source_row in source_rows:
            for slug, feed in approved_feeds.items():
                if source_key(feed) == source_row["source_key"]:
                    assert not _matches_feed(source_row["record_body"], feed), (
                        row["id"],
                        source_row["uid"],
                        row["body"],
                        slug,
                        source_row["record_body"],
                    )


def test_frozen_p0_disposition_and_replay_csv_row_totals() -> None:
    """Detect accidental truncation of the frozen pattern list or its source/UID replay."""
    dispositions = _read_csv(EVIDENCE / "p0-final-disposition-2026-10-06.csv")
    replay = _read_csv(EVIDENCE / "historical-selector-replay-2026-10-06.csv")

    assert len(dispositions) == 680
    assert len(replay) == 2028


def test_new_body_rules_meet_the_five_uid_cutoff() -> None:
    """New bodies meet the cutoff, except Denton Bond Oversight already approved earlier."""
    dispositions = _read_csv(EVIDENCE / "p0-final-disposition-2026-10-06.csv")
    replay = _read_csv(EVIDENCE / "historical-selector-replay-2026-10-06.csv")
    uids_by_id: dict[str, set[str]] = defaultdict(set)
    for row in replay:
        uids_by_id[row["inventory_id"]].add(row["uid"])

    new_body_feeds = {
        row["maintainer_final_destination"]
        for row in dispositions
        if row["maintainer_final_action"] == "approved-new-body-rule-local"
    }
    uids_by_feed: dict[str, set[str]] = defaultdict(set)
    for row in dispositions:
        if row["maintainer_final_destination"] in new_body_feeds:
            uids_by_feed[row["maintainer_final_destination"]].update(uids_by_id[row["id"]])

    assert uids_by_feed
    for slug, uids in uids_by_feed.items():
        if slug == "denton-tx-bond-oversight-committee":
            assert len(uids) == 4
        else:
            assert len(uids) >= 5, (slug, len(uids))


def test_aggregate_feed_exclusions_keep_bond_and_raw_footage_separate() -> None:
    """Dallas bond proceedings stay in their feed; raw-footage clips are not meetings."""
    feeds = _feed_map()
    assert not _matches_feed(
        "Citizens Bond Task Force: Town Hall Meeting",
        feeds["dallas-tx-public-info-meetings"],
    )
    assert _matches_feed(
        "Citizens Bond Task Force: Town Hall Meeting",
        feeds["dallas-tx-bond-program-meetings"],
    )
    assert not _matches_feed(
        "Raw Footage of Chief Halstead's Press Conference regarding recently arrested employee",
        feeds["fort-worth-tx-public-info-meetings"],
    )


def test_public_info_and_charter_series_route_to_one_intended_feed() -> None:
    feeds = _feed_map()
    charter_community = "Charter Review Commission: Community Meeting"
    assert _matches_feed(charter_community, feeds["dallas-tx-public-info-meetings"])
    assert not _matches_feed(charter_community, feeds["dallas-tx-charter-review-commission"])
    assert _matches_feed(
        "Redistricting Public Meeting Redistricting Public Meeting",
        feeds["fort-worth-tx-public-info-meetings"],
    )
    assert _matches_feed(
        "State Of the City 2022 State Of the City 2022",
        feeds["fort-worth-tx-public-info-meetings"],
    )
