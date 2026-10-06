"""Institution rules separate bond subcommittee recurrence from standing Council bodies."""

import copy
import json
from pathlib import Path

from citypods.bodies import record_matches_body, source_body_filter, source_body_inclusions
from citypods.config import load_city_configs
from citypods.feeds import build_rss
from citypods.records import record_to_episode


def test_economic_institution_rules_preserve_identity_and_recurring_labels():
    root = Path(__file__).resolve().parents[1]
    configs = {c.slug: c for c in load_city_configs(root / "config", {})}
    standing = configs["dallas-tx-economic-development"]
    bond = configs["dallas-tx-2024-bond-economic-development-subcommittee"]
    record = json.loads((root / "tests/fixtures/dallas-bond-economic-retained.json").read_text())
    original = copy.deepcopy(record)

    def owns(config, row):
        return record_matches_body(
            row, source_body_filter(config.source), source_body_inclusions(config.source)
        )

    assert owns(bond, record)
    assert not owns(standing, record)
    assert owns(bond, dict(record, provider_guid="future-occurrence"))
    for label in standing.source["body_exact"]:
        row = dict(record, body=label, provider_guid="standing-occurrence")
        assert owns(standing, row)
        assert not owns(bond, row)
    for label in (
        "2024 Bond Task Force Flood",
        "2024 Bond Task Force Streets and Transportation Subcommittee",
        "Unrelated Economic Development Event",
    ):
        row = dict(record, body=label, provider_guid="unrelated")
        assert not owns(standing, row)
        assert not owns(bond, row)
    rss = build_rss(bond, [record_to_episode(record)], "audio", "https://www.citymeetings.fyi")
    assert record["uid"] in rss
    assert record["audio"]["url"] in rss
    assert record == original
