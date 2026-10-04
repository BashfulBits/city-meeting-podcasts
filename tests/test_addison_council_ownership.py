"""Retained Council ownership and subscription identity after exact-label narrowing."""

import copy
import json
from pathlib import Path

from citypods.bodies import record_matches_body, source_body_filter, source_body_inclusions
from citypods.config import load_city_configs
from citypods.feeds import build_rss
from citypods.records import record_to_episode
from citypods.search import _city_for_record

ROOT = Path(__file__).resolve().parents[1]
COUNCIL = "addison-tx-city-council"
PZ = "addison-tx-planning-and-zoning-commission"


def _inputs():
    cities = sorted(
        (c for c in load_city_configs(ROOT / "config", {}) if c.city_entity == "addison-tx"),
        key=lambda c: c.slug,
    )
    records = json.loads((ROOT / "tests/fixtures/addison-council-retained.json").read_text())[
        "records"
    ]
    return cities, records


def _matches(city, record):
    return record_matches_body(
        record, source_body_filter(city.source), source_body_inclusions(city.source)
    )


def test_council_replay_removes_only_commission_work_sessions_and_ceremony():
    """Former Council rows retain identity while wrong-body subscriptions leave Council."""
    cities, records = _inputs()
    original = copy.deepcopy(records)
    council = next(c for c in cities if c.slug == COUNCIL)
    pz = next(c for c in cities if c.slug == PZ)
    selected = {uid for uid, record in records.items() if _matches(council, record)}
    assert len(records) == 549
    assert len(selected) == 500
    removed = set(records) - selected
    work_sessions = {
        uid
        for uid, record in records.items()
        if record["body"] == "Planning & Zoning Commission Work Session"
    }
    assert len(work_sessions) == 47
    assert removed == work_sessions | {"1f50e2274a6dd26c", "757ab2a4aaf9bbca"}
    for uid in work_sessions:
        record = records[uid]
        assert _matches(pz, record)
        assert _city_for_record(cities, record).slug == PZ
    for uid in selected:
        record = records[uid]
        assert _city_for_record(cities, record).slug == COUNCIL
    assert records == original


def test_exact_council_rules_preserve_joint_rss_uid_and_existing_audio():
    """Genuine joints stay in Council and P&Z with their original enclosure identities."""
    cities, records = _inputs()
    original = copy.deepcopy(records)
    council = next(c for c in cities if c.slug == COUNCIL)
    pz = next(c for c in cities if c.slug == PZ)
    joints = [record for record in records.values() if record["body"].startswith("Joint ")]
    assert len(joints) == 10
    for record in joints:
        assert _matches(council, record)
        assert _matches(pz, record)
        assert _city_for_record(cities, record).slug == COUNCIL
        xml = build_rss(
            council, [record_to_episode(record)], "audio", "https://www.citymeetings.fyi"
        )
        assert record["uid"] in xml
        assert record["audio"]["url"] in xml
    assert records == original


def test_council_exact_aliases_reject_other_bodies_and_unreviewed_years():
    """Reviewed complete labels accept normalized copies without accepting topic suffixes."""
    cities, _ = _inputs()
    council = next(c for c in cities if c.slug == COUNCIL)
    assert "body" not in council.source
    assert "body_any" not in council.source
    assert len(council.source["body_exact"]) == 25
    for label in council.source["body_exact"]:
        record = {"provider_guid": "new-reviewed-label", "body": label}
        assert _matches(council, record)
        assert _matches(council, {**record, "body": f"{label} {label}"})
        assert not _matches(council, {**record, "body": f"{label} Advisory Committee"})
    for label in (
        "Planning & Zoning Commission Work Session",
        "City Council Swearing-in Ceremony",
        "Bond Advisory Committee",
        "Work Session of the Finance Committee",
        "Fiscal Year 2027-2028 Budget Workshop",
        "2027 City Council Strategic Planning Session",
    ):
        assert not _matches(council, {"provider_guid": "not-a-guarded-row", "body": label})
