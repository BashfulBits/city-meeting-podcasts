"""Official standalone committee proceedings retain their original media and aggregate feed."""

import copy
import json
from pathlib import Path

from citypods.bodies import record_matches_body, source_body_filter, source_body_inclusions
from citypods.config import load_city_configs
from citypods.feeds import build_rss
from citypods.records import record_to_episode


def test_actual_arlington_committee_ownership_and_original_audio():
    """Four original recordings leave Council while remaining in committee and All Meetings."""
    root = Path(__file__).resolve().parents[1]
    cities = load_city_configs(root / "config", {})
    council = next(c for c in cities if c.slug == "arlington-tx-council")
    aggregate = next(c for c in cities if c.slug == "arlington-tx")
    committee = next(
        c
        for c in cities
        if c.slug == "arlington-tx-community-and-neighborhood-development-committee"
    )
    records = json.loads((root / "tests/fixtures/arlington-cnd-retained.json").read_text())
    original = copy.deepcopy(records)
    assert len(records) == 4
    for record in records.values():
        assert not record_matches_body(
            record, source_body_filter(council.source), source_body_inclusions(council.source)
        )
        for city in (committee, aggregate):
            assert record_matches_body(
                record, source_body_filter(city.source), source_body_inclusions(city.source)
            )
            rss = build_rss(city, [record_to_episode(record)], "audio", "https://citymeetings.fyi")
            assert record["uid"] in rss
            assert record["audio"]["url"] in rss
        wrong = dict(record, body="Council discussion of Community and Neighborhood Development")
        assert not record_matches_body(
            wrong, source_body_filter(committee.source), source_body_inclusions(committee.source)
        )
    assert records == original
