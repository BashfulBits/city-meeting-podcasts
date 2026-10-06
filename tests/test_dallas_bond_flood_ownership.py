"""The complete Flood subcommittee institution label admits stable recurrences."""

import copy
import json
from pathlib import Path

from citypods.bodies import record_matches_body, source_body_filter, source_body_inclusions
from citypods.config import load_city_configs
from citypods.feeds import build_rss
from citypods.records import record_to_episode


def test_flood_institution_rule_preserves_recording_identity():
    root = Path(__file__).resolve().parents[1]
    configs = {c.slug: c for c in load_city_configs(root / "config", {})}
    bond = configs["dallas-tx-2024-bond-flood-subcommittee"]
    records = json.loads((root / "tests/fixtures/dallas-bond-flood-retained.json").read_text())
    original = copy.deepcopy(records)
    assert len(records) == 1
    record = records[0]
    assert record["uid"] == "8280ec97799c999e"
    assert record["provider_guid"] == "269612"

    def owns(row):
        return record_matches_body(
            row, source_body_filter(bond.source), source_body_inclusions(bond.source)
        )

    assert owns(record)
    assert owns(dict(record, provider_guid="future-occurrence"))
    for label in (
        "City Council",
        "Council Flood Briefing",
        "Flood Control",
        "2024 Bond Task Force Streets",
        "2024 Bond Task Force Critical Facilities",
    ):
        assert not owns(dict(record, body=label))
    rss = build_rss(bond, [record_to_episode(record)], "audio", "https://www.citymeetings.fyi")
    assert record["uid"] in rss
    assert record["audio"]["url"] in rss
    assert records == original
