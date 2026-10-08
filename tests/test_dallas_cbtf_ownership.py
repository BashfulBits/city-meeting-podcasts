"""A verified task-force proceeding retains recording identity in its own feed."""

import copy
import json
from pathlib import Path

import pytest

from citypods.bodies import (
    is_excluded,
    record_matches_body,
    source_body_filter,
    source_body_inclusions,
)
from citypods.config import load_city_configs
from citypods.feeds import build_rss
from citypods.records import record_to_episode


def test_actual_config_keeps_cbtf_only_in_its_task_force_feed():
    """Actual selectors retain original RSS identity and reject Council leakage."""
    root = Path(__file__).resolve().parents[1]
    cities = load_city_configs(root / "config", {})
    council = next(c for c in cities if c.slug == "dallas-tx-city-council")
    commission = next(c for c in cities if c.slug == "dallas-tx-2024-community-bond-task-force")
    record = json.loads((root / "tests/fixtures/dallas-cbtf-retained.json").read_text())
    original = copy.deepcopy(record)
    assert not record_matches_body(
        record, source_body_filter(council.source), source_body_inclusions(council.source)
    )
    assert record_matches_body(
        record, source_body_filter(commission.source), source_body_inclusions(commission.source)
    )
    rss = build_rss(
        commission, [record_to_episode(record)], "audio", "https://www.citymeetings.fyi"
    )
    assert record["uid"] in rss
    assert record["audio"]["url"] in rss
    negative = dict(record, provider_guid="unrelated")
    assert not record_matches_body(
        negative, source_body_filter(commission.source), source_body_inclusions(commission.source)
    )
    assert record == original


@pytest.mark.parametrize(
    "uid",
    ["96c10f4f75706719", "aaf0ad8eaabe68a5", "130a7ebded8e0e1d", "cba1e023051d7c94"],
)
def test_additional_verified_cbtf_record_keeps_original_identity(uid):
    """Each exact recording gains coverage without broadening body-name admission."""
    root = Path(__file__).resolve().parents[1]
    cities = load_city_configs(root / "config", {})
    council = next(c for c in cities if c.slug == "dallas-tx-city-council")
    task_force = next(c for c in cities if c.slug == "dallas-tx-2024-community-bond-task-force")
    records = json.loads((root / "tests/fixtures/dallas-cbtf-additional-retained.json").read_text())
    record = next(r for r in records if r["uid"] == uid)
    original = copy.deepcopy(record)
    assert record_matches_body(
        record, source_body_filter(task_force.source), source_body_inclusions(task_force.source)
    )
    assert not record_matches_body(
        record, source_body_filter(council.source), source_body_inclusions(council.source)
    )
    rss = build_rss(
        task_force, [record_to_episode(record)], "audio", "https://www.citymeetings.fyi"
    )
    assert uid in rss
    assert record["audio"]["url"] in rss
    negative = dict(record, provider_guid="unverified-recording")
    assert not record_matches_body(
        negative, source_body_filter(task_force.source), source_body_inclusions(task_force.source)
    )
    assert record == original


@pytest.mark.parametrize("index", [0, 1, 2])
def test_three_verified_cbtf_proceedings_preserve_identity(index):
    """Two ambiguous labels require GUIDs; the complete chairs alias is reusable."""
    root = Path(__file__).resolve().parents[1]
    cities = load_city_configs(root / "config", {})
    council = next(c for c in cities if c.slug == "dallas-tx-city-council")
    task_force = next(c for c in cities if c.slug == "dallas-tx-2024-community-bond-task-force")
    records = json.loads((root / "tests/fixtures/dallas-cbtf-three-retained.json").read_text())
    record = records[index]
    original = copy.deepcopy(record)

    def matches(candidate):
        return record_matches_body(
            candidate,
            source_body_filter(task_force.source),
            source_body_inclusions(task_force.source),
        )

    assert matches(record)
    assert not record_matches_body(
        record, source_body_filter(council.source), source_body_inclusions(council.source)
    )
    rss = build_rss(
        task_force, [record_to_episode(record)], "audio", "https://www.citymeetings.fyi"
    )
    assert record["uid"] in rss
    assert record["audio"]["url"] in rss
    assert matches(dict(record, provider_guid="future-recording")) is (index == 2)
    for body in (record["body"] + " Public Town Hall", "CBTF and Subcommittee Chairs Meeting"):
        assert not matches(dict(record, provider_guid="future-recording", body=body))
    if index < 2:
        unproven_guid = ("unverified-recording", "272574")[index]
        assert not matches(dict(record, provider_guid=unproven_guid))
    assert record == original


