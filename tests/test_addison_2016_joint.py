"""One officially verified joint keeps both subscriptions and its original media identity."""

import copy
import json
from pathlib import Path

from citypods.bodies import record_matches_body, source_body_filter, source_body_inclusions
from citypods.config import load_city_configs
from citypods.feeds import build_rss
from citypods.records import record_to_episode
from citypods.search import _city_for_record


def test_actual_config_restores_only_verified_joint_council_subscription():
    """Council and P&Z expose the same UID/audio; an unrelated GUID is not admitted."""
    root = Path(__file__).resolve().parents[1]
    cities = [c for c in load_city_configs(root / "config", {}) if c.city_entity == "addison-tx"]
    record = json.loads((root / "tests/fixtures/addison-2016-joint.json").read_text())
    original = copy.deepcopy(record)
    council = next(c for c in cities if c.slug == "addison-tx-city-council")
    pz = next(c for c in cities if c.slug == "addison-tx-planning-and-zoning-commission")
    for city in (council, pz):
        assert record_matches_body(
            record, source_body_filter(city.source), source_body_inclusions(city.source)
        )
        rss = build_rss(city, [record_to_episode(record)], "audio", "https://www.citymeetings.fyi")
        assert record["uid"] in rss
        assert record["audio"]["url"] in rss
    assert _city_for_record(cities, record).slug == council.slug
    unrelated = {**record, "provider_guid": "another-guid"}
    assert not record_matches_body(
        unrelated, source_body_filter(council.source), source_body_inclusions(council.source)
    )
    assert record == original
