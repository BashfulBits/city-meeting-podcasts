"""Keep Dallas CBTF recordings in the bond-program feed by body-name rule."""

import json
from pathlib import Path

from citypods.bodies import (
    is_excluded,
    record_matches_body,
    source_body_filter,
    source_body_inclusions,
)
from citypods.config import load_city_configs, load_site_config
from citypods.feeds import build_rss
from citypods.records import record_to_episode

ROOT = Path(__file__).resolve().parents[1]
BOND_SLUG = "dallas-tx-bond-program-meetings"
TASK_FORCE_SLUG = "dallas-tx-2024-community-bond-task-force"
PUBLIC_INFO_SLUG = "dallas-tx-public-info-meetings"


def _configs():
    return {
        feed.slug: feed
        for feed in load_city_configs(
            ROOT / "config", load_site_config(ROOT / "config/site_config.yml")
        )
    }


def _owns(feed, record):
    if is_excluded(record.get("body"), feed.body_exclude):
        return False
    return record_matches_body(
        record,
        source_body_filter(feed.source),
        source_body_inclusions(feed.source),
    )


def test_dallas_cbtf_records_route_to_bond_program_without_public_info_expansion():
    configs = _configs()
    bond = configs[BOND_SLUG]
    task_force = configs[TASK_FORCE_SLUG]
    public_info = configs[PUBLIC_INFO_SLUG]
    rows = json.loads((ROOT / "tests/fixtures/dallas-bond-program-cbtf-retained.json").read_text())[
        "episodes"
    ]
    expected_bond_guids = {
        "273327",
        "272574",
        "277589",
        "233037",
        "246971",
        "269278",
        "280220",
        "269916",
        "259822",
        "272005",
    }
    expected_task_force_guids = expected_bond_guids - {"273327", "272574"}

    assert {row["provider_guid"] for row in rows} == expected_bond_guids
    assert all(_owns(bond, row) for row in rows)
    assert {
        row["provider_guid"] for row in rows if _owns(task_force, row)
    } == expected_task_force_guids

    by_guid = {row["provider_guid"]: row for row in rows}
    assert record_matches_body(
        by_guid["272574"],
        source_body_filter(public_info.source),
        source_body_inclusions(public_info.source),
    )
    assert not record_matches_body(
        by_guid["273327"],
        source_body_filter(public_info.source),
        source_body_inclusions(public_info.source),
    )

    xml = build_rss(
        bond,
        [record_to_episode(row) for row in rows],
        "audio",
        "https://www.citymeetings.fyi",
    )
    for row in rows:
        assert row["uid"] in xml
        assert row["audio"]["url"] in xml
