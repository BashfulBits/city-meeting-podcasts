"""A verified task-force proceeding retains recording identity in its own feed."""

import copy
import json
from pathlib import Path

import pytest

from citypods.bodies import record_matches_body, source_body_filter, source_body_inclusions
from citypods.config import load_city_configs
from citypods.feeds import build_rss
from citypods.records import record_to_episode


def test_actual_config_keeps_cbtf_only_in_its_task_force_feed():
    """Actual selectors retain original RSS identity and reject Council leakage."""
    root = Path(__file__).resolve().parents[1]
    cities = load_city_configs(root / "config", {})
    council = next(c for c in cities if c.slug == "dallas-tx-city-council")
    commission = next(c for c in cities if c.slug == "dallas-tx-2024-community-bond-task-force")
    record = json.loads((root / "tests/fixtures/dallas-cbtf-retained.json").read_text())
    original = copy.deepcopy(record)
    assert not record_matches_body(
        record, source_body_filter(council.source), source_body_inclusions(council.source)
    )
    assert record_matches_body(
        record, source_body_filter(commission.source), source_body_inclusions(commission.source)
    )
    rss = build_rss(
        commission, [record_to_episode(record)], "audio", "https://www.citymeetings.fyi"
    )
    assert record["uid"] in rss
    assert record["audio"]["url"] in rss
    negative = dict(record, provider_guid="unrelated")
    assert not record_matches_body(
        negative, source_body_filter(commission.source), source_body_inclusions(commission.source)
    )
    assert record == original


@pytest.mark.parametrize(
    "uid",
    ["96c10f4f75706719", "aaf0ad8eaabe68a5", "130a7ebded8e0e1d", "cba1e023051d7c94"],
)
def test_additional_verified_cbtf_record_keeps_original_identity(uid):
    """Each exact recording gains coverage without broadening body-name admission."""
    root = Path(__file__).resolve().parents[1]
    cities = load_city_configs(root / "config", {})
    council = next(c for c in cities if c.slug == "dallas-tx-city-council")
    task_force = next(c for c in cities if c.slug == "dallas-tx-2024-community-bond-task-force")
    records = json.loads((root / "tests/fixtures/dallas-cbtf-additional-retained.json").read_text())
    record = next(r for r in records if r["uid"] == uid)
    original = copy.deepcopy(record)
    assert record_matches_body(
        record, source_body_filter(task_force.source), source_body_inclusions(task_force.source)
    )
    assert not record_matches_body(
        record, source_body_filter(council.source), source_body_inclusions(council.source)
    )
    rss = build_rss(
        task_force, [record_to_episode(record)], "audio", "https://www.citymeetings.fyi"
    )
    assert uid in rss
    assert record["audio"]["url"] in rss
    negative = dict(record, provider_guid="unverified-recording")
    assert not record_matches_body(
        negative, source_body_filter(task_force.source), source_body_inclusions(task_force.source)
    )
    assert record == original
