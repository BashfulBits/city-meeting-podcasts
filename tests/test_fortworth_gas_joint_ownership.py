"""Exact official joint workshop slates prove both participants; preserve original audio."""

import copy
import json
from pathlib import Path

from citypods.bodies import record_matches_body, source_body_filter, source_body_inclusions
from citypods.config import load_city_configs
from citypods.feeds import build_rss
from citypods.records import record_to_episode


def test_gas_task_force_joint_retains_both_original_council_recordings():
    root = Path(__file__).resolve().parents[1]
    configs = {c.slug: c for c in load_city_configs(root / "config", {})}
    task_force = configs["fort-worth-tx-gas-drilling-task-force"]
    council = configs["fort-worth-tx-city-council"]
    rows = json.loads((root / "tests/fixtures/fortworth-gas-joint-retained.json").read_text())
    original = copy.deepcopy(rows)

    def owns(config, record):
        return record_matches_body(
            record, source_body_filter(config.source), source_body_inclusions(config.source)
        )

    assert {row["uid"] for row in rows} == {"0d5832dbd08e6dd8", "27254aa1a700569f"}
    for row in rows:
        assert owns(task_force, row)
        assert owns(council, row)
        assert owns(task_force, dict(row, provider_guid="future-same-proven-joint-alias"))
        for label in (
            "City Council Gas Drilling Workshop City Council Gas Drilling Workshop",
            "Gas Ordinance Recommendations Meeting Gas Ordinance Recommendations Meeting",
            "City Council",
            "Gas Drilling Task Force Workshop",
            row["body"] + " public town hall",
        ):
            assert not owns(task_force, dict(row, body=label, provider_guid="unproved-body"))
    for config in (task_force, council):
        rss = build_rss(
            config,
            [record_to_episode(row) for row in rows],
            "audio",
            "https://www.citymeetings.fyi",
        )
        for row in rows:
            assert row["uid"] in rss
            assert row["audio"]["url"] in rss
    assert task_force.source["feed_urls"] == council.source["feed_urls"]
    assert rows == original