def test_verified_may25_cbtf_preserves_aggregate_and_task_force_coverage():
    """The aggregate rule adds the same unchanged recording to the bond-program feed."""
    root = Path(__file__).resolve().parents[1]
    cities = load_city_configs(root / "config", {})
    record = json.loads((root / "tests/fixtures/dallas-cbtf-may25-retained.json").read_text())
    original = copy.deepcopy(record)
    task_force = next(c for c in cities if c.slug == "dallas-tx-2024-community-bond-task-force")
    bond = next(c for c in cities if c.slug == "dallas-tx-bond-program-meetings")

    def holders(candidate):
        return {
            c.slug
            for c in cities
            if c.city_entity == "dallas-tx"
            and record_matches_body(
                candidate, source_body_filter(c.source), source_body_inclusions(c.source)
            )
            and (
                not is_excluded(candidate.get("body"), c.body_exclude)
                or any(
                    inclusion.provider_guid == candidate.get("provider_guid")
                    for inclusion in source_body_inclusions(c.source)
                )
            )
        }

    assert holders(record) == {task_force.slug, bond.slug}
    assert holders(dict(record, provider_guid="unverified-recording")) == {bond.slug}
    sep26 = next(
        row
        for row in json.loads(
            (root / "tests/fixtures/dallas-bond-program-cbtf-retained.json").read_text()
        )["episodes"]
        if row["provider_guid"] == "272574"
    )
    assert holders(sep26) == {bond.slug}
    assert record["source_chapters"] == [
        {"start": 374, "end": 8578, "title": "2024 Bond CBTF Meeting on May 25, 2023."}
    ]
    rss = build_rss(
        task_force, [record_to_episode(record)], "audio", "https://www.citymeetings.fyi"
    )
    assert record["uid"] in rss
    assert record["audio"]["url"] in rss
    assert record == original


def test_sep26_town_hall_remains_bond_only():
    """Keep the recording in Bond without treating public comment as Public Info."""
    root = Path(__file__).resolve().parents[1]
    cities = load_city_configs(root / "config", {})
    feeds = {city.slug: city for city in cities if city.city_entity == "dallas-tx"}
    bond = feeds["dallas-tx-bond-program-meetings"]
    public_info = feeds["dallas-tx-public-info-meetings"]
    town_hall = json.loads(
        (root / "tests/fixtures/dallas-bond-public-info-town-hall.json").read_text()
    )
    september19 = next(
        record
        for record in json.loads(
            (root / "tests/fixtures/dallas-cbtf-three-retained.json").read_text()
        )
        if record["provider_guid"] == "272005"
    )
    original_town_hall = copy.deepcopy(town_hall)
    original_september19 = copy.deepcopy(september19)

    def matches_feed(record, city):
        """Apply the same source selectors the feed uses when routing records."""
        return record_matches_body(
            record,
            source_body_filter(city.source),
            source_body_inclusions(city.source),
        )

    assert matches_feed(town_hall, bond)
    assert not matches_feed(town_hall, public_info)
    assert not matches_feed(september19, public_info)
    assert matches_feed(september19, bond)

    bond_rss = build_rss(
        bond, [record_to_episode(town_hall)], "audio", "https://www.citymeetings.fyi"
    )
    public_info_rss = build_rss(
        public_info,
        [record_to_episode(town_hall)] if matches_feed(town_hall, public_info) else [],
        "audio",
        "https://www.citymeetings.fyi",
    )
    assert town_hall["uid"] in bond_rss
    assert town_hall["audio"]["url"] in bond_rss
    assert town_hall["uid"] not in public_info_rss
    assert town_hall["audio"]["url"] not in public_info_rss

    same_body_other_guid = dict(town_hall, provider_guid="272005")
    assert not matches_feed(same_body_other_guid, public_info)
    assert matches_feed(same_body_other_guid, bond)
    assert town_hall == original_town_hall
    assert september19 == original_september19
