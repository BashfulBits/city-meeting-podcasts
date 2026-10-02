"""Replay the full-label archive census against the aggregate TIF feed policy."""

import json
from pathlib import Path

import pytest

from citypods.bodies import matches, source_body_filter, source_body_inclusions
from citypods.config import load_city_configs
from citypods.records import source_key

ROOT = Path(__file__).resolve().parents[1]
CENSUS = json.loads((ROOT / "tests/fixtures/tif_coverage/archives.json").read_text())


@pytest.mark.parametrize("city,expected_rows", [("dallas-tx", 284), ("fort-worth-tx", 32)])
def test_tif_archive_coverage_and_migration(city, expected_rows):
    feeds = load_city_configs(ROOT / "config", {})
    aggregate = next(feed for feed in feeds if feed.slug == f"{city}-tif")
    census = CENSUS["cities"][city]
    selector = source_body_filter(aggregate.source)
    inclusions = {item.provider_guid for item in source_body_inclusions(aggregate.source)}
    included = 0
    for row in census["labels"]:
        selected = matches(row["body"], selector) or any(
            episode["provider_guid"] in inclusions for episode in row["episodes"]
        )
        assert selected == row["expected_tif"], row["body"]
        if selected:
            included += row["count"]
    assert included == expected_rows
    assert sum(row["count"] for row in census["labels"]) == census["total_provider_rows"]
    assert source_key(aggregate) == census["source_key"]
    assert set(aggregate.aliases) == set(census["former_slugs"])
    assert not set(aggregate.aliases) & {feed.slug for feed in feeds}


@pytest.mark.parametrize("city", ["dallas-tx", "fort-worth-tx"])
def test_tif_selectors_handle_future_variants_without_substring_false_positives(city):
    feed = next(
        feed for feed in load_city_configs(ROOT / "config", {}) if feed.slug == f"{city}-tif"
    )
    selector = source_body_filter(feed.source)
    for label in [
        "TIF 22 Board Special Meeting on 2027-01-01",
        "New District TIF Board of Directors",
        "Tax Increment Financing District Joint Board Meeting",
        "Reinvestment Zone Number 22 Board",
        "TIRZ 22 Board Meeting",
    ]:
        assert matches(label, selector), label
    for label in [
        "AATC Multifamily Summit",
        "Keep Fort Worth Beautiful Neighborhood Workshop",
        "Grow a Green Multifamily Development Symposium",
        "Public Improvement District Board",
        "Dallas Development Fund",
    ]:
        assert not matches(label, selector), label
