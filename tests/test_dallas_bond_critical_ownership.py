"""Critical Facilities institution routing preserves retained recording identity."""

import copy
import json
from pathlib import Path

from citypods.bodies import record_matches_body, source_body_filter, source_body_inclusions
from citypods.config import load_city_configs
from citypods.feeds import build_rss
from citypods.records import record_to_episode


def test_critical_institution_preserves_identity_and_rejects_near_labels():
    """The complete institution alias accepts future recordings without topic leakage."""
    root = Path(__file__).resolve().parents[1]
    cities = load_city_configs(root / "config", {})
    slug = "dallas-tx-2024-bond-critical-facilities-subcommittee"
    feed = next(c for c in cities if c.slug == slug)
    record = json.loads((root / "tests/fixtures/dallas-bond-critical-retained.json").read_text())
    original = copy.deepcopy(record)

    def matches(candidate, city=feed):
        return record_matches_body(
            candidate, source_body_filter(city.source), source_body_inclusions(city.source)
        )

    assert matches(record)
    assert matches(dict(record, provider_guid="future-critical-recording"))
    for city in cities:
        if city.city_entity == "dallas-tx" and city.slug != slug:
            assert not matches(record, city), city.slug
    for body in (
        record["body"] + " Public Town Hall",
        "City Council Critical Facilities Briefing",
        "Critical Facilities",
        "2024 Bond Task Force Critical Facilities",
    ):
        assert not matches(dict(record, provider_guid="future-critical-recording", body=body))
    rss = build_rss(feed, [record_to_episode(record)], "audio", "https://www.citymeetings.fyi")
    assert record["uid"] in rss
    assert record["audio"]["url"] in rss
    assert record == original
