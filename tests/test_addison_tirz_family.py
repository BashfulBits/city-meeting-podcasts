"""Actual-config regressions for Addison's separately convened TIRZ board."""

import copy
import json
from pathlib import Path

import defusedxml.ElementTree as DET

from citypods import bodies
from citypods.config import load_city_configs
from citypods.feeds import build_rss
from citypods.publication_selection import load_selection_index, select_feed_publication
from citypods.records import record_to_episode, source_key
from citypods.search import _city_for_record

ROOT = Path(__file__).resolve().parents[1]
UID = "757ab2a4aaf9bbca"


def retained():
    return json.loads((ROOT / "tests/fixtures/addison-tirz-retained.json").read_text())["records"]


def addison_cities():
    return [c for c in load_city_configs(ROOT / "config", {}) if c.city_entity == "addison-tx"]


def selects(city, record):
    return bodies.record_matches_body(
        record, bodies.source_body_filter(city.source), bodies.source_body_inclusions(city.source)
    )


def test_addison_tirz_retains_board_recording_and_audio():
    """TIF family projects the original dated board recording without rewriting it."""
    cities = addison_cities()
    tif = next(c for c in cities if c.slug == "addison-tx-tif")
    records = retained()
    original = copy.deepcopy(records)
    record = records[UID]
    assert source_key(tif) == "abbf5e25e078"
    assert tif.extra["meeting_family"] == "tif"
    assert tif.extra["remedy_policy"] == {"aggregate_family": "tif", "member_names": ["TIRZ #1"]}
    assert tif.podcast_title == "Addison: TIF Meetings"
    assert record["provider_guid"] == "394474"
    assert record["published"] == "2026-07-28T00:00:00+00:00"
    assert selects(tif, record)
    plan = select_feed_publication(
        load_selection_index(cities), tif, [record_to_episode(record)], records
    )
    assert not plan.held
    assert set(plan.selected_uids) == {UID}
    xml = build_rss(tif, list(plan.public_items), "audio", "https://www.citymeetings.fyi")
    item = DET.fromstring(xml).find("channel/item")
    assert item is not None
    assert item.findtext("guid") == UID
    assert item.findtext("title") == record["title"]
    assert item.find("enclosure").attrib["url"] == record["audio"]["url"]
    assert records == original


def test_addison_tirz_exact_board_identity_excludes_council_topics():
    """A Council TIRZ topic and unproved board aliases cannot enter the TIF feed."""
    tif = next(c for c in addison_cities() if c.slug == "addison-tx-tif")
    record = retained()[UID]
    for body in (
        "City Council",
        "City Council Work Session - TIRZ #1 Board Meeting",
        "TIRZ #1 Board Meeting Presentation",
        "TIRZ #2 Board Meeting",
        "Tax Increment Reinvestment Zone",
    ):
        candidate = {**record, "provider_guid": "unproved", "body": body}
        assert not selects(tif, candidate), body
    assert selects(tif, {**record, "body": "TIRZ #1 BOARD MEETING"})


def test_addison_tirz_moves_from_council_to_board_search_owner():
    """Separately convened board belongs to TIF alone, with corrected canonical owner."""
    cities = addison_cities()
    record = retained()[UID]
    assert {c.slug for c in cities if selects(c, record)} == {"addison-tx-tif"}
    assert _city_for_record(sorted(cities, key=lambda c: c.slug), record).slug == "addison-tx-tif"
