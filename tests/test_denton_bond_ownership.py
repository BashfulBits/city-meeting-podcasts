"""A verified Bond committee proceeding leaves Council without changing recording identity."""

import copy
import json
from pathlib import Path

from citypods.bodies import record_matches_body, source_body_filter, source_body_inclusions
from citypods.config import load_city_configs
from citypods.feeds import build_rss
from citypods.records import record_to_episode


def test_actual_config_keeps_bond_committee_out_of_council():
    """Actual selectors retain original RSS identity and reject Council leakage."""
    root = Path(__file__).resolve().parents[1]
    cities = load_city_configs(root / "config", {})
    council = next(c for c in cities if c.slug == "denton-tx-city-council")
    committee = next(
        c for c in cities if c.slug == "denton-tx-special-citizens-bond-advisory-committee"
    )
    records = json.loads((root / "tests/fixtures/denton-bond-retained.json").read_text())
    for record in records:
        original = copy.deepcopy(record)
        assert not record_matches_body(
            record, source_body_filter(council.source), source_body_inclusions(council.source)
        )
        assert record_matches_body(
            record, source_body_filter(committee.source), source_body_inclusions(committee.source)
        )
        rss = build_rss(
            committee, [record_to_episode(record)], "audio", "https://www.citymeetings.fyi"
        )
        assert record["uid"] in rss
        assert record["audio"]["url"] in rss
        negative = dict(record, provider_guid="unrelated")
        assert not record_matches_body(
            negative,
            source_body_filter(committee.source),
            source_body_inclusions(committee.source),
        )
        assert record == original
