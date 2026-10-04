"""Verified temporary Bond proceedings gain subscriptions without rewriting recordings."""

import copy
import json
from pathlib import Path

import defusedxml.ElementTree as DET
import pytest

from citypods import bodies
from citypods.config import load_city_configs
from citypods.feeds import build_rss
from citypods.publication_selection import load_selection_index, select_feed_publication
from citypods.records import record_to_episode, source_key
from citypods.search import _city_for_record

ROOT = Path(__file__).resolve().parents[1]
BOND_SLUG = "addison-tx-bond-committees"


def test_verified_bond_family_preserves_rss_records_and_corrects_search_fallback():
    """All four official meetings retain audio and become Bond-owned rather than BZA fallback."""
    cities = sorted(
        (c for c in load_city_configs(ROOT / "config", {}) if c.city_entity == "addison-tx"),
        key=lambda c: c.slug,
    )
    bond = next(c for c in cities if c.slug == BOND_SLUG)
    records = json.loads((ROOT / "tests/fixtures/addison-bond-retained.json").read_text())[
        "records"
    ]
    original = copy.deepcopy(records)
    assert {r["provider_guid"] for r in records.values()} == {
        "359742",
        "361917",
        "362796",
        "371625",
    }
    assert source_key(bond) == "abbf5e25e078"
    assert bond.extra["meeting_family"] == "bond"
    assert bond.podcast_title == "Addison: Bond Committees"
    index = load_selection_index(cities)
    prior_candidates = [c for c in cities if c.slug != BOND_SLUG]
    for uid, record in records.items():
        memberships = {
            c.slug
            for c in cities
            if bodies.record_matches_body(
                record, bodies.source_body_filter(c.source), bodies.source_body_inclusions(c.source)
            )
        }
        assert memberships == {BOND_SLUG}
        assert (
            _city_for_record(prior_candidates, record).slug
            == "addison-tx-board-of-zoning-adjustment"
        )
        assert _city_for_record(cities, record).slug == BOND_SLUG
        assert record["uid"] == uid
    plan = select_feed_publication(
        index, bond, [record_to_episode(r) for r in records.values()], records
    )
    assert not plan.held
    assert set(plan.selected_uids) == set(records)
    xml = build_rss(bond, list(plan.public_items), "audio", "https://www.citymeetings.fyi")
    channel = DET.fromstring(xml).find("channel")
    rss_items = {item.find("guid").text: item for item in channel.findall("item")}
    assert set(rss_items) == set(records)
    for uid, item in rss_items.items():
        assert item.find("title").text == records[uid]["title"]
        assert item.find("enclosure").get("url") == records[uid]["audio"]["url"]
    assert records == original


@pytest.mark.parametrize(
    "label",
    [
        "City Council Work Session",
        "Bond Election Information",
        "Bond Advisory Committee Presentation",
        "Other City Bond Advisory Committee",
        "Planning and Zoning Commission",
    ],
)
def test_bond_family_does_not_classify_reports_or_other_bodies(label):
    """Exact convened-body proof excludes Council reports and topic-only near matches."""
    bond = next(c for c in load_city_configs(ROOT / "config", {}) if c.slug == BOND_SLUG)
    assert not bodies.record_matches_body(
        {"body": label, "provider_guid": "unreviewed"},
        bodies.source_body_filter(bond.source),
        bodies.source_body_inclusions(bond.source),
    )
