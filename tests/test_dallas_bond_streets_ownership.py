"""Complete institution labels separate bond Streets recurrence from standing bodies."""

import copy
import json
from pathlib import Path

from citypods.bodies import record_matches_body, source_body_filter, source_body_inclusions
from citypods.config import load_city_configs
from citypods.feeds import build_rss
from citypods.records import record_to_episode


def test_streets_institution_rules_preserve_recordings_joints_and_exceptions():
    root = Path(__file__).resolve().parents[1]
    configs = {c.slug: c for c in load_city_configs(root / "config", {})}
    standing = configs["dallas-tx-transportation-and-infrastructure"]
    bond = configs["dallas-tx-2024-bond-streets-and-transportation-subcommittee"]
    records = json.loads((root / "tests/fixtures/dallas-bond-streets-retained.json").read_text())
    original = copy.deepcopy(records)

    def owns(config, row):
        return record_matches_body(
            row, source_body_filter(config.source), source_body_inclusions(config.source)
        )

    approved = {"269407", "270975", "277827", "277830"}
    for record in records:
        assert owns(bond, record)
        assert owns(standing, record) == (str(record["provider_guid"]) in approved)
        future = dict(record, provider_guid="future-occurrence")
        assert owns(bond, future)
        assert not owns(standing, future)
    for label in standing.source["body_exact"]:
        row = dict(records[0], body=label, provider_guid="standing-occurrence")
        assert owns(standing, row)
        assert not owns(bond, row)
    for inclusion in standing.source["body_includes"]:
        row = dict(records[0], **inclusion)
        assert owns(standing, row)
        assert owns(bond, row) == (str(inclusion["provider_guid"]) in approved)
        if inclusion["body"] not in standing.source["body_exact"]:
            assert not owns(standing, dict(row, provider_guid="unproved-exception"))
    for label in ("2024 Bond Task Force Flood", "Unrelated Transportation Event"):
        row = dict(records[0], body=label, provider_guid="unrelated")
        assert not owns(standing, row)
        assert not owns(bond, row)
    rss = build_rss(
        bond, [record_to_episode(r) for r in records], "audio", "https://www.citymeetings.fyi"
    )
    for record in records:
        assert record["uid"] in rss
        assert record["audio"]["url"] in rss
    standing_rss = build_rss(
        standing,
        [record_to_episode(r) for r in records if str(r["provider_guid"]) in approved],
        "audio",
        "https://www.citymeetings.fyi",
    )
    for record in records:
        if str(record["provider_guid"]) in approved:
            assert record["uid"] in standing_rss
            assert record["audio"]["url"] in standing_rss
    assert records == original
